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
    "ProposalSignalType",
]
