"""Provider-free cross-case suppression evaluation from explicit visual review decisions."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from game_highlight_finder.benchmark.review_queue_server import (
    ReviewAdjudicationDocument,
    ReviewQueueDocument,
)
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.storage.atomic import atomic_write_json, read_json

CROSS_CASE_SUPPRESSION_VERSION = "cross-case-suppression-v1"
CrossCaseVerdict = Literal[
    "NORMALIZED_AUDIO_PEAK_SEPARATES_REVIEWED_NEGATIVES",
    "NORMALIZED_AUDIO_PEAK_NO_CLEAN_SEPARATION",
]


class AudioScaleInterval(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    review_id: str = Field(min_length=1, max_length=128)
    audio_peak_over_loudness_db: float = Field(ge=-200, le=220)


class AudioScaleCase(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    case: str = Field(min_length=1, max_length=128)
    intervals: tuple[AudioScaleInterval, ...]


class AudioScaleDocument(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_version: Literal[1] = 1
    semantic_labels_inferred: Literal[False]
    provider_calls: Literal[0]
    cases: tuple[AudioScaleCase, ...]


class CrossCaseSuppressionRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case: str = Field(min_length=1, max_length=128)
    review_id: str = Field(min_length=1, max_length=128)
    decision: Literal["POSITIVE", "BORING"]
    audio_peak_over_loudness_db: float = Field(ge=-200, le=220)


class CrossCaseSuppressionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    version: str = CROSS_CASE_SUPPRESSION_VERSION
    set_id: str = Field(min_length=1, max_length=128)
    queue_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_cases: tuple[str, ...]
    reviewed_count: int = Field(ge=0)
    reviewer_kind: Literal["HUMAN", "ASSISTANT_VISUAL"]
    positive_count: int = Field(ge=1)
    boring_count: int = Field(ge=1)
    protected_positive_min_audio_peak_over_loudness_db: float = Field(ge=-200, le=220)
    rejected_boring_review_ids: tuple[str, ...]
    surviving_boring_review_ids: tuple[str, ...]
    rejected_boring_count: int = Field(ge=0)
    boring_count_total: int = Field(ge=1)
    verdict: CrossCaseVerdict
    rows: tuple[CrossCaseSuppressionRow, ...]
    provider_calls: Literal[0] = 0
    production_threshold_locked: Literal[False] = False
    warning: str = (
        "Calibration-only diagnostic from explicit visual review labels. Do not promote this "
        "threshold to production or use revealed validation/holdout data for tuning."
    )


def _queue_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assess_cross_case_suppression(
    queue: ReviewQueueDocument,
    adjudication: ReviewAdjudicationDocument,
    audio_scale: AudioScaleDocument,
    *,
    queue_sha256: str,
) -> CrossCaseSuppressionResult:
    """Evaluate one normalized audio feature after complete explicit visual adjudication."""

    if adjudication.set_id != queue.set_id or adjudication.queue_sha256 != queue_sha256:
        raise ValidationError("Cross-case adjudication does not match the current review queue")
    if not queue.not_ground_truth or not queue.excluded_from_m8_acceptance:
        raise ValidationError(
            "Cross-case review queue must remain calibration-only development data"
        )
    if not adjudication.selected_cases:
        raise ValidationError("Cross-case adjudication must declare selected cases")

    queue_case_names = tuple(case.case for case in queue.cases)
    if len(set(queue_case_names)) != len(queue_case_names):
        raise ValidationError("Cross-case review queue contains duplicate case names")
    selected = set(adjudication.selected_cases)
    if len(selected) != len(adjudication.selected_cases):
        raise ValidationError("Cross-case adjudication contains duplicate selected cases")
    unknown_cases = sorted(selected - set(queue_case_names))
    if unknown_cases:
        raise ValidationError(
            "Cross-case adjudication selects cases absent from the review queue",
            hint=", ".join(unknown_cases),
        )
    queue_rows: dict[str, str] = {}
    for case in queue.cases:
        if case.case not in selected:
            continue
        for interval in case.intervals:
            if interval.review_id in queue_rows:
                raise ValidationError("Cross-case review queue contains duplicate review IDs")
            queue_rows[interval.review_id] = case.case
    if not queue_rows:
        raise ValidationError("Cross-case selected cases contain no review intervals")

    decisions = {item.review_id: item for item in adjudication.decisions}
    if set(decisions) != set(queue_rows):
        missing = sorted(set(queue_rows) - set(decisions))
        extra = sorted(set(decisions) - set(queue_rows))
        hint = f"missing={missing}; extra={extra}"
        raise ValidationError(
            "Cross-case visual adjudication is incomplete or out of scope", hint=hint
        )
    uncertain = sorted(
        item.review_id for item in adjudication.decisions if item.decision == "UNCERTAIN"
    )
    if uncertain:
        raise ValidationError(
            "Cross-case suppression requires resolved POSITIVE/BORING decisions",
            hint=", ".join(uncertain),
        )

    feature_rows: dict[str, tuple[str, float]] = {}
    for audio_case in audio_scale.cases:
        if audio_case.case not in selected:
            continue
        for audio_interval in audio_case.intervals:
            if audio_interval.review_id in feature_rows:
                raise ValidationError(
                    "Cross-case audio-scale artifact contains duplicate review IDs"
                )
            feature_rows[audio_interval.review_id] = (
                audio_case.case,
                audio_interval.audio_peak_over_loudness_db,
            )
    if set(feature_rows) != set(queue_rows):
        raise ValidationError(
            "Cross-case audio-scale artifact does not cover selected review intervals"
        )

    rows: list[CrossCaseSuppressionRow] = []
    for review_id in sorted(queue_rows):
        decision = decisions[review_id].decision
        if decision == "UNCERTAIN":
            raise ValidationError(
                "Cross-case suppression requires resolved POSITIVE/BORING decisions",
                hint=review_id,
            )
        feature_case, feature = feature_rows[review_id]
        if feature_case != queue_rows[review_id]:
            raise ValidationError("Cross-case audio-scale case identity mismatch", hint=review_id)
        rows.append(
            CrossCaseSuppressionRow(
                case=feature_case,
                review_id=review_id,
                decision=decision,
                audio_peak_over_loudness_db=feature,
            )
        )

    positives = [row for row in rows if row.decision == "POSITIVE"]
    boring = [row for row in rows if row.decision == "BORING"]
    if not positives or not boring:
        raise ValidationError(
            "Cross-case suppression requires at least one POSITIVE and one BORING label"
        )

    threshold = min(row.audio_peak_over_loudness_db for row in positives)
    rejected = tuple(row.review_id for row in boring if row.audio_peak_over_loudness_db < threshold)
    surviving = tuple(
        row.review_id for row in boring if row.audio_peak_over_loudness_db >= threshold
    )
    verdict: CrossCaseVerdict = (
        "NORMALIZED_AUDIO_PEAK_SEPARATES_REVIEWED_NEGATIVES"
        if len(rejected) == len(boring)
        else "NORMALIZED_AUDIO_PEAK_NO_CLEAN_SEPARATION"
    )
    return CrossCaseSuppressionResult(
        set_id=queue.set_id,
        queue_sha256=queue_sha256,
        selected_cases=adjudication.selected_cases,
        reviewed_count=len(rows),
        reviewer_kind=adjudication.reviewer_kind,
        positive_count=len(positives),
        boring_count=len(boring),
        protected_positive_min_audio_peak_over_loudness_db=threshold,
        rejected_boring_review_ids=rejected,
        surviving_boring_review_ids=surviving,
        rejected_boring_count=len(rejected),
        boring_count_total=len(boring),
        verdict=verdict,
        rows=tuple(rows),
    )


def run_cross_case_suppression(
    queue_path: Path,
    adjudication_path: Path,
    audio_scale_path: Path,
    *,
    output_path: Path | None = None,
) -> tuple[CrossCaseSuppressionResult, Path]:
    """Load private cross-case artifacts and persist one provider-free diagnostic."""

    resolved_queue = queue_path.expanduser().resolve()
    resolved_adjudication = adjudication_path.expanduser().resolve()
    resolved_audio_scale = audio_scale_path.expanduser().resolve()
    for path, label in (
        (resolved_queue, "review queue"),
        (resolved_adjudication, "adjudication sidecar"),
        (resolved_audio_scale, "audio-scale artifact"),
    ):
        if not path.is_file():
            raise ValidationError(f"Cross-case {label} does not exist", hint=str(path))
    try:
        queue = ReviewQueueDocument.model_validate(read_json(resolved_queue))
        adjudication = ReviewAdjudicationDocument.model_validate(read_json(resolved_adjudication))
        audio_scale = AudioScaleDocument.model_validate(read_json(resolved_audio_scale))
    except (PydanticValidationError, OSError, TypeError, ValueError) as exc:
        raise ValidationError(
            "Cross-case suppression input artifact is invalid", hint=str(exc)
        ) from exc

    result = assess_cross_case_suppression(
        queue,
        adjudication,
        audio_scale,
        queue_sha256=_queue_sha256(resolved_queue),
    )
    target = (
        output_path.expanduser().resolve()
        if output_path is not None
        else resolved_queue.with_name("cross_case_suppression.json")
    )
    atomic_write_json(target, result.model_dump(mode="json"))
    return result, target


__all__ = [
    "CROSS_CASE_SUPPRESSION_VERSION",
    "AudioScaleDocument",
    "CrossCaseSuppressionResult",
    "assess_cross_case_suppression",
    "run_cross_case_suppression",
]
