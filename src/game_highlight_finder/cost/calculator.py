"""Deterministic Decimal-based conservative cost arithmetic."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_CEILING, Decimal

from game_highlight_finder.cost.fx import FxSnapshot
from game_highlight_finder.cost.models import (
    MICRO_UNITS_PER_THB,
    CostQuote,
    PricingEntry,
    parse_decimal,
)
from game_highlight_finder.errors import CostGateError
from game_highlight_finder.providers.base import ProviderUsageEstimate
from game_highlight_finder.timezones import configured_timezone

_DIMENSION_TO_MODALITY = {
    "input_text_tokens": "text",
    "input_image_tokens": "image",
    "input_video_tokens": "video",
    "input_audio_tokens": "audio",
}


def budget_period_for(timestamp: datetime, timezone_name: str) -> str:
    if timestamp.tzinfo is None:
        raise CostGateError("Budget timestamps must be timezone-aware")
    try:
        local = timestamp.astimezone(configured_timezone(timezone_name))
    except Exception as exc:
        raise CostGateError(f"Invalid budget timezone: {timezone_name}") from exc
    return local.strftime("%Y-%m")


def ceil_micro_thb(value: Decimal) -> int:
    if value < 0 or not value.is_finite():
        raise CostGateError("Cost arithmetic produced an invalid negative or non-finite amount")
    return int((value * MICRO_UNITS_PER_THB).to_integral_value(rounding=ROUND_CEILING))


def _usage_base_currency(usage: ProviderUsageEstimate, pricing: PricingEntry) -> Decimal:
    total = pricing.request_fee
    dimensions = usage.as_dimensions()
    for dimension, count in dimensions.items():
        if count == 0:
            continue
        if dimension == "output_tokens":
            rate = pricing.output_rate
        elif dimension == "cached_input_tokens":
            if pricing.cached_input_rate is None:
                raise CostGateError("Pricing does not define a cached-input rate")
            rate = pricing.cached_input_rate
        else:
            modality = _DIMENSION_TO_MODALITY.get(dimension)
            if modality is None or modality not in pricing.input_rates_by_modality:
                raise CostGateError(f"Pricing does not support usage dimension: {dimension}")
            rate = pricing.input_rates_by_modality[modality]
        total += Decimal(count) * rate / Decimal(1_000_000)
    if total < 0 or not total.is_finite():
        raise CostGateError("Pricing arithmetic produced an invalid amount")
    return total


def calculate_cost(
    usage: ProviderUsageEstimate,
    pricing: PricingEntry,
    fx: FxSnapshot,
    *,
    budget_currency: str = "THB",
    safety_factor: Decimal | str | int | float = Decimal("1"),
) -> int:
    """Return conservative integer micro-THB for one usage bound."""

    if pricing.currency != fx.base_currency or fx.quote_currency != budget_currency:
        raise CostGateError(
            f"No compatible currency path for pricing {pricing.currency} and FX "
            f"{fx.base_currency}/{fx.quote_currency}"
        )
    factor = parse_decimal(safety_factor, field="safety factor")
    if factor < 1:
        raise CostGateError("Safety factor must be at least 1")
    base = _usage_base_currency(usage, pricing)
    return ceil_micro_thb(base * fx.rate * factor)


def quote_cost(
    *,
    provider: str,
    model: str,
    billing_mode: str,
    usage: ProviderUsageEstimate,
    pricing: PricingEntry,
    fx: FxSnapshot,
    now: datetime,
    budget_timezone: str = "Asia/Bangkok",
    safety_factor: Decimal | str | int | float = Decimal("1.20"),
    budget_currency: str = "THB",
) -> CostQuote:
    if pricing.currency != fx.base_currency or fx.quote_currency != budget_currency:
        raise CostGateError(
            f"No compatible currency path for pricing {pricing.currency} and FX "
            f"{fx.base_currency}/{fx.quote_currency}"
        )
    base_cost = calculate_cost(
        usage, pricing, fx, budget_currency=budget_currency, safety_factor=Decimal("1")
    )
    reserved = calculate_cost(
        usage, pricing, fx, budget_currency=budget_currency, safety_factor=safety_factor
    )
    return CostQuote(
        provider=provider,
        model=model,
        billing_mode=billing_mode,
        budget_period=budget_period_for(now, budget_timezone),
        usage_estimate=usage,
        pricing_snapshot=pricing,
        fx_snapshot=fx.snapshot(),
        safety_factor=parse_decimal(safety_factor, field="safety factor"),
        base_cost_micro_thb=base_cost,
        reserved_cost_micro_thb=reserved,
    )
