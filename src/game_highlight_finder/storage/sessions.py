"""Session paths, locators, manifests, and cache verification."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from game_highlight_finder.config import AppConfig
from game_highlight_finder.domain.models import (
    ArtifactIdentity,
    Manifest,
    SourceAsset,
    SourceLocator,
    StageStatus,
    model_json,
)
from game_highlight_finder.errors import StorageError, ValidationError
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.hashing import hash_file

SESSION_ID_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}_unknown_[0-9a-f]{12}$")
INGEST_CACHE_VERSION = 2
INGEST_CONFIG_FINGERPRINT_VERSION = 1
PROXY_CACHE_VERSION = 1
PROXY_CONFIG_FINGERPRINT_VERSION = 1
LOCAL_SIGNALS_CACHE_VERSION = 1
LOCAL_SIGNALS_CONFIG_FINGERPRINT_VERSION = 1
SCOUT_CACHE_VERSION = 1
SCOUT_CONFIG_FINGERPRINT_VERSION = 1
GEMINI_PROVIDER_CACHE_VERSION = 2


@dataclass(frozen=True)
class SessionPaths:
    root: Path
    source: Path
    config: Path
    environment: Path
    manifest: Path
    logs: Path
    lock: Path
    proxy_dir: Path
    audio_dir: Path
    signals_dir: Path
    scout_dir: Path
    scout_raw_dir: Path
    scout_canonical_dir: Path
    session_map: Path
    tmp_dir: Path
    scout_windows_dir: Path
    reconcile_dir: Path
    candidates_dir: Path
    thumbnails_dir: Path
    extraction_manifest: Path
    reports_dir: Path
    ranking_path: Path
    report_path: Path
    report_meta_path: Path


def session_paths(data_dir: Path, session_id: str) -> SessionPaths:
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValidationError(f"Invalid session ID: {session_id}")
    root = data_dir.resolve() / "sessions" / session_id
    return SessionPaths(
        root=root,
        source=root / "source.json",
        config=root / "config.resolved.json",
        environment=root / "environment.json",
        manifest=root / "manifest.json",
        logs=root / "logs",
        lock=root / ".session.lock",
        proxy_dir=root / "proxy",
        audio_dir=root / "audio",
        signals_dir=root / "signals",
        scout_dir=root / "scout",
        scout_raw_dir=root / "scout" / "raw",
        scout_canonical_dir=root / "scout" / "canonical",
        session_map=root / "session_map.json",
        tmp_dir=root / "tmp",
        scout_windows_dir=root / "scout" / "windows",
        reconcile_dir=root / "reconcile",
        candidates_dir=root / "candidates",
        thumbnails_dir=root / "thumbnails",
        extraction_manifest=root / "extraction_manifest.json",
        reports_dir=root / "reports",
        ranking_path=root / "reports" / "ranking.json",
        report_path=root / "reports" / "index.html",
        report_meta_path=root / "reports" / "report.meta.json",
    )


def make_session_id(source: SourceAsset) -> str:
    date = datetime.fromtimestamp(source.mtime_ns / 1_000_000_000, tz=UTC).date().isoformat()
    return f"{date}_unknown_{source.sha256[:12]}"


def source_path_key(source_path: Path) -> str:
    normalized = os.path.normcase(str(source_path.resolve()))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def locator_path(data_dir: Path, source_path: Path) -> Path:
    return data_dir.resolve() / "index" / "sources" / f"{source_path_key(source_path)}.json"


def write_locator(data_dir: Path, locator: SourceLocator) -> None:
    atomic_write_json(locator_path(data_dir, locator.path), model_json(locator))


def load_locator(data_dir: Path, source_path: Path) -> SourceLocator | None:
    path = locator_path(data_dir, source_path)
    if not path.is_file():
        return None
    try:
        return SourceLocator.model_validate(read_json(path))
    except PydanticValidationError as exc:
        raise ValidationError("Stored source locator is corrupt.", hint=str(exc)) from exc


def load_manifest(path: Path) -> Manifest:
    try:
        return Manifest.model_validate(read_json(path))
    except PydanticValidationError as exc:
        raise ValidationError(f"Stored manifest is invalid: {path}", hint=str(exc)) from exc


def write_manifest(path: Path, manifest: Manifest) -> None:
    atomic_write_json(path, model_json(manifest))


def ingest_config_fingerprint(config: AppConfig) -> str:
    """Hash only configuration that can change ingest's external probe tool.

    M1 has no semantic ingest tuning knobs. The configured ffprobe executable is
    included because swapping it can change parsed metadata. Storage location,
    probe timeout, logging, and all future non-ingest sections are intentionally
    excluded; add a field here only when it materially changes ingest output or
    validity.
    """
    configured_path = config.tools.ffprobe_path
    probe_identity = (
        str(configured_path.expanduser().resolve()) if configured_path is not None else "PATH"
    )
    payload = {
        "fingerprint_version": INGEST_CONFIG_FINGERPRINT_VERSION,
        "ffprobe_path": probe_identity,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_ingest_cache_key(source: SourceAsset, config: AppConfig) -> str:
    encoded = json.dumps(
        ingest_cache_payload(source, config), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def ingest_cache_payload(source: SourceAsset, config: AppConfig) -> dict[str, object]:
    """Return the semantic identity for an ingest result.

    The stage cache version and ingest-specific inputs are authoritative here;
    the global package version is intentionally not part of cache identity.
    """

    return {
        "cache_version": INGEST_CACHE_VERSION,
        "source_sha256": source.sha256,
        "source_size_bytes": source.size_bytes,
        "source_mtime_ns": source.mtime_ns,
        "ingest_config_fingerprint": ingest_config_fingerprint(config),
    }


def _identity_payload(identity: object | None) -> str:
    if identity is None:
        return "unresolved"
    payload = getattr(identity, "cache_payload", None)
    if callable(payload):
        return json.dumps(payload(), sort_keys=True, separators=(",", ":"))
    return str(identity)


def proxy_config_fingerprint(
    config: AppConfig,
    *,
    ffmpeg_identity: object | None = None,
    ffprobe_identity: object | None = None,
) -> str:
    """Hash only settings and tool identities that can change proxy bytes/validation."""

    payload = {
        "fingerprint_version": PROXY_CONFIG_FINGERPRINT_VERSION,
        "proxy": config.media.proxy.model_dump(mode="json"),
        "audio": config.media.audio.model_dump(mode="json"),
        "ffmpeg": _identity_payload(ffmpeg_identity),
        "ffprobe": _identity_payload(ffprobe_identity),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def local_signals_config_fingerprint(
    config: AppConfig,
    *,
    ffmpeg_identity: object | None = None,
    ffprobe_identity: object | None = None,
) -> str:
    """Hash only signal settings and the tools used to produce them."""

    payload = {
        "fingerprint_version": LOCAL_SIGNALS_CONFIG_FINGERPRINT_VERSION,
        "silence": config.signals.silence.model_dump(mode="json"),
        "loudness": config.signals.loudness.model_dump(mode="json"),
        "ffmpeg": _identity_payload(ffmpeg_identity),
        "ffprobe": _identity_payload(ffprobe_identity),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_proxy_cache_key(
    source: SourceAsset,
    config: AppConfig,
    *,
    ffmpeg_identity: object | None = None,
    ffprobe_identity: object | None = None,
) -> str:
    encoded = json.dumps(
        proxy_cache_payload(
            source,
            config,
            ffmpeg_identity=ffmpeg_identity,
            ffprobe_identity=ffprobe_identity,
        ),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def proxy_cache_payload(
    source: SourceAsset,
    config: AppConfig,
    *,
    ffmpeg_identity: object | None = None,
    ffprobe_identity: object | None = None,
) -> dict[str, object]:
    """Return the semantic identity for a proxy result."""

    return {
        "cache_version": PROXY_CACHE_VERSION,
        "source_sha256": source.sha256,
        "source_duration_ms": source.duration_ms,
        "source_timestamp_origin_ms": source.timestamp_origin_ms,
        "proxy_config_fingerprint": proxy_config_fingerprint(
            config, ffmpeg_identity=ffmpeg_identity, ffprobe_identity=ffprobe_identity
        ),
    }


def compute_local_signals_cache_key(
    source: SourceAsset,
    config: AppConfig,
    *,
    proxy_artifact_sha256: str,
    ffmpeg_identity: object | None = None,
    ffprobe_identity: object | None = None,
) -> str:
    encoded = json.dumps(
        local_signals_cache_payload(
            source,
            config,
            proxy_artifact_sha256=proxy_artifact_sha256,
            ffmpeg_identity=ffmpeg_identity,
            ffprobe_identity=ffprobe_identity,
        ),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def local_signals_cache_payload(
    source: SourceAsset,
    config: AppConfig,
    *,
    proxy_artifact_sha256: str,
    ffmpeg_identity: object | None = None,
    ffprobe_identity: object | None = None,
) -> dict[str, object]:
    """Return the semantic identity for local signal artifacts."""

    return {
        "cache_version": LOCAL_SIGNALS_CACHE_VERSION,
        "source_sha256": source.sha256,
        "proxy_artifact_sha256": proxy_artifact_sha256,
        "local_signals_config_fingerprint": local_signals_config_fingerprint(
            config, ffmpeg_identity=ffmpeg_identity, ffprobe_identity=ffprobe_identity
        ),
    }


def scout_config_fingerprint(config: AppConfig, *, fixture_sha256: str | None = None) -> str:
    """Hash semantic Scout inputs; unrelated logging/UI settings never invalidate work."""

    payload = {
        "fingerprint_version": SCOUT_CONFIG_FINGERPRINT_VERSION,
        "backend": config.scout.backend,
        "schema_version": config.scout.schema_version,
        "canonicalization_version": config.scout.canonicalization_version,
        "response_max_bytes": config.scout.response_max_bytes,
        "fixture_sha256": fixture_sha256 or "builtin-deterministic",
        "model": config.scout.model,
        "billing_mode": config.scout.billing_mode,
        "media_resolution": config.scout.media_resolution,
        "max_duration_seconds": config.scout.max_duration_seconds,
        "max_output_tokens": config.scout.max_output_tokens,
        "thinking_level": config.scout.thinking_level,
        "reserved_thinking_tokens": config.scout.reserved_thinking_tokens,
        "prompt_version": config.scout.prompt_version,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def scout_cache_payload(
    source: SourceAsset,
    config: AppConfig,
    *,
    proxy_artifact_sha256: str,
    local_signals_artifact_sha256: str,
    fixture_sha256: str | None = None,
) -> dict[str, object]:
    """Return the semantic identity for the M3 Scout stage."""

    return {
        "cache_version": SCOUT_CACHE_VERSION,
        "source_sha256": source.sha256,
        "proxy_artifact_sha256": proxy_artifact_sha256,
        "local_signals_artifact_sha256": local_signals_artifact_sha256,
        "scout_config_fingerprint": scout_config_fingerprint(config, fixture_sha256=fixture_sha256),
    }


def compute_scout_cache_key(
    source: SourceAsset,
    config: AppConfig,
    *,
    proxy_artifact_sha256: str,
    local_signals_artifact_sha256: str,
    fixture_sha256: str | None = None,
) -> str:
    encoded = json.dumps(
        scout_cache_payload(
            source,
            config,
            proxy_artifact_sha256=proxy_artifact_sha256,
            local_signals_artifact_sha256=local_signals_artifact_sha256,
            fixture_sha256=fixture_sha256,
        ),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def gemini_provider_cache_payload(
    source: SourceAsset,
    config: AppConfig,
    *,
    proxy_artifact_sha256: str,
    local_signals_summary_hash: str,
    prompt_hash: str,
    schema_hash: str,
) -> dict[str, object]:
    """Semantic identity for a paid Gemini request only.

    Canonicalization and report/UI settings deliberately do not participate;
    a valid paid provider response can therefore be reused after local parser
    changes without another billable call.
    """

    return {
        "cache_version": GEMINI_PROVIDER_CACHE_VERSION,
        "source_sha256": source.sha256,
        "source_duration_ms": source.duration_ms,
        "proxy_artifact_sha256": proxy_artifact_sha256,
        "local_signals_summary_hash": local_signals_summary_hash,
        "provider": "gemini",
        "model": config.scout.model,
        "billing_mode": config.scout.billing_mode,
        "media_resolution": config.scout.media_resolution,
        "prompt_version": config.scout.prompt_version,
        "prompt_hash": prompt_hash,
        "schema_version": config.scout.schema_version,
        "schema_hash": schema_hash,
        "response_max_bytes": config.scout.response_max_bytes,
        "max_duration_seconds": config.scout.max_duration_seconds,
        "max_output_tokens": config.scout.max_output_tokens,
        "thinking_level": config.scout.thinking_level,
        "reserved_thinking_tokens": config.scout.reserved_thinking_tokens,
    }


def compute_gemini_provider_cache_key(
    source: SourceAsset,
    config: AppConfig,
    *,
    proxy_artifact_sha256: str,
    local_signals_summary_hash: str,
    prompt_hash: str,
    schema_hash: str,
) -> str:
    encoded = json.dumps(
        gemini_provider_cache_payload(
            source,
            config,
            proxy_artifact_sha256=proxy_artifact_sha256,
            local_signals_summary_hash=local_signals_summary_hash,
            prompt_hash=prompt_hash,
            schema_hash=schema_hash,
        ),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def artifact_identity(path: Path, *, relative_to: Path | None = None) -> ArtifactIdentity:
    display = str(path.relative_to(relative_to)) if relative_to is not None else str(path)
    if relative_to is not None:
        display = display.replace("\\", "/")
    return ArtifactIdentity(path=display, sha256=hash_file(path), size_bytes=path.stat().st_size)


def completed_cache_is_valid(
    paths: SessionPaths,
    manifest: Manifest,
    *,
    expected_cache_key: str,
) -> tuple[bool, str]:
    return completed_stage_cache_is_valid(
        paths, manifest, stage_name="ingest", expected_cache_key=expected_cache_key
    )


def completed_stage_cache_is_valid(
    paths: SessionPaths,
    manifest: Manifest,
    *,
    stage_name: str,
    expected_cache_key: str,
) -> tuple[bool, str]:
    stage = manifest.stages.get(stage_name)
    if stage is None:
        return False, f"{stage_name} state is missing"
    if stage.status is not StageStatus.COMPLETED:
        return False, f"{stage_name} state is {stage.status}"
    if stage.cache_key != expected_cache_key:
        return False, "cache key changed"
    for artifact in stage.output_artifacts:
        candidate = (paths.root / artifact.path).resolve()
        try:
            candidate.relative_to(paths.root.resolve())
        except ValueError:
            return False, "artifact path escapes session directory"
        if not candidate.is_file():
            return False, f"artifact missing: {artifact.path}"
        if (
            candidate.stat().st_size != artifact.size_bytes
            or hash_file(candidate) != artifact.sha256
        ):
            return False, f"artifact changed: {artifact.path}"
    return True, "verified"


def ensure_data_directory(data_dir: Path) -> tuple[bool, str | None]:
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / f".write-test-{os.getpid()}"
        with probe.open("xb") as handle:
            handle.write(b"ok")
            handle.flush()
            os.fsync(handle.fileno())
        probe.unlink()
        return True, None
    except OSError as exc:
        return False, str(exc)


def source_from_artifact(path: Path) -> SourceAsset:
    try:
        return SourceAsset.model_validate(read_json(path))
    except PydanticValidationError as exc:
        raise ValidationError(f"Stored source artifact is invalid: {path}", hint=str(exc)) from exc


def safe_create_session_directories(paths: SessionPaths) -> None:
    try:
        paths.logs.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StorageError(f"Cannot create session directory: {paths.root}", hint=str(exc)) from exc
