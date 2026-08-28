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
from game_highlight_finder.domain.models import Candidate, SessionMap
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.pipeline.boundary_refiner_gemini_batch import (
    run_gemini_boundary_refinement_batch_with_transports,
)
from game_highlight_finder.pipeline.gemini_scout import build_gemini_registry
from game_highlight_finder.pipeline.ingest import ingest_source
from game_highlight_finder.pipeline.proxy import generate_proxy
from game_highlight_finder.providers.gemini import FakeGeminiTransport, GeminiProviderError
from game_highlight_finder.storage.atomic import read_json
from game_highlight_finder.storage.hashing import hash_file
from game_highlight_finder.storage.sessions import session_paths

pytestmark = pytest.mark.integration
NOW = datetime(2026, 8, 28, 1, 30, tzinfo=UTC)


def _response(interaction_id: str) -> dict[str, object]:
    return {
        "status": "completed",
        "id": interaction_id,
        "output_text": json.dumps(
            {
                "status": "REFINED",
                "event_start_ms": 800,
                "event_end_ms": 2_200,
                "confidence": 0.9,
                "reason": "same event with provider-refined boundaries",
            }
        ),
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
            source="partial-failure-integration",
        ),
    )


def test_gemini_boundary_batch_preserves_settled_prefix_and_never_retries_ambiguous_tail(
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
    first = Candidate(
        candidate_id="cand_1111111111111111",
        category="SKILL",
        event_start_ms=500,
        event_end_ms=1_000,
        score=8.0,
        confidence=0.9,
        reason="first synthetic candidate",
    )
    second = Candidate(
        candidate_id="cand_2222222222222222",
        category="FUNNY",
        event_start_ms=1_200,
        event_end_ms=1_700,
        score=7.0,
        confidence=0.8,
        reason="second synthetic candidate",
    )
    session_map = SessionMap(
        created_at=datetime.now(UTC),
        producer_version=__version__,
        canonicalization_version="partial-failure-test-v1",
        session_id=ingest.session_id,
        source_id=ingest.source.source_id,
        duration_ms=ingest.source.duration_ms,
        candidates=[first, second],
        scout_backend="fake",
    )
    first_transport = FakeGeminiTransport(response=_response("first-settled"))
    second_transport = FakeGeminiTransport(
        response=_response("second-unused"),
        generation_error=GeminiProviderError(
            "synthetic timeout after dispatch",
            may_have_dispatched=True,
        ),
    )
    service = _service(config)

    with pytest.raises(ValidationError, match="timeout after dispatch"):
        run_gemini_boundary_refinement_batch_with_transports(
            ingest.source,
            proxy,
            session_map,
            config,
            candidate_ids=(first.candidate_id, second.candidate_id),
            transports={first.candidate_id: first_transport, second.candidate_id: second_transport},
            cost_service=service,
        )

    calls = service.calls()
    assert [call.status.value for call in calls] == ["SETTLED", "AMBIGUOUS"]
    assert first_transport.generation_count == 1
    assert first_transport.delete_count == 1
    assert second_transport.generation_count == 1
    paths = session_paths(config.storage.data_dir, ingest.session_id)
    refinement_dir = paths.scout_dir / "boundary_refinement"
    first_dir = refinement_dir / first.candidate_id
    second_dir = refinement_dir / second.candidate_id
    assert (first_dir / "response.gemini.json").is_file()
    assert read_json(first_dir / "cost.gemini.json")["state"] == "SETTLED"
    assert read_json(second_dir / "cost.gemini.json")["state"] == "AMBIGUOUS"
    assert not (refinement_dir / "batch.gemini.json").exists()
    assert not (refinement_dir / "session_map.refined.gemini.json").exists()
    assert session_map.scout_metadata == {}
    assert hash_file(tiny_video, source=True) == source_before

    with pytest.raises(ValidationError, match="unresolved"):
        run_gemini_boundary_refinement_batch_with_transports(
            ingest.source,
            proxy,
            session_map,
            config,
            candidate_ids=(first.candidate_id, second.candidate_id),
            transports={first.candidate_id: first_transport, second.candidate_id: second_transport},
            cost_service=service,
        )

    assert first_transport.generation_count == 1
    assert second_transport.generation_count == 1
    assert [call.status.value for call in service.calls()] == ["SETTLED", "AMBIGUOUS"]
    assert not (refinement_dir / "batch.gemini.json").exists()
    assert not (refinement_dir / "session_map.refined.gemini.json").exists()
