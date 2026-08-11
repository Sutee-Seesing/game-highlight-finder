"""Human-oriented session status calculation."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from game_highlight_finder.config import AppConfig
from game_highlight_finder.domain.models import AttemptRecord, SourceAsset, StageStatus
from game_highlight_finder.domain.time import format_duration
from game_highlight_finder.errors import SourceError
from game_highlight_finder.storage.sessions import (
    completed_cache_is_valid,
    compute_ingest_cache_key,
    load_manifest,
    session_paths,
    source_from_artifact,
)


class SessionStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    session_id: str
    source: SourceAsset
    ingest_status: StageStatus
    cache_state: str
    cache_detail: str
    artifact_paths: tuple[Path, ...]
    last_attempt: AttemptRecord | None

    @property
    def duration_text(self) -> str:
        return format_duration(self.source.duration_ms)


def get_session_status(session_id: str, config: AppConfig) -> SessionStatus:
    paths = session_paths(config.storage.data_dir, session_id)
    if not paths.root.is_dir():
        raise SourceError(f"Session does not exist: {session_id}")
    source = source_from_artifact(paths.source)
    manifest = load_manifest(paths.manifest)
    stage = manifest.stages["ingest"]
    if not source.path.is_file():
        cache_state, detail = "SOURCE_MISSING", "source path is no longer available"
    else:
        stat = source.path.stat()
        if stat.st_size != source.size_bytes or stat.st_mtime_ns != source.mtime_ns:
            cache_state, detail = "STALE", "source size or modification time changed"
        else:
            expected = compute_ingest_cache_key(source, config)
            valid, detail = completed_cache_is_valid(paths, manifest, expected_cache_key=expected)
            cache_state = "VALID" if valid else "STALE"
    return SessionStatus(
        session_id=session_id,
        source=source,
        ingest_status=stage.status,
        cache_state=cache_state,
        cache_detail=detail,
        artifact_paths=(paths.source, paths.config, paths.environment, paths.manifest, paths.logs),
        last_attempt=stage.attempts[-1] if stage.attempts else None,
    )
