"""Conservative per-session file lock for local processes."""

from __future__ import annotations

import ctypes
import json
import os
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from game_highlight_finder.errors import StorageError


class SessionLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._owned = False

    def __enter__(self) -> SessionLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "created_at": datetime.now(UTC).isoformat(),
        }
        for _attempt in range(2):
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(payload, handle, sort_keys=True)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                self._owned = True
                return
            except FileExistsError:
                if not self._remove_if_stale():
                    owner = self._read_owner()
                    raise StorageError(
                        f"Session is locked by another process: {self.path}",
                        hint=_owner_hint(owner),
                    ) from None
            except OSError as exc:
                raise StorageError(
                    f"Cannot create session lock: {self.path}", hint=str(exc)
                ) from exc
        raise StorageError(f"Could not acquire session lock: {self.path}")

    def release(self) -> None:
        if not self._owned:
            return
        try:
            owner = self._read_owner()
            if owner.get("pid") == os.getpid() and owner.get("hostname") == socket.gethostname():
                self.path.unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError(f"Cannot release session lock: {self.path}", hint=str(exc)) from exc
        finally:
            self._owned = False

    def _read_owner(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _remove_if_stale(self) -> bool:
        owner = self._read_owner()
        if owner.get("hostname") != socket.gethostname():
            return False
        pid = owner.get("pid")
        if not isinstance(pid, int) or pid <= 0 or _process_exists(pid):
            return False
        try:
            self.path.unlink()
        except OSError:
            return False
        return True


def _process_exists(pid: int) -> bool:
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        # Access denied means a protected process exists. Invalid parameter means no PID.
        return ctypes.get_last_error() == 5
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _owner_hint(owner: dict[str, Any]) -> str | None:
    if not owner:
        return "The lock file is unreadable; inspect it manually before removal."
    return f"pid={owner.get('pid')}, host={owner.get('hostname')}, since={owner.get('created_at')}"
