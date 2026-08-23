import json
from pathlib import Path

import pytest

from game_highlight_finder.pipeline.gemini_contract import (
    gemini_scout_schema,
    gemini_window_scout_schema,
)
from game_highlight_finder.pipeline.gemini_scout import estimate_gemini_usage
from game_highlight_finder.providers.base import ProviderRequest, ProviderUsageEstimate
from game_highlight_finder.providers.gemini import (
    FakeGeminiTransport,
    GeminiDispatchError,
    GeminiProvider,
    GenAITransport,
    diagnose_gemini_exception,
)
from game_highlight_finder.providers.gemini_capabilities import (
    resolve_gemini_media_resolution,
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
    for model in ("gemini-2.5-flash-lite", "gemini-3.5-flash-lite", "gemini-3.7-flash"):
        resolved = resolve_gemini_thinking_config(model, "low", 512)
        assert resolved.wire_level == "low"
        assert resolved.effective_mode == "low"
        assert resolved.reserved_thinking_tokens == 512


def test_gemini_37_rejects_minimal_thinking_and_preserves_supported_levels() -> None:
    with pytest.raises(ValueError, match="does not support thinking_level='minimal'"):
        resolve_gemini_thinking_config("gemini-3.7-flash", "minimal")

    for level in ("low", "medium", "high"):
        resolved = resolve_gemini_thinking_config("gemini-3.7-flash", level, 512)
        assert resolved.wire_level == level
        assert resolved.effective_mode == level


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


def test_media_resolution_is_model_aware_for_interactions_api() -> None:
    model_25 = resolve_gemini_media_resolution("gemini-2.5-flash-lite", "low")
    model_35 = resolve_gemini_media_resolution("gemini-3.5-flash-lite", "low")
    assert model_25.wire_level is None
    assert model_25.effective_mode == "default_unspecified"
    assert model_25.estimated_video_tokens_per_second == 258
    assert model_35.wire_level == "low"
    assert model_35.effective_mode == "low"
    assert model_35.estimated_video_tokens_per_second == 70
    high_25 = resolve_gemini_media_resolution("gemini-2.5-flash-lite", "high")
    high_35 = resolve_gemini_media_resolution("gemini-3.5-flash-lite", "high")
    assert high_25.wire_level is None
    assert high_25.effective_mode == "default_unspecified"
    assert high_25.estimated_video_tokens_per_second == 258
    assert high_35.wire_level == "high"
    assert high_35.effective_mode == "high"
    assert high_35.estimated_video_tokens_per_second == 280
    low_37 = resolve_gemini_media_resolution("gemini-3.7-flash", "low")
    high_37 = resolve_gemini_media_resolution("gemini-3.7-flash", "high")
    assert low_37.wire_level == "low"
    assert low_37.estimated_video_tokens_per_second == 70
    assert high_37.wire_level == "high"
    assert high_37.estimated_video_tokens_per_second == 280


@pytest.mark.parametrize(
    ("model", "expected_resolution"),
    [
        ("gemini-2.5-flash-lite", None),
        ("gemini-3.5-flash-lite", "low"),
    ],
)
def test_genai_transport_emits_resolution_only_for_gemini3(
    model: str, expected_resolution: str | None
) -> None:
    class Interactions:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] | None = None

        def create(self, **kwargs: object) -> dict[str, object]:
            self.kwargs = kwargs
            return {"status": "completed"}

    class Client:
        def __init__(self) -> None:
            self.interactions = Interactions()

    client = Client()
    transport = object.__new__(GenAITransport)
    transport._client = client  # type: ignore[attr-defined]
    transport.create_interaction(
        model=model,
        remote_uri="https://example.invalid/file",
        prompt="x",
        response_schema={"type": "object"},
        media_resolution="low",
        max_output_tokens=10,
        thinking_level=None,
        store=False,
    )
    assert client.interactions.kwargs is not None
    inputs = client.interactions.kwargs["input"]
    assert isinstance(inputs, list)
    video = inputs[0]
    assert isinstance(video, dict)
    if expected_resolution is None:
        assert "resolution" not in video
    else:
        assert video["resolution"] == expected_resolution


def test_genai_transport_emits_high_resolution_for_gemini3() -> None:
    class Interactions:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] | None = None

        def create(self, **kwargs: object) -> dict[str, object]:
            self.kwargs = kwargs
            return {"status": "completed"}

    class Client:
        def __init__(self) -> None:
            self.interactions = Interactions()

    client = Client()
    transport = object.__new__(GenAITransport)
    transport._client = client  # type: ignore[attr-defined]
    transport.create_interaction(
        model="gemini-3.5-flash-lite",
        remote_uri="https://example.invalid/file",
        prompt="x",
        response_schema={"type": "object"},
        media_resolution="high",
        max_output_tokens=10,
        thinking_level=None,
        store=False,
    )
    assert client.interactions.kwargs is not None
    inputs = client.interactions.kwargs["input"]
    assert isinstance(inputs, list)
    video = inputs[0]
    assert isinstance(video, dict)
    assert video["resolution"] == "high"


def test_usage_estimate_reflects_model_compatible_video_resolution() -> None:
    common = {
        "duration_ms": 10_000,
        "prompt": "x",
        "response_schema": {"type": "object"},
        "audio_present": False,
        "max_output_tokens": 10,
        "reserved_thinking_tokens": 0,
        "media_resolution": "low",
    }
    model_25 = estimate_gemini_usage(model="gemini-2.5-flash-lite", **common)
    model_35 = estimate_gemini_usage(model="gemini-3.5-flash-lite", **common)
    assert model_25.input_video_tokens == 2_580
    assert model_35.input_video_tokens == 700
    high_35 = estimate_gemini_usage(
        model="gemini-3.5-flash-lite",
        **{**common, "media_resolution": "high"},
    )
    assert high_35.input_video_tokens == 2_800


def test_fake_transport_mirrors_model_specific_resolution_wire_shape(tmp_path: Path) -> None:
    proxy_root = tmp_path / "proxy"
    proxy_root.mkdir()
    proxy_path = proxy_root / "analysis_proxy.mp4"
    proxy_path.write_bytes(b"proxy")
    for model, expected in (
        ("gemini-2.5-flash-lite", None),
        ("gemini-3.5-flash-lite", "low"),
    ):
        transport = FakeGeminiTransport()
        request = ProviderRequest(
            call_id=f"media-{model}",
            provider="gemini",
            model_id=model,
            billing_mode="standard",
            stage="scout",
            usage_estimate=ProviderUsageEstimate(input_video_tokens=1, output_tokens=1),
            request_payload={},
        )
        GeminiProvider(transport=transport).execute(
            request,
            proxy_path=proxy_path,
            session_proxy_root=proxy_root,
            prompt="x",
            response_schema={"type": "object"},
            media_resolution="low",
            max_output_tokens=10,
            thinking_level=None,
        )
        assert transport.last_request is not None
        video = transport.last_request["input"][0]
        if expected is None:
            assert "resolution" not in video
        else:
            assert video["resolution"] == expected


def test_window_scout_schema_omits_optional_context_timestamps_only_at_provider_boundary() -> None:
    base = gemini_scout_schema()
    window = gemini_window_scout_schema()

    base_top = base["properties"]["candidates"]["items"]["properties"]
    window_top = window["properties"]["candidates"]["items"]["properties"]
    base_nested = base["properties"]["matches"]["items"]["properties"]["candidates"]["items"][
        "properties"
    ]
    window_nested = window["properties"]["matches"]["items"]["properties"]["candidates"]["items"][
        "properties"
    ]

    for field in ("setup_start_ms", "payoff_end_ms"):
        assert field in base_top
        assert field in base_nested
        assert field not in window_top
        assert field not in window_nested


def test_window_scout_schema_stays_within_small_supported_subset() -> None:
    allowed = {"type", "properties", "required", "items", "enum"}
    structural_keys: set[str] = set()

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in allowed:
                    structural_keys.add(key)
                elif key.startswith("$") or key in {
                    "anyOf",
                    "oneOf",
                    "allOf",
                    "default",
                    "examples",
                    "const",
                    "pattern",
                    "exclusiveMinimum",
                    "exclusiveMaximum",
                    "unevaluatedProperties",
                }:
                    raise AssertionError(f"unsupported schema keyword: {key}")
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(gemini_window_scout_schema())
    assert structural_keys == allowed
