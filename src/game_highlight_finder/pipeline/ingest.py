"""Reliable, restart-safe M1 source ingest."""

from __future__ import annotations

import platform
import sys
from datetime import UTC, datetime
from os import stat_result
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from game_highlight_finder import __version__
from game_highlight_finder.config import AppConfig, config_hash, config_payload
from game_highlight_finder.domain.models import (
    ArtifactIdentity,
    ErrorRecord,
    SourceAsset,
    SourceLocator,
    StageStatus,
    model_json,
)
from game_highlight_finder.errors import AppError, ErrorCategory, SourceError, ValidationError
from game_highlight_finder.logging import RunLogger
from game_highlight_finder.media.ffprobe import parse_source_asset, run_ffprobe
from game_highlight_finder.media.tools import executable_version, require_executable
from game_highlight_finder.pipeline.manifest import (
    complete_ingest,
    fail_ingest,
    new_manifest,
    recover_interrupted,
    start_ingest,
)
from game_highlight_finder.storage.atomic import atomic_write_json
from game_highlight_finder.storage.hashing import hash_file
from game_highlight_finder.storage.lock import SessionLock
from game_highlight_finder.storage.sessions import (
    artifact_identity,
    completed_cache_is_valid,
    compute_ingest_cache_key,
    load_locator,
    load_manifest,
    make_session_id,
    safe_create_session_directories,
    session_paths,
    source_from_artifact,
    source_path_key,
    write_locator,
    write_manifest,
)


class IngestResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    session_id: str
    source: SourceAsset
    cache_hit: bool
    cache_reason: str
    session_dir: Path
    artifact_paths: tuple[Path, ...]


def ingest_source(source_path: Path, config: AppConfig) -> IngestResult:
    source = _validate_source_path(source_path, config.storage.data_dir)
    initial_stat = _safe_stat(source)

    cached = _try_fast_cache(source, initial_stat.st_size, initial_stat.st_mtime_ns, config)
    if cached is not None:
        return cached

    ffprobe_path = require_executable("ffprobe", config.tools.ffprobe_path)
    probe_version = executable_version(ffprobe_path)
    raw_probe = run_ffprobe(
        ffprobe_path,
        source,
        timeout_seconds=config.tools.probe_timeout_seconds,
    )
    source_sha256 = hash_file(source, source=True)
    _assert_source_stat_unchanged(source, initial_stat.st_size, initial_stat.st_mtime_ns)
    asset = parse_source_asset(
        raw_probe,
        source_path=source,
        source_sha256=source_sha256,
        size_bytes=initial_stat.st_size,
        mtime_ns=initial_stat.st_mtime_ns,
        probe_version=probe_version,
    )
    return _commit_ingest(asset, config, ffprobe_path)


def _try_fast_cache(
    source: Path, size_bytes: int, mtime_ns: int, config: AppConfig
) -> IngestResult | None:
    locator = load_locator(config.storage.data_dir, source)
    if locator is None:
        return None
    if (
        locator.path.resolve() != source
        or locator.path_key != source_path_key(source)
        or locator.size_bytes != size_bytes
        or locator.mtime_ns != mtime_ns
    ):
        return None
    paths = session_paths(config.storage.data_dir, locator.session_id)
    if not paths.manifest.is_file() or not paths.source.is_file():
        return None
    with SessionLock(paths.lock):
        manifest = load_manifest(paths.manifest)
        if recover_interrupted(manifest):
            write_manifest(paths.manifest, manifest)
            return None
        asset = source_from_artifact(paths.source)
        if asset.sha256 != locator.source_sha256:
            raise ValidationError("Source locator and source artifact hashes disagree.")
        expected = compute_ingest_cache_key(asset, config)
        valid, reason = completed_cache_is_valid(paths, manifest, expected_cache_key=expected)
        if not valid:
            if manifest.stages["ingest"].status is StageStatus.COMPLETED:
                manifest.stages["ingest"].status = StageStatus.STALE
                manifest.stages["ingest"].reason = reason
                manifest.updated_at = datetime.now(UTC)
                write_manifest(paths.manifest, manifest)
            return None
        _assert_source_stat_unchanged(source, size_bytes, mtime_ns)
        return IngestResult(
            session_id=locator.session_id,
            source=asset,
            cache_hit=True,
            cache_reason=reason,
            session_dir=paths.root,
            artifact_paths=(paths.source, paths.config, paths.environment, paths.manifest),
        )


def _commit_ingest(asset: SourceAsset, config: AppConfig, ffprobe_path: Path) -> IngestResult:
    session_id = make_session_id(asset)
    paths = session_paths(config.storage.data_dir, session_id)
    safe_create_session_directories(paths)
    timestamp = datetime.now(UTC)
    log = RunLogger(paths.logs / f"run-{timestamp.strftime('%Y%m%dT%H%M%S%fZ')}.jsonl")
    cache_key = compute_ingest_cache_key(asset, config)

    with SessionLock(paths.lock):
        if paths.manifest.is_file():
            manifest = load_manifest(paths.manifest)
            if recover_interrupted(manifest):
                write_manifest(paths.manifest, manifest)
            elif paths.source.is_file():
                valid, reason = completed_cache_is_valid(
                    paths, manifest, expected_cache_key=cache_key
                )
                if valid:
                    stored = source_from_artifact(paths.source)
                    return IngestResult(
                        session_id=session_id,
                        source=stored,
                        cache_hit=True,
                        cache_reason=reason,
                        session_dir=paths.root,
                        artifact_paths=(
                            paths.source,
                            paths.config,
                            paths.environment,
                            paths.manifest,
                        ),
                    )
                if manifest.stages["ingest"].status is StageStatus.COMPLETED:
                    manifest.stages["ingest"].status = StageStatus.STALE
                    manifest.stages["ingest"].reason = reason
                    manifest.updated_at = datetime.now(UTC)
                    write_manifest(paths.manifest, manifest)
        else:
            manifest = new_manifest(session_id)

        run_id = start_ingest(manifest, cache_key)
        write_manifest(paths.manifest, manifest)
        log.write("INFO", "ingest_started", "Source ingest started.", run_id=run_id)
        try:
            resolved_config = {
                "schema_version": 1,
                "created_at": timestamp.isoformat(),
                "producer_version": __version__,
                "config_hash": config_hash(config),
                "config": config_payload(config, redacted=True),
            }
            environment = _environment_payload(config, ffprobe_path, timestamp)
            atomic_write_json(paths.source, model_json(asset))
            atomic_write_json(paths.config, resolved_config)
            atomic_write_json(paths.environment, environment)
            _assert_source_stat_unchanged(asset.path, asset.size_bytes, asset.mtime_ns)

            inputs = [
                ArtifactIdentity(
                    path=str(asset.path), sha256=asset.sha256, size_bytes=asset.size_bytes
                )
            ]
            outputs = [
                artifact_identity(paths.source, relative_to=paths.root),
                artifact_identity(paths.config, relative_to=paths.root),
                artifact_identity(paths.environment, relative_to=paths.root),
            ]
            complete_ingest(manifest, inputs=inputs, outputs=outputs)
            write_manifest(paths.manifest, manifest)
            locator = SourceLocator(
                path=asset.path,
                path_key=source_path_key(asset.path),
                session_id=session_id,
                source_sha256=asset.sha256,
                size_bytes=asset.size_bytes,
                mtime_ns=asset.mtime_ns,
                updated_at=datetime.now(UTC),
            )
            write_locator(config.storage.data_dir, locator)
            log.write("INFO", "ingest_completed", "Source ingest completed.", run_id=run_id)
        except Exception as exc:
            error = _error_record(exc)
            try:
                fail_ingest(manifest, error)
                write_manifest(paths.manifest, manifest)
                log.write(
                    "ERROR",
                    "ingest_failed",
                    error.message,
                    category=error.category,
                    hint=error.hint,
                )
            except Exception:
                pass
            raise

    return IngestResult(
        session_id=session_id,
        source=asset,
        cache_hit=False,
        cache_reason="new ingest",
        session_dir=paths.root,
        artifact_paths=(paths.source, paths.config, paths.environment, paths.manifest),
    )


def _environment_payload(
    config: AppConfig, ffprobe_path: Path, created_at: datetime
) -> dict[str, object]:
    ffmpeg_path = config.tools.ffmpeg_path
    return {
        "schema_version": 1,
        "created_at": created_at.isoformat(),
        "producer_version": __version__,
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "ffprobe_path": str(ffprobe_path),
        "ffprobe_version": executable_version(ffprobe_path),
        "ffmpeg_path_configured": str(ffmpeg_path) if ffmpeg_path else None,
    }


def _validate_source_path(source_path: Path, data_dir: Path) -> Path:
    try:
        source = source_path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SourceError(
            f"Source file does not exist or cannot be resolved: {source_path}"
        ) from exc
    if not source.is_file():
        raise SourceError(f"Source path is not a regular file: {source}")
    try:
        if source.is_relative_to(data_dir.resolve()):
            raise SourceError(
                "Source video cannot be inside the configured session data directory."
            )
    except OSError as exc:
        raise SourceError(
            "Could not validate source/output path boundaries.", hint=str(exc)
        ) from exc
    stat = _safe_stat(source)
    if stat.st_size <= 0:
        raise SourceError(f"Source file is empty: {source}")
    try:
        with source.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        raise SourceError(f"Source file is not readable: {source}", hint=str(exc)) from exc
    return source


def _safe_stat(path: Path) -> stat_result:
    try:
        return path.stat()
    except OSError as exc:
        raise SourceError(f"Cannot inspect source file: {path}", hint=str(exc)) from exc


def _assert_source_stat_unchanged(path: Path, size_bytes: int, mtime_ns: int) -> None:
    current = _safe_stat(path)
    if current.st_size != size_bytes or current.st_mtime_ns != mtime_ns:
        raise SourceError(
            "Source changed during ingest; results were not accepted.",
            hint="Wait for recording/copying to finish, then run analyze again.",
        )


def _error_record(exc: Exception) -> ErrorRecord:
    if isinstance(exc, AppError):
        return ErrorRecord(
            category=exc.category.value,
            message=exc.message,
            hint=exc.hint,
            retryable=exc.category in {ErrorCategory.STORAGE, ErrorCategory.INTERNAL},
        )
    return ErrorRecord(
        category=ErrorCategory.INTERNAL.value,
        message="Unexpected internal error during ingest.",
        hint=type(exc).__name__,
        retryable=False,
    )
