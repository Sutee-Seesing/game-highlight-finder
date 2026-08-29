"""Provider-free calibration feasibility gate for candidate-local boundary refinement."""

from __future__ import annotations

from pathlib import Path
from statistics import median
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from game_highlight_finder.benchmark.evaluator import (
    annotation_sha256,
    deterministic_temporal_match,
    load_annotations,
)
from game_highlight_finder.benchmark.models import (
    BenchmarkAnnotations,
    BenchmarkDataset,
    BenchmarkSplit,
    EvaluationPolicy,
    Importance,
)
from game_highlight_finder.config import AppConfig
from game_highlight_finder.domain.models import SessionMap, Sha256
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.pipeline.boundary_refinement import plan_boundary_refinement
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.hashing import hash_file
from game_highlight_finder.storage.sessions import session_paths, source_from_artifact

BOUNDARY_REFINEMENT_FEASIBILITY_VERSION = "boundary-refinement-feasibility-v3"
DiagnosticVerdict = Literal[
    "MUST_CATCH_DETECTION_GAP",
    "DETECTION_GAPS_PRESENT",
    "BOUNDARY_HEADROOM_PRESENT",
    "NO_OBVIOUS_BOUNDARY_HEADROOM",
]
AnnotationCoverage = Literal["exhaustive", "sparse"]


class BoundaryRefinementAnnotationFeasibility(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    annotation_id: str = Field(min_length=1, max_length=128)
    importance: Importance
    event_start_ms: int = Field(ge=0)
    event_end_ms: int = Field(gt=0)
    strict_matched_candidate_id: str | None = None
    anchor_overlap_candidate_ids: tuple[str, ...] = ()
    context_reachable_candidate_ids: tuple[str, ...] = ()
    boundary_headroom: bool
    detection_gap: bool
    context_unreachable: bool

    @model_validator(mode="after")
    def flags_match_candidate_sets(self) -> BoundaryRefinementAnnotationFeasibility:
        if self.boundary_headroom != (
            bool(self.anchor_overlap_candidate_ids) and self.strict_matched_candidate_id is None
        ):
            raise ValueError("boundary headroom flag does not match candidate coverage")
        if self.detection_gap != (not self.anchor_overlap_candidate_ids):
            raise ValueError("detection gap flag does not match anchor coverage")
        if self.context_unreachable != (not self.context_reachable_candidate_ids):
            raise ValueError("context unreachable flag does not match context coverage")
        return self


class BoundaryRefinementFeasibility(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    version: str = BOUNDARY_REFINEMENT_FEASIBILITY_VERSION
    benchmark_id: str = Field(min_length=1, max_length=128)
    case_id: str = Field(min_length=1, max_length=128)
    split: Literal["calibration"] = "calibration"
    session_id: str = Field(min_length=1, max_length=128)
    source_sha256: Sha256
    annotation_sha256: Sha256
    dataset_sha256: Sha256
    evaluation_policy_fingerprint: Sha256
    scout_backend: str = Field(min_length=1, max_length=64)
    scout_model: str | None = Field(default=None, max_length=128)
    scout_prompt_version: str | None = Field(default=None, max_length=64)
    scout_provenance_source: str | None = Field(default=None, max_length=64)
    semantic_quality_applicable: bool
    annotation_coverage: AnnotationCoverage = "exhaustive"
    precision_tuning_safe: bool = True
    quality_interpretation_warning: str | None = Field(default=None, max_length=500)
    candidate_count: int = Field(ge=0)
    ground_truth_count: int = Field(ge=0)
    strict_match_count: int = Field(ge=0)
    strict_false_positive_count: int = Field(ge=0)
    strict_false_negative_count: int = Field(ge=0)
    strict_precision: float | None = Field(default=None, ge=0, le=1)
    strict_recall: float | None = Field(default=None, ge=0, le=1)
    must_catch_count: int = Field(ge=0)
    must_catch_strict_match_count: int = Field(ge=0)
    must_catch_strict_recall: float | None = Field(default=None, ge=0, le=1)
    anchor_overlap_annotation_count: int = Field(ge=0)
    context_reachable_annotation_count: int = Field(ge=0)
    detection_gap_count: int = Field(ge=0)
    context_unreachable_count: int = Field(ge=0)
    boundary_headroom_count: int = Field(ge=0)
    must_catch_detection_gap_count: int = Field(ge=0)
    must_catch_boundary_headroom_count: int = Field(ge=0)
    median_strict_start_error_ms: float | None = Field(default=None, ge=0)
    median_strict_end_error_ms: float | None = Field(default=None, ge=0)
    diagnostic_verdict: DiagnosticVerdict
    ground_truth_derived_candidate_ids: tuple[str, ...] = ()
    strict_unmatched_candidate_ids: tuple[str, ...] = ()
    confirmed_negative_candidate_ids: tuple[str, ...] = ()
    human_review_required_candidate_ids: tuple[str, ...] = ()
    candidate_review_complete: bool = False
    false_positive_suppression_safe: bool = False
    protected_positive_min_score: float | None = Field(default=None, ge=0, le=10)
    protected_positive_min_confidence: float | None = Field(default=None, ge=0, le=1)
    threshold_rejectable_confirmed_negative_candidate_ids: tuple[str, ...] = ()
    score_confidence_threshold_suppression_headroom: bool = False
    unmatched_candidate_ids: tuple[str, ...] = ()
    annotations: tuple[BoundaryRefinementAnnotationFeasibility, ...] = ()
    selection_warning: str = (
        "Candidate IDs in this artifact are derived from calibration ground truth and must never "
        "be used as a production candidate-selection policy."
    )
    provider_calls: Literal[0] = 0


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _overlaps(left_start: int, left_end: int, right_start: int, right_end: int) -> bool:
    return min(left_end, right_end) > max(left_start, right_start)


def _contains_interval(
    container_start: int,
    container_end: int,
    item_start: int,
    item_end: int,
) -> bool:
    """Return whether one half-open interval fully adjudicates another."""

    return container_start <= item_start and item_end <= container_end


def assess_boundary_refinement_feasibility(
    session_map: SessionMap,
    annotations: BenchmarkAnnotations,
    policy: EvaluationPolicy,
    *,
    dataset_sha256: str,
    annotation_document_sha256: str,
    annotation_coverage: AnnotationCoverage = "exhaustive",
) -> BoundaryRefinementFeasibility:
    """Measure whether boundary-only refinement has calibration headroom without provider I/O."""

    if abs(session_map.duration_ms - annotations.source_duration_ms) > 1_000:
        raise ValidationError("Boundary feasibility annotation duration does not match session map")
    if (
        session_map.game_profile != "unknown"
        and session_map.game_profile != annotations.game_profile
    ):
        raise ValidationError("Boundary feasibility game profile does not match session map")

    candidates = tuple(session_map.candidates)
    highlights = tuple(annotations.highlights)
    pairs = deterministic_temporal_match(candidates, highlights, policy)
    strict_by_annotation = {
        pair.truth.highlight.annotation_id: pair.prediction.candidate for pair in pairs
    }
    pair_by_annotation = {pair.truth.highlight.annotation_id: pair for pair in pairs}

    candidate_plans = {
        candidate.candidate_id: plan_boundary_refinement(candidate, session_map.duration_ms)
        for candidate in candidates
    }
    rows: list[BoundaryRefinementAnnotationFeasibility] = []
    ground_truth_derived: set[str] = set()
    for highlight in highlights:
        anchor_ids = tuple(
            candidate.candidate_id
            for candidate in candidates
            if _overlaps(
                candidate.event_start_ms,
                candidate.event_end_ms,
                highlight.event_start_ms,
                highlight.event_end_ms,
            )
        )
        context_ids = tuple(
            candidate.candidate_id
            for candidate in candidates
            if _overlaps(
                candidate_plans[candidate.candidate_id].source_start_ms,
                candidate_plans[candidate.candidate_id].source_end_ms,
                highlight.event_start_ms,
                highlight.event_end_ms,
            )
        )
        ground_truth_derived.update(anchor_ids)
        strict = strict_by_annotation.get(highlight.annotation_id)
        rows.append(
            BoundaryRefinementAnnotationFeasibility(
                annotation_id=highlight.annotation_id,
                importance=highlight.importance,
                event_start_ms=highlight.event_start_ms,
                event_end_ms=highlight.event_end_ms,
                strict_matched_candidate_id=(strict.candidate_id if strict is not None else None),
                anchor_overlap_candidate_ids=anchor_ids,
                context_reachable_candidate_ids=context_ids,
                boundary_headroom=bool(anchor_ids) and strict is None,
                detection_gap=not anchor_ids,
                context_unreachable=not context_ids,
            )
        )

    strict_count = len(pairs)
    candidate_count = len(candidates)
    truth_count = len(highlights)
    must_catch_ids = {
        highlight.annotation_id
        for highlight in highlights
        if highlight.importance is Importance.MUST_CATCH
    }
    strict_must = sum(annotation_id in strict_by_annotation for annotation_id in must_catch_ids)
    anchor_count = sum(not row.detection_gap for row in rows)
    context_count = sum(not row.context_unreachable for row in rows)
    detection_gap_count = sum(row.detection_gap for row in rows)
    context_unreachable_count = sum(row.context_unreachable for row in rows)
    headroom_count = sum(row.boundary_headroom for row in rows)
    must_detection_gap = sum(
        row.detection_gap and row.annotation_id in must_catch_ids for row in rows
    )
    must_headroom = sum(
        row.boundary_headroom and row.annotation_id in must_catch_ids for row in rows
    )

    verdict: DiagnosticVerdict
    if must_detection_gap:
        verdict = "MUST_CATCH_DETECTION_GAP"
    elif detection_gap_count:
        verdict = "DETECTION_GAPS_PRESENT"
    elif headroom_count:
        verdict = "BOUNDARY_HEADROOM_PRESENT"
    else:
        verdict = "NO_OBVIOUS_BOUNDARY_HEADROOM"

    ordered_ground_truth_candidates = tuple(
        candidate.candidate_id
        for candidate in candidates
        if candidate.candidate_id in ground_truth_derived
    )
    strict_matched_candidate_ids = {
        pair.prediction.candidate.candidate_id for pair in pairs
    }
    strict_unmatched_candidate_ids = tuple(
        candidate.candidate_id
        for candidate in candidates
        if candidate.candidate_id not in strict_matched_candidate_ids
    )
    semantic_positive_candidate_ids = set(ordered_ground_truth_candidates)
    if annotation_coverage == "exhaustive":
        confirmed_negative_candidate_ids = tuple(
            candidate.candidate_id
            for candidate in candidates
            if candidate.candidate_id not in semantic_positive_candidate_ids
        )
    else:
        confirmed_negative_candidate_ids = tuple(
            candidate.candidate_id
            for candidate in candidates
            if candidate.candidate_id not in semantic_positive_candidate_ids
            and any(
                _contains_interval(
                    boring.start_ms,
                    boring.end_ms,
                    candidate.event_start_ms,
                    candidate.event_end_ms,
                )
                for boring in annotations.boring_intervals
            )
        )
    confirmed_negative_ids = set(confirmed_negative_candidate_ids)
    human_review_required_candidate_ids = tuple(
        candidate.candidate_id
        for candidate in candidates
        if candidate.candidate_id not in semantic_positive_candidate_ids
        and candidate.candidate_id not in confirmed_negative_ids
    )
    candidate_review_complete = not human_review_required_candidate_ids
    start_errors = [pair.start_error_ms for pair in pair_by_annotation.values()]
    end_errors = [pair.end_error_ms for pair in pair_by_annotation.values()]
    scout_backend = session_map.scout_backend.strip().lower()
    scout_model = session_map.scout_metadata.get("model")
    scout_prompt_version = session_map.scout_metadata.get("window_prompt_version")
    scout_provenance_source = session_map.scout_metadata.get("scout_provenance_source")
    semantic_quality_applicable = scout_backend == "gemini"
    precision_tuning_safe = semantic_quality_applicable and annotation_coverage == "exhaustive"
    false_positive_suppression_safe = (
        semantic_quality_applicable
        and candidate_review_complete
        and bool(confirmed_negative_candidate_ids)
    )
    protected_positive_candidates = tuple(
        candidate
        for candidate in candidates
        if candidate.candidate_id in semantic_positive_candidate_ids
    )
    protected_positive_min_score = (
        min(candidate.score for candidate in protected_positive_candidates)
        if protected_positive_candidates
        else None
    )
    protected_positive_min_confidence = (
        min(candidate.confidence for candidate in protected_positive_candidates)
        if protected_positive_candidates
        else None
    )
    threshold_rejectable_confirmed_negative_candidate_ids = (
        tuple(
            candidate.candidate_id
            for candidate in candidates
            if candidate.candidate_id in confirmed_negative_ids
            and (
                candidate.score < protected_positive_min_score
                or candidate.confidence < protected_positive_min_confidence
            )
        )
        if false_positive_suppression_safe
        and protected_positive_min_score is not None
        and protected_positive_min_confidence is not None
        else ()
    )
    score_confidence_threshold_suppression_headroom = bool(
        threshold_rejectable_confirmed_negative_candidate_ids
    )
    warnings: list[str] = []
    if not semantic_quality_applicable:
        warnings.append(
            f"Scout backend {session_map.scout_backend!r} is not a semantic production Scout; "
            "feasibility metrics verify temporal/plumbing behavior only and must not be "
            "interpreted as semantic Scout detection quality."
        )
    if annotation_coverage == "sparse":
        if human_review_required_candidate_ids:
            warnings.append(
                "Calibration annotations are explicitly sparse; some semantic-unmatched "
                "candidates still require human review and are not confirmed false positives. "
                "Raw strict precision is diagnostic only and must not drive global Scout "
                "threshold tuning."
            )
        else:
            warnings.append(
                "Calibration annotations remain sparse, so source recall is not exhaustive and "
                "raw strict precision remains diagnostic. All current Scout candidates are "
                "adjudicated by highlight overlap or fully covering boring intervals; confirmed "
                "negatives may drive candidate-level false-positive suppression diagnostics only."
            )
    quality_warning = " ".join(warnings) or None
    return BoundaryRefinementFeasibility(
        benchmark_id=annotations.benchmark_id,
        case_id=annotations.case_id,
        session_id=session_map.session_id,
        source_sha256=annotations.source_sha256,
        annotation_sha256=annotation_document_sha256,
        dataset_sha256=dataset_sha256,
        evaluation_policy_fingerprint=policy.fingerprint(),
        scout_backend=session_map.scout_backend,
        scout_model=scout_model,
        scout_prompt_version=scout_prompt_version,
        scout_provenance_source=scout_provenance_source,
        semantic_quality_applicable=semantic_quality_applicable,
        annotation_coverage=annotation_coverage,
        precision_tuning_safe=precision_tuning_safe,
        quality_interpretation_warning=quality_warning,
        candidate_count=candidate_count,
        ground_truth_count=truth_count,
        strict_match_count=strict_count,
        strict_false_positive_count=candidate_count - strict_count,
        strict_false_negative_count=truth_count - strict_count,
        strict_precision=_ratio(strict_count, candidate_count),
        strict_recall=_ratio(strict_count, truth_count),
        must_catch_count=len(must_catch_ids),
        must_catch_strict_match_count=strict_must,
        must_catch_strict_recall=_ratio(strict_must, len(must_catch_ids)),
        anchor_overlap_annotation_count=anchor_count,
        context_reachable_annotation_count=context_count,
        detection_gap_count=detection_gap_count,
        context_unreachable_count=context_unreachable_count,
        boundary_headroom_count=headroom_count,
        must_catch_detection_gap_count=must_detection_gap,
        must_catch_boundary_headroom_count=must_headroom,
        median_strict_start_error_ms=(float(median(start_errors)) if start_errors else None),
        median_strict_end_error_ms=(float(median(end_errors)) if end_errors else None),
        diagnostic_verdict=verdict,
        ground_truth_derived_candidate_ids=ordered_ground_truth_candidates,
        strict_unmatched_candidate_ids=strict_unmatched_candidate_ids,
        confirmed_negative_candidate_ids=confirmed_negative_candidate_ids,
        human_review_required_candidate_ids=human_review_required_candidate_ids,
        candidate_review_complete=candidate_review_complete,
        false_positive_suppression_safe=false_positive_suppression_safe,
        protected_positive_min_score=protected_positive_min_score,
        protected_positive_min_confidence=protected_positive_min_confidence,
        threshold_rejectable_confirmed_negative_candidate_ids=(
            threshold_rejectable_confirmed_negative_candidate_ids
        ),
        score_confidence_threshold_suppression_headroom=(
            score_confidence_threshold_suppression_headroom
        ),
        unmatched_candidate_ids=strict_unmatched_candidate_ids,
        annotations=tuple(rows),
    )


def run_boundary_refinement_feasibility(
    session_id: str,
    dataset_path: Path,
    annotations_path: Path,
    config: AppConfig,
    *,
    output_path: Path | None = None,
) -> tuple[BoundaryRefinementFeasibility, Path]:
    """Validate one declared calibration case, assess it locally, and persist private JSON."""

    resolved_dataset = dataset_path.expanduser().resolve()
    resolved_annotations = annotations_path.expanduser().resolve()
    if not resolved_dataset.is_file():
        raise ValidationError("Boundary feasibility dataset manifest does not exist")
    if not resolved_annotations.is_file():
        raise ValidationError("Boundary feasibility annotation file does not exist")
    try:
        dataset = BenchmarkDataset.model_validate(read_json(resolved_dataset))
    except Exception as exc:
        raise ValidationError(
            "Boundary feasibility dataset manifest is invalid", hint=str(exc)
        ) from exc
    annotations = load_annotations(resolved_annotations)
    matching = [case for case in dataset.cases if case.case_id == annotations.case_id]
    if len(matching) != 1:
        raise ValidationError("Boundary feasibility annotation case is not uniquely declared")
    case = matching[0]
    if case.split is not BenchmarkSplit.CALIBRATION:
        raise ValidationError(
            "Boundary feasibility is calibration-only; validation/holdout data is forbidden"
        )
    if dataset.benchmark_id != annotations.benchmark_id:
        raise ValidationError("Boundary feasibility benchmark identity mismatch")
    if case.expected_source_sha256 != annotations.source_sha256:
        raise ValidationError("Boundary feasibility source identity mismatch")
    if case.game_profile != annotations.game_profile:
        raise ValidationError("Boundary feasibility game profile mismatch")
    declared_annotation = case.annotation_path.expanduser()
    if not declared_annotation.is_absolute():
        declared_annotation = (resolved_dataset.parent / declared_annotation).resolve()
    else:
        declared_annotation = declared_annotation.resolve()
    if declared_annotation != resolved_annotations:
        raise ValidationError(
            "Boundary feasibility annotation path differs from dataset declaration"
        )

    paths = session_paths(config.storage.data_dir, session_id)
    if not paths.source.is_file() or not paths.session_map.is_file():
        raise ValidationError("Boundary feasibility requires committed source and session map")
    source = source_from_artifact(paths.source)
    if source.sha256 != case.expected_source_sha256:
        raise ValidationError("Boundary feasibility session source does not match dataset case")
    try:
        session_map = SessionMap.model_validate(read_json(paths.session_map))
    except Exception as exc:
        raise ValidationError("Boundary feasibility session map is invalid", hint=str(exc)) from exc
    if session_map.source_id != source.source_id:
        raise ValidationError("Boundary feasibility session map source identity mismatch")
    if session_map.duration_ms != source.duration_ms:
        raise ValidationError("Boundary feasibility session map duration mismatch")

    assert dataset.evaluation_policy is not None
    result = assess_boundary_refinement_feasibility(
        session_map,
        annotations,
        dataset.evaluation_policy,
        dataset_sha256=hash_file(resolved_dataset),
        annotation_document_sha256=annotation_sha256(resolved_annotations),
        annotation_coverage=(
            "sparse" if "sparse-annotations" in case.tags else "exhaustive"
        ),
    )
    target = (
        output_path.expanduser().resolve()
        if output_path is not None
        else config.storage.data_dir.resolve()
        / "benchmarks"
        / "private"
        / "boundary_refinement"
        / f"{annotations.case_id}.feasibility.json"
    )
    atomic_write_json(target, result.model_dump(mode="json"))
    return result, target


__all__ = [
    "BOUNDARY_REFINEMENT_FEASIBILITY_VERSION",
    "BoundaryRefinementAnnotationFeasibility",
    "BoundaryRefinementFeasibility",
    "assess_boundary_refinement_feasibility",
    "run_boundary_refinement_feasibility",
]
