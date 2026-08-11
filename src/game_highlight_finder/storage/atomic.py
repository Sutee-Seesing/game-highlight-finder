"""Atomic and bounded JSON storage."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from game_highlight_finder.errors import StorageError, ValidationError

MAX_JSON_BYTES = 16 * 1024 * 1024


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
        raise StorageError(f"Cannot atomically write JSON artifact: {path}", hint=str(exc)) from exc


def read_json(path: Path, *, max_bytes: int = MAX_JSON_BYTES) -> Any:
    try:
        size = path.stat().st_size
        if size > max_bytes:
            raise ValidationError(f"JSON artifact is too large: {path} ({size} bytes)")
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except ValidationError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Cannot read valid JSON artifact: {path}", hint=str(exc)) from exc
