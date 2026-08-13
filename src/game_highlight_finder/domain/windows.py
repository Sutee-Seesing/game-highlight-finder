"""Deterministic bounded Scout windows for M6 long-session analysis."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, field_validator, model_validator

from game_highlight_finder.domain.models import PersistedModel, Sha256, StageStatus
from game_highlight_finder.errors import ValidationError


class ScoutWindow(PersistedModel):
    """One half-open source-relative analysis window and its lineage."""

    schema_version: Literal[1] = 1
    window_id: str = Field(pattern=r"^scout_window_[0-9a-f]{16}$")
    session_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(pattern=r"^src_[0-9a-f]{16}$")
    ordinal: int = Field(ge=0)
    source_start_ms: int = Field(ge=0)
    source_end_ms: int = Field(gt=0)
    source_duration_ms: int = Field(gt=0)
    overlap_before_ms: int = Field(default=0, ge=0)
    overlap_after_ms: int = Field(default=0, ge=0)
    proxy_path: str = Field(min_length=1, max_length=500)
    proxy_sha256: Sha256 | None = None
    parent_proxy_sha256: Sha256 | None = None
    signal_summary_hash: Sha256 | None = None
    provider_cache_key: Sha256 | None = None
    status: StageStatus = StageStatus.PENDING
    warnings: list[str] = Field(default_factory=list, max_length=32)

    @field_validator(
        "ordinal",
        "source_start_ms",
        "source_end_ms",
        "source_duration_ms",
        "overlap_before_ms",
        "overlap_after_ms",
        mode="before",
    )
    @classmethod
    def strict_integer(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("Scout window numeric fields must be integers")
        return value

    @model_validator(mode="after")
    def interval_is_valid(self) -> ScoutWindow:
        if self.source_end_ms <= self.source_start_ms:
            raise ValueError("Scout window end must be greater than start")
        if self.source_end_ms > self.source_duration_ms:
            raise ValueError("Scout window exceeds source duration")
        if self.overlap_before_ms > self.source_end_ms - self.source_start_ms:
            raise ValueError("Scout window before-overlap is too large")
        if self.overlap_after_ms > self.source_end_ms - self.source_start_ms:
            raise ValueError("Scout window after-overlap is too large")
        return self

    @property
    def duration_ms(self) -> int:
        return self.source_end_ms - self.source_start_ms


class WindowPlan(PersistedModel):
    schema_version: Literal[1] = 1
    session_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(pattern=r"^src_[0-9a-f]{16}$")
    source_duration_ms: int = Field(gt=0)
    max_window_duration_ms: int = Field(gt=0)
    overlap_ms: int = Field(ge=0)
    windows: list[ScoutWindow] = Field(min_length=1, max_length=10_000)
    plan_hash: Sha256 | None = None

    @model_validator(mode="after")
    def plan_is_contiguous(self) -> WindowPlan:
        if self.overlap_ms >= self.max_window_duration_ms:
            raise ValueError("window overlap must be shorter than maximum window duration")
        previous: ScoutWindow | None = None
        for index, window in enumerate(self.windows):
            if window.ordinal != index:
                raise ValueError("Scout window ordinals must be contiguous")
            if window.source_duration_ms != self.source_duration_ms:
                raise ValueError("Scout windows must share source duration")
            if window.source_start_ms > (previous.source_end_ms if previous else 0):
                raise ValueError("Scout window plan contains a gap")
            if previous is not None:
                expected = previous.source_end_ms - self.overlap_ms
                if window.source_start_ms != expected:
                    raise ValueError("Scout window overlap is not deterministic")
            if window.source_end_ms - window.source_start_ms > self.max_window_duration_ms:
                raise ValueError("Scout window exceeds configured maximum")
            previous = window
        if self.windows[0].source_start_ms != 0:
            raise ValueError("Scout window plan must start at source zero")
        if self.windows[-1].source_end_ms != self.source_duration_ms:
            raise ValueError("Scout window plan must end at source duration")
        return self


def _window_digest(
    *, session_id: str, source_id: str, ordinal: int, start_ms: int, end_ms: int
) -> str:
    payload = {
        "version": 1,
        "session_id": session_id,
        "source_id": source_id,
        "ordinal": ordinal,
        "start_ms": start_ms,
        "end_ms": end_ms,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def plan_scout_windows(
    source_duration_ms: int,
    *,
    max_duration_ms: int = 900_000,
    overlap_ms: int = 30_000,
    session_id: str = "session",
    source_id: str = "src_0000000000000000",
    max_windows: int = 1_024,
) -> WindowPlan:
    """Plan bounded half-open windows using integer arithmetic only."""

    values = (source_duration_ms, max_duration_ms, overlap_ms, max_windows)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise ValidationError("window planner inputs must be integers")
    if source_duration_ms <= 0 or max_duration_ms <= 0:
        raise ValidationError("source and maximum window duration must be positive")
    if overlap_ms < 0 or overlap_ms >= max_duration_ms:
        raise ValidationError("window overlap must be non-negative and shorter than window")
    if max_windows <= 0:
        raise ValidationError("max_windows must be positive")
    windows: list[ScoutWindow] = []
    start = 0
    ordinal = 0
    while True:
        end = min(source_duration_ms, start + max_duration_ms)
        next_start = end if end == source_duration_ms else end - overlap_ms
        before = 0 if not windows else max(0, windows[-1].source_end_ms - start)
        after = 0 if end == source_duration_ms else max(0, end - next_start)
        digest = _window_digest(
            session_id=session_id,
            source_id=source_id,
            ordinal=ordinal,
            start_ms=start,
            end_ms=end,
        )
        window = ScoutWindow(
            window_id=f"scout_window_{digest}",
            session_id=session_id,
            source_id=source_id,
            ordinal=ordinal,
            source_start_ms=start,
            source_end_ms=end,
            source_duration_ms=source_duration_ms,
            overlap_before_ms=before,
            overlap_after_ms=after,
            proxy_path="scout/windows/pending.mp4",
        )
        windows.append(window)
        if end == source_duration_ms:
            break
        ordinal += 1
        if ordinal >= max_windows:
            raise ValidationError("Scout window count exceeds configured safety limit")
        if next_start <= start:
            raise ValidationError("window planner made no forward progress")
        start = next_start
    plan = WindowPlan(
        session_id=session_id,
        source_id=source_id,
        source_duration_ms=source_duration_ms,
        max_window_duration_ms=max_duration_ms,
        overlap_ms=overlap_ms,
        windows=windows,
    )
    encoded = json.dumps(
        plan.model_dump(mode="json", exclude={"plan_hash"}), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return plan.model_copy(update={"plan_hash": hashlib.sha256(encoded).hexdigest()})


def window_plan_cache_key(plan: WindowPlan, *, parent_proxy_sha256: str) -> str:
    payload = {
        "plan": plan.model_dump(mode="json", exclude={"plan_hash"}),
        "parent_proxy_sha256": parent_proxy_sha256,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


__all__ = ["ScoutWindow", "WindowPlan", "plan_scout_windows", "window_plan_cache_key"]
