"""Provider-neutral contracts; no SDK or network implementation belongs here."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from game_highlight_finder.errors import ProviderContractError

# A provider adapter is an untrusted boundary.  Keep each usage dimension
# bounded before it reaches cost arithmetic or the durable ledger.  The bound
# is intentionally per dimension so a malformed response cannot smuggle an
# unbounded integer through one modality while the others remain small.
MAX_USAGE_TOKENS_PER_DIMENSION = 10_000_000


class ProviderContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderUsageEstimate(ProviderContractModel):
    """Maximum billable usage permitted for one future provider request."""

    input_text_tokens: int = Field(default=0, ge=0, le=MAX_USAGE_TOKENS_PER_DIMENSION)
    input_image_tokens: int = Field(default=0, ge=0, le=MAX_USAGE_TOKENS_PER_DIMENSION)
    input_video_tokens: int = Field(default=0, ge=0, le=MAX_USAGE_TOKENS_PER_DIMENSION)
    input_audio_tokens: int = Field(default=0, ge=0, le=MAX_USAGE_TOKENS_PER_DIMENSION)
    cached_input_tokens: int = Field(default=0, ge=0, le=MAX_USAGE_TOKENS_PER_DIMENSION)
    output_tokens: int = Field(default=0, ge=0, le=MAX_USAGE_TOKENS_PER_DIMENSION)

    @field_validator(
        "input_text_tokens",
        "input_image_tokens",
        "input_video_tokens",
        "input_audio_tokens",
        "cached_input_tokens",
        "output_tokens",
        mode="before",
    )
    @classmethod
    def strict_counts(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("usage counts must be integer values")
        return value

    def as_dimensions(self) -> dict[str, int]:
        return {
            "input_text_tokens": self.input_text_tokens,
            "input_image_tokens": self.input_image_tokens,
            "input_video_tokens": self.input_video_tokens,
            "input_audio_tokens": self.input_audio_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
        }


class ProviderUsageActual(ProviderUsageEstimate):
    """Provider-reported usage after a request has crossed the send boundary."""

    provider_request_id: str | None = Field(default=None, max_length=256)


class ProviderCapabilities(ProviderContractModel):
    video_input: bool = False
    audio_input: bool = False
    structured_output: bool = False
    file_upload: bool = False
    usage_metadata: bool = False
    remote_file_deletion: bool = False
    batch_execution: bool = False
    async_execution: bool = False


class ProviderModel(ProviderContractModel):
    provider: str = Field(min_length=1, max_length=64)
    model_id: str = Field(min_length=1, max_length=256)
    aliases: tuple[str, ...] = ()
    billing_modes: tuple[str, ...] = ("standard",)
    capabilities: ProviderCapabilities = ProviderCapabilities()
    enabled: bool = True

    @field_validator("billing_modes", mode="before")
    @classmethod
    def validate_billing_modes(cls, value: object) -> object:
        if isinstance(value, str):
            value = (value,)
        if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
            raise ValueError("billing_modes must be a sequence of strings")
        normalized = tuple(str(item).strip() for item in value)
        if not normalized or any(not item for item in normalized):
            raise ValueError("billing_modes must contain at least one non-empty mode")
        return normalized

    @model_validator(mode="after")
    def aliases_are_unique(self) -> ProviderModel:
        if len(set(self.aliases)) != len(self.aliases):
            raise ValueError("provider model aliases must be unique")
        if self.model_id in self.aliases:
            raise ValueError("provider model alias cannot equal its exact model ID")
        return self


class ProviderDescriptor(ProviderContractModel):
    provider: str = Field(min_length=1, max_length=64)
    display_name: str = Field(default="", max_length=160)
    models: tuple[ProviderModel, ...] = ()
    capabilities: ProviderCapabilities = ProviderCapabilities()
    enabled: bool = True

    @model_validator(mode="after")
    def models_match_provider(self) -> ProviderDescriptor:
        if any(model.provider != self.provider for model in self.models):
            raise ValueError("provider model belongs to a different provider")
        if len({model.model_id for model in self.models}) != len(self.models):
            raise ValueError("provider model IDs must be unique")
        return self


class ProviderRequest(ProviderContractModel):
    call_id: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=64)
    model_id: str = Field(min_length=1, max_length=256)
    billing_mode: str = Field(min_length=1, max_length=64)
    stage: str = Field(min_length=1, max_length=64)
    session_id: str | None = Field(default=None, max_length=128)
    usage_estimate: ProviderUsageEstimate
    request_payload: Mapping[str, Any] = Field(default_factory=dict)

    @property
    def request_fingerprint(self) -> str:
        payload = {
            "provider": self.provider,
            "model_id": self.model_id,
            "billing_mode": self.billing_mode,
            "stage": self.stage,
            "session_id": self.session_id,
            "usage_estimate": self.usage_estimate.model_dump(mode="json"),
            "request_payload": self.request_payload,
        }
        try:
            encoded = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ProviderContractError("Provider request contains non-canonical values.") from exc
        return hashlib.sha256(encoded).hexdigest()


class ProviderCallResult(ProviderContractModel):
    provider: str
    model_id: str
    provider_request_id: str | None = None
    usage: ProviderUsageActual
    result: Mapping[str, Any] = Field(default_factory=dict)
    completed_at: datetime


class ProviderAdapter(Protocol):
    """Port implemented by a future provider adapter after M4."""

    def descriptor(self) -> ProviderDescriptor: ...

    def execute(self, request: ProviderRequest) -> ProviderCallResult:
        """Perform provider I/O; M4 deliberately ships no real implementation."""
        ...


class ProviderRegistry:
    """Exact provider/model/billing-mode registry with deterministic aliases."""

    def __init__(self, descriptors: Sequence[ProviderDescriptor] = ()) -> None:
        self._descriptors: dict[str, ProviderDescriptor] = {}
        self._aliases: dict[str, tuple[str, str]] = {}
        for descriptor in descriptors:
            self.register(descriptor)

    def register(self, descriptor: ProviderDescriptor) -> None:
        if descriptor.provider in self._descriptors:
            raise ProviderContractError(f"Provider is already registered: {descriptor.provider}")
        if not descriptor.enabled:
            return
        for model in descriptor.models:
            for alias in model.aliases:
                if alias in self._aliases:
                    raise ProviderContractError(f"Provider model alias is ambiguous: {alias}")
                self._aliases[alias] = (descriptor.provider, model.model_id)
        self._descriptors[descriptor.provider] = descriptor

    def descriptor(self, provider: str) -> ProviderDescriptor:
        descriptor = self._descriptors.get(provider)
        if descriptor is None:
            raise ProviderContractError(f"Unknown provider: {provider}")
        return descriptor

    def resolve(self, provider: str, model_id: str, billing_mode: str) -> ProviderModel:
        descriptor = self.descriptor(provider)
        model = next((item for item in descriptor.models if item.model_id == model_id), None)
        if model is None:
            raise ProviderContractError(
                f"Unknown exact model for provider {provider!r}: {model_id!r}"
            )
        if billing_mode not in model.billing_modes:
            raise ProviderContractError(
                f"Unsupported billing mode {billing_mode!r} for {provider}/{model_id}"
            )
        if not model.enabled:
            raise ProviderContractError(f"Provider model is disabled: {provider}/{model_id}")
        return model

    def resolve_alias(self, alias: str, billing_mode: str) -> ProviderModel:
        target = self._aliases.get(alias)
        if target is None:
            raise ProviderContractError(f"Unknown provider model alias: {alias}")
        return self.resolve(target[0], target[1], billing_mode)

    def resolve_exact_or_alias(
        self, provider: str, model_or_alias: str, billing_mode: str
    ) -> ProviderModel:
        try:
            return self.resolve(provider, model_or_alias, billing_mode)
        except ProviderContractError as exact_error:
            target = self._aliases.get(model_or_alias)
            if target is None or target[0] != provider:
                raise exact_error
            return self.resolve(target[0], target[1], billing_mode)

    def models(self) -> tuple[ProviderModel, ...]:
        return tuple(
            model for descriptor in self._descriptors.values() for model in descriptor.models
        )
