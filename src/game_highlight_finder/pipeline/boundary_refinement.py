from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from game_highlight_finder.config import AppConfig
from game_highlight_finder.domain.models import Candidate, Sha256, SourceAsset, model_json
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.media.ffmpeg import (
    build_slow_motion_proxy_command,
    build_window_proxy_command,
    run_ffmpeg,
)
from game_highlight_finder.media.ffprobe import run_ffprobe
from game_highlight_finder.media.tools import (
    H264EncoderChoice,
    select_usable_h264_encoder,
    tool_identity,
)
from game_highlight_finder.pipeline.proxy import ProxyResult
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.hashing import hash_file
from game_highlight_finder.storage.sessions import make_session_id, session_paths

BOUNDARY_REFINEMENT_VERSION = "boundary-refiner-v1"
DEFAULT_PRE_CONTEXT_MS = 20_000
DEFAULT_POST_CONTEXT_MS = 10_000
SlowdownFactor = Literal[1, 2, 4]
DEFAULT_SLOWDOWN_FACTOR: SlowdownFactor = 2
SUPPORTED_SLOWDOWN_FACTORS = (1, 2, 4)


class BoundaryRefinementPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = BOUNDARY_REFINEMENT_VERSION
    candidate_id: str = Field(pattern=r"^cand_[0-9a-f]{16}$")
    source_start_ms: int = Field(ge=0)
    source_end_ms: int = Field(gt=0)
    anchor_start_ms: int = Field(ge=0)
    anchor_end_ms: int = Field(gt=0)
    slowdown_factor: SlowdownFactor = DEFAULT_SLOWDOWN_FACTOR

    @model_validator(mode="after")
    def intervals_are_ordered(self) -> BoundaryRefinementPlan:
        if self.source_end_ms <= self.source_start_ms:
            raise ValueError("refinement source interval must be non-empty")
        if self.anchor_end_ms <= self.anchor_start_ms:
            raise ValueError("refinement anchor interval must be non-empty")
        if self.anchor_start_ms < self.source_start_ms or self.anchor_end_ms > self.source_end_ms:
            raise ValueError("refinement anchor must be contained by the source interval")
        return self

    @property
    def source_duration_ms(self) -> int:
        return self.source_end_ms - self.source_start_ms

    @property
    def proxy_duration_ms(self) -> int:
        return self.source_duration_ms * self.slowdown_factor

    @property
    def anchor_proxy_start_ms(self) -> int:
        return (self.anchor_start_ms - self.source_start_ms) * self.slowdown_factor

    @property
    def anchor_proxy_end_ms(self) -> int:
        return (self.anchor_end_ms - self.source_start_ms) * self.slowdown_factor


class BoundaryRefinementResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["REFINED", "UNCERTAIN"]
    event_start_ms: int = Field(ge=0)
    event_end_ms: int = Field(gt=0)
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def interval_is_ordered(self) -> BoundaryRefinementResponse:
        if self.event_end_ms <= self.event_start_ms:
            raise ValueError("refined event interval must be non-empty")
        return self


class BoundaryRefinementMediaArtifact(BaseModel):
    """Committed local media/provenance prepared for one candidate refiner call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    version: str = BOUNDARY_REFINEMENT_VERSION
    plan: BoundaryRefinementPlan
    parent_proxy_sha256: Sha256
    context_proxy_path: str = Field(min_length=1, max_length=1000)
    context_proxy_sha256: Sha256
    slowed_proxy_path: str = Field(min_length=1, max_length=1000)
    slowed_proxy_sha256: Sha256
    encoder: str = Field(min_length=1, max_length=100)
    hardware_accelerated: bool
    audio_present: bool
    context_duration_ms: int = Field(gt=0)
    slowed_proxy_duration_ms: int = Field(gt=0)


class BoundaryRefinementMediaResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cache_hit: bool
    artifact_path: Path
    context_path: Path
    slowed_proxy_path: Path
    artifact: BoundaryRefinementMediaArtifact


def plan_boundary_refinement(
    candidate: Candidate,
    source_duration_ms: int,
    *,
    pre_context_ms: int = DEFAULT_PRE_CONTEXT_MS,
    post_context_ms: int = DEFAULT_POST_CONTEXT_MS,
    slowdown_factor: int = DEFAULT_SLOWDOWN_FACTOR,
) -> BoundaryRefinementPlan:
    if source_duration_ms <= 0:
        raise ValueError("source duration must be positive")
    if candidate.event_end_ms > source_duration_ms:
        raise ValueError("candidate exceeds source duration")
    if pre_context_ms < 0 or post_context_ms < 0:
        raise ValueError("boundary refinement context cannot be negative")
    if slowdown_factor not in SUPPORTED_SLOWDOWN_FACTORS:
        raise ValueError("slowdown factor must be one of 1, 2, or 4")
    validated_slowdown = cast(SlowdownFactor, slowdown_factor)
    return BoundaryRefinementPlan(
        candidate_id=candidate.candidate_id,
        source_start_ms=max(0, candidate.event_start_ms - pre_context_ms),
        source_end_ms=min(source_duration_ms, candidate.event_end_ms + post_context_ms),
        anchor_start_ms=candidate.event_start_ms,
        anchor_end_ms=candidate.event_end_ms,
        slowdown_factor=validated_slowdown,
    )


def build_boundary_refinement_proxy_command(
    ffmpeg_path: Path,
    input_path: Path,
    output_path: Path,
    plan: BoundaryRefinementPlan,
    *,
    has_audio: bool,
    encoder: H264EncoderChoice | None = None,
) -> list[str]:
    """Build the slowed refiner proxy with a codec usable on this machine."""

    selected = encoder or select_usable_h264_encoder(ffmpeg_path)
    return build_slow_motion_proxy_command(
        ffmpeg_path,
        input_path,
        output_path,
        slowdown_factor=plan.slowdown_factor,
        has_audio=has_audio,
        video_codec=selected.encoder,
        preset=selected.preset,
    )


def prepare_boundary_refinement_media(
    source: SourceAsset,
    proxy: ProxyResult,
    candidate: Candidate,
    config: AppConfig,
    *,
    pre_context_ms: int = DEFAULT_PRE_CONTEXT_MS,
    post_context_ms: int = DEFAULT_POST_CONTEXT_MS,
    slowdown_factor: int = DEFAULT_SLOWDOWN_FACTOR,
    force: bool = False,
    encoder: H264EncoderChoice | None = None,
) -> BoundaryRefinementMediaResult:
    """Prepare, validate, and persist candidate-local media without any provider call."""

    if proxy.session_id != make_session_id(source):
        raise ValidationError("boundary refinement source and proxy belong to different sessions")
    if not proxy.proxy_path.is_file():
        raise ValidationError("boundary refinement requires a committed analysis proxy")

    plan = plan_boundary_refinement(
        candidate,
        source.duration_ms,
        pre_context_ms=pre_context_ms,
        post_context_ms=post_context_ms,
        slowdown_factor=slowdown_factor,
    )
    paths = session_paths(config.storage.data_dir, proxy.session_id)
    item_dir = paths.scout_dir / "boundary_refinement" / candidate.candidate_id
    context_path = item_dir / "context.mp4"
    slowed_path = item_dir / "slowed.mp4"
    artifact_path = item_dir / "artifact.json"
    parent_sha = hash_file(proxy.proxy_path)

    if not force:
        cached = _load_cached_media_artifact(
            artifact_path,
            context_path,
            slowed_path,
            plan=plan,
            parent_proxy_sha256=parent_sha,
            session_root=paths.root,
        )
        if cached is not None:
            return BoundaryRefinementMediaResult(
                cache_hit=True,
                artifact_path=artifact_path,
                context_path=context_path,
                slowed_proxy_path=slowed_path,
                artifact=cached,
            )

    ffmpeg = tool_identity("ffmpeg", config.tools.ffmpeg_path)
    ffprobe = tool_identity("ffprobe", config.tools.ffprobe_path, include_capabilities=False)
    selected = encoder or select_usable_h264_encoder(ffmpeg.path)
    source_clock_origin = (
        source.timestamp_origin_ms
        if source.timestamp_origin_ms is not None
        else (source.video_stream.start_time_ms or 0)
    )
    proxy_start_ms = proxy.metadata.timestamp_mapping.source_to_proxy_ms(
        plan.source_start_ms + source_clock_origin
    )
    if proxy_start_ms < 0:
        raise ValidationError("boundary refinement mapped before the analysis proxy start")
    if proxy_start_ms >= proxy.metadata.duration_ms:
        raise ValidationError("boundary refinement mapped beyond the analysis proxy duration")

    temp_dir = paths.tmp_dir / f"boundary-refinement-{candidate.candidate_id}-{uuid4().hex[:8]}"
    temp_dir.mkdir(parents=True, exist_ok=False)
    temp_context = temp_dir / "context.partial.mp4"
    temp_slowed = temp_dir / "slowed.partial.mp4"
    try:
        run_ffmpeg(
            build_window_proxy_command(
                ffmpeg.path,
                proxy.proxy_path,
                temp_context,
                proxy_start_ms=proxy_start_ms,
                duration_ms=plan.source_duration_ms,
                has_audio=proxy.metadata.audio_present,
                video_codec=selected.encoder,
                preset=selected.preset,
            ),
            duration_ms=plan.source_duration_ms,
            timeout_seconds=config.tools.ffmpeg_timeout_seconds,
            termination_grace_seconds=config.tools.termination_grace_seconds,
        )
        context_probe = run_ffprobe(
            ffprobe.path,
            temp_context,
            timeout_seconds=config.tools.probe_timeout_seconds,
        )
        context_duration_ms = _validate_refinement_probe(
            context_probe,
            expected_duration_ms=plan.source_duration_ms,
            expected_audio=proxy.metadata.audio_present,
            label="boundary context",
        )

        run_ffmpeg(
            build_boundary_refinement_proxy_command(
                ffmpeg.path,
                temp_context,
                temp_slowed,
                plan,
                has_audio=proxy.metadata.audio_present,
                encoder=selected,
            ),
            duration_ms=plan.proxy_duration_ms,
            timeout_seconds=config.tools.ffmpeg_timeout_seconds,
            termination_grace_seconds=config.tools.termination_grace_seconds,
        )
        slowed_probe = run_ffprobe(
            ffprobe.path,
            temp_slowed,
            timeout_seconds=config.tools.probe_timeout_seconds,
        )
        slowed_duration_ms = _validate_refinement_probe(
            slowed_probe,
            expected_duration_ms=plan.proxy_duration_ms,
            expected_audio=proxy.metadata.audio_present,
            label="boundary slowed proxy",
        )

        item_dir.mkdir(parents=True, exist_ok=True)
        temp_context.replace(context_path)
        temp_slowed.replace(slowed_path)
        artifact = BoundaryRefinementMediaArtifact(
            plan=plan,
            parent_proxy_sha256=parent_sha,
            context_proxy_path=context_path.relative_to(paths.root).as_posix(),
            context_proxy_sha256=hash_file(context_path),
            slowed_proxy_path=slowed_path.relative_to(paths.root).as_posix(),
            slowed_proxy_sha256=hash_file(slowed_path),
            encoder=selected.encoder,
            hardware_accelerated=selected.hardware_accelerated,
            audio_present=proxy.metadata.audio_present,
            context_duration_ms=context_duration_ms,
            slowed_proxy_duration_ms=slowed_duration_ms,
        )
        atomic_write_json(artifact_path, model_json(artifact))
        return BoundaryRefinementMediaResult(
            cache_hit=False,
            artifact_path=artifact_path,
            context_path=context_path,
            slowed_proxy_path=slowed_path,
            artifact=artifact,
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def boundary_refinement_schema(plan: BoundaryRefinementPlan) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string", "enum": ["REFINED", "UNCERTAIN"]},
            "event_start_ms": {
                "type": "integer",
                "minimum": 0,
                "maximum": plan.proxy_duration_ms,
            },
            "event_end_ms": {
                "type": "integer",
                "minimum": 0,
                "maximum": plan.proxy_duration_ms,
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string"},
        },
        "required": ["status", "event_start_ms", "event_end_ms", "confidence", "reason"],
    }


def build_boundary_refinement_prompt(plan: BoundaryRefinementPlan, candidate: Candidate) -> str:
    return (
        f"Boundary refinement version: {plan.version}. The supplied clip has been slowed "
        f"{plan.slowdown_factor}x for more granular temporal inspection. Refine the SAME "
        "gameplay event already detected by Scout; do not switch to an adjacent event. "
        "The Scout anchor is approximately "
        f"{plan.anchor_proxy_start_ms}-{plan.anchor_proxy_end_ms} ms in this slowed clip. "
        "Set event_start_ms to the earliest useful reveal, engagement, "
        "or setup needed to understand that event, and event_end_ms through its immediate payoff "
        "or outcome. Use timestamps relative to this slowed clip. Return status UNCERTAIN with "
        "lower confidence if the exact boundary cannot be supported by visible/audio evidence. "
        f"Scout category={candidate.category}; Scout reason={candidate.reason!r}."
    )


def refined_interval_in_source(
    plan: BoundaryRefinementPlan,
    response: BoundaryRefinementResponse,
) -> tuple[int, int]:
    if response.event_end_ms > plan.proxy_duration_ms:
        raise ValueError("refined interval exceeds slowed proxy duration")
    factor = plan.slowdown_factor
    start_ms = plan.source_start_ms + response.event_start_ms // factor
    end_ms = plan.source_start_ms + (response.event_end_ms + factor - 1) // factor
    if end_ms <= start_ms:
        raise ValueError("mapped refinement interval must be non-empty")
    return start_ms, min(end_ms, plan.source_end_ms)


def apply_boundary_refinement(
    candidate: Candidate,
    plan: BoundaryRefinementPlan,
    response: BoundaryRefinementResponse,
    *,
    minimum_confidence: float = 0.5,
) -> Candidate:
    if candidate.candidate_id != plan.candidate_id:
        raise ValueError("boundary refinement plan belongs to a different candidate")
    if not 0 <= minimum_confidence <= 1:
        raise ValueError("minimum confidence must be between 0 and 1")
    if response.status != "REFINED" or response.confidence < minimum_confidence:
        return candidate
    start_ms, end_ms = refined_interval_in_source(plan, response)
    if end_ms <= candidate.event_start_ms or start_ms >= candidate.event_end_ms:
        raise ValueError("refined interval must overlap the Scout anchor event")
    actions = [*candidate.normalization_actions, BOUNDARY_REFINEMENT_VERSION]
    metadata = {**candidate.metadata, "boundary_refinement": BOUNDARY_REFINEMENT_VERSION}
    return candidate.model_copy(
        update={
            "event_start_ms": start_ms,
            "event_end_ms": end_ms,
            "setup_start_ms": min(candidate.setup_start_ms, start_ms)
            if candidate.setup_start_ms is not None
            else None,
            "payoff_end_ms": max(candidate.payoff_end_ms, end_ms)
            if candidate.payoff_end_ms is not None
            else None,
            "clip_start_ms": None,
            "clip_end_ms": None,
            "normalization_actions": actions,
            "metadata": metadata,
        }
    )


def _load_cached_media_artifact(
    artifact_path: Path,
    context_path: Path,
    slowed_path: Path,
    *,
    plan: BoundaryRefinementPlan,
    parent_proxy_sha256: str,
    session_root: Path,
) -> BoundaryRefinementMediaArtifact | None:
    if not artifact_path.is_file() or not context_path.is_file() or not slowed_path.is_file():
        return None
    try:
        artifact = BoundaryRefinementMediaArtifact.model_validate(read_json(artifact_path))
    except Exception:
        return None
    if artifact.plan != plan or artifact.parent_proxy_sha256 != parent_proxy_sha256:
        return None
    if artifact.context_proxy_path != context_path.relative_to(session_root).as_posix():
        return None
    if artifact.slowed_proxy_path != slowed_path.relative_to(session_root).as_posix():
        return None
    if artifact.context_proxy_sha256 != hash_file(context_path):
        return None
    if artifact.slowed_proxy_sha256 != hash_file(slowed_path):
        return None
    return artifact


def _validate_refinement_probe(
    raw: dict[str, Any],
    *,
    expected_duration_ms: int,
    expected_audio: bool,
    label: str,
) -> int:
    streams = raw.get("streams")
    format_data = raw.get("format")
    if not isinstance(streams, list) or not isinstance(format_data, dict):
        raise ValidationError(f"{label} ffprobe output is incomplete")
    videos = [
        item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"
    ]
    audios = [
        item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"
    ]
    if len(videos) != 1:
        raise ValidationError(f"{label} must contain exactly one video stream")
    if expected_audio and len(audios) != 1:
        raise ValidationError(f"{label} is missing expected audio")
    if not expected_audio and audios:
        raise ValidationError(f"{label} unexpectedly contains audio")
    try:
        duration_ms = round(float(str(format_data.get("duration"))) * 1000)
    except (TypeError, ValueError):
        raise ValidationError(f"{label} duration is missing or invalid") from None
    if duration_ms <= 0:
        raise ValidationError(f"{label} duration must be positive")
    tolerance_ms = max(750, int(expected_duration_ms * 0.03))
    if abs(duration_ms - expected_duration_ms) > tolerance_ms:
        raise ValidationError(
            f"{label} duration differs from plan beyond tolerance "
            f"({duration_ms} vs {expected_duration_ms} ms)"
        )
    return duration_ms
