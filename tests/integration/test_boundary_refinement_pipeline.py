import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from game_highlight_finder.config import AppConfig, CostConfig, StorageConfig, ToolsConfig
from game_highlight_finder.cost.fx import FxSnapshot
from game_highlight_finder.cost.production import production_pricing_catalog
from game_highlight_finder.cost.service import CostService
from game_highlight_finder.domain.models import Candidate
from game_highlight_finder.pipeline.boundary_refinement_runner import (
    preflight_boundary_refinement,
    prepare_boundary_refinement_proxy,
    run_boundary_refinement_diagnostic,
)
from game_highlight_finder.pipeline.gemini_scout import build_gemini_registry
from game_highlight_finder.pipeline.runner import analyze_m6_source
from game_highlight_finder.providers.gemini import FakeGeminiTransport


def _cost_service(config: AppConfig) -> CostService:
    return CostService(
        config,
        registry=build_gemini_registry(),
        pricing=production_pricing_catalog(),
        fx_snapshot=FxSnapshot(
            base_currency="USD",
            quote_currency="THB",
            rate=Decimal("36"),
            captured_at=datetime.now(UTC),
            source="offline-test",
        ),
    )


def test_boundary_refinement_fake_provider_lifecycle_and_reuse(
    tmp_path: Path,
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
) -> None:
    base = AppConfig(
        storage=StorageConfig(data_dir=tmp_path / "library"),
        tools=ToolsConfig(ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path),
        cost=CostConfig(monthly_budget_thb=Decimal("100")),
    )
    local = analyze_m6_source(tiny_video, base, stop_after="windows")
    assert local.proxy is not None
    source = local.ingest.source
    duration = source.duration_ms
    start = max(1, duration // 3)
    end = min(duration - 1, start + max(100, duration // 10))
    candidate = Candidate(
        candidate_id="cand_1111111111111111",
        category="SKILL",
        event_start_ms=start,
        event_end_ms=end,
        score=6.0,
        confidence=0.8,
        reason="boundary lifecycle fixture",
    )
    preparation = prepare_boundary_refinement_proxy(
        candidate,
        source,
        local.proxy.proxy_path,
        tmp_path / "boundary",
        base,
    )
    assert preparation.slow_proxy_path.is_file()
    refined_start = max(0, preparation.plan.anchor_proxy_start_ms - 200)
    refined_end = min(
        preparation.plan.proxy_duration_ms,
        preparation.plan.anchor_proxy_end_ms + 200,
    )
    response = {
        "status": "completed",
        "id": "boundary-interaction",
        "output_text": json.dumps(
            {
                "status": "REFINED",
                "event_start_ms": refined_start,
                "event_end_ms": refined_end,
                "confidence": 0.9,
                "reason": "same event with tighter local boundaries",
            }
        ),
        "usage": {"prompt_token_count": 500, "candidates_token_count": 40},
    }
    transport = FakeGeminiTransport(response=response)
    service = _cost_service(base)
    preflight = preflight_boundary_refinement(
        preparation,
        candidate,
        base,
        session_id=local.proxy.session_id,
        cost_service=service,
    )
    assert preflight.blocked is False
    assert transport.generation_count == 0
    assert service.ledger.list_calls() == ()

    first = run_boundary_refinement_diagnostic(
        preparation,
        candidate,
        base,
        session_id=local.proxy.session_id,
        allow_remote_upload=True,
        gemini_transport=transport,
        cost_service=service,
    )
    assert first.cache_hit is False
    assert first.provider_generation_calls == 1
    assert first.provider_uploads == 1
    assert first.paid_reservations_created == 1
    assert transport.upload_count == 1
    assert transport.generation_count == 1
    assert transport.delete_count == 1
    assert service.ledger.list_calls()[0].status.value == "SETTLED"
    assert first.candidate_after.event_start_ms <= candidate.event_start_ms

    second = run_boundary_refinement_diagnostic(
        preparation,
        candidate,
        base,
        session_id=local.proxy.session_id,
        allow_remote_upload=False,
        gemini_transport=transport,
        cost_service=service,
    )
    assert second.cache_hit is True
    assert second.provider_generation_calls == 0
    assert second.provider_uploads == 0
    assert second.paid_reservations_created == 0
    assert transport.upload_count == 1
    assert transport.generation_count == 1
    assert transport.delete_count == 1
    assert second.candidate_after == first.candidate_after


def test_boundary_refinement_rejects_raw_source_upload_path(
    tmp_path: Path,
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
) -> None:
    import pytest

    from game_highlight_finder.errors import ValidationError

    base = AppConfig(
        storage=StorageConfig(data_dir=tmp_path / "library"),
        tools=ToolsConfig(ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path),
    )
    local = analyze_m6_source(tiny_video, base, stop_after="proxy")
    source = local.ingest.source
    duration = source.duration_ms
    start = max(1, duration // 3)
    end = min(duration - 1, start + max(100, duration // 10))
    candidate = Candidate(
        candidate_id="cand_2222222222222222",
        category="SKILL",
        event_start_ms=start,
        event_end_ms=end,
        score=5.0,
        confidence=0.8,
        reason="privacy boundary fixture",
    )
    with pytest.raises(ValidationError, match="must never use the RAW source"):
        prepare_boundary_refinement_proxy(
            candidate,
            source,
            source.path,
            tmp_path / "boundary",
            base,
        )


def test_boundary_refinement_ambiguous_dispatch_is_never_retried(
    tmp_path: Path,
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
) -> None:
    import pytest

    from game_highlight_finder.errors import ValidationError

    base = AppConfig(
        storage=StorageConfig(data_dir=tmp_path / "library"),
        tools=ToolsConfig(ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path),
        cost=CostConfig(monthly_budget_thb=Decimal("100")),
    )
    local = analyze_m6_source(tiny_video, base, stop_after="windows")
    assert local.proxy is not None
    source = local.ingest.source
    duration = source.duration_ms
    start = max(1, duration // 3)
    end = min(duration - 1, start + max(100, duration // 10))
    candidate = Candidate(
        candidate_id="cand_3333333333333333",
        category="SKILL",
        event_start_ms=start,
        event_end_ms=end,
        score=5.0,
        confidence=0.8,
        reason="ambiguous dispatch fixture",
    )
    preparation = prepare_boundary_refinement_proxy(
        candidate,
        source,
        local.proxy.proxy_path,
        tmp_path / "boundary",
        base,
    )
    transport = FakeGeminiTransport(generation_error=RuntimeError("provider timeout"))
    service = _cost_service(base)

    with pytest.raises(ValidationError, match="no automatic retry"):
        run_boundary_refinement_diagnostic(
            preparation,
            candidate,
            base,
            session_id=local.proxy.session_id,
            allow_remote_upload=True,
            gemini_transport=transport,
            cost_service=service,
        )
    assert transport.generation_count == 1
    assert transport.delete_count == 1
    assert service.ledger.list_calls()[0].status.value == "AMBIGUOUS"

    with pytest.raises(ValidationError, match="unresolved cost lifecycle"):
        run_boundary_refinement_diagnostic(
            preparation,
            candidate,
            base,
            session_id=local.proxy.session_id,
            allow_remote_upload=True,
            gemini_transport=transport,
            cost_service=service,
        )
    assert transport.generation_count == 1
    assert transport.upload_count == 1
