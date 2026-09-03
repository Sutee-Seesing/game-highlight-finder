"""OpenRouter transport for the bounded multimodal gameplay bake-off.

The transport deliberately uses one stdlib HTTP POST with no client-side retry.
Local/private MP4 clips are encoded as ``data:video/mp4;base64,...`` and each
model profile pins its exact upstream provider, price ceiling, and response
format. The pipeline owns cost reservation, settlement, and semantic validation.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from game_highlight_finder.openrouter_models import (
    GLM_5V_TURBO,
    OPENROUTER_ROUND_A_PROFILES,
    get_openrouter_model_profile,
)
from game_highlight_finder.providers.base import (
    ProviderCapabilities,
    ProviderDescriptor,
    ProviderModel,
    ProviderUsageActual,
)

OPENROUTER_PROVIDER = "openrouter"
# Backward-compatible aliases for the first comparator profile.
OPENROUTER_GLM_5V_TURBO_MODEL_ID = GLM_5V_TURBO.model_id
OPENROUTER_UPSTREAM_PROVIDER_SLUG = GLM_5V_TURBO.upstream_provider_slug
OPENROUTER_API_SURFACE = "chat_completions"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_HTTP_ATTEMPTS = 1
OPENROUTER_DEFAULT_API_KEY_ENV = "OPENROUTER_API_KEY"
OPENROUTER_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
OPENROUTER_DEFAULT_MAX_REQUEST_BYTES = 16 * 1024 * 1024


class OpenRouterProviderError(Exception):
    """OpenRouter comparator failure with an explicit dispatch boundary."""

    def __init__(
        self,
        message: str,
        *,
        may_have_dispatched: bool = False,
        provider_request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.may_have_dispatched = may_have_dispatched
        self.provider_request_id = provider_request_id


class OpenRouterConfigurationError(OpenRouterProviderError):
    pass


class OpenRouterCompletionEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["completed"] = "completed"
    id: str = Field(min_length=1, max_length=256)
    model: str = Field(min_length=1, max_length=256)
    output_text: str = Field(min_length=1, max_length=1_048_576)
    finish_reason: str | None = Field(default=None, max_length=64)
    usage: ProviderUsageActual
    router_attempt_count: int | None = Field(default=None, ge=0, le=64)
    selected_provider: str | None = Field(default=None, max_length=128)
    reported_cost_usd: float | None = Field(default=None, ge=0)


class OpenRouterTransport(Protocol):
    api_surface: str
    http_retry_attempts: int
    media_transport_verified: bool

    def generate(
        self,
        *,
        media_path: Path,
        prompt: str,
        response_schema: Mapping[str, Any],
        model: str,
        max_output_tokens: int,
        thinking_mode: str,
        before_generation: Callable[[], None] | None = None,
    ) -> OpenRouterCompletionEnvelope: ...


HTTPPost = Callable[
    [str, Mapping[str, str], bytes, float], tuple[int, Mapping[str, str], bytes]
]


class OpenRouterHTTPTransport:
    """One-shot OpenRouter chat-completions transport with local MP4 base64 input."""

    api_surface = OPENROUTER_API_SURFACE
    http_retry_attempts = OPENROUTER_HTTP_ATTEMPTS
    media_transport_verified = True

    def __init__(
        self,
        *,
        api_key_env: str = OPENROUTER_DEFAULT_API_KEY_ENV,
        endpoint: str = OPENROUTER_API_URL,
        timeout_seconds: float = 180.0,
        max_request_bytes: int = OPENROUTER_DEFAULT_MAX_REQUEST_BYTES,
        http_post: HTTPPost | None = None,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 3_600:
            raise ValueError("OpenRouter timeout must be in (0, 3600] seconds")
        if max_request_bytes < 1_000_000 or max_request_bytes > 64 * 1024 * 1024:
            raise ValueError("OpenRouter max request bytes must be between 1 MB and 64 MiB")
        self.api_key_env = api_key_env
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.max_request_bytes = max_request_bytes
        self._http_post = http_post or _stdlib_http_post

    def generate(
        self,
        *,
        media_path: Path,
        prompt: str,
        response_schema: Mapping[str, Any],
        model: str,
        max_output_tokens: int,
        thinking_mode: str,
        before_generation: Callable[[], None] | None = None,
    ) -> OpenRouterCompletionEnvelope:
        try:
            profile = get_openrouter_model_profile(model)
        except ValueError as exc:
            raise OpenRouterConfigurationError(str(exc)) from exc
        if not media_path.is_file() or media_path.suffix.lower() != ".mp4":
            raise OpenRouterConfigurationError("OpenRouter comparator requires a local MP4 clip.")
        api_key = os.getenv(self.api_key_env, "").strip()
        if not api_key:
            raise OpenRouterConfigurationError(
                f"OpenRouter credential environment variable is missing: {self.api_key_env}."
            )
        if max_output_tokens <= 0:
            raise OpenRouterConfigurationError("OpenRouter max output tokens must be positive.")
        if thinking_mode not in {"enabled", "disabled"}:
            raise OpenRouterConfigurationError("Unsupported OpenRouter reasoning mode.")
        if thinking_mode == "enabled" and not profile.supports_reasoning:
            raise OpenRouterConfigurationError(
                f"OpenRouter model {model} does not support the locked reasoning contract."
            )

        try:
            media_bytes = media_path.read_bytes()
        except OSError as exc:
            raise OpenRouterConfigurationError("Cannot read OpenRouter proposal media.") from exc
        media_data_url = "data:video/mp4;base64," + base64.b64encode(media_bytes).decode("ascii")
        request_prompt = prompt
        if profile.response_format_mode == "json_schema":
            response_format: dict[str, object] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "hybrid_judge",
                    "strict": True,
                    "schema": dict(response_schema),
                },
            }
        else:
            schema_json = json.dumps(
                response_schema,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            request_prompt += (
                "\nProvider formatting contract: output one JSON object only. "
                "The required JSON Schema is: " + schema_json
            )
            response_format = {"type": "json_object"}
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": request_prompt},
                        {"type": "video_url", "video_url": {"url": media_data_url}},
                    ],
                }
            ],
            "max_tokens": max_output_tokens,
            "reasoning": {"enabled": thinking_mode == "enabled", "exclude": True},
            "response_format": response_format,
            "provider": {
                "only": [profile.upstream_provider_slug],
                "allow_fallbacks": False,
                "require_parameters": True,
                "max_price": {
                    "prompt": float(profile.max_prompt_price_per_token_usd),
                    "completion": float(profile.max_completion_price_per_token_usd),
                },
            },
            "usage": {"include": True},
            "stream": False,
        }
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(body) > self.max_request_bytes:
            raise OpenRouterConfigurationError(
                "OpenRouter encoded request exceeds the local payload-size guard."
            )
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-OpenRouter-Metadata": "enabled",
        }
        if before_generation is not None:
            before_generation()
        try:
            status, response_headers, response_body = self._http_post(
                self.endpoint,
                headers,
                body,
                self.timeout_seconds,
            )
        except OpenRouterProviderError:
            raise
        except Exception as exc:
            raise OpenRouterProviderError(
                "OpenRouter request failed after the dispatch boundary.",
                may_have_dispatched=True,
            ) from exc
        generation_id = _header(response_headers, "x-generation-id")
        if status != 200:
            detail = _safe_error_message(response_body)
            raise OpenRouterProviderError(
                f"OpenRouter HTTP {status}: {detail}",
                may_have_dispatched=True,
                provider_request_id=generation_id,
            )
        try:
            raw = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OpenRouterProviderError(
                "OpenRouter returned an invalid JSON response after dispatch.",
                may_have_dispatched=True,
                provider_request_id=generation_id,
            ) from exc
        return _parse_completion(raw, generation_id=generation_id, expected_model=model)


class FakeOpenRouterTransport:
    """Deterministic offline transport for comparator lifecycle tests."""

    api_surface = OPENROUTER_API_SURFACE
    http_retry_attempts = OPENROUTER_HTTP_ATTEMPTS
    media_transport_verified = True

    def __init__(
        self,
        *,
        response: Mapping[str, Any] | str | None = None,
        usage: ProviderUsageActual | None = None,
        generation_error: Exception | None = None,
        api_surface: str = OPENROUTER_API_SURFACE,
        http_retry_attempts: int = OPENROUTER_HTTP_ATTEMPTS,
        media_transport_verified: bool = True,
        router_attempt_count: int | None = 1,
        selected_provider: str | None = None,
    ) -> None:
        self.api_surface = api_surface
        self.http_retry_attempts = http_retry_attempts
        self.media_transport_verified = media_transport_verified
        if response is None:
            response = {
                "decision": "REJECT",
                "summary": "ordinary gameplay with no clip-worthy payoff",
                "events": [],
            }
        self.response = response
        self.usage = usage or ProviderUsageActual(
            input_text_tokens=2_500,
            output_tokens=80,
            thinking_tokens=120,
            provider_request_id="fake-openrouter-1",
        )
        self.generation_error = generation_error
        self.router_attempt_count = router_attempt_count
        self.selected_provider = selected_provider
        self.generation_count = 0
        self.generated_media: list[Path] = []

    def generate(
        self,
        *,
        media_path: Path,
        prompt: str,
        response_schema: Mapping[str, Any],
        model: str,
        max_output_tokens: int,
        thinking_mode: str,
        before_generation: Callable[[], None] | None = None,
    ) -> OpenRouterCompletionEnvelope:
        del prompt, response_schema, max_output_tokens, thinking_mode
        self.generation_count += 1
        self.generated_media.append(media_path)
        if before_generation is not None:
            before_generation()
        if self.generation_error is not None:
            raise self.generation_error
        output_text = self.response if isinstance(self.response, str) else json.dumps(self.response)
        profile = get_openrouter_model_profile(model)
        return OpenRouterCompletionEnvelope(
            id=self.usage.provider_request_id or "fake-openrouter-1",
            model=model,
            output_text=output_text,
            finish_reason="stop",
            usage=self.usage,
            router_attempt_count=self.router_attempt_count,
            selected_provider=self.selected_provider or profile.selected_provider_name,
        )


def openrouter_provider_descriptor() -> ProviderDescriptor:
    """Return the locked seven-model OpenRouter gameplay bake-off catalog."""

    provider_capabilities = ProviderCapabilities(
        video_input=True,
        audio_input=False,
        structured_output=True,
        file_upload=False,
        usage_metadata=True,
        remote_file_deletion=False,
        batch_execution=False,
        async_execution=False,
    )
    models: list[ProviderModel] = []
    for profile in OPENROUTER_ROUND_A_PROFILES:
        capabilities = ProviderCapabilities(
            video_input=True,
            audio_input=profile.model_id == "xiaomi/mimo-v2.5",
            structured_output=profile.response_format_mode == "json_schema",
            file_upload=False,
            usage_metadata=True,
            remote_file_deletion=False,
            batch_execution=False,
            async_execution=False,
        )
        models.append(
            ProviderModel(
                provider=OPENROUTER_PROVIDER,
                model_id=profile.model_id,
                billing_modes=("standard",),
                capabilities=capabilities,
            )
        )
    return ProviderDescriptor(
        provider=OPENROUTER_PROVIDER,
        display_name="OpenRouter multimodal gameplay bake-off",
        capabilities=provider_capabilities,
        models=tuple(models),
    )


def _stdlib_http_post(
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout_seconds: float,
) -> tuple[int, Mapping[str, str], bytes]:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read(OPENROUTER_MAX_RESPONSE_BYTES + 1)
            if len(payload) > OPENROUTER_MAX_RESPONSE_BYTES:
                raise OpenRouterProviderError(
                    "OpenRouter response exceeded the local response-size guard.",
                    may_have_dispatched=True,
                    provider_request_id=response.headers.get("X-Generation-Id"),
                )
            return int(response.status), dict(response.headers.items()), payload
    except urllib.error.HTTPError as exc:
        payload = exc.read(OPENROUTER_MAX_RESPONSE_BYTES + 1)
        return int(exc.code), dict(exc.headers.items()), payload[:OPENROUTER_MAX_RESPONSE_BYTES]
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OpenRouterProviderError(
            "OpenRouter network failure after dispatch.",
            may_have_dispatched=True,
        ) from exc


def _parse_completion(
    raw: object,
    *,
    generation_id: str | None,
    expected_model: str,
) -> OpenRouterCompletionEnvelope:
    if not isinstance(raw, dict):
        raise OpenRouterProviderError(
            "OpenRouter completion response must be an object.",
            may_have_dispatched=True,
            provider_request_id=generation_id,
        )
    response_id = raw.get("id")
    model = raw.get("model")
    choices = raw.get("choices")
    usage = raw.get("usage")
    if not isinstance(response_id, str) or not response_id:
        response_id = generation_id
    if not isinstance(response_id, str) or not response_id:
        raise OpenRouterProviderError(
            "OpenRouter completed response is missing a generation id.",
            may_have_dispatched=True,
        )
    if model != expected_model:
        raise OpenRouterProviderError(
            "OpenRouter completed response used an unexpected model.",
            may_have_dispatched=True,
            provider_request_id=response_id,
        )
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise OpenRouterProviderError(
            "OpenRouter completed response must contain exactly one choice.",
            may_have_dispatched=True,
            provider_request_id=response_id,
        )
    message = choices[0].get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise OpenRouterProviderError(
            "OpenRouter completed response is missing text content.",
            may_have_dispatched=True,
            provider_request_id=response_id,
        )
    actual_usage, reported_cost = _parse_usage(usage, provider_request_id=response_id)
    metadata = raw.get("openrouter_metadata")
    attempt_count: int | None = None
    selected_provider: str | None = None
    if isinstance(metadata, dict):
        attempt = metadata.get("attempt")
        if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt >= 0:
            attempt_count = attempt
        endpoints = metadata.get("endpoints")
        if isinstance(endpoints, dict):
            available = endpoints.get("available")
            if isinstance(available, list):
                selected = [
                    item
                    for item in available
                    if isinstance(item, dict) and item.get("selected") is True
                ]
                if len(selected) == 1 and isinstance(selected[0].get("provider"), str):
                    selected_provider = selected[0]["provider"]
    return OpenRouterCompletionEnvelope(
        id=response_id,
        model=model,
        output_text=message["content"],
        finish_reason=(
            choices[0].get("finish_reason")
            if isinstance(choices[0].get("finish_reason"), str)
            else None
        ),
        usage=actual_usage,
        router_attempt_count=attempt_count,
        selected_provider=selected_provider,
        reported_cost_usd=reported_cost,
    )


def _parse_usage(
    raw: object,
    *,
    provider_request_id: str,
) -> tuple[ProviderUsageActual, float | None]:
    if not isinstance(raw, dict):
        raise OpenRouterProviderError(
            "OpenRouter completed response is missing authoritative usage metadata.",
            may_have_dispatched=True,
            provider_request_id=provider_request_id,
        )
    prompt_tokens = _strict_nonnegative_int(raw.get("prompt_tokens"), "prompt_tokens")
    completion_tokens = _strict_nonnegative_int(
        raw.get("completion_tokens"), "completion_tokens"
    )
    cached_tokens = 0
    prompt_details = raw.get("prompt_tokens_details")
    if isinstance(prompt_details, dict) and prompt_details.get("cached_tokens") is not None:
        cached_tokens = _strict_nonnegative_int(
            prompt_details.get("cached_tokens"), "cached_tokens"
        )
    reasoning_tokens = 0
    completion_details = raw.get("completion_tokens_details")
    if (
        isinstance(completion_details, dict)
        and completion_details.get("reasoning_tokens") is not None
    ):
        reasoning_tokens = _strict_nonnegative_int(
            completion_details.get("reasoning_tokens"), "reasoning_tokens"
        )
    if cached_tokens > prompt_tokens:
        raise OpenRouterProviderError(
            "OpenRouter cached token count exceeds prompt token count.",
            may_have_dispatched=True,
            provider_request_id=provider_request_id,
        )
    if reasoning_tokens > completion_tokens:
        raise OpenRouterProviderError(
            "OpenRouter reasoning token count exceeds completion token count.",
            may_have_dispatched=True,
            provider_request_id=provider_request_id,
        )
    reported_cost: float | None = None
    cost = raw.get("cost")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
        reported_cost = float(cost)
    # OpenRouter reports aggregate prompt tokens rather than a text/video split.
    # GLM-5V-Turbo currently prices all uncached input tokens identically, so the
    # aggregate uncached count is stored in the text dimension for deterministic
    # settlement while cached tokens retain their explicit discounted dimension.
    return (
        ProviderUsageActual(
            input_text_tokens=prompt_tokens - cached_tokens,
            cached_input_tokens=cached_tokens,
            output_tokens=completion_tokens - reasoning_tokens,
            thinking_tokens=reasoning_tokens,
            provider_request_id=provider_request_id,
        ),
        reported_cost,
    )


def _strict_nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OpenRouterProviderError(
            f"OpenRouter usage field {field} is missing or invalid.",
            may_have_dispatched=True,
        )
    return value


def _safe_error_message(body: bytes) -> str:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "provider request failed"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            error_message = error.get("message")
            if isinstance(error_message, str):
                return error_message[:500]
        message = payload.get("message")
        if isinstance(message, str):
            return message[:500]
    return "provider request failed"


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


__all__ = [
    "OPENROUTER_API_SURFACE",
    "OPENROUTER_API_URL",
    "OPENROUTER_DEFAULT_API_KEY_ENV",
    "OPENROUTER_DEFAULT_MAX_REQUEST_BYTES",
    "OPENROUTER_GLM_5V_TURBO_MODEL_ID",
    "OPENROUTER_HTTP_ATTEMPTS",
    "OPENROUTER_PROVIDER",
    "OPENROUTER_UPSTREAM_PROVIDER_SLUG",
    "FakeOpenRouterTransport",
    "OpenRouterCompletionEnvelope",
    "OpenRouterConfigurationError",
    "OpenRouterHTTPTransport",
    "OpenRouterProviderError",
    "OpenRouterTransport",
    "openrouter_provider_descriptor",
]
