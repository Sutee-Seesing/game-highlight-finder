"""Private, dataset-driven review-proxy generation.

Review proxies are convenience copies for human inspection only.  They never
replace the authoritative benchmark source and are deliberately isolated from
the production proxy/extraction encoders and from every provider integration.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any

from game_highlight_finder.benchmark.models import (
    BenchmarkAnnotations,
    BenchmarkCase,
    BenchmarkDataset,
)
from game_highlight_finder.config import AppConfig
from game_highlight_finder.errors import DependencyError, SourceError, StorageError, ValidationError
from game_highlight_finder.media.ffmpeg import ProgressUpdate, run_ffmpeg
from game_highlight_finder.media.ffprobe import run_ffprobe
from game_highlight_finder.media.tools import executable_version, require_executable
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.hashing import hash_file

REVIEW_PROXY_MANIFEST_VERSION = 1
MAX_DURATION_DRIFT_MS = 250
DEFAULT_OUTPUT_SUBDIRECTORY = ("benchmarks", "private", "review-proxies")


@dataclass(frozen=True)
class ReviewProxyProfile:
    """One deliberately compact, human-review-only encoding profile."""

    name: str
    max_height: int
    max_fps: float
    video_bitrate_kbps: int
    audio_bitrate_kbps: int

    def __post_init__(self) -> None:
        if self.max_height < 144:
            raise ValueError("review proxy max height must be at least 144")
        if self.max_fps <= 0:
            raise ValueError("review proxy max FPS must be positive")
        if self.video_bitrate_kbps < 100 or self.audio_bitrate_kbps < 16:
            raise ValueError("review proxy bitrates are too small")

    def payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "max_height": self.max_height,
            "max_fps": self.max_fps,
            "video_bitrate_kbps": self.video_bitrate_kbps,
            "audio_bitrate_kbps": self.audio_bitrate_kbps,
        }

    def summary(self) -> str:
        return (
            f"max {self.max_height}p, {self.max_fps:g} fps, "
            f"{self.video_bitrate_kbps} kbps video + {self.audio_bitrate_kbps} kbps AAC"
        )


DEFAULT_REVIEW_PROFILE = ReviewProxyProfile(
    name="default",
    max_height=720,
    max_fps=30.0,
    video_bitrate_kbps=1_000,
    audio_bitrate_kbps=96,
)
SMALL_REVIEW_PROFILE = ReviewProxyProfile(
    name="small",
    max_height=540,
    max_fps=30.0,
    video_bitrate_kbps=750,
    audio_bitrate_kbps=64,
)


def make_review_profile(
    *,
    small: bool = False,
    max_height: int | None = None,
    max_fps: float | None = None,
    video_bitrate_kbps: int | None = None,
    audio_bitrate_kbps: int | None = None,
) -> ReviewProxyProfile:
    """Build a profile from the small/default preset and explicit CLI overrides."""

    base = SMALL_REVIEW_PROFILE if small else DEFAULT_REVIEW_PROFILE
    custom = any(
        value is not None for value in (max_height, max_fps, video_bitrate_kbps, audio_bitrate_kbps)
    )
    return ReviewProxyProfile(
        name=f"{base.name}-custom" if custom else base.name,
        max_height=max_height if max_height is not None else base.max_height,
        max_fps=max_fps if max_fps is not None else base.max_fps,
        video_bitrate_kbps=(
            video_bitrate_kbps if video_bitrate_kbps is not None else base.video_bitrate_kbps
        ),
        audio_bitrate_kbps=(
            audio_bitrate_kbps if audio_bitrate_kbps is not None else base.audio_bitrate_kbps
        ),
    )


@dataclass(frozen=True)
class EncoderCapability:
    encoder: str
    available: bool
    ffmpeg_path: Path
    detail: str


def probe_encoder_capability(ffmpeg_path: Path, encoder: str = "h264_nvenc") -> EncoderCapability:
    """Probe FFmpeg's encoder registry without scraping an unbounded log."""

    try:
        result = subprocess.run(
            [str(ffmpeg_path), "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DependencyError("Could not probe FFmpeg encoder capability.", hint=str(exc)) from exc
    detail = (result.stdout + "\n" + result.stderr)[-20_000:]
    if result.returncode != 0:
        raise DependencyError("FFmpeg encoder capability probe failed.", hint=detail[-2_000:])
    available = re.search(rf"(?m)^\s*[^\n]*\b{re.escape(encoder)}\b", detail) is not None
    return EncoderCapability(
        encoder=encoder, available=available, ffmpeg_path=ffmpeg_path, detail=detail
    )


@dataclass(frozen=True)
class MediaProbe:
    duration_ms: int
    width: int
    height: int
    video_codec: str
    fps: float | None
    audio_present: bool
    audio_codec: str | None
    audio_channels: int | None
    bitrate_kbps: int | None
    format_name: str


@dataclass(frozen=True)
class FileFingerprint:
    sha256: str
    size_bytes: int
    mtime_ns: int


@dataclass(frozen=True)
class ResolvedReviewCase:
    case: BenchmarkCase
    source_path: Path
    annotation_path: Path
    annotation: BenchmarkAnnotations
    source_probe: MediaProbe
    source_fingerprint: FileFingerprint


@dataclass(frozen=True)
class ReviewProxyCaseResult:
    case_id: str
    status: str
    source_path: Path
    proxy_path: Path
    source_size_bytes: int
    proxy_size_bytes: int
    source_duration_ms: int
    proxy_duration_ms: int
    duration_delta_ms: int
    source_sha256: str
    proxy_sha256: str
    video_codec: str
    width: int
    height: int
    fps: float | None
    audio_retained: bool
    encoder: str

    @property
    def reduction_percent(self) -> float:
        if self.source_size_bytes <= 0:
            return 0.0
        return (self.source_size_bytes - self.proxy_size_bytes) * 100.0 / self.source_size_bytes


@dataclass(frozen=True)
class ReviewProxyBatchResult:
    dataset_path: Path
    output_dir: Path
    manifest_path: Path
    profile: ReviewProxyProfile
    encoder: str
    ffmpeg_identity: str
    cases: tuple[ReviewProxyCaseResult, ...]

    @property
    def generated_count(self) -> int:
        return sum(item.status == "COMPLETED" for item in self.cases)

    @property
    def cache_hit_count(self) -> int:
        return sum(item.status == "CACHE_HIT" for item in self.cases)

    @property
    def original_total_size(self) -> int:
        return sum(item.source_size_bytes for item in self.cases)

    @property
    def proxy_total_size(self) -> int:
        return sum(item.proxy_size_bytes for item in self.cases)

    @property
    def total_reduction_percent(self) -> float:
        if self.original_total_size <= 0:
            return 0.0
        return (self.original_total_size - self.proxy_total_size) * 100.0 / self.original_total_size

    @property
    def largest_proxy_bytes(self) -> int:
        return max((item.proxy_size_bytes for item in self.cases), default=0)

    @property
    def smallest_proxy_bytes(self) -> int:
        return min((item.proxy_size_bytes for item in self.cases), default=0)

    @property
    def maximum_duration_delta_ms(self) -> int:
        return max((item.duration_delta_ms for item in self.cases), default=0)


ProgressCallback = Callable[[str, ProgressUpdate], None]


def default_review_proxy_dir(config: AppConfig) -> Path:
    return config.storage.data_dir.resolve().joinpath(*DEFAULT_OUTPUT_SUBDIRECTORY)


def load_review_dataset(path: Path) -> BenchmarkDataset:
    """Load a strict private dataset manifest without guessing missing fields."""

    target = path.expanduser().resolve()
    try:
        value = read_json(target)
        return BenchmarkDataset.model_validate(value)
    except Exception as exc:
        if isinstance(exc, ValidationError):
            raise
        raise ValidationError(
            "Review-proxy dataset manifest is invalid.", hint=str(target)
        ) from exc


def compute_generation_config_fingerprint(
    profile: ReviewProxyProfile,
    *,
    encoder: str,
    ffmpeg_identity: str,
) -> str:
    payload = {
        "profile": profile.payload(),
        "encoder": encoder,
        "ffmpeg_identity": ffmpeg_identity,
        "format": "mp4-h264-aac-yuv420p-faststart",
        "timeline_policy": "full-source-no-trim-no-speed-change",
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_review_proxy_command(
    ffmpeg_path: Path,
    source_path: Path,
    output_path: Path,
    profile: ReviewProxyProfile,
    *,
    encoder: str = "h264_nvenc",
    has_audio: bool,
    audio_channels: int | None = None,
) -> list[str]:
    """Build a full-duration, no-crop, no-upscale MP4 review command."""

    scale = (
        f"scale=w=min(iw\\,iw*{profile.max_height}/ih):h=min(ih\\,{profile.max_height}):"
        "force_original_aspect_ratio=decrease:force_divisible_by=2"
    )
    command = [
        str(ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-copyts",
        "-start_at_zero",
        "-i",
        str(source_path),
        "-map",
        "0:v:0",
        "-vf",
        scale,
        "-fpsmax",
        _format_fps(profile.max_fps),
        "-c:v",
        encoder,
    ]
    if encoder == "h264_nvenc":
        command.extend(["-preset", "p5"])
    else:
        command.extend(["-preset", "veryfast"])
    command.extend(
        [
            "-b:v",
            f"{profile.video_bitrate_kbps}k",
            "-pix_fmt",
            "yuv420p",
        ]
    )
    if has_audio:
        command.extend(
            [
                "-map",
                "0:a:0",
                "-c:a",
                "aac",
                "-ac",
                "2" if (audio_channels or 0) >= 2 else "1",
                "-b:a",
                f"{profile.audio_bitrate_kbps}k",
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


def resolve_review_case(
    dataset_path: Path,
    case: BenchmarkCase,
    config: AppConfig,
    *,
    ffprobe_path: Path,
) -> ResolvedReviewCase:
    """Resolve and verify one dataset case and its private annotation metadata."""

    source_path = _resolve_private_path(case.source_path, dataset_path.parent)
    annotation_path = _resolve_private_path(case.annotation_path, dataset_path.parent)
    if not source_path.is_file():
        raise SourceError("Benchmark source video is missing.", hint=str(source_path))
    if not annotation_path.is_file():
        raise ValidationError(
            "Benchmark annotation metadata is missing.", hint=str(annotation_path)
        )
    fingerprint = _fingerprint_source(source_path)
    if fingerprint.sha256 != case.expected_source_sha256:
        raise SourceError(
            "Benchmark source SHA-256 does not match the accepted dataset.",
            hint=(
                f"Case {case.case_id}: expected {case.expected_source_sha256}; "
                f"observed {fingerprint.sha256}."
            ),
        )
    source_probe = probe_media(ffprobe_path, source_path, config)
    try:
        annotation_value = read_json(annotation_path)
        annotation = BenchmarkAnnotations.model_validate(annotation_value)
    except Exception as exc:
        raise ValidationError(
            "Benchmark annotation metadata is invalid.", hint=str(annotation_path)
        ) from exc
    if annotation.case_id != case.case_id:
        raise ValidationError(
            "Dataset case and annotation case IDs do not match.", hint=case.case_id
        )
    if annotation.source_sha256 != fingerprint.sha256:
        raise SourceError("Annotation source SHA-256 does not match the dataset source.")
    if annotation.source_path is not None:
        annotation_source = annotation.source_path.expanduser().resolve()
        if annotation_source != source_path:
            raise SourceError("Annotation source_path does not match the dataset case.")
    if annotation.game_profile != case.game_profile:
        raise ValidationError(
            "Dataset and annotation game profiles do not match.", hint=case.case_id
        )
    if abs(annotation.source_duration_ms - source_probe.duration_ms) > MAX_DURATION_DRIFT_MS:
        raise SourceError("Annotation duration does not match the authoritative source duration.")
    after = _fingerprint_source(source_path)
    if after != fingerprint:
        raise SourceError(
            "Benchmark source changed while it was being verified.", hint=case.case_id
        )
    return ResolvedReviewCase(
        case=case,
        source_path=source_path,
        annotation_path=annotation_path,
        annotation=annotation,
        source_probe=source_probe,
        source_fingerprint=fingerprint,
    )


def probe_media(ffprobe_path: Path, path: Path, config: AppConfig) -> MediaProbe:
    raw = run_ffprobe(ffprobe_path, path, timeout_seconds=config.tools.probe_timeout_seconds)
    streams = raw.get("streams")
    format_data = raw.get("format")
    if not isinstance(streams, list) or not isinstance(format_data, dict):
        raise ValidationError("ffprobe media output must contain streams[] and format{}.")
    videos = [
        stream
        for stream in streams
        if isinstance(stream, dict)
        and stream.get("codec_type") == "video"
        and not _attached_picture(stream)
    ]
    if not videos:
        raise ValidationError("Media contains no usable video stream.", hint=str(path))
    video = videos[0]
    duration_ms = _duration_ms(format_data.get("duration") or video.get("duration"))
    width = _positive_int(video.get("width"), "video width")
    height = _positive_int(video.get("height"), "video height")
    codec = _required_text(video.get("codec_name"), "video codec")
    audio_streams = [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "audio"
    ]
    audio = audio_streams[0] if audio_streams else None
    return MediaProbe(
        duration_ms=duration_ms,
        width=width,
        height=height,
        video_codec=codec,
        fps=_parse_fps(video.get("avg_frame_rate")),
        audio_present=audio is not None,
        audio_codec=_required_text(audio.get("codec_name"), "audio codec") if audio else None,
        audio_channels=_optional_int(audio.get("channels")) if audio else None,
        bitrate_kbps=_bitrate_kbps(video.get("bit_rate") or format_data.get("bit_rate")),
        format_name=_required_text(format_data.get("format_name"), "container format"),
    )


def make_review_proxies(
    dataset_path: Path,
    config: AppConfig,
    *,
    profile: ReviewProxyProfile | None = None,
    output_dir: Path | None = None,
    overwrite: bool = False,
    allow_cpu_fallback: bool = False,
    case_id: str | None = None,
    progress_callback: ProgressCallback | None = None,
) -> ReviewProxyBatchResult:
    """Generate private proxies for dataset cases, or return verified cache hits."""

    dataset_target = dataset_path.expanduser().resolve()
    if not dataset_target.is_file():
        raise ValidationError(
            "Review-proxy dataset manifest does not exist.", hint=str(dataset_target)
        )
    dataset = load_review_dataset(dataset_target)
    selected_cases = tuple(
        case for case in dataset.cases if case_id is None or case.case_id == case_id
    )
    if not selected_cases:
        raise ValidationError(
            "Requested review-proxy case was not found in the dataset.", hint=case_id
        )
    selected_profile = profile or DEFAULT_REVIEW_PROFILE
    output = (output_dir or default_review_proxy_dir(config)).expanduser().resolve()
    ffmpeg = require_executable("ffmpeg", config.tools.ffmpeg_path)
    ffprobe = require_executable("ffprobe", config.tools.ffprobe_path)
    ffmpeg_identity = executable_version(ffmpeg)
    capability = probe_encoder_capability(ffmpeg)
    if capability.available:
        encoder = "h264_nvenc"
    elif allow_cpu_fallback:
        encoder = "libx264"
    else:
        raise DependencyError(
            "NVENC encoder h264_nvenc is unavailable; review-proxy generation is GPU-required.",
            hint="Use --allow-cpu-fallback only when explicitly accepting CPU encoding.",
        )
    config_fingerprint = compute_generation_config_fingerprint(
        selected_profile, encoder=encoder, ffmpeg_identity=ffmpeg_identity
    )
    manifest_path = output / "manifest.json"
    manifest = _load_manifest(manifest_path)
    _initialize_manifest(
        manifest,
        dataset=dataset,
        output=output,
        profile=selected_profile,
        encoder=encoder,
        ffmpeg_identity=ffmpeg_identity,
        config_fingerprint=config_fingerprint,
    )
    results: list[ReviewProxyCaseResult] = []
    for case in selected_cases:
        resolved = resolve_review_case(
            dataset_target,
            case,
            config,
            ffprobe_path=ffprobe,
        )
        _assert_output_directory_safe(output, resolved.source_path)
        output.mkdir(parents=True, exist_ok=True)
        proxy_path = output / f"{case.case_id}-review.mp4"
        prior = _manifest_case(manifest, case.case_id)
        if not overwrite and _cache_is_valid(
            prior,
            proxy_path=proxy_path,
            source=resolved,
            config_fingerprint=config_fingerprint,
        ):
            result = _cache_result(prior, resolved, proxy_path, encoder)
            results.append(result)
            prior["status"] = "CACHE_HIT"
            _write_manifest(manifest_path, manifest)
            continue
        if proxy_path.exists() and not overwrite:
            raise StorageError(
                "A stale review proxy already exists; pass --overwrite to regenerate it.",
                hint=str(proxy_path),
            )
        temp_path = _temporary_output_path(proxy_path)
        try:
            callback = (
                None
                if progress_callback is None
                else lambda update, current=case.case_id: progress_callback(current, update)
            )
            run_ffmpeg(
                build_review_proxy_command(
                    ffmpeg,
                    resolved.source_path,
                    temp_path,
                    selected_profile,
                    encoder=encoder,
                    has_audio=resolved.source_probe.audio_present,
                    audio_channels=resolved.source_probe.audio_channels,
                ),
                duration_ms=resolved.source_probe.duration_ms,
                timeout_seconds=config.tools.ffmpeg_timeout_seconds,
                termination_grace_seconds=config.tools.termination_grace_seconds,
                progress_callback=callback,
            )
            if not temp_path.is_file():
                raise StorageError(
                    "FFmpeg completed without producing a review proxy.", hint=case.case_id
                )
            proxy_probe = probe_media(ffprobe, temp_path, config)
            _validate_proxy(resolved.source_probe, proxy_probe, selected_profile)
            proxy_fingerprint = _fingerprint_output(temp_path)
            after = _fingerprint_source(resolved.source_path)
            if after != resolved.source_fingerprint:
                raise SourceError(
                    "Authoritative benchmark source changed during encoding.", hint=case.case_id
                )
            temp_path.replace(proxy_path)
            result = ReviewProxyCaseResult(
                case_id=case.case_id,
                status="COMPLETED",
                source_path=resolved.source_path,
                proxy_path=proxy_path,
                source_size_bytes=resolved.source_fingerprint.size_bytes,
                proxy_size_bytes=proxy_fingerprint.size_bytes,
                source_duration_ms=resolved.source_probe.duration_ms,
                proxy_duration_ms=proxy_probe.duration_ms,
                duration_delta_ms=abs(proxy_probe.duration_ms - resolved.source_probe.duration_ms),
                source_sha256=resolved.source_fingerprint.sha256,
                proxy_sha256=proxy_fingerprint.sha256,
                video_codec=proxy_probe.video_codec,
                width=proxy_probe.width,
                height=proxy_probe.height,
                fps=proxy_probe.fps,
                audio_retained=(
                    not resolved.source_probe.audio_present or proxy_probe.audio_present
                ),
                encoder=encoder,
            )
            results.append(result)
            manifest["cases"][case.case_id] = _manifest_record(
                result,
                profile=selected_profile,
                ffmpeg_identity=ffmpeg_identity,
                config_fingerprint=config_fingerprint,
                bitrate_profile=selected_profile.summary(),
            )
            _write_manifest(manifest_path, manifest)
        except BaseException as exc:
            _manifest_case(manifest, case.case_id).update(
                {
                    "case_id": case.case_id,
                    "status": "FAILED",
                    "source_sha256": resolved.source_fingerprint.sha256,
                    "source_duration_ms": resolved.source_probe.duration_ms,
                    "generation_config_fingerprint": config_fingerprint,
                    "error": str(exc),
                }
            )
            _write_manifest(manifest_path, manifest)
            raise
        finally:
            with suppress(OSError):
                temp_path.unlink(missing_ok=True)
    return ReviewProxyBatchResult(
        dataset_path=dataset_target,
        output_dir=output,
        manifest_path=manifest_path,
        profile=selected_profile,
        encoder=encoder,
        ffmpeg_identity=ffmpeg_identity,
        cases=tuple(results),
    )


def _assert_output_directory_safe(output: Path, source: Path) -> None:
    source_dir = source.parent.resolve()
    if output.is_relative_to(source_dir) or source_dir.is_relative_to(output):
        raise ValidationError(
            "Review-proxy output must be separate from the authoritative source tree.",
            hint=str(output),
        )


def _resolve_private_path(path: Path, base: Path) -> Path:
    return path.expanduser().resolve() if path.is_absolute() else (base / path).resolve()


def _fingerprint_source(path: Path) -> FileFingerprint:
    try:
        before = path.stat()
    except OSError as exc:
        raise SourceError("Cannot stat benchmark source.", hint=str(path)) from exc
    sha256 = hash_file(path, source=True)
    try:
        after = path.stat()
    except OSError as exc:
        raise SourceError("Benchmark source disappeared during hashing.", hint=str(path)) from exc
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise SourceError("Benchmark source changed during hashing.", hint=str(path))
    return FileFingerprint(sha256=sha256, size_bytes=after.st_size, mtime_ns=after.st_mtime_ns)


def _fingerprint_output(path: Path) -> FileFingerprint:
    try:
        stat = path.stat()
    except OSError as exc:
        raise StorageError("Cannot stat generated review proxy.", hint=str(path)) from exc
    return FileFingerprint(
        sha256=hash_file(path), size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns
    )


def _cache_is_valid(
    record: Mapping[str, object],
    *,
    proxy_path: Path,
    source: ResolvedReviewCase,
    config_fingerprint: str,
) -> bool:
    if not record or record.get("status") not in {"COMPLETED", "CACHE_HIT"}:
        return False
    if record.get("source_sha256") != source.source_fingerprint.sha256:
        return False
    if record.get("generation_config_fingerprint") != config_fingerprint:
        return False
    if record.get("proxy_path") != str(proxy_path) or not proxy_path.is_file():
        return False
    persisted_hash = record.get("proxy_sha256")
    if not isinstance(persisted_hash, str):
        return False
    return hash_file(proxy_path) == persisted_hash


def _cache_result(
    record: Mapping[str, object],
    source: ResolvedReviewCase,
    proxy_path: Path,
    encoder: str,
) -> ReviewProxyCaseResult:
    try:
        return ReviewProxyCaseResult(
            case_id=source.case.case_id,
            status="CACHE_HIT",
            source_path=source.source_path,
            proxy_path=proxy_path,
            source_size_bytes=source.source_fingerprint.size_bytes,
            proxy_size_bytes=int(str(record["size_bytes"])),
            source_duration_ms=source.source_probe.duration_ms,
            proxy_duration_ms=int(str(record["proxy_duration_ms"])),
            duration_delta_ms=int(str(record["duration_delta_ms"])),
            source_sha256=source.source_fingerprint.sha256,
            proxy_sha256=str(record["proxy_sha256"]),
            video_codec=str(record["video_codec"]),
            width=int(str(record["width"])),
            height=int(str(record["height"])),
            fps=float(str(record["fps"])) if record.get("fps") is not None else None,
            audio_retained=bool(record["audio_retained"]),
            encoder=encoder,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StorageError(
            "Review-proxy cache manifest record is incomplete.", hint=str(exc)
        ) from exc


def _manifest_record(
    result: ReviewProxyCaseResult,
    *,
    profile: ReviewProxyProfile,
    ffmpeg_identity: str,
    config_fingerprint: str,
    bitrate_profile: str,
) -> dict[str, object]:
    return {
        "case_id": result.case_id,
        "status": result.status,
        "source_path": str(result.source_path),
        "source_sha256": result.source_sha256,
        "source_duration_ms": result.source_duration_ms,
        "proxy_path": str(result.proxy_path),
        "proxy_sha256": result.proxy_sha256,
        "proxy_duration_ms": result.proxy_duration_ms,
        "size_bytes": result.proxy_size_bytes,
        "video_codec": result.video_codec,
        "width": result.width,
        "height": result.height,
        "fps": result.fps,
        "bitrate_profile": bitrate_profile,
        "encoder": result.encoder,
        "ffmpeg_identity": ffmpeg_identity,
        "generation_config_fingerprint": config_fingerprint,
        "duration_delta_ms": result.duration_delta_ms,
        "audio_retained": result.audio_retained,
        "profile": profile.payload(),
        "created_at": datetime.now(UTC).isoformat(),
    }


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"cases": {}}
    try:
        value = read_json(path)
    except Exception as exc:
        raise StorageError("Review-proxy manifest cannot be read.", hint=str(path)) from exc
    if not isinstance(value, dict) or not isinstance(value.get("cases", {}), dict):
        raise ValidationError("Review-proxy manifest has an invalid schema.", hint=str(path))
    return value


def _initialize_manifest(
    manifest: dict[str, Any],
    *,
    dataset: BenchmarkDataset,
    output: Path,
    profile: ReviewProxyProfile,
    encoder: str,
    ffmpeg_identity: str,
    config_fingerprint: str,
) -> None:
    manifest.update(
        {
            "schema_version": REVIEW_PROXY_MANIFEST_VERSION,
            "benchmark_id": dataset.benchmark_id,
            "output_dir": str(output),
            "profile": profile.payload(),
            "encoder": encoder,
            "ffmpeg_identity": ffmpeg_identity,
            "generation_config_fingerprint": config_fingerprint,
            "updated_at": datetime.now(UTC).isoformat(),
        }
    )
    if not isinstance(manifest.get("cases"), dict):
        manifest["cases"] = {}


def _manifest_case(manifest: dict[str, Any], case_id: str) -> dict[str, Any]:
    cases = manifest.setdefault("cases", {})
    if not isinstance(cases, dict):
        raise ValidationError("Review-proxy manifest cases must be an object.")
    value = cases.setdefault(case_id, {})
    if not isinstance(value, dict):
        value = {}
        cases[case_id] = value
    return value


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = datetime.now(UTC).isoformat()
    atomic_write_json(path, manifest)


def _validate_proxy(source: MediaProbe, proxy: MediaProbe, profile: ReviewProxyProfile) -> None:
    if "mp4" not in proxy.format_name.lower():
        raise ValidationError("Review proxy output is not an MP4 container.")
    if proxy.video_codec not in {"h264", "avc1"}:
        raise ValidationError("Review proxy output is not H.264.", hint=proxy.video_codec)
    if proxy.width > source.width or proxy.height > source.height:
        raise ValidationError("Review proxy must never upscale the source.")
    if proxy.height > profile.max_height:
        raise ValidationError("Review proxy exceeds the configured maximum height.")
    source_ratio = source.width / source.height
    proxy_ratio = proxy.width / proxy.height
    if abs(proxy_ratio - source_ratio) / source_ratio > 0.02:
        raise ValidationError("Review proxy aspect ratio differs materially from the source.")
    if proxy.fps is not None and proxy.fps > profile.max_fps + 0.5:
        raise ValidationError("Review proxy frame rate exceeds the configured cap.")
    if source.fps is not None and proxy.fps is not None and proxy.fps > source.fps + 0.5:
        raise ValidationError("Review proxy frame rate must not increase the source rate.")
    if abs(proxy.duration_ms - source.duration_ms) > MAX_DURATION_DRIFT_MS:
        raise ValidationError(
            "Review proxy duration drift exceeds the strict 250 ms tolerance.",
            hint=f"source={source.duration_ms} ms proxy={proxy.duration_ms} ms",
        )
    if source.audio_present and not proxy.audio_present:
        raise ValidationError("Source audio was present but review proxy audio is missing.")
    if proxy.audio_present and proxy.audio_codec != "aac":
        raise ValidationError("Review proxy audio must be AAC.")


def _temporary_output_path(output: Path) -> Path:
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{output.stem}.", suffix=".partial.mp4", dir=output.parent
        )
        os.close(descriptor)
        temporary = Path(name)
        temporary.unlink(missing_ok=True)
        return temporary
    except OSError as exc:
        raise StorageError(
            "Cannot create a private review-proxy temporary path.", hint=str(exc)
        ) from exc


def _attached_picture(stream: Mapping[str, object]) -> bool:
    disposition = stream.get("disposition")
    return isinstance(disposition, dict) and disposition.get("attached_pic") == 1


def _duration_ms(value: object) -> int:
    try:
        decimal = Decimal(str(value))
        if not decimal.is_finite() or decimal <= 0:
            raise InvalidOperation
        result = int((decimal * Decimal(1000)).to_integral_value(rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError("Media duration is missing or invalid.") from None
    if result <= 0:
        raise ValidationError("Media duration must be positive.")
    return result


def _parse_fps(value: object) -> float | None:
    if value in (None, "", "0/0", "N/A"):
        return None
    try:
        fraction = Fraction(str(value))
        result = float(fraction)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    return result if result > 0 else None


def _bitrate_kbps(value: object) -> int | None:
    if value in (None, "", "N/A"):
        return None
    try:
        result = int(str(value)) // 1000
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ValidationError(f"{label} is invalid.")
    try:
        result = int(str(value))
    except (TypeError, ValueError):
        raise ValidationError(f"{label} is invalid.") from None
    if result <= 0:
        raise ValidationError(f"{label} is invalid.")
    return result


def _optional_int(value: object) -> int | None:
    try:
        result = int(str(value)) if value is not None else None
    except (TypeError, ValueError):
        return None
    return result if result is None or result > 0 else None


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} is missing or invalid.")
    return value.strip()


def _format_fps(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


__all__ = [
    "DEFAULT_REVIEW_PROFILE",
    "MAX_DURATION_DRIFT_MS",
    "SMALL_REVIEW_PROFILE",
    "ReviewProxyBatchResult",
    "ReviewProxyCaseResult",
    "ReviewProxyProfile",
    "build_review_proxy_command",
    "compute_generation_config_fingerprint",
    "default_review_proxy_dir",
    "load_review_dataset",
    "make_review_profile",
    "make_review_proxies",
    "probe_encoder_capability",
    "probe_media",
    "resolve_review_case",
]
