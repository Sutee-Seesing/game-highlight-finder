"""Pure/local M8A benchmark evaluation.

This module consumes already-persisted session artifacts and human annotations.  It
does not import or invoke a Scout, provider adapter, upload API, or network client.
The evaluator is intentionally a measurement layer: changing annotations changes the
evaluation identity, never the inference/cache identity.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from game_highlight_finder import __version__
from game_highlight_finder.benchmark.models import (
    EVALUATION_POLICY_VERSION,
    AnnotatedHighlight,
    AnnotatedMatch,
    BenchmarkAnnotations,
    BenchmarkEvaluation,
    BenchmarkSplit,
    BestOfMetrics,
    BoringMetrics,
    BoundaryMeasurement,
    BoundaryMetrics,
    CategoryConfusion,
    CategoryMetrics,
    CostMetrics,
    DuplicateCandidate,
    DuplicateMetrics,
    EvaluationCounts,
    EvaluationPolicy,
    ExperimentIdentity,
    ExtraCandidate,
    Importance,
    MatchedPair,
    MatchMetrics,
    MissedAnnotation,
    Modality,
    PrimaryMetrics,
    ReviewMetrics,
    RuntimeMetrics,
    SliceMetric,
    StorageMetrics,
)
from game_highlight_finder.config import AppConfig
from game_highlight_finder.cost.ledger import CostLedger, LedgerRecord, LifecycleStatus
from game_highlight_finder.cost.service import budget_to_micro_thb
from game_highlight_finder.domain.models import Candidate, Match, SessionMap, SourceAsset
from game_highlight_finder.errors import StorageError, ValidationError
from game_highlight_finder.pipeline.extraction import ExtractionManifest
from game_highlight_finder.pipeline.ranking import RankingArtifact, rank_session_map
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.hashing import hash_file
from game_highlight_finder.storage.sessions import (
    SessionPaths,
    load_manifest,
    session_paths,
    source_from_artifact,
)


class AnnotationValidationSummary(BaseModel):
    """Stable, human-readable summary returned by ``benchmark validate``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_identity: str
    benchmark_id: str
    case_id: str
    source_duration_ms: int
    match_count: int
    highlight_count: int
    must_catch_count: int
    worth_review_count: int
    optional_count: int
    boring_interval_count: int
    modality_breakdown: dict[str, int]
    total_annotated_highlight_duration_ms: int
    annotation_sha256: str


class EvaluationRun(BaseModel):
    """Evaluation plus the private path where a CLI invocation persisted it."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    evaluation: BenchmarkEvaluation
    output_path: Path


@dataclass(frozen=True)
class _Prediction:
    candidate: Candidate

    @property
    def start_ms(self) -> int:
        return self.candidate.event_start_ms

    @property
    def end_ms(self) -> int:
        return self.candidate.event_end_ms


@dataclass(frozen=True)
class _Truth:
    highlight: AnnotatedHighlight

    @property
    def start_ms(self) -> int:
        return self.highlight.event_start_ms

    @property
    def end_ms(self) -> int:
        return self.highlight.event_end_ms


@dataclass(frozen=True)
class _Pair:
    prediction: _Prediction
    truth: _Truth
    iou: float
    start_error_ms: int
    end_error_ms: int


def annotation_sha256(path: Path) -> str:
    """Hash annotation bytes exactly as persisted, including formatting."""

    try:
        return hash_file(path)
    except OSError as exc:
        raise ValidationError(f"Annotation file cannot be read: {path}", hint=str(exc)) from exc


def load_annotations(path: Path) -> BenchmarkAnnotations:
    """Load and validate a bounded annotation document, failing closed."""

    try:
        value = read_json(path)
        return BenchmarkAnnotations.model_validate(value)
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError(
            f"Annotation JSON is invalid: {path}",
            hint="Fix the annotation schema, timestamps, and duplicate IDs before retrying.",
        ) from exc


def validate_annotations_file(path: Path) -> AnnotationValidationSummary:
    annotations = load_annotations(path)
    source_identity = "PASS (annotation hash only)"
    if annotations.source_path is not None:
        source = annotations.source_path.expanduser().resolve()
        if not source.is_file():
            raise ValidationError("Annotated source file is missing.", hint=str(source))
        actual = hash_file(source, source=True)
        if actual != annotations.source_sha256:
            raise ValidationError(
                "Source identity FAIL: annotation SHA-256 does not match the local source.",
                hint=f"Expected {annotations.source_sha256}; observed {actual}.",
            )
        source_identity = "PASS"
    counts = {importance.value: 0 for importance in Importance}
    modalities = {modality.value: 0 for modality in Modality}
    for highlight in annotations.highlights:
        counts[highlight.importance.value] += 1
        modalities[highlight.modality.value] += 1
    return AnnotationValidationSummary(
        source_identity=source_identity,
        benchmark_id=annotations.benchmark_id,
        case_id=annotations.case_id,
        source_duration_ms=annotations.source_duration_ms,
        match_count=len(annotations.matches),
        highlight_count=len(annotations.highlights),
        must_catch_count=counts[Importance.MUST_CATCH.value],
        worth_review_count=counts[Importance.WORTH_REVIEW.value],
        optional_count=counts[Importance.OPTIONAL.value],
        boring_interval_count=len(annotations.boring_intervals),
        modality_breakdown=modalities,
        total_annotated_highlight_duration_ms=sum(
            item.event_end_ms - item.event_start_ms for item in annotations.highlights
        ),
        annotation_sha256=annotation_sha256(path),
    )


def interval_iou(left_start: int, left_end: int, right_start: int, right_end: int) -> float:
    """Intersection-over-union for half-open integer intervals."""

    intersection = max(0, min(left_end, right_end) - max(left_start, right_start))
    union = max(left_end, right_end) - min(left_start, right_start)
    return intersection / union if union > 0 else 0.0


def _interval_gap(left_start: int, left_end: int, right_start: int, right_end: int) -> int:
    if left_end < right_start:
        return right_start - left_end
    if right_end < left_start:
        return left_start - right_end
    return 0


def temporal_match_qualifies(
    prediction_start_ms: int,
    prediction_end_ms: int,
    truth_start_ms: int,
    truth_end_ms: int,
    policy: EvaluationPolicy,
) -> bool:
    """Apply the documented M8A temporal policy without category gating.

    A pair qualifies when it has the configured event IoU, or when both boundaries
    are within the configured tolerance and the intervals are not farther apart
    than that tolerance.  The latter makes the tolerance edge explicit for small
    adjacent/offset intervals while still rejecting unrelated events.
    """

    iou = interval_iou(prediction_start_ms, prediction_end_ms, truth_start_ms, truth_end_ms)
    if iou >= policy.event_iou_threshold:
        return True
    start_error = abs(prediction_start_ms - truth_start_ms)
    end_error = abs(prediction_end_ms - truth_end_ms)
    return (
        start_error <= policy.boundary_tolerance_ms
        and end_error <= policy.boundary_tolerance_ms
        and _interval_gap(prediction_start_ms, prediction_end_ms, truth_start_ms, truth_end_ms)
        <= policy.boundary_tolerance_ms
    )


def _candidate_pair(
    prediction: _Prediction, truth: _Truth, policy: EvaluationPolicy
) -> _Pair | None:
    if not temporal_match_qualifies(
        prediction.start_ms,
        prediction.end_ms,
        truth.start_ms,
        truth.end_ms,
        policy,
    ):
        return None
    return _Pair(
        prediction=prediction,
        truth=truth,
        iou=interval_iou(
            prediction.start_ms,
            prediction.end_ms,
            truth.start_ms,
            truth.end_ms,
        ),
        start_error_ms=abs(prediction.start_ms - truth.start_ms),
        end_error_ms=abs(prediction.end_ms - truth.end_ms),
    )


def deterministic_temporal_match(
    predictions: Sequence[Candidate],
    highlights: Sequence[AnnotatedHighlight],
    policy: EvaluationPolicy,
) -> tuple[_Pair, ...]:
    """Perform deterministic one-to-one matching with no randomness.

    Qualifying pairs are ordered by descending IoU, lowest combined boundary error,
    descending prediction score, then stable prediction/annotation IDs.  Greedy
    selection is intentional and is persisted through the resulting pair list.
    """

    potential: list[_Pair] = []
    for candidate in predictions:
        for highlight in highlights:
            pair = _candidate_pair(_Prediction(candidate), _Truth(highlight), policy)
            if pair is not None:
                potential.append(pair)
    potential.sort(
        key=lambda pair: (
            -pair.iou,
            pair.start_error_ms + pair.end_error_ms,
            -pair.prediction.candidate.score,
            pair.prediction.candidate.candidate_id,
            pair.truth.highlight.annotation_id,
        )
    )
    used_predictions: set[str] = set()
    used_annotations: set[str] = set()
    selected: list[_Pair] = []
    for pair in potential:
        prediction_id = pair.prediction.candidate.candidate_id
        annotation_id = pair.truth.highlight.annotation_id
        if prediction_id in used_predictions or annotation_id in used_annotations:
            continue
        used_predictions.add(prediction_id)
        used_annotations.add(annotation_id)
        selected.append(pair)
    selected.sort(
        key=lambda pair: (
            pair.truth.highlight.event_start_ms,
            pair.truth.highlight.annotation_id,
            pair.prediction.candidate.candidate_id,
        )
    )
    return tuple(selected)


def _median(values: Iterable[int | float]) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _p90(values: Iterable[int | float]) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    index = max(0, math.ceil(len(ordered) * 0.9) - 1)
    return ordered[index]


def _safe_ratio(numerator: int | float, denominator: int | float) -> float | None:
    return float(numerator) / float(denominator) if denominator else None


def _union_duration(intervals: Iterable[tuple[int, int]]) -> int:
    ordered = sorted((start, end) for start, end in intervals if end > start)
    if not ordered:
        return 0
    total = 0
    current_start, current_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            total += current_end - current_start
            current_start, current_end = start, end
    return total + current_end - current_start


def _intersection_duration(left: tuple[int, int], right: tuple[int, int]) -> int:
    return max(0, min(left[1], right[1]) - max(left[0], right[0]))


def _review_intervals(candidates: Iterable[Candidate]) -> list[tuple[int, int]]:
    return [
        (candidate.clip_start_ms, candidate.clip_end_ms)
        for candidate in candidates
        if candidate.clip_start_ms is not None
        and candidate.clip_end_ms is not None
        and candidate.clip_end_ms > candidate.clip_start_ms
    ]


def _metric_slices(
    labels: Sequence[str],
    highlights: Sequence[AnnotatedHighlight],
    pairs: Sequence[_Pair],
    *,
    attribute: str,
) -> tuple[SliceMetric, ...]:
    by_label: dict[str, list[AnnotatedHighlight]] = {label: [] for label in labels}
    matched_by_label: dict[str, set[str]] = {label: set() for label in labels}
    predictions_by_label: dict[str, set[str]] = {label: set() for label in labels}
    for highlight in highlights:
        label = str(getattr(highlight, attribute).value)
        if label in by_label:
            by_label[label].append(highlight)
    for pair in pairs:
        label = str(getattr(pair.truth.highlight, attribute).value)
        if label in matched_by_label:
            matched_by_label[label].add(pair.truth.highlight.annotation_id)
            predictions_by_label[label].add(pair.prediction.candidate.candidate_id)
    return tuple(
        SliceMetric(
            label=label,
            ground_truth=len(by_label[label]),
            matched=len(matched_by_label[label]),
            recall=_safe_ratio(len(matched_by_label[label]), len(by_label[label])),
            predictions=len(predictions_by_label[label]),
        )
        for label in labels
    )


def _validate_source_identity(source: SourceAsset) -> None:
    if not source.path.is_file():
        raise ValidationError("Original source is missing; benchmark evaluation refused.")
    try:
        stat = source.path.stat()
        if stat.st_size != source.size_bytes or stat.st_mtime_ns != source.mtime_ns:
            raise ValidationError("Original source identity changed; benchmark evaluation refused.")
        if hash_file(source.path, source=True) != source.sha256:
            raise ValidationError("Original source SHA-256 changed; benchmark evaluation refused.")
    except OSError as exc:
        raise ValidationError(
            "Original source cannot be verified; benchmark evaluation refused.", hint=str(exc)
        ) from exc


def _load_completed_session(
    session_id: str,
    config: AppConfig,
) -> tuple[SessionPaths, SourceAsset, SessionMap, RankingArtifact | None, list[str]]:
    paths = session_paths(config.storage.data_dir, session_id)
    if not paths.root.is_dir():
        raise ValidationError(
            f"Session does not exist: {session_id}",
            hint="Run: highlight analyze <video> or highlight resume <session-id>.",
        )
    if (
        not paths.source.is_file()
        or not paths.session_map.is_file()
        or not paths.manifest.is_file()
    ):
        raise ValidationError(
            "Session is incomplete: source, manifest, and SessionMap are required.",
            hint=f"Run: highlight resume {session_id}",
        )
    source = source_from_artifact(paths.source)
    _validate_source_identity(source)
    try:
        session_map = SessionMap.model_validate(read_json(paths.session_map))
        manifest = load_manifest(paths.manifest)
    except Exception as exc:
        raise ValidationError(
            "Session artifacts are invalid; benchmark evaluation refused.",
            hint=f"Run: highlight resume {session_id}",
        ) from exc
    if session_map.session_id != session_id or session_map.source_id != source.source_id:
        raise ValidationError("SessionMap identity does not match the persisted source.")
    if session_map.duration_ms != source.duration_ms:
        raise ValidationError("SessionMap duration does not match the persisted source.")
    required = ["scout", "reconcile"]
    if session_map.candidates:
        required.append("extract")
    incomplete: list[str] = []
    for stage_name in required:
        stage = manifest.stages.get(stage_name)
        if stage is None or stage.status.value != "COMPLETED":
            incomplete.append(stage_name)
    if incomplete:
        raise ValidationError(
            "Session is incomplete; benchmark evaluation requires completed stages: "
            + ", ".join(incomplete),
            hint=f"Run: highlight resume {session_id}",
        )
    warnings: list[str] = []
    ranking: RankingArtifact | None = None
    if paths.ranking_path.is_file():
        try:
            candidate_ranking = RankingArtifact.model_validate(read_json(paths.ranking_path))
            expected = rank_session_map(session_map, best_of_limit=config.report.best_of_limit)
            if candidate_ranking.model_dump(mode="json") != expected.model_dump(mode="json"):
                warnings.append("ranking artifact is stale; deterministic score order used")
            else:
                ranking = candidate_ranking
        except Exception:
            warnings.append("ranking artifact is invalid; deterministic score order used")
    elif session_map.candidates:
        warnings.append("ranking artifact is unavailable; deterministic score order used")
    if session_map.candidates:
        if not paths.extraction_manifest.is_file():
            raise ValidationError(
                "Extraction manifest is missing for a session with candidates.",
                hint=f"Run: highlight resume {session_id}",
            )
        try:
            extraction = ExtractionManifest.model_validate(read_json(paths.extraction_manifest))
        except Exception as exc:
            raise ValidationError(
                "Extraction manifest is invalid; benchmark evaluation refused.",
                hint=f"Run: highlight resume {session_id}",
            ) from exc
        if extraction.status != "COMPLETED" or extraction.source_sha256 != source.sha256:
            raise ValidationError(
                "Extraction is incomplete or has a mismatched source identity.",
                hint=f"Run: highlight resume {session_id}",
            )
        records = {record.candidate_id: record for record in extraction.records}
        if set(records) != {candidate.candidate_id for candidate in session_map.candidates}:
            raise ValidationError(
                "Extraction manifest does not cover the canonical candidate library.",
                hint=f"Run: highlight resume {session_id}",
            )
        for candidate_id, record in records.items():
            if record.status != "COMPLETED":
                raise ValidationError(
                    f"Extraction record is incomplete: {candidate_id}",
                    hint=f"Run: highlight resume {session_id}",
                )
            output = (paths.root / record.output_path).resolve()
            try:
                output.relative_to(paths.root.resolve())
            except ValueError as exc:
                raise ValidationError(
                    f"Extraction output escapes the session: {candidate_id}"
                ) from exc
            if (
                not output.is_file()
                or record.output_sha256 is None
                or hash_file(output) != record.output_sha256
            ):
                raise ValidationError(
                    f"Extraction output is missing or hash-invalid: {candidate_id}",
                    hint=f"Run: highlight resume {session_id}",
                )
    return paths, source, session_map, ranking, warnings


def build_experiment_identity(
    source: SourceAsset,
    annotations: BenchmarkAnnotations,
    annotations_sha256: str,
    session_map: SessionMap,
    config: AppConfig,
) -> ExperimentIdentity:
    """Capture semantic inference/evaluation dimensions without ground-truth leakage."""

    def fingerprint(value: object) -> str:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
        return hashlib.sha256(encoded).hexdigest()

    metadata = session_map.scout_metadata
    provider = metadata.get("provider", metadata.get("backend", config.scout.backend))
    if provider == "fake" and config.scout.backend != "fake":
        provider = config.scout.backend
    model = metadata.get("model", "fake-scout-v1" if provider == "fake" else config.scout.model)
    prompt_version = metadata.get("prompt_version", config.scout.window_prompt_version)
    proxy_settings = {
        "proxy": config.media.proxy.model_dump(mode="json"),
        "audio": config.media.audio.model_dump(mode="json"),
    }
    signal_settings = config.signals.model_dump(mode="json")
    return ExperimentIdentity(
        provider=provider,
        model=model,
        billing_mode=config.scout.billing_mode,
        media_resolution=config.scout.media_resolution,
        thinking_level=config.scout.thinking_level,
        prompt_version=prompt_version,
        provider_schema_version=config.scout.schema_version,
        canonicalization_version=session_map.canonicalization_version,
        window_duration_seconds=config.scout.window_duration_seconds,
        window_overlap_seconds=config.scout.window_overlap_seconds,
        proxy_settings_fingerprint=fingerprint(proxy_settings),
        signal_settings_fingerprint=fingerprint(signal_settings),
        extraction_config_fingerprint=fingerprint(config.media.extraction.model_dump(mode="json")),
        ranking_config_fingerprint=fingerprint({"best_of_limit": config.report.best_of_limit}),
        evaluator_policy_version=EVALUATION_POLICY_VERSION,
        source_sha256=source.sha256,
        annotation_sha256=annotations_sha256,
        application_version=__version__,
        git_version=os.environ.get("GITHUB_SHA"),
    )


def _cost_metrics(
    config: AppConfig, session_id: str, source_duration_ms: int, true_positives: int
) -> CostMetrics:
    ledger_path = (
        config.cost.ledger_path or config.storage.data_dir.resolve() / "cost" / "ledger.sqlite3"
    )
    calls: tuple[LedgerRecord, ...] = ()
    hold_warning: list[str] = []
    if ledger_path.is_file():
        try:
            ledger = CostLedger(
                ledger_path,
                budget_micro_thb=budget_to_micro_thb(config.cost.monthly_budget_thb),
            )
            calls = tuple(call for call in ledger.list_calls() if call.session_id == session_id)
            if ledger.safety_hold() is not None:
                hold_warning.append("global cost safety hold is active")
        except (StorageError, OSError) as exc:
            raise ValidationError(
                "Authoritative cost ledger could not be read.", hint=str(exc)
            ) from exc
    settled = sum(
        call.settled_cost_micro_thb or 0 for call in calls if call.status is LifecycleStatus.SETTLED
    )
    reserved = sum(
        call.reserved_cost_micro_thb for call in calls if call.status is LifecycleStatus.RESERVED
    )
    in_flight = sum(
        call.reserved_cost_micro_thb for call in calls if call.status is LifecycleStatus.IN_FLIGHT
    )
    ambiguous = sum(
        call.reserved_cost_micro_thb for call in calls if call.status is LifecycleStatus.AMBIGUOUS
    )
    unresolved = reserved + in_flight + ambiguous > 0
    warnings = list(hold_warning)
    if unresolved:
        warnings.append(
            "financial lifecycle is unresolved; ambiguous exposure is not settled actual cost"
        )
    financially_resolved = not unresolved
    source_hours = source_duration_ms / 3_600_000
    settled_thb = settled / 1_000_000
    return CostMetrics(
        settled_micro_thb=settled,
        reserved_micro_thb=reserved,
        in_flight_micro_thb=in_flight,
        ambiguous_micro_thb=ambiguous,
        call_count=len(calls),
        financially_resolved=financially_resolved,
        thb_per_source_hour=(
            settled_thb / source_hours if financially_resolved and source_hours else None
        ),
        thb_per_true_positive=(
            settled_thb / true_positives if financially_resolved and true_positives else None
        ),
        warnings=tuple(warnings),
    )


def _runtime_metrics(manifest: Any, source_duration_ms: int) -> RuntimeMetrics:
    starts = [
        stage.started_at for stage in manifest.stages.values() if stage.started_at is not None
    ]
    ends = [
        stage.completed_at for stage in manifest.stages.values() if stage.completed_at is not None
    ]
    if not starts or not ends:
        return RuntimeMetrics(source_duration_ms=source_duration_ms)
    wall_ms = max(0, round((max(ends) - min(starts)).total_seconds() * 1000))
    source_hours = source_duration_ms / 3_600_000
    return RuntimeMetrics(
        total_analysis_wall_time_ms=wall_ms,
        source_duration_ms=source_duration_ms,
        real_time_factor=wall_ms / source_duration_ms if source_duration_ms else None,
        compute_minutes_per_source_hour=(wall_ms / 60_000 / source_hours if source_hours else None),
    )


def _storage_metrics(paths: SessionPaths, source: SourceAsset) -> StorageMetrics:
    groups: dict[str, int] = defaultdict(int)
    source_resolved = source.path.resolve()
    for path in paths.root.rglob("*"):
        if not path.is_file():
            continue
        try:
            if path.resolve() == source_resolved:
                continue
            size = path.stat().st_size
            relative = path.relative_to(paths.root).as_posix()
        except OSError:
            continue
        top = relative.split("/", 1)[0]
        group = {
            "proxy": "proxy/audio/signals",
            "audio": "proxy/audio/signals",
            "signals": "proxy/audio/signals",
            "scout": "Scout artifacts",
            "candidates": "extracted clips",
            "thumbnails": "thumbnails",
            "reports": "report/ranking",
        }.get(top, "other")
        groups[group] += size
    total = sum(groups.values())
    source_hours = source.duration_ms / 3_600_000
    return StorageMetrics(
        total_bytes=total,
        source_duration_ms=source.duration_ms,
        megabytes_per_source_hour=(total / 1_000_000 / source_hours if source_hours else None),
        groups=dict(sorted(groups.items())),
    )


def _match_metrics(
    predicted_matches: Sequence[Match],
    annotated_matches: Sequence[AnnotatedMatch],
    policy: EvaluationPolicy,
) -> MatchMetrics:
    if not annotated_matches:
        return MatchMetrics(available=False)
    potential: list[tuple[float, int, float, Match, AnnotatedMatch]] = []
    for predicted in predicted_matches:
        for annotated in annotated_matches:
            if temporal_match_qualifies(
                predicted.start_ms,
                predicted.end_ms,
                annotated.start_ms,
                annotated.end_ms,
                policy,
            ):
                potential.append(
                    (
                        interval_iou(
                            predicted.start_ms,
                            predicted.end_ms,
                            annotated.start_ms,
                            annotated.end_ms,
                        ),
                        abs(predicted.start_ms - annotated.start_ms)
                        + abs(predicted.end_ms - annotated.end_ms),
                        -predicted.confidence,
                        predicted,
                        annotated,
                    )
                )
    potential.sort(
        key=lambda item: (-item[0], item[1], item[2], item[3].match_id, item[4].annotation_id)
    )
    used_pred: set[str] = set()
    used_ann: set[str] = set()
    errors: list[tuple[int, int]] = []
    for _iou, _error, _confidence, predicted, annotated in potential:
        if predicted.match_id in used_pred or annotated.annotation_id in used_ann:
            continue
        used_pred.add(predicted.match_id)
        used_ann.add(annotated.annotation_id)
        errors.append(
            (abs(predicted.start_ms - annotated.start_ms), abs(predicted.end_ms - annotated.end_ms))
        )
    return MatchMetrics(
        available=True,
        predicted_match_count=len(predicted_matches),
        annotated_match_count=len(annotated_matches),
        matched_match_count=len(errors),
        median_start_error_ms=_median(item[0] for item in errors),
        median_end_error_ms=_median(item[1] for item in errors),
        unmatched_predicted_matches=len(predicted_matches) - len(used_pred),
        missed_annotated_matches=len(annotated_matches) - len(used_ann),
    )


def evaluate_session(
    session_id: str,
    annotations_path: Path,
    config: AppConfig,
    *,
    policy: EvaluationPolicy | None = None,
    split: BenchmarkSplit = BenchmarkSplit.CALIBRATION,
    now: datetime | None = None,
) -> BenchmarkEvaluation:
    """Evaluate a completed local session without invoking any provider."""

    annotations = load_annotations(annotations_path)
    annotation_hash = annotation_sha256(annotations_path)
    paths, source, session_map, ranking, warnings = _load_completed_session(session_id, config)
    if annotations.source_sha256 != source.sha256:
        raise ValidationError(
            "Annotation/source identity mismatch; evaluation refused.",
            hint=f"Annotation expects {annotations.source_sha256}; source is {source.sha256}.",
        )
    if abs(annotations.source_duration_ms - source.duration_ms) > 1_000:
        raise ValidationError("Annotation duration does not match the source within tolerance.")
    if (
        annotations.game_profile != session_map.game_profile
        and session_map.game_profile != "unknown"
    ):
        raise ValidationError(
            "Annotation game profile does not match the completed session map.",
            hint=f"Annotation={annotations.game_profile}; session={session_map.game_profile}.",
        )
    active_policy = policy or EvaluationPolicy()
    predictions = tuple(session_map.candidates)
    highlights = tuple(annotations.highlights)
    pairs = deterministic_temporal_match(predictions, highlights, active_policy)
    matched_prediction_ids = {pair.prediction.candidate.candidate_id for pair in pairs}
    matched_annotation_ids = {pair.truth.highlight.annotation_id for pair in pairs}
    true_positives = len(pairs)
    false_positives = len(predictions) - true_positives
    false_negatives = len(highlights) - true_positives
    counts = EvaluationCounts(
        predictions=len(predictions),
        ground_truth_highlights=len(highlights),
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
    )
    precision = _safe_ratio(true_positives, len(predictions))
    recall = _safe_ratio(true_positives, len(highlights))
    f1 = (
        (2 * precision * recall / (precision + recall))
        if precision is not None and recall is not None and precision + recall
        else None
    )
    primary = PrimaryMetrics(
        **counts.model_dump(mode="python"),
        precision=precision,
        recall=recall,
        f1=f1,
    )
    boundary_measurements = tuple(
        BoundaryMeasurement(
            prediction_id=pair.prediction.candidate.candidate_id,
            annotation_id=pair.truth.highlight.annotation_id,
            prediction_start_ms=pair.prediction.start_ms,
            prediction_end_ms=pair.prediction.end_ms,
            annotation_start_ms=pair.truth.start_ms,
            annotation_end_ms=pair.truth.end_ms,
            start_error_ms=pair.start_error_ms,
            end_error_ms=pair.end_error_ms,
            combined_boundary_error_ms=pair.start_error_ms + pair.end_error_ms,
            event_iou=pair.iou,
        )
        for pair in pairs
    )
    matched_models: list[MatchedPair] = []
    for pair, measurement in zip(pairs, boundary_measurements, strict=True):
        predicted_category = pair.prediction.candidate.category
        annotated_category = pair.truth.highlight.category
        matched_models.append(
            MatchedPair(
                prediction_id=pair.prediction.candidate.candidate_id,
                annotation_id=pair.truth.highlight.annotation_id,
                importance=pair.truth.highlight.importance,
                modality=pair.truth.highlight.modality,
                predicted_category=predicted_category,
                annotated_category=annotated_category,
                category_match=(
                    predicted_category == annotated_category
                    if annotated_category is not None
                    else None
                ),
                prediction_score=pair.prediction.candidate.score,
                prediction_confidence=pair.prediction.candidate.confidence,
                measurement=measurement,
            )
        )
    missed = tuple(
        MissedAnnotation(
            annotation_id=highlight.annotation_id,
            importance=highlight.importance,
            modality=highlight.modality,
            start_ms=highlight.event_start_ms,
            end_ms=highlight.event_end_ms,
            category=highlight.category,
        )
        for highlight in highlights
        if highlight.annotation_id not in matched_annotation_ids
    )
    extras = tuple(
        ExtraCandidate(
            candidate_id=candidate.candidate_id,
            score=candidate.score,
            confidence=candidate.confidence,
            category=candidate.category,
            start_ms=candidate.event_start_ms,
            end_ms=candidate.event_end_ms,
        )
        for candidate in predictions
        if candidate.candidate_id not in matched_prediction_ids
    )
    duplicate_models: list[DuplicateCandidate] = []
    for extra in extras:
        duplicate_target: str | None = None
        for highlight in highlights:
            if (
                temporal_match_qualifies(
                    extra.start_ms,
                    extra.end_ms,
                    highlight.event_start_ms,
                    highlight.event_end_ms,
                    active_policy,
                )
                and highlight.annotation_id in matched_annotation_ids
            ):
                duplicate_target = highlight.annotation_id
                break
        if duplicate_target is not None:
            duplicate_models.append(
                DuplicateCandidate(
                    **extra.model_dump(mode="python"), matched_annotation_id=duplicate_target
                )
            )
    duplicate_count = len(duplicate_models)
    duplicate_metrics = DuplicateMetrics(
        duplicate_prediction_count=duplicate_count,
        duplicate_rate=_safe_ratio(duplicate_count, len(predictions)),
    )
    importance_metrics = _metric_slices(
        [importance.value for importance in Importance], highlights, pairs, attribute="importance"
    )
    modality_metrics = _metric_slices(
        [modality.value for modality in Modality], highlights, pairs, attribute="modality"
    )
    boundary_metrics = BoundaryMetrics(
        matched_count=len(boundary_measurements),
        median_start_error_ms=_median(item.start_error_ms for item in boundary_measurements),
        median_end_error_ms=_median(item.end_error_ms for item in boundary_measurements),
        median_iou=_median(item.event_iou for item in boundary_measurements),
        p90_boundary_error_ms=_p90(
            item.combined_boundary_error_ms for item in boundary_measurements
        ),
        measurements=boundary_measurements,
    )
    if ranking is not None:
        best_of_ids = tuple(ranking.best_of_candidate_ids)
    elif session_map.best_of_candidate_ids:
        best_of_ids = tuple(session_map.best_of_candidate_ids)
    else:
        best_of_ids = tuple(
            candidate.candidate_id
            for candidate in sorted(
                predictions,
                key=lambda candidate: (
                    -candidate.score,
                    -candidate.confidence,
                    candidate.event_start_ms,
                    candidate.candidate_id,
                ),
            )[:3]
        )
    best_pairs = [pair for pair in pairs if pair.prediction.candidate.candidate_id in best_of_ids]
    best_annotation_ids = {pair.truth.highlight.annotation_id for pair in best_pairs}
    useful_highlights = [
        highlight
        for highlight in highlights
        if highlight.importance in {Importance.MUST_CATCH, Importance.WORTH_REVIEW}
    ]
    useful_ids = {highlight.annotation_id for highlight in useful_highlights}
    best_of_metrics = BestOfMetrics(
        best_of_count=len(best_of_ids),
        must_catch_found=sum(
            1
            for highlight in highlights
            if highlight.annotation_id in best_annotation_ids
            and highlight.importance is Importance.MUST_CATCH
        ),
        worth_review_found=sum(
            1
            for highlight in highlights
            if highlight.annotation_id in best_annotation_ids
            and highlight.importance is Importance.WORTH_REVIEW
        ),
        useful_ground_truth_count=len(useful_highlights),
        useful_true_positives=len(best_annotation_ids & useful_ids),
        best_of_precision=_safe_ratio(len(best_pairs), len(best_of_ids)),
        best_of_recall=_safe_ratio(len(best_annotation_ids & useful_ids), len(useful_highlights)),
    )
    review_intervals = _review_intervals(predictions)
    review_ms = _union_duration(review_intervals)
    review_metrics = ReviewMetrics(
        candidate_review_ms=review_ms,
        source_duration_ms=source.duration_ms,
        review_ratio=_safe_ratio(review_ms, source.duration_ms),
        review_percentage=(review_ms / source.duration_ms * 100 if source.duration_ms else None),
    )
    boring_intervals = [(item.start_ms, item.end_ms) for item in annotations.boring_intervals]
    boring_candidates = [
        candidate
        for candidate in predictions
        if any(
            _intersection_duration((candidate.event_start_ms, candidate.event_end_ms), interval) > 0
            for interval in boring_intervals
        )
    ]
    boring_review_intersections = [
        (max(start, boring_start), min(end, boring_end))
        for start, end in review_intervals
        for boring_start, boring_end in boring_intervals
        if min(end, boring_end) > max(start, boring_start)
    ]
    boring_metrics = BoringMetrics(
        annotated_boring_interval_count=len(boring_intervals),
        candidates_overlapping_boring=len(boring_candidates),
        false_positives_per_source_hour=(
            len(boring_candidates) / (source.duration_ms / 3_600_000)
            if source.duration_ms
            else None
        ),
        candidate_review_ms_inside_boring=_union_duration(boring_review_intersections),
    )
    category_counter: dict[tuple[str | None, str | None], list[int]] = defaultdict(lambda: [0, 0])
    for matched_model in matched_models:
        key = (matched_model.predicted_category, matched_model.annotated_category)
        category_counter[key][0] += 1
        if matched_model.category_match:
            category_counter[key][1] += 1
    category_metrics = CategoryMetrics(
        annotated_category_count=sum(
            1 for highlight in highlights if highlight.category is not None
        ),
        category_matches=sum(1 for pair in matched_models if pair.category_match),
        confusion=tuple(
            CategoryConfusion(
                predicted_category=key[0],
                annotated_category=key[1],
                matches=value[0],
                correct=value[1],
            )
            for key, value in sorted(
                category_counter.items(), key=lambda item: (item[0][0] or "", item[0][1] or "")
            )
        ),
    )
    match_metrics = _match_metrics(session_map.matches, annotations.matches, active_policy)
    cost_metrics = _cost_metrics(config, session_id, source.duration_ms, true_positives)
    runtime_metrics = _runtime_metrics(load_manifest(paths.manifest), source.duration_ms)
    storage_metrics = _storage_metrics(paths, source)
    warnings.extend(cost_metrics.warnings)
    if not annotations.matches:
        warnings.append("manual match annotations are absent; match metrics are N/A")
    if not annotations.boring_intervals:
        warnings.append("boring intervals are absent; boring false-positive metrics are limited")
    evaluation = BenchmarkEvaluation(
        created_at=now or datetime.now(UTC),
        evaluation_policy=active_policy,
        benchmark_id=annotations.benchmark_id,
        case_id=annotations.case_id,
        split=split,
        game_profile=annotations.game_profile,
        session_id=session_id,
        source_id=source.source_id,
        source_duration_ms=source.duration_ms,
        source_sha256=source.sha256,
        annotation_sha256=annotation_hash,
        experiment=build_experiment_identity(
            source, annotations, annotation_hash, session_map, config
        ),
        counts=counts,
        primary_metrics=primary,
        importance_metrics=importance_metrics,
        modality_metrics=modality_metrics,
        boundary_metrics=boundary_metrics,
        duplicate_metrics=duplicate_metrics,
        best_of_metrics=best_of_metrics,
        boring_metrics=boring_metrics,
        category_metrics=category_metrics,
        match_metrics=match_metrics,
        review_metrics=review_metrics,
        cost_metrics=cost_metrics,
        runtime_metrics=runtime_metrics,
        storage_metrics=storage_metrics,
        matched_pairs=tuple(matched_models),
        missed_annotations=missed,
        extra_candidates=extras,
        duplicate_candidates=tuple(duplicate_models),
        warnings=tuple(dict.fromkeys(warnings)),
    )
    return evaluation


def persist_evaluation(evaluation: BenchmarkEvaluation, output_path: Path) -> Path:
    """Atomically write one private evaluation JSON artifact."""

    atomic_write_json(output_path, evaluation.model_dump(mode="json"))
    return output_path


def load_evaluation(path: Path) -> BenchmarkEvaluation:
    try:
        return BenchmarkEvaluation.model_validate(read_json(path))
    except Exception as exc:
        raise ValidationError(f"Benchmark evaluation JSON is invalid: {path}") from exc


__all__ = [
    "AnnotationValidationSummary",
    "EvaluationRun",
    "annotation_sha256",
    "build_experiment_identity",
    "deterministic_temporal_match",
    "evaluate_session",
    "interval_iou",
    "load_annotations",
    "load_evaluation",
    "persist_evaluation",
    "temporal_match_qualifies",
    "validate_annotations_file",
]
