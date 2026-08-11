"""Exact time conversion helpers for source-relative integer milliseconds."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from game_highlight_finder.errors import ValidationError


def seconds_to_ms(value: str | int | Decimal) -> int:
    try:
        seconds = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"Invalid timestamp value: {value!r}") from exc
    if not seconds.is_finite() or seconds < 0:
        raise ValidationError(f"Timestamp must be finite and non-negative: {value!r}")
    return int((seconds * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def optional_seconds_to_ms(value: object) -> int | None:
    if value is None or value == "N/A":
        return None
    return seconds_to_ms(str(value))


def format_duration(duration_ms: int) -> str:
    total_seconds, milliseconds = divmod(duration_ms, 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if milliseconds:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
