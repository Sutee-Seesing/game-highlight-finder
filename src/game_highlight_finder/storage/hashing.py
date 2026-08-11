"""Streaming content hashing."""

from __future__ import annotations

import hashlib
from pathlib import Path

from game_highlight_finder.errors import SourceError, StorageError

CHUNK_SIZE = 1024 * 1024


def hash_file(path: Path, *, source: bool = False) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(CHUNK_SIZE):
                digest.update(chunk)
    except OSError as exc:
        error_type = SourceError if source else StorageError
        raise error_type(f"Cannot hash file: {path}", hint=str(exc)) from exc
    return digest.hexdigest()
