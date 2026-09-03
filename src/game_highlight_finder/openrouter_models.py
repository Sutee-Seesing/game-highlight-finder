"""Locked OpenRouter model profiles for the bounded H5A gameplay bake-off.

Profiles are deliberately explicit: exact model, exact upstream provider, response
format contract, reasoning support, context ceiling, and conservative list-price
rates. Promotional discounts may reduce actual cash spend but never reduce the
reservation rate used by the local hard-budget gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

ResponseFormatMode = Literal["json_schema", "json_object"]


@dataclass(frozen=True, slots=True)
class OpenRouterModelProfile:
    model_id: str
    display_name: str
    upstream_provider_slug: str
    selected_provider_name: str
    context_tokens: int
    input_per_million_usd: Decimal
    output_per_million_usd: Decimal
    cached_input_per_million_usd: Decimal | None
    response_format_mode: ResponseFormatMode
    supports_reasoning: bool
    pricing_source: str
    pricing_verified_at: datetime
    effective_from: datetime
    base_price_prompt_token_limit: int | None = None
    notes: str = ""

    @property
    def max_prompt_price_per_token_usd(self) -> Decimal:
        return self.input_per_million_usd / Decimal(1_000_000)

    @property
    def max_completion_price_per_token_usd(self) -> Decimal:
        return self.output_per_million_usd / Decimal(1_000_000)


_VERIFIED = datetime(2026, 9, 3, 5, 15, tzinfo=UTC)

QWEN_38_FLASH = OpenRouterModelProfile(
    model_id="qwen/qwen3.8-flash",
    display_name="Qwen3.8 Flash",
    upstream_provider_slug="alibaba",
    selected_provider_name="Alibaba",
    context_tokens=1_000_000,
    input_per_million_usd=Decimal("0.15"),
    output_per_million_usd=Decimal("0.47"),
    cached_input_per_million_usd=Decimal("0.016"),
    response_format_mode="json_schema",
    supports_reasoning=True,
    pricing_source="https://openrouter.ai/api/v1/models/qwen/qwen3.8-flash/endpoints",
    pricing_verified_at=_VERIFIED,
    effective_from=datetime(2026, 8, 26, tzinfo=UTC),
)

GLM_53_FLASH = OpenRouterModelProfile(
    model_id="z-ai/glm-5.3-flash",
    display_name="GLM-5.3-Flash",
    upstream_provider_slug="z-ai",
    selected_provider_name="Z.AI",
    context_tokens=1_048_576,
    input_per_million_usd=Decimal("0.15"),
    output_per_million_usd=Decimal("0.50"),
    cached_input_per_million_usd=Decimal("0.03"),
    response_format_mode="json_object",
    supports_reasoning=True,
    pricing_source="https://openrouter.ai/api/v1/models/z-ai/glm-5.3-flash/endpoints",
    pricing_verified_at=_VERIFIED,
    effective_from=datetime(2026, 8, 26, tzinfo=UTC),
    notes=(
        "Reservation uses undiscounted list price. OpenRouter showed a 50% Z.AI discount "
        "through 2026-09-09T16:00:00Z when this profile was verified."
    ),
)

MIMO_V25 = OpenRouterModelProfile(
    model_id="xiaomi/mimo-v2.5",
    display_name="MiMo-V2.5",
    upstream_provider_slug="xiaomi",
    selected_provider_name="Xiaomi",
    context_tokens=1_048_576,
    input_per_million_usd=Decimal("0.14"),
    output_per_million_usd=Decimal("0.28"),
    cached_input_per_million_usd=Decimal("0.0028"),
    response_format_mode="json_object",
    supports_reasoning=True,
    pricing_source="https://openrouter.ai/api/v1/models/xiaomi/mimo-v2.5/endpoints",
    pricing_verified_at=_VERIFIED,
    effective_from=datetime(2026, 4, 22, tzinfo=UTC),
    notes="Pinned to Xiaomi direct instead of the cheaper but unhealthy GMICloud route.",
)

SEED_20_MINI = OpenRouterModelProfile(
    model_id="bytedance-seed/seed-2.0-mini",
    display_name="Seed-2.0-Mini",
    upstream_provider_slug="seed",
    selected_provider_name="Seed",
    context_tokens=262_144,
    input_per_million_usd=Decimal("0.10"),
    output_per_million_usd=Decimal("0.40"),
    cached_input_per_million_usd=None,
    response_format_mode="json_schema",
    supports_reasoning=True,
    pricing_source="https://openrouter.ai/api/v1/models/bytedance-seed/seed-2.0-mini/endpoints",
    pricing_verified_at=_VERIFIED,
    effective_from=datetime(2026, 2, 26, tzinfo=UTC),
    base_price_prompt_token_limit=128_000,
    notes=(
        "Endpoint doubles prompt/completion rates at >=128k prompt tokens; "
        "bake-off must stay below it."
    ),
)

SEED_20_LITE = OpenRouterModelProfile(
    model_id="bytedance-seed/seed-2.0-lite",
    display_name="Seed-2.0-Lite",
    upstream_provider_slug="seed",
    selected_provider_name="Seed",
    context_tokens=262_144,
    input_per_million_usd=Decimal("0.25"),
    output_per_million_usd=Decimal("2.00"),
    cached_input_per_million_usd=None,
    response_format_mode="json_schema",
    supports_reasoning=True,
    pricing_source="https://openrouter.ai/api/v1/models/bytedance-seed/seed-2.0-lite/endpoints",
    pricing_verified_at=_VERIFIED,
    effective_from=datetime(2026, 3, 10, tzinfo=UTC),
    base_price_prompt_token_limit=128_000,
    notes=(
        "Endpoint doubles prompt/completion rates at >=128k prompt tokens; "
        "bake-off must stay below it."
    ),
)

GLM_5V_TURBO = OpenRouterModelProfile(
    model_id="z-ai/glm-5v-turbo",
    display_name="GLM-5V-Turbo",
    upstream_provider_slug="z-ai",
    selected_provider_name="Z.AI",
    context_tokens=202_752,
    input_per_million_usd=Decimal("1.20"),
    output_per_million_usd=Decimal("4.00"),
    cached_input_per_million_usd=Decimal("0.24"),
    response_format_mode="json_object",
    supports_reasoning=True,
    pricing_source="https://openrouter.ai/api/v1/models/z-ai/glm-5v-turbo/endpoints",
    pricing_verified_at=_VERIFIED,
    effective_from=datetime(2026, 4, 1, tzinfo=UTC),
)

QWEN_38_MAX = OpenRouterModelProfile(
    model_id="qwen/qwen3.8-max",
    display_name="Qwen3.8 Max",
    upstream_provider_slug="alibaba",
    selected_provider_name="Alibaba",
    context_tokens=1_000_000,
    input_per_million_usd=Decimal("2.00"),
    output_per_million_usd=Decimal("6.00"),
    cached_input_per_million_usd=Decimal("0.25"),
    response_format_mode="json_schema",
    supports_reasoning=True,
    pricing_source="https://openrouter.ai/api/v1/models/qwen/qwen3.8-max/endpoints",
    pricing_verified_at=_VERIFIED,
    effective_from=datetime(2026, 8, 3, tzinfo=UTC),
)

OPENROUTER_ROUND_A_PROFILES = (
    QWEN_38_FLASH,
    GLM_53_FLASH,
    MIMO_V25,
    SEED_20_MINI,
    SEED_20_LITE,
    GLM_5V_TURBO,
    QWEN_38_MAX,
)
OPENROUTER_ROUND_A_MODEL_IDS = tuple(profile.model_id for profile in OPENROUTER_ROUND_A_PROFILES)
OPENROUTER_MODEL_PROFILES = {profile.model_id: profile for profile in OPENROUTER_ROUND_A_PROFILES}


def get_openrouter_model_profile(model_id: str) -> OpenRouterModelProfile:
    try:
        return OPENROUTER_MODEL_PROFILES[model_id]
    except KeyError as exc:
        raise ValueError(f"Unsupported OpenRouter bake-off model: {model_id}") from exc


__all__ = [
    "GLM_5V_TURBO",
    "GLM_53_FLASH",
    "MIMO_V25",
    "OPENROUTER_MODEL_PROFILES",
    "OPENROUTER_ROUND_A_MODEL_IDS",
    "OPENROUTER_ROUND_A_PROFILES",
    "QWEN_38_FLASH",
    "QWEN_38_MAX",
    "SEED_20_LITE",
    "SEED_20_MINI",
    "OpenRouterModelProfile",
    "ResponseFormatMode",
    "get_openrouter_model_profile",
]
