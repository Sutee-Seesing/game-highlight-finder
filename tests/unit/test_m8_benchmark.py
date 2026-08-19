from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from game_highlight_finder.benchmark.aggregate import (
    aggregate_comparison,
    aggregate_dataset,
    aggregate_evaluations,
    experiment_fingerprint,
    render_markdown,
)
from game_highlight_finder.benchmark.evaluator import (
    deterministic_temporal_match,
    validate_annotations_file,
)
from game_highlight_finder.benchmark.identity import benchmark_identity_compatible
from game_highlight_finder.benchmark.models import (
    AnnotatedHighlight,
    BenchmarkAnnotations,
    BenchmarkCase,
    BenchmarkComparisonManifest,
    BenchmarkDataset,
    BenchmarkEvaluation,
    BenchmarkResultRef,
    BenchmarkResultSet,
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
        ["benchmark", "compare", "--help"],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.stdout


def test_full_policy_fingerprint_is_canonical_and_authoritative() -> None:
    first = EvaluationPolicy.model_validate(
        {
            "boundary_tolerance_ms": 3_000,
            "event_iou_threshold": 0.25,
            "policy_version": "m8-eval-v1",
            "schema_version": 1,
        }
    )
    reordered = EvaluationPolicy.model_validate(
        {
            "schema_version": 1,
            "policy_version": "m8-eval-v1",
            "event_iou_threshold": 0.25,
            "boundary_tolerance_ms": 3_000,
        }
    )
    different_iou = EvaluationPolicy(event_iou_threshold=0.50)
    different_tolerance = EvaluationPolicy(boundary_tolerance_ms=4_000)
    different_version = EvaluationPolicy(policy_version="m8-eval-v2")
    assert first.fingerprint() == reordered.fingerprint()
    assert first.fingerprint() == first.evaluation_policy_fingerprint
    assert first.fingerprint() != different_iou.fingerprint()
    assert first.fingerprint() != different_tolerance.fingerprint()
    assert first.fingerprint() != different_version.fingerprint()
    assert first.semantic_payload() == {
        "schema_version": 1,
        "policy_version": "m8-eval-v1",
        "event_iou_threshold": 0.25,
        "boundary_tolerance_ms": 3_000,
    }


def _rebind_evaluation(
    evaluation: BenchmarkEvaluation,
    *,
    case_id: str,
    source_sha256: str,
    annotation_sha256: str,
    model: str | None = None,
    policy: EvaluationPolicy | None = None,
    benchmark_id: str = "synthetic",
) -> BenchmarkEvaluation:
    active_policy = policy or evaluation.evaluation_policy
    experiment = evaluation.experiment.model_copy(
        update={
            "source_sha256": source_sha256,
            "annotation_sha256": annotation_sha256,
            "model": model or evaluation.experiment.model,
            "evaluator_policy_version": active_policy.policy_version,
            "evaluator_policy_fingerprint": active_policy.fingerprint(),
        }
    )
    return evaluation.model_copy(
        update={
            "case_id": case_id,
            "benchmark_id": benchmark_id,
            "source_sha256": source_sha256,
            "annotation_sha256": annotation_sha256,
            "evaluation_policy": active_policy,
            "evaluation_policy_fingerprint": active_policy.fingerprint(),
            "experiment": experiment,
            "evaluation_fingerprint": "",
        }
    )


def _comparison_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """Create two local result sets over two private synthetic cases."""

    policy = EvaluationPolicy()
    source_hash = "1" * 64
    annotation_dir = tmp_path / "annotations"
    annotation_dir.mkdir(parents=True)
    cases: list[BenchmarkCase] = []
    refs_a: list[BenchmarkResultRef] = []
    refs_b: list[BenchmarkResultRef] = []
    for case_id, split in (
        ("case-a", BenchmarkSplit.CALIBRATION),
        ("case-b", BenchmarkSplit.VALIDATION),
    ):
        annotation_path = annotation_dir / f"{case_id}.json"
        annotation_path.write_bytes(f"synthetic annotation {case_id}".encode())
        annotation_hash = hashlib.sha256(annotation_path.read_bytes()).hexdigest()
        cases.append(
            BenchmarkCase(
                case_id=case_id,
                source_path=tmp_path / "private" / f"{case_id}.mp4",
                expected_source_sha256=source_hash,
                annotation_path=annotation_path.relative_to(tmp_path),
                game_profile="synthetic",
                split=split,
            )
        )
        base = _evaluation(case_id, split, tp=1, fp=0, fn=0)
        evaluation_a = _rebind_evaluation(
            base,
            case_id=case_id,
            source_sha256=source_hash,
            annotation_sha256=annotation_hash,
            model="synthetic-model-a",
            policy=policy,
            benchmark_id="synthetic-comparison",
        )
        evaluation_b = _rebind_evaluation(
            base,
            case_id=case_id,
            source_sha256=source_hash,
            annotation_sha256=annotation_hash,
            model="synthetic-model-b",
            policy=policy,
            benchmark_id="synthetic-comparison",
        )
        path_a = tmp_path / "experiments" / "a" / "results" / f"{case_id}.json"
        path_b = tmp_path / "experiments" / "b" / "results" / f"{case_id}.json"
        atomic_write_json(path_a, evaluation_a.model_dump(mode="json"))
        atomic_write_json(path_b, evaluation_b.model_dump(mode="json"))
        refs_a.append(
            BenchmarkResultRef(
                case_id=case_id,
                evaluation_path=Path("experiments") / "a" / "results" / f"{case_id}.json",
            )
        )
        refs_b.append(
            BenchmarkResultRef(
                case_id=case_id,
                evaluation_path=Path("experiments") / "b" / "results" / f"{case_id}.json",
            )
        )
    dataset = BenchmarkDataset(
        benchmark_id="synthetic-comparison",
        name="Synthetic comparison",
        evaluation_policy=policy,
        cases=tuple(cases),
    )
    dataset_path = tmp_path / "dataset.json"
    atomic_write_json(dataset_path, dataset.model_dump(mode="json"))
    result_set_a = BenchmarkResultSet(
        result_set_id="model-a",
        label="Synthetic Model A",
        benchmark_id=dataset.benchmark_id,
        evaluation_policy_fingerprint=policy.fingerprint(),
        results=tuple(refs_a),
    )
    result_set_b = BenchmarkResultSet(
        result_set_id="model-b",
        label="Synthetic Model B",
        benchmark_id=dataset.benchmark_id,
        evaluation_policy_fingerprint=policy.fingerprint(),
        results=tuple(refs_b),
    )
    comparison = BenchmarkComparisonManifest(
        comparison_id="synthetic-comparison",
        benchmark_dataset_path=Path("dataset.json"),
        result_sets=(result_set_a, result_set_b),
    )
    comparison_path = tmp_path / "comparison.json"
    atomic_write_json(comparison_path, comparison.model_dump(mode="json"))
    return comparison_path, dataset_path


def test_dataset_policy_mismatch_and_legacy_migration_are_fail_closed(tmp_path: Path) -> None:
    policy = EvaluationPolicy(event_iou_threshold=0.50)
    dataset = BenchmarkDataset(
        benchmark_id="synthetic",
        name="Synthetic",
        evaluation_policy=policy,
        evaluation_policy_version=policy.policy_version,
        cases=(),
    )
    assert dataset.policy_fingerprint == policy.fingerprint()
    legacy = BenchmarkDataset(
        benchmark_id="legacy",
        name="Legacy",
        evaluation_policy_version="m8-eval-v1",
        cases=(),
    )
    assert legacy.evaluation_policy is not None
    assert legacy.evaluation_policy.fingerprint() == EvaluationPolicy().fingerprint()
    evaluation = _evaluation("case-a", BenchmarkSplit.CALIBRATION, tp=1, fp=0, fn=0)
    mismatch = evaluation.model_copy(
        update={
            "evaluation_policy": policy,
            "evaluation_policy_fingerprint": policy.fingerprint(),
            "experiment": evaluation.experiment.model_copy(
                update={
                    "evaluator_policy_fingerprint": policy.fingerprint(),
                }
            ),
            "evaluation_fingerprint": "",
        }
    )
    with pytest.raises(Exception, match="policy"):
        aggregate_evaluations([evaluation, mismatch], benchmark_id="synthetic")
    del tmp_path


def test_locked_m8_private_annotation_id_is_compatible_only_by_lock_hashes(
    tmp_path: Path,
) -> None:
    benchmark_root = tmp_path / "benchmarks"
    dataset_dir = benchmark_root / "datasets"
    annotation_dir = benchmark_root / "annotations"
    private_dir = benchmark_root / "private"
    result_dir = dataset_dir / "results"
    for path in (dataset_dir, annotation_dir, private_dir, result_dir):
        path.mkdir(parents=True)
    annotation_path = annotation_dir / "case-a.json"
    annotation_path.write_bytes(b"immutable locked annotation bytes")
    annotation_hash = hashlib.sha256(annotation_path.read_bytes()).hexdigest()
    source_hash = "1" * 64
    case = BenchmarkCase(
        case_id="case-a",
        source_path=tmp_path / "private" / "case-a.mkv",
        expected_source_sha256=source_hash,
        annotation_path=Path("../annotations/case-a.json"),
        game_profile="synthetic",
        split=BenchmarkSplit.CALIBRATION,
    )
    dataset = BenchmarkDataset(
        benchmark_id="m8-real-v1",
        name="locked identity fixture",
        cases=(case,),
        evaluation_policy=EvaluationPolicy(),
    )
    dataset_path = dataset_dir / "m8-real-v1.json"
    atomic_write_json(dataset_path, dataset.model_dump(mode="json"))
    lock_path = private_dir / "m8-real-v1-ground-truth-lock.json"
    atomic_write_json(
        lock_path,
        {
            "cases": [
                {
                    "case_id": "case-a",
                    "split": "calibration",
                    "source_sha256": source_hash,
                    "annotation_sha256": annotation_hash,
                }
            ]
        },
    )
    evaluation = _rebind_evaluation(
        _evaluation("case-a", BenchmarkSplit.CALIBRATION, tp=1, fp=0, fn=0),
        case_id="case-a",
        source_sha256=source_hash,
        annotation_sha256=annotation_hash,
        benchmark_id="m8-private",
    )
    result_path = result_dir / "case-a.json"
    atomic_write_json(result_path, evaluation.model_dump(mode="json"))

    assert benchmark_identity_compatible(
        dataset_path,
        dataset,
        case,
        evaluation_benchmark_id="m8-private",
        evaluation_case_id="case-a",
        evaluation_source_sha256=source_hash,
        evaluation_annotation_sha256=annotation_hash,
        expected_annotation_sha256=annotation_hash,
        lock_path=lock_path,
    )
    assert not benchmark_identity_compatible(
        dataset_path,
        dataset,
        case,
        evaluation_benchmark_id="m8-private",
        evaluation_case_id="case-a",
        evaluation_source_sha256=source_hash,
        evaluation_annotation_sha256="2" * 64,
        expected_annotation_sha256=annotation_hash,
        lock_path=lock_path,
    )
    aggregate = aggregate_dataset(dataset_path)
    assert aggregate.aggregate.benchmark_id == "m8-real-v1"
    assert aggregate.aggregate.per_case[0].benchmark_id == "m8-real-v1"


def test_multi_experiment_comparison_is_separate_and_count_weighted(tmp_path: Path) -> None:
    comparison_path, _dataset_path = _comparison_fixture(tmp_path)
    result = aggregate_comparison(comparison_path)
    assert result.aggregate.comparison_id == "synthetic-comparison"
    assert len(result.aggregate.groups) == 6  # 2 experiments x (cal/val/combined)
    labels = {group.experiment_label for group in result.aggregate.groups}
    assert labels == {"Synthetic Model A", "Synthetic Model B"}
    assert all(group.result_set_id in {"model-a", "model-b"} for group in result.aggregate.groups)
    assert "Synthetic Model A" in result.markdown_path.read_text(encoding="utf-8")
    assert str(tmp_path) not in result.markdown_path.read_text(encoding="utf-8")


def test_multi_experiment_comparison_blocks_unequal_coverage(tmp_path: Path) -> None:
    comparison_path, _dataset_path = _comparison_fixture(tmp_path)
    raw = json.loads(comparison_path.read_text(encoding="utf-8"))
    raw["result_sets"][1]["results"] = raw["result_sets"][1]["results"][:1]
    atomic_write_json(comparison_path, raw)
    with pytest.raises(Exception, match="coverage mismatch"):
        aggregate_comparison(comparison_path)


def test_mixed_experiment_result_set_and_annotation_revision_are_rejected(tmp_path: Path) -> None:
    comparison_path, _dataset_path = _comparison_fixture(tmp_path)
    raw = json.loads(comparison_path.read_text(encoding="utf-8"))
    first_ref = raw["result_sets"][1]["results"][0]
    second_ref = raw["result_sets"][1]["results"][1]
    second_ref["evaluation_path"] = raw["result_sets"][0]["results"][1]["evaluation_path"]
    atomic_write_json(comparison_path, raw)
    with pytest.raises(Exception, match="mixes experiment"):
        aggregate_comparison(comparison_path)
    del first_ref

    comparison_path, _dataset_path = _comparison_fixture(tmp_path / "revision")
    annotation_path = tmp_path / "revision" / "annotations" / "case-b.json"
    annotation_path.write_bytes(b"annotation revision B")
    with pytest.raises(Exception, match="Annotation revision mismatch"):
        aggregate_comparison(comparison_path)


def test_experiment_fingerprint_excludes_only_case_ground_truth_identity() -> None:
    first = _evaluation("case-a", BenchmarkSplit.CALIBRATION, tp=1, fp=0, fn=0)
    second = _evaluation("case-b", BenchmarkSplit.CALIBRATION, tp=1, fp=0, fn=0)
    assert experiment_fingerprint(first) == experiment_fingerprint(second)
    changed_model = _rebind_evaluation(
        second,
        case_id="case-b",
        source_sha256="1" * 64,
        annotation_sha256=second.annotation_sha256,
        model="different-model",
    )
    assert experiment_fingerprint(first) != experiment_fingerprint(changed_model)


@pytest.mark.parametrize(
    "policy",
    [
        EvaluationPolicy(event_iou_threshold=0.50),
        EvaluationPolicy(boundary_tolerance_ms=4_000),
        EvaluationPolicy(policy_version="m8-eval-v2"),
    ],
)
def test_aggregate_blocks_any_full_policy_mismatch(policy: EvaluationPolicy) -> None:
    first = _evaluation("case-a", BenchmarkSplit.CALIBRATION, tp=1, fp=0, fn=0)
    second = _rebind_evaluation(
        _evaluation("case-b", BenchmarkSplit.CALIBRATION, tp=1, fp=0, fn=0),
        case_id="case-b",
        source_sha256="1" * 64,
        annotation_sha256="2" * 64,
        policy=policy,
    )
    with pytest.raises(Exception, match="policy"):
        aggregate_evaluations([first, second], benchmark_id="synthetic")


def test_annotation_revision_changes_evaluation_identity_but_not_experiment_identity() -> None:
    first = _evaluation("case-a", BenchmarkSplit.CALIBRATION, tp=1, fp=0, fn=0)
    first_validated = BenchmarkEvaluation.model_validate(first.model_dump(mode="json"))
    revised = _rebind_evaluation(
        first,
        case_id="case-a",
        source_sha256=first.source_sha256,
        annotation_sha256="2" * 64,
    )
    revised_validated = BenchmarkEvaluation.model_validate(revised.model_dump(mode="json"))
    assert first_validated.evaluation_fingerprint != revised_validated.evaluation_fingerprint
    assert first_validated.annotation_sha256 != revised_validated.annotation_sha256
    assert experiment_fingerprint(first_validated) == experiment_fingerprint(revised_validated)


def test_result_set_rejects_duplicate_case_references() -> None:
    with pytest.raises(ValueError, match="duplicate case"):
        BenchmarkResultSet(
            result_set_id="duplicate",
            label="Duplicate",
            benchmark_id="synthetic",
            evaluation_policy_fingerprint=EvaluationPolicy().fingerprint(),
            results=(
                BenchmarkResultRef(case_id="same", evaluation_path="a.json"),
                BenchmarkResultRef(case_id="same", evaluation_path="b.json"),
            ),
        )


def test_shareable_markdown_redacts_paths_and_secret_assignments() -> None:
    evaluation = _evaluation("case-a", BenchmarkSplit.CALIBRATION, tp=1, fp=0, fn=0)
    unsafe = evaluation.model_copy(
        update={
            "warnings": (r"C:\Users\owner\private.json API_KEY=super-secret",),
            "evaluation_fingerprint": "",
        }
    )
    report = render_markdown(aggregate_evaluations([unsafe], benchmark_id="synthetic"))
    assert "super-secret" not in report
    assert r"C:\Users\owner" not in report
    assert "PRIVATE_PATH" in report
