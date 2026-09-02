"""Gemini Scout adapter with a narrow, deterministic transport seam.

The module is importable without ``google-genai``.  The SDK is loaded only by
``GenAITransport`` when an explicitly opted-in Gemini run is requested.  The
provider owns only network/file lifecycle mechanics; budgeting and canonical
validation remain in the pipeline and M4 services.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
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
from game_highlight_finder.providers.gemini_capabilities import (
    GEMINI_MODEL_IDS,
    resolve_gemini_media_resolution,
    resolve_gemini_thinking_config,
    validate_wire_thinking_level,
)
from game_highlight_finder.storage.atomic import atomic_write_json, read_json

GEMINI_PROVIDER = "gemini"
GEMINI_MODEL_ID = "gemini-3.5-flash-lite"


@dataclass(frozen=True)
class GeminiFailureDiagnostic:
    """Safe provider failure metadata suitable for local persistence."""

    exception_class: str
    sdk_error_class: str | None
    phase: str
    dispatch: str
    http_status: int | None
    provider_code: str | int | None
    provider_status: str | None
    provider_request_id: str | None
    message: str
    exception_chain: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "exception_class": self.exception_class,
            "sdk_error_class": self.sdk_error_class,
            "phase": self.phase,
            "dispatch": self.dispatch,
            "http_status": self.http_status,
            "provider_code": self.provider_code,
            "provider_status": self.provider_status,
            "provider_request_id": self.provider_request_id,
            "message": self.message,
            "exception_chain": list(self.exception_chain),
        }


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
        diagnostic: GeminiFailureDiagnostic | None = None,
    ) -> None:
        super().__init__(message)
        self.may_have_dispatched = may_have_dispatched
        self.provider_request_id = provider_request_id
        self.response = dict(response or {})
        self.remote_file = remote_file
        self.cleanup_pending = cleanup_pending
        self.diagnostic = diagnostic

    def safe_diagnostic(self) -> dict[str, object] | None:
        return self.diagnostic.as_dict() if self.diagnostic is not None else None


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

    def safe_metadata(
        self,
        *,
        deletion_status: str = "pending",
        failure_diagnostic: GeminiFailureDiagnostic | None = None,
    ) -> dict[str, Any]:
        # The URI is deliberately omitted.  It is needed only in-memory for
        # the immediately-following generation call and may contain a signed
        # or otherwise sensitive resource URL.
        metadata: dict[str, Any] = {
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
        if failure_diagnostic is not None:
            metadata["failure_diagnostic"] = failure_diagnostic.as_dict()
        return metadata


class GeminiInteractionEnvelope(BaseModel):
    """Sanitized final response; thought steps are never represented."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    interaction_id: str | None = Field(default=None, max_length=256)
    model: str = GEMINI_MODEL_ID
    status: str = Field(min_length=1, max_length=64)
    output_text: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)
    finish_reason: str | None = Field(default=None, max_length=256)
    safety_block_reason: str | None = Field(default=None, max_length=256)
    incomplete_reason: str | None = Field(default=None, max_length=256)
    remote_file_name: str | None = None
    remote_cleanup_status: str = "pending"

    @field_validator("usage", mode="before")
    @classmethod
    def strict_usage_values(cls, value: object) -> dict[str, Any]:
        return _normalize_usage_metadata(value)


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
        thinking_level: str | None,
        store: bool,
    ) -> Any: ...

    def delete_file(self, name: str) -> None: ...


@dataclass(frozen=True)
class _DispatchSignal:
    dispatched: bool


_DEFAULT_GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
_NUMBERED_GEMINI_API_KEY_RE = re.compile(r"^GEMINI_API_KEY([1-9][0-9]*)$")


def _resolve_gemini_api_key(api_key_env: str, environ: Mapping[str, str]) -> str:
    """Resolve one credential without persisting or exposing its value.

    The configured variable remains authoritative.  Only the default
    ``GEMINI_API_KEY`` name may fall back to a numbered local key pool, which
    keeps persisted config/request fingerprints stable across machines that
    store credentials as ``GEMINI_API_KEY1``, ``GEMINI_API_KEY2``, and so on.
    Selection is deterministic and never retries a provider call.
    """

    key = environ.get(api_key_env)
    if key:
        return key
    if api_key_env != _DEFAULT_GEMINI_API_KEY_ENV:
        raise GeminiConfigurationError(
            f"Gemini API key environment variable {api_key_env} is not set."
        )

    numbered: list[tuple[int, str]] = []
    for name, value in environ.items():
        if not value:
            continue
        match = _NUMBERED_GEMINI_API_KEY_RE.fullmatch(name)
        if match is not None:
            numbered.append((int(match.group(1)), value))
    if numbered:
        numbered.sort(key=lambda item: item[0])
        return numbered[0][1]

    raise GeminiConfigurationError(
        "Gemini API key environment variable GEMINI_API_KEY is not set, and no "
        "numbered fallback GEMINI_API_KEY1..N is available."
    )


class GenAITransport:
    """Lazy wrapper around the official ``google-genai`` Python SDK."""

    def __init__(
        self,
        *,
        api_key_env: str = "GEMINI_API_KEY",
        api_version: str | None = None,
    ) -> None:
        key = _resolve_gemini_api_key(api_key_env, os.environ)
        self.api_version = api_version
        if api_version is not None and api_version not in {"v1", "v1beta"}:
            raise GeminiConfigurationError(
                f"Unsupported Gemini API version {api_version!r}.",
                may_have_dispatched=False,
            )
        try:
            from google import genai  # type: ignore[import-not-found, unused-ignore]
        except ImportError as exc:  # pragma: no cover - exercised when optional extra is absent
            raise GeminiConfigurationError(
                "The optional google-genai dependency is not installed.",
                may_have_dispatched=False,
            ) from exc
        client_kwargs: dict[str, Any] = {"api_key": key}
        if api_version is not None:
            client_kwargs["http_options"] = {"api_version": api_version}
        try:
            self._client = genai.Client(**client_kwargs)
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
        thinking_level: str | None,
        store: bool,
    ) -> Any:
        # The SDK accepts the REST-shaped multimodal input.  We intentionally
        # pass one video followed by bounded text, as recommended by the
        # official video-understanding guide.
        try:
            validate_wire_thinking_level(model, thinking_level)
        except ValueError as exc:
            raise GeminiConfigurationError(str(exc), may_have_dispatched=False) from exc
        try:
            media = resolve_gemini_media_resolution(model, media_resolution)
        except ValueError as exc:
            raise GeminiConfigurationError(str(exc), may_have_dispatched=False) from exc
        generation_config: dict[str, Any] = {
            "max_output_tokens": max_output_tokens,
        }
        generation_config.update(
            {"thinking_level": thinking_level} if thinking_level is not None else {}
        )
        video_input: dict[str, Any] = {
            "type": "video",
            "uri": remote_uri,
            "mime_type": "video/mp4",
        }
        if media.wire_level is not None:
            video_input["resolution"] = media.wire_level
        try:
            return self._client.interactions.create(
                model=model,
                input=[
                    video_input,
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
            diagnostic = diagnose_gemini_exception(exc)
            raise GeminiDispatchError(
                _diagnostic_message(diagnostic),
                may_have_dispatched=diagnostic.dispatch != "NO",
                provider_request_id=diagnostic.provider_request_id,
                diagnostic=diagnostic,
            ) from exc

    def delete_file(self, name: str) -> None:
        try:
            self._client.files.delete(name=name)
        except Exception as exc:  # pragma: no cover - requires SDK/network
            if _is_not_found_error(exc):
                return
            raise GeminiCleanupError("Gemini remote file deletion failed.") from exc


_SECRET_PATTERNS = (
    (re.compile(r"AIza[0-9A-Za-z_-]{20,}"), "<redacted-key>"),
    (re.compile(r"(?i)(?:authorization|x-goog-api-key)\s*[:=]\s*[^,;\s]+"), "<redacted-auth>"),
    (re.compile(r"https?://[^\s'\"<>]+"), "<redacted-url>"),
    (re.compile(r"(?i)files/[A-Za-z0-9._-]+"), "<redacted-file>"),
)


def _safe_provider_message(exc: BaseException) -> str:
    try:
        value = getattr(exc, "message", None) or str(exc)
    except Exception:
        value = type(exc).__name__
    value = " ".join(str(value).split())
    for pattern, replacement in _SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    return value[:512] or type(exc).__name__


def _exception_chain(exc: BaseException) -> tuple[BaseException, ...]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return tuple(chain)


def _safe_request_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 256 or not re.fullmatch(r"[A-Za-z0-9._:-]+", value):
        return None
    return value


def _exception_http_status(chain: tuple[BaseException, ...]) -> int | None:
    for item in chain:
        for candidate in (
            getattr(item, "status_code", None),
            getattr(getattr(item, "response", None), "status_code", None),
        ):
            if isinstance(candidate, int) and 100 <= candidate <= 599:
                return candidate
    return None


def _exception_provider_code(chain: tuple[BaseException, ...]) -> str | int | None:
    for item in chain:
        candidate = getattr(item, "code", None)
        if isinstance(candidate, (str, int)) and not isinstance(candidate, bool):
            value = str(candidate) if isinstance(candidate, str) else candidate
            if isinstance(value, int) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
                return value
    return None


def _exception_provider_status(chain: tuple[BaseException, ...]) -> str | None:
    for item in chain:
        candidate = getattr(item, "status", None)
        if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", candidate):
            return candidate
    return None


def _exception_request_id(chain: tuple[BaseException, ...]) -> str | None:
    for item in chain:
        for name in ("provider_request_id", "request_id", "id"):
            value = _safe_request_id(getattr(item, name, None))
            if value is not None:
                return value
    return None


def _diagnostic_phase(chain: tuple[BaseException, ...], http_status: int | None) -> tuple[str, str]:
    names = [type(item).__name__.lower() for item in chain]
    if any(
        "responsevalidationerror" in name or "apiresponsevalidationerror" in name for name in names
    ):
        return "RESPONSE_PARSE", "YES"
    if http_status is not None or any(
        name in {"apierror", "clienterror", "servererror", "apistatuserror"}
        or name.endswith("apierror")
        for name in names
    ):
        return "HTTP_OR_PROVIDER", "YES"
    if any(
        "timeout" in name
        or "connection" in name
        or "noresponse" in name
        or type(item).__module__.lower().startswith("httpx")
        for item, name in zip(chain, names, strict=True)
    ):
        return "NETWORK_OR_TIMEOUT", "UNKNOWN"
    if any(
        isinstance(item, (TypeError, ValueError, AttributeError, PydanticValidationError))
        for item in chain
    ):
        return "PRE_DISPATCH", "NO"
    return "DISPATCH_UNKNOWN", "UNKNOWN"


def diagnose_gemini_exception(exc: BaseException) -> GeminiFailureDiagnostic:
    """Extract safe SDK/provider evidence without retaining request secrets."""

    chain = _exception_chain(exc)
    http_status = _exception_http_status(chain)
    phase, dispatch = _diagnostic_phase(chain, http_status)
    root = chain[0]
    exception_class = f"{type(root).__module__}.{type(root).__name__}"
    sdk_error_class = next(
        (
            f"{type(item).__module__}.{type(item).__name__}"
            for item in chain
            if type(item).__module__.startswith("google.")
        ),
        None,
    )
    return GeminiFailureDiagnostic(
        exception_class=exception_class,
        sdk_error_class=sdk_error_class,
        phase=phase,
        dispatch=dispatch,
        http_status=http_status,
        provider_code=_exception_provider_code(chain),
        provider_status=_exception_provider_status(chain),
        provider_request_id=_exception_request_id(chain),
        message=_safe_provider_message(root),
        exception_chain=tuple(f"{type(item).__module__}.{type(item).__name__}" for item in chain),
    )


def _diagnostic_message(diagnostic: GeminiFailureDiagnostic) -> str:
    return (
        "Gemini interaction failed "
        f"(phase={diagnostic.phase}, dispatch={diagnostic.dispatch}): "
        f"{diagnostic.message}"
    )


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
                    model_id="gemini-2.5-flash-lite",
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
                ProviderModel(
                    provider=GEMINI_PROVIDER,
                    model_id="gemini-3.7-flash",
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
        thinking_level: str | None = "minimal",
        remote_metadata_path: Path | None = None,
        before_generation: Callable[[], None] | None = None,
        upload_validator: Callable[[Path], None] | None = None,
    ) -> ProviderCallResult:
        if request.provider != GEMINI_PROVIDER:
            raise GeminiConfigurationError(
                f"Gemini adapter cannot execute provider {request.provider!r}."
            )
        if request.model_id not in GEMINI_MODEL_IDS or request.billing_mode != "standard":
            raise GeminiConfigurationError(
                "Gemini Scout requires a supported model and Standard billing mode."
            )
        configured_thinking_level = thinking_level or "minimal"
        try:
            thinking_config = resolve_gemini_thinking_config(
                request.model_id,
                configured_thinking_level,
            )
        except ValueError as exc:
            raise GeminiConfigurationError(str(exc), may_have_dispatched=False) from exc
        path = proxy_path or _payload_path(request.request_payload, "proxy_path")
        if path is None:
            raise GeminiPrivacyError("Gemini requires an analysis proxy path.")
        root = session_proxy_root or _payload_path(request.request_payload, "session_proxy_root")
        if root is None:
            root = path.parent
        if upload_validator is None:
            validate_proxy_upload(path, root)
        else:
            upload_validator(path)
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
                    thinking_level=thinking_config.wire_level,
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
                    may_have_dispatched=exc.may_have_dispatched,
                    provider_request_id=exc.provider_request_id,
                    response=exc.response,
                    remote_file=remote,
                    diagnostic=exc.diagnostic,
                ) from exc
            except Exception as exc:
                diagnostic = diagnose_gemini_exception(exc)
                if diagnostic.dispatch == "NO":
                    diagnostic = replace(
                        diagnostic,
                        phase="DISPATCH_UNKNOWN",
                        dispatch="UNKNOWN",
                    )
                raise GeminiDispatchError(
                    _diagnostic_message(diagnostic),
                    may_have_dispatched=diagnostic.dispatch != "NO",
                    remote_file=remote,
                    diagnostic=diagnostic,
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
                        remote.safe_metadata(
                            deletion_status=cleanup_status,
                            failure_diagnostic=exc.diagnostic,
                        ),
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
                            remote.safe_metadata(
                                deletion_status=cleanup_status,
                                failure_diagnostic=None,
                            ),
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
        usage: Mapping[str, Any] | None = None,
        upload_error: Exception | None = None,
        processing_states: Sequence[str] = ("ACTIVE",),
        generation_error: Exception | None = None,
        delete_error: Exception | None = None,
        api_version: str = "v1",
    ) -> None:
        self.api_version = api_version
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
        self.request_calls: list[dict[str, Any]] = []
        self.last_request: dict[str, Any] | None = None
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
        model = str(kwargs.get("model"))
        media = resolve_gemini_media_resolution(model, str(kwargs.get("media_resolution") or "low"))
        video_input: dict[str, Any] = {
            "type": "video",
            "uri": kwargs.get("remote_uri"),
            "mime_type": "video/mp4",
        }
        if media.wire_level is not None:
            video_input["resolution"] = media.wire_level
        request = {
            "model": model,
            "input": [
                video_input,
                {"type": "text", "text": kwargs.get("prompt", "")},
            ],
            "response_format": {
                "type": "text",
                "mime_type": "application/json",
                "schema": dict(kwargs.get("response_schema", {})),
            },
            "generation_config": {
                "max_output_tokens": kwargs.get("max_output_tokens"),
                **(
                    {"thinking_level": kwargs.get("thinking_level")}
                    if kwargs.get("thinking_level") is not None
                    else {}
                ),
            },
            "store": kwargs.get("store"),
        }
        self.last_request = request
        self.request_calls.append(request)
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


def _normalize_structured_output_text(output_text: str) -> str:
    """Remove one whole-response Markdown JSON fence and nothing else.

    Gemini Interactions can return fenced JSON even when ``response_format``
    requests JSON.  The canonical parser remains strict JSON, so normalize
    only the presentation wrapper when the entire trimmed response is exactly
    one `````json`` (or unlabelled `````) fence.  Prose or mixed content is
    intentionally left untouched and will still fail closed downstream.
    """

    stripped = output_text.strip()
    if not stripped.startswith("```") or not stripped.endswith("```"):
        return output_text
    first_newline = stripped.find("\n")
    if first_newline < 0:
        return output_text
    opener = stripped[:first_newline].strip().lower()
    if opener not in {"```", "```json"}:
        return output_text
    inner = stripped[first_newline + 1 : -3].strip()
    return inner


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
    output_text = _normalize_structured_output_text(output_text)
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


_USAGE_SCALAR_KEYS = {
    "total_input_tokens",
    "total_output_tokens",
    "total_thought_tokens",
    "total_cached_tokens",
    "total_tokens",
    "total_tool_use_tokens",
    "prompt_token_count",
    "input_tokens",
    "input_token_count",
    "candidates_token_count",
    "output_tokens",
    "visible_output_tokens",
    "thoughts_token_count",
    "thinking_tokens",
    "video_token_count",
    "input_video_tokens",
    "audio_token_count",
    "input_audio_tokens",
    "image_token_count",
    "input_image_tokens",
    "cached_content_token_count",
    "cached_input_tokens",
}
_USAGE_BREAKDOWN_KEYS = {
    "input_tokens_by_modality",
    "cached_tokens_by_modality",
    "output_tokens_by_modality",
    "tool_use_tokens_by_modality",
}
_USAGE_MODALITIES = {"text", "image", "video", "audio", "document"}


def usage_from_envelope(envelope: GeminiInteractionEnvelope) -> ProviderUsageActual:
    """Map current Interactions usage into the bounded M4 dimensions.

    Interactions totals and modality breakdowns are authoritative.  When a
    breakdown is present, its sum must not exceed ``total_input_tokens`` and
    any safe residual is assigned to text exactly once.  Legacy aliases remain
    supported only when they agree with current totals; conflicting duplicate
    representations fail closed.
    """

    usage = envelope.usage
    current_input = _first_int(usage, "total_input_tokens")
    current_output = _first_int(usage, "total_output_tokens")
    current_thinking = _first_int(usage, "total_thought_tokens")
    current_cached = _first_int(usage, "total_cached_tokens")
    current_shape = any(
        key in usage
        for key in {
            "total_input_tokens",
            "total_output_tokens",
            "total_thought_tokens",
            "total_cached_tokens",
            "input_tokens_by_modality",
            "cached_tokens_by_modality",
        }
    )
    mapped: dict[str, int]
    cached_count = 0
    output_count = 0
    thinking_count = 0

    try:
        if current_shape:
            if current_input is None or current_output is None:
                raise ValueError(
                    "Interactions usage must include total_input_tokens and total_output_tokens"
                )
            _reject_conflicting_alias(
                usage,
                current_input,
                "total_input_tokens",
                "prompt_token_count",
                "input_tokens",
                "input_token_count",
            )
            _reject_conflicting_alias(
                usage,
                current_output,
                "total_output_tokens",
                "candidates_token_count",
                "output_tokens",
                "visible_output_tokens",
            )
            if current_thinking is not None:
                _reject_conflicting_alias(
                    usage,
                    current_thinking,
                    "total_thought_tokens",
                    "thoughts_token_count",
                    "thinking_tokens",
                )
            if current_cached is not None:
                _reject_conflicting_alias(
                    usage,
                    current_cached,
                    "total_cached_tokens",
                    "cached_content_token_count",
                    "cached_input_tokens",
                )
            _reject_conflicting_modalities(usage)
            mapped = _map_interactions_input(usage, current_input, current_cached or 0)
            output_count = current_output
            thinking_count = current_thinking or 0
            cached_count = current_cached or 0
        else:
            # Legacy/fake fixtures use independent prompt and modality fields.
            # Keep their historical semantics, but still bound every value.
            prompt = _first_int(usage, "prompt_token_count", "input_tokens", "input_token_count")
            video = _first_int(usage, "video_token_count", "input_video_tokens")
            audio = _first_int(usage, "audio_token_count", "input_audio_tokens")
            image = _first_int(usage, "image_token_count", "input_image_tokens")
            cached = _first_int(usage, "cached_content_token_count", "cached_input_tokens")
            output = _first_int(
                usage, "candidates_token_count", "output_tokens", "visible_output_tokens"
            )
            thinking = _first_int(usage, "thoughts_token_count", "thinking_tokens")
            if not any(
                value is not None
                for value in (prompt, video, audio, image, cached, output, thinking)
            ):
                raise ValueError("no authoritative usage metadata")
            mapped = {
                "input_text_tokens": prompt or 0,
                "input_image_tokens": image or 0,
                "input_video_tokens": video or 0,
                "input_audio_tokens": audio or 0,
            }
            cached_count = cached or 0
            output_count = output or 0
            thinking_count = thinking or 0

        return ProviderUsageActual(
            **mapped,
            cached_input_tokens=cached_count,
            output_tokens=output_count,
            thinking_tokens=thinking_count,
            provider_request_id=envelope.interaction_id,
        )
    except (PydanticValidationError, ValueError) as exc:
        raise GeminiMissingUsageError(
            "Gemini returned missing, conflicting, or unsafe usage metadata.",
            may_have_dispatched=True,
            provider_request_id=envelope.interaction_id,
            response=envelope.model_dump(mode="json"),
        ) from exc


def _map_interactions_input(
    usage: Mapping[str, Any], total_input: int, total_cached: int
) -> dict[str, int]:
    breakdown = usage.get("input_tokens_by_modality")
    totals = {"text": 0, "image": 0, "video": 0, "audio": 0}
    if breakdown is not None:
        if not isinstance(breakdown, Sequence) or isinstance(breakdown, (str, bytes, bytearray)):
            raise ValueError("input_tokens_by_modality must be an array")
        for item in breakdown:
            if not isinstance(item, Mapping):
                raise ValueError("input_tokens_by_modality contains a malformed entry")
            modality = item.get("modality")
            tokens = item.get("tokens")
            if not isinstance(modality, str) or modality not in _USAGE_MODALITIES:
                raise ValueError("input_tokens_by_modality contains an unsupported modality")
            if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
                raise ValueError("input_tokens_by_modality contains an invalid token count")
            if tokens > MAX_USAGE_TOKENS_PER_DIMENSION:
                raise ValueError("input_tokens_by_modality exceeds the safety bound")
            if modality == "document" and tokens:
                raise ValueError("document input is not chargeable by the M4 Gemini dimensions")
            if modality in totals:
                if totals[modality] != 0:
                    raise ValueError("input_tokens_by_modality contains duplicate modalities")
                totals[modality] = tokens
    breakdown_total = sum(totals.values())
    if breakdown_total > total_input:
        raise ValueError("input modality breakdown exceeds total_input_tokens")

    cached_breakdown = usage.get("cached_tokens_by_modality")
    if total_cached or cached_breakdown is not None:
        if cached_breakdown is None:
            raise ValueError(
                "cached input requires cached_tokens_by_modality to avoid double charging"
            )
        cached_total = 0
        if not isinstance(cached_breakdown, Sequence) or isinstance(
            cached_breakdown, (str, bytes, bytearray)
        ):
            raise ValueError("cached_tokens_by_modality must be an array")
        for item in cached_breakdown:
            if not isinstance(item, Mapping):
                raise ValueError("cached_tokens_by_modality contains a malformed entry")
            modality = item.get("modality")
            tokens = item.get("tokens")
            if not isinstance(modality, str) or modality not in totals:
                raise ValueError("cached_tokens_by_modality contains an unsupported modality")
            if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
                raise ValueError("cached_tokens_by_modality contains an invalid token count")
            if totals[modality] < tokens:
                raise ValueError("cached modality tokens exceed input modality tokens")
            totals[modality] -= tokens
            cached_total += tokens
        if cached_total != total_cached:
            raise ValueError("cached modality breakdown conflicts with total_cached_tokens")
    residual = total_input - breakdown_total
    totals["text"] += residual
    return {
        "input_text_tokens": totals["text"],
        "input_image_tokens": totals["image"],
        "input_video_tokens": totals["video"],
        "input_audio_tokens": totals["audio"],
    }


def _reject_conflicting_alias(
    usage: Mapping[str, Any], current: int, current_name: str, *aliases: str
) -> None:
    for alias in aliases:
        legacy = usage.get(alias)
        if legacy is not None and legacy != current:
            raise ValueError(f"{current_name} conflicts with legacy usage field {alias}")


def _reject_conflicting_modalities(usage: Mapping[str, Any]) -> None:
    aliases = {
        "video": ("video_token_count", "input_video_tokens"),
        "audio": ("audio_token_count", "input_audio_tokens"),
        "image": ("image_token_count", "input_image_tokens"),
        "text": ("prompt_token_count", "input_tokens", "input_token_count"),
    }
    current_breakdown = usage.get("input_tokens_by_modality")
    current_values: dict[str, int] = {}
    if isinstance(current_breakdown, Sequence) and not isinstance(
        current_breakdown, (str, bytes, bytearray)
    ):
        current_values = {
            str(item["modality"]): int(item["tokens"])
            for item in current_breakdown
            if isinstance(item, Mapping)
        }
    for modality, names in aliases.items():
        legacy = _first_int(usage, *names)
        if legacy is None:
            continue
        if current_breakdown is None:
            raise ValueError(
                f"legacy {modality} usage cannot be combined with Interactions totals safely"
            )
        if current_values.get(modality, 0) != legacy:
            raise ValueError(f"Interactions {modality} usage conflicts with a legacy alias")


def _normalize_usage_metadata(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("Gemini usage metadata must be an object")
    raw = _jsonable(value)
    if not isinstance(raw, Mapping):
        raise ValueError("Gemini usage metadata must be an object")
    normalized: dict[str, Any] = {}
    for raw_key, raw_value in raw.items():
        key = _snake(str(raw_key))
        if key in _USAGE_SCALAR_KEYS:
            if (
                isinstance(raw_value, bool)
                or not isinstance(raw_value, int)
                or raw_value < 0
                or raw_value > MAX_USAGE_TOKENS_PER_DIMENSION
            ):
                raise ValueError("Gemini usage metadata contains an invalid count")
            normalized[key] = raw_value
        elif key in _USAGE_BREAKDOWN_KEYS:
            normalized[key] = _normalize_modality_breakdown(raw_value)
        # Unknown provider fields are intentionally excluded from the
        # sanitized artifact.  They cannot influence authoritative billing.
    return normalized


def _normalize_modality_breakdown(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("Gemini modality token usage must be an array")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("Gemini modality token usage contains a malformed entry")
        modality = item.get("modality")
        tokens = item.get("tokens")
        if not isinstance(modality, str):
            raise ValueError("Gemini modality token usage has no modality")
        modality = modality.lower()
        if modality in seen:
            raise ValueError("Gemini modality token usage contains duplicate modalities")
        if modality not in _USAGE_MODALITIES:
            raise ValueError("Gemini modality token usage contains an unsupported modality")
        if (
            isinstance(tokens, bool)
            or not isinstance(tokens, int)
            or tokens < 0
            or tokens > MAX_USAGE_TOKENS_PER_DIMENSION
        ):
            raise ValueError("Gemini modality token usage contains an invalid count")
        seen.add(modality)
        result.append({"modality": modality, "tokens": tokens})
    return result


def _usage_dict(response: Any) -> dict[str, Any]:
    usage = _field(response, "usage", None) or _field(response, "usage_metadata", None)
    if usage is None:
        return {}
    raw = _jsonable(usage)
    if not isinstance(raw, Mapping):
        return {}
    return _normalize_usage_metadata(raw)


def _first_int(values: Mapping[str, Any], *names: str) -> int | None:
    for name in names:
        if name in values:
            value = values[name]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"usage field {name} is not an integer")
            return int(value)
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
    "GEMINI_MODEL_IDS",
    "GEMINI_PROVIDER",
    "FakeGeminiTransport",
    "GeminiCleanupError",
    "GeminiConfigurationError",
    "GeminiDispatchError",
    "GeminiFailureDiagnostic",
    "GeminiInteractionEnvelope",
    "GeminiMissingUsageError",
    "GeminiPrivacyError",
    "GeminiProvider",
    "GeminiProviderError",
    "GeminiRemoteFile",
    "GeminiTransport",
    "GenAITransport",
    "diagnose_gemini_exception",
    "gemini_provider_descriptor",
    "sanitize_interaction_response",
    "usage_from_envelope",
    "validate_proxy_upload",
    "wait_until_ready",
]
