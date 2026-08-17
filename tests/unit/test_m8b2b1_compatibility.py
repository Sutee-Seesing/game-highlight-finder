import json
from pathlib import Path

import pytest

from game_highlight_finder.providers.base import ProviderRequest, ProviderUsageEstimate
from game_highlight_finder.providers.gemini import (
    FakeGeminiTransport,
    GeminiDispatchError,
    GeminiProvider,
    GenAITransport,
    diagnose_gemini_exception,
)
from game_highlight_finder.providers.gemini_capabilities import (
    resolve_gemini_thinking_config,
)


def test_model_default_minimum_thinking_is_capability_aware() -> None:
    model_25 = resolve_gemini_thinking_config("gemini-2.5-flash-lite")
    model_35 = resolve_gemini_thinking_config("gemini-3.5-flash-lite")
    assert model_25.wire_level is None
    assert model_25.effective_mode == "default_off"
    assert model_25.reserved_thinking_tokens == 0
    assert model_35.wire_level is None
    assert model_35.effective_mode == "default_minimal"
    assert model_35.reserved_thinking_tokens == 1_024
    assert model_25.payload() == resolve_gemini_thinking_config("gemini-2.5-flash-lite").payload()


def test_explicit_supported_levels_remain_model_specific() -> None:
    for model in ("gemini-2.5-flash-lite", "gemini-3.5-flash-lite"):
        resolved = resolve_gemini_thinking_config(model, "low", 512)
        assert resolved.wire_level == "low"
        assert resolved.effective_mode == "low"
        assert resolved.reserved_thinking_tokens == 512


def test_genai_transport_rejects_unsupported_wire_minimal_before_client_dispatch() -> None:
    class Client:
        class Interactions:
            def __init__(self) -> None:
                self.calls = 0

            def create(self, **_: object) -> object:
                self.calls += 1
                return None

        def __init__(self) -> None:
            self.interactions = self.Interactions()

    client = Client()
    transport = object.__new__(GenAITransport)
    transport._client = client  # type: ignore[attr-defined]
    from game_highlight_finder.providers.gemini import GeminiConfigurationError

    with pytest.raises(GeminiConfigurationError, match="omit the field"):
        transport.create_interaction(
            model="gemini-2.5-flash-lite",
            remote_uri="https://example.invalid/file",
            prompt="x",
            response_schema={"type": "object"},
            media_resolution="low",
            max_output_tokens=10,
            thinking_level="minimal",
            store=False,
        )
    assert client.interactions.calls == 0


def test_genai_transport_marks_local_sdk_validation_pre_dispatch() -> None:
    class Client:
        class Interactions:
            def create(self, **_: object) -> object:
                raise TypeError("SDK rejected generation_config before HTTP")

        interactions = Interactions()

    transport = object.__new__(GenAITransport)
    transport._client = Client()  # type: ignore[attr-defined]
    with pytest.raises(GeminiDispatchError) as caught:
        transport.create_interaction(
            model="gemini-3.5-flash-lite",
            remote_uri="https://example.invalid/file",
            prompt="x",
            response_schema={"type": "object"},
            media_resolution="low",
            max_output_tokens=10,
            thinking_level=None,
            store=False,
        )
    error = caught.value
    assert error.may_have_dispatched is False
    assert error.diagnostic is not None
    assert error.diagnostic.phase == "PRE_DISPATCH"
    assert error.diagnostic.dispatch == "NO"


def test_genai_transport_preserves_sanitized_provider_status_and_dispatch() -> None:
    class ProviderRejected(Exception):
        __module__ = "google.genai.errors"

        def __init__(self) -> None:
            super().__init__("INVALID_ARGUMENT https://private.example/signed?token=secret")
            self.status_code = 400
            self.code = "INVALID_ARGUMENT"
            self.status = "INVALID_ARGUMENT"
            self.request_id = "req-123"

    class Client:
        class Interactions:
            def create(self, **_: object) -> object:
                raise ProviderRejected()

        interactions = Interactions()

    transport = object.__new__(GenAITransport)
    transport._client = Client()  # type: ignore[attr-defined]
    with pytest.raises(GeminiDispatchError) as caught:
        transport.create_interaction(
            model="gemini-3.5-flash-lite",
            remote_uri="https://example.invalid/file",
            prompt="x",
            response_schema={"type": "object"},
            media_resolution="low",
            max_output_tokens=10,
            thinking_level=None,
            store=False,
        )
    error = caught.value
    assert error.may_have_dispatched is True
    assert error.diagnostic is not None
    assert error.diagnostic.phase == "HTTP_OR_PROVIDER"
    assert error.diagnostic.dispatch == "YES"
    assert error.diagnostic.http_status == 400
    assert error.diagnostic.provider_code == "INVALID_ARGUMENT"
    assert error.diagnostic.provider_request_id == "req-123"
    assert "private.example" not in error.diagnostic.message
    assert "secret" not in error.diagnostic.message


@pytest.mark.parametrize("model", ["gemini-2.5-flash-lite", "gemini-3.5-flash-lite"])
def test_provider_omits_default_thinking_level_for_both_models(model: str, tmp_path: Path) -> None:
    proxy_root = tmp_path / "proxy"
    proxy_root.mkdir()
    proxy_path = proxy_root / "analysis_proxy.mp4"
    proxy_path.write_bytes(b"proxy")
    transport = FakeGeminiTransport()
    request = ProviderRequest(
        call_id=f"thinking-{model}",
        provider="gemini",
        model_id=model,
        billing_mode="standard",
        stage="scout",
        usage_estimate=ProviderUsageEstimate(input_text_tokens=1, output_tokens=1),
        request_payload={},
    )
    result = GeminiProvider(transport=transport).execute(
        request,
        proxy_path=proxy_path,
        session_proxy_root=proxy_root,
        prompt="x",
        response_schema={"type": "object"},
        media_resolution="low",
        max_output_tokens=10,
        thinking_level="minimal",
    )
    assert result.provider == "gemini"
    assert transport.last_request is not None
    assert "thinking_level" not in transport.last_request["generation_config"]


def test_diagnostic_serialization_is_secret_safe() -> None:
    diagnostic = diagnose_gemini_exception(
        RuntimeError("key=AIza012345678901234567890123456789 https://example.invalid/x")
    )
    serialized = json.dumps(diagnostic.as_dict())
    assert "AIza" not in serialized
    assert "example.invalid" not in serialized
