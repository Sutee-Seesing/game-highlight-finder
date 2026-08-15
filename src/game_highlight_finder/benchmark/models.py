"""Strict, versioned models for the M8A benchmark foundation.

The benchmark layer is deliberately independent from Scout/provider execution.  These
models describe private dataset manifests, human annotations, immutable experiment
identity, and machine-readable evaluation results.  They contain no media bytes and
never persist provider credentials or raw provider responses.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from game_highlight_finder.domain.models import Sha256

BENCHMARK_SCHEMA_VERSION = 1
ANNOTATION_SCHEMA_VERSION = 1
EVALUATION_SCHEMA_VERSION = 1
EVALUATOR_VERSION = "m8a-evaluator-v1"
EVALUATION_POLICY_VERSION = "m8-eval-v1"

MAX_ANNOTATION_DURATION_MS = 86_400_000
MAX_TEXT = 2_000
MAX_ID = 128


class BenchmarkModel(BaseModel):
    """Immutable persisted model with a closed schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class BenchmarkSplit(StrEnum):
    CALIBRATION = "calibration"
    VALIDATION = "validation"


class Importance(StrEnum):
    MUST_CATCH = "MUST_CATCH"
    WORTH_REVIEW = "WORTH_REVIEW"
    OPTIONAL = "OPTIONAL"


class Modality(StrEnum):
    VISUAL = "VISUAL"
    AUDIO = "AUDIO"
    VISUAL_AND_AUDIO = "VISUAL_AND_AUDIO"
    UNKNOWN = "UNKNOWN"


def _strict_integer(value: object, *, field_name: str) -> object:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer number of milliseconds")
    return value


def _finite_number(value: object, *, field_name: str) -> object:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite number")
    if not math.isfinite(float(value)):
        raise ValueError(f"{field_name} must be a finite number")
    return value


BoundedId = Annotated[str, Field(min_length=1, max_length=MAX_ID)]
BoundedText = Annotated[str, Field(min_length=1, max_length=MAX_TEXT)]


class EvaluationPolicy(BenchmarkModel):
    """Versioned matching policy persisted with every evaluation."""

    schema_version: Literal[1] = 1
    policy_version: str = Field(
        default=EVALUATION_POLICY_VERSION,
        pattern=r"^[a-z0-9][a-z0-9._-]{1,63}$",
    )
    event_iou_threshold: float = Field(default=0.25, ge=0, le=1)
    boundary_tolerance_ms: int = Field(default=3_000, ge=0, le=600_000)

    @field_validator("event_iou_threshold", mode="before")
    @classmethod
    def strict_iou_threshold(cls, value: object) -> object:
        return _finite_number(value, field_name="event_iou_threshold")

    @field_validator("boundary_tolerance_ms", mode="before")
    @classmethod
    def strict_tolerance(cls, value: object) -> object:
        return _strict_integer(value, field_name="boundary_tolerance_ms")

    def semantic_payload(self) -> dict[str, object]:
        """Return the complete, ordered-independent policy identity payload.

        Only fields that change the evaluation ruler belong here.  Timestamps,
        paths, Python representations, and other operational metadata are
        intentionally excluded so every process derives the same identity.
        """

        return {
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "event_iou_threshold": self.event_iou_threshold,
            "boundary_tolerance_ms": self.boundary_tolerance_ms,
        }

    def fingerprint(self) -> str:
        """Return the canonical SHA-256 identity of this evaluation ruler."""

        encoded = json.dumps(self.semantic_payload(), sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        return hashlib.sha256(encoded).hexdigest()

    @property
    def evaluation_policy_fingerprint(self) -> str:
        """Readable property alias used by persisted benchmark artifacts."""

        return self.fingerprint()

    @property
    def semantic_fingerprint(self) -> str:
        """Alias for callers that name the identity after its semantic payload."""

        return self.fingerprint()

    @property
    def policy_fingerprint(self) -> str:
        return self.fingerprint()


def compute_evaluation_policy_fingerprint(policy: EvaluationPolicy) -> str:
    """Compute a policy fingerprint without relying on object repr/order."""

    return policy.fingerprint()


def evaluation_policy_fingerprint(policy: EvaluationPolicy) -> str:
    """Short public alias for the canonical policy fingerprint helper."""

    return compute_evaluation_policy_fingerprint(policy)


class BenchmarkCase(BenchmarkModel):
    """One private source/annotation pair in a benchmark dataset."""

    case_id: str = Field(
        min_length=1,
        max_length=MAX_ID,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
    )
    source_path: Path
    expected_source_sha256: Sha256
    annotation_path: Path
    game_profile: str = Field(
        default="unknown",
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$",
    )
    split: BenchmarkSplit
    tags: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    notes: str = Field(default="", max_length=MAX_TEXT)
    # Optional private locator for the result generated by ``benchmark evaluate``.
    # It is never copied into a sanitized aggregate report.
    result_path: Path | None = None

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(tag.strip().lower() for tag in value)
        if any(not tag or len(tag) > 64 for tag in normalized):
            raise ValueError("benchmark tags must be non-empty strings of at most 64 characters")
        if len(set(normalized)) != len(normalized):
            raise ValueError("benchmark tags must be unique")
        return normalized


class BenchmarkDataset(BenchmarkModel):
    """Strict versioned manifest; source bytes are intentionally absent."""

    schema_version: Literal[1] = 1
    manifest_type: Literal["dataset"] = "dataset"
    benchmark_id: str = Field(
        min_length=1,
        max_length=MAX_ID,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
    )
    name: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=MAX_TEXT)
    evaluation_policy_version: str = Field(
        default=EVALUATION_POLICY_VERSION,
        pattern=r"^[a-z0-9][a-z0-9._-]{1,63}$",
    )
    cases: tuple[BenchmarkCase, ...] = Field(default_factory=tuple, max_length=10_000)
    evaluation_policy: EvaluationPolicy | None = None
    # Populated deterministically from ``evaluation_policy``.  It remains
    # optional only while loading accepted legacy manifests that omitted the
    # full policy; the known historical m8-eval-v1 ruler is migrated below.
    evaluation_policy_fingerprint: str | None = None

    @model_validator(mode="after")
    def unique_cases_and_policy(self) -> BenchmarkDataset:
        ids = [case.case_id for case in self.cases]
        if len(set(ids)) != len(ids):
            raise ValueError("benchmark case IDs must be unique")
        if self.evaluation_policy is None:
            if self.evaluation_policy_version != EVALUATION_POLICY_VERSION:
                raise ValueError(
                    "dataset evaluation_policy is required for non-legacy policy versions"
                )
            # Deterministic migration for the only accepted legacy manifest:
            # m8-eval-v1 means exactly the historical 0.25 / 3000 ruler.
            object.__setattr__(self, "evaluation_policy", EvaluationPolicy())
        assert self.evaluation_policy is not None
        if self.evaluation_policy.policy_version != self.evaluation_policy_version:
            raise ValueError("dataset policy version does not match its policy")
        computed = self.evaluation_policy.fingerprint()
        if (
            self.evaluation_policy_fingerprint is not None
            and self.evaluation_policy_fingerprint != computed
        ):
            raise ValueError("dataset evaluation policy fingerprint does not match its policy")
        object.__setattr__(self, "evaluation_policy_fingerprint", computed)
        return self

    @property
    def policy_fingerprint(self) -> str:
        """Authoritative policy identity for dataset comparisons."""

        assert self.evaluation_policy is not None
        return self.evaluation_policy.fingerprint()


class AnnotatedMatch(BenchmarkModel):
    annotation_id: str = Field(min_length=1, max_length=MAX_ID)
    ordinal: int | None = Field(default=None, ge=0)
    start_ms: int = Field(ge=0, le=MAX_ANNOTATION_DURATION_MS)
    end_ms: int = Field(gt=0, le=MAX_ANNOTATION_DURATION_MS)
    label: str | None = Field(default=None, max_length=240)
    confidence: float | None = Field(default=None, ge=0, le=1)
    notes: str | None = Field(default=None, max_length=MAX_TEXT)

    @field_validator("ordinal", "start_ms", "end_ms", mode="before")
    @classmethod
    def strict_match_integers(cls, value: object) -> object:
        if value is None:
            return value
        return _strict_integer(value, field_name="match time/index")

    @field_validator("confidence", mode="before")
    @classmethod
    def strict_confidence(cls, value: object) -> object:
        if value is None:
            return None
        return _finite_number(value, field_name="match confidence")

    @model_validator(mode="after")
    def interval_is_ordered(self) -> AnnotatedMatch:
        if self.end_ms <= self.start_ms:
            raise ValueError("annotated match end must be greater than start")
        return self


class AnnotatedHighlight(BenchmarkModel):
    annotation_id: str = Field(min_length=1, max_length=MAX_ID)
    match_annotation_id: str | None = Field(default=None, max_length=MAX_ID)
    event_start_ms: int = Field(ge=0, le=MAX_ANNOTATION_DURATION_MS)
    event_end_ms: int = Field(gt=0, le=MAX_ANNOTATION_DURATION_MS)
    setup_start_ms: int | None = Field(default=None, ge=0, le=MAX_ANNOTATION_DURATION_MS)
    payoff_end_ms: int | None = Field(default=None, gt=0, le=MAX_ANNOTATION_DURATION_MS)
    category: str | None = Field(default=None, max_length=64)
    importance: Importance
    modality: Modality
    notes: str | None = Field(default=None, max_length=MAX_TEXT)

    @field_validator(
        "event_start_ms", "event_end_ms", "setup_start_ms", "payoff_end_ms", mode="before"
    )
    @classmethod
    def strict_highlight_integers(cls, value: object) -> object:
        if value is None:
            return value
        return _strict_integer(value, field_name="highlight timestamp")

    @field_validator("category")
    @classmethod
    def normalize_category(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        return normalized or None

    @model_validator(mode="after")
    def interval_context_is_ordered(self) -> AnnotatedHighlight:
        if self.event_end_ms <= self.event_start_ms:
            raise ValueError("annotated highlight event end must be greater than start")
        if self.setup_start_ms is not None and self.setup_start_ms > self.event_start_ms:
            raise ValueError("highlight setup must not start after the event")
        if self.payoff_end_ms is not None and self.payoff_end_ms < self.event_end_ms:
            raise ValueError("highlight payoff must include the event")
        return self


class BoringInterval(BenchmarkModel):
    annotation_id: str = Field(min_length=1, max_length=MAX_ID)
    start_ms: int = Field(ge=0, le=MAX_ANNOTATION_DURATION_MS)
    end_ms: int = Field(gt=0, le=MAX_ANNOTATION_DURATION_MS)
    notes: str | None = Field(default=None, max_length=MAX_TEXT)

    @field_validator("start_ms", "end_ms", mode="before")
    @classmethod
    def strict_boring_integers(cls, value: object) -> object:
        return _strict_integer(value, field_name="boring interval timestamp")

    @model_validator(mode="after")
    def interval_is_ordered(self) -> BoringInterval:
        if self.end_ms <= self.start_ms:
            raise ValueError("boring interval end must be greater than start")
        return self


class BenchmarkAnnotations(BenchmarkModel):
    """Private manual ground truth.  IDs are stable across annotation revisions."""

    schema_version: Literal[1] = 1
    annotation_version: str = Field(default="m8-annotation-v1", min_length=1, max_length=64)
    benchmark_id: str = Field(min_length=1, max_length=MAX_ID)
    case_id: str = Field(min_length=1, max_length=MAX_ID)
    source_sha256: Sha256
    source_duration_ms: int = Field(gt=0, le=MAX_ANNOTATION_DURATION_MS)
    game_profile: str = Field(
        default="unknown",
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$",
    )
    annotated_by: str = Field(default="local", min_length=1, max_length=128)
    matches: tuple[AnnotatedMatch, ...] = Field(default_factory=tuple, max_length=2_000)
    highlights: tuple[AnnotatedHighlight, ...] = Field(default_factory=tuple, max_length=20_000)
    boring_intervals: tuple[BoringInterval, ...] = Field(default_factory=tuple, max_length=20_000)
    notes: str = Field(default="", max_length=MAX_TEXT)
    # This locator is private operational metadata.  It is omitted from aggregate
    # reports and can be removed before sharing an annotation file.
    source_path: Path | None = None

    @field_validator("source_duration_ms", mode="before")
    @classmethod
    def strict_source_duration(cls, value: object) -> object:
        return _strict_integer(value, field_name="source_duration_ms")

    @model_validator(mode="after")
    def validate_annotations(self) -> BenchmarkAnnotations:
        ids = [item.annotation_id for item in self.matches]
        ids.extend(item.annotation_id for item in self.highlights)
        ids.extend(item.annotation_id for item in self.boring_intervals)
        if len(set(ids)) != len(ids):
            raise ValueError(
                "annotation IDs must be unique across matches, highlights, and boring intervals"
            )
        match_ids = {item.annotation_id for item in self.matches}
        for match in self.matches:
            if match.end_ms > self.source_duration_ms:
                raise ValueError("annotated match exceeds source duration")
        for highlight in self.highlights:
            if highlight.event_end_ms > self.source_duration_ms:
                raise ValueError("annotated highlight exceeds source duration")
            if (
                highlight.setup_start_ms is not None
                and highlight.setup_start_ms > self.source_duration_ms
            ):
                raise ValueError("highlight setup exceeds source duration")
            if (
                highlight.payoff_end_ms is not None
                and highlight.payoff_end_ms > self.source_duration_ms
            ):
                raise ValueError("highlight payoff exceeds source duration")
            if (
                highlight.match_annotation_id is not None
                and highlight.match_annotation_id not in match_ids
            ):
                raise ValueError("highlight references an unknown annotated match")
        for interval in self.boring_intervals:
            if interval.end_ms > self.source_duration_ms:
                raise ValueError("boring interval exceeds source duration")
        return self


class ExperimentIdentity(BenchmarkModel):
    """All configuration dimensions that make two provider runs comparable."""

    schema_version: Literal[1] = 1
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=256)
    billing_mode: str = Field(min_length=1, max_length=64)
    media_resolution: str = Field(min_length=1, max_length=64)
    thinking_level: str = Field(min_length=1, max_length=64)
    prompt_version: str = Field(min_length=1, max_length=128)
    provider_schema_version: int = Field(ge=1, le=100)
    canonicalization_version: str = Field(min_length=1, max_length=128)
    window_duration_seconds: int = Field(gt=0, le=86_400)
    window_overlap_seconds: int = Field(ge=0, le=86_399)
    proxy_settings_fingerprint: str = Field(min_length=1, max_length=256)
    signal_settings_fingerprint: str = Field(min_length=1, max_length=256)
    extraction_config_fingerprint: str = Field(min_length=1, max_length=256)
    ranking_config_fingerprint: str = Field(min_length=1, max_length=256)
    evaluator_policy_version: str = Field(min_length=1, max_length=64)
    # Full semantic policy identity.  The empty default only supports loading
    # pre-hardening M8A results; it is filled with the known legacy ruler below.
    evaluator_policy_fingerprint: str = ""
    source_sha256: Sha256
    annotation_sha256: Sha256
    application_version: str | None = Field(default=None, max_length=128)
    git_version: str | None = Field(default=None, max_length=128)

    @field_validator(
        "provider_schema_version",
        "window_duration_seconds",
        "window_overlap_seconds",
        mode="before",
    )
    @classmethod
    def strict_identity_integers(cls, value: object) -> object:
        return _strict_integer(value, field_name="experiment identity integer")

    @model_validator(mode="after")
    def window_bounds_are_sane(self) -> ExperimentIdentity:
        if self.window_overlap_seconds >= self.window_duration_seconds:
            raise ValueError("experiment window overlap must be shorter than duration")
        if not self.evaluator_policy_fingerprint:
            if self.evaluator_policy_version != EVALUATION_POLICY_VERSION:
                raise ValueError("experiment policy fingerprint is required for non-legacy policy")
            object.__setattr__(
                self,
                "evaluator_policy_fingerprint",
                EvaluationPolicy().fingerprint(),
            )
        if len(self.evaluator_policy_fingerprint) != 64 or any(
            char not in "0123456789abcdef" for char in self.evaluator_policy_fingerprint
        ):
            raise ValueError("experiment evaluator policy fingerprint must be lowercase SHA-256")
        return self


class EvaluationCounts(BenchmarkModel):
    predictions: int = Field(ge=0)
    ground_truth_highlights: int = Field(ge=0)
    true_positives: int = Field(ge=0)
    false_positives: int = Field(ge=0)
    false_negatives: int = Field(ge=0)


class PrimaryMetrics(EvaluationCounts):
    precision: float | None = Field(default=None, ge=0, le=1)
    recall: float | None = Field(default=None, ge=0, le=1)
    f1: float | None = Field(default=None, ge=0, le=1)


class SliceMetric(BenchmarkModel):
    label: str = Field(min_length=1, max_length=64)
    ground_truth: int = Field(ge=0)
    matched: int = Field(ge=0)
    recall: float | None = Field(default=None, ge=0, le=1)
    predictions: int = Field(default=0, ge=0)
    false_positives: int = Field(default=0, ge=0)


class BoundaryMeasurement(BenchmarkModel):
    prediction_id: str = Field(min_length=1, max_length=MAX_ID)
    annotation_id: str = Field(min_length=1, max_length=MAX_ID)
    prediction_start_ms: int = Field(ge=0)
    prediction_end_ms: int = Field(gt=0)
    annotation_start_ms: int = Field(ge=0)
    annotation_end_ms: int = Field(gt=0)
    start_error_ms: int = Field(ge=0)
    end_error_ms: int = Field(ge=0)
    combined_boundary_error_ms: int = Field(ge=0)
    event_iou: float = Field(ge=0, le=1)


class MatchedPair(BenchmarkModel):
    prediction_id: str = Field(min_length=1, max_length=MAX_ID)
    annotation_id: str = Field(min_length=1, max_length=MAX_ID)
    importance: Importance
    modality: Modality
    predicted_category: str | None = Field(default=None, max_length=64)
    annotated_category: str | None = Field(default=None, max_length=64)
    category_match: bool | None = None
    prediction_score: float = Field(ge=0, le=10)
    prediction_confidence: float = Field(ge=0, le=1)
    measurement: BoundaryMeasurement


class MissedAnnotation(BenchmarkModel):
    annotation_id: str = Field(min_length=1, max_length=MAX_ID)
    importance: Importance
    modality: Modality
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    category: str | None = Field(default=None, max_length=64)


class ExtraCandidate(BenchmarkModel):
    candidate_id: str = Field(min_length=1, max_length=MAX_ID)
    score: float = Field(ge=0, le=10)
    confidence: float = Field(ge=0, le=1)
    category: str = Field(min_length=1, max_length=64)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)


class DuplicateCandidate(ExtraCandidate):
    matched_annotation_id: str | None = Field(default=None, max_length=MAX_ID)


class BoundaryMetrics(BenchmarkModel):
    matched_count: int = Field(ge=0)
    median_start_error_ms: float | None = Field(default=None, ge=0)
    median_end_error_ms: float | None = Field(default=None, ge=0)
    median_iou: float | None = Field(default=None, ge=0, le=1)
    p90_boundary_error_ms: float | None = Field(default=None, ge=0)
    measurements: tuple[BoundaryMeasurement, ...] = Field(default_factory=tuple, max_length=20_000)


class DuplicateMetrics(BenchmarkModel):
    duplicate_prediction_count: int = Field(ge=0)
    duplicate_rate: float | None = Field(default=None, ge=0, le=1)


class ReviewMetrics(BenchmarkModel):
    candidate_review_ms: int = Field(ge=0)
    source_duration_ms: int = Field(gt=0)
    review_ratio: float | None = Field(default=None, ge=0)
    review_percentage: float | None = Field(default=None, ge=0)


class BestOfMetrics(BenchmarkModel):
    best_of_count: int = Field(ge=0)
    must_catch_found: int = Field(ge=0)
    worth_review_found: int = Field(ge=0)
    useful_ground_truth_count: int = Field(ge=0)
    useful_true_positives: int = Field(ge=0)
    best_of_precision: float | None = Field(default=None, ge=0, le=1)
    best_of_recall: float | None = Field(default=None, ge=0, le=1)


class BoringMetrics(BenchmarkModel):
    annotated_boring_interval_count: int = Field(ge=0)
    candidates_overlapping_boring: int = Field(ge=0)
    false_positives_per_source_hour: float | None = Field(default=None, ge=0)
    candidate_review_ms_inside_boring: int = Field(ge=0)


class CategoryConfusion(BenchmarkModel):
    predicted_category: str | None = Field(default=None, max_length=64)
    annotated_category: str | None = Field(default=None, max_length=64)
    matches: int = Field(ge=0)
    correct: int = Field(ge=0)


class CategoryMetrics(BenchmarkModel):
    annotated_category_count: int = Field(ge=0)
    category_matches: int = Field(ge=0)
    confusion: tuple[CategoryConfusion, ...] = Field(default_factory=tuple, max_length=10_000)


class MatchMetrics(BenchmarkModel):
    available: bool
    predicted_match_count: int | None = Field(default=None, ge=0)
    annotated_match_count: int | None = Field(default=None, ge=0)
    matched_match_count: int | None = Field(default=None, ge=0)
    median_start_error_ms: float | None = Field(default=None, ge=0)
    median_end_error_ms: float | None = Field(default=None, ge=0)
    unmatched_predicted_matches: int | None = Field(default=None, ge=0)
    missed_annotated_matches: int | None = Field(default=None, ge=0)


class CostMetrics(BenchmarkModel):
    settled_micro_thb: int = Field(ge=0)
    reserved_micro_thb: int = Field(ge=0)
    in_flight_micro_thb: int = Field(ge=0)
    ambiguous_micro_thb: int = Field(ge=0)
    call_count: int = Field(ge=0)
    financially_resolved: bool
    thb_per_source_hour: float | None = Field(default=None, ge=0)
    thb_per_true_positive: float | None = Field(default=None, ge=0)
    warnings: tuple[str, ...] = Field(default_factory=tuple, max_length=100)


class RuntimeMetrics(BenchmarkModel):
    total_analysis_wall_time_ms: int | None = Field(default=None, ge=0)
    source_duration_ms: int = Field(gt=0)
    real_time_factor: float | None = Field(default=None, ge=0)
    compute_minutes_per_source_hour: float | None = Field(default=None, ge=0)


class StorageMetrics(BenchmarkModel):
    total_bytes: int = Field(ge=0)
    source_duration_ms: int = Field(gt=0)
    megabytes_per_source_hour: float | None = Field(default=None, ge=0)
    groups: dict[str, int] = Field(default_factory=dict, max_length=32)


class BenchmarkEvaluation(BenchmarkModel):
    """Authoritative JSON result for one completed session/case."""

    schema_version: Literal[1] = 1
    evaluator_version: str = Field(default=EVALUATOR_VERSION, min_length=1, max_length=64)
    created_at: datetime
    evaluation_policy: EvaluationPolicy
    evaluation_policy_fingerprint: str = ""
    benchmark_id: str = Field(min_length=1, max_length=MAX_ID)
    case_id: str = Field(min_length=1, max_length=MAX_ID)
    split: BenchmarkSplit
    game_profile: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(min_length=1, max_length=128)
    source_duration_ms: int = Field(gt=0)
    source_sha256: Sha256
    annotation_sha256: Sha256
    experiment: ExperimentIdentity
    counts: EvaluationCounts
    primary_metrics: PrimaryMetrics
    importance_metrics: tuple[SliceMetric, ...] = Field(default_factory=tuple, max_length=8)
    modality_metrics: tuple[SliceMetric, ...] = Field(default_factory=tuple, max_length=8)
    boundary_metrics: BoundaryMetrics
    duplicate_metrics: DuplicateMetrics
    best_of_metrics: BestOfMetrics
    boring_metrics: BoringMetrics
    category_metrics: CategoryMetrics
    match_metrics: MatchMetrics
    review_metrics: ReviewMetrics
    cost_metrics: CostMetrics
    runtime_metrics: RuntimeMetrics
    storage_metrics: StorageMetrics
    matched_pairs: tuple[MatchedPair, ...] = Field(default_factory=tuple, max_length=20_000)
    missed_annotations: tuple[MissedAnnotation, ...] = Field(
        default_factory=tuple, max_length=20_000
    )
    extra_candidates: tuple[ExtraCandidate, ...] = Field(default_factory=tuple, max_length=20_000)
    duplicate_candidates: tuple[DuplicateCandidate, ...] = Field(
        default_factory=tuple, max_length=20_000
    )
    warnings: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    # Deterministic result identity.  ``created_at`` is excluded so re-persisting
    # the same local artifacts yields the same identity; annotation bytes remain
    # part of the identity through annotation_sha256.
    evaluation_fingerprint: str = ""

    @field_validator("source_duration_ms", mode="before")
    @classmethod
    def strict_evaluation_duration(cls, value: object) -> object:
        return _strict_integer(value, field_name="evaluation source_duration_ms")

    @model_validator(mode="after")
    def identities_agree(self) -> BenchmarkEvaluation:
        if self.experiment.source_sha256 != self.source_sha256:
            raise ValueError("experiment and evaluation source hashes disagree")
        if self.experiment.annotation_sha256 != self.annotation_sha256:
            raise ValueError("experiment and evaluation annotation hashes disagree")
        if self.experiment.evaluator_policy_version != self.evaluation_policy.policy_version:
            raise ValueError("experiment and evaluation policy versions disagree")
        computed_policy = self.evaluation_policy.fingerprint()
        if (
            self.evaluation_policy_fingerprint
            and self.evaluation_policy_fingerprint != computed_policy
        ):
            raise ValueError("evaluation policy fingerprint does not match its policy")
        object.__setattr__(self, "evaluation_policy_fingerprint", computed_policy)
        if self.experiment.evaluator_policy_fingerprint != computed_policy:
            raise ValueError("experiment and evaluation policy fingerprints disagree")
        payload = self.model_dump(
            mode="json",
            exclude={"created_at", "evaluation_fingerprint"},
        )
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        computed_result = hashlib.sha256(encoded).hexdigest()
        if self.evaluation_fingerprint and self.evaluation_fingerprint != computed_result:
            raise ValueError("evaluation fingerprint does not match evaluation contents")
        object.__setattr__(self, "evaluation_fingerprint", computed_result)
        return self

    @property
    def policy_fingerprint(self) -> str:
        return self.evaluation_policy_fingerprint


class AggregateGroup(BenchmarkModel):
    """Count-weighted aggregate for one experiment/split/profile slice."""

    experiment_fingerprint: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=256)
    split: str = Field(pattern=r"^(calibration|validation|combined)$")
    game_profile: str = Field(min_length=1, max_length=64)
    case_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=10_000)
    source_duration_ms: int = Field(gt=0)
    counts: EvaluationCounts
    primary_metrics: PrimaryMetrics
    importance_metrics: tuple[SliceMetric, ...] = Field(default_factory=tuple, max_length=8)
    modality_metrics: tuple[SliceMetric, ...] = Field(default_factory=tuple, max_length=8)
    boundary_metrics: BoundaryMetrics
    duplicate_metrics: DuplicateMetrics
    review_metrics: ReviewMetrics
    best_of_metrics: BestOfMetrics
    cost_metrics: CostMetrics
    runtime_metrics: RuntimeMetrics
    storage_metrics: StorageMetrics
    warnings: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    experiment_label: str | None = Field(default=None, max_length=240)
    result_set_id: str | None = Field(default=None, max_length=MAX_ID)


class BenchmarkAggregate(BenchmarkModel):
    schema_version: Literal[1] = 1
    evaluator_version: str = Field(default=EVALUATOR_VERSION, min_length=1, max_length=64)
    created_at: datetime
    benchmark_id: str = Field(min_length=1, max_length=MAX_ID)
    evaluation_policy_version: str = Field(min_length=1, max_length=64)
    evaluation_policy: EvaluationPolicy | None = None
    evaluation_policy_fingerprint: str = ""
    comparison_id: str | None = Field(default=None, max_length=MAX_ID)
    result_set_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=1_000)
    groups: tuple[AggregateGroup, ...] = Field(default_factory=tuple, max_length=10_000)
    per_case: tuple[BenchmarkEvaluation, ...] = Field(default_factory=tuple, max_length=10_000)
    warnings: tuple[str, ...] = Field(default_factory=tuple, max_length=100)

    @model_validator(mode="after")
    def policy_identity_is_consistent(self) -> BenchmarkAggregate:
        if self.evaluation_policy is None:
            if self.evaluation_policy_version != EVALUATION_POLICY_VERSION:
                raise ValueError("aggregate evaluation policy is required for non-legacy policy")
            object.__setattr__(self, "evaluation_policy", EvaluationPolicy())
        assert self.evaluation_policy is not None
        if self.evaluation_policy.policy_version != self.evaluation_policy_version:
            raise ValueError("aggregate policy version does not match its policy")
        computed = self.evaluation_policy.fingerprint()
        if self.evaluation_policy_fingerprint and self.evaluation_policy_fingerprint != computed:
            raise ValueError("aggregate policy fingerprint does not match its policy")
        object.__setattr__(self, "evaluation_policy_fingerprint", computed)
        return self

    @property
    def policy_fingerprint(self) -> str:
        return self.evaluation_policy_fingerprint


class BenchmarkResultRef(BenchmarkModel):
    """One case evaluation reference in a semantic experiment result set."""

    schema_version: Literal[1] = 1
    case_id: str = Field(min_length=1, max_length=MAX_ID)
    evaluation_path: Path


class BenchmarkResultSet(BenchmarkModel):
    """A single experiment's completed evaluations over benchmark cases."""

    schema_version: Literal[1] = 1
    manifest_type: Literal["result_set"] = "result_set"
    result_set_id: str = Field(
        min_length=1,
        max_length=MAX_ID,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
    )
    label: str = Field(min_length=1, max_length=240)
    benchmark_id: str = Field(min_length=1, max_length=MAX_ID)
    evaluation_policy_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    experiment_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    results: tuple[BenchmarkResultRef, ...] = Field(default_factory=tuple, max_length=10_000)

    @model_validator(mode="after")
    def unique_result_cases(self) -> BenchmarkResultSet:
        case_ids = [item.case_id for item in self.results]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("result set contains duplicate case references")
        return self


class BenchmarkComparisonManifest(BenchmarkModel):
    """Strict manifest describing an apples-to-apples multi-experiment comparison."""

    schema_version: Literal[1] = 1
    manifest_type: Literal["comparison"] = "comparison"
    comparison_id: str = Field(
        min_length=1,
        max_length=MAX_ID,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
    )
    benchmark_dataset_path: Path
    result_sets: tuple[BenchmarkResultSet | Path, ...] = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def inline_result_set_ids_unique(self) -> BenchmarkComparisonManifest:
        ids = [
            item.result_set_id for item in self.result_sets if isinstance(item, BenchmarkResultSet)
        ]
        if len(set(ids)) != len(ids):
            raise ValueError("comparison contains duplicate result set IDs")
        return self


__all__ = [
    "ANNOTATION_SCHEMA_VERSION",
    "BENCHMARK_SCHEMA_VERSION",
    "EVALUATION_POLICY_VERSION",
    "EVALUATOR_VERSION",
    "AggregateGroup",
    "AnnotatedHighlight",
    "AnnotatedMatch",
    "BenchmarkAggregate",
    "BenchmarkAnnotations",
    "BenchmarkCase",
    "BenchmarkComparisonManifest",
    "BenchmarkDataset",
    "BenchmarkEvaluation",
    "BenchmarkResultRef",
    "BenchmarkResultSet",
    "BenchmarkSplit",
    "BestOfMetrics",
    "BoringInterval",
    "BoringMetrics",
    "BoundaryMeasurement",
    "BoundaryMetrics",
    "CategoryConfusion",
    "CategoryMetrics",
    "CostMetrics",
    "DuplicateCandidate",
    "DuplicateMetrics",
    "EvaluationCounts",
    "EvaluationPolicy",
    "ExperimentIdentity",
    "ExtraCandidate",
    "Importance",
    "MatchMetrics",
    "MatchedPair",
    "MissedAnnotation",
    "Modality",
    "PrimaryMetrics",
    "ReviewMetrics",
    "RuntimeMetrics",
    "SliceMetric",
    "StorageMetrics",
    "compute_evaluation_policy_fingerprint",
    "evaluation_policy_fingerprint",
]
