"""SQLite cost ledger with transactional reservations and explicit lifecycle states."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from game_highlight_finder.cost.calculator import calculate_cost
from game_highlight_finder.cost.fx import FxSnapshot
from game_highlight_finder.cost.models import CostQuote, PricingEntry
from game_highlight_finder.errors import (
    BudgetExceededError,
    CostGateError,
    CostIntegrityError,
    StorageError,
)
from game_highlight_finder.providers.base import ProviderUsageActual, ProviderUsageEstimate

CURRENT_SCHEMA_VERSION = 1
ACTIVE_STATES = ("RESERVED", "IN_FLIGHT", "AMBIGUOUS")


class LifecycleStatus(StrEnum):
    RESERVED = "RESERVED"
    IN_FLIGHT = "IN_FLIGHT"
    SETTLED = "SETTLED"
    RELEASED = "RELEASED"
    AMBIGUOUS = "AMBIGUOUS"


class LedgerRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str
    request_fingerprint: str
    session_id: str | None
    stage: str
    provider: str
    model: str
    billing_mode: str
    budget_period: str
    status: LifecycleStatus
    estimated_usage: ProviderUsageEstimate
    actual_usage: ProviderUsageActual | None = None
    pricing_snapshot: PricingEntry
    fx_snapshot: FxSnapshot
    reserved_cost_micro_thb: int = Field(ge=0)
    settled_cost_micro_thb: int | None = Field(default=None, ge=0)
    provider_request_id: str | None = None
    created_at: datetime
    updated_at: datetime
    ambiguity_reason: str | None = None
    integrity_error: str | None = None

    @property
    def exposure_micro_thb(self) -> int:
        if self.status is LifecycleStatus.SETTLED:
            return self.settled_cost_micro_thb or 0
        if self.status in {
            LifecycleStatus.RESERVED,
            LifecycleStatus.IN_FLIGHT,
            LifecycleStatus.AMBIGUOUS,
        }:
            return self.reserved_cost_micro_thb
        return 0


class BudgetSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    budget_period: str
    budget_micro_thb: int = Field(ge=0)
    settled_micro_thb: int = Field(ge=0)
    reserved_micro_thb: int = Field(ge=0)
    in_flight_micro_thb: int = Field(ge=0)
    ambiguous_micro_thb: int = Field(ge=0)
    available_micro_thb: int = Field(ge=0)
    ambiguous_calls: int = Field(ge=0)
    unreconciled_calls: int = Field(ge=0)

    @property
    def active_micro_thb(self) -> int:
        return self.reserved_micro_thb + self.in_flight_micro_thb + self.ambiguous_micro_thb

    @property
    def exposure_micro_thb(self) -> int:
        return self.settled_micro_thb + self.active_micro_thb


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise CostGateError("Ledger timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class CostLedger:
    """Authoritative SQLite ledger. It initializes only when a cost operation is invoked."""

    def __init__(self, path: Path, *, budget_micro_thb: int, busy_timeout_ms: int = 5_000) -> None:
        if budget_micro_thb < 0:
            raise CostGateError("Monthly budget cannot be negative")
        self.path = path.expanduser().resolve()
        self.budget_micro_thb = budget_micro_thb
        self.busy_timeout_ms = busy_timeout_ms
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self.path,
                timeout=max(self.busy_timeout_ms / 1000, 0.1),
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except sqlite3.Error as exc:
            raise StorageError(f"Cannot open cost ledger: {self.path}", hint=str(exc)) from exc

    def initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            row = connection.execute(
                "SELECT MAX(version) AS version FROM schema_migrations"
            ).fetchone()
            version = int(row["version"] or 0)
            if version > CURRENT_SCHEMA_VERSION:
                raise StorageError(
                    f"Cost ledger schema {version} is newer than supported {CURRENT_SCHEMA_VERSION}"
                )
            if version < 1:
                statements = (
                    """
                    CREATE TABLE IF NOT EXISTS calls (
                        call_id TEXT PRIMARY KEY,
                        request_fingerprint TEXT NOT NULL,
                        session_id TEXT,
                        stage TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        model TEXT NOT NULL,
                        billing_mode TEXT NOT NULL,
                        budget_period TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(
                            status IN ('RESERVED','IN_FLIGHT','SETTLED','RELEASED','AMBIGUOUS')
                        ),
                        estimated_usage_json TEXT NOT NULL,
                        actual_usage_json TEXT,
                        pricing_snapshot_json TEXT NOT NULL,
                        fx_snapshot_json TEXT NOT NULL,
                        reserved_cost_micro_thb INTEGER NOT NULL
                            CHECK(reserved_cost_micro_thb >= 0),
                        settled_cost_micro_thb INTEGER
                            CHECK(settled_cost_micro_thb IS NULL OR settled_cost_micro_thb >= 0),
                        provider_request_id TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        ambiguity_reason TEXT,
                        integrity_error TEXT
                    )
                    """,
                    (
                        "CREATE INDEX IF NOT EXISTS idx_calls_period_status "
                        "ON calls(budget_period, status)"
                    ),
                    (
                        "CREATE INDEX IF NOT EXISTS idx_calls_request_fingerprint "
                        "ON calls(request_fingerprint)"
                    ),
                    """
                    CREATE TABLE IF NOT EXISTS ledger_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        call_id TEXT,
                        event_type TEXT NOT NULL,
                        occurred_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        FOREIGN KEY(call_id) REFERENCES calls(call_id)
                    )
                    """,
                    (
                        "CREATE INDEX IF NOT EXISTS idx_ledger_events_call "
                        "ON ledger_events(call_id, occurred_at)"
                    ),
                )
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES (1, CURRENT_TIMESTAMP)"
                )
            connection.execute("COMMIT")
        except StorageError:
            connection.execute("ROLLBACK")
            raise
        except sqlite3.Error as exc:
            connection.execute("ROLLBACK")
            raise StorageError("Cost ledger migration failed.", hint=str(exc)) from exc
        finally:
            connection.close()

    def reserve(
        self,
        *,
        call_id: str,
        request_fingerprint: str,
        quote: CostQuote,
        stage: str,
        session_id: str | None = None,
        now: datetime | None = None,
    ) -> LedgerRecord:
        if not call_id or not request_fingerprint:
            raise CostGateError("call_id and request fingerprint are required")
        timestamp = now or _utc_now()
        if timestamp.tzinfo is None:
            raise CostGateError("Reservation timestamp must be timezone-aware")
        connection = self._connect()
        blocked = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            if existing is not None:
                existing_record = self._row_to_record(existing)
                self._ensure_idempotent_reservation(existing_record, request_fingerprint, quote)
                connection.execute("COMMIT")
                return existing_record

            summary = self._summary_in_transaction(connection, quote.budget_period)
            if summary.exposure_micro_thb + quote.reserved_cost_micro_thb > self.budget_micro_thb:
                self._event(
                    connection,
                    call_id=None,
                    event_type="BUDGET_BLOCKED",
                    occurred_at=timestamp,
                    payload={
                        "call_id": call_id,
                        "budget_period": quote.budget_period,
                        "requested_micro_thb": quote.reserved_cost_micro_thb,
                        "exposure_micro_thb": summary.exposure_micro_thb,
                        "budget_micro_thb": self.budget_micro_thb,
                    },
                )
                blocked = True
            else:
                connection.execute(
                    """
                    INSERT INTO calls(
                        call_id, request_fingerprint, session_id, stage, provider, model,
                        billing_mode,
                        budget_period, status, estimated_usage_json, actual_usage_json,
                        pricing_snapshot_json, fx_snapshot_json, reserved_cost_micro_thb,
                        settled_cost_micro_thb, provider_request_id, created_at, updated_at,
                        ambiguity_reason, integrity_error
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, 'RESERVED', ?, NULL, ?, ?, ?, NULL, NULL,
                        ?, ?, NULL, NULL
                    )
                    """,
                    (
                        call_id,
                        request_fingerprint,
                        session_id,
                        stage,
                        quote.provider,
                        quote.model,
                        quote.billing_mode,
                        quote.budget_period,
                        _json(quote.usage_estimate.model_dump(mode="json")),
                        _json(quote.pricing_snapshot.snapshot()),
                        _json(quote.fx_snapshot),
                        quote.reserved_cost_micro_thb,
                        _timestamp(timestamp),
                        _timestamp(timestamp),
                    ),
                )
                self._event(
                    connection,
                    call_id=call_id,
                    event_type="BUDGET_RESERVED",
                    occurred_at=timestamp,
                    payload={"reserved_micro_thb": quote.reserved_cost_micro_thb},
                )
            connection.execute("COMMIT")
        except BudgetExceededError:
            connection.execute("ROLLBACK")
            raise
        except sqlite3.IntegrityError as exc:
            connection.execute("ROLLBACK")
            raise CostGateError(
                "Cost ledger reservation identity conflict.", hint=str(exc)
            ) from exc
        except sqlite3.Error as exc:
            connection.execute("ROLLBACK")
            raise StorageError(
                "Cost ledger reservation transaction failed.", hint=str(exc)
            ) from exc
        finally:
            connection.close()
        if blocked:
            raise BudgetExceededError(
                hint=(
                    f"Requested {quote.reserved_cost_micro_thb} micro-THB with "
                    f"{summary.exposure_micro_thb} already exposed against "
                    f"{self.budget_micro_thb}."
                )
            )
        return self.get(call_id)

    def mark_in_flight(
        self, call_id: str, *, provider_request_id: str | None = None, now: datetime | None = None
    ) -> LedgerRecord:
        return self._transition(
            call_id,
            expected={LifecycleStatus.RESERVED, LifecycleStatus.IN_FLIGHT},
            target=LifecycleStatus.IN_FLIGHT,
            event_type="CALL_IN_FLIGHT",
            provider_request_id=provider_request_id,
            now=now,
        )

    def release(
        self, call_id: str, *, confirmed_no_dispatch: bool = False, now: datetime | None = None
    ) -> LedgerRecord:
        record = self.get(call_id)
        if record.status is LifecycleStatus.AMBIGUOUS and not confirmed_no_dispatch:
            raise CostGateError(
                "Ambiguous call cannot be released without explicit no-charge evidence"
            )
        allowed = {LifecycleStatus.RESERVED}
        if confirmed_no_dispatch:
            allowed.add(LifecycleStatus.AMBIGUOUS)
        return self._transition(
            call_id,
            expected=allowed,
            target=LifecycleStatus.RELEASED,
            event_type="RESERVATION_RELEASED",
            now=now,
        )

    def mark_ambiguous(
        self, call_id: str, reason: str, *, now: datetime | None = None
    ) -> LedgerRecord:
        if not reason.strip():
            raise CostGateError("Ambiguous call reason is required")
        return self._transition(
            call_id,
            expected={LifecycleStatus.IN_FLIGHT},
            target=LifecycleStatus.AMBIGUOUS,
            event_type="CALL_AMBIGUOUS",
            ambiguity_reason=reason.strip(),
            now=now,
        )

    def settle(
        self,
        call_id: str,
        actual_usage: ProviderUsageActual | ProviderUsageEstimate,
        *,
        provider_request_id: str | None = None,
        now: datetime | None = None,
    ) -> LedgerRecord:
        timestamp = now or _utc_now()
        if timestamp.tzinfo is None:
            raise CostGateError("Settlement timestamp must be timezone-aware")
        connection = self._connect()
        overage = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,)).fetchone()
            if row is None:
                raise CostGateError(f"Unknown cost call: {call_id}")
            record = self._row_to_record(row)
            usage = ProviderUsageActual.model_validate(actual_usage.model_dump(mode="python"))
            actual_payload = usage.model_dump(mode="json")
            if record.status is LifecycleStatus.SETTLED:
                if (
                    record.actual_usage is not None
                    and record.actual_usage.model_dump(mode="json") == actual_payload
                ):
                    connection.execute("COMMIT")
                    return record
                raise CostGateError("Conflicting settlement data for an already settled call")
            if record.status not in {LifecycleStatus.IN_FLIGHT, LifecycleStatus.AMBIGUOUS}:
                raise CostGateError(f"Cannot settle call in state {record.status}")
            actual_cost = calculate_cost(
                usage,
                record.pricing_snapshot,
                record.fx_snapshot,
                safety_factor=1,
            )
            overage = actual_cost > record.reserved_cost_micro_thb
            integrity_error = (
                "Actual provider cost exceeded the conservative reservation" if overage else None
            )
            connection.execute(
                "UPDATE calls SET status='SETTLED', actual_usage_json=?, settled_cost_micro_thb=?, "
                "provider_request_id=COALESCE(?, provider_request_id), "
                "updated_at=?, integrity_error=? "
                "WHERE call_id=?",
                (
                    _json(actual_payload),
                    actual_cost,
                    provider_request_id or usage.provider_request_id,
                    _timestamp(timestamp),
                    integrity_error,
                    call_id,
                ),
            )
            self._event(
                connection,
                call_id=call_id,
                event_type="OVERAGE_DETECTED" if overage else "CALL_SETTLED",
                occurred_at=timestamp,
                payload={"settled_micro_thb": actual_cost},
            )
            connection.execute("COMMIT")
        except (CostGateError, CostIntegrityError):
            connection.execute("ROLLBACK")
            raise
        except sqlite3.Error as exc:
            connection.execute("ROLLBACK")
            raise StorageError("Cost ledger settlement transaction failed.", hint=str(exc)) from exc
        finally:
            connection.close()
        settled = self.get(call_id)
        if overage:
            raise CostIntegrityError(
                "Actual provider cost exceeded the conservative reservation.",
                hint=f"Call {call_id} settled at {settled.settled_cost_micro_thb} micro-THB.",
            )
        return settled

    def reconcile(
        self,
        call_id: str,
        *,
        actual_usage: ProviderUsageActual | ProviderUsageEstimate | None = None,
        release_confirmed: bool = False,
        provider_request_id: str | None = None,
        now: datetime | None = None,
    ) -> LedgerRecord:
        if actual_usage is not None and release_confirmed:
            raise CostGateError("Reconciliation cannot both settle and release a call")
        if actual_usage is not None:
            return self.settle(
                call_id, actual_usage, provider_request_id=provider_request_id, now=now
            )
        if release_confirmed:
            return self.release(call_id, confirmed_no_dispatch=True, now=now)
        raise CostGateError("Explicit reconciliation requires actual usage or no-charge evidence")

    def get(self, call_id: str) -> LedgerRecord:
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,)).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("Cannot read cost ledger call.", hint=str(exc)) from exc
        finally:
            connection.close()
        if row is None:
            raise CostGateError(f"Unknown cost call: {call_id}")
        return self._row_to_record(row)

    def list_calls(self, *, budget_period: str | None = None) -> tuple[LedgerRecord, ...]:
        connection = self._connect()
        try:
            if budget_period is None:
                rows = connection.execute(
                    "SELECT * FROM calls ORDER BY created_at, call_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM calls WHERE budget_period=? ORDER BY created_at, call_id",
                    (budget_period,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError("Cannot list cost ledger calls.", hint=str(exc)) from exc
        finally:
            connection.close()
        return tuple(self._row_to_record(row) for row in rows)

    def summary(self, budget_period: str) -> BudgetSummary:
        connection = self._connect()
        try:
            return self._summary_in_transaction(connection, budget_period)
        except sqlite3.Error as exc:
            raise StorageError("Cannot summarize cost ledger.", hint=str(exc)) from exc
        finally:
            connection.close()

    def _summary_in_transaction(
        self, connection: sqlite3.Connection, budget_period: str
    ) -> BudgetSummary:
        row = connection.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN status='SETTLED'
                THEN settled_cost_micro_thb ELSE 0 END), 0) AS settled,
              COALESCE(SUM(CASE WHEN status='RESERVED'
                THEN reserved_cost_micro_thb ELSE 0 END), 0) AS reserved,
              COALESCE(SUM(CASE WHEN status='IN_FLIGHT'
                THEN reserved_cost_micro_thb ELSE 0 END), 0) AS in_flight,
              COALESCE(SUM(CASE WHEN status='AMBIGUOUS'
                THEN reserved_cost_micro_thb ELSE 0 END), 0) AS ambiguous,
              COALESCE(SUM(CASE WHEN status='AMBIGUOUS' THEN 1 ELSE 0 END), 0) AS ambiguous_calls
            FROM calls WHERE budget_period=?
            """,
            (budget_period,),
        ).fetchone()
        settled = int(row["settled"])
        reserved = int(row["reserved"])
        in_flight = int(row["in_flight"])
        ambiguous = int(row["ambiguous"])
        exposure = settled + reserved + in_flight + ambiguous
        return BudgetSummary(
            budget_period=budget_period,
            budget_micro_thb=self.budget_micro_thb,
            settled_micro_thb=settled,
            reserved_micro_thb=reserved,
            in_flight_micro_thb=in_flight,
            ambiguous_micro_thb=ambiguous,
            available_micro_thb=max(0, self.budget_micro_thb - exposure),
            ambiguous_calls=int(row["ambiguous_calls"]),
            unreconciled_calls=int(row["ambiguous_calls"]),
        )

    def _transition(
        self,
        call_id: str,
        *,
        expected: set[LifecycleStatus],
        target: LifecycleStatus,
        event_type: str,
        provider_request_id: str | None = None,
        ambiguity_reason: str | None = None,
        now: datetime | None = None,
    ) -> LedgerRecord:
        timestamp = now or _utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM calls WHERE call_id=?", (call_id,)).fetchone()
            if row is None:
                raise CostGateError(f"Unknown cost call: {call_id}")
            record = self._row_to_record(row)
            if record.status is target:
                connection.execute("COMMIT")
                return record
            if record.status not in expected:
                raise CostGateError(f"Cannot transition call from {record.status} to {target}")
            connection.execute(
                "UPDATE calls SET status=?, provider_request_id=COALESCE(?, provider_request_id), "
                "ambiguity_reason=COALESCE(?, ambiguity_reason), updated_at=? WHERE call_id=?",
                (
                    target.value,
                    provider_request_id,
                    ambiguity_reason,
                    _timestamp(timestamp),
                    call_id,
                ),
            )
            self._event(
                connection,
                call_id=call_id,
                event_type=event_type,
                occurred_at=timestamp,
                payload={"status": target.value, "reason": ambiguity_reason},
            )
            connection.execute("COMMIT")
        except CostGateError:
            connection.execute("ROLLBACK")
            raise
        except sqlite3.Error as exc:
            connection.execute("ROLLBACK")
            raise StorageError("Cost ledger lifecycle transaction failed.", hint=str(exc)) from exc
        finally:
            connection.close()
        return self.get(call_id)

    def _ensure_idempotent_reservation(
        self, record: LedgerRecord, request_fingerprint: str, quote: CostQuote
    ) -> None:
        if record.request_fingerprint != request_fingerprint:
            raise CostGateError("call_id was reused with a different request fingerprint")
        if (
            record.provider != quote.provider
            or record.model != quote.model
            or record.billing_mode != quote.billing_mode
            or record.budget_period != quote.budget_period
            or record.reserved_cost_micro_thb != quote.reserved_cost_micro_thb
        ):
            raise CostGateError("call_id was reused with conflicting reservation semantics")
        if record.status is LifecycleStatus.AMBIGUOUS:
            raise CostGateError("Ambiguous call must be reconciled before it can be reused")
        if record.status is LifecycleStatus.RELEASED:
            raise CostGateError("Released call_id cannot be reserved again")

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        *,
        call_id: str | None,
        event_type: str,
        occurred_at: datetime,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO ledger_events(call_id,event_type,occurred_at,payload_json) "
            "VALUES(?,?,?,?)",
            (call_id, event_type, _timestamp(occurred_at), _json(payload)),
        )

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> LedgerRecord:
        actual_payload = json.loads(row["actual_usage_json"]) if row["actual_usage_json"] else None
        return LedgerRecord(
            call_id=row["call_id"],
            request_fingerprint=row["request_fingerprint"],
            session_id=row["session_id"],
            stage=row["stage"],
            provider=row["provider"],
            model=row["model"],
            billing_mode=row["billing_mode"],
            budget_period=row["budget_period"],
            status=LifecycleStatus(row["status"]),
            estimated_usage=ProviderUsageEstimate.model_validate(
                json.loads(row["estimated_usage_json"])
            ),
            actual_usage=ProviderUsageActual.model_validate(actual_payload)
            if actual_payload
            else None,
            pricing_snapshot=PricingEntry.model_validate(json.loads(row["pricing_snapshot_json"])),
            fx_snapshot=FxSnapshot.model_validate(json.loads(row["fx_snapshot_json"])),
            reserved_cost_micro_thb=int(row["reserved_cost_micro_thb"]),
            settled_cost_micro_thb=(
                int(row["settled_cost_micro_thb"])
                if row["settled_cost_micro_thb"] is not None
                else None
            ),
            provider_request_id=row["provider_request_id"],
            created_at=_parse_timestamp(row["created_at"]),
            updated_at=_parse_timestamp(row["updated_at"]),
            ambiguity_reason=row["ambiguity_reason"],
            integrity_error=row["integrity_error"],
        )


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
