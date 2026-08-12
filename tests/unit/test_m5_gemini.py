from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from game_highlight_finder.config import (
    AppConfig,
    CostConfig,
    ScoutConfig,
    StorageConfig,
    ToolsConfig,
    config_payload,
)
from game_highlight_finder.cost.fx import FxSnapshot
from game_highlight_finder.cost.production import production_pricing_catalog
from game_highlight_finder.cost.service import CostService
from game_highlight_finder.errors import CostGateError, ValidationError
from game_highlight_finder.pipeline.gemini_contract import (
    build_gemini_prompt,
    gemini_scout_schema,
    schema_hash,
)
from game_highlight_finder.pipeline.gemini_scout import (
    build_gemini_registry,
    estimate_gemini_usage,
    generate_gemini_scout,
    preflight_gemini_scout,
)
from game_highlight_finder.pipeline.ingest import ingest_source
from game_highlight_finder.pipeline.local_signals import generate_local_signals
from game_highlight_finder.pipeline.proxy import generate_proxy
from game_highlight_finder.providers.base import ProviderUsageEstimate
from game_highlight_finder.providers.gemini import (
    FakeGeminiTransport,
    GeminiInteractionEnvelope,
    GeminiMissingUsageError,
    GeminiPrivacyError,
    GeminiProvider,
    GeminiProviderError,
    sanitize_interaction_response,
    usage_from_envelope,
    validate_proxy_upload,
)

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def _config(tmp_path: Path, ffmpeg_path: Path, ffprobe_path: Path) -> AppConfig:
    fx_path = tmp_path / "fx.json"
    fx_path.write_text(
        json.dumps(
            {
                "base_currency": "USD",
                "quote_currency": "THB",
                "rate": "36",
                "captured_at": NOW.isoformat(),
                "source": "test",
            }
        ),
        encoding="utf-8",
    )
    return AppConfig(
        storage=StorageConfig(data_dir=tmp_path / "library"),
        tools=ToolsConfig(ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path),
        scout=ScoutConfig(
            backend="gemini",
            allow_remote_upload=True,
            max_duration_seconds=900,
        ),
        cost=CostConfig(fx_snapshot_path=fx_path),
    )


def _service(config: AppConfig) -> CostService:
    return CostService(
        config,
        registry=build_gemini_registry(),
        pricing=production_pricing_catalog(),
        fx_snapshot=FxSnapshot(
            base_currency="USD",
            quote_currency="THB",
            rate=Decimal("36"),
            captured_at=NOW,
            source="test",
        ),
    )


def _response(duration_ms: int) -> dict[str, object]:
    return {
        "status": "completed",
        "id": "interaction-test-1",
        "output_text": json.dumps(
            {
                "schema_version": 1,
                "source_duration_ms": duration_ms,
                "time_basis": "source_relative",
                "matches": [],
                "candidates": [],
                "warnings": [],
                "metadata": {"backend": "gemini"},
            }
        ),
        "usage": {
            "prompt_token_count": 1_000,
            "candidates_token_count": 20,
            "thoughts_token_count": 5,
        },
    }


def test_prompt_and_schema_are_deterministic_and_non_quota() -> None:
    prompt = build_gemini_prompt(duration_ms=10_000, local_signal_summary={"active": 1})
    assert "quota" not in prompt.lower()
    assert prompt == build_gemini_prompt(duration_ms=10_000, local_signal_summary={"active": 1})
    assert schema_hash() == schema_hash()
    assert gemini_scout_schema()["required"]
    usage = estimate_gemini_usage(
        duration_ms=10_000,
        prompt=prompt,
        response_schema=gemini_scout_schema(),
        audio_present=True,
        max_output_tokens=100,
        max_thinking_tokens=50,
    )
    assert usage.input_video_tokens == 660
    assert usage.input_audio_tokens == 320
    assert usage.billable_output_tokens == 150


def test_gemini_defaults_are_fake_and_secret_safe() -> None:
    config = AppConfig()
    assert config.scout.backend == "fake"
    assert config.scout.model == "gemini-3.5-flash-lite"
    redacted = config_payload(config)
    assert "GEMINI_API_KEY" not in json.dumps(redacted)
    assert config_payload(config, redacted=False)["scout"]["api_key_env"] == "GEMINI_API_KEY"
    with pytest.raises(PydanticValidationError):
        ScoutConfig(backend="unsupported")  # type: ignore[arg-type]


def test_exact_gemini_pricing_is_versioned_and_freshness_is_fail_closed() -> None:
    catalog = production_pricing_catalog()
    entry = catalog.lookup("gemini", "gemini-3.5-flash-lite", "standard", now=NOW, max_age_days=30)
    assert entry.input_rates_by_modality == {
        "text": Decimal("0.30"),
        "image": Decimal("0.30"),
        "video": Decimal("0.30"),
        "audio": Decimal("0.30"),
    }
    assert entry.output_rate == Decimal("2.50")
    with pytest.raises(CostGateError):
        catalog.lookup(
            "gemini",
            "gemini-3.5-flash-lite",
            "standard",
            now=NOW + timedelta(days=31),
            max_age_days=30,
        )


def test_usage_mapping_requires_authoritative_counts_and_bounds_thinking() -> None:
    envelope = GeminiInteractionEnvelope(
        status="completed",
        usage={
            "prompt_token_count": 10,
            "video_token_count": 20,
            "audio_token_count": 30,
            "candidates_token_count": 40,
            "thoughts_token_count": 5,
        },
    )
    actual = usage_from_envelope(envelope)
    assert actual.input_video_tokens == 20
    assert actual.input_audio_tokens == 30
    assert actual.billable_output_tokens == 45
    with pytest.raises(GeminiMissingUsageError):
        usage_from_envelope(GeminiInteractionEnvelope(status="completed"))
    with pytest.raises(GeminiMissingUsageError):
        usage_from_envelope(
            GeminiInteractionEnvelope(
                status="completed",
                usage={"candidates_token_count": 10_000_000, "thoughts_token_count": 1},
            )
        )


def test_sanitized_response_excludes_thought_steps() -> None:
    envelope = sanitize_interaction_response(
        {
            "id": "interaction-safe",
            "status": "completed",
            "output_text": "{}",
            "usage": {"prompt_token_count": 1, "candidates_token_count": 1},
            "thoughts": [{"text": "private reasoning"}],
            "steps": [{"type": "thought", "text": "private reasoning"}],
        },
        model="gemini-3.5-flash-lite",
        remote_file_name="files/proxy",
        max_bytes=1024,
    )
    serialized = json.dumps(envelope.model_dump(mode="json"), sort_keys=True)
    assert "private reasoning" not in serialized
    assert "thoughts" not in serialized
    assert envelope.remote_file_name == "files/proxy"


def test_raw_source_is_rejected_by_privacy_boundary(tmp_path: Path) -> None:
    raw = tmp_path / "source.mp4"
    raw.write_bytes(b"raw")
    with pytest.raises(GeminiPrivacyError):
        validate_proxy_upload(raw, tmp_path / "proxy")


def test_provider_privacy_rejection_precedes_api_key_initialization(tmp_path: Path) -> None:
    raw = tmp_path / "source.mp4"
    raw.write_bytes(b"raw")
    from game_highlight_finder.providers.base import ProviderRequest

    request = ProviderRequest(
        call_id="privacy-call",
        provider="gemini",
        model_id="gemini-3.5-flash-lite",
        billing_mode="standard",
        stage="scout",
        usage_estimate=ProviderUsageEstimate(input_text_tokens=1),
        request_payload={"prompt": "x", "response_schema": {"type": "object"}},
    )
    with pytest.raises(GeminiPrivacyError):
        GeminiProvider().execute(request, proxy_path=raw, session_proxy_root=tmp_path / "proxy")


def test_gemini_fake_transport_lifecycle_cache_and_cleanup(
    tiny_video: Path, ffmpeg_path: Path, ffprobe_path: Path, tmp_path: Path
) -> None:
    config = _config(tmp_path, ffmpeg_path, ffprobe_path)
    ingest = ingest_source(tiny_video, config)
    proxy = generate_proxy(ingest.source, config)
    signals = generate_local_signals(ingest.source, proxy, config)
    transport = FakeGeminiTransport(response=_response(ingest.source.duration_ms))
    service = _service(config)
    first = generate_gemini_scout(
        ingest.source,
        proxy,
        signals,
        config,
        transport=transport,
        cost_service=service,
    )
    assert first.backend == "gemini"
    assert transport.upload_count == 1
    assert transport.generation_count == 1
    assert transport.delete_count == 1
    second = generate_gemini_scout(
        ingest.source,
        proxy,
        signals,
        config,
        transport=transport,
        cost_service=service,
    )
    assert second.cache_hit is True
    assert transport.upload_count == 1
    assert transport.generation_count == 1


def test_gemini_preflight_requires_fx_and_makes_no_transport_calls(
    tiny_video: Path, ffmpeg_path: Path, ffprobe_path: Path, tmp_path: Path
) -> None:
    config = _config(tmp_path, ffmpeg_path, ffprobe_path)
    ingest = ingest_source(tiny_video, config)
    proxy = generate_proxy(ingest.source, config)
    signals = generate_local_signals(ingest.source, proxy, config)
    service = _service(config)
    result = preflight_gemini_scout(
        ingest.source,
        proxy,
        signals,
        config,
        cost_service=service,
    )
    assert result.proxy_only is True
    assert result.quote.reserved_cost_micro_thb >= result.quote.base_cost_micro_thb
    assert service.ledger.list_calls() == ()


def test_upload_failure_releases_reservation_before_any_generation(
    tiny_video: Path, ffmpeg_path: Path, ffprobe_path: Path, tmp_path: Path
) -> None:
    config = _config(tmp_path, ffmpeg_path, ffprobe_path)
    ingest = ingest_source(tiny_video, config)
    proxy = generate_proxy(ingest.source, config)
    signals = generate_local_signals(ingest.source, proxy, config)
    service = _service(config)
    transport = FakeGeminiTransport(upload_error=GeminiProviderError("upload failed"))
    with pytest.raises(ValidationError):
        generate_gemini_scout(
            ingest.source,
            proxy,
            signals,
            config,
            transport=transport,
            cost_service=service,
        )
    calls = service.ledger.list_calls()
    assert len(calls) == 1
    assert calls[0].status.value == "RELEASED"
    assert transport.generation_count == 0


def test_missing_usage_is_ambiguous_and_not_retried(
    tiny_video: Path, ffmpeg_path: Path, ffprobe_path: Path, tmp_path: Path
) -> None:
    config = _config(tmp_path, ffmpeg_path, ffprobe_path)
    ingest = ingest_source(tiny_video, config)
    proxy = generate_proxy(ingest.source, config)
    signals = generate_local_signals(ingest.source, proxy, config)
    transport = FakeGeminiTransport(
        response={
            "status": "completed",
            "id": "missing-usage",
            "output_text": json.dumps(
                {
                    "schema_version": 1,
                    "source_duration_ms": ingest.source.duration_ms,
                    "time_basis": "source_relative",
                    "matches": [],
                    "candidates": [],
                    "warnings": [],
                    "metadata": {"backend": "gemini"},
                }
            ),
            "usage": {},
        }
    )
    service = _service(config)
    with pytest.raises(ValidationError):
        generate_gemini_scout(
            ingest.source,
            proxy,
            signals,
            config,
            transport=transport,
            cost_service=service,
        )
    assert service.ledger.list_calls()[0].status.value == "AMBIGUOUS"
    with pytest.raises(ValidationError):
        generate_gemini_scout(
            ingest.source,
            proxy,
            signals,
            config,
            transport=transport,
            cost_service=service,
        )
    assert transport.generation_count == 1


def test_remote_cleanup_failure_does_not_regenerate(
    tiny_video: Path, ffmpeg_path: Path, ffprobe_path: Path, tmp_path: Path
) -> None:
    config = _config(tmp_path, ffmpeg_path, ffprobe_path)
    ingest = ingest_source(tiny_video, config)
    proxy = generate_proxy(ingest.source, config)
    signals = generate_local_signals(ingest.source, proxy, config)
    transport = FakeGeminiTransport(
        response=_response(ingest.source.duration_ms),
        delete_error=GeminiProviderError("delete unavailable"),
    )
    service = _service(config)
    first = generate_gemini_scout(
        ingest.source,
        proxy,
        signals,
        config,
        transport=transport,
        cost_service=service,
    )
    remote_metadata = first.session_dir / "scout" / "raw" / "gemini_remote_file.json"
    metadata = json.loads(remote_metadata.read_text(encoding="utf-8"))
    assert metadata["deletion_status"] == "pending"
    assert "uri" not in metadata
    second = generate_gemini_scout(
        ingest.source,
        proxy,
        signals,
        config,
        transport=transport,
        cost_service=service,
    )
    assert second.cache_hit is True
    assert transport.generation_count == 1


def test_gemini_ambiguous_generation_is_not_retried(
    tiny_video: Path, ffmpeg_path: Path, ffprobe_path: Path, tmp_path: Path
) -> None:
    config = _config(tmp_path, ffmpeg_path, ffprobe_path)
    ingest = ingest_source(tiny_video, config)
    proxy = generate_proxy(ingest.source, config)
    signals = generate_local_signals(ingest.source, proxy, config)
    transport = FakeGeminiTransport(
        response=_response(ingest.source.duration_ms),
        generation_error=GeminiProviderError("timeout after dispatch", may_have_dispatched=True),
    )
    service = _service(config)
    with pytest.raises(ValidationError):
        generate_gemini_scout(
            ingest.source,
            proxy,
            signals,
            config,
            transport=transport,
            cost_service=service,
        )
    assert transport.generation_count == 1
    with pytest.raises(ValidationError):
        generate_gemini_scout(
            ingest.source,
            proxy,
            signals,
            config,
            transport=transport,
            cost_service=service,
        )
    assert transport.generation_count == 1


def test_provider_upload_rejects_raw_path(tmp_path: Path) -> None:
    raw = tmp_path / "source.mp4"
    raw.write_bytes(b"source")
    provider = GeminiProvider(transport=FakeGeminiTransport())
    request = {
        "call_id": "call",
        "provider": "gemini",
        "model_id": "gemini-3.5-flash-lite",
        "billing_mode": "standard",
        "stage": "scout",
        "usage_estimate": {"input_text_tokens": 1},
        "request_payload": {
            "proxy_path": str(raw),
            "session_proxy_root": str(tmp_path / "proxy"),
            "prompt": "x",
            "response_schema": {"type": "object"},
        },
    }
    from game_highlight_finder.providers.base import ProviderRequest

    with pytest.raises(GeminiProviderError):
        provider.execute(ProviderRequest.model_validate(request))
