"""Timestamp-safe local proxy and analysis-audio generation."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from game_highlight_finder import __version__
from game_highlight_finder.config import AppConfig
from game_highlight_finder.domain.models import (
    ArtifactIdentity,
    ErrorRecord,
    ProxyMetadata,
    SourceAsset,
    StageStatus,
    TimestampMapping,
    model_json,
)
from game_highlight_finder.errors import AppError, ErrorCategory, SourceError, ValidationError
from game_highlight_finder.logging import RunLogger
from game_highlight_finder.media.ffmpeg import (
    analysis_audio_stream_indexes,
    build_audio_command,
    build_concat_mixed_audio_command,
    build_mixed_audio_intermediate_command,
    build_proxy_command,
    run_ffmpeg,
)
from game_highlight_finder.media.ffprobe import run_ffprobe
from game_highlight_finder.media.tools import tool_identity
from game_highlight_finder.pipeline.manifest import (
    complete_stage,
    ensure_m2_stages,
    fail_stage,
    recover_interrupted,
    start_stage,
)
from game_highlight_finder.storage.atomic import atomic_write_bytes, atomic_write_json, read_json
from game_highlight_finder.storage.lock import SessionLock
from game_highlight_finder.storage.preflight import check_disk_space
from game_highlight_finder.storage.sessions import (
    artifact_identity,
    completed_stage_cache_is_valid,
    compute_proxy_cache_key,
    load_manifest,
    session_paths,
    write_manifest,
)

# Bounded mixing avoids source-dependent silent early EOF in a single multi-hour amix graph.
MIX_CHUNK_DURATION_MS = 900_000
MIX_CHUNK_TRIGGER_MS = 1_800_000


class ProxyResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    session_id: str
    cache_hit: bool
    cache_reason: str
    proxy_path: Path
    audio_path: Path | None
    metadata_path: Path
    metadata: ProxyMetadata
    session_dir: Path


def generate_proxy(source: SourceAsset, config: AppConfig) -> ProxyResult:
    """Generate and validate proxy/audio derivatives, or return a verified cache hit."""

    paths = session_paths(config.storage.data_dir, _session_id_from_source(source))
    if not paths.manifest.is_file():
        raise ValidationError("Cannot generate a proxy before ingest has committed a session.")
    safe_dirs = (paths.proxy_dir, paths.audio_dir, paths.signals_dir, paths.tmp_dir)
    for directory in safe_dirs:
        directory.mkdir(parents=True, exist_ok=True)
    ffmpeg = tool_identity("ffmpeg", config.tools.ffmpeg_path)
    ffprobe = tool_identity("ffprobe", config.tools.ffprobe_path, include_capabilities=False)
    cache_key = compute_proxy_cache_key(
        source, config, ffmpeg_identity=ffmpeg, ffprobe_identity=ffprobe
    )
    proxy_path = paths.proxy_dir / "analysis_proxy.mp4"
    metadata_path = paths.proxy_dir / "metadata.json"
    audio_path = paths.audio_dir / "analysis_audio.m4a"

    log = RunLogger(_log_path(paths.logs, "proxy"))
    with SessionLock(paths.lock):
        manifest = load_manifest(paths.manifest)
        changed = ensure_m2_stages(manifest)
        if recover_interrupted(manifest):
            changed = True
        if changed:
            write_manifest(paths.manifest, manifest)
        valid, reason = completed_stage_cache_is_valid(
            paths, manifest, stage_name="proxy", expected_cache_key=cache_key
        )
        if valid:
            # A matching cache key and file hash cannot prove that the encoded
            # video reached the end: older B proxies had full-length audio but
            # a video stream that stopped midway through the recording.
            try:
                cached_probe = run_ffprobe(
                    ffprobe.path, proxy_path,
                    timeout_seconds=config.tools.probe_timeout_seconds,
                )
                cached_audio_indexes = analysis_audio_stream_indexes(source, config)
                validate_proxy_probe(
                    cached_probe, source, config,
                    expected_audio=bool(cached_audio_indexes),
                )
                if len(cached_audio_indexes) > 1:
                    _validate_proxy_audio_duration(cached_probe, source.duration_ms)
            except (SourceError, ValidationError) as exc:
                valid = False
                reason = f"Cached proxy media failed validation: {exc}"
        if valid:
            metadata = _load_metadata(metadata_path)
            return ProxyResult(
                session_id=manifest.session_id,
                cache_hit=True,
                cache_reason=reason,
                proxy_path=proxy_path,
                audio_path=audio_path if metadata.audio_present else None,
                metadata_path=metadata_path,
                metadata=metadata,
                session_dir=paths.root,
            )
        stage = manifest.stages["proxy"]
        if stage.status is StageStatus.COMPLETED:
            stage.status = StageStatus.STALE
            stage.reason = reason
        downstream = manifest.stages["local_signals"]
        if downstream.status is StageStatus.COMPLETED:
            downstream.status = StageStatus.STALE
            downstream.reason = "proxy input changed"
        run_id = start_stage(manifest, "proxy", cache_key)
        write_manifest(paths.manifest, manifest)
        log.write("INFO", "proxy_started", "Proxy generation started.", run_id=run_id)
        temp_dir = paths.tmp_dir / run_id
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_proxy = temp_dir / "analysis_proxy.partial.mp4"
        temp_audio = temp_dir / "analysis_audio.partial.m4a"
        temp_mixed_audio = temp_dir / "analysis_audio.mixed.nut"
        temp_metadata = temp_dir / "metadata.partial.json"
        try:
            source_audio_indexes = analysis_audio_stream_indexes(source, config)
            has_audio = bool(source_audio_indexes)
            multi_track_mix = len(source_audio_indexes) > 1
            check_disk_space(
                paths.root,
                source_size_bytes=source.size_bytes,
                duration_ms=source.duration_ms,
                config=config,
                lossless_mix_intermediate=multi_track_mix,
                segmented_mix_intermediate=(
                    multi_track_mix and source.duration_ms > MIX_CHUNK_TRIGGER_MS
                ),
            )
            if multi_track_mix and source.duration_ms > MIX_CHUNK_TRIGGER_MS:
                _generate_segmented_mixed_audio(
                    ffmpeg.path, ffprobe.path, source, config, source_audio_indexes,
                    temp_dir, temp_mixed_audio,
                )
            if multi_track_mix and source.duration_ms <= MIX_CHUNK_TRIGGER_MS:
                run_ffmpeg(
                    build_mixed_audio_intermediate_command(
                        ffmpeg.path, source.path, temp_mixed_audio, config,
                        audio_stream_indexes=source_audio_indexes,
                    ),
                    duration_ms=source.duration_ms,
                    timeout_seconds=config.tools.ffmpeg_timeout_seconds,
                    termination_grace_seconds=config.tools.termination_grace_seconds,
                )
                mixed_probe = run_ffprobe(
                    ffprobe.path, temp_mixed_audio,
                    timeout_seconds=config.tools.probe_timeout_seconds,
                )
                _validate_mixed_audio_probe(mixed_probe, source.duration_ms, config)
            run_ffmpeg(
                build_proxy_command(
                    ffmpeg.path,
                    source.path,
                    temp_proxy,
                    config,
                    has_audio=has_audio,
                    audio_stream_indexes=(None if multi_track_mix else source_audio_indexes),
                    mixed_audio_path=(temp_mixed_audio if multi_track_mix else None),
                ),
                duration_ms=source.duration_ms,
                timeout_seconds=config.tools.ffmpeg_timeout_seconds,
                termination_grace_seconds=config.tools.termination_grace_seconds,
            )
            proxy_probe = run_ffprobe(
                ffprobe.path,
                temp_proxy,
                timeout_seconds=config.tools.probe_timeout_seconds,
            )
            parsed = validate_proxy_probe(proxy_probe, source, config, expected_audio=has_audio)
            if multi_track_mix:
                _validate_proxy_audio_duration(proxy_probe, source.duration_ms)
            proxy_duration_ms = parsed["duration_ms"]
            if has_audio:
                run_ffmpeg(
                    build_audio_command(
                        ffmpeg.path,
                        temp_mixed_audio if multi_track_mix else source.path,
                        temp_audio,
                        config,
                        audio_stream_indexes=(None if multi_track_mix else source_audio_indexes),
                        mixed_audio_input=multi_track_mix,
                    ),
                    duration_ms=source.duration_ms,
                    timeout_seconds=config.tools.ffmpeg_timeout_seconds,
                    termination_grace_seconds=config.tools.termination_grace_seconds,
                )
                audio_probe = run_ffprobe(
                    ffprobe.path,
                    temp_audio,
                    timeout_seconds=config.tools.probe_timeout_seconds,
                )
                _validate_audio_probe(audio_probe, source.duration_ms)
            else:
                temp_audio = None  # type: ignore[assignment]
            mapping = TimestampMapping(
                source_start_ms=(
                    source.timestamp_origin_ms or source.video_stream.start_time_ms or 0
                ),
                proxy_start_ms=0,
                source_duration_ms=source.duration_ms,
                proxy_duration_ms=proxy_duration_ms,
            )
            warnings = list(source.warnings)
            if not has_audio:
                warnings.append("Source has no audio; audio-specific signals will be empty.")
            elif config.media.audio.source_mix_mode == "mix_all" and len(source_audio_indexes) > 1:
                warnings.append(
                    "Mixed all source audio streams into analysis audio so multi-track OBS "
                    "voice/game sources are not silently discarded."
                )
            metadata = ProxyMetadata(
                created_at=datetime.now(UTC),
                producer_version=__version__,
                proxy_path="proxy/analysis_proxy.mp4",
                duration_ms=proxy_duration_ms,
                width=parsed["width"],
                height=parsed["height"],
                video_codec=parsed["video_codec"],
                audio_present=has_audio,
                audio_codec=parsed["audio_codec"],
                audio_sample_rate_hz=parsed["audio_sample_rate_hz"],
                audio_channels=parsed["audio_channels"],
                audio_source_mode=(config.media.audio.source_mix_mode if has_audio else "none"),
                audio_source_stream_indexes=list(source_audio_indexes),
                timestamp_mapping=mapping,
                warnings=warnings,
                tool_identities={"ffmpeg": ffmpeg.version, "ffprobe": ffprobe.version},
            )
            atomic_write_json(temp_metadata, model_json(metadata))
            _commit_file(temp_proxy, proxy_path)
            if has_audio and temp_audio is not None:
                _commit_file(temp_audio, audio_path)
            elif audio_path.exists():
                audio_path.unlink()
            _commit_file(temp_metadata, metadata_path)
            outputs = [
                artifact_identity(proxy_path, relative_to=paths.root),
                artifact_identity(metadata_path, relative_to=paths.root),
            ]
            if has_audio:
                outputs.append(artifact_identity(audio_path, relative_to=paths.root))
            inputs = [
                ArtifactIdentity(
                    path=str(source.path), sha256=source.sha256, size_bytes=source.size_bytes
                )
            ]
            complete_stage(
                manifest,
                "proxy",
                inputs=inputs,
                outputs=outputs,
                item_states={"audio": "COMPLETED" if has_audio else "SKIPPED"},
            )
            write_manifest(paths.manifest, manifest)
            log.write("INFO", "proxy_completed", "Proxy generation completed.", run_id=run_id)
            return ProxyResult(
                session_id=manifest.session_id,
                cache_hit=False,
                cache_reason="generated",
                proxy_path=proxy_path,
                audio_path=audio_path if has_audio else None,
                metadata_path=metadata_path,
                metadata=metadata,
                session_dir=paths.root,
            )
        except BaseException as exc:
            error = _error_record(exc)
            try:
                fail_stage(manifest, "proxy", error)
                write_manifest(paths.manifest, manifest)
                log.write("ERROR", "proxy_failed", error.message, category=error.category)
            except Exception:
                pass
            raise
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


def _generate_segmented_mixed_audio(
    ffmpeg_path: Path,
    ffprobe_path: Path,
    source: SourceAsset,
    config: AppConfig,
    audio_indexes: tuple[int, ...],
    temp_dir: Path,
    output_path: Path,
) -> None:
    """Bound every mix graph; stream-copy verified NUT chunks with exact time offsets."""

    if MIX_CHUNK_DURATION_MS <= 0:
        raise ValidationError("Lossless mix chunk duration must be positive.")
    concat_lines = ["ffconcat version 1.0"]
    for index, start_ms in enumerate(range(0, source.duration_ms, MIX_CHUNK_DURATION_MS)):
        chunk_duration_ms = min(MIX_CHUNK_DURATION_MS, source.duration_ms - start_ms)
        chunk_path = temp_dir / f"analysis_audio.part_{index:04d}.nut"
        run_ffmpeg(
            build_mixed_audio_intermediate_command(
                ffmpeg_path, source.path, chunk_path, config,
                audio_stream_indexes=audio_indexes,
                start_ms=start_ms,
                duration_ms=chunk_duration_ms,
            ),
            duration_ms=chunk_duration_ms,
            timeout_seconds=config.tools.ffmpeg_timeout_seconds,
            termination_grace_seconds=config.tools.termination_grace_seconds,
        )
        chunk_probe = run_ffprobe(
            ffprobe_path, chunk_path, timeout_seconds=config.tools.probe_timeout_seconds
        )
        _validate_mixed_audio_probe(chunk_probe, chunk_duration_ms, config)
        chunk_format = chunk_probe.get("format")
        chunk_actual_ms = _duration_ms(
            chunk_format.get("duration") if isinstance(chunk_format, dict) else None
        )
        if abs(chunk_actual_ms - chunk_duration_ms) > 250:
            raise ValidationError(
                "Lossless audio chunk ended before its requested interval "
                f"({start_ms}+{chunk_actual_ms} vs {chunk_duration_ms} ms)."
            )
        # The NUT format duration may be a few milliseconds shorter than its last
        # packet's end. Explicit requested durations prevent timestamp overlap.
        concat_lines.extend(
            [f"file '{chunk_path.name}'", f"duration {chunk_duration_ms / 1000:.3f}"]
        )
    concat_path = temp_dir / "analysis_audio.concat.ffconcat"
    atomic_write_bytes(concat_path, ("\n".join(concat_lines) + "\n").encode("utf-8"))
    run_ffmpeg(
        build_concat_mixed_audio_command(ffmpeg_path, concat_path, output_path),
        duration_ms=source.duration_ms,
        timeout_seconds=config.tools.ffmpeg_timeout_seconds,
        termination_grace_seconds=config.tools.termination_grace_seconds,
    )
    joined_probe = run_ffprobe(
        ffprobe_path, output_path, timeout_seconds=config.tools.probe_timeout_seconds
    )
    _validate_mixed_audio_probe(joined_probe, source.duration_ms, config)


def validate_proxy_probe(
    raw: dict[str, Any],
    source: SourceAsset,
    config: AppConfig,
    *,
    expected_audio: bool,
) -> dict[str, Any]:
    streams = raw.get("streams")
    format_data = raw.get("format")
    if not isinstance(streams, list) or not isinstance(format_data, dict):
        raise ValidationError("Proxy ffprobe output must contain streams[] and format{}.")
    videos = [
        item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"
    ]
    audios = [
        item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"
    ]
    if len(videos) != 1:
        raise ValidationError("Validated proxy must contain exactly one video stream.")
    video = videos[0]
    width = _positive_int(video.get("width"), "proxy width")
    height = _positive_int(video.get("height"), "proxy height")
    if width > config.media.proxy.max_width or height > config.media.proxy.max_height:
        raise ValidationError("Proxy dimensions exceed configured limits.")
    source_ratio = source.video_stream.width / source.video_stream.height
    proxy_ratio = width / height
    if abs(proxy_ratio - source_ratio) / source_ratio > 0.02:
        raise ValidationError("Proxy aspect ratio differs materially from the source.")
    # ffprobe reports the encoded bitstream codec (h264), not the FFmpeg encoder
    # implementation name (libx264 or h264_nvenc).
    expected_codecs: set[str] = {"h264"}
    if video.get("codec_name") not in expected_codecs:
        raise ValidationError(
            f"Proxy video codec is {video.get('codec_name')!r}; expected "
            f"{config.media.proxy.video_codec}."
        )
    duration_ms = _duration_ms(format_data.get("duration"))
    tolerance = max(500, int(source.duration_ms * 0.02))
    if abs(duration_ms - source.duration_ms) > tolerance:
        raise ValidationError(
            "Proxy duration differs from source beyond tolerance "
            f"({duration_ms} vs {source.duration_ms} ms)."
        )
    # Container duration may be set by full-length audio even when the video
    # stream silently ended much earlier; the video stream itself must reach the tail.
    video_duration_ms = _duration_ms(video.get("duration"))
    if abs(video_duration_ms - source.duration_ms) > 1000:
        raise ValidationError(
            "Proxy video stream duration differs from source beyond tolerance "
            f"({video_duration_ms} vs {source.duration_ms} ms)."
        )
    if expected_audio and len(audios) != 1:
        raise ValidationError("Source audio was present but proxy audio is missing.")
    if not expected_audio and audios:
        raise ValidationError("Source has no audio but proxy contains an audio stream.")
    audio = audios[0] if audios else {}
    return {
        "width": width,
        "height": height,
        "duration_ms": duration_ms,
        "video_codec": str(video.get("codec_name")),
        "audio_codec": audio.get("codec_name"),
        "audio_sample_rate_hz": _optional_int(audio.get("sample_rate")),
        "audio_channels": _optional_int(audio.get("channels")),
    }


def _validate_mixed_audio_probe(
    raw: dict[str, Any], source_duration_ms: int, config: AppConfig
) -> None:
    """Reject silent early EOF before the limiter consumes the float intermediate."""

    streams = raw.get("streams")
    audio_streams = [
        stream for stream in streams if isinstance(stream, dict)
        and stream.get("codec_type") == "audio"
    ] if isinstance(streams, list) else []
    if len(audio_streams) != 1:
        raise ValidationError("Lossless mixed audio must contain exactly one audio stream.")
    audio = audio_streams[0]
    if (
        audio.get("codec_name") != "pcm_f32le"
        or _optional_int(audio.get("sample_rate")) != config.media.audio.sample_rate_hz
        or _optional_int(audio.get("channels")) != config.media.audio.channels
    ):
        raise ValidationError(
            "Lossless mixed audio format does not match the expected PCM float contract."
        )
    format_data = raw.get("format")
    duration_ms = _duration_ms(
        format_data.get("duration") if isinstance(format_data, dict) else None
    )
    if abs(duration_ms - source_duration_ms) > 1000:
        raise ValidationError(
            "Lossless mixed audio duration differs from source beyond tolerance "
            f"({duration_ms} vs {source_duration_ms} ms)."
        )


def _validate_proxy_audio_duration(raw: dict[str, Any], source_duration_ms: int) -> None:
    """A full video container does not prove that its mixed audio reached the tail."""

    streams = raw.get("streams")
    audios = [
        stream for stream in streams if isinstance(stream, dict)
        and stream.get("codec_type") == "audio"
    ] if isinstance(streams, list) else []
    if len(audios) != 1:
        raise ValidationError("Mixed proxy must contain exactly one audio stream.")
    duration_ms = _duration_ms(audios[0].get("duration"))
    if abs(duration_ms - source_duration_ms) > 1000:
        raise ValidationError(
            "Mixed proxy audio duration differs from source beyond tolerance "
            f"({duration_ms} vs {source_duration_ms} ms)."
        )


def _validate_audio_probe(raw: dict[str, Any], source_duration_ms: int) -> None:
    streams = raw.get("streams")
    if not isinstance(streams, list) or not any(
        isinstance(item, dict) and item.get("codec_type") == "audio" for item in streams
    ):
        raise ValidationError("Analysis audio output has no readable audio stream.")
    format_data = raw.get("format")
    if isinstance(format_data, dict):
        duration = _duration_ms(format_data.get("duration"))
        if abs(duration - source_duration_ms) > max(750, int(source_duration_ms * 0.03)):
            raise ValidationError(
                "Analysis audio duration differs from source beyond tolerance "
                f"({duration} vs {source_duration_ms} ms)."
            )


def _duration_ms(value: object) -> int:
    try:
        result = round(float(str(value)) * 1000)
    except (TypeError, ValueError):
        raise ValidationError("Media duration is missing or invalid.") from None
    if result <= 0:
        raise ValidationError("Media duration must be positive.")
    return result


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
        return int(str(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _commit_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.replace(destination)


def _load_metadata(path: Path) -> ProxyMetadata:
    try:
        return ProxyMetadata.model_validate(read_json(path))
    except Exception as exc:
        raise ValidationError("Stored proxy metadata is invalid.", hint=str(exc)) from exc


def _session_id_from_source(source: SourceAsset) -> str:
    # The stable session ID is the same function used by ingest; avoid importing a pipeline helper.
    from game_highlight_finder.storage.sessions import make_session_id

    return make_session_id(source)


def _log_path(logs: Path, stage: str) -> Path:
    return logs / f"run-{stage}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.jsonl"


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
        message="Unexpected error during proxy generation.",
        hint=type(exc).__name__,
        retryable=False,
    )
