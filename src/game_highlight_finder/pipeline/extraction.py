"""Accurate source extraction, thumbnails, and restart-safe M6 manifests."""

from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from game_highlight_finder import __version__
from game_highlight_finder.config import AppConfig
from game_highlight_finder.domain.models import SessionMap, SourceAsset
from game_highlight_finder.domain.reconcile import derive_clip_boundaries
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.media.ffmpeg import (
    build_extraction_command,
    build_thumbnail_command,
    run_ffmpeg,
)
from game_highlight_finder.media.ffprobe import run_ffprobe
from game_highlight_finder.media.tools import tool_identity
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.hashing import hash_file
from game_highlight_finder.storage.sessions import make_session_id, session_paths


class ExtractionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    candidate_id: str = Field(pattern=r"^cand_[0-9a-f]{16}$")
    source_id: str = Field(pattern=r"^src_[0-9a-f]{16}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_start_ms: int = Field(ge=0)
    requested_end_ms: int = Field(gt=0)
    mode: str = Field(pattern=r"^(accurate|copy)$")
    accuracy_class: str = Field(pattern=r"^(frame-accurate|keyframe-approximate)$")
    output_path: str = Field(min_length=1, max_length=500)
    output_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    output_size_bytes: int | None = Field(default=None, ge=0)
    probed_duration_ms: int | None = Field(default=None, gt=0)
    thumbnail_path: str | None = Field(default=None, max_length=500)
    thumbnail_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    ffmpeg_identity: str = Field(min_length=1, max_length=1_000)
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: str = Field(pattern=r"^(COMPLETED|FAILED|INCOMPLETE)$")
    warning: str | None = Field(default=None, max_length=500)
    error: str | None = Field(default=None, max_length=500)


class ExtractionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: int = 1
    created_at: datetime
    updated_at: datetime
    producer_version: str
    session_id: str
    source_id: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    records: tuple[ExtractionRecord, ...] = ()
    status: str = Field(pattern=r"^(COMPLETED|INCOMPLETE|FAILED)$")
    warnings: tuple[str, ...] = ()


class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    manifest: ExtractionManifest
    manifest_path: Path
    completed: int = Field(ge=0)
    cache_hits: int = Field(ge=0)
    incomplete: int = Field(ge=0)
    session_dir: Path


def extraction_config_fingerprint(config: AppConfig) -> str:
    payload = {
        "version": 1,
        "extraction": config.media.extraction.model_dump(mode="json"),
        "ffprobe": str(config.tools.ffprobe_path or "PATH"),
        "ffmpeg": str(config.tools.ffmpeg_path or "PATH"),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _probe_duration_ms(raw: dict[str, object]) -> int | None:
    format_data = raw.get("format")
    if isinstance(format_data, dict):
        value = format_data.get("duration")
        try:
            if value is not None:
                return max(1, round(float(str(value)) * 1000))
        except (TypeError, ValueError):
            pass
    streams = raw.get("streams")
    if not isinstance(streams, list):
        streams = []
    for stream in streams:
        if isinstance(stream, dict):
            try:
                value = stream.get("duration")
                if value is not None:
                    return max(1, round(float(str(value)) * 1000))
            except (TypeError, ValueError):
                continue
    return None


def _source_identity_is_current(source: SourceAsset) -> None:
    try:
        stat = source.path.stat()
    except OSError as exc:
        raise ValidationError("Original source is no longer available.", hint=str(exc)) from exc
    if stat.st_size != source.size_bytes or stat.st_mtime_ns != source.mtime_ns:
        raise ValidationError("Original source changed since ingest; extraction refused.")
    # Hash the source at the extraction boundary as a second, fail-closed check.
    if hash_file(source.path, source=True) != source.sha256:
        raise ValidationError("Original source hash changed since ingest; extraction refused.")


def _load_manifest(path: Path) -> ExtractionManifest | None:
    if not path.is_file():
        return None
    try:
        return ExtractionManifest.model_validate(read_json(path))
    except Exception:
        return None


def extract_candidates(
    source: SourceAsset,
    session_map: SessionMap,
    config: AppConfig,
) -> ExtractionResult:
    """Extract all canonical candidates from the immutable original source."""

    _source_identity_is_current(source)
    session_id = make_session_id(source)
    paths = session_paths(config.storage.data_dir, session_id)
    paths.candidates_dir.mkdir(parents=True, exist_ok=True)
    paths.thumbnails_dir.mkdir(parents=True, exist_ok=True)
    paths.tmp_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = tool_identity("ffmpeg", config.tools.ffmpeg_path)
    ffprobe = tool_identity("ffprobe", config.tools.ffprobe_path, include_capabilities=False)
    fingerprint = extraction_config_fingerprint(config)
    bounded_map = derive_clip_boundaries(session_map, source.duration_ms, config.media.extraction)
    now = datetime.now(UTC)
    old = _load_manifest(paths.extraction_manifest)
    existing = {record.candidate_id: record for record in (old.records if old else ())}
    records: list[ExtractionRecord] = []
    cache_hits = 0
    incomplete = 0
    for candidate in bounded_map.candidates:
        if candidate.clip_start_ms is None or candidate.clip_end_ms is None:
            incomplete += 1
            continue
        output_rel = f"candidates/{candidate.candidate_id}.mp4"
        thumb_rel = f"thumbnails/{candidate.candidate_id}.jpg"
        output = paths.root / output_rel
        thumbnail = paths.root / thumb_rel
        previous = existing.get(candidate.candidate_id)
        if (
            previous is not None
            and previous.status == "COMPLETED"
            and previous.source_sha256 == source.sha256
            and previous.requested_start_ms == candidate.clip_start_ms
            and previous.requested_end_ms == candidate.clip_end_ms
            and previous.config_fingerprint == fingerprint
            and output.is_file()
            and previous.output_sha256 == hash_file(output)
            and (
                not config.media.extraction.thumbnail
                or (thumbnail.is_file() and previous.thumbnail_sha256 == hash_file(thumbnail))
            )
        ):
            records.append(previous)
            cache_hits += 1
            continue
        temp_dir = paths.tmp_dir / f"extract-{candidate.candidate_id}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_output = temp_dir / "candidate.partial.mp4"
        temp_thumbnail = temp_dir / "thumbnail.partial.jpg"
        try:
            run_ffmpeg(
                build_extraction_command(
                    ffmpeg.path,
                    source.path,
                    temp_output,
                    start_ms=candidate.clip_start_ms,
                    end_ms=candidate.clip_end_ms,
                    extraction=config.media.extraction,
                    has_audio=source.selected_audio_stream is not None,
                    timestamp_origin_ms=source.timestamp_origin_ms or 0,
                ),
                duration_ms=candidate.clip_end_ms - candidate.clip_start_ms,
                timeout_seconds=config.tools.ffmpeg_timeout_seconds,
                termination_grace_seconds=config.tools.termination_grace_seconds,
            )
            if not temp_output.is_file() or temp_output.stat().st_size <= 0:
                raise ValidationError("FFmpeg produced an empty candidate output")
            probe = run_ffprobe(
                ffprobe.path, temp_output, timeout_seconds=config.tools.probe_timeout_seconds
            )
            probed_duration = _probe_duration_ms(probe)
            if probed_duration is None:
                raise ValidationError("candidate output duration could not be validated")
            requested_duration = candidate.clip_end_ms - candidate.clip_start_ms
            warning = None
            if (
                config.media.extraction.mode == "accurate"
                and abs(probed_duration - requested_duration)
                > config.media.extraction.accurate_tolerance_ms
            ):
                raise ValidationError("accurate extraction duration is outside tolerance")
            if config.media.extraction.mode == "copy":
                warning = "stream-copy extraction is keyframe-approximate"
            temp_output.replace(output)
            thumb_sha: str | None = None
            if config.media.extraction.thumbnail:
                midpoint = max(0, requested_duration // 2)
                run_ffmpeg(
                    build_thumbnail_command(
                        ffmpeg.path,
                        output,
                        temp_thumbnail,
                        at_ms=midpoint,
                        width=config.media.extraction.thumbnail_width,
                        height=config.media.extraction.thumbnail_height,
                    ),
                    duration_ms=requested_duration,
                    timeout_seconds=config.tools.ffmpeg_timeout_seconds,
                    termination_grace_seconds=config.tools.termination_grace_seconds,
                )
                if not temp_thumbnail.is_file() or temp_thumbnail.stat().st_size <= 0:
                    raise ValidationError("thumbnail was not produced")
                temp_thumbnail.replace(thumbnail)
                thumb_sha = hash_file(thumbnail)
            records.append(
                ExtractionRecord(
                    candidate_id=candidate.candidate_id,
                    source_id=source.source_id,
                    source_sha256=source.sha256,
                    requested_start_ms=candidate.clip_start_ms,
                    requested_end_ms=candidate.clip_end_ms,
                    mode=config.media.extraction.mode,
                    accuracy_class="frame-accurate"
                    if config.media.extraction.mode == "accurate"
                    else "keyframe-approximate",
                    output_path=output_rel,
                    output_sha256=hash_file(output),
                    output_size_bytes=output.stat().st_size,
                    probed_duration_ms=probed_duration,
                    thumbnail_path=thumb_rel if config.media.extraction.thumbnail else None,
                    thumbnail_sha256=thumb_sha,
                    ffmpeg_identity=ffmpeg.version,
                    config_fingerprint=fingerprint,
                    status="COMPLETED",
                    warning=warning,
                )
            )
        except BaseException as exc:
            incomplete += 1
            records.append(
                ExtractionRecord(
                    candidate_id=candidate.candidate_id,
                    source_id=source.source_id,
                    source_sha256=source.sha256,
                    requested_start_ms=candidate.clip_start_ms,
                    requested_end_ms=candidate.clip_end_ms,
                    mode=config.media.extraction.mode,
                    accuracy_class="frame-accurate"
                    if config.media.extraction.mode == "accurate"
                    else "keyframe-approximate",
                    output_path=output_rel,
                    ffmpeg_identity=ffmpeg.version,
                    config_fingerprint=fingerprint,
                    status="INCOMPLETE",
                    error=type(exc).__name__,
                )
            )
        finally:
            for partial in (temp_output, temp_thumbnail):
                partial.unlink(missing_ok=True)
            with suppress(OSError):
                temp_dir.rmdir()
        partial_manifest = ExtractionManifest(
            created_at=old.created_at if old else now,
            updated_at=datetime.now(UTC),
            producer_version=__version__,
            session_id=session_id,
            source_id=source.source_id,
            source_sha256=source.sha256,
            records=tuple(records),
            status="INCOMPLETE" if incomplete else "COMPLETED",
            warnings=tuple(["one or more candidates require retry"] if incomplete else []),
        )
        atomic_write_json(paths.extraction_manifest, partial_manifest.model_dump(mode="json"))
    final = ExtractionManifest(
        created_at=old.created_at if old else now,
        updated_at=datetime.now(UTC),
        producer_version=__version__,
        session_id=session_id,
        source_id=source.source_id,
        source_sha256=source.sha256,
        records=tuple(records),
        status="INCOMPLETE" if incomplete else "COMPLETED",
        warnings=tuple(["one or more candidates require retry"] if incomplete else []),
    )
    atomic_write_json(paths.extraction_manifest, final.model_dump(mode="json"))
    return ExtractionResult(
        manifest=final,
        manifest_path=paths.extraction_manifest,
        completed=sum(1 for item in records if item.status == "COMPLETED"),
        cache_hits=cache_hits,
        incomplete=incomplete,
        session_dir=paths.root,
    )


__all__ = [
    "ExtractionManifest",
    "ExtractionRecord",
    "ExtractionResult",
    "build_extraction_command",
    "build_thumbnail_command",
    "extract_candidates",
    "extraction_config_fingerprint",
]
