"""Validated money, pricing, and quote snapshots used by the cost boundary."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from game_highlight_finder.providers.base import ProviderUsageActual, ProviderUsageEstimate

MICRO_UNITS_PER_THB = 1_000_000


class CostModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


def parse_decimal(value: object, *, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"{field} must be a finite decimal")
    try:
        converted = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not converted.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return converted


class Money(CostModel):
    """Authoritative integer micro-unit money value."""

    micro_thb: int = Field(ge=0)

    @field_validator("micro_thb", mode="before")
    @classmethod
    def strict_micro_units(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("micro-THB must be an integer")
        return value

    @property
    def thb(self) -> Decimal:
        return Decimal(self.micro_thb) / MICRO_UNITS_PER_THB

    def display(self) -> str:
        return f"฿{self.thb:.2f}"


class PricingEntry(CostModel):
    """One exact, auditable price entry; rates are currency units per million."""

    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(
        min_length=1,
        max_length=256,
        validation_alias=AliasChoices("model", "model_id"),
    )
    billing_mode: str = Field(min_length=1, max_length=64)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    input_rates_by_modality: dict[str, Decimal] = Field(
        default_factory=dict,
        max_length=16,
        validation_alias=AliasChoices("input_rates_by_modality", "rates"),
    )
    cached_input_rate: Decimal | None = None
    output_rate: Decimal = Decimal("0")
    request_fee: Decimal = Decimal("0")
    effective_from: datetime = Field(
        validation_alias=AliasChoices("effective_from", "effective_at")
    )
    effective_until: datetime | None = None
    verified_at: datetime = Field(validation_alias=AliasChoices("verified_at", "retrieved_at"))
    source: str = Field(min_length=1, max_length=500)
    catalog_version: str = Field(min_length=1, max_length=64)
    notes: str = Field(default="", max_length=500)

    @field_validator("input_rates_by_modality", mode="before")
    @classmethod
    def strict_input_rates(cls, value: object) -> dict[str, Decimal]:
        if not isinstance(value, dict):
            raise ValueError("input rates must be an object")
        result: dict[str, Decimal] = {}
        for key, rate in value.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("input rate modality names must be non-empty strings")
            parsed = parse_decimal(rate, field=f"input rate {key}")
            if parsed < 0:
                raise ValueError("input rates cannot be negative")
            result[key.strip()] = parsed
        return result

    @field_validator("cached_input_rate", mode="before")
    @classmethod
    def strict_cached_input_rate(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        parsed = parse_decimal(value, field="pricing rate")
        if parsed < 0:
            raise ValueError("pricing rates cannot be negative")
        return parsed

    @field_validator("output_rate", "request_fee", mode="before")
    @classmethod
    def strict_rates(cls, value: object) -> Decimal:
        parsed = parse_decimal(value, field="pricing rate")
        if parsed < 0:
            raise ValueError("pricing rates cannot be negative")
        return parsed

    @model_validator(mode="after")
    def validate_dates_and_currency(self) -> PricingEntry:
        if self.effective_from.tzinfo is None or self.verified_at.tzinfo is None:
            raise ValueError("pricing timestamps must be timezone-aware")
        if self.effective_until is not None and (
            self.effective_until.tzinfo is None or self.effective_until <= self.effective_from
        ):
            raise ValueError("pricing effective_until must be after effective_from")
        if (
            len(self.currency) != 3
            or not self.currency.isalpha()
            or self.currency.upper() != self.currency
        ):
            raise ValueError("pricing currency must be a three-letter uppercase code")
        if not self.input_rates_by_modality and self.output_rate == 0 and self.request_fee == 0:
            raise ValueError("pricing entry must contain at least one non-zero rate")
        return self

    @property
    def model_id(self) -> str:
        return self.model

    def is_fresh(self, *, now: datetime, max_age_days: int) -> bool:
        if now.tzinfo is None:
            raise ValueError("freshness checks require a timezone-aware timestamp")
        if self.verified_at > now:
            return False
        age_seconds = (now - self.verified_at).total_seconds()
        if age_seconds > max_age_days * 86_400:
            return False
        if now < self.effective_from:
            return False
        return self.effective_until is None or now < self.effective_until

    def snapshot(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_file(cls, path: Path) -> PricingEntry:
        try:
            return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"Cannot load pricing snapshot: {path}") from exc


class CostQuote(CostModel):
    provider: str
    model: str
    billing_mode: str
    budget_period: str
    usage_estimate: ProviderUsageEstimate
    pricing_snapshot: PricingEntry
    fx_snapshot: dict[str, Any]
    safety_factor: Decimal = Decimal("1")
    base_cost_micro_thb: int = Field(ge=0)
    reserved_cost_micro_thb: int = Field(ge=0)

    @field_validator("safety_factor", mode="before")
    @classmethod
    def strict_safety_factor(cls, value: object) -> Decimal:
        parsed = parse_decimal(value, field="safety factor")
        if parsed < 1:
            raise ValueError("safety factor must be at least 1")
        return parsed

    @field_validator("base_cost_micro_thb", "reserved_cost_micro_thb", mode="before")
    @classmethod
    def strict_cost_units(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("cost units must be integers")
        return value

    @property
    def reserved_money(self) -> Money:
        return Money(micro_thb=self.reserved_cost_micro_thb)


__all__ = [
    "MICRO_UNITS_PER_THB",
    "CostQuote",
    "Money",
    "PricingEntry",
    "ProviderUsageActual",
    "ProviderUsageEstimate",
    "parse_decimal",
]
