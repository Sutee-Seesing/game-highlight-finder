"""Creator-owned evaluation corpus for C1 product usefulness, separate from event GT."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from game_highlight_finder.domain.models import EditorialRole, PersistedModel, SessionMap
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.sessions import SessionPaths

CREATOR_EVALUATION_VERSION = "c1-creator-evaluation-v1"


class CreatorDecision(StrEnum):
    KEEP = "KEEP"
    MAYBE = "MAYBE"
    REJECT_BORING = "REJECT_BORING"
    REJECT_WRONG_EVENT = "REJECT_WRONG_EVENT"
    REJECT_BAD_BOUNDARY = "REJECT_BAD_BOUNDARY"
    REJECT_DUPLICATE = "REJECT_DUPLICATE"


class CreatorCandidateReview(PersistedModel):
    """One human product-usefulness decision; never event-detection ground truth."""

    candidate_id: str = Field(pattern=r"^cand_[0-9a-f]{16}$")
    decision: CreatorDecision
    presented_editorial_role: EditorialRole | None = None
    owner_editorial_role: EditorialRole | None = None
    desired_start_ms: int | None = Field(default=None, ge=0)
    desired_end_ms: int | None = Field(default=None, gt=0)
    review_time_ms: int | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def desired_boundary_is_ordered(self) -> CreatorCandidateReview:
        if (self.desired_start_ms is None) != (self.desired_end_ms is None):
            raise ValueError("desired boundary must provide both start and end")
        if (
            self.desired_start_ms is not None
            and self.desired_end_ms is not None
            and self.desired_end_ms <= self.desired_start_ms
        ):
            raise ValueError("desired boundary must be non-empty")
        return self


class ObviousCreatorMiss(PersistedModel):
    """A creator-worthy source interval the candidate pack failed to surface."""

    label: Literal["MISS_OBVIOUS"] = "MISS_OBVIOUS"
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    description: str = Field(min_length=1, max_length=1_000)
    expected_editorial_role: EditorialRole | None = None
    category_hint: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def interval_is_ordered(self) -> ObviousCreatorMiss:
        if self.end_ms <= self.start_ms:
            raise ValueError("obvious-miss interval must be non-empty")
        return self


class CreatorEvaluationCorpus(PersistedModel):
    """Durable owner review artifact kept explicitly separate from benchmark GT."""

    schema_version: Literal[1] = 1
    created_at: datetime
    version: str = CREATOR_EVALUATION_VERSION
    session_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(pattern=r"^src_[0-9a-f]{16}$")
    source_duration_ms: int = Field(gt=0)
    candidate_reviews: list[CreatorCandidateReview] = Field(default_factory=list, max_length=10_000)
    obvious_misses: list[ObviousCreatorMiss] = Field(default_factory=list, max_length=10_000)
    notes: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def identities_and_intervals_are_unique(self) -> CreatorEvaluationCorpus:
        candidate_ids = [review.candidate_id for review in self.candidate_reviews]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("creator candidate reviews must have unique candidate IDs")
        for miss in self.obvious_misses:
            if miss.end_ms > self.source_duration_ms:
                raise ValueError("obvious creator miss exceeds source duration")
        return self


class CreatorReviewTemplateCandidate(PersistedModel):
    """One editable owner-review row generated from the exact presented review map."""

    candidate_id: str = Field(pattern=r"^cand_[0-9a-f]{16}$")
    category: str = Field(min_length=1, max_length=64)
    presented_editorial_role: EditorialRole | None = None
    clip_start_ms: int | None = Field(default=None, ge=0)
    clip_end_ms: int | None = Field(default=None, gt=0)
    moment_summary: str | None = Field(default=None, max_length=500)
    creator_reason: str | None = Field(default=None, max_length=500)
    decision: CreatorDecision | None = None
    owner_editorial_role: EditorialRole | None = None
    desired_start_ms: int | None = Field(default=None, ge=0)
    desired_end_ms: int | None = Field(default=None, gt=0)
    review_time_ms: int | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def desired_boundary_is_ordered(self) -> CreatorReviewTemplateCandidate:
        if (self.desired_start_ms is None) != (self.desired_end_ms is None):
            raise ValueError("desired boundary must provide both start and end")
        if (
            self.desired_start_ms is not None
            and self.desired_end_ms is not None
            and self.desired_end_ms <= self.desired_start_ms
        ):
            raise ValueError("desired boundary must be non-empty")
        return self


class CreatorReviewTemplate(PersistedModel):
    """Editable creator-review worksheet; null decisions mean review is unfinished."""

    schema_version: Literal[1] = 1
    created_at: datetime
    version: str = CREATOR_EVALUATION_VERSION
    session_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(pattern=r"^src_[0-9a-f]{16}$")
    source_duration_ms: int = Field(gt=0)
    candidates: list[CreatorReviewTemplateCandidate] = Field(
        default_factory=list, max_length=10_000
    )
    obvious_misses: list[ObviousCreatorMiss] = Field(default_factory=list, max_length=10_000)
    notes: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def candidate_ids_are_unique(self) -> CreatorReviewTemplate:
        candidate_ids = [candidate.candidate_id for candidate in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("creator review template candidate IDs must be unique")
        return self


class CreatorEvaluationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reviewed_count: int = Field(ge=0)
    keep_count: int = Field(ge=0)
    maybe_count: int = Field(ge=0)
    reject_count: int = Field(ge=0)
    keep_rate: float = Field(ge=0, le=1)
    keep_or_maybe_rate: float = Field(ge=0, le=1)
    standalone_presented_count: int = Field(ge=0)
    standalone_keep_count: int = Field(ge=0)
    standalone_keep_rate: float = Field(ge=0, le=1)
    montage_presented_count: int = Field(ge=0)
    montage_useful_count: int = Field(ge=0)
    montage_useful_rate: float = Field(ge=0, le=1)
    bad_boundary_count: int = Field(ge=0)
    wrong_event_count: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)
    obvious_miss_count: int = Field(ge=0)
    recorded_owner_review_time_ms: int = Field(ge=0)


def create_creator_review_template(
    review_map: SessionMap,
    *,
    created_at: datetime | None = None,
) -> CreatorReviewTemplate:
    """Create an editable worksheet from exactly the candidates shown to the creator."""

    return CreatorReviewTemplate(
        created_at=created_at or datetime.now(UTC),
        session_id=review_map.session_id,
        source_id=review_map.source_id,
        source_duration_ms=review_map.duration_ms,
        candidates=[
            CreatorReviewTemplateCandidate(
                candidate_id=candidate.candidate_id,
                category=candidate.category,
                presented_editorial_role=candidate.editorial_role,
                clip_start_ms=candidate.clip_start_ms,
                clip_end_ms=candidate.clip_end_ms,
                moment_summary=candidate.moment_summary,
                creator_reason=candidate.creator_reason,
            )
            for candidate in review_map.candidates
        ],
    )


def persist_creator_review_template(
    paths: SessionPaths,
    template: CreatorReviewTemplate,
) -> None:
    if paths.root.name != template.session_id:
        raise ValueError("creator review template session does not match target session path")
    paths.hybrid_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(paths.creator_review_template_path, template.model_dump(mode="json"))


def load_creator_review_template(path: SessionPaths | Path) -> CreatorReviewTemplate:
    target = path.creator_review_template_path if isinstance(path, SessionPaths) else path
    return CreatorReviewTemplate.model_validate(read_json(target))


def creator_evaluation_from_template(
    template: CreatorReviewTemplate,
) -> CreatorEvaluationCorpus:
    """Convert a fully reviewed worksheet into the durable product-evaluation corpus."""

    unfinished = [
        candidate.candidate_id
        for candidate in template.candidates
        if candidate.decision is None
    ]
    if unfinished:
        raise ValueError(
            "creator review template still has unfinished decisions: " + ", ".join(unfinished[:10])
        )
    return CreatorEvaluationCorpus(
        created_at=datetime.now(UTC),
        session_id=template.session_id,
        source_id=template.source_id,
        source_duration_ms=template.source_duration_ms,
        candidate_reviews=[
            CreatorCandidateReview(
                candidate_id=candidate.candidate_id,
                decision=candidate.decision,
                presented_editorial_role=candidate.presented_editorial_role,
                owner_editorial_role=candidate.owner_editorial_role,
                desired_start_ms=candidate.desired_start_ms,
                desired_end_ms=candidate.desired_end_ms,
                review_time_ms=candidate.review_time_ms,
                note=candidate.note,
            )
            for candidate in template.candidates
            if candidate.decision is not None
        ],
        obvious_misses=list(template.obvious_misses),
        notes=list(template.notes),
    )


def validate_creator_evaluation(
    corpus: CreatorEvaluationCorpus,
    review_map: SessionMap,
) -> None:
    """Validate owner labels against the exact presented hybrid review map."""

    if corpus.session_id != review_map.session_id:
        raise ValueError("creator evaluation session does not match review map")
    if corpus.source_id != review_map.source_id:
        raise ValueError("creator evaluation source does not match review map")
    if corpus.source_duration_ms != review_map.duration_ms:
        raise ValueError("creator evaluation duration does not match review map")
    candidates = {candidate.candidate_id: candidate for candidate in review_map.candidates}
    for review in corpus.candidate_reviews:
        candidate = candidates.get(review.candidate_id)
        if candidate is None:
            raise ValueError(
                f"creator review references unpresented candidate {review.candidate_id}"
            )
        if (
            review.presented_editorial_role is not None
            and review.presented_editorial_role is not candidate.editorial_role
        ):
            raise ValueError("creator review presented role does not match review map")
        if review.desired_end_ms is not None and review.desired_end_ms > review_map.duration_ms:
            raise ValueError("creator review desired boundary exceeds source duration")


def summarize_creator_evaluation(corpus: CreatorEvaluationCorpus) -> CreatorEvaluationSummary:
    reviews = corpus.candidate_reviews
    reviewed = len(reviews)
    keep = sum(review.decision is CreatorDecision.KEEP for review in reviews)
    maybe = sum(review.decision is CreatorDecision.MAYBE for review in reviews)
    rejects = reviewed - keep - maybe
    standalone = [
        review
        for review in reviews
        if review.presented_editorial_role is EditorialRole.STANDALONE_STORY
    ]
    montage = [
        review
        for review in reviews
        if review.presented_editorial_role is EditorialRole.MONTAGE_BEAT
    ]
    standalone_keep = sum(review.decision is CreatorDecision.KEEP for review in standalone)
    montage_useful = sum(
        review.decision in {CreatorDecision.KEEP, CreatorDecision.MAYBE} for review in montage
    )
    return CreatorEvaluationSummary(
        reviewed_count=reviewed,
        keep_count=keep,
        maybe_count=maybe,
        reject_count=rejects,
        keep_rate=_rate(keep, reviewed),
        keep_or_maybe_rate=_rate(keep + maybe, reviewed),
        standalone_presented_count=len(standalone),
        standalone_keep_count=standalone_keep,
        standalone_keep_rate=_rate(standalone_keep, len(standalone)),
        montage_presented_count=len(montage),
        montage_useful_count=montage_useful,
        montage_useful_rate=_rate(montage_useful, len(montage)),
        bad_boundary_count=sum(
            review.decision is CreatorDecision.REJECT_BAD_BOUNDARY for review in reviews
        ),
        wrong_event_count=sum(
            review.decision is CreatorDecision.REJECT_WRONG_EVENT for review in reviews
        ),
        duplicate_count=sum(
            review.decision is CreatorDecision.REJECT_DUPLICATE for review in reviews
        ),
        obvious_miss_count=len(corpus.obvious_misses),
        recorded_owner_review_time_ms=sum(review.review_time_ms or 0 for review in reviews),
    )


def persist_creator_evaluation(
    paths: SessionPaths,
    corpus: CreatorEvaluationCorpus,
    review_map: SessionMap,
) -> None:
    validate_creator_evaluation(corpus, review_map)
    if paths.root.name != corpus.session_id:
        raise ValueError("creator evaluation session does not match target session path")
    paths.hybrid_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(paths.creator_evaluation_path, corpus.model_dump(mode="json"))
    atomic_write_json(
        paths.creator_evaluation_summary_path,
        summarize_creator_evaluation(corpus).model_dump(mode="json"),
    )


def load_creator_evaluation(paths: SessionPaths) -> CreatorEvaluationCorpus:
    return CreatorEvaluationCorpus.model_validate(read_json(paths.creator_evaluation_path))


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


__all__ = [
    "CREATOR_EVALUATION_VERSION",
    "CreatorCandidateReview",
    "CreatorDecision",
    "CreatorEvaluationCorpus",
    "CreatorEvaluationSummary",
    "CreatorReviewTemplate",
    "CreatorReviewTemplateCandidate",
    "ObviousCreatorMiss",
    "create_creator_review_template",
    "creator_evaluation_from_template",
    "load_creator_evaluation",
    "load_creator_review_template",
    "persist_creator_evaluation",
    "persist_creator_review_template",
    "summarize_creator_evaluation",
    "validate_creator_evaluation",
]
