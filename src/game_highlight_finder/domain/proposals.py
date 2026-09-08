"""Provider-neutral factual proposal contracts for hybrid creator triage."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from game_highlight_finder.domain.models import PersistedModel


class ProposalSignalType(StrEnum):
    """Normalized evidence source type; proposals are facts/anchors, not stories."""

    AUDIO_ACTIVITY = "AUDIO_ACTIVITY"
    SCENE_ACTIVITY = "SCENE_ACTIVITY"
    ASR_UTTERANCE = "ASR_UTTERANCE"
    VISUAL_STATE_CHANGE = "VISUAL_STATE_CHANGE"
    OCR_STATE_CHANGE = "OCR_STATE_CHANGE"
    GAME_EVENT = "GAME_EVENT"
    MANUAL_MARKER = "MANUAL_MARKER"


class ProposalRoute(StrEnum):
    """Cost/coverage routing class; never a creator-worthiness label."""

    MUST_INSPECT = "MUST_INSPECT"
    SUPPORTED = "SUPPORTED"
    SAMPLED_WEAK = "SAMPLED_WEAK"
    DEFERRED_WEAK = "DEFERRED_WEAK"


class Proposal(PersistedModel):
    """One high-recall factual anchor that may deserve semantic inspection."""

    proposal_id: str = Field(pattern=r"^prop_[0-9a-f]{16}$")
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    signal_type: ProposalSignalType
    event_hypothesis: str | None = Field(default=None, min_length=1, max_length=128)
    confidence: float = Field(ge=0, le=1)
    sources: list[str] = Field(min_length=1, max_length=16)
    metadata: dict[str, str] = Field(default_factory=dict, max_length=32)

    @field_validator("start_ms", "end_ms", mode="before")
    @classmethod
    def strict_integer_time(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("proposal timestamps must be integer milliseconds")
        return value

    @model_validator(mode="after")
    def interval_and_sources_are_valid(self) -> Proposal:
        if self.end_ms <= self.start_ms:
            raise ValueError("proposal interval must be non-empty")
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("proposal sources must be unique")
        return self


class ProposalArtifact(PersistedModel):
    """Versioned proposal timeline before any creator/story judgment."""

    schema_version: int = 1
    created_at: datetime
    producer_version: str
    session_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(pattern=r"^src_[0-9a-f]{16}$")
    source_duration_ms: int = Field(gt=0)
    proposals: list[Proposal] = Field(default_factory=list, max_length=10_000)
    warnings: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def proposals_fit_source(self) -> ProposalArtifact:
        seen: set[str] = set()
        for proposal in self.proposals:
            if proposal.end_ms > self.source_duration_ms:
                raise ValueError("proposal exceeds source duration")
            if proposal.proposal_id in seen:
                raise ValueError("proposal IDs must be unique")
            seen.add(proposal.proposal_id)
        return self


class TranscriptUtterance(PersistedModel):
    """One source-relative utterance from a local/provider-neutral transcript fixture."""

    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=1_000)
    speaker: str | None = Field(default=None, min_length=1, max_length=128)
    language: str | None = Field(default=None, min_length=1, max_length=32)
    confidence: float = Field(default=1.0, ge=0, le=1)

    @field_validator("start_ms", "end_ms", mode="before")
    @classmethod
    def strict_integer_time(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("transcript timestamps must be integer milliseconds")
        return value

    @model_validator(mode="after")
    def interval_is_valid(self) -> TranscriptUtterance:
        if self.end_ms <= self.start_ms:
            raise ValueError("transcript utterance interval must be non-empty")
        return self


class TranscriptFixture(PersistedModel):
    """Source-bound transcript evidence; speech is evidence, never creator truth."""

    schema_version: int = 1
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_duration_ms: int = Field(gt=0)
    utterances: list[TranscriptUtterance] = Field(default_factory=list, max_length=20_000)
    notes: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def utterances_fit_source(self) -> TranscriptFixture:
        for utterance in self.utterances:
            if utterance.end_ms > self.source_duration_ms:
                raise ValueError("transcript utterance exceeds source duration")
        return self


class ProposalSummary(PersistedModel):
    """Provider-free density/duplication telemetry for one factual proposal timeline."""

    schema_version: int = 1
    created_at: datetime
    session_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(pattern=r"^src_[0-9a-f]{16}$")
    source_duration_ms: int = Field(gt=0)
    factual_anchor_count_before_clustering: int = Field(ge=0)
    proposal_neighborhood_count: int = Field(ge=0)
    clustered_reduction_count: int = Field(ge=0)
    multi_source_neighborhood_count: int = Field(ge=0)
    explicit_hypothesis_count: int = Field(ge=0)
    proposals_per_source_hour: float = Field(ge=0)
    by_signal_type: dict[str, int] = Field(default_factory=dict, max_length=32)


class ProposalRoutingDecision(PersistedModel):
    """One deterministic evidence/cost routing decision for a preserved proposal."""

    proposal_id: str = Field(pattern=r"^prop_[0-9a-f]{16}$")
    route: ProposalRoute
    reason: str = Field(min_length=1, max_length=500)


class ProposalRoutingPlan(PersistedModel):
    """Provider-free routing plan over an immutable factual proposal artifact."""

    schema_version: int = 1
    policy_version: str = Field(min_length=1, max_length=128)
    created_at: datetime
    session_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(pattern=r"^src_[0-9a-f]{16}$")
    source_duration_ms: int = Field(gt=0)
    weak_sample_interval_ms: int = Field(gt=0)
    decisions: list[ProposalRoutingDecision] = Field(default_factory=list, max_length=10_000)
    selected_proposal_ids: list[str] = Field(default_factory=list, max_length=10_000)
    deferred_proposal_ids: list[str] = Field(default_factory=list, max_length=10_000)
    selected_per_source_hour: float = Field(ge=0)
    route_counts: dict[str, int] = Field(default_factory=dict, max_length=16)

    @model_validator(mode="after")
    def decisions_form_exact_partition(self) -> ProposalRoutingPlan:
        decision_ids = [decision.proposal_id for decision in self.decisions]
        if len(set(decision_ids)) != len(decision_ids):
            raise ValueError("proposal routing decisions must have unique proposal IDs")
        selected = set(self.selected_proposal_ids)
        deferred = set(self.deferred_proposal_ids)
        if selected & deferred:
            raise ValueError("selected and deferred proposal IDs must be disjoint")
        if selected | deferred != set(decision_ids):
            raise ValueError("routing selected/deferred IDs must partition all decisions")
        for decision in self.decisions:
            should_select = decision.route is not ProposalRoute.DEFERRED_WEAK
            if should_select != (decision.proposal_id in selected):
                raise ValueError(
                    "routing decision route disagrees with selected/deferred partition"
                )
        return self


class ManualProposalMarker(PersistedModel):
    """One owner/fixture supplied factual marker; it carries no editorial judgment."""

    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    label: str = Field(min_length=1, max_length=256)
    event_hypothesis: str | None = Field(default=None, min_length=1, max_length=128)
    confidence: float = Field(default=1.0, ge=0, le=1)

    @field_validator("start_ms", "end_ms", mode="before")
    @classmethod
    def strict_integer_time(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("manual marker timestamps must be integer milliseconds")
        return value

    @model_validator(mode="after")
    def interval_is_valid(self) -> ManualProposalMarker:
        if self.end_ms <= self.start_ms:
            raise ValueError("manual marker interval must be non-empty")
        return self


class ManualProposalMarkerSet(PersistedModel):
    """Source-bound local marker fixture used to enrich proposal recall deterministically."""

    schema_version: int = 1
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_duration_ms: int = Field(gt=0)
    markers: list[ManualProposalMarker] = Field(default_factory=list, max_length=10_000)
    notes: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def markers_fit_source(self) -> ManualProposalMarkerSet:
        for marker in self.markers:
            if marker.end_ms > self.source_duration_ms:
                raise ValueError("manual marker exceeds source duration")
        return self


__all__ = [
    "ManualProposalMarker",
    "ManualProposalMarkerSet",
    "Proposal",
    "ProposalArtifact",
    "ProposalRoute",
    "ProposalRoutingDecision",
    "ProposalRoutingPlan",
    "ProposalSignalType",
    "ProposalSummary",
    "TranscriptFixture",
    "TranscriptUtterance",
]
