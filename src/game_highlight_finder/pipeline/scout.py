"""M3 offline Scout stage: raw fixture -> validated canonical SessionMap."""

from __future__ import annotations

import hashlib
import shutil
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from game_highlight_finder.config import AppConfig
from game_highlight_finder.domain.canonical import canonicalize_scout_response, parse_scout_response
from game_highlight_finder.domain.models import (
    ArtifactIdentity,
    ErrorRecord,
    SessionMap,
    SourceAsset,
    StageStatus,
    model_json,
)
from game_highlight_finder.errors import AppError, ErrorCategory, ValidationError
from game_highlight_finder.logging import RunLogger
from game_highlight_finder.pipeline.fake_scout import FakeScout, FakeScoutOutput
from game_highlight_finder.pipeline.local_signals import LocalSignalsResult
from game_highlight_finder.pipeline.manifest import (
    complete_stage,
    ensure_m3_stages,
    fail_stage,
    recover_interrupted,
    start_stage,
)
from game_highlight_finder.pipeline.proxy import ProxyResult
from game_highlight_finder.storage.atomic import atomic_write_bytes, atomic_write_json, read_json
from game_highlight_finder.storage.lock import SessionLock
from game_highlight_finder.storage.sessions import (
    artifact_identity,
    completed_stage_cache_is_valid,
    compute_scout_cache_key,
    load_manifest,
    session_paths,
    write_manifest,
)


class ScoutResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    session_id: str
    backend: str
    cache_hit: bool
    cache_reason: str
    raw_path: Path
    canonical_path: Path
    session_map_path: Path
    session_map: SessionMap
    session_dir: Path


def generate_scout(
    source: SourceAsset,
    proxy: ProxyResult,
    local_signals: LocalSignalsResult,
    config: AppConfig,
) -> ScoutResult:
    """Run the configured Scout backend behind one persistence boundary."""

    if config.scout.backend == "gemini":
        from game_highlight_finder.pipeline.gemini_scout import generate_gemini_scout

        return generate_gemini_scout(source, proxy, local_signals, config)

    expected_session_id = source_session_id(source)
    if proxy.session_id != expected_session_id or local_signals.session_id != expected_session_id:
        raise ValidationError("Scout inputs belong to different sessions.")
    paths = session_paths(config.storage.data_dir, proxy.session_id)
    for directory in (paths.scout_raw_dir, paths.scout_canonical_dir, paths.tmp_dir):
        directory.mkdir(parents=True, exist_ok=True)
    fixture = FakeScout(config.scout.fixture_path, max_bytes=config.scout.response_max_bytes)
    fixture_sha256 = fixture.fixture_sha256()
    proxy_sha, proxy_size, signals_sha, signals_size = _upstream_artifact_hashes(
        proxy, local_signals
    )
    cache_key = compute_scout_cache_key(
        source,
        config,
        proxy_artifact_sha256=proxy_sha,
        local_signals_artifact_sha256=signals_sha,
        fixture_sha256=fixture_sha256,
    )
    raw_path = paths.scout_raw_dir / "fake_response.json"
    canonical_path = paths.scout_canonical_dir / "scout_result.json"
    log = RunLogger(_log_path(paths.logs))

    with SessionLock(paths.lock):
        manifest = load_manifest(paths.manifest)
        changed = ensure_m3_stages(manifest)
        if recover_interrupted(manifest):
            changed = True
        if changed:
            write_manifest(paths.manifest, manifest)
        valid, reason = completed_stage_cache_is_valid(
            paths, manifest, stage_name="scout", expected_cache_key=cache_key
        )
        if valid:
            session_map = load_session_map(paths.session_map)
            return ScoutResult(
                session_id=manifest.session_id,
                backend="fake",
                cache_hit=True,
                cache_reason=reason,
                raw_path=raw_path,
                canonical_path=canonical_path,
                session_map_path=paths.session_map,
                session_map=session_map,
                session_dir=paths.root,
            )
        stage = manifest.stages["scout"]
        if stage.status is StageStatus.COMPLETED:
            stage.status = StageStatus.STALE
            stage.reason = reason
        run_id = start_stage(manifest, "scout", cache_key)
        write_manifest(paths.manifest, manifest)
        temp_dir = paths.tmp_dir / run_id
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_raw = temp_dir / "fake_response.json"
        temp_canonical = temp_dir / "scout_result.json"
        temp_map = temp_dir / "session_map.json"
        log.write("INFO", "scout_started", "Fake Scout stage started.", run_id=run_id)
        try:
            # Commit raw bytes first. Even a malformed response remains immutable evidence
            # for the failed attempt and can be re-canonicalized without rerunning a provider.
            output = _reuse_raw_or_generate(
                raw_path,
                fixture,
                fixture_sha256=fixture_sha256,
                source_duration_ms=source.duration_ms,
                source_sha256=source.sha256,
                max_bytes=config.scout.response_max_bytes,
            )
            atomic_write_bytes(temp_raw, output.raw_bytes)
            temp_raw.replace(raw_path)
            session_map = canonicalize_scout_response(
                output.raw_bytes,
                session_id=source_session_id(source),
                source_id=source.source_id,
                source_duration_ms=source.duration_ms,
                game_profile="unknown",
                source_offset_ms=0,
                created_at=source.created_at,
                max_response_bytes=config.scout.response_max_bytes,
            )
            atomic_write_json(temp_canonical, model_json(session_map))
            atomic_write_json(temp_map, model_json(session_map))
            temp_canonical.replace(canonical_path)
            temp_map.replace(paths.session_map)
            inputs = [
                ArtifactIdentity(
                    path="proxy/analysis_proxy.mp4", sha256=proxy_sha, size_bytes=proxy_size
                ),
                ArtifactIdentity(
                    path="signals/activity.json", sha256=signals_sha, size_bytes=signals_size
                ),
            ]
            outputs = [
                artifact_identity(raw_path, relative_to=paths.root),
                artifact_identity(canonical_path, relative_to=paths.root),
                artifact_identity(paths.session_map, relative_to=paths.root),
            ]
            complete_stage(
                manifest,
                "scout",
                inputs=inputs,
                outputs=outputs,
                item_states={"response": "COMPLETED", "canonical": "COMPLETED"},
            )
            write_manifest(paths.manifest, manifest)
            log.write("INFO", "scout_completed", "Fake Scout stage completed.", run_id=run_id)
            return ScoutResult(
                session_id=manifest.session_id,
                backend="fake",
                cache_hit=False,
                cache_reason=output.description,
                raw_path=raw_path,
                canonical_path=canonical_path,
                session_map_path=paths.session_map,
                session_map=session_map,
                session_dir=paths.root,
            )
        except BaseException as exc:
            error = _error_record(exc)
            try:
                fail_stage(manifest, "scout", error)
                write_manifest(paths.manifest, manifest)
                log.write("ERROR", "scout_failed", error.message, category=error.category)
            except Exception:
                pass
            raise
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


run_fake_scout = generate_scout


def load_session_map(path: Path) -> SessionMap:
    try:
        return SessionMap.model_validate(read_json(path))
    except Exception as exc:
        raise ValidationError("Stored canonical session map is invalid.", hint=str(exc)) from exc


def source_session_id(source: SourceAsset) -> str:
    from game_highlight_finder.storage.sessions import make_session_id

    return make_session_id(source)


def _upstream_artifact_hashes(
    proxy: ProxyResult, local_signals: LocalSignalsResult
) -> tuple[str, int, str, int]:
    from game_highlight_finder.storage.hashing import hash_file

    proxy_path = proxy.proxy_path
    signals_path = local_signals.signals_path
    if not proxy_path.is_file() or not signals_path.is_file():
        raise ValidationError("Scout requires committed proxy and local-signal artifacts.")
    return (
        hash_file(proxy_path),
        proxy_path.stat().st_size,
        hash_file(signals_path),
        signals_path.stat().st_size,
    )


def _reuse_raw_or_generate(
    raw_path: Path,
    fixture: FakeScout,
    *,
    fixture_sha256: str | None,
    source_duration_ms: int,
    source_sha256: str,
    max_bytes: int,
) -> FakeScoutOutput:
    """Prefer an existing raw artifact when only canonicalization needs rerunning."""

    if raw_path.is_file():
        try:
            if raw_path.stat().st_size <= max_bytes:
                raw = raw_path.read_bytes()
                raw_hash = hashlib.sha256(raw).hexdigest()
                syntactically_valid = True
                try:
                    parse_scout_response(raw, max_bytes=max_bytes)
                except ValidationError:
                    syntactically_valid = False
                if syntactically_valid and (fixture_sha256 is None or raw_hash == fixture_sha256):
                    return FakeScoutOutput(
                        raw_bytes=raw,
                        fixture_sha256=fixture_sha256,
                        description="raw-recanonicalized",
                    )
        except OSError:
            pass
    return fixture.generate(source_duration_ms=source_duration_ms, source_sha256=source_sha256)


def _log_path(logs: Path) -> Path:
    return logs / f"run-scout-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.jsonl"


def _error_record(exc: BaseException) -> ErrorRecord:
    if isinstance(exc, AppError):
        return ErrorRecord(
            category=exc.category.value,
            message=exc.message,
            hint=exc.hint,
            retryable=exc.category in {ErrorCategory.STORAGE, ErrorCategory.INTERNAL},
        )
    return ErrorRecord(
        category=ErrorCategory.INTERNAL.value,
        message="Unexpected error during Fake Scout generation.",
        hint=type(exc).__name__,
        retryable=False,
    )
