from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from game_highlight_finder.config import AppConfig, CostConfig, ScoutConfig, StorageConfig
from game_highlight_finder.cost.fx import FxSnapshot
from game_highlight_finder.cost.production import production_pricing_catalog
from game_highlight_finder.cost.service import CostService
from game_highlight_finder.domain.models import Candidate, model_json
from game_highlight_finder.pipeline.boundary_refinement import (
    BoundaryRefinementMediaArtifact,
    BoundaryRefinementMediaResult,
    plan_boundary_refinement,
)
from game_highlight_finder.pipeline.boundary_refiner_gemini import (
    run_gemini_boundary_refinement_with_transport,
)
from game_highlight_finder.pipeline.boundary_refiner_gemini_batch import (
    preflight_gemini_boundary_refinement_batch,
)
from game_highlight_finder.pipeline.gemini_scout import build_gemini_registry
from game_highlight_finder.providers.gemini import FakeGeminiTransport
from game_highlight_finder.storage.atomic import atomic_write_json
from game_highlight_finder.storage.hashing import hash_file

NOW = datetime(2026, 8, 28, 1, 0, tzinfo=UTC)
SESSION_ID = "2026-08-28_unknown_444444444444"


def _candidate(candidate_id: str, start_ms: int, end_ms: int) -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        category="SKILL",
        event_start_ms=start_ms,
        event_end_ms=end_ms,
        score=8.0,
        confidence=0.9,
        reason=f"cache-aware preflight anchor {candidate_id}",
    )


def _media(tmp_path: Path, candidate: Candidate) -> BoundaryRefinementMediaResult:
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
        context_proxy_path=f"scout/boundary_refinement/{candidate.candidate_id}/context.mp4",
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


def _config(tmp_path: Path, budget_micro_thb: int = 100_000_000) -> AppConfig:
    return AppConfig(
        storage=StorageConfig(data_dir=tmp_path / "library"),
        scout=ScoutConfig(
            backend="gemini",
            model="gemini-3.7-flash",
            allow_remote_upload=True,
            thinking_level="low",
        ),
        cost=CostConfig(
            monthly_budget_thb=Decimal(budget_micro_thb) / Decimal(1_000_000)
        ),
    )


def _service(config: AppConfig, *, with_fx: bool = True) -> CostService:
    return CostService(
        config,
        registry=build_gemini_registry(),
        pricing=production_pricing_catalog(),
        fx_snapshot=(
            FxSnapshot(
                base_currency="USD",
                quote_currency="THB",
                rate=Decimal("32.5"),
                captured_at=NOW,
                source="cache-preflight-test",
            )
            if with_fx
            else None
        ),
    )


def _provider_response(interaction_id: str) -> dict[str, object]:
    return {
        "status": "completed",
        "id": interaction_id,
        "output_text": json.dumps(
            {
                "status": "REFINED",
                "event_start_ms": 400,
                "event_end_ms": 2_100,
                "confidence": 0.9,
                "reason": "same event with provider-refined local boundaries",
            }
        ),
        "usage": {
            "prompt_token_count": 1_000,
            "candidates_token_count": 50,
            "thoughts_token_count": 10,
        },
    }


def test_batch_preflight_counts_only_incremental_exposure_for_settled_cache_hits(
    tmp_path: Path,
) -> None:
    first = _candidate("cand_1111111111111111", 500, 1_200)
    second = _candidate("cand_2222222222222222", 1_800, 2_500)
    items = ((first, _media(tmp_path, first)), (second, _media(tmp_path, second)))
    high_config = _config(tmp_path)
    high_service = _service(high_config)

    run_gemini_boundary_refinement_with_transport(
        items[0][1],
        first,
        high_config,
        session_id=SESSION_ID,
        transport=FakeGeminiTransport(response=_provider_response("first-settled")),
        cost_service=high_service,
    )

    mixed_high = preflight_gemini_boundary_refinement_batch(
        items,
        high_config,
        session_id=SESSION_ID,
        cost_service=high_service,
    )
    assert mixed_high.items[0].cache_hit is True
    assert mixed_high.items[0].preflight is None
    assert mixed_high.items[1].cache_hit is False
    assert mixed_high.items[1].preflight is not None
    second_reserved = mixed_high.items[1].preflight.quote.reserved_cost_micro_thb
    first_settled = high_service.calls()[0].settled_cost_micro_thb or 0

    exact_budget = first_settled + second_reserved
    mixed_config = _config(tmp_path, exact_budget)
    mixed_service = _service(mixed_config)
    mixed = preflight_gemini_boundary_refinement_batch(
        items,
        mixed_config,
        session_id=SESSION_ID,
        cost_service=mixed_service,
    )

    assert mixed.items[0].cache_hit is True
    assert mixed.items[0].preflight is None
    assert mixed.items[1].cache_hit is False
    assert mixed.items[1].preflight is not None
    assert mixed.total_reserved_cost_micro_thb == second_reserved
    assert mixed.available_micro_thb == second_reserved

    run_gemini_boundary_refinement_with_transport(
        items[1][1],
        second,
        mixed_config,
        session_id=SESSION_ID,
        transport=FakeGeminiTransport(response=_provider_response("second-settled")),
        cost_service=mixed_service,
    )
    total_settled = sum(call.settled_cost_micro_thb or 0 for call in mixed_service.calls())
    no_headroom_config = _config(tmp_path, total_settled)
    no_headroom_service = _service(no_headroom_config)

    cached = preflight_gemini_boundary_refinement_batch(
        items,
        no_headroom_config,
        session_id=SESSION_ID,
        cost_service=no_headroom_service,
    )

    assert all(item.cache_hit for item in cached.items)
    assert all(item.preflight is None for item in cached.items)
    assert cached.total_base_cost_micro_thb == 0
    assert cached.total_reserved_cost_micro_thb == 0
    assert cached.available_micro_thb == 0

    no_fx_service = _service(no_headroom_config, with_fx=False)
    cached_without_fresh_quote = preflight_gemini_boundary_refinement_batch(
        items,
        no_headroom_config,
        session_id=SESSION_ID,
        cost_service=no_fx_service,
    )

    assert all(item.cache_hit for item in cached_without_fresh_quote.items)
    assert all(item.preflight is None for item in cached_without_fresh_quote.items)
    assert cached_without_fresh_quote.total_reserved_cost_micro_thb == 0
