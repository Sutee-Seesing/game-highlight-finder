"""Low-cost, deterministic local silence/loudness signal generation."""

from __future__ import annotations

import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from game_highlight_finder import __version__
from game_highlight_finder.config import AppConfig
from game_highlight_finder.domain.models import (
    ArtifactIdentity,
    AudioActivityInterval,
    ErrorRecord,
    LocalSignalsArtifact,
    SourceAsset,
    StageStatus,
    TimeInterval,
    model_json,
)
from game_highlight_finder.errors import AppError, ErrorCategory, ValidationError
from game_highlight_finder.logging import RunLogger
from game_highlight_finder.media.ffmpeg import build_signal_command, run_ffmpeg
from game_highlight_finder.media.tools import tool_identity
from game_highlight_finder.pipeline.manifest import (
    complete_stage,
    ensure_m2_stages,
    fail_stage,
    recover_interrupted,
    start_stage,
)
from game_highlight_finder.pipeline.proxy import ProxyResult
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.lock import SessionLock
from game_highlight_finder.storage.sessions import (
    artifact_identity,
    completed_stage_cache_is_valid,
    compute_local_signals_cache_key,
    load_manifest,
    session_paths,
    write_manifest,
)


class LocalSignalsResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    session_id: str
    cache_hit: bool
    cache_reason: str
    signals_path: Path
    signals: LocalSignalsArtifact
    session_dir: Path


_SILENCE_START_RE = re.compile(r"silence_start:\s*([-+]?\d+(?:\.\d+)?)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*([-+]?\d+(?:\.\d+)?)")
_LOUDNESS_RE = re.compile(
    r"t:\s*([-+]?\d+(?:\.\d+)?).*?M:\s*([-+]?\d+(?:\.\d+)?)\s*LUFS",
    re.IGNORECASE,
)
_ASTATS_RE = re.compile(
    r"pts_time:\s*([-+]?\d+(?:\.\d+)?).*?"
    r"RMS_level=([-+]?\d+(?:\.\d+)?)",
    re.IGNORECASE | re.DOTALL,
)
_INTEGRATED_RE = re.compile(r"^\s*I:\s*([-+]?\d+(?:\.\d+)?)\s*LUFS", re.MULTILINE)


def generate_local_signals(
    source: SourceAsset,
    proxy: ProxyResult,
    config: AppConfig,
) -> LocalSignalsResult:
    paths = session_paths(config.storage.data_dir, proxy.session_id)
    paths.signals_dir.mkdir(parents=True, exist_ok=True)
    paths.tmp_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = tool_identity("ffmpeg", config.tools.ffmpeg_path)
    ffprobe = tool_identity("ffprobe", config.tools.ffprobe_path, include_capabilities=False)
    proxy_artifact_sha = _proxy_artifact_hash(proxy)
    cache_key = compute_local_signals_cache_key(
        source,
        config,
        proxy_artifact_sha256=proxy_artifact_sha,
        ffmpeg_identity=ffmpeg,
        ffprobe_identity=ffprobe,
    )
    signals_path = paths.signals_dir / "activity.json"
    log = RunLogger(_log_path(paths.logs))
    with SessionLock(paths.lock):
        manifest = load_manifest(paths.manifest)
        changed = ensure_m2_stages(manifest)
        if recover_interrupted(manifest):
            changed = True
        if changed:
            write_manifest(paths.manifest, manifest)
        valid, reason = completed_stage_cache_is_valid(
            paths, manifest, stage_name="local_signals", expected_cache_key=cache_key
        )
        if valid:
            signals = load_signals(signals_path)
            return LocalSignalsResult(
                session_id=manifest.session_id,
                cache_hit=True,
                cache_reason=reason,
                signals_path=signals_path,
                signals=signals,
                session_dir=paths.root,
            )
        stage = manifest.stages["local_signals"]
        if stage.status is StageStatus.COMPLETED:
            stage.status = StageStatus.STALE
            stage.reason = reason
        run_id = start_stage(manifest, "local_signals", cache_key)
        write_manifest(paths.manifest, manifest)
        temp_dir = paths.tmp_dir / run_id
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_signals = temp_dir / "activity.partial.json"
        log.write(
            "INFO", "local_signals_started", "Local signal generation started.", run_id=run_id
        )
        try:
            warnings: list[str] = []
            stderr = ""
            if proxy.audio_path is not None and proxy.audio_path.is_file():
                result = run_ffmpeg(
                    build_signal_command(ffmpeg.path, proxy.audio_path, config),
                    duration_ms=proxy.metadata.duration_ms,
                    timeout_seconds=config.tools.ffmpeg_timeout_seconds,
                    termination_grace_seconds=config.tools.termination_grace_seconds,
                    max_stderr_lines=200_000,
                    max_stderr_chars=20_000_000,
                )
                stderr = result.stderr
                silence_proxy = parse_silence_intervals(
                    stderr, duration_ms=proxy.metadata.duration_ms
                )
                activity_proxy, overall_loudness = parse_loudness_activity(
                    stderr,
                    duration_ms=proxy.metadata.duration_ms,
                    interval_ms=config.signals.loudness.interval_ms,
                    active_threshold_db=config.signals.silence.noise_db,
                )
                silence = _map_intervals(
                    silence_proxy, proxy.metadata.timestamp_mapping, source.duration_ms
                )
                activity = _map_activity(
                    activity_proxy, proxy.metadata.timestamp_mapping, source.duration_ms
                )
                if not activity:
                    # ebur128 intentionally emits a bounded summary for short clips and
                    # can be configured to avoid unbounded per-frame logs for long clips.
                    activity = [
                        AudioActivityInterval(
                            start_ms=0,
                            end_ms=source.duration_ms,
                            mean_db=overall_loudness,
                            active=(
                                overall_loudness is not None
                                and overall_loudness > config.signals.silence.noise_db
                            ),
                        )
                    ]
            else:
                warnings.append("No analysis audio is available; audio signals are empty.")
                silence = []
                activity = []
                overall_loudness = None
            signals = LocalSignalsArtifact(
                created_at=datetime.now(UTC),
                producer_version=__version__,
                source_duration_ms=source.duration_ms,
                audio_present=proxy.metadata.audio_present,
                silence_intervals=silence,
                audio_activity=activity,
                warnings=warnings,
                tool_identities={"ffmpeg": ffmpeg.version, "ffprobe": ffprobe.version},
                overall_loudness_lufs=overall_loudness,
            )
            atomic_write_json(temp_signals, model_json(signals))
            temp_signals.replace(signals_path)
            inputs = [
                ArtifactIdentity(
                    path=str(proxy.proxy_path),
                    sha256=_hash_from_proxy(proxy, "proxy/analysis_proxy.mp4"),
                    size_bytes=proxy.proxy_path.stat().st_size,
                )
            ]
            if proxy.audio_path is not None:
                inputs.append(
                    ArtifactIdentity(
                        path=str(proxy.audio_path),
                        sha256=_hash_from_proxy(proxy, "audio/analysis_audio.m4a"),
                        size_bytes=proxy.audio_path.stat().st_size,
                    )
                )
            outputs = [artifact_identity(signals_path, relative_to=paths.root)]
            complete_stage(
                manifest,
                "local_signals",
                inputs=inputs,
                outputs=outputs,
                item_states={"audio": "COMPLETED" if proxy.metadata.audio_present else "SKIPPED"},
            )
            write_manifest(paths.manifest, manifest)
            log.write(
                "INFO",
                "local_signals_completed",
                "Local signal generation completed.",
                run_id=run_id,
            )
            return LocalSignalsResult(
                session_id=manifest.session_id,
                cache_hit=False,
                cache_reason="generated",
                signals_path=signals_path,
                signals=signals,
                session_dir=paths.root,
            )
        except BaseException as exc:
            error = _error_record(exc)
            try:
                fail_stage(manifest, "local_signals", error)
                write_manifest(paths.manifest, manifest)
                log.write("ERROR", "local_signals_failed", error.message, category=error.category)
            except Exception:
                pass
            raise
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


def parse_silence_intervals(text: str, *, duration_ms: int) -> list[TimeInterval]:
    """Parse FFmpeg silencedetect output into clamped integer-millisecond intervals."""

    starts = [float(match.group(1)) for match in _SILENCE_START_RE.finditer(text)]
    ends = [float(match.group(1)) for match in _SILENCE_END_RE.finditer(text)]
    intervals: list[TimeInterval] = []
    end_index = 0
    for start in starts:
        while end_index < len(ends) and ends[end_index] < start:
            end_index += 1
        end = ends[end_index] if end_index < len(ends) else duration_ms / 1000
        if end_index < len(ends):
            end_index += 1
        start_ms = max(0, min(duration_ms, round(start * 1000)))
        end_ms = max(0, min(duration_ms, round(end * 1000)))
        if end_ms > start_ms:
            intervals.append(TimeInterval(start_ms=start_ms, end_ms=end_ms))
    return _merge_intervals(intervals)


def parse_loudness_activity(
    text: str,
    *,
    duration_ms: int,
    interval_ms: int,
    active_threshold_db: float,
) -> tuple[list[AudioActivityInterval], float | None]:
    samples: list[tuple[int, float]] = []
    for match in _LOUDNESS_RE.finditer(text):
        try:
            seconds = float(match.group(1))
            db = float(match.group(2))
        except ValueError:
            continue
        if seconds >= 0 and seconds * 1000 <= duration_ms and -200 <= db <= 20:
            samples.append((round(seconds * 1000), db))
    for match in _ASTATS_RE.finditer(text):
        try:
            seconds = float(match.group(1))
            db = float(match.group(2))
        except ValueError:
            continue
        if seconds >= 0 and seconds * 1000 <= duration_ms and -200 <= db <= 20:
            samples.append((round(seconds * 1000), db))
    buckets: dict[int, list[float]] = {}
    for timestamp, db in samples:
        bucket = min(duration_ms - 1, timestamp) // interval_ms * interval_ms
        buckets.setdefault(bucket, []).append(db)
    activity = [
        AudioActivityInterval(
            start_ms=start,
            end_ms=min(duration_ms, start + interval_ms),
            mean_db=sum(values) / len(values),
            peak_db=max(values),
            active=max(values) > active_threshold_db,
        )
        for start, values in sorted(buckets.items())
        if start < duration_ms
    ]
    integrated = None
    integrated_match = _INTEGRATED_RE.search(text)
    if integrated_match:
        try:
            value = float(integrated_match.group(1))
            if -200 <= value <= 20:
                integrated = value
        except ValueError:
            pass
    return activity[:20_000], integrated


def load_signals(path: Path) -> LocalSignalsArtifact:
    try:
        return LocalSignalsArtifact.model_validate(read_json(path))
    except Exception as exc:
        raise ValidationError("Stored local-signal artifact is invalid.", hint=str(exc)) from exc


def _merge_intervals(intervals: list[TimeInterval]) -> list[TimeInterval]:
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda interval: (interval.start_ms, interval.end_ms))
    merged: list[TimeInterval] = [ordered[0]]
    for interval in ordered[1:]:
        previous = merged[-1]
        if interval.start_ms <= previous.end_ms:
            merged[-1] = TimeInterval(
                start_ms=previous.start_ms,
                end_ms=max(previous.end_ms, interval.end_ms),
            )
        else:
            merged.append(interval)
    return merged[:20_000]


def _map_intervals(
    intervals: list[TimeInterval], mapping: Any, source_duration_ms: int
) -> list[TimeInterval]:
    mapped: list[TimeInterval] = []
    for interval in intervals:
        start = max(
            0,
            min(
                source_duration_ms,
                mapping.proxy_to_source_ms(interval.start_ms) - mapping.source_start_ms,
            ),
        )
        end = max(
            0,
            min(
                source_duration_ms,
                mapping.proxy_to_source_ms(interval.end_ms) - mapping.source_start_ms,
            ),
        )
        if end > start:
            mapped.append(TimeInterval(start_ms=start, end_ms=end))
    return _merge_intervals(mapped)


def _map_activity(
    intervals: list[AudioActivityInterval], mapping: Any, source_duration_ms: int
) -> list[AudioActivityInterval]:
    mapped: list[AudioActivityInterval] = []
    for interval in intervals:
        start = max(
            0,
            min(
                source_duration_ms,
                mapping.proxy_to_source_ms(interval.start_ms) - mapping.source_start_ms,
            ),
        )
        end = max(
            0,
            min(
                source_duration_ms,
                mapping.proxy_to_source_ms(interval.end_ms) - mapping.source_start_ms,
            ),
        )
        if end > start:
            mapped.append(
                AudioActivityInterval(
                    start_ms=start,
                    end_ms=end,
                    mean_db=interval.mean_db,
                    peak_db=interval.peak_db,
                    active=interval.active,
                )
            )
    return mapped[:20_000]


def _proxy_artifact_hash(proxy: ProxyResult) -> str:
    for path, digest in _proxy_artifact_pairs(proxy):
        if path.replace("\\", "/") == "proxy/analysis_proxy.mp4":
            return digest
    raise ValidationError("Proxy manifest is missing the proxy artifact hash.")


def _hash_from_proxy(proxy: ProxyResult, relative_path: str) -> str:
    for path, digest in _proxy_artifact_pairs(proxy):
        if path.replace("\\", "/") == relative_path:
            return digest
    # A caller may receive a direct ProxyResult from a test; calculate safely in that case.
    candidate = proxy.session_dir / relative_path
    from game_highlight_finder.storage.hashing import hash_file

    return hash_file(candidate)


def _proxy_artifact_pairs(proxy: ProxyResult) -> list[tuple[str, str]]:
    paths = session_paths(proxy.session_dir.parent.parent, proxy.session_id)
    manifest = load_manifest(paths.manifest)
    return [
        (artifact.path, artifact.sha256) for artifact in manifest.stages["proxy"].output_artifacts
    ]


def _log_path(logs: Path) -> Path:
    return logs / f"run-local-signals-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.jsonl"


def _error_record(exc: BaseException) -> ErrorRecord:
    if isinstance(exc, AppError):
        return ErrorRecord(
            category=exc.category.value,
            message=exc.message,
            hint=exc.hint,
            retryable=exc.category in {ErrorCategory.STORAGE, ErrorCategory.INTERNAL},
        )
    return ErrorRecord(
        category=ErrorCategory.INTERNAL.value,
        message="Unexpected error during local signal generation.",
        hint=type(exc).__name__,
        retryable=False,
    )
