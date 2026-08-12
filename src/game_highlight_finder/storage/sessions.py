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

from game_highlight_finder import __version__
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


@dataclass(frozen=True)
class SessionPaths:
    root: Path
    source: Path
    config: Path
    environment: Path
    manifest: Path
    logs: Path
    lock: Path


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
    payload = {
        "cache_version": INGEST_CACHE_VERSION,
        "producer_version": __version__,
        "source_sha256": source.sha256,
        "source_size_bytes": source.size_bytes,
        "source_mtime_ns": source.mtime_ns,
        "ingest_config_fingerprint": ingest_config_fingerprint(config),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def artifact_identity(path: Path, *, relative_to: Path | None = None) -> ArtifactIdentity:
    display = str(path.relative_to(relative_to)) if relative_to is not None else str(path)
    return ArtifactIdentity(path=display, sha256=hash_file(path), size_bytes=path.stat().st_size)


def completed_cache_is_valid(
    paths: SessionPaths,
    manifest: Manifest,
    *,
    expected_cache_key: str,
) -> tuple[bool, str]:
    stage = manifest.stages["ingest"]
    if stage.status is not StageStatus.COMPLETED:
        return False, f"ingest state is {stage.status}"
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
