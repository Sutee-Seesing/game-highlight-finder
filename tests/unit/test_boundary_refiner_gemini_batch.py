from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from game_highlight_finder.config import AppConfig, CostConfig, ScoutConfig, StorageConfig
from game_highlight_finder.cost.fx import FxSnapshot
from game_highlight_finder.cost.production import production_pricing_catalog
from game_highlight_finder.cost.service import CostService
from game_highlight_finder.domain.models import Candidate, model_json
from game_highlight_finder.errors import BudgetExceededError, ValidationError
from game_highlight_finder.pipeline.boundary_refinement import (
    BoundaryRefinementMediaArtifact,
    BoundaryRefinementMediaResult,
    plan_boundary_refinement,
)
from game_highlight_finder.pipeline.boundary_refiner_gemini_batch import (
    preflight_gemini_boundary_refinement_batch,
)
from game_highlight_finder.pipeline.gemini_scout import build_gemini_registry
from game_highlight_finder.storage.atomic import atomic_write_json
from game_highlight_finder.storage.hashing import hash_file

NOW = datetime(2026, 8, 26, 4, 0, tzinfo=UTC)
SESSION_ID = "2026-08-26_unknown_333333333333"


def _candidate(candidate_id: str, *, start_ms: int, end_ms: int) -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        category="SKILL",
        event_start_ms=start_ms,
        event_end_ms=end_ms,
        score=7.0,
        confidence=0.9,
        reason=f"Scout anchor {candidate_id}",
    )


def _media(
    tmp_path: Path,
    candidate: Candidate,
) -> BoundaryRefinementMediaResult:
    item_dir = (
        tmp_path
        / "library"
        / "sessions"
        / SESSION_ID
        / "scout"
        / "boundary_refinement"
        / candidate.candidate_id
    )
    item_dir.mkdir(parents=True)
    context_path = item_dir / "context.mp4"
    slowed_path = item_dir / "slowed.mp4"
    artifact_path = item_dir / "artifact.json"
    context_path.write_bytes(f"context-{candidate.candidate_id}".encode())
    slowed_path.write_bytes(f"slowed-{candidate.candidate_id}".encode())
    plan = plan_boundary_refinement(
        candidate,
        4_000,
        pre_context_ms=250,
        post_context_ms=300,
        slowdown_factor=2,
    )
    artifact = BoundaryRefinementMediaArtifact(
        plan=plan,
        parent_proxy_sha256="a" * 64,
        context_proxy_path=(f"scout/boundary_refinement/{candidate.candidate_id}/context.mp4"),
        context_proxy_sha256=hash_file(context_path),
        slowed_proxy_path=f"scout/boundary_refinement/{candidate.candidate_id}/slowed.mp4",
        slowed_proxy_sha256=hash_file(slowed_path),
        encoder="libx264",
        hardware_accelerated=False,
        audio_present=True,
        context_duration_ms=plan.source_duration_ms,
        slowed_proxy_duration_ms=plan.proxy_duration_ms,
    )
    atomic_write_json(artifact_path, model_json(artifact))
    return BoundaryRefinementMediaResult(
        cache_hit=False,
        artifact_path=artifact_path,
        context_path=context_path,
        slowed_proxy_path=slowed_path,
        artifact=artifact,
    )


def _config(tmp_path: Path, *, budget_thb: Decimal = Decimal("100")) -> AppConfig:
    return AppConfig(
        storage=StorageConfig(data_dir=tmp_path / "library"),
        scout=ScoutConfig(
            backend="gemini",
            model="gemini-3.7-flash",
            allow_remote_upload=True,
            thinking_level="low",
        ),
        cost=CostConfig(monthly_budget_thb=budget_thb),
    )


def _service(config: AppConfig) -> CostService:
    return CostService(
        config,
        registry=build_gemini_registry(),
        pricing=production_pricing_catalog(),
        fx_snapshot=FxSnapshot(
            base_currency="USD",
            quote_currency="THB",
            rate=Decimal("32.5"),
            captured_at=NOW,
            source="unit-test",
        ),
    )


def _items(
    tmp_path: Path,
) -> tuple[
    tuple[Candidate, BoundaryRefinementMediaResult],
    tuple[Candidate, BoundaryRefinementMediaResult],
]:
    first = _candidate("cand_1111111111111111", start_ms=500, end_ms=1_200)
    second = _candidate("cand_2222222222222222", start_ms=1_800, end_ms=2_500)
    return (first, _media(tmp_path, first)), (second, _media(tmp_path, second))


def test_batch_preflight_aggregates_exact_quotes_without_ledger_writes(tmp_path: Path) -> None:
    items = _items(tmp_path)
    config = _config(tmp_path)
    service = _service(config)

    result = preflight_gemini_boundary_refinement_batch(
        items,
        config,
        session_id=SESSION_ID,
        cost_service=service,
    )

    assert result.selected_candidate_ids == (
        "cand_1111111111111111",
        "cand_2222222222222222",
    )
    assert tuple(item.candidate_id for item in result.items) == result.selected_candidate_ids
    assert all(item.preflight is not None for item in result.items)
    preflights = tuple(item.preflight for item in result.items if item.preflight is not None)
    assert result.total_base_cost_micro_thb == sum(
        item.quote.base_cost_micro_thb for item in preflights
    )
    assert result.total_reserved_cost_micro_thb == sum(
        item.quote.reserved_cost_micro_thb for item in preflights
    )
    assert result.total_reserved_cost_micro_thb > 0
    assert result.total_reserved_cost_micro_thb <= result.available_micro_thb
    assert service.calls() == ()


def test_batch_preflight_refuses_aggregate_exposure_even_when_each_item_fits(
    tmp_path: Path,
) -> None:
    items = _items(tmp_path)
    high_config = _config(tmp_path)
    high_service = _service(high_config)
    high = preflight_gemini_boundary_refinement_batch(
        items,
        high_config,
        session_id=SESSION_ID,
        cost_service=high_service,
    )
    assert all(item.preflight is not None for item in high.items)
    high_preflights = tuple(item.preflight for item in high.items if item.preflight is not None)
    largest_item = max(item.quote.reserved_cost_micro_thb for item in high_preflights)
    assert high.total_reserved_cost_micro_thb > largest_item

    low_budget = Decimal(largest_item) / Decimal(1_000_000)
    low_config = _config(tmp_path, budget_thb=low_budget)
    low_service = _service(low_config)

    with pytest.raises(BudgetExceededError) as exc_info:
        preflight_gemini_boundary_refinement_batch(
            items,
            low_config,
            session_id=SESSION_ID,
            cost_service=low_service,
        )
    assert exc_info.value.hint is not None
    assert "batch requires" in exc_info.value.hint

    assert low_service.summary().available_micro_thb == largest_item
    assert low_service.calls() == ()


def test_batch_preflight_rejects_duplicate_candidates_before_quote(tmp_path: Path) -> None:
    first, _ = _items(tmp_path)
    config = _config(tmp_path)
    service = _service(config)

    with pytest.raises(ValidationError, match="must be unique"):
        preflight_gemini_boundary_refinement_batch(
            (first, first),
            config,
            session_id=SESSION_ID,
            cost_service=service,
        )

    assert service.calls() == ()
