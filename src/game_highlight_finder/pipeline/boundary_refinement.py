from __future__ import annotations

from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from game_highlight_finder.domain.models import Candidate

BOUNDARY_REFINEMENT_VERSION = "boundary-refiner-v1"
DEFAULT_PRE_CONTEXT_MS = 20_000
DEFAULT_POST_CONTEXT_MS = 10_000
SlowdownFactor = Literal[1, 2, 4]
DEFAULT_SLOWDOWN_FACTOR: SlowdownFactor = 2
SUPPORTED_SLOWDOWN_FACTORS = (1, 2, 4)


class BoundaryRefinementPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = BOUNDARY_REFINEMENT_VERSION
    candidate_id: str = Field(pattern=r"^cand_[0-9a-f]{16}$")
    source_start_ms: int = Field(ge=0)
    source_end_ms: int = Field(gt=0)
    anchor_start_ms: int = Field(ge=0)
    anchor_end_ms: int = Field(gt=0)
    slowdown_factor: SlowdownFactor = DEFAULT_SLOWDOWN_FACTOR

    @model_validator(mode="after")
    def intervals_are_ordered(self) -> BoundaryRefinementPlan:
        if self.source_end_ms <= self.source_start_ms:
            raise ValueError("refinement source interval must be non-empty")
        if self.anchor_end_ms <= self.anchor_start_ms:
            raise ValueError("refinement anchor interval must be non-empty")
        if self.anchor_start_ms < self.source_start_ms or self.anchor_end_ms > self.source_end_ms:
            raise ValueError("refinement anchor must be contained by the source interval")
        return self

    @property
    def source_duration_ms(self) -> int:
        return self.source_end_ms - self.source_start_ms

    @property
    def proxy_duration_ms(self) -> int:
        return self.source_duration_ms * self.slowdown_factor

    @property
    def anchor_proxy_start_ms(self) -> int:
        return (self.anchor_start_ms - self.source_start_ms) * self.slowdown_factor

    @property
    def anchor_proxy_end_ms(self) -> int:
        return (self.anchor_end_ms - self.source_start_ms) * self.slowdown_factor


class BoundaryRefinementResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["REFINED", "UNCERTAIN"]
    event_start_ms: int = Field(ge=0)
    event_end_ms: int = Field(gt=0)
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def interval_is_ordered(self) -> BoundaryRefinementResponse:
        if self.event_end_ms <= self.event_start_ms:
            raise ValueError("refined event interval must be non-empty")
        return self


def plan_boundary_refinement(
    candidate: Candidate,
    source_duration_ms: int,
    *,
    pre_context_ms: int = DEFAULT_PRE_CONTEXT_MS,
    post_context_ms: int = DEFAULT_POST_CONTEXT_MS,
    slowdown_factor: int = DEFAULT_SLOWDOWN_FACTOR,
) -> BoundaryRefinementPlan:
    if source_duration_ms <= 0:
        raise ValueError("source duration must be positive")
    if candidate.event_end_ms > source_duration_ms:
        raise ValueError("candidate exceeds source duration")
    if pre_context_ms < 0 or post_context_ms < 0:
        raise ValueError("boundary refinement context cannot be negative")
    if slowdown_factor not in SUPPORTED_SLOWDOWN_FACTORS:
        raise ValueError("slowdown factor must be one of 1, 2, or 4")
    validated_slowdown = cast(SlowdownFactor, slowdown_factor)
    return BoundaryRefinementPlan(
        candidate_id=candidate.candidate_id,
        source_start_ms=max(0, candidate.event_start_ms - pre_context_ms),
        source_end_ms=min(source_duration_ms, candidate.event_end_ms + post_context_ms),
        anchor_start_ms=candidate.event_start_ms,
        anchor_end_ms=candidate.event_end_ms,
        slowdown_factor=validated_slowdown,
    )


def boundary_refinement_schema(plan: BoundaryRefinementPlan) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string", "enum": ["REFINED", "UNCERTAIN"]},
            "event_start_ms": {
                "type": "integer",
                "minimum": 0,
                "maximum": plan.proxy_duration_ms,
            },
            "event_end_ms": {
                "type": "integer",
                "minimum": 0,
                "maximum": plan.proxy_duration_ms,
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string"},
        },
        "required": ["status", "event_start_ms", "event_end_ms", "confidence", "reason"],
    }


def build_boundary_refinement_prompt(plan: BoundaryRefinementPlan, candidate: Candidate) -> str:
    return (
        f"Boundary refinement version: {plan.version}. The supplied clip has been slowed "
        f"{plan.slowdown_factor}x for more granular temporal inspection. Refine the SAME "
        "gameplay event already detected by Scout; do not switch to an adjacent event. "
        "The Scout anchor is approximately "
        f"{plan.anchor_proxy_start_ms}-{plan.anchor_proxy_end_ms} ms in this slowed clip. "
        "Set event_start_ms to the earliest useful reveal, engagement, "
        "or setup needed to understand that event, and event_end_ms through its immediate payoff "
        "or outcome. Use timestamps relative to this slowed clip. Return status UNCERTAIN with "
        "lower confidence if the exact boundary cannot be supported by visible/audio evidence. "
        f"Scout category={candidate.category}; Scout reason={candidate.reason!r}."
    )


def refined_interval_in_source(
    plan: BoundaryRefinementPlan,
    response: BoundaryRefinementResponse,
) -> tuple[int, int]:
    if response.event_end_ms > plan.proxy_duration_ms:
        raise ValueError("refined interval exceeds slowed proxy duration")
    factor = plan.slowdown_factor
    start_ms = plan.source_start_ms + response.event_start_ms // factor
    end_ms = plan.source_start_ms + (response.event_end_ms + factor - 1) // factor
    if end_ms <= start_ms:
        raise ValueError("mapped refinement interval must be non-empty")
    return start_ms, min(end_ms, plan.source_end_ms)


def apply_boundary_refinement(
    candidate: Candidate,
    plan: BoundaryRefinementPlan,
    response: BoundaryRefinementResponse,
    *,
    minimum_confidence: float = 0.5,
) -> Candidate:
    if candidate.candidate_id != plan.candidate_id:
        raise ValueError("boundary refinement plan belongs to a different candidate")
    if not 0 <= minimum_confidence <= 1:
        raise ValueError("minimum confidence must be between 0 and 1")
    if response.status != "REFINED" or response.confidence < minimum_confidence:
        return candidate
    start_ms, end_ms = refined_interval_in_source(plan, response)
    if end_ms <= candidate.event_start_ms or start_ms >= candidate.event_end_ms:
        raise ValueError("refined interval must overlap the Scout anchor event")
    actions = [*candidate.normalization_actions, BOUNDARY_REFINEMENT_VERSION]
    metadata = {**candidate.metadata, "boundary_refinement": BOUNDARY_REFINEMENT_VERSION}
    return candidate.model_copy(
        update={
            "event_start_ms": start_ms,
            "event_end_ms": end_ms,
            "setup_start_ms": min(candidate.setup_start_ms, start_ms)
            if candidate.setup_start_ms is not None
            else None,
            "payoff_end_ms": max(candidate.payoff_end_ms, end_ms)
            if candidate.payoff_end_ms is not None
            else None,
            "clip_start_ms": None,
            "clip_end_ms": None,
            "normalization_actions": actions,
            "metadata": metadata,
        }
    )
