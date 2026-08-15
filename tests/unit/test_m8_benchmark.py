from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from game_highlight_finder.benchmark.aggregate import aggregate_evaluations, render_markdown
from game_highlight_finder.benchmark.evaluator import (
    deterministic_temporal_match,
    validate_annotations_file,
)
from game_highlight_finder.benchmark.models import (
    AnnotatedHighlight,
    BenchmarkAnnotations,
    BenchmarkEvaluation,
    BenchmarkSplit,
    BestOfMetrics,
    BoringInterval,
    BoringMetrics,
    BoundaryMetrics,
    CategoryMetrics,
    CostMetrics,
    DuplicateMetrics,
    EvaluationCounts,
    EvaluationPolicy,
    ExperimentIdentity,
    Importance,
    MatchMetrics,
    Modality,
    PrimaryMetrics,
    ReviewMetrics,
    RuntimeMetrics,
    SliceMetric,
    StorageMetrics,
)
from game_highlight_finder.cli import app
from game_highlight_finder.domain.models import Candidate
from game_highlight_finder.storage.atomic import atomic_write_json


def _candidate(suffix: str, start: int, end: int, *, score: float = 8.0) -> Candidate:
    return Candidate(
        candidate_id=f"cand_{suffix * 16}",
        category="CLUTCH",
        event_start_ms=start,
        event_end_ms=end,
        score=score,
        confidence=0.8,
        reason="synthetic fixture",
    )


def test_annotation_schema_rejects_duplicate_ids_and_reversed_intervals() -> None:
    with pytest.raises(ValueError, match="end must be greater"):
        AnnotatedHighlight(
            annotation_id="h1",
            event_start_ms=200,
            event_end_ms=100,
            importance=Importance.MUST_CATCH,
            modality=Modality.VISUAL,
        )
    with pytest.raises(ValueError, match="annotation IDs must be unique"):
        BenchmarkAnnotations(
            source_sha256="0" * 64,
            source_duration_ms=2_000,
            benchmark_id="synthetic",
            case_id="case-a",
            highlights=(
                AnnotatedHighlight(
                    annotation_id="same",
                    event_start_ms=100,
                    event_end_ms=200,
                    importance=Importance.MUST_CATCH,
                    modality=Modality.VISUAL,
                ),
            ),
            boring_intervals=(BoringInterval(annotation_id="same", start_ms=500, end_ms=700),),
        )


def test_deterministic_temporal_matching_covers_m8a_edge_fixtures() -> None:
    policy = EvaluationPolicy(event_iou_threshold=0.25, boundary_tolerance_ms=3_000)
    highlights = [
        AnnotatedHighlight(
            annotation_id="h-exact",
            event_start_ms=1_000,
            event_end_ms=2_000,
            importance=Importance.MUST_CATCH,
            modality=Modality.VISUAL,
        ),
        AnnotatedHighlight(
            annotation_id="h-audio",
            event_start_ms=5_000,
            event_end_ms=6_000,
            importance=Importance.WORTH_REVIEW,
            modality=Modality.AUDIO,
        ),
    ]
    predictions = [
        _candidate("a", 1_000, 2_000, score=9.0),  # exact match
        _candidate("b", 1_100, 2_100, score=8.0),  # duplicate of h-exact
        _candidate("c", 5_250, 6_250, score=7.0),  # partial IoU and audio hit
        _candidate("d", 9_000, 9_500),  # false positive
    ]
    pairs = deterministic_temporal_match(predictions, highlights, policy)
    assert [
        (item.prediction.candidate.candidate_id, item.truth.highlight.annotation_id)
        for item in pairs
    ] == [
        ("cand_" + "a" * 16, "h-exact"),
        ("cand_" + "c" * 16, "h-audio"),
    ]
    # A candidate below IoU and outside the boundary tolerance is not a match.
    assert not deterministic_temporal_match(
        [_candidate("e", 20_001, 21_001)],
        [
            AnnotatedHighlight(
                annotation_id="h-far",
                event_start_ms=17_000,
                event_end_ms=18_000,
                importance=Importance.OPTIONAL,
                modality=Modality.UNKNOWN,
            )
        ],
        policy,
    )


def test_annotation_validate_reports_counts_and_source_identity(tmp_path: Path) -> None:
    source = tmp_path / "synthetic-source.bin"
    source.write_bytes(b"synthetic gameplay placeholder")
    import hashlib

    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    annotation_path = tmp_path / "annotation.json"
    annotations = BenchmarkAnnotations(
        benchmark_id="synthetic",
        case_id="case-a",
        source_sha256=source_hash,
        source_duration_ms=10_000,
        game_profile="synthetic",
        source_path=source,
        matches=(),
        highlights=(
            AnnotatedHighlight(
                annotation_id="must",
                event_start_ms=1_000,
                event_end_ms=2_000,
                importance=Importance.MUST_CATCH,
                modality=Modality.VISUAL_AND_AUDIO,
            ),
            AnnotatedHighlight(
                annotation_id="worth",
                event_start_ms=3_000,
                event_end_ms=3_500,
                importance=Importance.WORTH_REVIEW,
                modality=Modality.AUDIO,
            ),
        ),
        boring_intervals=(BoringInterval(annotation_id="boring", start_ms=0, end_ms=500),),
    )
    atomic_write_json(annotation_path, annotations.model_dump(mode="json"))
    summary = validate_annotations_file(annotation_path)
    assert summary.source_identity == "PASS"
    assert summary.must_catch_count == 1
    assert summary.worth_review_count == 1
    assert summary.boring_interval_count == 1
    assert summary.modality_breakdown[Modality.AUDIO.value] == 1


def _evaluation(
    case_id: str, split: BenchmarkSplit, *, tp: int, fp: int, fn: int
) -> BenchmarkEvaluation:
    source_hash = "1" * 64
    annotation_hash = (case_id.encode().hex() + "0" * 64)[:64]
    counts = EvaluationCounts(
        predictions=tp + fp,
        ground_truth_highlights=tp + fn,
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
    )
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    primary = PrimaryMetrics(
        **counts.model_dump(mode="python"),
        precision=precision,
        recall=recall,
        f1=(2 * precision * recall / (precision + recall) if precision and recall else None),
    )
    experiment = ExperimentIdentity(
        provider="fake",
        model="synthetic",
        billing_mode="standard",
        media_resolution="local",
        thinking_level="none",
        prompt_version="fixture-v1",
        provider_schema_version=1,
        canonicalization_version="m8-test",
        window_duration_seconds=900,
        window_overlap_seconds=30,
        proxy_settings_fingerprint="proxy",
        signal_settings_fingerprint="signals",
        extraction_config_fingerprint="extract",
        ranking_config_fingerprint="rank",
        evaluator_policy_version="m8-eval-v1",
        source_sha256=source_hash,
        annotation_sha256=annotation_hash,
    )
    return BenchmarkEvaluation(
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        evaluation_policy=EvaluationPolicy(),
        benchmark_id="synthetic",
        case_id=case_id,
        split=split,
        game_profile="synthetic",
        session_id=f"session-{case_id}",
        source_id="src_" + "2" * 16,
        source_duration_ms=3_600_000,
        source_sha256=source_hash,
        annotation_sha256=annotation_hash,
        experiment=experiment,
        counts=counts,
        primary_metrics=primary,
        importance_metrics=(
            SliceMetric(label="MUST_CATCH", ground_truth=tp + fn, matched=tp, recall=recall),
        ),
        modality_metrics=(
            SliceMetric(label="VISUAL", ground_truth=tp + fn, matched=tp, recall=recall),
        ),
        boundary_metrics=BoundaryMetrics(matched_count=tp),
        duplicate_metrics=DuplicateMetrics(duplicate_prediction_count=0, duplicate_rate=0),
        best_of_metrics=BestOfMetrics(
            best_of_count=min(3, tp + fp),
            must_catch_found=tp,
            worth_review_found=0,
            useful_ground_truth_count=tp + fn,
            useful_true_positives=tp,
            best_of_precision=precision,
            best_of_recall=recall,
        ),
        boring_metrics=BoringMetrics(
            annotated_boring_interval_count=0,
            candidates_overlapping_boring=0,
            false_positives_per_source_hour=0,
            candidate_review_ms_inside_boring=0,
        ),
        category_metrics=CategoryMetrics(annotated_category_count=0, category_matches=0),
        match_metrics=MatchMetrics(available=False),
        review_metrics=ReviewMetrics(
            candidate_review_ms=60_000,
            source_duration_ms=3_600_000,
            review_ratio=1 / 60,
            review_percentage=100 / 60,
        ),
        cost_metrics=CostMetrics(
            settled_micro_thb=1_000_000,
            reserved_micro_thb=0,
            in_flight_micro_thb=0,
            ambiguous_micro_thb=0,
            call_count=1,
            financially_resolved=True,
            thb_per_source_hour=1,
            thb_per_true_positive=1 / tp if tp else None,
        ),
        runtime_metrics=RuntimeMetrics(
            total_analysis_wall_time_ms=1_000,
            source_duration_ms=3_600_000,
            real_time_factor=1 / 3_600,
            compute_minutes_per_source_hour=1 / 60,
        ),
        storage_metrics=StorageMetrics(
            total_bytes=1_000_000,
            source_duration_ms=3_600_000,
            megabytes_per_source_hour=1,
        ),
    )


def test_aggregate_is_count_weighted_and_has_calibration_validation_combined() -> None:
    calibration = _evaluation("cal-1", BenchmarkSplit.CALIBRATION, tp=1, fp=0, fn=0)
    validation = _evaluation("val-1", BenchmarkSplit.VALIDATION, tp=0, fp=1, fn=1)
    aggregate = aggregate_evaluations([calibration, validation], benchmark_id="synthetic")
    assert {group.split for group in aggregate.groups} == {"calibration", "validation", "combined"}
    combined = next(group for group in aggregate.groups if group.split == "combined")
    assert combined.counts.true_positives == 1
    assert combined.counts.false_positives == 1
    assert combined.counts.false_negatives == 1
    assert combined.primary_metrics.precision == pytest.approx(0.5)
    assert combined.primary_metrics.recall == pytest.approx(0.5)
    markdown = render_markdown(aggregate)
    assert "calibration" in markdown and "validation" in markdown and "combined" in markdown
    assert "PRIVATE_SOURCE_PATH" not in markdown


def test_benchmark_cli_help_is_local_and_provider_free() -> None:
    runner = CliRunner()
    for args in (
        ["benchmark", "--help"],
        ["benchmark", "template", "--help"],
        ["benchmark", "validate", "--help"],
        ["benchmark", "evaluate", "--help"],
        ["benchmark", "aggregate", "--help"],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.stdout
