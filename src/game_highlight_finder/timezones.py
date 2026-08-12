"""Configured timezone resolution with a deterministic Windows fallback."""

from __future__ import annotations

from datetime import timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def configured_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        # Windows installations can lack the optional IANA tzdata package. The
        # project default has no DST transitions, so this fixed-offset fallback
        # preserves Asia/Bangkok month boundaries without inferring UTC.
        if name == "Asia/Bangkok":
            return timezone(timedelta(hours=7), name="Asia/Bangkok")
        raise
