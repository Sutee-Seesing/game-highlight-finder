"""Versioned production pricing snapshots verified from Google's official page.

The catalog is intentionally small: M5 uses one exact Standard model entry and
does not silently fall back to another model or billing tier.  FX remains an
explicit user-supplied snapshot in accordance with the M4 policy.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from game_highlight_finder.cost.models import PricingEntry
from game_highlight_finder.cost.pricing import PricingCatalog

GEMINI_MODEL_ID = "gemini-3.5-flash-lite"
GEMINI_PRICING_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing"
GEMINI_MODEL_SOURCE = "https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite"
# The checkout's validation clock is 2026-08-13 Asia/Bangkok (2026-08-12
# afternoon UTC).  Keep the snapshot behind that clock so freshness checks do
# not accidentally treat a just-verified entry as future-dated.
GEMINI_PRICING_VERIFIED_AT = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
GEMINI_CATALOG_VERSION = "google-gemini-2026-08-13-v1"


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


def production_pricing_catalog() -> PricingCatalog:
    """Return a fresh immutable-in-practice catalog for the exact M5 model."""

    return PricingCatalog([GEMINI_STANDARD_PRICING])


__all__ = [
    "GEMINI_CATALOG_VERSION",
    "GEMINI_MODEL_ID",
    "GEMINI_PRICING_SOURCE",
    "GEMINI_PRICING_VERIFIED_AT",
    "GEMINI_STANDARD_PRICING",
    "production_pricing_catalog",
]
