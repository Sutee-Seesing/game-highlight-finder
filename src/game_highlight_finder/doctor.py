"""Local readiness checks for M1."""

from __future__ import annotations

import shutil
import sys
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from game_highlight_finder import __version__
from game_highlight_finder.config import AppConfig
from game_highlight_finder.media.tools import executable_version, resolve_executable
from game_highlight_finder.storage.sessions import ensure_data_directory

SUPPORTED_PYTHON = (3, 12)
LOW_DISK_WARNING_BYTES = 10 * 1024**3


class CheckLevel(StrEnum):
    PASS = "PASS"
    WARNING = "WARNING"
    FAIL = "FAIL"


class DoctorCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    level: CheckLevel
    message: str
    path: Path | None = None


class DoctorReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    checks: tuple[DoctorCheck, ...]

    @property
    def has_failures(self) -> bool:
        return any(check.level is CheckLevel.FAIL for check in self.checks)


def run_doctor(config: AppConfig, *, config_error: str | None = None) -> DoctorReport:
    checks: list[DoctorCheck] = [
        DoctorCheck(name="application", level=CheckLevel.PASS, message=f"version {__version__}"),
        DoctorCheck(
            name="python",
            level=(
                CheckLevel.PASS if sys.version_info[:2] == SUPPORTED_PYTHON else CheckLevel.FAIL
            ),
            message=(f"{sys.version.split()[0]} at {sys.executable}; required 3.12.x"),
            path=Path(sys.executable),
        ),
        DoctorCheck(
            name="configuration",
            level=CheckLevel.FAIL if config_error else CheckLevel.PASS,
            message=config_error or "configuration loaded and validated",
        ),
    ]
    checks.extend(
        _tool_check("ffmpeg", config.tools.ffmpeg_path),
    )
    checks.extend(
        _tool_check("ffprobe", config.tools.ffprobe_path),
    )
    writable, error = ensure_data_directory(config.storage.data_dir)
    checks.append(
        DoctorCheck(
            name="data directory",
            level=CheckLevel.PASS if writable else CheckLevel.FAIL,
            message=("writable" if writable else f"not writable: {error}"),
            path=config.storage.data_dir,
        )
    )
    if writable:
        try:
            usage = shutil.disk_usage(config.storage.data_dir)
            level = CheckLevel.WARNING if usage.free < LOW_DISK_WARNING_BYTES else CheckLevel.PASS
            checks.append(
                DoctorCheck(
                    name="disk space",
                    level=level,
                    message=f"{usage.free / 1024**3:.1f} GiB free",
                    path=config.storage.data_dir,
                )
            )
        except OSError as exc:
            checks.append(
                DoctorCheck(
                    name="disk space",
                    level=CheckLevel.WARNING,
                    message=f"could not determine free space: {exc}",
                    path=config.storage.data_dir,
                )
            )
    return DoctorReport(checks=tuple(checks))


def _tool_check(name: str, configured: Path | None) -> list[DoctorCheck]:
    path = resolve_executable(name, configured)
    if path is None:
        return [
            DoctorCheck(
                name=name,
                level=CheckLevel.FAIL,
                message=(
                    "not found; on Windows run `scoop install ffmpeg`, or configure the "
                    f"explicit {name}_path / GHF_{name.upper()}_PATH"
                ),
                path=configured,
            )
        ]
    try:
        version = executable_version(path)
    except Exception as exc:
        return [DoctorCheck(name=name, level=CheckLevel.FAIL, message=str(exc), path=path)]
    return [DoctorCheck(name=name, level=CheckLevel.PASS, message=version, path=path)]
