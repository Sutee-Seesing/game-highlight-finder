"""FFmpeg command construction, machine-readable progress, and safe execution."""

from __future__ import annotations

import queue
import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from game_highlight_finder.config import AppConfig
from game_highlight_finder.domain.models import SourceAsset
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


def analysis_audio_stream_indexes(source: SourceAsset, config: AppConfig) -> tuple[int, ...]:
    """Resolve deterministic source audio coverage for analysis and creator clips."""

    indexes = tuple(sorted(stream.index for stream in source.audio_streams))
    if not indexes:
        return ()
    if config.media.audio.source_mix_mode == "first":
        selected = source.selected_audio_stream
        if selected is None:
            raise ValueError("source audio metadata has no selected legacy track")
        return (selected,)
    return indexes


def _append_analysis_audio_mapping(
    command: list[str],
    audio_stream_indexes: tuple[int, ...] | None,
    *,
    limit_mix: bool = True,
) -> None:
    """Map one source track or deterministically mix explicit absolute stream indexes."""

    # ``None`` keeps the historical direct-builder behavior for old call sites/tests.
    if audio_stream_indexes is None:
        command.extend(["-map", "0:a:0"])
        return
    if not audio_stream_indexes:
        raise ValueError("audio stream indexes must be non-empty when audio is enabled")
    if any(index < 0 for index in audio_stream_indexes) or len(set(audio_stream_indexes)) != len(
        audio_stream_indexes
    ):
        raise ValueError("audio stream indexes must be unique non-negative integers")
    if len(audio_stream_indexes) == 1:
        command.extend(["-map", f"0:{audio_stream_indexes[0]}"])
        return

    inputs = "".join(f"[0:{index}]" for index in audio_stream_indexes)
    # Keep source-track relative levels; long-form mixing uses a separate lossless
    # intermediate so its limiter cannot prematurely terminate the amix graph.
    filter_graph = (
        f"{inputs}amix=inputs={len(audio_stream_indexes)}"
        ":normalize=0:dropout_transition=0"
    )
    if limit_mix:
        filter_graph += ",alimiter=limit=0.95"
    filter_graph += "[analysis_audio]"
    command.extend(["-filter_complex", filter_graph, "-map", "[analysis_audio]"])


def build_mixed_audio_intermediate_command(
    ffmpeg_path: Path,
    source_path: Path,
    output_path: Path,
    config: AppConfig,
    *,
    audio_stream_indexes: tuple[int, ...],
    start_ms: int = 0,
    duration_ms: int | None = None,
) -> list[str]:
    """Mix source tracks to lossless float NUT, optionally in a bounded interval."""

    if len(audio_stream_indexes) < 2:
        raise ValueError("lossless intermediate requires multiple audio streams")
    if start_ms < 0 or (duration_ms is not None and duration_ms <= 0):
        raise ValueError("mix interval must have a non-negative start and positive duration")
    if start_ms and duration_ms is None:
        raise ValueError("a bounded mix start requires a duration")
    command = [
        str(ffmpeg_path), "-hide_banner", "-loglevel", "error", "-nostdin",
        "-y",
        *(["-ss", f"{start_ms / 1000:.3f}"] if start_ms else []),
        "-i", str(source_path),
    ]
    _append_analysis_audio_mapping(command, audio_stream_indexes, limit_mix=False)
    command.extend(
        [
            *(["-t", f"{duration_ms / 1000:.3f}"] if duration_ms is not None else []),
            "-vn", "-c:a", "pcm_f32le", "-ac", str(config.media.audio.channels),
            "-ar", str(config.media.audio.sample_rate_hz), "-f", "nut",
            "-progress", "pipe:1", "-nostats", str(output_path),
        ]
    )
    return command


def build_concat_mixed_audio_command(
    ffmpeg_path: Path, concat_list_path: Path, output_path: Path
) -> list[str]:
    """Join bounded PCM-float NUT chunks without decoding or re-encoding."""

    return [
        str(ffmpeg_path), "-hide_banner", "-loglevel", "error", "-nostdin",
        "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list_path),
        "-map", "0:a:0", "-c:a", "copy", "-f", "nut",
        "-progress", "pipe:1", "-nostats", str(output_path),
    ]


def build_proxy_command(
    ffmpeg_path: Path,
    source_path: Path,
    output_path: Path,
    config: AppConfig,
    *,
    has_audio: bool,
    audio_stream_indexes: tuple[int, ...] | None = None,
    mixed_audio_path: Path | None = None,
) -> list[str]:
    if mixed_audio_path is not None and (not has_audio or audio_stream_indexes is not None):
        raise ValueError("mixed proxy audio must replace, not accompany, source audio mapping")
    proxy = config.media.proxy
    use_cuda = proxy.video_acceleration == "cuda"
    scale = (
        f"{'scale_cuda' if use_cuda else 'scale'}="
        f"w=min(iw\\,{proxy.max_width}):h=min(ih\\,{proxy.max_height})"
        ":force_original_aspect_ratio=decrease:force_divisible_by=2"
    )
    if use_cuda:
        scale += ":format=nv12"
    command = [
        str(ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        *(["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"] if use_cuda else []),
        "-i",
        str(source_path),
        *(["-i", str(mixed_audio_path)] if mixed_audio_path is not None else []),
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
        *([] if use_cuda else ["-pix_fmt", "yuv420p"]),
    ]
    if has_audio:
        if mixed_audio_path is not None:
            command.extend(["-map", "1:a:0", "-af", "alimiter=limit=0.90:level=0"])
        else:
            _append_analysis_audio_mapping(command, audio_stream_indexes)
        command.extend(
            [
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
    *,
    audio_stream_indexes: tuple[int, ...] | None = None,
    mixed_audio_input: bool = False,
) -> list[str]:
    if mixed_audio_input and audio_stream_indexes is not None:
        raise ValueError("mixed audio input must not map original source streams")
    audio = config.media.audio
    command = [
        str(ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source_path),
    ]
    _append_analysis_audio_mapping(command, audio_stream_indexes)
    if mixed_audio_input:
        command.extend(["-af", "alimiter=limit=0.90:level=0"])
    command.extend(
        [
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
    )
    return command


def build_signal_command(
    ffmpeg_path: Path,
    audio_path: Path,
    config: AppConfig,
    *,
    interval_ms: int | None = None,
) -> list[str]:
    silence = config.signals.silence
    effective_interval_ms = (
        config.signals.loudness.interval_ms if interval_ms is None else interval_ms
    )
    if effective_interval_ms <= 0:
        raise ValueError("signal interval must be positive")
    samples = max(
        1,
        round(config.media.audio.sample_rate_hz * effective_interval_ms / 1000),
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
        "-nostdin",
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


def _format_ms_seconds(milliseconds: int) -> str:
    """Format integer milliseconds without float rounding or locale effects."""

    if isinstance(milliseconds, bool) or not isinstance(milliseconds, int) or milliseconds < 0:
        raise ValueError("milliseconds must be a non-negative integer")
    formatted = format(Decimal(milliseconds) / Decimal(1000), "f")
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return formatted or "0"


def build_window_proxy_command(
    ffmpeg_path: Path,
    analysis_proxy_path: Path,
    output_path: Path,
    *,
    proxy_start_ms: int,
    duration_ms: int,
    has_audio: bool,
    video_codec: str = "h264_nvenc",
    preset: str = "p4",
) -> list[str]:
    """Cut a Scout window from the committed analysis proxy only."""

    if duration_ms <= 0:
        raise ValueError("window duration must be positive")
    command = [
        str(ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(analysis_proxy_path),
        "-ss",
        _format_ms_seconds(proxy_start_ms),
        "-t",
        _format_ms_seconds(duration_ms),
        "-map",
        "0:v:0",
        "-c:v",
        video_codec,
        "-preset",
        preset,
        "-pix_fmt",
        "yuv420p",
    ]
    if has_audio:
        command.extend(["-map", "0:a:0?", "-c:a", "aac", "-ac", "1"])
    else:
        command.append("-an")
    command.extend(
        [
            "-avoid_negative_ts",
            "make_zero",
            "-reset_timestamps",
            "1",
            "-movflags",
            "+faststart",
            "-progress",
            "pipe:1",
            "-nostats",
            str(output_path),
        ]
    )
    return command


def build_slow_motion_proxy_command(
    ffmpeg_path: Path,
    input_path: Path,
    output_path: Path,
    *,
    slowdown_factor: int,
    has_audio: bool,
    video_codec: str = "h264_nvenc",
    preset: str = "p4",
) -> list[str]:
    """Slow a pre-cut candidate-local proxy while preserving mono audio."""

    if slowdown_factor not in {1, 2, 4}:
        raise ValueError("slowdown factor must be one of 1, 2, or 4")
    command = [
        str(ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
    ]
    if slowdown_factor != 1:
        command.extend(["-vf", f"setpts={slowdown_factor}*(PTS-STARTPTS)"])
    command.extend(["-c:v", video_codec, "-preset", preset, "-pix_fmt", "yuv420p"])
    if has_audio:
        command.extend(["-map", "0:a:0?"])
        if slowdown_factor != 1:
            atempo = ",".join("atempo=0.5" for _ in range(slowdown_factor.bit_length() - 1))
            command.extend(["-af", f"asetpts=PTS-STARTPTS,{atempo}"])
        command.extend(["-c:a", "aac", "-ac", "1"])
    else:
        command.append("-an")
    command.extend(
        [
            "-avoid_negative_ts",
            "make_zero",
            "-reset_timestamps",
            "1",
            "-movflags",
            "+faststart",
            "-progress",
            "pipe:1",
            "-nostats",
            str(output_path),
        ]
    )
    return command


def build_extraction_command(
    ffmpeg_path: Path,
    source_path: Path,
    output_path: Path,
    *,
    start_ms: int,
    end_ms: int,
    extraction: object,
    has_audio: bool,
    timestamp_origin_ms: int = 0,
    audio_stream_indexes: tuple[int, ...] | None = None,
) -> list[str]:
    """Build an accurate or explicitly approximate source extraction command."""

    if end_ms <= start_ms:
        raise ValueError("extraction interval must be non-empty")
    mode = getattr(extraction, "mode", "accurate")
    duration_ms = end_ms - start_ms
    seek_ms = start_ms + max(0, timestamp_origin_ms)
    command = [
        str(ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-ss",
        _format_ms_seconds(seek_ms),
        "-i",
        str(source_path),
        "-t",
        _format_ms_seconds(duration_ms),
        "-map",
        "0:v:0",
    ]
    mixed_audio = has_audio and audio_stream_indexes is not None and len(audio_stream_indexes) > 1
    if has_audio:
        _append_analysis_audio_mapping(command, audio_stream_indexes)
    if mode == "copy":
        command.extend(["-c:v", "copy"])
        if mixed_audio:
            command.extend(["-c:a", str(getattr(extraction, "audio_codec", "aac"))])
        elif has_audio:
            command.extend(["-c:a", "copy"])
        else:
            command.append("-an")
    else:
        video_codec = str(getattr(extraction, "video_codec", "h264_nvenc"))
        preset = str(getattr(extraction, "preset", "p5"))
        command.extend(["-c:v", video_codec, "-preset", preset])
        if video_codec == "h264_nvenc":
            command.extend(["-rc", "vbr", "-cq", str(getattr(extraction, "crf", 18)), "-b:v", "0"])
        else:
            command.extend(["-crf", str(getattr(extraction, "crf", 18))])
        command.extend(["-pix_fmt", "yuv420p"])
        if has_audio:
            command.extend(["-c:a", str(getattr(extraction, "audio_codec", "aac"))])
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


def build_thumbnail_command(
    ffmpeg_path: Path,
    source_path: Path,
    output_path: Path,
    *,
    at_ms: int,
    width: int = 320,
    height: int = 180,
) -> list[str]:
    if at_ms < 0 or width <= 0 or height <= 0:
        raise ValueError("thumbnail time and dimensions must be positive")
    return [
        str(ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-ss",
        _format_ms_seconds(at_ms),
        "-i",
        str(source_path),
        "-frames:v",
        "1",
        "-vf",
        f"scale={width}:{height}:force_original_aspect_ratio=decrease",
        "-q:v",
        "2",
        "-f",
        "image2",
        str(output_path),
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
