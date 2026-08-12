"""Gemini Scout adapter with a narrow, deterministic transport seam.

The module is importable without ``google-genai``.  The SDK is loaded only by
``GenAITransport`` when an explicitly opted-in Gemini run is requested.  The
provider owns only network/file lifecycle mechanics; budgeting and canonical
validation remain in the pipeline and M4 services.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic import ValidationError as PydanticValidationError

from game_highlight_finder.providers.base import (
    MAX_USAGE_TOKENS_PER_DIMENSION,
    ProviderAdapter,
    ProviderCallResult,
    ProviderCapabilities,
    ProviderDescriptor,
    ProviderModel,
    ProviderRequest,
    ProviderUsageActual,
)
from game_highlight_finder.storage.atomic import atomic_write_json, read_json

GEMINI_PROVIDER = "gemini"
GEMINI_MODEL_ID = "gemini-3.5-flash-lite"


class GeminiProviderError(Exception):
    """Provider failure with explicit dispatch/cleanup semantics."""

    def __init__(
        self,
        message: str,
        *,
        may_have_dispatched: bool = False,
        provider_request_id: str | None = None,
        response: Mapping[str, Any] | None = None,
        remote_file: GeminiRemoteFile | None = None,
        cleanup_pending: bool = False,
    ) -> None:
        super().__init__(message)
        self.may_have_dispatched = may_have_dispatched
        self.provider_request_id = provider_request_id
        self.response = dict(response or {})
        self.remote_file = remote_file
        self.cleanup_pending = cleanup_pending


class GeminiConfigurationError(GeminiProviderError):
    pass


class GeminiPrivacyError(GeminiProviderError):
    pass


class GeminiMissingUsageError(GeminiProviderError):
    pass


class GeminiCleanupError(GeminiProviderError):
    pass


class GeminiDispatchError(GeminiProviderError):
    pass


class GeminiRemoteFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=256)
    uri: str | None = Field(default=None, max_length=2_000)
    mime_type: str = Field(default="video/mp4", min_length=1, max_length=128)
    state: str = Field(default="ACTIVE", min_length=1, max_length=64)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expiration_time: datetime | None = None
    deleted: bool = False

    def safe_metadata(self, *, deletion_status: str = "pending") -> dict[str, Any]:
        # The URI is deliberately omitted.  It is needed only in-memory for
        # the immediately-following generation call and may contain a signed
        # or otherwise sensitive resource URL.
        return {
            "name": self.name,
            "mime_type": self.mime_type,
            "state": self.state,
            "created_at": self.created_at.isoformat(),
            "expiration_time": (
                self.expiration_time.isoformat() if self.expiration_time is not None else None
            ),
            "deletion_status": deletion_status,
            "provider": GEMINI_PROVIDER,
        }


class GeminiInteractionEnvelope(BaseModel):
    """Sanitized final response; thought steps are never represented."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    interaction_id: str | None = Field(default=None, max_length=256)
    model: str = GEMINI_MODEL_ID
    status: str = Field(min_length=1, max_length=64)
    output_text: str = ""
    usage: dict[str, int] = Field(default_factory=dict)
    finish_reason: str | None = Field(default=None, max_length=256)
    safety_block_reason: str | None = Field(default=None, max_length=256)
    incomplete_reason: str | None = Field(default=None, max_length=256)
    remote_file_name: str | None = None
    remote_cleanup_status: str = "pending"

    @field_validator("usage", mode="before")
    @classmethod
    def strict_usage_values(cls, value: object) -> dict[str, int]:
        if not isinstance(value, Mapping):
            raise ValueError("Gemini usage metadata must be an object")
        normalized: dict[str, int] = {}
        for key, count in value.items():
            if (
                not isinstance(key, str)
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
                or count > MAX_USAGE_TOKENS_PER_DIMENSION
            ):
                raise ValueError("Gemini usage metadata contains an invalid count")
            normalized[key] = count
        return normalized


class GeminiTransport(Protocol):
    """Small seam used by both the real SDK transport and offline fake tests."""

    def upload(self, path: Path, *, mime_type: str) -> GeminiRemoteFile: ...

    def get_file(self, name: str) -> GeminiRemoteFile: ...

    def create_interaction(
        self,
        *,
        model: str,
        remote_uri: str,
        prompt: str,
        response_schema: Mapping[str, Any],
        media_resolution: str,
        max_output_tokens: int,
        max_thinking_tokens: int,
        store: bool,
    ) -> Any: ...

    def delete_file(self, name: str) -> None: ...


@dataclass(frozen=True)
class _DispatchSignal:
    dispatched: bool


class GenAITransport:
    """Lazy wrapper around the official ``google-genai`` Python SDK."""

    def __init__(self, *, api_key_env: str = "GEMINI_API_KEY") -> None:
        key = os.environ.get(api_key_env)
        if not key:
            raise GeminiConfigurationError(
                f"Gemini API key environment variable {api_key_env} is not set."
            )
        try:
            from google import genai  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - exercised when optional extra is absent
            raise GeminiConfigurationError(
                "The optional google-genai dependency is not installed.",
                may_have_dispatched=False,
            ) from exc
        try:
            self._client = genai.Client(api_key=key)
        except Exception as exc:  # pragma: no cover - SDK-specific construction errors
            raise GeminiConfigurationError("Cannot initialize the Gemini SDK client.") from exc

    def upload(self, path: Path, *, mime_type: str) -> GeminiRemoteFile:
        try:
            uploaded = self._client.files.upload(
                file=str(path),
                config={"mime_type": mime_type, "display_name": "ghf-analysis-proxy"},
            )
        except Exception as exc:  # pragma: no cover - requires SDK/network
            raise GeminiProviderError("Gemini proxy upload failed.") from exc
        return _remote_file_from_object(
            uploaded,
            mime_type=mime_type,
            default_state="PROCESSING",
        )

    def get_file(self, name: str) -> GeminiRemoteFile:
        try:
            remote = self._client.files.get(name=name)
        except Exception as exc:  # pragma: no cover - requires SDK/network
            raise GeminiProviderError("Gemini remote file status lookup failed.") from exc
        return _remote_file_from_object(remote, default_state="PROCESSING")

    def create_interaction(
        self,
        *,
        model: str,
        remote_uri: str,
        prompt: str,
        response_schema: Mapping[str, Any],
        media_resolution: str,
        max_output_tokens: int,
        max_thinking_tokens: int,
        store: bool,
    ) -> Any:
        # The SDK accepts the REST-shaped multimodal input.  We intentionally
        # pass one video followed by bounded text, as recommended by the
        # official video-understanding guide.
        generation_config: dict[str, Any] = {
            "media_resolution": media_resolution,
            "max_output_tokens": max_output_tokens,
        }
        if max_thinking_tokens == 0:
            generation_config["thinking_level"] = "minimal"
        try:
            return self._client.interactions.create(
                model=model,
                input=[
                    {"type": "video", "uri": remote_uri, "mime_type": "video/mp4"},
                    {"type": "text", "text": prompt},
                ],
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": dict(response_schema),
                },
                generation_config=generation_config,
                store=store,
            )
        except Exception as exc:  # pragma: no cover - requires SDK/network
            raise GeminiDispatchError(
                "Gemini interaction request failed after the send boundary.",
                may_have_dispatched=True,
            ) from exc

    def delete_file(self, name: str) -> None:
        try:
            self._client.files.delete(name=name)
        except Exception as exc:  # pragma: no cover - requires SDK/network
            if _is_not_found_error(exc):
                return
            raise GeminiCleanupError("Gemini remote file deletion failed.") from exc


class GeminiProvider(ProviderAdapter):
    """Provider contract implementation; all network I/O is transport-injected."""

    def __init__(
        self,
        *,
        transport: GeminiTransport | None = None,
        api_key_env: str = "GEMINI_API_KEY",
        readiness_timeout_seconds: float = 120.0,
        readiness_poll_initial_seconds: float = 1.0,
        readiness_poll_max_seconds: float = 8.0,
        cleanup_retry_limit: int = 3,
    ) -> None:
        self._transport = transport
        self.api_key_env = api_key_env
        self.readiness_timeout_seconds = readiness_timeout_seconds
        self.readiness_poll_initial_seconds = readiness_poll_initial_seconds
        self.readiness_poll_max_seconds = readiness_poll_max_seconds
        self.cleanup_retry_limit = cleanup_retry_limit
        self._descriptor = ProviderDescriptor(
            provider=GEMINI_PROVIDER,
            display_name="Google Gemini Developer API",
            capabilities=ProviderCapabilities(
                video_input=True,
                audio_input=True,
                structured_output=True,
                file_upload=True,
                usage_metadata=True,
                remote_file_deletion=True,
                # M5 intentionally exposes one bounded synchronous request.
                # Batch/long-running orchestration is reserved for M6+.
                batch_execution=False,
                async_execution=False,
            ),
            models=(
                ProviderModel(
                    provider=GEMINI_PROVIDER,
                    model_id=GEMINI_MODEL_ID,
                    billing_modes=("standard",),
                    capabilities=ProviderCapabilities(
                        video_input=True,
                        audio_input=True,
                        structured_output=True,
                        file_upload=True,
                        usage_metadata=True,
                        remote_file_deletion=True,
                        batch_execution=False,
                        async_execution=False,
                    ),
                ),
            ),
        )

    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def execute(
        self,
        request: ProviderRequest,
        *,
        proxy_path: Path | None = None,
        session_proxy_root: Path | None = None,
        prompt: str | None = None,
        response_schema: Mapping[str, Any] | None = None,
        media_resolution: str = "low",
        max_output_tokens: int = 2_048,
        max_thinking_tokens: int = 1_024,
        remote_metadata_path: Path | None = None,
        before_generation: Callable[[], None] | None = None,
    ) -> ProviderCallResult:
        if request.provider != GEMINI_PROVIDER:
            raise GeminiConfigurationError(
                f"Gemini adapter cannot execute provider {request.provider!r}."
            )
        if request.model_id != GEMINI_MODEL_ID or request.billing_mode != "standard":
            raise GeminiConfigurationError(
                "Gemini M5 requires the exact stable model and Standard billing mode."
            )
        path = proxy_path or _payload_path(request.request_payload, "proxy_path")
        if path is None:
            raise GeminiPrivacyError("Gemini requires an analysis proxy path.")
        root = session_proxy_root or _payload_path(request.request_payload, "session_proxy_root")
        if root is None:
            root = path.parent
        validate_proxy_upload(path, root)
        if prompt is None:
            prompt = str(request.request_payload.get("prompt", ""))
        if response_schema is None:
            value = request.request_payload.get("response_schema", {})
            response_schema = value if isinstance(value, Mapping) else {}
        if not prompt.strip() or not response_schema:
            raise GeminiConfigurationError("Gemini request prompt and response schema are required")
        # Resolve credentials/client only after the privacy and exact-contract
        # checks above.  A rejected RAW path must never depend on API-key state.
        transport = self._transport or GenAITransport(api_key_env=self.api_key_env)

        remote: GeminiRemoteFile | None = None
        try:
            remote = transport.upload(path, mime_type="video/mp4")
            if remote_metadata_path is not None:
                atomic_write_json(remote_metadata_path, remote.safe_metadata())
            remote = wait_until_ready(
                transport,
                remote,
                timeout_seconds=self.readiness_timeout_seconds,
                initial_delay_seconds=self.readiness_poll_initial_seconds,
                max_delay_seconds=self.readiness_poll_max_seconds,
            )
            if remote.uri is None:
                raise GeminiProviderError(
                    "Gemini Files API returned no usable URI.", remote_file=remote
                )
            if before_generation is not None:
                before_generation()
            try:
                raw_response = transport.create_interaction(
                    model=request.model_id,
                    remote_uri=remote.uri,
                    prompt=prompt,
                    response_schema=response_schema,
                    media_resolution=media_resolution,
                    max_output_tokens=max_output_tokens,
                    max_thinking_tokens=max_thinking_tokens,
                    store=False,
                )
            except GeminiProviderError as exc:
                # The transport was invoked after the send boundary.  Even a
                # transport-raised error with incomplete classification must
                # remain conservative and reconcile as AMBIGUOUS.
                if exc.may_have_dispatched:
                    raise
                raise GeminiDispatchError(
                    str(exc),
                    may_have_dispatched=True,
                    provider_request_id=exc.provider_request_id,
                    response=exc.response,
                    remote_file=remote,
                ) from exc
            except Exception as exc:
                raise GeminiDispatchError(
                    "Gemini interaction failed after the send boundary.",
                    may_have_dispatched=True,
                    remote_file=remote,
                ) from exc
            envelope = sanitize_interaction_response(
                raw_response,
                model=request.model_id,
                remote_file_name=remote.name,
                max_bytes=int(request.request_payload.get("response_max_bytes", 1_048_576)),
            )
            if envelope.status.lower() != "completed":
                raise GeminiProviderError(
                    f"Gemini interaction ended with status {envelope.status!r}.",
                    may_have_dispatched=True,
                    provider_request_id=envelope.interaction_id,
                    response=envelope.model_dump(mode="json"),
                    remote_file=remote,
                )
            usage = usage_from_envelope(envelope)
            cleanup_status = "deleted"
            try:
                delete_remote_file(
                    transport,
                    remote,
                    retries=self.cleanup_retry_limit,
                )
            except GeminiCleanupError:
                cleanup_status = "pending"
            envelope = envelope.model_copy(update={"remote_cleanup_status": cleanup_status})
            if remote_metadata_path is not None:
                atomic_write_json(
                    remote_metadata_path,
                    remote.safe_metadata(deletion_status=cleanup_status),
                )
            return ProviderCallResult(
                provider=request.provider,
                model_id=request.model_id,
                provider_request_id=envelope.interaction_id,
                usage=usage,
                result=envelope.model_dump(mode="json"),
                completed_at=datetime.now(UTC),
            )
        except GeminiProviderError as exc:
            if remote is not None:
                exc.remote_file = remote
                cleanup_status = "pending"
                try:
                    # Deleting a remote file is safe/idempotent and must not
                    # trigger a second generation attempt, even for an
                    # ambiguous provider outcome.
                    delete_remote_file(
                        transport,
                        remote,
                        retries=self.cleanup_retry_limit,
                    )
                    cleanup_status = "deleted"
                except GeminiCleanupError:
                    pass
                if exc.response:
                    exc.response["remote_cleanup_status"] = cleanup_status
                if remote_metadata_path is not None:
                    atomic_write_json(
                        remote_metadata_path,
                        remote.safe_metadata(deletion_status=cleanup_status),
                    )
            raise
        except Exception:
            # Local persistence/callback failures after upload are not provider
            # dispatch failures, but they must not strand a remote media object.
            # Cleanup is best effort and never causes a generation retry.
            if remote is not None:
                cleanup_status = "pending"
                try:
                    delete_remote_file(
                        transport,
                        remote,
                        retries=self.cleanup_retry_limit,
                    )
                    cleanup_status = "deleted"
                except GeminiCleanupError:
                    pass
                if remote_metadata_path is not None:
                    with suppress(OSError):
                        atomic_write_json(
                            remote_metadata_path,
                            remote.safe_metadata(deletion_status=cleanup_status),
                        )
            raise

    def retry_remote_cleanup(self, metadata_path: Path) -> bool:
        """Retry only remote deletion; never invokes generation."""

        try:
            payload = read_json(metadata_path)
            name = payload.get("name") if isinstance(payload, Mapping) else None
            status = payload.get("deletion_status") if isinstance(payload, Mapping) else None
            if not isinstance(name, str) or not name or status == "deleted":
                return True
            transport = self._transport or GenAITransport(api_key_env=self.api_key_env)
            remote = GeminiRemoteFile(
                name=name,
                mime_type=str(payload.get("mime_type", "video/mp4")),
                state=str(payload.get("state", "ACTIVE")),
                created_at=_parse_datetime(payload.get("created_at")),
            )
            delete_remote_file(transport, remote, retries=self.cleanup_retry_limit)
            atomic_write_json(metadata_path, remote.safe_metadata(deletion_status="deleted"))
            return True
        except (OSError, ValueError, GeminiProviderError):
            return False


def gemini_provider_descriptor() -> ProviderDescriptor:
    return GeminiProvider(transport=_NoNetworkTransport()).descriptor()


class _NoNetworkTransport:
    """Descriptor-only transport that can never accidentally perform I/O."""

    def upload(self, path: Path, *, mime_type: str) -> GeminiRemoteFile:
        raise GeminiProviderError("Gemini transport is not configured for network I/O")

    def get_file(self, name: str) -> GeminiRemoteFile:
        raise GeminiProviderError("Gemini transport is not configured for network I/O")

    def create_interaction(self, **kwargs: Any) -> Any:
        raise GeminiProviderError("Gemini transport is not configured for network I/O")

    def delete_file(self, name: str) -> None:
        raise GeminiProviderError("Gemini transport is not configured for network I/O")


class FakeGeminiTransport:
    """Deterministic offline transport for lifecycle and recovery tests."""

    def __init__(
        self,
        *,
        response: Mapping[str, Any] | str | None = None,
        usage: Mapping[str, int] | None = None,
        upload_error: Exception | None = None,
        processing_states: Sequence[str] = ("ACTIVE",),
        generation_error: Exception | None = None,
        delete_error: Exception | None = None,
    ) -> None:
        self.response = response or {
            "status": "completed",
            "id": "fake-interaction-1",
            "output_text": (
                '{"schema_version":1,"source_duration_ms":1,'
                '"time_basis":"source_relative","matches":[],"candidates":[],'
                '"warnings":[],"metadata":{"backend":"gemini"}}'
            ),
            "usage": usage or {"prompt_token_count": 100, "candidates_token_count": 20},
        }
        self.upload_error = upload_error
        self.processing_states = list(processing_states)
        self.generation_error = generation_error
        self.delete_error = delete_error
        self.upload_count = 0
        self.generation_count = 0
        self.delete_count = 0
        self.uploaded_paths: list[Path] = []
        self.remote = GeminiRemoteFile(name="files/fake-proxy", uri="https://example.invalid/fake")

    def upload(self, path: Path, *, mime_type: str) -> GeminiRemoteFile:
        self.upload_count += 1
        self.uploaded_paths.append(path)
        if self.upload_error is not None:
            raise self.upload_error
        return self.remote.model_copy(
            update={"mime_type": mime_type, "state": self.processing_states[0]}
        )

    def get_file(self, name: str) -> GeminiRemoteFile:
        if len(self.processing_states) > 1:
            state = self.processing_states.pop(0)
        else:
            state = self.processing_states[0]
        return self.remote.model_copy(update={"name": name, "state": state})

    def create_interaction(self, **kwargs: Any) -> Any:
        self.generation_count += 1
        if self.generation_error is not None:
            raise self.generation_error
        return self.response

    def delete_file(self, name: str) -> None:
        self.delete_count += 1
        if self.delete_error is not None:
            raise self.delete_error


def validate_proxy_upload(path: Path, session_proxy_root: Path) -> None:
    """Enforce that only a committed session analysis proxy can cross the boundary."""

    try:
        resolved_path = path.expanduser().resolve()
        resolved_root = session_proxy_root.expanduser().resolve()
        resolved_path.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise GeminiPrivacyError(
            "Gemini may upload only the committed session analysis proxy, never the RAW source."
        ) from exc
    if not resolved_path.is_file() or resolved_path.suffix.lower() not in {".mp4", ".mov", ".webm"}:
        raise GeminiPrivacyError("Gemini upload artifact is not a valid analysis-proxy media file.")
    if resolved_path.name != "analysis_proxy.mp4":
        raise GeminiPrivacyError("Gemini upload artifact must be the session analysis_proxy.mp4.")


def wait_until_ready(
    transport: GeminiTransport,
    remote: GeminiRemoteFile,
    *,
    timeout_seconds: float,
    initial_delay_seconds: float,
    max_delay_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> GeminiRemoteFile:
    state = remote.state.upper()
    if state in {"ACTIVE", "READY", "COMPLETED"}:
        return remote
    if state in {"FAILED", "ERROR", "CANCELLED"}:
        raise GeminiProviderError(
            f"Gemini remote file processing failed: {state}", remote_file=remote
        )
    deadline = monotonic() + timeout_seconds
    delay = initial_delay_seconds
    current = remote
    while monotonic() < deadline:
        sleep(min(delay, max(0.0, deadline - monotonic())))
        current = transport.get_file(current.name)
        state = current.state.upper()
        if state in {"ACTIVE", "READY", "COMPLETED"}:
            return current
        if state in {"FAILED", "ERROR", "CANCELLED"}:
            raise GeminiProviderError(
                f"Gemini remote file processing failed: {state}", remote_file=current
            )
        delay = min(max_delay_seconds, delay * 2)
    raise GeminiProviderError("Gemini remote file readiness polling timed out", remote_file=current)


def delete_remote_file(
    transport: GeminiTransport, remote: GeminiRemoteFile, *, retries: int
) -> None:
    attempts = max(1, retries + 1)
    for attempt in range(attempts):
        try:
            transport.delete_file(remote.name)
            return
        except Exception as exc:
            if _is_not_found_error(exc):
                return
            if attempt == attempts - 1:
                raise GeminiCleanupError(
                    "Gemini remote file deletion failed; cleanup remains pending.",
                    remote_file=remote,
                    cleanup_pending=True,
                ) from exc


def sanitize_interaction_response(
    response: Any,
    *,
    model: str,
    remote_file_name: str | None,
    max_bytes: int,
) -> GeminiInteractionEnvelope:
    """Extract only final output and safe usage/status fields from an SDK object."""

    if isinstance(response, (str, bytes, bytearray)):
        try:
            response = json.loads(response)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise GeminiProviderError(
                "Gemini interaction response was not valid JSON metadata.",
                may_have_dispatched=True,
            ) from exc
    if not isinstance(response, Mapping) and response is None:
        raise GeminiProviderError(
            "Gemini interaction response was empty.",
            may_have_dispatched=True,
        )
    status = _enum_name(_field(response, "status", "completed"), default="completed")
    output_text = _field(response, "output_text", None)
    if output_text is None:
        output_text = _field(response, "text", "")
    if not isinstance(output_text, str):
        raise GeminiProviderError(
            "Gemini response did not expose a safe final output string.",
            may_have_dispatched=True,
        )
    if len(output_text.encode("utf-8")) > max_bytes:
        raise GeminiProviderError(
            "Gemini structured output exceeds the configured byte limit",
            may_have_dispatched=True,
        )
    usage = _usage_dict(response)
    interaction_id = _field(response, "id", None) or _field(response, "interaction_id", None)
    finish_reason = _field(response, "finish_reason", None)
    safety_block = _field(response, "safety_block_reason", None)
    incomplete_reason = _field(response, "incomplete_reason", None)
    try:
        return GeminiInteractionEnvelope(
            interaction_id=str(interaction_id) if interaction_id is not None else None,
            model=model,
            status=status,
            output_text=output_text,
            usage=usage,
            finish_reason=str(finish_reason) if finish_reason is not None else None,
            safety_block_reason=str(safety_block) if safety_block is not None else None,
            incomplete_reason=str(incomplete_reason) if incomplete_reason is not None else None,
            remote_file_name=remote_file_name,
        )
    except PydanticValidationError as exc:
        raise GeminiProviderError(
            "Gemini response envelope failed provider-boundary validation.",
            may_have_dispatched=True,
        ) from exc


def usage_from_envelope(envelope: GeminiInteractionEnvelope) -> ProviderUsageActual:
    usage = envelope.usage
    prompt = _first_int(usage, "prompt_token_count", "input_tokens", "input_token_count")
    video = _first_int(usage, "video_token_count", "input_video_tokens")
    audio = _first_int(usage, "audio_token_count", "input_audio_tokens")
    image = _first_int(usage, "image_token_count", "input_image_tokens")
    cached = _first_int(usage, "cached_content_token_count", "cached_input_tokens")
    output = _first_int(usage, "candidates_token_count", "output_tokens", "visible_output_tokens")
    thinking = _first_int(usage, "thoughts_token_count", "thinking_tokens")
    if not any(value is not None for value in (prompt, video, audio, image, output, thinking)):
        raise GeminiMissingUsageError(
            "Gemini returned a completed response without authoritative usage metadata.",
            may_have_dispatched=True,
            provider_request_id=envelope.interaction_id,
            response=envelope.model_dump(mode="json"),
        )
    # Prompt token counts are text/context totals when modality-specific counts
    # are not exposed.  That is conservative for billing and still bounded by
    # the M4 usage model.
    try:
        return ProviderUsageActual(
            input_text_tokens=prompt or 0,
            input_image_tokens=image or 0,
            input_video_tokens=video or 0,
            input_audio_tokens=audio or 0,
            cached_input_tokens=cached or 0,
            output_tokens=output or 0,
            thinking_tokens=thinking or 0,
            provider_request_id=envelope.interaction_id,
        )
    except PydanticValidationError as exc:
        raise GeminiMissingUsageError(
            "Gemini returned usage metadata outside the M4 safety bounds.",
            may_have_dispatched=True,
            provider_request_id=envelope.interaction_id,
            response=envelope.model_dump(mode="json"),
        ) from exc


def _usage_dict(response: Any) -> dict[str, int]:
    usage = _field(response, "usage", None) or _field(response, "usage_metadata", None)
    if usage is None:
        return {}
    raw = _jsonable(usage)
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and 0 <= value <= 10_000_000:
            result[_snake(str(key))] = value
    return result


def _first_int(values: Mapping[str, int], *names: str) -> int | None:
    for name in names:
        if name in values:
            return values[name]
    return None


def _field(value: Any, name: str, default: Any) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_jsonable(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _jsonable(model_dump(mode="json", exclude_none=True))
        except TypeError:
            return _jsonable(model_dump())
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _jsonable(to_dict())
    return str(value)


def _snake(value: str) -> str:
    result: list[str] = []
    for char in value:
        if char.isupper():
            result.append("_")
        result.append(char.lower())
    return "".join(result).lstrip("_")


def _remote_file_from_object(
    value: Any,
    *,
    mime_type: str = "video/mp4",
    default_state: str = "ACTIVE",
) -> GeminiRemoteFile:
    name = _field(value, "name", None)
    if not isinstance(name, str) or not name:
        raise GeminiProviderError("Gemini upload response did not contain a file name")
    created = _parse_datetime(_field(value, "create_time", None))
    expiration = _field(value, "expiration_time", None)
    return GeminiRemoteFile(
        name=name,
        uri=_field(value, "uri", None),
        mime_type=str(_field(value, "mime_type", mime_type) or mime_type),
        state=_enum_name(_field(value, "state", default_state), default=default_state),
        created_at=created,
        expiration_time=_parse_datetime(expiration) if expiration else None,
    )


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


def _enum_name(value: Any, *, default: str) -> str:
    name = getattr(value, "name", None)
    if isinstance(name, str) and name:
        return name
    if value is None:
        return default
    return str(value)


def _is_not_found_error(exc: BaseException) -> bool:
    """Treat an already-expired/deleted remote file as idempotent cleanup."""

    current: BaseException | None = exc
    for _ in range(4):
        if current is None:
            break
        if isinstance(current, FileNotFoundError):
            return True
        status = getattr(current, "status_code", None) or getattr(current, "code", None)
        if status in {404, "404", "NOT_FOUND", "not_found"}:
            return True
        if "not found" in str(current).lower():
            return True
        current = current.__cause__
    return False


def _payload_path(payload: Mapping[str, Any], key: str) -> Path | None:
    value = payload.get(key)
    return Path(value) if isinstance(value, (str, Path)) else None


__all__ = [
    "GEMINI_MODEL_ID",
    "GEMINI_PROVIDER",
    "FakeGeminiTransport",
    "GeminiCleanupError",
    "GeminiConfigurationError",
    "GeminiDispatchError",
    "GeminiInteractionEnvelope",
    "GeminiMissingUsageError",
    "GeminiPrivacyError",
    "GeminiProvider",
    "GeminiProviderError",
    "GeminiRemoteFile",
    "GeminiTransport",
    "GenAITransport",
    "gemini_provider_descriptor",
    "sanitize_interaction_response",
    "usage_from_envelope",
    "validate_proxy_upload",
    "wait_until_ready",
]
