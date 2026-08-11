"""Executable resolution and version checks."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from game_highlight_finder.errors import DependencyError
from game_highlight_finder.redaction import redact_text


def resolve_executable(name: str, configured: Path | None = None) -> Path | None:
    if configured is not None:
        candidate = configured.expanduser().resolve()
        return candidate if candidate.is_file() else None
    located = shutil.which(name)
    return Path(located).resolve() if located else None


def require_executable(name: str, configured: Path | None = None) -> Path:
    path = resolve_executable(name, configured)
    if path is None:
        raise DependencyError(
            f"Required executable '{name}' was not found.",
            hint=(
                "Install FFmpeg on Windows (recommended: `scoop install ffmpeg`) or set the "
                f"configured {name}_path / GHF_{name.upper()}_PATH."
            ),
        )
    return path


def executable_version(path: Path, *, timeout_seconds: int = 15) -> str:
    try:
        result = subprocess.run(
            [str(path), "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DependencyError(f"Cannot execute dependency: {path}", hint=str(exc)) from exc
    if result.returncode != 0:
        detail = redact_text((result.stderr or result.stdout).strip()[-1000:])
        raise DependencyError(f"Dependency version check failed: {path}", hint=detail)
    first_line = result.stdout.splitlines()[0].strip() if result.stdout else ""
    if not first_line:
        raise DependencyError(f"Dependency returned no version information: {path}")
    return first_line
