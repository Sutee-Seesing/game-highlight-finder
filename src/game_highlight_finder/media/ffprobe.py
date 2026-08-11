"""Safe ffprobe command generation and untrusted output parsing."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from game_highlight_finder import __version__
from game_highlight_finder.domain.models import AudioStream, Rational, SourceAsset, VideoStream
from game_highlight_finder.domain.time import optional_seconds_to_ms
from game_highlight_finder.errors import DependencyError, SourceError, ValidationError
from game_highlight_finder.redaction import redact_text

MAX_PROBE_OUTPUT_BYTES = 8 * 1024 * 1024


def build_ffprobe_command(ffprobe_path: Path, source_path: Path) -> list[str]:
    return [
        str(ffprobe_path),
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(source_path),
    ]


def run_ffprobe(ffprobe_path: Path, source_path: Path, *, timeout_seconds: int) -> dict[str, Any]:
    command = build_ffprobe_command(ffprobe_path, source_path)
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
    except subprocess.TimeoutExpired as exc:
        raise DependencyError(
            f"ffprobe timed out after {timeout_seconds} seconds.", hint=str(source_path)
        ) from exc
    except OSError as exc:
        raise DependencyError("Could not start ffprobe.", hint=str(exc)) from exc
    if result.returncode != 0:
        detail = redact_text(result.stderr.strip()[-2000:])
        raise SourceError("ffprobe could not read the source as a video.", hint=detail or None)
    if len(result.stdout.encode("utf-8")) > MAX_PROBE_OUTPUT_BYTES:
        raise ValidationError("ffprobe output exceeded the safe size limit.")
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValidationError("ffprobe returned invalid JSON.", hint=str(exc)) from exc
    if not isinstance(parsed, dict):
        raise ValidationError("ffprobe JSON root must be an object.")
    return parsed


def parse_source_asset(
    raw: dict[str, Any],
    *,
    source_path: Path,
    source_sha256: str,
    size_bytes: int,
    mtime_ns: int,
    probe_version: str,
    created_at: datetime | None = None,
) -> SourceAsset:
    streams = raw.get("streams")
    format_data = raw.get("format")
    if not isinstance(streams, list) or not isinstance(format_data, dict):
        raise ValidationError("ffprobe output must contain streams[] and format{}.")

    warnings: list[str] = []
    video_candidates = [
        stream
        for stream in streams
        if isinstance(stream, dict)
        and stream.get("codec_type") == "video"
        and not _is_attached_picture(stream)
    ]
    if not video_candidates:
        raise ValidationError("No usable video stream was found.")
    if len(video_candidates) > 1:
        warnings.append("Multiple video streams found; selected the first non-attached stream.")
    video = _parse_video_stream(video_candidates[0], warnings)

    audio_streams = [
        _parse_audio_stream(stream, warnings)
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "audio"
    ]
    if not audio_streams:
        warnings.append("No audio stream found.")

    duration_ms = _first_duration_ms(format_data, video_candidates[0])
    if duration_ms is None or duration_ms <= 0:
        raise ValidationError("A positive source duration could not be determined.")

    format_name = format_data.get("format_name")
    if not isinstance(format_name, str) or not format_name.strip():
        raise ValidationError("Source container/format name is missing.")
    timestamp_origin = _safe_optional_ms(format_data.get("start_time"), warnings, "format start")
    if timestamp_origin not in (None, 0):
        warnings.append(f"Source timestamp origin is non-zero ({timestamp_origin} ms).")

    try:
        return SourceAsset(
            created_at=created_at or datetime.now(UTC),
            producer_version=__version__,
            source_id=f"src_{source_sha256[:16]}",
            path=source_path.resolve(),
            sha256=source_sha256,
            size_bytes=size_bytes,
            mtime_ns=mtime_ns,
            duration_ms=duration_ms,
            container=format_name,
            video_stream=video,
            audio_streams=audio_streams,
            selected_video_stream=video.index,
            selected_audio_stream=audio_streams[0].index if audio_streams else None,
            timestamp_origin_ms=timestamp_origin,
            probe_version=probe_version,
            warnings=warnings,
        )
    except PydanticValidationError as exc:
        raise ValidationError("Validated source metadata is invalid.", hint=str(exc)) from exc


def _is_attached_picture(stream: dict[str, Any]) -> bool:
    disposition = stream.get("disposition")
    return isinstance(disposition, dict) and disposition.get("attached_pic") == 1


def _parse_video_stream(stream: dict[str, Any], warnings: list[str]) -> VideoStream:
    index = _required_int(stream.get("index"), "video stream index", minimum=0)
    codec = _required_string(stream.get("codec_name"), "video codec")
    width = _required_int(stream.get("width"), "video width", minimum=1)
    height = _required_int(stream.get("height"), "video height", minimum=1)
    average = _parse_rational(stream.get("avg_frame_rate"), warnings, "average frame rate")
    real = _parse_rational(stream.get("r_frame_rate"), warnings, "real frame rate")
    if average is None:
        warnings.append("Average video frame rate is missing or invalid.")
    if average is not None and real is not None:
        avg_fraction = Fraction(average.numerator, average.denominator)
        real_fraction = Fraction(real.numerator, real.denominator)
        if abs(float(avg_fraction - real_fraction)) > 0.01:
            warnings.append(
                "Average and real frame rates differ; source may be variable-frame-rate."
            )
    return VideoStream(
        index=index,
        codec_name=codec,
        width=width,
        height=height,
        pixel_format=_optional_string(stream.get("pix_fmt")),
        average_frame_rate=average,
        real_frame_rate=real,
        time_base=_parse_rational(stream.get("time_base"), warnings, "video time base"),
        duration_ms=_safe_optional_ms(stream.get("duration"), warnings, "video duration"),
        start_time_ms=_safe_optional_ms(stream.get("start_time"), warnings, "video start"),
    )


def _parse_audio_stream(stream: dict[str, Any], warnings: list[str]) -> AudioStream:
    tags = stream.get("tags")
    language = tags.get("language") if isinstance(tags, dict) else None
    return AudioStream(
        index=_required_int(stream.get("index"), "audio stream index", minimum=0),
        codec_name=_required_string(stream.get("codec_name"), "audio codec"),
        channels=_optional_int(stream.get("channels"), minimum=1),
        sample_rate_hz=_optional_int(stream.get("sample_rate"), minimum=1),
        language=_optional_string(language),
        time_base=_parse_rational(stream.get("time_base"), warnings, "audio time base"),
        duration_ms=_safe_optional_ms(stream.get("duration"), warnings, "audio duration"),
        start_time_ms=_safe_optional_ms(stream.get("start_time"), warnings, "audio start"),
    )


def _first_duration_ms(*sources: dict[str, Any]) -> int | None:
    for source in sources:
        try:
            value = optional_seconds_to_ms(source.get("duration"))
        except ValidationError:
            continue
        if value is not None and value > 0:
            return value
    return None


def _parse_rational(value: object, warnings: list[str], label: str) -> Rational | None:
    if value in (None, "", "N/A", "0/0"):
        return None
    try:
        fraction = Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        warnings.append(f"Invalid {label}: {value!r}.")
        return None
    if fraction.denominator <= 0 or fraction < 0:
        warnings.append(f"Invalid {label}: {value!r}.")
        return None
    return Rational(numerator=fraction.numerator, denominator=fraction.denominator)


def _safe_optional_ms(value: object, warnings: list[str], label: str) -> int | None:
    try:
        return optional_seconds_to_ms(value)
    except ValidationError:
        warnings.append(f"Invalid {label}: {value!r}.")
        return None


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"Missing or invalid {label}.")
    return value.strip()


def _optional_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _required_int(value: object, label: str, *, minimum: int) -> int:
    result = _optional_int(value, minimum=minimum)
    if result is None:
        raise ValidationError(f"Missing or invalid {label}.")
    return result


def _optional_int(value: object, *, minimum: int) -> int | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= minimum else None
