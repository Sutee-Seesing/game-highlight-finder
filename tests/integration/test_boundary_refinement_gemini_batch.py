from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from game_highlight_finder import __version__
from game_highlight_finder.config import (
    AppConfig,
    CostConfig,
    ExtractionConfig,
    MediaConfig,
    ProxyConfig,
    ScoutConfig,
    StorageConfig,
    ToolsConfig,
)
from game_highlight_finder.cost.fx import FxSnapshot
from game_highlight_finder.cost.production import production_pricing_catalog
from game_highlight_finder.cost.service import CostService
from game_highlight_finder.domain.models import Candidate, SessionMap, model_json
from game_highlight_finder.pipeline.boundary_refiner_gemini_batch import (
    run_gemini_boundary_refinement_batch_with_transports,
)
from game_highlight_finder.pipeline.gemini_scout import build_gemini_registry
from game_highlight_finder.pipeline.ingest import ingest_source
from game_highlight_finder.pipeline.proxy import generate_proxy
from game_highlight_finder.providers.gemini import FakeGeminiTransport
from game_highlight_finder.storage.atomic import read_json
from game_highlight_finder.storage.hashing import hash_file

pytestmark = pytest.mark.integration
NOW = datetime(2026, 8, 26, 10, 0, tzinfo=UTC)


def _provider_response(
    payload: dict[str, object],
    *,
    interaction_id: str,
) -> dict[str, object]:
    return {
        "status": "completed",
        "id": interaction_id,
        "output_text": json.dumps(payload),
        "usage": {
            "prompt_token_count": 1_000,
            "candidates_token_count": 50,
            "thoughts_token_count": 10,
        },
    }


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
            source="integration-test",
        ),
    )


def test_gemini_boundary_refinement_batch_preflights_executes_persists_and_resumes(
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    tmp_path: Path,
) -> None:
    config = AppConfig(
        storage=StorageConfig(data_dir=tmp_path / "library"),
        tools=ToolsConfig(ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path),
        media=MediaConfig(
            proxy=ProxyConfig(video_codec="libx264", preset="veryfast"),
            extraction=ExtractionConfig(video_codec="libx264", preset="veryfast"),
        ),
        scout=ScoutConfig(
            backend="gemini",
            model="gemini-3.7-flash",
            allow_remote_upload=True,
            thinking_level="low",
        ),
        cost=CostConfig(monthly_budget_thb=Decimal("100")),
    )
    source_before = hash_file(tiny_video, source=True)
    ingest = ingest_source(tiny_video, config)
    proxy = generate_proxy(ingest.source, config)
    first_candidate = Candidate(
        candidate_id="cand_1111111111111111",
        category="SKILL",
        event_start_ms=500,
        event_end_ms=1_000,
        score=8.0,
        confidence=0.9,
        reason="first synthetic Scout candidate",
    )
    second_candidate = Candidate(
        candidate_id="cand_2222222222222222",
        category="FUNNY",
        event_start_ms=1_200,
        event_end_ms=1_700,
        score=7.0,
        confidence=0.8,
        reason="second synthetic Scout candidate",
    )
    session_map = SessionMap(
        created_at=datetime.now(UTC),
        producer_version=__version__,
        canonicalization_version="gemini-boundary-batch-test-v1",
        session_id=ingest.session_id,
        source_id=ingest.source.source_id,
        duration_ms=ingest.source.duration_ms,
        candidates=[first_candidate, second_candidate],
        scout_backend="fake",
    )
    first_transport = FakeGeminiTransport(
        response=_provider_response(
            {
                "status": "REFINED",
                "event_start_ms": 800,
                "event_end_ms": 2_200,
                "confidence": 0.9,
                "reason": "same event with wider provider-refined boundaries",
            },
            interaction_id="fake-gemini-boundary-1",
        )
    )
    second_transport = FakeGeminiTransport(
        response=_provider_response(
            {
                "status": "UNCERTAIN",
                "event_start_ms": 2_200,
                "event_end_ms": 3_600,
                "confidence": 0.3,
                "reason": "synthetic provider boundary remains uncertain",
            },
            interaction_id="fake-gemini-boundary-2",
        )
    )
    transports = {
        first_candidate.candidate_id: first_transport,
        second_candidate.candidate_id: second_transport,
    }
    service = _service(config)

    first = run_gemini_boundary_refinement_batch_with_transports(
        ingest.source,
        proxy,
        session_map,
        config,
        candidate_ids=(first_candidate.candidate_id, second_candidate.candidate_id),
        transports=transports,
        cost_service=service,
    )

    assert first.generated_responses == 2
    assert first.response_cache_hits == 0
    assert first.preflight.total_reserved_cost_micro_thb > 0
    assert first.artifact.backend == "gemini"
    assert first.artifact.execution_mode == "injected_transport"
    assert first.artifact.selected_candidate_ids == (
        first_candidate.candidate_id,
        second_candidate.candidate_id,
    )
    assert first_transport.upload_count == 1
    assert first_transport.generation_count == 1
    assert first_transport.delete_count == 1
    assert second_transport.upload_count == 1
    assert second_transport.generation_count == 1
    assert second_transport.delete_count == 1
    assert all(call.status.value == "SETTLED" for call in service.calls())
    assert len(service.calls()) == 2

    refined = {candidate.candidate_id: candidate for candidate in first.session_map.candidates}
    assert (
        refined[first_candidate.candidate_id].event_start_ms,
        refined[first_candidate.candidate_id].event_end_ms,
    ) == (400, 1_100)
    assert (
        refined[second_candidate.candidate_id].event_start_ms,
        refined[second_candidate.candidate_id].event_end_ms,
    ) == (1_200, 1_700)
    assert all(candidate.clip_start_ms is not None for candidate in first.session_map.candidates)
    assert all(candidate.clip_end_ms is not None for candidate in first.session_map.candidates)
    assert first.session_map.scout_metadata["boundary_refinement_backend"] == "gemini"
    assert read_json(first.artifact_path) == model_json(first.artifact)
    assert read_json(first.refined_session_map_path) == model_json(first.session_map)
    assert session_map.scout_metadata == {}
    assert all(candidate.clip_start_ms is None for candidate in session_map.candidates)
    assert hash_file(tiny_video, source=True) == source_before

    second = run_gemini_boundary_refinement_batch_with_transports(
        ingest.source,
        proxy,
        session_map,
        config,
        candidate_ids=(first_candidate.candidate_id, second_candidate.candidate_id),
        transports=transports,
        cost_service=service,
    )

    assert second.generated_responses == 0
    assert second.media_cache_hits == 2
    assert second.response_cache_hits == 2
    assert second.session_map == first.session_map
    assert all(item.cache_hit for item in second.artifact.items)
    assert first_transport.upload_count == 1
    assert first_transport.generation_count == 1
    assert second_transport.upload_count == 1
    assert second_transport.generation_count == 1
    assert len(service.calls()) == 2
    assert hash_file(tiny_video, source=True) == source_before
