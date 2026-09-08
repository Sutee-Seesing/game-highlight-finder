"""Verified story assembly for the C1 hybrid creator-triage pipeline."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from game_highlight_finder.domain.models import (
    Candidate,
    EditorialRole,
    ResolutionState,
    StoryState,
)

STORY_ASSEMBLY_VERSION = "c1-story-assembler-v1"


class StoryAssembly(BaseModel):
    """Semantic boundary roles for one already-verified standalone story."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = STORY_ASSEMBLY_VERSION
    candidate_id: str = Field(pattern=r"^cand_[0-9a-f]{16}$")
    setup_start_ms: int = Field(ge=0)
    event_start_ms: int = Field(ge=0)
    event_end_ms: int = Field(gt=0)
    payoff_end_ms: int = Field(gt=0)
    reaction_end_ms: int | None = Field(default=None, gt=0)
    related_candidate_ids: list[str] = Field(default_factory=list, max_length=32)
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def semantic_boundaries_are_ordered(self) -> StoryAssembly:
        if self.event_end_ms <= self.event_start_ms:
            raise ValueError("story event interval must be non-empty")
        if self.setup_start_ms > self.event_start_ms:
            raise ValueError("story setup must not start after the event")
        if self.payoff_end_ms < self.event_end_ms:
            raise ValueError("story payoff must include the event")
        if self.reaction_end_ms is not None and self.reaction_end_ms < self.payoff_end_ms:
            raise ValueError("story reaction must not end before the payoff")
        if len(set(self.related_candidate_ids)) != len(self.related_candidate_ids):
            raise ValueError("related candidate IDs must be unique")
        if self.candidate_id in self.related_candidate_ids:
            raise ValueError("story candidate cannot relate to itself")
        return self



def apply_story_assembly(
    candidate: Candidate,
    assembly: StoryAssembly,
    *,
    source_duration_ms: int,
) -> Candidate:
    """Attach semantic story boundaries only after factual verification passed.

    This gate intentionally refuses to turn a provisional standalone hypothesis into a
    story merely because a semantic judge preferred that editorial role.
    """

    if candidate.candidate_id != assembly.candidate_id:
        raise ValueError("story assembly belongs to a different candidate")
    if source_duration_ms <= 0:
        raise ValueError("source duration must be positive")
    terminal_end_ms = assembly.reaction_end_ms or assembly.payoff_end_ms
    if terminal_end_ms > source_duration_ms:
        raise ValueError("story assembly exceeds source duration")
    if candidate.editorial_role is not EditorialRole.STANDALONE_STORY:
        raise ValueError("story assembly is only valid for standalone-story candidates")
    if candidate.story_state is not StoryState.COMPLETE:
        raise ValueError("story assembly requires COMPLETE story state")
    if candidate.resolution_state not in {
        ResolutionState.VERIFIED,
        ResolutionState.NOT_APPLICABLE,
    }:
        raise ValueError("story assembly requires verified or non-terminal resolution state")
    if (
        assembly.event_end_ms <= candidate.event_start_ms
        or assembly.event_start_ms >= candidate.event_end_ms
    ):
        raise ValueError("assembled story event must overlap the provisional event")

    metadata = {
        **candidate.metadata,
        "story_assembly_version": assembly.version,
        "story_assembly_reason": assembly.reason,
    }
    return candidate.model_copy(
        update={
            "kind": "STORY",
            "setup_start_ms": assembly.setup_start_ms,
            "event_start_ms": assembly.event_start_ms,
            "event_end_ms": assembly.event_end_ms,
            "payoff_end_ms": assembly.payoff_end_ms,
            "reaction_end_ms": assembly.reaction_end_ms,
            "related_candidate_ids": list(assembly.related_candidate_ids),
            "clip_start_ms": None,
            "clip_end_ms": None,
            "normalization_actions": [
                *candidate.normalization_actions,
                assembly.version,
            ],
            "metadata": metadata,
        }
    )


__all__ = [
    "STORY_ASSEMBLY_VERSION",
    "StoryAssembly",
    "apply_story_assembly",
]
