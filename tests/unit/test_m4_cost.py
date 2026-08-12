from __future__ import annotations

import multiprocessing
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError
from typer.testing import CliRunner

from game_highlight_finder.cli import app
from game_highlight_finder.config import AppConfig, CostConfig, StorageConfig
from game_highlight_finder.cost.calculator import budget_period_for, calculate_cost, quote_cost
from game_highlight_finder.cost.fx import FxSnapshot
from game_highlight_finder.cost.ledger import CostLedger, LifecycleStatus
from game_highlight_finder.cost.models import PricingEntry
from game_highlight_finder.cost.pricing import PricingCatalog
from game_highlight_finder.cost.service import CostRequest, CostService
from game_highlight_finder.errors import (
    BudgetExceededError,
    CostGateError,
    CostIntegrityError,
    CostSafetyHoldError,
    StorageError,
)
from game_highlight_finder.providers.base import (
    MAX_USAGE_TOKENS_PER_DIMENSION,
    ProviderCapabilities,
    ProviderDescriptor,
    ProviderModel,
    ProviderRegistry,
    ProviderUsageEstimate,
)
from game_highlight_finder.providers.fake import FakeProvider

runner = CliRunner()
NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def _pricing(*, verified_at: datetime = NOW, input_rate: str = "0.10") -> PricingEntry:
    return PricingEntry(
        provider="fake",
        model="fake-model",
        billing_mode="standard",
        currency="USD",
        input_rates_by_modality={"text": Decimal(input_rate)},
        output_rate=Decimal("0.20"),
        effective_from=NOW - timedelta(days=2),
        verified_at=verified_at,
        source="synthetic-test-price",
        catalog_version="test-v1",
    )


def _fx(*, captured_at: datetime = NOW, rate: str = "36") -> FxSnapshot:
    return FxSnapshot(
        base_currency="USD",
        quote_currency="THB",
        rate=Decimal(rate),
        captured_at=captured_at,
        source="synthetic-test-fx",
    )


def _registry() -> ProviderRegistry:
    return ProviderRegistry(
        [
            ProviderDescriptor(
                provider="fake",
                models=(
                    ProviderModel(
                        provider="fake",
                        model_id="fake-model",
                        aliases=("fake-cheap",),
                        billing_modes=("standard", "batch"),
                        capabilities=ProviderCapabilities(usage_metadata=True),
                    ),
                ),
            )
        ]
    )


def _service(tmp_path: Path, *, budget: str = "100") -> CostService:
    config = AppConfig(
        storage=StorageConfig(data_dir=tmp_path / "data"),
        cost=CostConfig(
            monthly_budget_thb=Decimal(budget),
            pricing_max_age_days=30,
            fx_max_age_days=30,
        ),
    )
    return CostService(
        config,
        registry=_registry(),
        pricing=PricingCatalog([_pricing()]),
        fx_snapshot=_fx(),
    )


def _request(call_id: str, *, tokens: int = 100_000, model: str = "fake-model") -> CostRequest:
    return CostRequest(
        call_id=call_id,
        provider="fake",
        model=model,
        billing_mode="standard",
        stage="scout",
        session_id="session-test",
        usage_estimate=ProviderUsageEstimate(input_text_tokens=tokens),
        request_payload={"fixture": "offline"},
    )


def _worker_reserve(
    path: str, budget_micro_thb: int, quote_payload: dict[str, object], result_queue: object
) -> None:
    from game_highlight_finder.cost.models import CostQuote

    try:
        ledger = CostLedger(Path(path), budget_micro_thb=budget_micro_thb)
        quote = CostQuote.model_validate(quote_payload)
        record = ledger.reserve(
            call_id=f"worker-{multiprocessing.current_process().pid}",
            request_fingerprint="worker-fingerprint-" + str(multiprocessing.current_process().pid),
            quote=quote,
            stage="scout",
            now=NOW,
        )
        result_queue.put(("ok", record.call_id))  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover - exercised in child process
        result_queue.put((type(exc).__name__, str(exc)))  # type: ignore[attr-defined]


def test_config_has_m4_default_budget_and_timezone(tmp_path: Path) -> None:
    config = AppConfig(storage=StorageConfig(data_dir=tmp_path / "data"))
    assert config.cost.monthly_budget_thb == Decimal("100.00")
    assert config.cost.budget_timezone == "Asia/Bangkok"


def test_valid_price_and_fx_produce_deterministic_conservative_quote() -> None:
    usage = ProviderUsageEstimate(input_text_tokens=100_000, output_tokens=100_000)
    pricing = _pricing()
    fx = _fx()
    first = quote_cost(
        provider="fake",
        model="fake-model",
        billing_mode="standard",
        usage=usage,
        pricing=pricing,
        fx=fx,
        now=NOW,
        safety_factor="1.20",
    )
    second = quote_cost(
        provider="fake",
        model="fake-model",
        billing_mode="standard",
        usage=usage,
        pricing=pricing,
        fx=fx,
        now=NOW,
        safety_factor="1.20",
    )
    assert first.reserved_cost_micro_thb == second.reserved_cost_micro_thb == 1_296_000
    assert first.base_cost_micro_thb == 1_080_000


@pytest.mark.parametrize(
    "factory",
    [
        lambda: PricingEntry(
            provider="fake",
            model="fake-model",
            billing_mode="standard",
            input_rates_by_modality={"text": Decimal("NaN")},
            effective_from=NOW,
            verified_at=NOW,
            source="test",
            catalog_version="v1",
        ),
        lambda: _fx(rate="0"),
        lambda: _fx(rate="-1"),
    ],
)
def test_malformed_or_nonpositive_rates_fail_closed(factory: object) -> None:
    with pytest.raises((PydanticValidationError, ValueError)):
        factory()  # type: ignore[operator]


@pytest.mark.parametrize(
    "field_name",
    [
        "input_text_tokens",
        "input_image_tokens",
        "input_video_tokens",
        "input_audio_tokens",
        "cached_input_tokens",
        "output_tokens",
    ],
)
def test_untrusted_usage_counts_have_an_absolute_upper_bound(field_name: str) -> None:
    with pytest.raises(PydanticValidationError, match="less than or equal"):
        ProviderUsageEstimate(**{field_name: MAX_USAGE_TOKENS_PER_DIMENSION + 1})


def test_missing_output_rate_fails_closed_for_output_usage() -> None:
    pricing = PricingEntry(
        provider="fake",
        model="fake-model",
        billing_mode="standard",
        input_rates_by_modality={"text": Decimal("0.10")},
        effective_from=NOW,
        verified_at=NOW,
        source="test",
        catalog_version="v1",
    )
    assert pricing.output_rate is None
    with pytest.raises(CostGateError, match="output rate"):
        calculate_cost(
            ProviderUsageEstimate(output_tokens=1),
            pricing,
            _fx(),
        )


def test_unknown_provider_model_mode_and_price_fail_closed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(CostGateError, match="Unknown provider"):
        service.quote(
            _request("unknown-provider").model_copy(update={"provider": "missing"}), now=NOW
        )
    with pytest.raises(CostGateError, match="Unknown exact model"):
        service.quote(_request("unknown-model", model="missing-model"), now=NOW)
    with pytest.raises(CostGateError, match="Unsupported billing mode"):
        service.quote(
            _request("unknown-mode").model_copy(update={"billing_mode": "realtime"}), now=NOW
        )
    no_price = CostService(
        service.config,
        registry=_registry(),
        pricing=PricingCatalog(),
        fx_snapshot=_fx(),
    )
    with pytest.raises(CostGateError, match="No exact pricing"):
        no_price.quote(_request("missing-price"), now=NOW)


def test_stale_price_and_fx_fail_closed(tmp_path: Path) -> None:
    stale_price = CostService(
        _service(tmp_path).config,
        registry=_registry(),
        pricing=PricingCatalog([_pricing(verified_at=NOW - timedelta(days=31))]),
        fx_snapshot=_fx(),
    )
    with pytest.raises(CostGateError, match="stale"):
        stale_price.quote(_request("stale-price"), now=NOW)
    stale_fx = _service(tmp_path)
    stale_fx.set_fx_snapshot(_fx(captured_at=NOW - timedelta(days=31)))
    with pytest.raises(CostGateError, match="stale"):
        stale_fx.quote(_request("stale-fx"), now=NOW)
    missing_fx = _service(tmp_path)
    missing_fx.fx_snapshot = None
    with pytest.raises(CostGateError, match="FX snapshot"):
        missing_fx.quote(_request("missing-fx"), now=NOW)


def test_lifecycle_release_settle_and_idempotency(tmp_path: Path) -> None:
    service = _service(tmp_path)
    request = _request("call-lifecycle")
    quote = service.quote(request, now=NOW)
    reserved = service.reserve(request, quote=quote, now=NOW)
    duplicate = service.reserve(request, quote=quote, now=NOW)
    assert reserved.call_id == duplicate.call_id
    assert reserved.status is LifecycleStatus.RESERVED
    service.mark_in_flight(request.call_id, provider_request_id="provider-1")
    actual = ProviderUsageEstimate(input_text_tokens=50_000)
    settled = service.settle(request.call_id, actual, provider_request_id="provider-1")
    assert settled.status is LifecycleStatus.SETTLED
    assert settled.settled_cost_micro_thb is not None
    repeated = service.settle(request.call_id, actual, provider_request_id="provider-1")
    assert repeated.settled_cost_micro_thb == settled.settled_cost_micro_thb
    with pytest.raises(CostGateError, match="Conflicting settlement"):
        service.settle(request.call_id, ProviderUsageEstimate(input_text_tokens=60_000))

    release_request = _request("call-release")
    service.reserve(release_request, now=NOW)
    service.release(release_request.call_id)
    assert service.ledger.get(release_request.call_id).status is LifecycleStatus.RELEASED


def test_budget_gate_exact_limit_and_release(tmp_path: Path) -> None:
    service = _service(tmp_path, budget="0.432")
    first = _request("call-budget", tokens=100_000)
    service.reserve(first, now=NOW)
    with pytest.raises(BudgetExceededError):
        service.reserve(_request("call-blocked", tokens=1), now=NOW)
    service.release(first.call_id)
    service.reserve(_request("call-after-release", tokens=100_000), now=NOW)


def test_ambiguous_call_retains_exposure_and_requires_reconciliation(tmp_path: Path) -> None:
    service = _service(tmp_path)
    request = _request("call-ambiguous")
    service.reserve(request, now=NOW)
    service.mark_in_flight(request.call_id)
    service.mark_ambiguous(request.call_id, "send timeout after dispatch boundary")
    reopened = CostLedger(
        service.ledger.path,
        budget_micro_thb=service.ledger.budget_micro_thb,
    )
    record = reopened.get(request.call_id)
    assert record.status is LifecycleStatus.AMBIGUOUS
    assert reopened.summary("2026-08").ambiguous_micro_thb == record.reserved_cost_micro_thb
    with pytest.raises(CostGateError, match="must be reconciled"):
        reopened.reserve(
            call_id=request.call_id,
            request_fingerprint=request.request_fingerprint,
            quote=service.quote(request, now=NOW),
            stage=request.stage,
            session_id=request.session_id,
            now=NOW,
        )
    with pytest.raises(CostGateError, match="cannot be released"):
        reopened.release(request.call_id)
    reopened.reconcile(request.call_id, release_confirmed=True)
    assert reopened.get(request.call_id).status is LifecycleStatus.RELEASED


def test_actual_overage_is_persisted_and_raises_integrity_error(tmp_path: Path) -> None:
    service = _service(tmp_path)
    request = _request("call-overage", tokens=1)
    service.reserve(request, now=NOW)
    service.mark_in_flight(request.call_id)
    with pytest.raises(CostIntegrityError, match="exceeded"):
        service.settle(request.call_id, ProviderUsageEstimate(input_text_tokens=200_000))
    record = service.ledger.get(request.call_id)
    assert record.status is LifecycleStatus.SETTLED
    assert (record.settled_cost_micro_thb or 0) > record.reserved_cost_micro_thb
    assert record.integrity_error is not None
    hold = service.safety_hold()
    assert hold is not None
    assert hold.active is True
    assert hold.call_id == request.call_id
    assert service.summary(now=NOW).safety_hold_active is True
    reopened = CostLedger(
        service.ledger.path,
        budget_micro_thb=service.ledger.budget_micro_thb,
    )
    assert reopened.safety_hold() == hold
    with pytest.raises(CostSafetyHoldError):
        reopened.reserve(
            call_id="call-blocked-after-reopen",
            request_fingerprint="blocked-after-reopen",
            quote=service.quote(_request("call-blocked-after-reopen"), now=NOW),
            stage="scout",
            now=NOW,
        )
    with pytest.raises(CostSafetyHoldError, match="safety hold"):
        service.reserve(_request("call-blocked-by-overage"), now=NOW)
    service.acknowledge_safety_hold("reviewed provider billing evidence", now=NOW)
    assert service.safety_hold() is None
    service.reserve(_request("call-after-ack"), now=NOW)


def test_month_boundary_uses_configured_timezone() -> None:
    before = datetime(2026, 8, 31, 16, 59, 59, tzinfo=UTC)
    after = datetime(2026, 8, 31, 17, 0, 0, tzinfo=UTC)
    assert budget_period_for(before, "Asia/Bangkok") == "2026-08"
    assert budget_period_for(after, "Asia/Bangkok") == "2026-09"


def test_sqlite_migration_and_unknown_newer_schema_fail_safely(tmp_path: Path) -> None:
    path = tmp_path / "cost" / "ledger.sqlite3"
    ledger = CostLedger(path, budget_micro_thb=100_000_000)
    assert ledger.path.is_file()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT value_json FROM ledger_control WHERE key='safety_hold'"
            ).fetchone()
            is None
        )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE schema_migrations SET version=999 "
            "WHERE version=(SELECT MAX(version) FROM schema_migrations)"
        )
        connection.commit()
    with pytest.raises(StorageError, match="newer than supported"):
        CostLedger(path, budget_micro_thb=100_000_000)


def test_v1_ledger_is_upgraded_with_safety_control_table(tmp_path: Path) -> None:
    path = tmp_path / "cost" / "ledger.sqlite3"
    CostLedger(path, budget_micro_thb=100_000_000)
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version=2")
        connection.execute("DROP TABLE ledger_control")
        connection.commit()
    upgraded = CostLedger(path, budget_micro_thb=100_000_000)
    assert upgraded.safety_hold() is None
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='ledger_control'"
            ).fetchone()
            is not None
        )


def test_v1_overage_is_migrated_into_a_global_safety_hold(tmp_path: Path) -> None:
    path = tmp_path / "cost" / "ledger.sqlite3"
    ledger = CostLedger(path, budget_micro_thb=100_000_000)
    quote = quote_cost(
        provider="fake",
        model="fake-model",
        billing_mode="standard",
        usage=ProviderUsageEstimate(input_text_tokens=1),
        pricing=_pricing(),
        fx=_fx(),
        now=NOW,
        safety_factor="1",
    )
    ledger.reserve(
        call_id="v1-overage",
        request_fingerprint="v1-overage-fingerprint",
        quote=quote,
        stage="scout",
        now=NOW,
    )
    ledger.mark_in_flight("v1-overage", now=NOW)
    with pytest.raises(CostIntegrityError):
        ledger.settle("v1-overage", ProviderUsageEstimate(input_text_tokens=200_000), now=NOW)
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version=2")
        connection.execute("DROP TABLE ledger_control")
        connection.commit()
    upgraded = CostLedger(path, budget_micro_thb=100_000_000)
    hold = upgraded.safety_hold()
    assert hold is not None
    assert hold.call_id == "v1-overage"


def test_fake_provider_is_deterministic_and_offline() -> None:
    provider = FakeProvider()
    request = _request("fake-call")
    from game_highlight_finder.providers.base import ProviderRequest

    provider_request = ProviderRequest(
        call_id=request.call_id,
        provider="fake",
        model_id="fake-model",
        billing_mode=request.billing_mode,
        stage=request.stage,
        session_id=request.session_id,
        usage_estimate=request.usage_estimate,
        request_payload=request.request_payload,
    )
    first = provider.execute(provider_request)
    second = provider.execute(provider_request)
    assert first.provider_request_id == second.provider_request_id
    assert first.usage.input_text_tokens == request.usage_estimate.input_text_tokens


def test_cost_cli_status_initializes_only_when_explicitly_requested(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--data-dir", str(tmp_path / "data"), "cost", "status"])
    assert result.exit_code == 0
    assert "Monthly hard cap: ฿100.00" in result.stdout
    assert "Asia/Bangkok" in result.stdout


def test_concurrent_reservations_cannot_cross_hard_cap(tmp_path: Path) -> None:
    path = tmp_path / "cost" / "ledger.sqlite3"
    ledger = CostLedger(path, budget_micro_thb=1_000_000)
    quote = quote_cost(
        provider="fake",
        model="fake-model",
        billing_mode="standard",
        usage=ProviderUsageEstimate(input_text_tokens=100_000),
        pricing=_pricing(),
        fx=_fx(),
        now=NOW,
        safety_factor="1",
    )
    assert quote.reserved_cost_micro_thb == 360_000
    # Deliberately use a smaller synthetic budget than the quote so all workers
    # contend for the same atomic decision and none can silently pass.
    budget_micro_thb = quote.reserved_cost_micro_thb * 2
    ledger = CostLedger(path, budget_micro_thb=budget_micro_thb)
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(
            target=_worker_reserve,
            args=(str(path), budget_micro_thb, quote.model_dump(mode="json"), queue),
        )
        for _ in range(3)
    ]
    for process in processes:
        process.start()
    results = [queue.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(timeout=30)
    assert sum(result[0] == "ok" for result in results) == 2
    assert sum(result[0] == "BudgetExceededError" for result in results) == 1
    summary = ledger.summary("2026-08")
    assert summary.exposure_micro_thb <= budget_micro_thb
