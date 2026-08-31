from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from game_highlight_finder.benchmark.cross_case_suppression import (
    AudioScaleDocument,
    assess_cross_case_suppression,
    run_cross_case_suppression,
)
from game_highlight_finder.benchmark.review_queue_server import (
    ReviewAdjudicationDocument,
    ReviewDecisionItem,
    ReviewQueueDocument,
)
from game_highlight_finder.cli import app
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.storage.atomic import atomic_write_json


def _queue_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "set_id": "cross-case-dev",
        "not_ground_truth": True,
        "excluded_from_m8_acceptance": True,
        "provider_calls": 0,
        "cases": [
            {
                "case": "xonotic",
                "session_id": "session-xonotic",
                "source": "xonotic.webm",
                "duration_ms": 20_000,
                "intervals": [
                    {
                        "review_id": "x-1",
                        "start_ms": 1_000,
                        "end_ms": 2_000,
                        "review_clip": "clips/x-1.mp4",
                    },
                    {
                        "review_id": "x-2",
                        "start_ms": 3_000,
                        "end_ms": 4_000,
                        "review_clip": "clips/x-2.mp4",
                    },
                ],
            },
            {
                "case": "freedoom",
                "session_id": "session-freedoom",
                "source": "freedoom.webm",
                "duration_ms": 20_000,
                "intervals": [
                    {
                        "review_id": "f-1",
                        "start_ms": 5_000,
                        "end_ms": 6_000,
                        "review_clip": "clips/f-1.mp4",
                    },
                    {
                        "review_id": "f-2",
                        "start_ms": 7_000,
                        "end_ms": 8_000,
                        "review_clip": "clips/f-2.mp4",
                    },
                ],
            },
        ],
    }


def _queue_bytes() -> bytes:
    return (
        json.dumps(_queue_payload(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _queue_sha() -> str:
    return hashlib.sha256(_queue_bytes()).hexdigest()


def _queue() -> ReviewQueueDocument:
    return ReviewQueueDocument.model_validate(_queue_payload())


def _adjudication(
    *,
    uncertain: bool = False,
    omit: str | None = None,
    reviewer_kind: str = "HUMAN",
) -> ReviewAdjudicationDocument:
    values = {
        "x-1": "BORING",
        "x-2": "POSITIVE",
        "f-1": "BORING",
        "f-2": "POSITIVE",
    }
    if uncertain:
        values["x-1"] = "UNCERTAIN"
    decisions = tuple(
        ReviewDecisionItem(review_id=review_id, decision=decision)
        for review_id, decision in values.items()
        if review_id != omit
    )
    return ReviewAdjudicationDocument(
        set_id="cross-case-dev",
        queue_sha256=_queue_sha(),
        updated_at=datetime(2026, 8, 31, tzinfo=UTC),
        selected_cases=("xonotic", "freedoom"),
        decisions=decisions,
        reviewer_kind=reviewer_kind,
    )


def _audio_scale(*, surviving_boring: bool = False) -> AudioScaleDocument:
    x1 = 7.0 if surviving_boring else 3.0
    return AudioScaleDocument.model_validate(
        {
            "schema_version": 1,
            "semantic_labels_inferred": False,
            "provider_calls": 0,
            "cases": [
                {
                    "case": "xonotic",
                    "intervals": [
                        {"review_id": "x-1", "audio_peak_over_loudness_db": x1},
                        {"review_id": "x-2", "audio_peak_over_loudness_db": 6.0},
                    ],
                },
                {
                    "case": "freedoom",
                    "intervals": [
                        {"review_id": "f-1", "audio_peak_over_loudness_db": 4.0},
                        {"review_id": "f-2", "audio_peak_over_loudness_db": 8.0},
                    ],
                },
            ],
        }
    )


def test_cross_case_normalized_peak_can_cleanly_separate_reviewed_negatives() -> None:
    result = assess_cross_case_suppression(
        _queue(),
        _adjudication(),
        _audio_scale(),
        queue_sha256=_queue_sha(),
    )

    assert result.reviewed_count == 4
    assert result.reviewer_kind == "HUMAN"
    assert result.positive_count == 2
    assert result.boring_count == 2
    assert result.protected_positive_min_audio_peak_over_loudness_db == 6.0
    assert result.rejected_boring_review_ids == ("f-1", "x-1")
    assert result.surviving_boring_review_ids == ()
    assert result.verdict == "NORMALIZED_AUDIO_PEAK_SEPARATES_REVIEWED_NEGATIVES"
    assert result.provider_calls == 0
    assert result.production_threshold_locked is False


def test_cross_case_preserves_assistant_visual_provenance() -> None:
    result = assess_cross_case_suppression(
        _queue(),
        _adjudication(reviewer_kind="ASSISTANT_VISUAL"),
        _audio_scale(),
        queue_sha256=_queue_sha(),
    )

    assert result.reviewer_kind == "ASSISTANT_VISUAL"
    assert result.provider_calls == 0
    assert result.production_threshold_locked is False


def test_cross_case_reports_no_clean_separation_when_boring_survives() -> None:
    result = assess_cross_case_suppression(
        _queue(),
        _adjudication(),
        _audio_scale(surviving_boring=True),
        queue_sha256=_queue_sha(),
    )

    assert result.rejected_boring_review_ids == ("f-1",)
    assert result.surviving_boring_review_ids == ("x-1",)
    assert result.verdict == "NORMALIZED_AUDIO_PEAK_NO_CLEAN_SEPARATION"


def test_cross_case_rejects_incomplete_or_uncertain_visual_review() -> None:
    with pytest.raises(ValidationError, match="incomplete"):
        assess_cross_case_suppression(
            _queue(),
            _adjudication(omit="x-1"),
            _audio_scale(),
            queue_sha256=_queue_sha(),
        )
    with pytest.raises(ValidationError, match="resolved POSITIVE/BORING"):
        assess_cross_case_suppression(
            _queue(),
            _adjudication(uncertain=True),
            _audio_scale(),
            queue_sha256=_queue_sha(),
        )


def test_cross_case_rejects_queue_identity_mismatch() -> None:
    with pytest.raises(ValidationError, match="does not match"):
        assess_cross_case_suppression(
            _queue(),
            _adjudication(),
            _audio_scale(),
            queue_sha256="0" * 64,
        )


def test_cross_case_runner_and_cli_persist_private_provider_free_artifact(tmp_path: Path) -> None:
    queue_path = tmp_path / "review_queue.json"
    queue_path.write_bytes(_queue_bytes())
    adjudication_path = tmp_path / "review_queue.adjudication.json"
    audio_scale_path = tmp_path / "audio-scale.json"
    atomic_write_json(adjudication_path, _adjudication().model_dump(mode="json"))
    atomic_write_json(audio_scale_path, _audio_scale().model_dump(mode="json"))

    result, output = run_cross_case_suppression(
        queue_path,
        adjudication_path,
        audio_scale_path,
    )
    assert output.is_file()
    assert result.provider_calls == 0

    cli_output = tmp_path / "cli-cross-case.json"
    cli = CliRunner().invoke(
        app,
        [
            "benchmark",
            "cross-case-suppression",
            str(queue_path),
            "--adjudication",
            str(adjudication_path),
            "--audio-scale",
            str(audio_scale_path),
            "--output",
            str(cli_output),
        ],
    )
    assert cli.exit_code == 0, cli.output
    assert "provider/API calls: ZERO" in cli.output
    assert "Reviewer kind: HUMAN" in cli.output
    assert "Rejected boring intervals: 2/2" in cli.output
    assert "Production threshold locked: NO" in cli.output
    assert cli_output.is_file()


def test_cross_case_cli_help_is_available() -> None:
    result = CliRunner().invoke(app, ["benchmark", "cross-case-suppression", "--help"])
    assert result.exit_code == 0, result.output
    assert "--adjudication" in result.output
    assert "--audio-scale" in result.output
