"""Human-oriented session status calculation."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from game_highlight_finder.config import AppConfig
from game_highlight_finder.domain.models import AttemptRecord, SourceAsset, StageStatus
from game_highlight_finder.domain.time import format_duration
from game_highlight_finder.errors import SourceError
from game_highlight_finder.pipeline.manifest import ensure_m3_stages
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
    stages: dict[str, StageStatus] = Field(default_factory=dict)
    stage_details: dict[str, str] = Field(default_factory=dict)

    @property
    def duration_text(self) -> str:
        return format_duration(self.source.duration_ms)


def get_session_status(session_id: str, config: AppConfig) -> SessionStatus:
    paths = session_paths(config.storage.data_dir, session_id)
    if not paths.root.is_dir():
        raise SourceError(f"Session does not exist: {session_id}")
    source = source_from_artifact(paths.source)
    manifest = load_manifest(paths.manifest)
    if ensure_m3_stages(manifest):
        from game_highlight_finder.storage.sessions import write_manifest

        write_manifest(paths.manifest, manifest)
    ingest_stage = manifest.stages["ingest"]
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
    stages = {name: stage.status for name, stage in manifest.stages.items()}
    stage_details = {
        name: _stage_detail(paths, stage)
        for name, stage in manifest.stages.items()
        if name != "ingest"
    }
    artifacts: list[Path] = [
        paths.source,
        paths.config,
        paths.environment,
        paths.manifest,
        paths.logs,
    ]
    for stage in manifest.stages.values():
        for artifact in stage.output_artifacts:
            candidate = (paths.root / artifact.path).resolve()
            if candidate.is_file() and candidate not in artifacts:
                artifacts.append(candidate)
    return SessionStatus(
        session_id=session_id,
        source=source,
        ingest_status=ingest_stage.status,
        cache_state=cache_state,
        cache_detail=detail,
        artifact_paths=tuple(artifacts),
        last_attempt=ingest_stage.attempts[-1] if ingest_stage.attempts else None,
        stages=stages,
        stage_details=stage_details,
    )


def _stage_detail(paths: object, stage: object) -> str:
    # This intentionally checks only committed output hashes here; the next run computes
    # the full stage-specific key using resolved external-tool identities.
    from game_highlight_finder.domain.models import StageRecord
    from game_highlight_finder.storage.hashing import hash_file

    if not isinstance(stage, StageRecord):
        return "unknown"
    if stage.status is not StageStatus.COMPLETED:
        return stage.reason or (stage.error.message if stage.error else "")
    root = paths.root  # type: ignore[attr-defined]
    for artifact in stage.output_artifacts:
        candidate = (root / artifact.path).resolve()
        if not candidate.is_file() or candidate.stat().st_size != artifact.size_bytes:
            return "stale: artifact missing/changed"
        if hash_file(candidate) != artifact.sha256:
            return "stale: artifact hash changed"
    return "cache valid"
