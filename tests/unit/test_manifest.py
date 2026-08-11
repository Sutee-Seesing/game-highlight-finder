from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from game_highlight_finder.domain.models import ArtifactIdentity, StageStatus
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.pipeline.manifest import (
    complete_ingest,
    new_manifest,
    recover_interrupted,
    start_ingest,
)

NOW = datetime(2026, 8, 11, tzinfo=UTC)
ARTIFACT = ArtifactIdentity(path="source.json", sha256="a" * 64, size_bytes=1)


def test_manifest_happy_path() -> None:
    manifest = new_manifest("2026-08-11_unknown_aaaaaaaaaaaa", now=NOW)
    run_id = start_ingest(manifest, "cache-key", now=NOW, run_id="run-1")
    complete_ingest(
        manifest,
        inputs=[ARTIFACT],
        outputs=[ARTIFACT],
        now=NOW + timedelta(seconds=1),
    )

    assert run_id == "run-1"
    assert manifest.stages["ingest"].status is StageStatus.COMPLETED
    assert manifest.stages["ingest"].attempts[-1].status is StageStatus.COMPLETED


def test_invalid_manifest_transition_is_rejected() -> None:
    manifest = new_manifest("2026-08-11_unknown_aaaaaaaaaaaa", now=NOW)

    with pytest.raises(ValidationError):
        complete_ingest(manifest, inputs=[], outputs=[])


def test_running_attempt_is_recovered_as_interrupted() -> None:
    manifest = new_manifest("2026-08-11_unknown_aaaaaaaaaaaa", now=NOW)
    start_ingest(manifest, "cache-key", now=NOW, run_id="run-1")

    changed = recover_interrupted(manifest, now=NOW + timedelta(minutes=1))

    assert changed is True
    assert manifest.stages["ingest"].status is StageStatus.FAILED
    assert manifest.stages["ingest"].reason == "INTERRUPTED"
    assert manifest.stages["ingest"].attempts[-1].error is not None
