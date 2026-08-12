"""Exact versioned pricing catalog with explicit freshness checks."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from game_highlight_finder.cost.models import PricingEntry
from game_highlight_finder.errors import CostGateError


class PricingCatalog:
    def __init__(self, entries: Iterable[PricingEntry] = ()) -> None:
        self._entries: dict[tuple[str, str, str], PricingEntry] = {}
        for entry in entries:
            self.add(entry)

    def add(self, entry: PricingEntry) -> None:
        key = (entry.provider, entry.model, entry.billing_mode)
        if key in self._entries:
            raise CostGateError(
                "Duplicate exact pricing entry: "
                f"{entry.provider}/{entry.model}/{entry.billing_mode}"
            )
        self._entries[key] = entry

    def lookup(
        self,
        provider: str,
        model: str,
        billing_mode: str,
        *,
        now: datetime | None = None,
        max_age_days: int | None = None,
    ) -> PricingEntry:
        entry = self._entries.get((provider, model, billing_mode))
        if entry is None:
            raise CostGateError(f"No exact pricing entry for {provider}/{model}/{billing_mode}")
        if max_age_days is not None:
            check_time = now or datetime.now(UTC)
            if not entry.is_fresh(now=check_time, max_age_days=max_age_days):
                raise CostGateError(
                    f"Pricing entry is stale or not effective: {provider}/{model}/{billing_mode}"
                )
        return entry

    def entries(self) -> tuple[PricingEntry, ...]:
        return tuple(self._entries.values())

    @classmethod
    def from_file(cls, path: Path) -> PricingCatalog:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            entries = payload["entries"] if isinstance(payload, dict) else payload
            if not isinstance(entries, list):
                raise ValueError("pricing catalog entries must be a list")
            return cls(PricingEntry.model_validate(item) for item in entries)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise CostGateError(f"Cannot load pricing catalog: {path}") from exc
