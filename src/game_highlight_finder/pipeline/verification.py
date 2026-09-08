"""Provider-neutral factual/story verification contracts for C1 hybrid triage."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from game_highlight_finder.domain.models import (
    Candidate,
    CandidateClaim,
    ClaimStatus,
    ResolutionState,
    StoryState,
)
from game_highlight_finder.pipeline.context_expansion import ContextExpansionPlan

VERIFICATION_VERSION = "c1-resolution-verifier-v1"


class CandidateVerification(BaseModel):
    """Independent verifier result applied after provisional semantic judgment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = VERIFICATION_VERSION
    candidate_id: str = Field(pattern=r"^cand_[0-9a-f]{16}$")
    story_state: StoryState
    resolution_state: ResolutionState
    claims: list[CandidateClaim] = Field(default_factory=list, max_length=32)
    needs_more_context: bool = False
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def resolved_state_has_matching_claim(self) -> CandidateVerification:
        if self.resolution_state is ResolutionState.VERIFIED and not any(
            claim.status is ClaimStatus.VERIFIED for claim in self.claims
        ):
            raise ValueError("verified resolution requires a verified evidence-backed claim")
        if self.resolution_state is ResolutionState.CONTRADICTED and not any(
            claim.status is ClaimStatus.CONTRADICTED for claim in self.claims
        ):
            raise ValueError(
                "contradicted resolution requires a contradicted evidence-backed claim"
            )
        if self.needs_more_context and self.resolution_state in {
            ResolutionState.VERIFIED,
            ResolutionState.CONTRADICTED,
            ResolutionState.NOT_APPLICABLE,
        }:
            raise ValueError("resolved verification must not request more context")
        return self


class FakeResolutionVerifier:
    """Deterministic provider-free verifier for orchestration tests."""

    def __init__(
        self,
        fixtures: Mapping[str, CandidateVerification | Sequence[CandidateVerification]],
    ) -> None:
        self._fixtures = dict(fixtures)
        self._candidate_calls: dict[str, int] = {}
        self.calls = 0

    def verify(
        self,
        candidate: Candidate,
        context: ContextExpansionPlan | None = None,
    ) -> CandidateVerification:
        self.calls += 1
        try:
            fixture = self._fixtures[candidate.candidate_id]
        except KeyError as exc:
            raise ValueError(f"no verifier fixture for {candidate.candidate_id}") from exc
        if isinstance(fixture, CandidateVerification):
            verification = fixture
        else:
            sequence = list(fixture)
            if not sequence:
                raise ValueError("verifier fixture sequence cannot be empty")
            index = self._candidate_calls.get(candidate.candidate_id, 0)
            verification = sequence[min(index, len(sequence) - 1)]
            self._candidate_calls[candidate.candidate_id] = index + 1
        if verification.candidate_id != candidate.candidate_id:
            raise ValueError("verifier fixture belongs to a different candidate")
        if context is not None:
            proposal_id = candidate.metadata.get("proposal_id")
            if proposal_id and context.proposal_id != proposal_id:
                raise ValueError("verification context belongs to a different proposal")
        return verification


def apply_candidate_verification(
    candidate: Candidate,
    verification: CandidateVerification,
) -> Candidate:
    """Attach verifier truth without letting the semantic judge self-certify it."""

    if candidate.candidate_id != verification.candidate_id:
        raise ValueError("verification belongs to a different candidate")
    payload = candidate.model_dump(mode="python")
    payload.update(
        {
            "story_state": verification.story_state,
            "resolution_state": verification.resolution_state,
            "claims": verification.claims,
            "normalization_actions": [
                *candidate.normalization_actions,
                verification.version,
            ],
            "metadata": {
                **candidate.metadata,
                "verification_version": verification.version,
                "verification_needs_more_context": str(verification.needs_more_context).lower(),
            },
        }
    )
    return Candidate.model_validate(payload)


__all__ = [
    "VERIFICATION_VERSION",
    "CandidateVerification",
    "FakeResolutionVerifier",
    "apply_candidate_verification",
]
