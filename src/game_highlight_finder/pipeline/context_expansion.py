"""Pure proposal-centered context planning for hybrid semantic inspection."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from game_highlight_finder.domain.proposals import Proposal

CONTEXT_PLANNER_VERSION = "c1-context-planner-v1"
DEFAULT_PRE_CONTEXT_MS = 15_000
DEFAULT_POST_CONTEXT_MS = 30_000
DEFAULT_EXPANSION_STEP_MS = 30_000
DEFAULT_MAX_CONTEXT_MS = 120_000
ExpansionDirection = Literal["before", "after", "both"]


class ContextExpansionPlan(BaseModel):
    """Bounded source-relative media neighborhood around one factual proposal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = CONTEXT_PLANNER_VERSION
    proposal_id: str = Field(pattern=r"^prop_[0-9a-f]{16}$")
    source_duration_ms: int = Field(gt=0)
    anchor_start_ms: int = Field(ge=0)
    anchor_end_ms: int = Field(gt=0)
    context_start_ms: int = Field(ge=0)
    context_end_ms: int = Field(gt=0)
    expansion_step_ms: int = Field(gt=0)
    max_context_ms: int = Field(gt=0)
    expansion_count: int = Field(default=0, ge=0)
    exhausted: bool = False

    @model_validator(mode="after")
    def intervals_are_valid(self) -> ContextExpansionPlan:
        if self.anchor_end_ms <= self.anchor_start_ms:
            raise ValueError("context anchor must be non-empty")
        if self.context_end_ms <= self.context_start_ms:
            raise ValueError("context interval must be non-empty")
        if self.anchor_start_ms < self.context_start_ms or self.anchor_end_ms > self.context_end_ms:
            raise ValueError("context interval must contain the proposal anchor")
        if self.context_end_ms > self.source_duration_ms:
            raise ValueError("context interval exceeds source duration")
        if self.context_end_ms - self.context_start_ms > self.max_context_ms:
            raise ValueError("context interval exceeds configured maximum")
        return self

    @property
    def context_duration_ms(self) -> int:
        return self.context_end_ms - self.context_start_ms


def plan_proposal_context(
    proposal: Proposal,
    source_duration_ms: int,
    *,
    pre_context_ms: int = DEFAULT_PRE_CONTEXT_MS,
    post_context_ms: int = DEFAULT_POST_CONTEXT_MS,
    expansion_step_ms: int = DEFAULT_EXPANSION_STEP_MS,
    max_context_ms: int = DEFAULT_MAX_CONTEXT_MS,
) -> ContextExpansionPlan:
    """Create initial bounded semantic context around one factual proposal."""

    _validate_policy(
        source_duration_ms=source_duration_ms,
        pre_context_ms=pre_context_ms,
        post_context_ms=post_context_ms,
        expansion_step_ms=expansion_step_ms,
        max_context_ms=max_context_ms,
    )
    if proposal.end_ms > source_duration_ms:
        raise ValueError("proposal exceeds source duration")
    anchor_duration = proposal.end_ms - proposal.start_ms
    if anchor_duration > max_context_ms:
        raise ValueError("proposal anchor is longer than the context budget")

    desired_start = max(0, proposal.start_ms - pre_context_ms)
    desired_end = min(source_duration_ms, proposal.end_ms + post_context_ms)
    start_ms, end_ms = _fit_budget(
        anchor_start_ms=proposal.start_ms,
        anchor_end_ms=proposal.end_ms,
        desired_start_ms=desired_start,
        desired_end_ms=desired_end,
        source_duration_ms=source_duration_ms,
        max_context_ms=max_context_ms,
    )
    exhausted = _cannot_expand(
        start_ms,
        end_ms,
        source_duration_ms=source_duration_ms,
        max_context_ms=max_context_ms,
    )
    return ContextExpansionPlan(
        proposal_id=proposal.proposal_id,
        source_duration_ms=source_duration_ms,
        anchor_start_ms=proposal.start_ms,
        anchor_end_ms=proposal.end_ms,
        context_start_ms=start_ms,
        context_end_ms=end_ms,
        expansion_step_ms=expansion_step_ms,
        max_context_ms=max_context_ms,
        exhausted=exhausted,
    )


def expand_context(
    plan: ContextExpansionPlan,
    *,
    direction: ExpansionDirection = "both",
) -> ContextExpansionPlan:
    """Expand context deterministically without exceeding source or context budget."""

    if plan.exhausted:
        return plan

    before_step = plan.expansion_step_ms if direction in {"before", "both"} else 0
    after_step = plan.expansion_step_ms if direction in {"after", "both"} else 0
    desired_start = max(0, plan.context_start_ms - before_step)
    desired_end = min(plan.source_duration_ms, plan.context_end_ms + after_step)
    start_ms, end_ms = _fit_budget(
        anchor_start_ms=plan.anchor_start_ms,
        anchor_end_ms=plan.anchor_end_ms,
        desired_start_ms=desired_start,
        desired_end_ms=desired_end,
        source_duration_ms=plan.source_duration_ms,
        max_context_ms=plan.max_context_ms,
        prefer_direction=direction,
    )
    changed = start_ms != plan.context_start_ms or end_ms != plan.context_end_ms
    exhausted = _cannot_expand(
        start_ms,
        end_ms,
        source_duration_ms=plan.source_duration_ms,
        max_context_ms=plan.max_context_ms,
    )
    return plan.model_copy(
        update={
            "context_start_ms": start_ms,
            "context_end_ms": end_ms,
            "expansion_count": plan.expansion_count + (1 if changed else 0),
            "exhausted": exhausted or not changed,
        }
    )


def expand_if_needed(
    plan: ContextExpansionPlan,
    *,
    needs_more_context: bool,
    direction: ExpansionDirection = "both",
) -> ContextExpansionPlan:
    """Apply one expansion only when a judge/verifier explicitly requests it."""

    if not needs_more_context:
        return plan
    return expand_context(plan, direction=direction)


def _validate_policy(
    *,
    source_duration_ms: int,
    pre_context_ms: int,
    post_context_ms: int,
    expansion_step_ms: int,
    max_context_ms: int,
) -> None:
    if source_duration_ms <= 0:
        raise ValueError("source duration must be positive")
    if pre_context_ms < 0 or post_context_ms < 0:
        raise ValueError("initial context cannot be negative")
    if expansion_step_ms <= 0:
        raise ValueError("expansion step must be positive")
    if max_context_ms <= 0:
        raise ValueError("max context must be positive")


def _fit_budget(
    *,
    anchor_start_ms: int,
    anchor_end_ms: int,
    desired_start_ms: int,
    desired_end_ms: int,
    source_duration_ms: int,
    max_context_ms: int,
    prefer_direction: ExpansionDirection = "both",
) -> tuple[int, int]:
    if desired_end_ms - desired_start_ms <= max_context_ms:
        return desired_start_ms, desired_end_ms

    if prefer_direction == "after":
        end_ms = min(source_duration_ms, desired_end_ms)
        start_ms = max(0, end_ms - max_context_ms)
    elif prefer_direction == "before":
        start_ms = max(0, desired_start_ms)
        end_ms = min(source_duration_ms, start_ms + max_context_ms)
    else:
        anchor_mid = (anchor_start_ms + anchor_end_ms) // 2
        half = max_context_ms // 2
        start_ms = max(0, anchor_mid - half)
        end_ms = min(source_duration_ms, start_ms + max_context_ms)
        start_ms = max(0, end_ms - max_context_ms)

    if start_ms > anchor_start_ms:
        start_ms = anchor_start_ms
        end_ms = min(source_duration_ms, start_ms + max_context_ms)
    if end_ms < anchor_end_ms:
        end_ms = anchor_end_ms
        start_ms = max(0, end_ms - max_context_ms)
    return start_ms, end_ms


def _cannot_expand(
    start_ms: int,
    end_ms: int,
    *,
    source_duration_ms: int,
    max_context_ms: int,
) -> bool:
    return (
        end_ms - start_ms >= max_context_ms
        or (start_ms == 0 and end_ms == source_duration_ms)
    )


__all__ = [
    "CONTEXT_PLANNER_VERSION",
    "DEFAULT_EXPANSION_STEP_MS",
    "DEFAULT_MAX_CONTEXT_MS",
    "DEFAULT_POST_CONTEXT_MS",
    "DEFAULT_PRE_CONTEXT_MS",
    "ContextExpansionPlan",
    "ExpansionDirection",
    "expand_context",
    "expand_if_needed",
    "plan_proposal_context",
]
