"""Versioned production pricing snapshots for explicitly selected paid models.

Each entry is exact-model and exact-billing-mode only. FX remains an explicit
local snapshot in accordance with the M4 policy; there is no silent model,
provider, billing-tier, or currency fallback.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from game_highlight_finder.cost.models import PricingEntry
from game_highlight_finder.cost.pricing import PricingCatalog

GEMINI_MODEL_ID = "gemini-3.5-flash-lite"
GEMINI_25_MODEL_ID = "gemini-2.5-flash-lite"
GEMINI_37_MODEL_ID = "gemini-3.7-flash"
GEMINI_PRICING_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing"
GEMINI_MODEL_SOURCE = "https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite"
GEMINI_25_MODEL_SOURCE = "https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash-lite"
GEMINI_37_MODEL_SOURCE = "https://ai.google.dev/gemini-api/docs/models/gemini-3.7-flash"
# The checkout's validation clock is 2026-08-13 Asia/Bangkok (2026-08-12
# afternoon UTC). Keep the older snapshots behind that clock so freshness
# checks do not accidentally treat them as future-dated.
GEMINI_PRICING_VERIFIED_AT = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
GEMINI_CATALOG_VERSION = "google-gemini-2026-08-13-v1"
GEMINI_37_CATALOG_VERSION = "google-gemini-2026-08-23-v3"
GEMINI_37_PRICING_VERIFIED_AT = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)

OPENROUTER_GLM_5V_TURBO_MODEL_ID = "z-ai/glm-5v-turbo"
OPENROUTER_GLM_5V_TURBO_SOURCE = (
    "https://openrouter.ai/api/v1/models/z-ai/glm-5v-turbo/endpoints"
)
OPENROUTER_GLM_5V_TURBO_PRICING_VERIFIED_AT = datetime(2026, 9, 3, 2, 50, tzinfo=UTC)
OPENROUTER_GLM_5V_TURBO_CATALOG_VERSION = "openrouter-zai-vision-2026-09-03-v1"


GEMINI_STANDARD_PRICING = PricingEntry(
    provider="gemini",
    model=GEMINI_MODEL_ID,
    billing_mode="standard",
    currency="USD",
    input_rates_by_modality={
        "text": Decimal("0.30"),
        "image": Decimal("0.30"),
        "video": Decimal("0.30"),
        "audio": Decimal("0.30"),
    },
    output_rate=Decimal("2.50"),
    effective_from=datetime(2026, 7, 21, tzinfo=UTC),
    verified_at=GEMINI_PRICING_VERIFIED_AT,
    source=GEMINI_PRICING_SOURCE,
    catalog_version=GEMINI_CATALOG_VERSION,
    notes=(
        "Google Developer API Standard paid tier; output rate includes thinking tokens. "
        f"Model capability source: {GEMINI_MODEL_SOURCE}."
    ),
)

GEMINI_25_STANDARD_PRICING = PricingEntry(
    provider="gemini",
    model=GEMINI_25_MODEL_ID,
    billing_mode="standard",
    currency="USD",
    input_rates_by_modality={
        "text": Decimal("0.10"),
        "image": Decimal("0.10"),
        "video": Decimal("0.10"),
        "audio": Decimal("0.30"),
    },
    output_rate=Decimal("0.40"),
    effective_from=datetime(2026, 7, 21, tzinfo=UTC),
    verified_at=GEMINI_PRICING_VERIFIED_AT,
    source=GEMINI_PRICING_SOURCE,
    catalog_version=GEMINI_CATALOG_VERSION,
    notes=(
        "Google Developer API Standard paid tier; output rate includes thinking tokens. "
        f"Model capability source: {GEMINI_25_MODEL_SOURCE}."
    ),
)

GEMINI_37_STANDARD_PRICING = PricingEntry(
    provider="gemini",
    model=GEMINI_37_MODEL_ID,
    billing_mode="standard",
    currency="USD",
    input_rates_by_modality={
        "text": Decimal("0.75"),
        "image": Decimal("0.75"),
        "video": Decimal("0.75"),
        "audio": Decimal("0.75"),
    },
    cached_input_rate=Decimal("0.075"),
    output_rate=Decimal("3.75"),
    effective_from=datetime(2026, 8, 23, tzinfo=UTC),
    verified_at=GEMINI_37_PRICING_VERIFIED_AT,
    source=GEMINI_PRICING_SOURCE,
    catalog_version=GEMINI_37_CATALOG_VERSION,
    notes=(
        "Google Developer API Standard paid tier; cached input rate is $0.075 per "
        "million tokens and output rate includes thinking tokens. "
        "Introductory promotional rates apply through 2026-12-31; reverify before "
        "using after that date. "
        f"Model capability source: {GEMINI_37_MODEL_SOURCE}."
    ),
)

OPENROUTER_GLM_5V_TURBO_STANDARD_PRICING = PricingEntry(
    provider="openrouter",
    model=OPENROUTER_GLM_5V_TURBO_MODEL_ID,
    billing_mode="standard",
    currency="USD",
    input_rates_by_modality={
        "text": Decimal("1.20"),
        "image": Decimal("1.20"),
        "video": Decimal("1.20"),
        "audio": Decimal("1.20"),
    },
    cached_input_rate=Decimal("0.24"),
    output_rate=Decimal("4.00"),
    effective_from=datetime(2026, 4, 1, tzinfo=UTC),
    verified_at=OPENROUTER_GLM_5V_TURBO_PRICING_VERIFIED_AT,
    source=OPENROUTER_GLM_5V_TURBO_SOURCE,
    catalog_version=OPENROUTER_GLM_5V_TURBO_CATALOG_VERSION,
    notes=(
        "OpenRouter exact endpoint snapshot for z-ai/glm-5v-turbo, whose only current "
        "endpoint is Z.AI. Prompt $1.20/M, cache-read $0.24/M, completion $4.00/M. "
        "All uncached input dimensions are conservatively priced at the same published "
        "prompt-token rate; OpenRouter aggregate usage can therefore settle safely even "
        "without a provider-reported text/video token split."
    ),
)


def production_pricing_catalog() -> PricingCatalog:
    """Return exact production pricing entries for explicitly selectable paid models."""

    return PricingCatalog(
        [
            GEMINI_25_STANDARD_PRICING,
            GEMINI_STANDARD_PRICING,
            GEMINI_37_STANDARD_PRICING,
            OPENROUTER_GLM_5V_TURBO_STANDARD_PRICING,
        ]
    )


__all__ = [
    "GEMINI_25_MODEL_ID",
    "GEMINI_25_MODEL_SOURCE",
    "GEMINI_25_STANDARD_PRICING",
    "GEMINI_37_CATALOG_VERSION",
    "GEMINI_37_MODEL_ID",
    "GEMINI_37_MODEL_SOURCE",
    "GEMINI_37_PRICING_VERIFIED_AT",
    "GEMINI_37_STANDARD_PRICING",
    "GEMINI_CATALOG_VERSION",
    "GEMINI_MODEL_ID",
    "GEMINI_PRICING_SOURCE",
    "GEMINI_PRICING_VERIFIED_AT",
    "GEMINI_STANDARD_PRICING",
    "OPENROUTER_GLM_5V_TURBO_CATALOG_VERSION",
    "OPENROUTER_GLM_5V_TURBO_MODEL_ID",
    "OPENROUTER_GLM_5V_TURBO_PRICING_VERIFIED_AT",
    "OPENROUTER_GLM_5V_TURBO_SOURCE",
    "OPENROUTER_GLM_5V_TURBO_STANDARD_PRICING",
    "production_pricing_catalog",
]
