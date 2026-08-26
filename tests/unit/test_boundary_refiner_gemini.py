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
from game_highlight_finder.domain.models import Candidate, model_json
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.pipeline.boundary_refinement import (
    BoundaryRefinementMediaArtifact,
    BoundaryRefinementMediaResult,
    plan_boundary_refinement,
)
from game_highlight_finder.pipeline.boundary_refiner_gemini import (
    preflight_gemini_boundary_refinement,
    run_gemini_boundary_refinement_with_transport,
)
from game_highlight_finder.pipeline.gemini_scout import build_gemini_registry
from game_highlight_finder.providers.gemini import FakeGeminiTransport, GeminiProviderError
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.hashing import hash_file

NOW = datetime(2026, 8, 26, 4, 0, tzinfo=UTC)
SESSION_ID = "2026-08-26_unknown_111111111111"
OTHER_SESSION_ID = "2026-08-26_unknown_222222222222"


def _candidate() -> Candidate:
    return Candidate(
        candidate_id="cand_0123456789abcdef",
        category="SKILL",
        event_start_ms=500,
        event_end_ms=1_200,
        score=7.0,
        confidence=0.9,
        reason="Scout anchor for provider-boundary test",
    )


def _media(
    tmp_path: Path,
    candidate: Candidate,
    *,
    session_id: str = SESSION_ID,
) -> BoundaryRefinementMediaResult:
    item_dir = (
        tmp_path
        / "library"
        / "sessions"
        / session_id
        / "scout"
        / "boundary_refinement"
        / candidate.candidate_id
    )
    item_dir.mkdir(parents=True)
    context_path = item_dir / "context.mp4"
    slowed_path = item_dir / "slowed.mp4"
    artifact_path = item_dir / "artifact.json"
    context_path.write_bytes(b"context-provider-test")
    slowed_path.write_bytes(b"slowed-provider-test")
    plan = plan_boundary_refinement(
        candidate,
        2_000,
        pre_context_ms=250,
        post_context_ms=300,
        slowdown_factor=2,
    )
    artifact = BoundaryRefinementMediaArtifact(
        plan=plan,
        parent_proxy_sha256="a" * 64,
        context_proxy_path="scout/boundary_refinement/cand_0123456789abcdef/context.mp4",
        context_proxy_sha256=hash_file(context_path),
        slowed_proxy_path="scout/boundary_refinement/cand_0123456789abcdef/slowed.mp4",
        slowed_proxy_sha256=hash_file(slowed_path),
        encoder="libx264",
        hardware_accelerated=False,
        audio_present=True,
        context_duration_ms=1_250,
        slowed_proxy_duration_ms=2_500,
    )
    atomic_write_json(artifact_path, model_json(artifact))
    return BoundaryRefinementMediaResult(
        cache_hit=False,
        artifact_path=artifact_path,
        context_path=context_path,
        slowed_proxy_path=slowed_path,
        artifact=artifact,
    )


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        storage=StorageConfig(data_dir=tmp_path / "library"),
        scout=ScoutConfig(
            backend="gemini",
            model="gemini-3.7-flash",
            allow_remote_upload=True,
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
        "id": "fake-boundary-interaction-1",
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


def test_boundary_refiner_preflight_quotes_without_reservation_or_transport(tmp_path: Path) -> None:
    candidate = _candidate()
    media = _media(tmp_path, candidate)
    config = _config(tmp_path)
    service = _service(config)

    result = preflight_gemini_boundary_refinement(
        media,
        candidate,
        config,
        session_id=SESSION_ID,
        cost_service=service,
    )

    assert result.provider == "gemini"
    assert result.model == "gemini-3.7-flash"
    assert result.session_id == SESSION_ID
    assert result.media_sha256 == media.artifact.slowed_proxy_sha256
    assert result.usage_estimate.input_video_tokens > 0
    assert result.quote.reserved_cost_micro_thb > 0
    assert service.calls() == ()


def test_session_identity_changes_provider_request_fingerprint(tmp_path: Path) -> None:
    candidate = _candidate()
    config = _config(tmp_path)
    service = _service(config)
    first_media = _media(tmp_path, candidate, session_id=SESSION_ID)
    second_media = _media(tmp_path, candidate, session_id=OTHER_SESSION_ID)

    first = preflight_gemini_boundary_refinement(
        first_media,
        candidate,
        config,
        session_id=SESSION_ID,
        cost_service=service,
    )
    second = preflight_gemini_boundary_refinement(
        second_media,
        candidate,
        config,
        session_id=OTHER_SESSION_ID,
        cost_service=service,
    )

    assert first.provider_request_fingerprint != second.provider_request_fingerprint


def test_mismatched_session_media_is_rejected_before_reservation(tmp_path: Path) -> None:
    candidate = _candidate()
    media = _media(tmp_path, candidate)
    config = _config(tmp_path)
    service = _service(config)
    transport = FakeGeminiTransport(response=_provider_response())

    with pytest.raises(ValidationError, match="requested session"):
        run_gemini_boundary_refinement_with_transport(
            media,
            candidate,
            config,
            session_id=OTHER_SESSION_ID,
            transport=transport,
            cost_service=service,
        )

    assert service.calls() == ()
    assert transport.upload_count == 0


def test_noncanonical_session_id_is_rejected(tmp_path: Path) -> None:
    candidate = _candidate()
    media = _media(tmp_path, candidate)
    config = _config(tmp_path)
    service = _service(config)

    with pytest.raises(ValidationError, match="normalized"):
        preflight_gemini_boundary_refinement(
            media,
            candidate,
            config,
            session_id=f" {SESSION_ID}",
            cost_service=service,
        )

    assert service.calls() == ()


def test_injected_fake_transport_lifecycle_settles_and_cache_reuses(tmp_path: Path) -> None:
    candidate = _candidate()
    media = _media(tmp_path, candidate)
    config = _config(tmp_path)
    service = _service(config)
    transport = FakeGeminiTransport(response=_provider_response())

    first = run_gemini_boundary_refinement_with_transport(
        media,
        candidate,
        config,
        session_id=SESSION_ID,
        transport=transport,
        cost_service=service,
    )

    assert first.cache_hit is False
    assert first.session_id == SESSION_ID
    assert transport.upload_count == 1
    assert transport.generation_count == 1
    assert transport.delete_count == 1
    assert transport.uploaded_paths == [media.slowed_proxy_path]
    assert (first.candidate.event_start_ms, first.candidate.event_end_ms) == (450, 1_300)
    assert read_json(first.request_meta_path)["execution_mode"] == "injected_transport"
    assert read_json(first.request_meta_path)["session_id"] == SESSION_ID
    assert read_json(first.response_path)["backend"] == "gemini"
    assert read_json(first.cost_path)["state"] == "SETTLED"
    assert read_json(first.remote_metadata_path)["deletion_status"] == "deleted"

    second = run_gemini_boundary_refinement_with_transport(
        media,
        candidate,
        config,
        session_id=SESSION_ID,
        transport=transport,
        cost_service=service,
    )
    assert second.cache_hit is True
    assert second.candidate == first.candidate
    assert transport.upload_count == 1
    assert transport.generation_count == 1
    assert len(service.calls()) == 1


def test_tampered_slowed_proxy_fails_before_reservation_or_transport(tmp_path: Path) -> None:
    candidate = _candidate()
    media = _media(tmp_path, candidate)
    config = _config(tmp_path)
    service = _service(config)
    transport = FakeGeminiTransport(response=_provider_response())
    media.slowed_proxy_path.write_bytes(b"tampered")

    with pytest.raises(ValidationError, match="hash"):
        run_gemini_boundary_refinement_with_transport(
            media,
            candidate,
            config,
            session_id=SESSION_ID,
            transport=transport,
            cost_service=service,
        )

    assert service.calls() == ()
    assert transport.upload_count == 0
    assert transport.generation_count == 0


def test_upload_failure_releases_reservation_without_generation_retry(tmp_path: Path) -> None:
    candidate = _candidate()
    media = _media(tmp_path, candidate)
    config = _config(tmp_path)
    service = _service(config)
    transport = FakeGeminiTransport(
        response=_provider_response(),
        upload_error=GeminiProviderError("synthetic upload failure", may_have_dispatched=False),
    )

    with pytest.raises(ValidationError, match="upload failure"):
        run_gemini_boundary_refinement_with_transport(
            media,
            candidate,
            config,
            session_id=SESSION_ID,
            transport=transport,
            cost_service=service,
        )

    calls = service.calls()
    assert len(calls) == 1
    assert calls[0].session_id == SESSION_ID
    assert calls[0].status.value == "RELEASED"
    assert transport.upload_count == 1
    assert transport.generation_count == 0


def test_ambiguous_generation_is_persisted_and_not_retried(tmp_path: Path) -> None:
    candidate = _candidate()
    media = _media(tmp_path, candidate)
    config = _config(tmp_path)
    service = _service(config)
    transport = FakeGeminiTransport(
        response=_provider_response(),
        generation_error=GeminiProviderError(
            "synthetic timeout after dispatch",
            may_have_dispatched=True,
        ),
    )

    with pytest.raises(ValidationError, match="timeout after dispatch"):
        run_gemini_boundary_refinement_with_transport(
            media,
            candidate,
            config,
            session_id=SESSION_ID,
            transport=transport,
            cost_service=service,
        )

    calls = service.calls()
    assert len(calls) == 1
    assert calls[0].session_id == SESSION_ID
    assert calls[0].status.value == "AMBIGUOUS"
    assert transport.generation_count == 1
    assert read_json(media.artifact_path.parent / "cost.gemini.json")["state"] == "AMBIGUOUS"

    with pytest.raises(ValidationError, match="unresolved"):
        run_gemini_boundary_refinement_with_transport(
            media,
            candidate,
            config,
            session_id=SESSION_ID,
            transport=transport,
            cost_service=service,
        )
    assert transport.generation_count == 1
