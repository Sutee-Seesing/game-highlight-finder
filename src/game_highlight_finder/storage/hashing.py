"""Streaming content hashing."""

from __future__ import annotations

import hashlib
from pathlib import Path

from game_highlight_finder.errors import SourceError, StorageError

CHUNK_SIZE = 1024 * 1024


def hash_file(path: Path, *, source: bool = False) -> str:
    try:
        with path.open("rb") as handle:
            # ``file_digest`` keeps the same SHA-256 semantics while using the
            # interpreter's buffered file path for multi-gigabyte recordings.
            # The fallback preserves compatibility with older Python runtimes.
            file_digest = getattr(hashlib, "file_digest", None)
            if callable(file_digest):
                return str(file_digest(handle, "sha256").hexdigest())
            digest = hashlib.sha256()
            while chunk := handle.read(CHUNK_SIZE):
                digest.update(chunk)
            return digest.hexdigest()
    except OSError as exc:
        error_type = SourceError if source else StorageError
        raise error_type(f"Cannot hash file: {path}", hint=str(exc)) from exc
