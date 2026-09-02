from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from game_highlight_finder.config import AppConfig, CostConfig, ScoutConfig, StorageConfig
from game_highlight_finder.cost.fx import FxSnapshot
from game_highlight_finder.cost.production import production_pricing_catalog
from game_highlight_finder.cost.service import CostService
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.pipeline.gemini_scout import build_gemini_registry
from game_highlight_finder.pipeline.hybrid_judge_gemini import (
    preflight_gemini_hybrid_judge_batch,
    run_gemini_hybrid_judge_batch_with_transport,
    run_gemini_hybrid_judge_with_transport,
)
from game_highlight_finder.pipeline.hybrid_proposals import (
    HybridAnchor,
    HybridProposal,
    HybridProposalPlan,
    HybridProposalPolicy,
    HybridProposalPreparation,
    PreparedHybridProposal,
)
from game_highlight_finder.providers.gemini import FakeGeminiTransport, GeminiProviderError
from game_highlight_finder.storage.atomic import read_json
from game_highlight_finder.storage.hashing import hash_file

NOW = datetime(2026, 9, 2, 4, 0, tzinfo=UTC)
SESSION_ID = "2026-09-02_unknown_aaaaaaaaaaaa"
SOURCE_ID = "src_" + "b" * 16


def _proposal(proposal_id: str, start_ms: int, end_ms: int) -> HybridProposal:
    return HybridProposal(
        proposal_id=proposal_id,
        start_ms=start_ms,
        end_ms=end_ms,
        anchors=[
            HybridAnchor(
                anchor_ms=start_ms + min(10_000, (end_ms - start_ms) // 2),
                audio_percentile=0.95,
                motion_percentile=0.9,
                fused_score=1.2,
                selected_by="FUSED",
            )
        ],
    )


def _preparation(tmp_path: Path, proposals: list[HybridProposal]) -> HybridProposalPreparation:
    prepared: list[PreparedHybridProposal] = []
    for proposal in proposals:
        item_dir = (
            tmp_path
            / "library"
            / "sessions"
            / SESSION_ID
            / "scout"
            / "proposals"
            / "clips"
            / proposal.proposal_id
        )
        item_dir.mkdir(parents=True)
        media = item_dir / "analysis_proposal.mp4"
        media.write_bytes(f"proposal-media-{proposal.proposal_id}".encode())
        prepared.append(
            PreparedHybridProposal(
                proposal=proposal,
                proxy_path=media,
                proxy_sha256=hash_file(media),
                cache_hit=False,
            )
        )
    intervals = sorted((item.start_ms, item.end_ms) for item in proposals)
    total = 0
    if intervals:
        start, end = intervals[0]
        for next_start, next_end in intervals[1:]:
            if next_start <= end:
                end = max(end, next_end)
            else:
                total += end - start
                start, end = next_start, next_end
        total += end - start
    plan = HybridProposalPlan(
        session_id=SESSION_ID,
        source_id=SOURCE_ID,
        source_duration_ms=120_000,
        parent_proxy_sha256="c" * 64,
        local_signals_sha256="d" * 64,
        policy=HybridProposalPolicy(),
        anchors=[anchor for proposal in proposals for anchor in proposal.anchors],
        proposals=proposals,
        total_proposed_duration_ms=total,
        proposal_ratio=total / 120_000,
        plan_hash="e" * 64,
    )
    return HybridProposalPreparation(
        plan=plan,
        plan_path=tmp_path / "plan.json",
        motion_path=tmp_path / "motion.json",
        motion_cache_hit=False,
        prepared=tuple(prepared),
        cache_hits=0,
        generated=len(prepared),
    )


def _config(tmp_path: Path, *, allow_remote_upload: bool = True) -> AppConfig:
    return AppConfig(
        storage=StorageConfig(data_dir=tmp_path / "library"),
        scout=ScoutConfig(
            backend="gemini",
            model="gemini-3.7-flash",
            allow_remote_upload=allow_remote_upload,
            thinking_level="low",
        ),
        cost=CostConfig(monthly_budget_thb=Decimal("100")),
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


def _provider_response() -> dict[str, object]:
    return {
        "status": "completed",
        "id": "fake-hybrid-judge-interaction-1",
        "output_text": json.dumps(
            {
                "decision": "KEEP",
                "summary": "visible fight with payoff",
                "events": [
                    {
                        "event_start_ms": 4_000,
                        "event_end_ms": 11_000,
                        "category": "SKILL",
                        "score": 8.0,
                        "confidence": 0.9,
                        "reason": "visible multi-kill engagement",
                        "visible_evidence": ["two visible eliminations before the fight ends"],
                    }
                ],
            }
        ),
        "usage": {
            "prompt_token_count": 900,
            "candidates_token_count": 80,
            "thoughts_token_count": 20,
        },
    }


def test_batch_preflight_quotes_all_proposals_without_ledger_writes(tmp_path: Path) -> None:
    preparation = _preparation(
        tmp_path,
        [
            _proposal("proposal_1111111111111111", 0, 20_000),
            _proposal("proposal_2222222222222222", 30_000, 50_000),
        ],
    )
    config = _config(tmp_path, allow_remote_upload=False)
    service = _service(config)

    result = preflight_gemini_hybrid_judge_batch(preparation, config, cost_service=service)

    assert result.provider == "gemini"
    assert result.model == "gemini-3.7-flash"
    assert result.planned_generation_calls == 2
    assert result.cache_hit_count == 0
    assert result.aggregate_maximum_reserved_micro_thb > 0
    assert result.post_reservation_headroom_micro_thb >= 0
    assert result.provider_calls == 0
    assert result.remote_uploads == 0
    assert result.ledger_reservations == 0
    assert all(item.maximum_reserved_micro_thb > 0 for item in result.items)
    assert service.calls() == ()


def test_injected_fake_transport_settles_maps_event_and_cache_reuses(tmp_path: Path) -> None:
    proposal = _proposal("proposal_3333333333333333", 50_000, 70_000)
    preparation = _preparation(tmp_path, [proposal])
    config = _config(tmp_path)
    service = _service(config)
    transport = FakeGeminiTransport(response=_provider_response())

    first = run_gemini_hybrid_judge_with_transport(
        preparation,
        preparation.prepared[0],
        config,
        transport=transport,
        cost_service=service,
    )

    assert first.cache_hit is False
    assert transport.upload_count == 1
    assert transport.generation_count == 1
    assert transport.delete_count == 1
    assert transport.uploaded_paths == [preparation.prepared[0].proxy_path]
    assert len(first.candidates) == 1
    assert (first.candidates[0].event_start_ms, first.candidates[0].event_end_ms) == (
        54_000,
        61_000,
    )
    assert read_json(first.request_meta_path)["execution_mode"] == "injected_transport"
    assert read_json(first.cost_path)["state"] == "SETTLED"
    assert read_json(first.remote_metadata_path)["deletion_status"] == "deleted"

    second = run_gemini_hybrid_judge_with_transport(
        preparation,
        preparation.prepared[0],
        config,
        transport=transport,
        cost_service=service,
    )
    assert second.cache_hit is True
    assert second.candidates == first.candidates
    assert transport.upload_count == 1
    assert transport.generation_count == 1
    assert len(service.calls()) == 1

    preflight = preflight_gemini_hybrid_judge_batch(
        preparation,
        config,
        cost_service=service,
    )
    assert preflight.planned_generation_calls == 0
    assert preflight.cache_hit_count == 1
    assert preflight.aggregate_maximum_reserved_micro_thb == 0


def test_remote_upload_opt_in_is_required_before_reservation(tmp_path: Path) -> None:
    preparation = _preparation(
        tmp_path,
        [_proposal("proposal_4444444444444444", 0, 20_000)],
    )
    config = _config(tmp_path, allow_remote_upload=False)
    service = _service(config)
    transport = FakeGeminiTransport(response=_provider_response())

    with pytest.raises(ValidationError, match="opt-in"):
        run_gemini_hybrid_judge_with_transport(
            preparation,
            preparation.prepared[0],
            config,
            transport=transport,
            cost_service=service,
        )
    assert service.calls() == ()
    assert transport.upload_count == 0


def test_tampered_proposal_media_fails_before_reservation_or_transport(tmp_path: Path) -> None:
    preparation = _preparation(
        tmp_path,
        [_proposal("proposal_5555555555555555", 0, 20_000)],
    )
    config = _config(tmp_path)
    service = _service(config)
    transport = FakeGeminiTransport(response=_provider_response())
    preparation.prepared[0].proxy_path.write_bytes(b"tampered")

    with pytest.raises(ValidationError, match="hash"):
        run_gemini_hybrid_judge_with_transport(
            preparation,
            preparation.prepared[0],
            config,
            transport=transport,
            cost_service=service,
        )
    assert service.calls() == ()
    assert transport.upload_count == 0
    assert transport.generation_count == 0


def test_ambiguous_generation_is_persisted_and_never_auto_retried(tmp_path: Path) -> None:
    preparation = _preparation(
        tmp_path,
        [_proposal("proposal_6666666666666666", 0, 20_000)],
    )
    config = _config(tmp_path)
    service = _service(config)
    transport = FakeGeminiTransport(
        response=_provider_response(),
        generation_error=GeminiProviderError(
            "synthetic hybrid judge timeout after dispatch",
            may_have_dispatched=True,
        ),
    )

    with pytest.raises(ValidationError, match="timeout after dispatch"):
        run_gemini_hybrid_judge_with_transport(
            preparation,
            preparation.prepared[0],
            config,
            transport=transport,
            cost_service=service,
        )
    assert len(service.calls()) == 1
    assert service.calls()[0].status.value == "AMBIGUOUS"
    assert transport.generation_count == 1
    item_dir = preparation.prepared[0].proxy_path.parent
    assert read_json(item_dir / "cost.judge.gemini.json")["state"] == "AMBIGUOUS"

    with pytest.raises(ValidationError, match="unresolved"):
        run_gemini_hybrid_judge_with_transport(
            preparation,
            preparation.prepared[0],
            config,
            transport=transport,
            cost_service=service,
        )
    assert transport.generation_count == 1


def test_injected_batch_preflights_then_reuses_every_settled_item(tmp_path: Path) -> None:
    preparation = _preparation(
        tmp_path,
        [
            _proposal("proposal_7777777777777777", 0, 20_000),
            _proposal("proposal_8888888888888888", 30_000, 50_000),
        ],
    )
    config = _config(tmp_path)
    service = _service(config)
    transport = FakeGeminiTransport(response=_provider_response())

    first = run_gemini_hybrid_judge_batch_with_transport(
        preparation,
        config,
        transport=transport,
        cost_service=service,
    )

    assert first.generation_calls == 2
    assert first.cache_hits == 0
    assert transport.upload_count == 2
    assert transport.generation_count == 2
    assert transport.delete_count == 2
    assert len(first.item_results) == 2
    assert len(first.candidates) == 2
    assert len(service.calls()) == 2

    second = run_gemini_hybrid_judge_batch_with_transport(
        preparation,
        config,
        transport=transport,
        cost_service=service,
    )
    assert second.generation_calls == 0
    assert second.cache_hits == 2
    assert second.candidates == first.candidates
    assert transport.generation_count == 2
    assert len(service.calls()) == 2

    preflight = preflight_gemini_hybrid_judge_batch(preparation, config, cost_service=service)
