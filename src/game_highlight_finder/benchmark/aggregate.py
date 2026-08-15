"""Count-weighted aggregation and privacy-safe Markdown reporting for M8A."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from game_highlight_finder.benchmark.evaluator import annotation_sha256, load_evaluation
from game_highlight_finder.benchmark.models import (
    AggregateGroup,
    BenchmarkAggregate,
    BenchmarkCase,
    BenchmarkDataset,
    BenchmarkEvaluation,
    BenchmarkSplit,
    BestOfMetrics,
    BoundaryMetrics,
    CostMetrics,
    DuplicateMetrics,
    EvaluationCounts,
    PrimaryMetrics,
    ReviewMetrics,
    RuntimeMetrics,
    SliceMetric,
    StorageMetrics,
)
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.storage.atomic import atomic_write_bytes, atomic_write_json, read_json


class AggregateRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    aggregate: BenchmarkAggregate
    json_path: Path
    markdown_path: Path


def _experiment_fingerprint(evaluation: BenchmarkEvaluation) -> str:
    payload = evaluation.experiment.model_dump(mode="json")
    payload.pop("source_sha256", None)
    payload.pop("annotation_sha256", None)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _sum_slices(
    evaluations: Iterable[BenchmarkEvaluation], attribute: str
) -> tuple[SliceMetric, ...]:
    totals: dict[str, dict[str, int]] = defaultdict(
        lambda: {"ground_truth": 0, "matched": 0, "predictions": 0, "false_positives": 0}
    )
    for evaluation in evaluations:
        for item in getattr(evaluation, attribute):
            totals[item.label]["ground_truth"] += item.ground_truth
            totals[item.label]["matched"] += item.matched
            totals[item.label]["predictions"] += item.predictions
            totals[item.label]["false_positives"] += item.false_positives
    return tuple(
        SliceMetric(
            label=label,
            ground_truth=values["ground_truth"],
            matched=values["matched"],
            recall=(values["matched"] / values["ground_truth"] if values["ground_truth"] else None),
            predictions=values["predictions"],
            false_positives=values["false_positives"],
        )
        for label, values in sorted(totals.items())
    )


def _aggregate_group(
    evaluations: tuple[BenchmarkEvaluation, ...],
    *,
    split_override: str | None = None,
) -> AggregateGroup:
    first = evaluations[0]
    counts = EvaluationCounts(
        predictions=sum(item.counts.predictions for item in evaluations),
        ground_truth_highlights=sum(item.counts.ground_truth_highlights for item in evaluations),
        true_positives=sum(item.counts.true_positives for item in evaluations),
        false_positives=sum(item.counts.false_positives for item in evaluations),
        false_negatives=sum(item.counts.false_negatives for item in evaluations),
    )
    precision = counts.true_positives / counts.predictions if counts.predictions else None
    recall = (
        counts.true_positives / counts.ground_truth_highlights
        if counts.ground_truth_highlights
        else None
    )
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else None
    )
    primary = PrimaryMetrics(
        **counts.model_dump(mode="python"), precision=precision, recall=recall, f1=f1
    )
    raw_measurements = tuple(
        measurement
        for evaluation in evaluations
        for measurement in evaluation.boundary_metrics.measurements
    )
    ordered_start = sorted(item.start_error_ms for item in raw_measurements)
    ordered_end = sorted(item.end_error_ms for item in raw_measurements)
    ordered_iou = sorted(item.event_iou for item in raw_measurements)
    ordered_combined = sorted(item.combined_boundary_error_ms for item in raw_measurements)

    def median(values: Sequence[float | int]) -> float | None:
        if not values:
            return None
        middle = len(values) // 2
        if len(values) % 2:
            return float(values[middle])
        return (float(values[middle - 1]) + float(values[middle])) / 2

    def p90(values: Sequence[float | int]) -> float | None:
        if not values:
            return None
        index = max(0, int((len(values) * 0.9 + 0.999999999) // 1) - 1)
        return float(values[index])

    boundary = BoundaryMetrics(
        matched_count=len(raw_measurements),
        median_start_error_ms=median(ordered_start),
        median_end_error_ms=median(ordered_end),
        median_iou=median(ordered_iou),
        p90_boundary_error_ms=p90(ordered_combined),
        measurements=raw_measurements,
    )
    duplicate_count = sum(item.duplicate_metrics.duplicate_prediction_count for item in evaluations)
    duplicate = DuplicateMetrics(
        duplicate_prediction_count=duplicate_count,
        duplicate_rate=duplicate_count / counts.predictions if counts.predictions else None,
    )
    review_ms = sum(item.review_metrics.candidate_review_ms for item in evaluations)
    source_duration_ms = sum(item.source_duration_ms for item in evaluations)
    source_hours = source_duration_ms / 3_600_000
    review = ReviewMetrics(
        candidate_review_ms=review_ms,
        source_duration_ms=source_duration_ms,
        review_ratio=review_ms / source_duration_ms if source_duration_ms else None,
        review_percentage=review_ms / source_duration_ms * 100 if source_duration_ms else None,
    )
    must_found = sum(item.best_of_metrics.must_catch_found for item in evaluations)
    worth_found = sum(item.best_of_metrics.worth_review_found for item in evaluations)
    useful_gt = sum(item.best_of_metrics.useful_ground_truth_count for item in evaluations)
    useful_tp = sum(item.best_of_metrics.useful_true_positives for item in evaluations)
    best_count = sum(item.best_of_metrics.best_of_count for item in evaluations)
    best_tp = sum(
        round((item.best_of_metrics.best_of_precision or 0) * item.best_of_metrics.best_of_count)
        for item in evaluations
    )
    best_of = BestOfMetrics(
        best_of_count=best_count,
        must_catch_found=must_found,
        worth_review_found=worth_found,
        useful_ground_truth_count=useful_gt,
        useful_true_positives=useful_tp,
        best_of_precision=best_tp / best_count if best_count else None,
        best_of_recall=useful_tp / useful_gt if useful_gt else None,
    )
    settled = sum(item.cost_metrics.settled_micro_thb for item in evaluations)
    reserved = sum(item.cost_metrics.reserved_micro_thb for item in evaluations)
    in_flight = sum(item.cost_metrics.in_flight_micro_thb for item in evaluations)
    ambiguous = sum(item.cost_metrics.ambiguous_micro_thb for item in evaluations)
    financially_resolved = all(item.cost_metrics.financially_resolved for item in evaluations)
    settled_thb = settled / 1_000_000
    cost = CostMetrics(
        settled_micro_thb=settled,
        reserved_micro_thb=reserved,
        in_flight_micro_thb=in_flight,
        ambiguous_micro_thb=ambiguous,
        call_count=sum(item.cost_metrics.call_count for item in evaluations),
        financially_resolved=financially_resolved,
        thb_per_source_hour=settled_thb / source_hours
        if financially_resolved and source_hours
        else None,
        thb_per_true_positive=settled_thb / counts.true_positives
        if financially_resolved and counts.true_positives
        else None,
        warnings=tuple(
            dict.fromkeys(message for item in evaluations for message in item.cost_metrics.warnings)
        ),
    )
    wall_times = [item.runtime_metrics.total_analysis_wall_time_ms for item in evaluations]
    wall_ms = (
        sum(item for item in wall_times if item is not None)
        if all(item is not None for item in wall_times)
        else None
    )
    runtime = RuntimeMetrics(
        total_analysis_wall_time_ms=wall_ms,
        source_duration_ms=source_duration_ms,
        real_time_factor=wall_ms / source_duration_ms
        if wall_ms is not None and source_duration_ms
        else None,
        compute_minutes_per_source_hour=(
            wall_ms / 60_000 / source_hours if wall_ms is not None and source_hours else None
        ),
    )
    total_bytes = sum(item.storage_metrics.total_bytes for item in evaluations)
    groups: dict[str, int] = defaultdict(int)
    for item in evaluations:
        for name, value in item.storage_metrics.groups.items():
            groups[name] += value
    storage = StorageMetrics(
        total_bytes=total_bytes,
        source_duration_ms=source_duration_ms,
        megabytes_per_source_hour=total_bytes / 1_000_000 / source_hours if source_hours else None,
        groups=dict(sorted(groups.items())),
    )
    return AggregateGroup(
        experiment_fingerprint=_experiment_fingerprint(first),
        provider=first.experiment.provider,
        model=first.experiment.model,
        split=split_override or first.split.value,
        game_profile=first.game_profile,
        case_ids=tuple(sorted(item.case_id for item in evaluations)),
        source_duration_ms=source_duration_ms,
        counts=counts,
        primary_metrics=primary,
        importance_metrics=_sum_slices(evaluations, "importance_metrics"),
        modality_metrics=_sum_slices(evaluations, "modality_metrics"),
        boundary_metrics=boundary,
        duplicate_metrics=duplicate,
        review_metrics=review,
        best_of_metrics=best_of,
        cost_metrics=cost,
        runtime_metrics=runtime,
        storage_metrics=storage,
        warnings=tuple(dict.fromkeys(message for item in evaluations for message in item.warnings)),
    )


def aggregate_evaluations(
    evaluations: Iterable[BenchmarkEvaluation],
    *,
    benchmark_id: str,
    now: datetime | None = None,
) -> BenchmarkAggregate:
    values = tuple(evaluations)
    if not values:
        raise ValidationError("No benchmark evaluation results were found to aggregate.")
    policies = {item.evaluation_policy.policy_version for item in values}
    if len(policies) != 1:
        raise ValidationError("Evaluation policy mismatch blocks benchmark comparison.")
    if any(item.benchmark_id != benchmark_id for item in values):
        raise ValidationError("Benchmark ID mismatch blocks aggregate comparison.")
    grouped: dict[tuple[str, BenchmarkSplit, str], list[BenchmarkEvaluation]] = defaultdict(list)
    for evaluation in values:
        grouped[
            (_experiment_fingerprint(evaluation), evaluation.split, evaluation.game_profile)
        ].append(evaluation)
    split_groups = [
        _aggregate_group(tuple(items))
        for _key, items in sorted(
            grouped.items(), key=lambda item: (item[0][0], item[0][1].value, item[0][2])
        )
    ]
    combined_grouped: dict[tuple[str, str], list[BenchmarkEvaluation]] = defaultdict(list)
    for evaluation in values:
        combined_grouped[(_experiment_fingerprint(evaluation), evaluation.game_profile)].append(
            evaluation
        )
    combined_groups = [
        _aggregate_group(tuple(items), split_override="combined")
        for _key, items in sorted(combined_grouped.items(), key=lambda item: item[0])
    ]
    groups = tuple([*split_groups, *combined_groups])
    return BenchmarkAggregate(
        created_at=now or datetime.now(UTC),
        benchmark_id=benchmark_id,
        evaluation_policy_version=next(iter(policies)),
        groups=groups,
        per_case=tuple(sorted(values, key=lambda item: item.case_id)),
        warnings=(),
    )


def _candidate_result_paths(dataset_path: Path, case: BenchmarkCase) -> tuple[Path, ...]:
    case_id = case.case_id
    explicit = case.result_path
    candidates: list[Path] = []
    if explicit is not None:
        explicit_path = Path(explicit)
        candidates.append(
            explicit_path if explicit_path.is_absolute() else dataset_path.parent / explicit_path
        )
    annotation_path = Path(case.annotation_path)
    if not annotation_path.is_absolute():
        annotation_path = dataset_path.parent / annotation_path
    candidates.extend(
        [
            annotation_path.parent / "results" / f"{case_id}.json",
            dataset_path.parent / "results" / f"{case_id}.json",
            dataset_path.parent / f"{case_id}.evaluation.json",
        ]
    )
    return tuple(dict.fromkeys(path.expanduser().resolve() for path in candidates))


def aggregate_dataset(
    dataset_path: Path,
    *,
    output_path: Path | None = None,
    markdown_path: Path | None = None,
) -> AggregateRun:
    try:
        dataset = BenchmarkDataset.model_validate(read_json(dataset_path))
    except Exception as exc:
        raise ValidationError(f"Benchmark dataset manifest is invalid: {dataset_path}") from exc
    evaluations: list[BenchmarkEvaluation] = []
    for case in dataset.cases:
        result_path = next(
            (path for path in _candidate_result_paths(dataset_path, case) if path.is_file()), None
        )
        if result_path is None:
            raise ValidationError(
                f"Evaluation result is missing for benchmark case {case.case_id}.",
                hint="Run: highlight benchmark evaluate <session-id> --annotations <file>.",
            )
        evaluation = load_evaluation(result_path)
        annotation_path = Path(case.annotation_path)
        if not annotation_path.is_absolute():
            annotation_path = dataset_path.parent / annotation_path
        expected_annotation_hash = annotation_sha256(annotation_path)
        if evaluation.annotation_sha256 != expected_annotation_hash:
            raise ValidationError(f"Annotation mismatch blocks comparison for case {case.case_id}.")
        if evaluation.source_sha256 != case.expected_source_sha256:
            raise ValidationError(
                f"Source identity mismatch blocks comparison for case {case.case_id}."
            )
        if evaluation.benchmark_id != dataset.benchmark_id or evaluation.case_id != case.case_id:
            raise ValidationError(f"Benchmark case identity mismatch for case {case.case_id}.")
        if evaluation.split is not case.split:
            raise ValidationError(f"Calibration/validation split mismatch for case {case.case_id}.")
        if evaluation.game_profile != case.game_profile:
            raise ValidationError(
                f"Game-profile mismatch blocks comparison for case {case.case_id}."
            )
        evaluations.append(evaluation)
    aggregate = aggregate_evaluations(evaluations, benchmark_id=dataset.benchmark_id)
    json_target = (
        (output_path or dataset_path.parent / "results" / "aggregate.json").expanduser().resolve()
    )
    markdown_target = (
        (markdown_path or dataset_path.parent / "reports" / "aggregate.md").expanduser().resolve()
    )
    atomic_write_json(json_target, aggregate.model_dump(mode="json"))
    atomic_write_bytes(markdown_target, render_markdown(aggregate).encode("utf-8"))
    return AggregateRun(aggregate=aggregate, json_path=json_target, markdown_path=markdown_target)


def _format_ratio(value: float | None, *, percent: bool = False) -> str:
    if value is None:
        return "N/A"
    return f"{value * 100:.1f}%" if percent else f"{value:.3f}"


def render_markdown(aggregate: BenchmarkAggregate) -> str:
    """Render a shareable report without local paths, media, or secrets."""

    lines = [
        "# M8A Benchmark Aggregate",
        "",
        f"Benchmark: `{aggregate.benchmark_id}`  ",
        f"Evaluation policy: `{aggregate.evaluation_policy_version}`  ",
        "",
        "| Experiment | Split | Profile | Cases | Hours | Predictions | Precision | Recall | "
        "MUST recall | "
        "Audio recall | Visual recall | Median boundary error | Duplicate rate | Review % | "
        "THB/hour | Runtime factor | Storage MB/hour | Best-of recall |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for group in aggregate.groups:
        must = next(
            (item.recall for item in group.importance_metrics if item.label == "MUST_CATCH"), None
        )
        audio = next(
            (item.recall for item in group.modality_metrics if item.label == "AUDIO"), None
        )
        visual = next(
            (item.recall for item in group.modality_metrics if item.label == "VISUAL"), None
        )
        hours = group.source_duration_ms / 3_600_000
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{group.provider}/{group.model}` ({group.experiment_fingerprint[:12]})",
                    group.split,
                    group.game_profile,
                    str(len(group.case_ids)),
                    f"{hours:.2f}",
                    str(group.counts.predictions),
                    _format_ratio(group.primary_metrics.precision),
                    _format_ratio(group.primary_metrics.recall),
                    _format_ratio(must),
                    _format_ratio(audio),
                    _format_ratio(visual),
                    _format_ratio(group.boundary_metrics.median_start_error_ms),
                    _format_ratio(group.duplicate_metrics.duplicate_rate),
                    _format_ratio(group.review_metrics.review_ratio, percent=True),
                    _format_ratio(group.cost_metrics.thb_per_source_hour),
                    _format_ratio(group.runtime_metrics.real_time_factor),
                    _format_ratio(group.storage_metrics.megabytes_per_source_hour),
                    _format_ratio(group.best_of_metrics.best_of_recall),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "Metrics are count-weighted from raw case counts; percentages are not averaged "
            "across sessions.",
            "Boundary medians use the persisted per-match measurements. Category correctness "
            "is secondary to temporal detection.",
            "",
            "## Per-case diagnostics",
            "",
        ]
    )
    for evaluation in aggregate.per_case:
        lines.append(
            f"- `{evaluation.case_id}` ({evaluation.split.value}, {evaluation.game_profile}): "
            f"precision {_format_ratio(evaluation.primary_metrics.precision)}, "
            f"recall {_format_ratio(evaluation.primary_metrics.recall)}, "
            f"{evaluation.counts.true_positives} TP / {evaluation.counts.false_positives} FP / "
            f"{evaluation.counts.false_negatives} FN."
        )
    if any(evaluation.warnings for evaluation in aggregate.per_case):
        lines.extend(["", "## Warnings", ""])
        for evaluation in aggregate.per_case:
            for warning in evaluation.warnings:
                lines.append(f"- `{evaluation.case_id}`: {warning}")
    return "\n".join(lines) + "\n"


__all__ = [
    "AggregateRun",
    "aggregate_dataset",
    "aggregate_evaluations",
    "render_markdown",
]
