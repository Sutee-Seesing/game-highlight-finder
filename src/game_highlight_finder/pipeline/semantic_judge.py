"""Provider-neutral semantic-judge contract for proposal-centered creator triage."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from game_highlight_finder.domain.models import (
    Candidate,
    CandidateClaim,
    ClaimStatus,
    EditorialRole,
)
from game_highlight_finder.domain.proposals import Proposal
from game_highlight_finder.pipeline.context_expansion import ContextExpansionPlan

SEMANTIC_JUDGE_VERSION = "c1-semantic-judge-v1"


class SemanticJudgment(BaseModel):
    """Provisional interpretation of a factual proposal; never a verifier result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = SEMANTIC_JUDGE_VERSION
    proposal_id: str = Field(pattern=r"^prop_[0-9a-f]{16}$")
    event_start_ms: int = Field(ge=0)
    event_end_ms: int = Field(gt=0)
    category: str = Field(min_length=1, max_length=48)
    editorial_role: EditorialRole
    creator_score: float = Field(ge=0, le=10)
    confidence: float = Field(ge=0, le=1)
    moment_summary: str = Field(min_length=1, max_length=500)
    creator_reason: str = Field(min_length=1, max_length=500)
    claim_hypotheses: list[str] = Field(default_factory=list, max_length=32)
    needs_more_context: bool = False
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("event_start_ms", "event_end_ms", mode="before")
    @classmethod
    def strict_integer_time(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("semantic-judgment timestamps must be integer milliseconds")
        return value

    @field_validator("claim_hypotheses")
    @classmethod
    def normalize_claim_hypotheses(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            claim = value.strip().upper()
            if not claim or not claim.replace("_", "").isalnum() or not claim[0].isalpha():
                raise ValueError("claim hypotheses must use uppercase identifier syntax")
            if claim not in normalized:
                normalized.append(claim)
        return normalized

    @model_validator(mode="after")
    def interval_is_ordered(self) -> SemanticJudgment:
        if self.event_end_ms <= self.event_start_ms:
            raise ValueError("semantic-judgment event interval must be non-empty")
        return self


class FakeSemanticJudge:
    """Deterministic provider-free semantic judge for contract and orchestration tests."""

    def __init__(
        self,
        fixtures: Mapping[str, SemanticJudgment | Sequence[SemanticJudgment]],
    ) -> None:
        self._fixtures = dict(fixtures)
        self._proposal_calls: dict[str, int] = {}
        self.calls = 0

    def judge(
        self,
        proposal: Proposal,
        context: ContextExpansionPlan | None = None,
    ) -> SemanticJudgment:
        self.calls += 1
        try:
            fixture = self._fixtures[proposal.proposal_id]
        except KeyError as exc:
            raise ValueError(f"no semantic-judge fixture for {proposal.proposal_id}") from exc
        if isinstance(fixture, SemanticJudgment):
            judgment = fixture
        else:
            sequence = list(fixture)
            if not sequence:
                raise ValueError("semantic-judge fixture sequence cannot be empty")
            index = self._proposal_calls.get(proposal.proposal_id, 0)
            judgment = sequence[min(index, len(sequence) - 1)]
            self._proposal_calls[proposal.proposal_id] = index + 1
        if judgment.proposal_id != proposal.proposal_id:
            raise ValueError("semantic-judge fixture belongs to a different proposal")
        if context is not None:
            if context.proposal_id != proposal.proposal_id:
                raise ValueError("semantic context belongs to a different proposal")
            if (
                judgment.event_start_ms < context.context_start_ms
                or judgment.event_end_ms > context.context_end_ms
            ):
                raise ValueError("semantic judgment event exceeds supplied context")
        return judgment


def candidate_from_semantic_judgment(
    *,
    candidate_id: str,
    proposal: Proposal,
    judgment: SemanticJudgment,
    source_window_ids: list[str] | None = None,
) -> Candidate:
    """Create a provisional Candidate with every semantic claim still unverified."""

    if judgment.proposal_id != proposal.proposal_id:
        raise ValueError("semantic judgment belongs to a different proposal")
    claims = [
        CandidateClaim(claim_type=claim_type, status=ClaimStatus.UNVERIFIED)
        for claim_type in judgment.claim_hypotheses
    ]
    return Candidate(
        candidate_id=candidate_id,
        category=judgment.category,
        event_start_ms=judgment.event_start_ms,
        event_end_ms=judgment.event_end_ms,
        score=judgment.creator_score,
        confidence=judgment.confidence,
        reason=judgment.reason,
        moment_summary=judgment.moment_summary,
        creator_reason=judgment.creator_reason,
        editorial_role=judgment.editorial_role,
        claims=claims,
        source_window_ids=source_window_ids or [],
        metadata={
            "proposal_id": proposal.proposal_id,
            "semantic_judge_version": judgment.version,
            "needs_more_context": str(judgment.needs_more_context).lower(),
        },
    )


__all__ = [
    "SEMANTIC_JUDGE_VERSION",
    "FakeSemanticJudge",
    "SemanticJudgment",
    "candidate_from_semantic_judgment",
]
