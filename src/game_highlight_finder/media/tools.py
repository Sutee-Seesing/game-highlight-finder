"""Executable resolution, version checks, and bounded codec capability probes."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from game_highlight_finder.errors import DependencyError
from game_highlight_finder.redaction import redact_text


@dataclass(frozen=True)
class ToolIdentity:
    """Stable external-tool identity used by derivative-stage cache keys."""

    name: str
    path: Path
    version: str
    capabilities: tuple[str, ...] = ()

    def cache_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": str(self.path.resolve()),
            "version": self.version,
            "capabilities": self.capabilities,
        }

    def __str__(self) -> str:
        return f"{self.name} {self.version} ({self.path})"


@dataclass(frozen=True)
class H264EncoderChoice:
    """One H.264 encoder proven usable by a tiny local encode smoke test."""

    encoder: str
    preset: str
    hardware_accelerated: bool


_H264_ENCODER_CANDIDATES: tuple[tuple[str, str, bool], ...] = (
    ("h264_nvenc", "p4", True),
    ("h264_qsv", "veryfast", True),
    ("libx264", "veryfast", False),
)


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


def tool_identity(
    name: str,
    configured: Path | None = None,
    *,
    include_capabilities: bool = True,
) -> ToolIdentity:
    path = require_executable(name, configured)
    version = executable_version(path)
    capabilities = _probe_capabilities(path) if include_capabilities and name == "ffmpeg" else ()
    return ToolIdentity(name=name, path=path, version=version, capabilities=capabilities)


def select_usable_h264_encoder(
    ffmpeg_path: Path,
    *,
    timeout_seconds: int = 15,
) -> H264EncoderChoice:
    """Prefer NVENC, then Intel QSV, then CPU only after a real encode succeeds.

    FFmpeg builds can advertise encoders whose hardware runtime is absent.  In
    particular, ``-encoders`` may list ``h264_nvenc`` on a machine without the
    NVIDIA CUDA runtime.  A one-frame lavfi encode is bounded, local, provider-free,
    and proves that the selected encoder can actually open on the current machine.
    """

    if timeout_seconds <= 0:
        raise ValueError("encoder probe timeout must be positive")
    failures: list[str] = []
    for encoder, preset, hardware_accelerated in _H264_ENCODER_CANDIDATES:
        command = [
            str(ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:d=0.1:r=30",
            "-frames:v",
            "1",
            "-c:v",
            encoder,
            "-preset",
            preset,
            "-pix_fmt",
            "yuv420p",
            "-f",
            "null",
            "-",
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append(f"{encoder}: {type(exc).__name__}")
            continue
        if result.returncode == 0:
            return H264EncoderChoice(
                encoder=encoder,
                preset=preset,
                hardware_accelerated=hardware_accelerated,
            )
        detail = redact_text((result.stderr or result.stdout).strip())
        last_line = detail.splitlines()[-1][:240] if detail else f"exit {result.returncode}"
        failures.append(f"{encoder}: {last_line}")
    raise DependencyError(
        "No usable H.264 encoder is available on this machine.",
        hint="; ".join(failures)[-1500:],
    )


def _probe_capabilities(path: Path) -> tuple[str, ...]:
    """Capture only relevant, bounded FFmpeg capability names for cache identity."""

    discovered: set[str] = set()
    for listing, marker in (("-encoders", "encoder"), ("-filters", "filter")):
        try:
            result = subprocess.run(
                [str(path), "-hide_banner", listing],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode != 0:
            continue
        text = result.stdout + "\n" + result.stderr
        relevant = (
            ("libx264", "h264_nvenc", "h264_qsv", "aac")
            if marker == "encoder"
            else ("silencedetect", "ebur128", "astats")
        )
        for capability in relevant:
            if capability in text:
                discovered.add(capability)
    return tuple(sorted(discovered))
