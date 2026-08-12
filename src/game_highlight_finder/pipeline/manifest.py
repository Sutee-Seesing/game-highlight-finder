"""Atomic, restart-safe stage state transitions."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from game_highlight_finder import __version__
from game_highlight_finder.domain.models import (
    ArtifactIdentity,
    AttemptRecord,
    ErrorRecord,
    Manifest,
    StageRecord,
    StageStatus,
)
from game_highlight_finder.errors import ValidationError


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_manifest(session_id: str, *, now: datetime | None = None) -> Manifest:
    timestamp = now or utc_now()
    return Manifest(
        created_at=timestamp,
        updated_at=timestamp,
        producer_version=__version__,
        session_id=session_id,
        stages={
            "ingest": StageRecord(stage="ingest"),
            "proxy": StageRecord(stage="proxy"),
            "local_signals": StageRecord(stage="local_signals"),
            "scout": StageRecord(stage="scout"),
        },
    )


M2_STAGE_NAMES = ("proxy", "local_signals")
M3_STAGE_NAMES = ("scout",)


def ensure_m2_stages(manifest: Manifest, *, now: datetime | None = None) -> bool:
    """Add M2 stage records to a legacy M1 manifest without changing its ingest record."""

    changed = False
    for name in M2_STAGE_NAMES:
        if name not in manifest.stages:
            manifest.stages[name] = StageRecord(stage=name)
            changed = True
    if changed:
        manifest.updated_at = now or utc_now()
    return changed


def ensure_m3_stages(manifest: Manifest, *, now: datetime | None = None) -> bool:
    """Add M2 and M3 stage records to legacy manifests without rewriting artifacts."""

    changed = ensure_m2_stages(manifest, now=now)
    for name in M3_STAGE_NAMES:
        if name not in manifest.stages:
            manifest.stages[name] = StageRecord(stage=name)
            changed = True
    if changed:
        manifest.updated_at = now or utc_now()
    return changed


def recover_interrupted(manifest: Manifest, *, now: datetime | None = None) -> bool:
    timestamp = now or utc_now()
    changed = False
    for stage in manifest.stages.values():
        if stage.status is not StageStatus.RUNNING:
            continue
        error = ErrorRecord(
            category="internal",
            message=f"Previous {stage.stage} attempt was interrupted before completion.",
            retryable=True,
        )
        if stage.attempts and stage.attempts[-1].status is StageStatus.RUNNING:
            stage.attempts[-1].status = StageStatus.FAILED
            stage.attempts[-1].completed_at = timestamp
            stage.attempts[-1].error = error
        stage.status = StageStatus.FAILED
        stage.completed_at = timestamp
        stage.error = error
        stage.reason = "INTERRUPTED"
        changed = True
    if changed:
        manifest.updated_at = timestamp
    return changed


def start_stage(
    manifest: Manifest,
    stage_name: str,
    cache_key: str,
    *,
    now: datetime | None = None,
    run_id: str | None = None,
) -> str:
    stage = manifest.stages.get(stage_name)
    if stage is None:
        stage = StageRecord(stage=stage_name)
        manifest.stages[stage_name] = stage
    if stage.status not in {StageStatus.PENDING, StageStatus.FAILED, StageStatus.STALE}:
        raise ValidationError(f"Cannot start {stage_name} from state {stage.status}.")
    timestamp = now or utc_now()
    identifier = run_id or uuid4().hex
    stage.status = StageStatus.RUNNING
    stage.cache_key = cache_key
    stage.started_at = timestamp
    stage.completed_at = None
    stage.error = None
    stage.reason = None
    stage.attempts.append(
        AttemptRecord(run_id=identifier, status=StageStatus.RUNNING, started_at=timestamp)
    )
    manifest.updated_at = timestamp
    return identifier


def complete_stage(
    manifest: Manifest,
    stage_name: str,
    *,
    inputs: list[ArtifactIdentity],
    outputs: list[ArtifactIdentity],
    item_states: dict[str, str] | None = None,
    now: datetime | None = None,
) -> None:
    stage = manifest.stages.get(stage_name)
    if stage is None or stage.status is not StageStatus.RUNNING or not stage.attempts:
        raise ValidationError(f"Cannot complete {stage_name} when it is not running.")
    timestamp = now or utc_now()
    attempt = stage.attempts[-1]
    attempt.status = StageStatus.COMPLETED
    attempt.completed_at = timestamp
    stage.status = StageStatus.COMPLETED
    stage.completed_at = timestamp
    stage.input_artifacts = inputs
    stage.output_artifacts = outputs
    stage.item_states = item_states or {}
    stage.error = None
    stage.reason = None
    manifest.updated_at = timestamp


def fail_stage(
    manifest: Manifest, stage_name: str, error: ErrorRecord, *, now: datetime | None = None
) -> None:
    stage = manifest.stages.get(stage_name)
    if stage is None or stage.status is not StageStatus.RUNNING or not stage.attempts:
        raise ValidationError(f"Cannot fail {stage_name} when it is not running.")
    timestamp = now or utc_now()
    attempt = stage.attempts[-1]
    attempt.status = StageStatus.FAILED
    attempt.completed_at = timestamp
    attempt.error = error
    stage.status = StageStatus.FAILED
    stage.completed_at = timestamp
    stage.error = error
    manifest.updated_at = timestamp


def start_ingest(
    manifest: Manifest, cache_key: str, *, now: datetime | None = None, run_id: str | None = None
) -> str:
    return start_stage(manifest, "ingest", cache_key, now=now, run_id=run_id)


def complete_ingest(
    manifest: Manifest,
    *,
    inputs: list[ArtifactIdentity],
    outputs: list[ArtifactIdentity],
    now: datetime | None = None,
) -> None:
    complete_stage(manifest, "ingest", inputs=inputs, outputs=outputs, now=now)


def fail_ingest(manifest: Manifest, error: ErrorRecord, *, now: datetime | None = None) -> None:
    fail_stage(manifest, "ingest", error, now=now)
