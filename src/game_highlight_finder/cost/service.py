"""Central cost gate API for future paid provider stages."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from game_highlight_finder.config import AppConfig
from game_highlight_finder.cost.calculator import budget_period_for, quote_cost
from game_highlight_finder.cost.fx import FxSnapshot
from game_highlight_finder.cost.ledger import (
    BudgetSummary,
    CostLedger,
    CostSafetyHold,
    LedgerRecord,
)
from game_highlight_finder.cost.models import MICRO_UNITS_PER_THB, CostQuote
from game_highlight_finder.cost.pricing import PricingCatalog
from game_highlight_finder.errors import CostGateError
from game_highlight_finder.providers.base import (
    ProviderRegistry,
    ProviderUsageActual,
    ProviderUsageEstimate,
)


class CostRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=256)
    billing_mode: str = Field(min_length=1, max_length=64)
    stage: str = Field(min_length=1, max_length=64)
    session_id: str | None = Field(default=None, max_length=128)
    usage_estimate: ProviderUsageEstimate
    request_payload: Mapping[str, Any] = Field(default_factory=dict)

    @property
    def request_fingerprint(self) -> str:
        from game_highlight_finder.providers.base import ProviderRequest

        return ProviderRequest(
            call_id=self.call_id,
            provider=self.provider,
            model_id=self.model,
            billing_mode=self.billing_mode,
            stage=self.stage,
            session_id=self.session_id,
            usage_estimate=self.usage_estimate,
            request_payload=self.request_payload,
        ).request_fingerprint


def budget_to_micro_thb(value: object) -> int:
    try:
        decimal_value = Decimal(str(value))
    except Exception as exc:
        raise CostGateError("Monthly budget must be a finite decimal") from exc
    if not decimal_value.is_finite() or decimal_value < 0:
        raise CostGateError("Monthly budget must be a non-negative finite decimal")
    return int((decimal_value * MICRO_UNITS_PER_THB).to_integral_value(rounding=ROUND_FLOOR))


class CostService:
    """Resolve exact contracts, quote conservatively, and gate the ledger atomically."""

    def __init__(
        self,
        config: AppConfig,
        *,
        registry: ProviderRegistry,
        pricing: PricingCatalog,
        fx_snapshot: FxSnapshot | None = None,
    ) -> None:
        self.config = config
        self.registry = registry
        self.pricing = pricing
        self.fx_snapshot = fx_snapshot
        self.ledger = CostLedger(
            self.ledger_path(config),
            budget_micro_thb=budget_to_micro_thb(config.cost.monthly_budget_thb),
        )

    @classmethod
    def from_config(cls, config: AppConfig, *, registry: ProviderRegistry) -> CostService:
        pricing = (
            PricingCatalog.from_file(config.cost.pricing_catalog_path)
            if config.cost.pricing_catalog_path is not None
            else PricingCatalog()
        )
        fx_snapshot = (
            FxSnapshot.from_file(config.cost.fx_snapshot_path)
            if config.cost.fx_snapshot_path is not None
            else None
        )
        return cls(config, registry=registry, pricing=pricing, fx_snapshot=fx_snapshot)

    @staticmethod
    def ledger_path(config: AppConfig) -> Path:
        return (
            config.cost.ledger_path or config.storage.data_dir.resolve() / "cost" / "ledger.sqlite3"
        )

    def set_fx_snapshot(self, snapshot: FxSnapshot) -> None:
        self.fx_snapshot = snapshot

    def quote(self, request: CostRequest, *, now: datetime | None = None) -> CostQuote:
        timestamp = now or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise CostGateError("Cost quote timestamp must be timezone-aware")
        model = self.registry.resolve_exact_or_alias(
            request.provider, request.model, request.billing_mode
        )
        pricing = self.pricing.lookup(
            model.provider,
            model.model_id,
            request.billing_mode,
            now=timestamp,
            max_age_days=self.config.cost.pricing_max_age_days,
        )
        snapshot = self.fx_snapshot
        if snapshot is None:
            raise CostGateError("An explicit fresh FX snapshot is required before reservation")
        snapshot.require_for(
            base_currency=pricing.currency,
            quote_currency="THB",
            now=timestamp,
            max_age_days=self.config.cost.fx_max_age_days,
        )
        return quote_cost(
            provider=model.provider,
            model=model.model_id,
            billing_mode=request.billing_mode,
            usage=request.usage_estimate,
            pricing=pricing,
            fx=snapshot,
            now=timestamp,
            budget_timezone=self.config.cost.budget_timezone,
            safety_factor=self.config.cost.estimate_safety_factor,
        )

    def reserve(
        self,
        request: CostRequest,
        *,
        quote: CostQuote | None = None,
        now: datetime | None = None,
    ) -> LedgerRecord:
        resolved_quote = quote or self.quote(request, now=now)
        resolved_model = self.registry.resolve_exact_or_alias(
            request.provider, request.model, request.billing_mode
        )
        if resolved_quote.provider != resolved_model.provider:
            raise CostGateError("Reservation quote provider does not match request")
        if resolved_quote.model != resolved_model.model_id:
            raise CostGateError("Reservation quote model does not match request")
        if resolved_quote.billing_mode != request.billing_mode:
            raise CostGateError("Reservation quote billing mode does not match request")
        if resolved_quote.usage_estimate.model_dump(
            mode="json"
        ) != request.usage_estimate.model_dump(mode="json"):
            raise CostGateError("Reservation quote usage does not match request")
        timestamp = now or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise CostGateError("Reservation timestamp must be timezone-aware")
        if not resolved_quote.pricing_snapshot.is_fresh(
            now=timestamp, max_age_days=self.config.cost.pricing_max_age_days
        ):
            raise CostGateError("Reservation pricing snapshot is stale")
        fx = FxSnapshot.model_validate(resolved_quote.fx_snapshot)
        fx.require_for(
            base_currency=resolved_quote.pricing_snapshot.currency,
            quote_currency="THB",
            now=timestamp,
            max_age_days=self.config.cost.fx_max_age_days,
        )
        expected_period = budget_period_for(timestamp, self.config.cost.budget_timezone)
        if resolved_quote.budget_period != expected_period:
            raise CostGateError(
                "Reservation quote billing period does not match current budget period"
            )
        return self.ledger.reserve(
            call_id=request.call_id,
            request_fingerprint=request.request_fingerprint,
            quote=resolved_quote,
            stage=request.stage,
            session_id=request.session_id,
            now=now,
        )

    def mark_in_flight(
        self, call_id: str, *, provider_request_id: str | None = None
    ) -> LedgerRecord:
        return self.ledger.mark_in_flight(call_id, provider_request_id=provider_request_id)

    def settle(
        self,
        call_id: str,
        actual_usage: ProviderUsageActual | ProviderUsageEstimate,
        *,
        provider_request_id: str | None = None,
    ) -> LedgerRecord:
        return self.ledger.settle(call_id, actual_usage, provider_request_id=provider_request_id)

    def release(
        self, call_id: str, *, confirmed_no_dispatch: bool = False
    ) -> LedgerRecord:
        return self.ledger.release(call_id, confirmed_no_dispatch=confirmed_no_dispatch)

    def mark_ambiguous(self, call_id: str, reason: str) -> LedgerRecord:
        return self.ledger.mark_ambiguous(call_id, reason)

    def reconcile(
        self,
        call_id: str,
        *,
        actual_usage: ProviderUsageActual | ProviderUsageEstimate | None = None,
        release_confirmed: bool = False,
        provider_request_id: str | None = None,
    ) -> LedgerRecord:
        return self.ledger.reconcile(
            call_id,
            actual_usage=actual_usage,
            release_confirmed=release_confirmed,
            provider_request_id=provider_request_id,
        )

    def safety_hold(self) -> CostSafetyHold | None:
        return self.ledger.safety_hold()

    def get_safety_hold(self) -> CostSafetyHold | None:
        return self.ledger.safety_hold()

    def acknowledge_safety_hold(self, reason: str, *, now: datetime | None = None) -> None:
        self.ledger.acknowledge_safety_hold(reason, now=now)

    def clear_safety_hold(self, reason: str, *, now: datetime | None = None) -> None:
        self.ledger.acknowledge_safety_hold(reason, now=now)

    def summary(self, *, now: datetime | None = None) -> BudgetSummary:
        timestamp = now or datetime.now(UTC)
        period = budget_period_for(timestamp, self.config.cost.budget_timezone)
        return self.ledger.summary(period)

    def calls(self, *, now: datetime | None = None) -> tuple[LedgerRecord, ...]:
        timestamp = now or datetime.now(UTC)
        period = budget_period_for(timestamp, self.config.cost.budget_timezone)
        return self.ledger.list_calls(budget_period=period)
