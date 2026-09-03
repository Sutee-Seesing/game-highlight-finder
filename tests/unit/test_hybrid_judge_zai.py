from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from game_highlight_finder.config import AppConfig, CostConfig, StorageConfig
from game_highlight_finder.cost.fx import FxSnapshot
from game_highlight_finder.cost.production import (
    OPENROUTER_GLM_5V_TURBO_MODEL_ID,
    OPENROUTER_GLM_5V_TURBO_STANDARD_PRICING,
    production_pricing_catalog,
)
from game_highlight_finder.cost.service import CostService
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.pipeline.hybrid_judge_zai import (
    ZAIHybridJudgeSettings,
    build_zai_registry,
    preflight_zai_hybrid_judge_batch,
    run_zai_hybrid_judge_with_transport,
)
from game_highlight_finder.pipeline.hybrid_proposals import (
    HybridAnchor,
    HybridProposal,
    HybridProposalPlan,
    HybridProposalPolicy,
    HybridProposalPreparation,
    PreparedHybridProposal,
)
from game_highlight_finder.providers.base import ProviderUsageActual
from game_highlight_finder.providers.openrouter import (
    OPENROUTER_API_URL,
    FakeOpenRouterTransport,
    OpenRouterConfigurationError,
    OpenRouterHTTPTransport,
    OpenRouterProviderError,
    openrouter_provider_descriptor,
)
from game_highlight_finder.storage.atomic import read_json
from game_highlight_finder.storage.hashing import hash_file

NOW = datetime(2026, 9, 3, 1, 30, tzinfo=UTC)
SESSION_ID = "2026-09-03_unknown_aaaaaaaaaaaa"
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
    total = sum(item.end_ms - item.start_ms for item in proposals)
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


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        storage=StorageConfig(data_dir=tmp_path / "library"),
        cost=CostConfig(monthly_budget_thb=Decimal("100")),
    )


def _service(config: AppConfig) -> CostService:
    return CostService(
        config,
        registry=build_zai_registry(),
        pricing=production_pricing_catalog(),
        fx_snapshot=FxSnapshot(
            base_currency="USD",
            quote_currency="THB",
            rate=Decimal("32.5"),
            captured_at=NOW,
            source="unit-test",
        ),
    )


def _settings(*, allow_remote_media: bool = True) -> ZAIHybridJudgeSettings:
    return ZAIHybridJudgeSettings(allow_remote_media=allow_remote_media)


def _keep_response() -> dict[str, object]:
    return {
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


def test_zai_descriptor_and_pricing_are_exact_and_quality_first() -> None:
    descriptor = openrouter_provider_descriptor()
    model = descriptor.models[0]

    assert descriptor.provider == "openrouter"
    assert (
        model.model_id
        == OPENROUTER_GLM_5V_TURBO_MODEL_ID
        == "z-ai/glm-5v-turbo"
    )
    assert model.capabilities.video_input is True
    assert model.capabilities.file_upload is False
    assert model.capabilities.structured_output is False
    assert OPENROUTER_GLM_5V_TURBO_STANDARD_PRICING.currency == "USD"
    assert OPENROUTER_GLM_5V_TURBO_STANDARD_PRICING.input_rates_by_modality["video"] == Decimal(
        "1.20"
    )
    assert OPENROUTER_GLM_5V_TURBO_STANDARD_PRICING.cached_input_rate == Decimal("0.24")
    assert OPENROUTER_GLM_5V_TURBO_STANDARD_PRICING.output_rate == Decimal("4.00")


def test_zai_preflight_quotes_batch_without_provider_or_ledger_writes(tmp_path: Path) -> None:
    preparation = _preparation(
        tmp_path,
        [
            _proposal("proposal_1111111111111111", 0, 20_000),
            _proposal("proposal_2222222222222222", 30_000, 50_000),
        ],
    )
    config = _config(tmp_path)
    service = _service(config)

    result = preflight_zai_hybrid_judge_batch(
        preparation,
        config,
        settings=_settings(allow_remote_media=False),
        cost_service=service,
    )

    assert result.provider == "openrouter"
    assert result.model == "z-ai/glm-5v-turbo"
    assert result.planned_generation_calls == 2
    assert result.cache_hit_count == 0
    assert result.aggregate_maximum_reserved_micro_thb > 0
    assert result.provider_calls == 0
    assert result.remote_uploads == 0
    assert result.ledger_reservations == 0
    assert result.live_media_transport_verified is True
    assert service.calls() == ()


def test_unverified_openrouter_media_transport_fails_before_reservation(tmp_path: Path) -> None:
    preparation = _preparation(
        tmp_path,
        [_proposal("proposal_3333333333333333", 0, 20_000)],
    )
    config = _config(tmp_path)
    service = _service(config)
    transport = FakeOpenRouterTransport(media_transport_verified=False)

    with pytest.raises(ValidationError, match="not verified"):
        run_zai_hybrid_judge_with_transport(
            preparation,
            preparation.prepared[0],
            config,
            transport=transport,
            settings=_settings(),
            cost_service=service,
        )

    assert service.calls() == ()
    assert transport.generation_count == 0
    assert not (
        preparation.prepared[0].proxy_path.parent / "request.judge.openrouter.json"
    ).exists()


def test_fake_zai_settles_maps_event_and_cache_reuses(tmp_path: Path) -> None:
    proposal = _proposal("proposal_4444444444444444", 50_000, 70_000)
    preparation = _preparation(tmp_path, [proposal])
    config = _config(tmp_path)
    service = _service(config)
    transport = FakeOpenRouterTransport(response=_keep_response())

    first = run_zai_hybrid_judge_with_transport(
        preparation,
        preparation.prepared[0],
        config,
        transport=transport,
        settings=_settings(),
        cost_service=service,
    )

    assert first.cache_hit is False
    assert transport.generation_count == 1
    assert len(first.candidates) == 1
    assert (first.candidates[0].event_start_ms, first.candidates[0].event_end_ms) == (
        54_000,
        61_000,
    )
    request_meta = read_json(first.request_meta_path)
    assert request_meta["provider"] == "openrouter"
    assert request_meta["upstream_provider"] == "z-ai"
    assert request_meta["api_surface"] == "chat_completions"
    assert request_meta["http_attempts"] == 1
    assert request_meta["media_transport_contract"] == "openrouter-base64-video-v1"
    assert read_json(first.cost_path)["state"] == "SETTLED"

    second = run_zai_hybrid_judge_with_transport(
        preparation,
        preparation.prepared[0],
        config,
        transport=transport,
        settings=_settings(),
        cost_service=service,
    )
    assert second.cache_hit is True
    assert second.candidates == first.candidates
    assert transport.generation_count == 1
    assert len(service.calls()) == 1

    preflight = preflight_zai_hybrid_judge_batch(
        preparation,
        config,
        settings=_settings(allow_remote_media=False),
        cost_service=service,
    )
    assert preflight.planned_generation_calls == 0
    assert preflight.cache_hit_count == 1
    assert preflight.aggregate_maximum_reserved_micro_thb == 0


def test_zai_retry_policy_mismatch_fails_before_reservation(tmp_path: Path) -> None:
    preparation = _preparation(
        tmp_path,
        [_proposal("proposal_5555555555555555", 0, 20_000)],
    )
    config = _config(tmp_path)
    service = _service(config)
    transport = FakeOpenRouterTransport(http_retry_attempts=5)

    with pytest.raises(ValidationError, match="attempts=1"):
        run_zai_hybrid_judge_with_transport(
            preparation,
            preparation.prepared[0],
            config,
            transport=transport,
            settings=_settings(),
            cost_service=service,
        )
    assert service.calls() == ()
    assert transport.generation_count == 0


def test_zai_tampered_media_fails_before_reservation(tmp_path: Path) -> None:
    preparation = _preparation(
        tmp_path,
        [_proposal("proposal_6666666666666666", 0, 20_000)],
    )
    config = _config(tmp_path)
    service = _service(config)
    transport = FakeOpenRouterTransport()
    preparation.prepared[0].proxy_path.write_bytes(b"tampered")

    with pytest.raises(ValidationError, match="hash"):
        run_zai_hybrid_judge_with_transport(
            preparation,
            preparation.prepared[0],
            config,
            transport=transport,
            settings=_settings(),
            cost_service=service,
        )
    assert service.calls() == ()
    assert transport.generation_count == 0


def test_zai_completed_invalid_semantic_response_stays_settled(tmp_path: Path) -> None:
    preparation = _preparation(
        tmp_path,
        [_proposal("proposal_7777777777777777", 0, 20_000)],
    )
    config = _config(tmp_path)
    service = _service(config)
    invalid = _keep_response()
    invalid_event = invalid["events"]
    assert isinstance(invalid_event, list)
    invalid_event[0]["confidence"] = 8.0
    transport = FakeOpenRouterTransport(response=invalid)

    with pytest.raises(ValidationError):
        run_zai_hybrid_judge_with_transport(
            preparation,
            preparation.prepared[0],
            config,
            transport=transport,
            settings=_settings(),
            cost_service=service,
        )

    assert transport.generation_count == 1
    assert service.calls()[0].status.value == "SETTLED"
    item_dir = preparation.prepared[0].proxy_path.parent
    assert read_json(item_dir / "cost.judge.openrouter.json")["state"] == "SETTLED"
    assert (item_dir / "response.judge.openrouter.raw.json").exists()
    assert not (item_dir / "response.judge.openrouter.json").exists()

    with pytest.raises(ValidationError, match="settled"):
        run_zai_hybrid_judge_with_transport(
            preparation,
            preparation.prepared[0],
            config,
            transport=transport,
            settings=_settings(),
            cost_service=service,
        )
    assert transport.generation_count == 1


def test_zai_ambiguous_generation_is_persisted_without_retry(tmp_path: Path) -> None:
    preparation = _preparation(
        tmp_path,
        [_proposal("proposal_8888888888888888", 0, 20_000)],
    )
    config = _config(tmp_path)
    service = _service(config)
    transport = FakeOpenRouterTransport(
        generation_error=OpenRouterProviderError(
            "synthetic timeout after dispatch",
            may_have_dispatched=True,
        )
    )

    with pytest.raises(ValidationError, match="timeout after dispatch"):
        run_zai_hybrid_judge_with_transport(
            preparation,
            preparation.prepared[0],
            config,
            transport=transport,
            settings=_settings(),
            cost_service=service,
        )

    assert transport.generation_count == 1
    assert service.calls()[0].status.value == "AMBIGUOUS"
    assert read_json(preparation.prepared[0].proxy_path.parent / "cost.judge.openrouter.json")[
        "state"
    ] == "AMBIGUOUS"

    with pytest.raises(ValidationError, match="unresolved"):
        run_zai_hybrid_judge_with_transport(
            preparation,
            preparation.prepared[0],
            config,
            transport=transport,
            settings=_settings(),
            cost_service=service,
        )
    assert transport.generation_count == 1


def test_zai_actual_usage_can_settle_below_conservative_reservation(tmp_path: Path) -> None:
    preparation = _preparation(
        tmp_path,
        [_proposal("proposal_9999999999999999", 0, 20_000)],
    )
    config = _config(tmp_path)
    service = _service(config)
    transport = FakeOpenRouterTransport(
        usage=ProviderUsageActual(
            input_text_tokens=100,
            input_video_tokens=100,
            output_tokens=10,
            thinking_tokens=10,
            provider_request_id="zai-low-usage",
        )
    )

    result = run_zai_hybrid_judge_with_transport(
        preparation,
        preparation.prepared[0],
        config,
        transport=transport,
        settings=_settings(),
        cost_service=service,
    )

    cost = read_json(result.cost_path)
    assert cost["state"] == "SETTLED"
    assert cost["settled_micro_thb"] < cost["reserved_micro_thb"]


def test_openrouter_http_transport_encodes_local_mp4_and_locks_routing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media = tmp_path / "proposal.mp4"
    media_bytes = b"fake-mp4-bytes-for-openrouter"
    media.write_bytes(media_bytes)
    captured: dict[str, object] = {}

    def fake_post(
        url: str,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> tuple[int, dict[str, str], bytes]:
        captured.update(
            {"url": url, "headers": headers, "body": body, "timeout": timeout_seconds}
        )
        response = {
            "id": "gen-openrouter-1",
            "model": "z-ai/glm-5v-turbo",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            {
                                "decision": "REJECT",
                                "summary": "ordinary gameplay",
                                "events": [],
                            }
                        )
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 6_000,
                "completion_tokens": 200,
                "prompt_tokens_details": {"cached_tokens": 100},
                "completion_tokens_details": {"reasoning_tokens": 50},
                "cost": 0.008,
            },
            "openrouter_metadata": {
                "attempt": 1,
                "endpoints": {
                    "available": [
                        {
                            "provider": "Z.AI",
                            "model": "z-ai/glm-5v-turbo",
                            "selected": True,
                        }
                    ]
                },
            },
        }
        return 200, {"X-Generation-Id": "gen-openrouter-1"}, json.dumps(response).encode()

    monkeypatch.setenv("OPENROUTER_API_KEY", "unit-test-key")
    transport = OpenRouterHTTPTransport(http_post=fake_post)
    envelope = transport.generate(
        media_path=media,
        prompt="judge this clip",
        response_schema={"type": "object"},
        model="z-ai/glm-5v-turbo",
        max_output_tokens=1_024,
        thinking_mode="enabled",
    )

    assert captured["url"] == OPENROUTER_API_URL
    body = captured["body"]
    assert isinstance(body, bytes)
    payload = json.loads(body.decode())
    assert payload["model"] == "z-ai/glm-5v-turbo"
    assert payload["provider"] == {
        "only": ["z-ai"],
        "allow_fallbacks": False,
        "require_parameters": True,
        "max_price": {"prompt": 0.0000012, "completion": 0.000004},
    }
    assert payload["usage"] == {"include": True}
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["reasoning"] == {"enabled": True, "exclude": True}
    video_url = payload["messages"][0]["content"][1]["video_url"]["url"]
    assert video_url.startswith("data:video/mp4;base64,")
    assert base64.b64decode(video_url.split(",", 1)[1]) == media_bytes
    assert envelope.router_attempt_count == 1
    assert envelope.selected_provider == "Z.AI"
    assert envelope.usage.input_text_tokens == 5_900
    assert envelope.usage.cached_input_tokens == 100
    assert envelope.usage.output_tokens == 150
    assert envelope.usage.thinking_tokens == 50
    assert envelope.reported_cost_usd == 0.008


def test_openrouter_payload_guard_blocks_before_http_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media = tmp_path / "large.mp4"
    media.write_bytes(b"x" * 800_000)
    calls = 0

    def fake_post(
        url: str,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> tuple[int, dict[str, str], bytes]:
        nonlocal calls
        del url, headers, body, timeout_seconds
        calls += 1
        return 500, {}, b"{}"

    monkeypatch.setenv("OPENROUTER_API_KEY", "unit-test-key")
    transport = OpenRouterHTTPTransport(max_request_bytes=1_000_000, http_post=fake_post)
    with pytest.raises(OpenRouterConfigurationError, match="payload-size guard"):
        transport.generate(
            media_path=media,
            prompt="judge",
            response_schema={"type": "object"},
            model="z-ai/glm-5v-turbo",
            max_output_tokens=1_024,
            thinking_mode="enabled",
        )
    assert calls == 0


def test_openrouter_http_error_has_exactly_one_client_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media = tmp_path / "proposal.mp4"
    media.write_bytes(b"small-video")
    calls = 0

    def fake_post(
        url: str,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> tuple[int, dict[str, str], bytes]:
        nonlocal calls
        del url, headers, body, timeout_seconds
        calls += 1
        return 503, {"X-Generation-Id": "gen-failed"}, b'{"error":{"message":"busy"}}'

    monkeypatch.setenv("OPENROUTER_API_KEY", "unit-test-key")
    transport = OpenRouterHTTPTransport(http_post=fake_post)
    with pytest.raises(OpenRouterProviderError, match="HTTP 503") as exc_info:
        transport.generate(
            media_path=media,
            prompt="judge",
            response_schema={"type": "object"},
            model="z-ai/glm-5v-turbo",
            max_output_tokens=1_024,
            thinking_mode="enabled",
        )
    assert calls == 1
    assert exc_info.value.may_have_dispatched is True
    assert exc_info.value.provider_request_id == "gen-failed"


def test_router_retry_metadata_is_settled_then_rejected_locally(tmp_path: Path) -> None:
    preparation = _preparation(
        tmp_path,
        [_proposal("proposal_abababababababab", 0, 20_000)],
    )
    config = _config(tmp_path)
    service = _service(config)
    transport = FakeOpenRouterTransport(
        response=_keep_response(),
        router_attempt_count=2,
        selected_provider="Z.AI",
    )

    with pytest.raises(ValidationError, match="exactly one upstream provider attempt"):
        run_zai_hybrid_judge_with_transport(
            preparation,
            preparation.prepared[0],
            config,
            transport=transport,
            settings=_settings(),
            cost_service=service,
        )

    assert transport.generation_count == 1
    assert service.calls()[0].status.value == "SETTLED"
    item_dir = preparation.prepared[0].proxy_path.parent
    assert read_json(item_dir / "cost.judge.openrouter.json")["state"] == "SETTLED"
    assert (item_dir / "response.judge.openrouter.raw.json").exists()
    assert not (item_dir / "response.judge.openrouter.json").exists()
