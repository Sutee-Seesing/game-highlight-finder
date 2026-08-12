"""Deterministic, offline provider contract fixture for M4 tests only."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from game_highlight_finder.providers.base import (
    ProviderAdapter,
    ProviderCallResult,
    ProviderCapabilities,
    ProviderDescriptor,
    ProviderModel,
    ProviderRequest,
    ProviderUsageActual,
)


class FakeProvider(ProviderAdapter):
    def __init__(self, *, provider: str = "fake", model_id: str = "fake-model") -> None:
        self._descriptor = ProviderDescriptor(
            provider=provider,
            display_name="Offline test provider",
            capabilities=ProviderCapabilities(usage_metadata=True, structured_output=True),
            models=(
                ProviderModel(
                    provider=provider,
                    model_id=model_id,
                    aliases=("fake-cheap",),
                    billing_modes=("standard",),
                    capabilities=ProviderCapabilities(usage_metadata=True, structured_output=True),
                ),
            ),
        )

    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def execute(self, request: ProviderRequest) -> ProviderCallResult:
        digest = hashlib.sha256(request.request_fingerprint.encode("ascii")).hexdigest()[:24]
        return ProviderCallResult(
            provider=request.provider,
            model_id=request.model_id,
            provider_request_id=f"fake-{digest}",
            usage=ProviderUsageActual(
                **request.usage_estimate.model_dump(mode="python"),
                provider_request_id=f"fake-{digest}",
            ),
            result={"status": "offline-fake"},
            completed_at=datetime.now(UTC),
        )
