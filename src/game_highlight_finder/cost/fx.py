"""Explicit, local FX snapshots. No network refresh is performed by M4."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator, model_validator

from game_highlight_finder.cost.models import CostModel, parse_decimal
from game_highlight_finder.errors import CostGateError


class FxSnapshot(CostModel):
    base_currency: str = Field(min_length=3, max_length=3)
    quote_currency: str = Field(min_length=3, max_length=3)
    rate: Decimal
    captured_at: datetime
    source: str = Field(min_length=1, max_length=500)
    snapshot_version: str = Field(default="fx-v1", min_length=1, max_length=64)

    @field_validator("rate", mode="before")
    @classmethod
    def strict_rate(cls, value: object) -> Decimal:
        parsed = parse_decimal(value, field="FX rate")
        if parsed <= 0:
            raise ValueError("FX rate must be positive")
        return parsed

    @model_validator(mode="after")
    def validate_snapshot(self) -> FxSnapshot:
        for field in ("base_currency", "quote_currency"):
            value = getattr(self, field)
            if len(value) != 3 or not value.isalpha() or value.upper() != value:
                raise ValueError("FX currency codes must be three-letter uppercase values")
        if self.captured_at.tzinfo is None:
            raise ValueError("FX captured_at must be timezone-aware")
        if self.base_currency == self.quote_currency and self.rate != 1:
            raise ValueError("same-currency FX snapshots must use rate 1")
        return self

    def is_fresh(self, *, now: datetime, max_age_days: int) -> bool:
        if now.tzinfo is None:
            raise ValueError("freshness checks require a timezone-aware timestamp")
        if self.captured_at > now:
            return False
        return (now - self.captured_at).total_seconds() <= max_age_days * 86_400

    def require_for(
        self, *, base_currency: str, quote_currency: str, now: datetime, max_age_days: int
    ) -> None:
        if self.base_currency != base_currency or self.quote_currency != quote_currency:
            raise CostGateError(
                f"FX snapshot does not cover {base_currency}/{quote_currency}: "
                f"{self.base_currency}/{self.quote_currency}"
            )
        if not self.is_fresh(now=now, max_age_days=max_age_days):
            raise CostGateError("FX snapshot is missing, stale, or outside its effective time")

    def snapshot(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_file(cls, path: Path) -> FxSnapshot:
        try:
            return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"Cannot load FX snapshot: {path}") from exc
