"""FFmpeg command construction, machine-readable progress, and safe execution."""

from __future__ import annotations

import queue
import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from game_highlight_finder.config import AppConfig
from game_highlight_finder.errors import DependencyError, StorageError
from game_highlight_finder.redaction import redact_text

_PROGRESS_FIELDS = frozenset({"out_time_us", "out_time_ms", "out_time", "speed", "progress"})
_MAX_PROGRESS_VALUE_LENGTH = 256


@dataclass(frozen=True)
class ProgressUpdate:
    out_time_ms: int | None = None
    percent: float | None = None
    speed: str | None = None
    progress: str | None = None


class FFmpegExecutionError(DependencyError):
    """FFmpeg started but did not produce an accepted result."""


class FFmpegCancelled(StorageError):
    """Encoding was interrupted or cancelled before completion."""


class FFmpegProgressParser:
    """Parse FFmpeg ``-progress`` key/value blocks without scraping human logs."""

    def __init__(self, duration_ms: int | None = None) -> None:
        self.duration_ms = duration_ms
        self._values: dict[str, str] = {}

    def feed(self, line: str) -> ProgressUpdate | None:
        text = line.strip()
        if not text or "=" not in text:
            return None
        key, value = text.split("=", 1)
        key = key.strip()
        if key not in _PROGRESS_FIELDS:
            return None
        value = value.strip()
        self._values[key] = value if len(value) <= _MAX_PROGRESS_VALUE_LENGTH else ""
        if key != "progress":
            return None
        out_time_ms = _parse_progress_time_ms(self._values)
        progress = self._values.get("progress")
        percent: float | None = None
        if self.duration_ms and out_time_ms is not None:
            percent = max(0.0, min(100.0, out_time_ms * 100.0 / self.duration_ms))
        elif self.duration_ms and progress == "end":
            percent = 100.0
        return ProgressUpdate(
            out_time_ms=out_time_ms,
            percent=percent,
            speed=self._values.get("speed"),
            progress=progress,
        )


def parse_progress_text(text: str, *, duration_ms: int | None = None) -> list[ProgressUpdate]:
    parser = FFmpegProgressParser(duration_ms)
    updates: list[ProgressUpdate] = []
    for line in text.splitlines():
        update = parser.feed(line)
        if update is not None:
            updates.append(update)
    return updates


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    value = value.strip()
    if not value or len(value) > 32:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_progress_time_ms(values: dict[str, str]) -> int | None:
    """Return the progress timestamp in canonical integer milliseconds.

    FFmpeg's ``out_time_us`` is expressed in microseconds.  The legacy
    ``out_time_ms`` field is unfortunately named: it carries the same
    microsecond-scale value in FFmpeg's machine-readable progress output.
    Prefer the explicit field when both are available, and retain a textual
    ``out_time`` fallback for older or unusual builds.
    """

    for field in ("out_time_us", "out_time_ms"):
        value = _parse_int(values.get(field))
        if value is not None:
            converted = _microseconds_to_milliseconds(value)
            if converted is not None:
                return converted
    return _parse_timestamp_ms(values.get("out_time"))


def _microseconds_to_milliseconds(value: int) -> int | None:
    if value < 0:
        return None
    return value // 1_000


_OUT_TIME_PATTERN = re.compile(
    r"^(?P<hours>\d+):(?P<minutes>[0-5]\d):(?P<seconds>[0-5]\d)"
    r"(?:\.(?P<fraction>\d{1,6}))?$"
)


def _parse_timestamp_ms(value: str | None) -> int | None:
    """Parse FFmpeg's ``HH:MM:SS[.fraction]`` timestamp defensively."""

    if value is None or len(value) > 48:
        return None
    match = _OUT_TIME_PATTERN.fullmatch(value.strip())
    if match is None:
        return None
    hours = _parse_int(match.group("hours"))
    minutes = _parse_int(match.group("minutes"))
    seconds = _parse_int(match.group("seconds"))
    if hours is None or minutes is None or seconds is None:
        return None
    fraction = (match.group("fraction") or "").ljust(6, "0")
    microseconds = _parse_int(fraction) or 0
    return ((hours * 3_600 + minutes * 60 + seconds) * 1_000) + microseconds // 1_000


def compute_proxy_dimensions(
    width: int,
    height: int,
    *,
    max_width: int = 854,
    max_height: int = 480,
) -> tuple[int, int]:
    """Scale down while preserving aspect ratio and producing encoder-safe even sizes."""

    if width <= 0 or height <= 0 or max_width <= 0 or max_height <= 0:
        raise ValueError("video dimensions and limits must be positive")
    scale = min(1.0, max_width / width, max_height / height)
    scaled_width = max(2, int(width * scale))
    scaled_height = max(2, int(height * scale))
    # Round down to even dimensions; never exceed either configured maximum.
    scaled_width -= scaled_width % 2
    scaled_height -= scaled_height % 2
    return max(2, scaled_width), max(2, scaled_height)


def build_proxy_command(
    ffmpeg_path: Path,
    source_path: Path,
    output_path: Path,
    config: AppConfig,
    *,
    has_audio: bool,
) -> list[str]:
    proxy = config.media.proxy
    scale = (
        f"scale=w=min(iw\\,{proxy.max_width}):h=min(ih\\,{proxy.max_height})"
        ":force_original_aspect_ratio=decrease:force_divisible_by=2"
    )
    command = [
        str(ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source_path),
        "-map",
        "0:v:0",
        "-vf",
        scale,
        "-r",
        _format_fps(proxy.fps),
        "-fps_mode",
        "cfr",
        "-c:v",
        proxy.video_codec,
        "-preset",
        proxy.preset,
        "-b:v",
        f"{proxy.video_bitrate_kbps}k",
        "-pix_fmt",
        "yuv420p",
    ]
    if has_audio:
        command.extend(
            [
                "-map",
                "0:a:0",
                "-c:a",
                proxy.audio_codec,
                "-ac",
                "1",
                "-b:a",
                f"{proxy.audio_bitrate_kbps}k",
                "-ar",
                str(config.media.audio.sample_rate_hz),
            ]
        )
    else:
        command.append("-an")
    command.extend(
        [
            "-avoid_negative_ts",
            "make_zero",
            "-movflags",
            "+faststart",
            "-progress",
            "pipe:1",
            "-nostats",
            str(output_path),
        ]
    )
    return command


def build_audio_command(
    ffmpeg_path: Path,
    source_path: Path,
    output_path: Path,
    config: AppConfig,
) -> list[str]:
    audio = config.media.audio
    return [
        str(ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source_path),
        "-map",
        "0:a:0",
        "-vn",
        "-c:a",
        audio.codec,
        "-ac",
        str(audio.channels),
        "-ar",
        str(audio.sample_rate_hz),
        "-b:a",
        f"{audio.bitrate_kbps}k",
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-nostats",
        str(output_path),
    ]


def build_signal_command(
    ffmpeg_path: Path,
    audio_path: Path,
    config: AppConfig,
) -> list[str]:
    silence = config.signals.silence
    samples = max(
        1,
        round(config.media.audio.sample_rate_hz * config.signals.loudness.interval_ms / 1000),
    )
    filters = (
        f"silencedetect=noise={silence.noise_db:g}dB:d={silence.min_duration_seconds:g},"
        f"asetnsamples=n={samples}:pad=1,"
        "astats=metadata=1:reset=1,"
        "ametadata=print:key=lavfi.astats.Overall.RMS_level,"
        "ebur128=framelog=verbose:peak=true"
    )
    # The null muxer keeps this analysis local and emits machine-parseable filter logs.
    return [
        str(ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "info",
        "-i",
        str(audio_path),
        "-af",
        filters,
        "-f",
        "null",
        "-progress",
        "pipe:1",
        "-nostats",
        "-",
    ]


def _format_fps(fps: float) -> str:
    return f"{fps:.6f}".rstrip("0").rstrip(".")


@dataclass(frozen=True)
class FFmpegResult:
    returncode: int
    stderr: str
    progress: tuple[ProgressUpdate, ...]


def run_ffmpeg(
    command: list[str],
    *,
    duration_ms: int | None = None,
    timeout_seconds: float = 7200,
    termination_grace_seconds: float = 5,
    progress_callback: Callable[[ProgressUpdate], None] | None = None,
    cancel_event: threading.Event | None = None,
    max_stderr_lines: int = 200,
    max_stderr_chars: int = 4_000,
) -> FFmpegResult:
    """Run FFmpeg without a shell and terminate only the child process on cancellation."""

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            bufsize=1,
        )
    except OSError as exc:
        raise DependencyError("Could not start FFmpeg.", hint=str(exc)) from exc

    events: queue.Queue[tuple[str, str | None]] = queue.Queue()

    def _read(stream: Iterator[str] | None, channel: str) -> None:
        if stream is None:
            return
        for line in stream:
            events.put((channel, str(line)))
        events.put((channel, None))

    threads = [
        threading.Thread(target=_read, args=(process.stdout, "stdout"), daemon=True),
        threading.Thread(target=_read, args=(process.stderr, "stderr"), daemon=True),
    ]
    for thread in threads:
        thread.start()

    parser = FFmpegProgressParser(duration_ms)
    updates: list[ProgressUpdate] = []
    stderr_lines: list[str] = []
    started = time.monotonic()
    cancelled = False
    try:
        while process.poll() is None or not events.empty():
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                _terminate(process, termination_grace_seconds)
            elif time.monotonic() - started > timeout_seconds:
                _terminate(process, termination_grace_seconds)
                raise FFmpegExecutionError(
                    f"FFmpeg timed out after {timeout_seconds:g} seconds.",
                    hint="The partial output was not accepted; rerun to retry.",
                )
            try:
                channel, line = events.get(timeout=0.05)
            except queue.Empty:
                continue
            if line is None:
                continue
            if channel == "stderr":
                stderr_lines.append(line)
                if len(stderr_lines) > max_stderr_lines:
                    del stderr_lines[:-max_stderr_lines]
            else:
                update = parser.feed(line)
                if update is not None:
                    updates.append(update)
                    if progress_callback is not None:
                        progress_callback(update)
    except KeyboardInterrupt as exc:
        _terminate(process, termination_grace_seconds)
        raise FFmpegCancelled("FFmpeg was interrupted; no partial output was accepted.") from exc
    finally:
        if process.poll() is None:
            _terminate(process, termination_grace_seconds)
        for thread in threads:
            thread.join(timeout=1)
    returncode = process.wait()
    stderr_text = "".join(stderr_lines).strip()
    if max_stderr_chars > 0:
        stderr_text = stderr_text[-max_stderr_chars:]
    stderr = redact_text(stderr_text)
    if cancelled:
        raise FFmpegCancelled("FFmpeg was cancelled; no partial output was accepted.")
    if returncode != 0:
        raise FFmpegExecutionError(f"FFmpeg exited with code {returncode}.", hint=stderr or None)
    return FFmpegResult(returncode=returncode, stderr=stderr, progress=tuple(updates))


def _terminate(process: subprocess.Popen[str], grace_seconds: float) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=grace_seconds)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=grace_seconds)
        except (OSError, subprocess.TimeoutExpired):
            pass
