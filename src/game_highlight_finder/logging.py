"""Structured per-run logging with redaction."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from game_highlight_finder.errors import StorageError
from game_highlight_finder.redaction import redact_data, redact_text


class RunLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, level: str, event: str, message: str, **fields: Any) -> None:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": level,
            "event": event,
            "message": redact_text(message),
            **redact_data(fields),
        }
        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError as exc:
            raise StorageError(f"Cannot write structured log: {self.path}", hint=str(exc)) from exc
