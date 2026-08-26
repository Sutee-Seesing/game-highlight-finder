"""Provider-free aggregate preflight for a bounded Gemini boundary-refiner batch."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from game_highlight_finder.config import AppConfig
from game_highlight_finder.cost.service import CostService
from game_highlight_finder.domain.models import Candidate
from game_highlight_finder.errors import BudgetExceededError, ValidationError
from game_highlight_finder.pipeline.boundary_refinement import BoundaryRefinementMediaResult
from game_highlight_finder.pipeline.boundary_refinement_batch import (
    MAX_BOUNDARY_REFINEMENT_BATCH_CANDIDATES,
)
from game_highlight_finder.pipeline.boundary_refiner_gemini import (
    GeminiBoundaryRefinementPreflight,
    preflight_gemini_boundary_refinement,
)

BOUNDARY_REFINER_GEMINI_BATCH_PREFLIGHT_VERSION = "boundary-refiner-gemini-batch-preflight-v1"


class GeminiBoundaryRefinementBatchPreflightItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(pattern=r"^cand_[0-9a-f]{16}$")
    preflight: GeminiBoundaryRefinementPreflight


class GeminiBoundaryRefinementBatchPreflight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = BOUNDARY_REFINER_GEMINI_BATCH_PREFLIGHT_VERSION
    session_id: str = Field(min_length=1, max_length=128)
    selected_candidate_ids: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_BOUNDARY_REFINEMENT_BATCH_CANDIDATES,
    )
    items: tuple[GeminiBoundaryRefinementBatchPreflightItem, ...]
    total_base_cost_micro_thb: int = Field(ge=0)
    total_reserved_cost_micro_thb: int = Field(ge=0)
    available_micro_thb: int = Field(ge=0)

    @model_validator(mode="after")
    def item_order_matches_selection(self) -> GeminiBoundaryRefinementBatchPreflight:
        if tuple(item.candidate_id for item in self.items) != self.selected_candidate_ids:
            raise ValueError(
                "Gemini boundary-refiner batch items must match selected candidate order"
            )
        return self


def preflight_gemini_boundary_refinement_batch(
    items: Sequence[tuple[Candidate, BoundaryRefinementMediaResult]],
    config: AppConfig,
    *,
    session_id: str,
    cost_service: CostService,
) -> GeminiBoundaryRefinementBatchPreflight:
    """Quote a whole explicit candidate batch without reservation, upload, or generation."""

    selected = tuple(items)
    if not selected:
        raise ValidationError("Gemini boundary-refiner batch preflight requires explicit items")
    if len(selected) > MAX_BOUNDARY_REFINEMENT_BATCH_CANDIDATES:
        raise ValidationError(
            "Gemini boundary-refiner batch preflight exceeds the maximum candidate count "
            f"({MAX_BOUNDARY_REFINEMENT_BATCH_CANDIDATES})"
        )
    candidate_ids = tuple(candidate.candidate_id for candidate, _ in selected)
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValidationError("Gemini boundary-refiner batch candidate IDs must be unique")

    preflight_items: list[GeminiBoundaryRefinementBatchPreflightItem] = []
    for candidate, media in selected:
        preflight = preflight_gemini_boundary_refinement(
            media,
            candidate,
            config,
            session_id=session_id,
            cost_service=cost_service,
        )
        preflight_items.append(
            GeminiBoundaryRefinementBatchPreflightItem(
                candidate_id=candidate.candidate_id,
                preflight=preflight,
            )
        )

    total_base = sum(item.preflight.quote.base_cost_micro_thb for item in preflight_items)
    total_reserved = sum(item.preflight.quote.reserved_cost_micro_thb for item in preflight_items)
    available = preflight_items[0].preflight.available_micro_thb
    if total_reserved > available:
        raise BudgetExceededError(
            hint=(
                f"Gemini boundary-refiner batch requires {total_reserved} micro-THB, "
                f"but only {available} is available."
            )
        )

    return GeminiBoundaryRefinementBatchPreflight(
        session_id=session_id,
        selected_candidate_ids=candidate_ids,
        items=tuple(preflight_items),
        total_base_cost_micro_thb=total_base,
        total_reserved_cost_micro_thb=total_reserved,
        available_micro_thb=available,
    )


__all__ = [
    "BOUNDARY_REFINER_GEMINI_BATCH_PREFLIGHT_VERSION",
    "GeminiBoundaryRefinementBatchPreflight",
    "GeminiBoundaryRefinementBatchPreflightItem",
    "preflight_gemini_boundary_refinement_batch",
]
