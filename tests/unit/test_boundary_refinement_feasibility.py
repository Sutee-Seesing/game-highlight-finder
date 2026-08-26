from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from game_highlight_finder import __version__
from game_highlight_finder.benchmark.boundary_feasibility import (
    assess_boundary_refinement_feasibility,
    run_boundary_refinement_feasibility,
)
from game_highlight_finder.benchmark.models import (
    AnnotatedHighlight,
    BenchmarkAnnotations,
    BenchmarkCase,
    BenchmarkDataset,
    BenchmarkSplit,
    EvaluationPolicy,
    Importance,
    Modality,
)
from game_highlight_finder.cli import app
from game_highlight_finder.config import AppConfig, StorageConfig, config_payload
from game_highlight_finder.domain.models import (
    Candidate,
    Rational,
    SessionMap,
    SourceAsset,
    VideoStream,
    model_json,
)
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.hashing import hash_file
from game_highlight_finder.storage.sessions import session_paths

SESSION_ID = "2026-08-26_unknown_aaaaaaaaaaaa"
SOURCE_ID = "src_aaaaaaaaaaaaaaaa"


def _candidate(candidate_id: str, start_ms: int, end_ms: int) -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        category="SKILL",
        event_start_ms=start_ms,
        event_end_ms=end_ms,
        score=8.0,
        confidence=0.9,
        reason=f"candidate {candidate_id}",
    )


def _annotations(source_sha256: str) -> BenchmarkAnnotations:
    return BenchmarkAnnotations(
        benchmark_id="m8-real-v1",
        case_id="cal-01",
        source_sha256=source_sha256,
        source_duration_ms=80_000,
        game_profile="valorant",
        highlights=(
            AnnotatedHighlight(
                annotation_id="hl-1",
                event_start_ms=1_100,
                event_end_ms=2_100,
                importance=Importance.WORTH_REVIEW,
                modality=Modality.VISUAL,
            ),
            AnnotatedHighlight(
                annotation_id="hl-2",
                event_start_ms=10_500,
                event_end_ms=20_000,
                importance=Importance.MUST_CATCH,
                modality=Modality.VISUAL_AND_AUDIO,
            ),
            AnnotatedHighlight(
                annotation_id="hl-3",
                event_start_ms=50_000,
                event_end_ms=51_000,
                importance=Importance.MUST_CATCH,
                modality=Modality.VISUAL,
            ),
        ),
    )


def _session_map(source_sha256: str) -> SessionMap:
    del source_sha256
    return SessionMap(
        created_at=datetime(2026, 8, 26, tzinfo=UTC),
        producer_version=__version__,
        canonicalization_version="feasibility-test-v1",
        session_id=SESSION_ID,
        source_id=SOURCE_ID,
        duration_ms=80_000,
        candidates=[
            _candidate("cand_1111111111111111", 1_000, 2_000),
            _candidate("cand_2222222222222222", 10_000, 11_000),
            _candidate("cand_3333333333333333", 75_000, 76_000),
        ],
        scout_backend="gemini",
        game_profile="valorant",
    )


def test_feasibility_separates_boundary_headroom_from_detection_gaps() -> None:
    source_sha = "a" * 64
    result = assess_boundary_refinement_feasibility(
        _session_map(source_sha),
        _annotations(source_sha),
        EvaluationPolicy(),
        dataset_sha256="b" * 64,
        annotation_document_sha256="c" * 64,
    )

    assert result.strict_match_count == 1
    assert result.strict_false_positive_count == 2
    assert result.strict_false_negative_count == 2
    assert result.anchor_overlap_annotation_count == 2
    assert result.boundary_headroom_count == 1
    assert result.detection_gap_count == 1
    assert result.context_unreachable_count == 1
    assert result.must_catch_boundary_headroom_count == 1
    assert result.must_catch_detection_gap_count == 1
    assert result.diagnostic_verdict == "MUST_CATCH_DETECTION_GAP"
    assert result.ground_truth_derived_candidate_ids == (
        "cand_1111111111111111",
        "cand_2222222222222222",
    )
    by_id = {item.annotation_id: item for item in result.annotations}
    assert by_id["hl-1"].strict_matched_candidate_id == "cand_1111111111111111"
    assert by_id["hl-2"].boundary_headroom is True
    assert by_id["hl-3"].detection_gap is True
    assert by_id["hl-3"].context_unreachable is True
    assert result.provider_calls == 0


def _write_private_case(
    tmp_path: Path,
    *,
    split: BenchmarkSplit,
) -> tuple[Path, Path, AppConfig]:
    data_dir = tmp_path / "library"
    source_path = tmp_path / "calibration-source.mp4"
    source_path.write_bytes(b"private calibration source")
    source_sha = hash_file(source_path, source=True)
    annotations = _annotations(source_sha)
    annotation_path = tmp_path / "cal-01.annotations.json"
    atomic_write_json(annotation_path, annotations.model_dump(mode="json"))
    dataset = BenchmarkDataset(
        benchmark_id="m8-real-v1",
        name="private calibration fixture",
        cases=(
            BenchmarkCase(
                case_id="cal-01",
                source_path=source_path,
                expected_source_sha256=source_sha,
                annotation_path=annotation_path,
                game_profile="valorant",
                split=split,
            ),
        ),
    )
    dataset_path = tmp_path / "dataset.json"
    atomic_write_json(dataset_path, dataset.model_dump(mode="json"))

    config = AppConfig(storage=StorageConfig(data_dir=data_dir))
    if split is BenchmarkSplit.CALIBRATION:
        stat = source_path.stat()
        source = SourceAsset(
            created_at=datetime(2026, 8, 26, tzinfo=UTC),
            producer_version=__version__,
            source_id=SOURCE_ID,
            path=source_path.resolve(),
            sha256=source_sha,
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            duration_ms=80_000,
            container="mp4",
            video_stream=VideoStream(
                index=0,
                codec_name="h264",
                width=1920,
                height=1080,
                average_frame_rate=Rational(numerator=60, denominator=1),
            ),
            selected_video_stream=0,
            probe_version="unit-test",
        )
        paths = session_paths(data_dir, SESSION_ID)
        paths.root.mkdir(parents=True)
        atomic_write_json(paths.source, model_json(source))
        atomic_write_json(paths.session_map, model_json(_session_map(source_sha)))
        atomic_write_json(
            paths.config,
            {
                "schema_version": 1,
                "config": config_payload(config, redacted=True),
            },
        )
    return dataset_path, annotation_path, config


def test_feasibility_runner_persists_private_calibration_artifact(tmp_path: Path) -> None:
    dataset_path, annotation_path, config = _write_private_case(
        tmp_path,
        split=BenchmarkSplit.CALIBRATION,
    )

    result, output = run_boundary_refinement_feasibility(
        SESSION_ID,
        dataset_path,
        annotation_path,
        config,
    )

    assert output.is_file()
    assert (
        output
        == (
            config.storage.data_dir
            / "benchmarks"
            / "private"
            / "boundary_refinement"
            / "cal-01.feasibility.json"
        ).resolve()
    )
    assert read_json(output) == result.model_dump(mode="json")
    assert result.split == "calibration"
    assert result.provider_calls == 0
    assert "production candidate-selection" in result.selection_warning


def test_feasibility_cli_is_provider_free_and_uses_persisted_session_config(
    tmp_path: Path,
) -> None:
    dataset_path, annotation_path, config = _write_private_case(
        tmp_path,
        split=BenchmarkSplit.CALIBRATION,
    )
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(config.storage.data_dir),
            "benchmark",
            "boundary-feasibility",
            SESSION_ID,
            "--dataset",
            str(dataset_path),
            "--annotations",
            str(annotation_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "provider/API calls: ZERO" in result.output
    assert "MUST_CATCH_DETECTION_GAP" in result.output
    assert "never production selection" in result.output
    output = (
        config.storage.data_dir
        / "benchmarks"
        / "private"
        / "boundary_refinement"
        / "cal-01.feasibility.json"
    )
    assert output.is_file()


def test_feasibility_runner_rejects_validation_before_session_access(tmp_path: Path) -> None:
    dataset_path, annotation_path, config = _write_private_case(
        tmp_path,
        split=BenchmarkSplit.VALIDATION,
    )

    with pytest.raises(ValidationError, match="calibration-only"):
        run_boundary_refinement_feasibility(
            SESSION_ID,
            dataset_path,
            annotation_path,
            config,
        )
