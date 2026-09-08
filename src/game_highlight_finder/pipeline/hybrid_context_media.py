"""Provider-free materialization of routed proposal-centered context media."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from game_highlight_finder.config import AppConfig
from game_highlight_finder.domain.models import ProxyMetadata, SourceAsset
from game_highlight_finder.domain.proposals import ProposalArtifact
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.media.ffmpeg import build_window_proxy_command, run_ffmpeg
from game_highlight_finder.media.ffprobe import run_ffprobe
from game_highlight_finder.media.tools import tool_identity
from game_highlight_finder.pipeline.context_expansion import (
    CONTEXT_PLANNER_VERSION,
    ContextExpansionPlan,
    plan_proposal_context,
)
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.hashing import hash_file
from game_highlight_finder.storage.sessions import SessionPaths

HYBRID_CONTEXT_MEDIA_VERSION = "c1-hybrid-context-media-v1"


class HybridContextMedia(BaseModel):
    """One committed analysis-proxy derivative for semantic inspection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    version: str = HYBRID_CONTEXT_MEDIA_VERSION
    context_id: str = Field(pattern=r"^hctx_[0-9a-f]{16}$")
    session_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(pattern=r"^src_[0-9a-f]{16}$")
    proposal_id: str = Field(pattern=r"^prop_[0-9a-f]{16}$")
    plan: ContextExpansionPlan
    proxy_path: str = Field(min_length=1, max_length=500)
    proxy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_proxy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    materialization_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    audio_present: bool
    warnings: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def plan_matches_identity(self) -> HybridContextMedia:
        if self.plan.proposal_id != self.proposal_id:
            raise ValueError("context plan belongs to a different proposal")
        return self


class HybridContextPreparationResult(BaseModel):
    """Local result for all routed context clips in one session."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    session_id: str
    contexts: tuple[HybridContextMedia, ...]
    generated: int = Field(ge=0)
    cache_hits: int = Field(ge=0)
    contexts_dir: Path


def prepare_hybrid_context_media(
    *,
    source: SourceAsset,
    session_id: str,
    proxy_path: Path,
    proxy_metadata: ProxyMetadata,
    routed_proposals: ProposalArtifact,
    paths: SessionPaths,
    config: AppConfig,
    force: bool = False,
) -> HybridContextPreparationResult:
    """Cut initial proposal-centered contexts from the committed analysis proxy only."""

    if routed_proposals.session_id != session_id:
        raise ValidationError("routed proposal artifact belongs to a different session")
    if routed_proposals.source_id != source.source_id:
        raise ValidationError("routed proposal artifact belongs to a different source")
    if routed_proposals.source_duration_ms != source.duration_ms:
        raise ValidationError("routed proposal duration does not match source")
    if not proxy_path.is_file():
        raise ValidationError("hybrid context preparation requires committed analysis proxy")
    if proxy_metadata.timestamp_mapping.source_duration_ms != source.duration_ms:
        raise ValidationError("analysis-proxy timestamp mapping does not match source duration")

    contexts_root = paths.hybrid_contexts_dir
    contexts_root.mkdir(parents=True, exist_ok=True)
    paths.tmp_dir.mkdir(parents=True, exist_ok=True)
    parent_sha = hash_file(proxy_path)
    ffmpeg = tool_identity("ffmpeg", config.tools.ffmpeg_path)
    ffprobe = tool_identity("ffprobe", config.tools.ffprobe_path, include_capabilities=False)

    contexts: list[HybridContextMedia] = []
    generated = 0
    cache_hits = 0
    for proposal in routed_proposals.proposals:
        plan = plan_proposal_context(proposal, source.duration_ms)
        context_id = deterministic_context_id(
            session_id=session_id,
            source_id=source.source_id,
            plan=plan,
        )
        item_dir = contexts_root / context_id
        item_dir.mkdir(parents=True, exist_ok=True)
        relative_proxy = f"hybrid/contexts/{context_id}/analysis_context.mp4"
        context_proxy = paths.root / relative_proxy
        metadata_path = item_dir / "context.json"
        materialization_key = _materialization_key(
            plan=plan,
            parent_proxy_sha256=parent_sha,
            ffmpeg_version=ffmpeg.version,
            ffprobe_version=ffprobe.version,
            video_codec=config.media.proxy.video_codec,
            preset=config.media.proxy.preset,
            audio_present=proxy_metadata.audio_present,
        )

        existing = _load_context(metadata_path)
        if (
            not force
            and existing is not None
            and existing.context_id == context_id
            and existing.materialization_key == materialization_key
            and existing.parent_proxy_sha256 == parent_sha
            and context_proxy.is_file()
            and existing.proxy_sha256 == hash_file(context_proxy)
        ):
            contexts.append(existing)
            cache_hits += 1
            continue

        proxy_start_ms = max(
            0,
            proxy_metadata.timestamp_mapping.source_to_proxy_ms(
                plan.context_start_ms + (source.timestamp_origin_ms or 0)
            ),
        )
        temp = paths.tmp_dir / f"{context_id}.partial.mp4"
        temp.unlink(missing_ok=True)
        run_ffmpeg(
            build_window_proxy_command(
                ffmpeg.path,
                proxy_path,
                temp,
                proxy_start_ms=proxy_start_ms,
                duration_ms=plan.context_duration_ms,
                has_audio=proxy_metadata.audio_present,
                video_codec=config.media.proxy.video_codec,
                preset=config.media.proxy.preset,
            ),
            duration_ms=plan.context_duration_ms,
            timeout_seconds=config.tools.ffmpeg_timeout_seconds,
            termination_grace_seconds=config.tools.termination_grace_seconds,
        )
        if not temp.is_file() or temp.stat().st_size <= 0:
            raise ValidationError(f"hybrid context proxy was not produced: {context_id}")
        run_ffprobe(
            ffprobe.path,
            temp,
            timeout_seconds=config.tools.probe_timeout_seconds,
        )
        temp.replace(context_proxy)
        committed = HybridContextMedia(
            context_id=context_id,
            session_id=session_id,
            source_id=source.source_id,
            proposal_id=proposal.proposal_id,
            plan=plan,
            proxy_path=relative_proxy,
            proxy_sha256=hash_file(context_proxy),
            parent_proxy_sha256=parent_sha,
            materialization_key=materialization_key,
            audio_present=proxy_metadata.audio_present,
            warnings=list(dict.fromkeys(proposal.sources))[:32],
        )
        atomic_write_json(metadata_path, committed.model_dump(mode="json"))
        contexts.append(committed)
        generated += 1

    return HybridContextPreparationResult(
        session_id=session_id,
        contexts=tuple(contexts),
        generated=generated,
        cache_hits=cache_hits,
        contexts_dir=contexts_root,
    )


def validate_hybrid_context_proxy(
    path: Path,
    contexts_root: Path,
    *,
    expected_parent_proxy_sha256: str | None = None,
) -> HybridContextMedia:
    """Accept only a committed hybrid analysis_context.mp4 with matching provenance."""

    resolved = path.resolve()
    root = contexts_root.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ValidationError("hybrid context path escapes the committed context root") from exc
    if relative.name != "analysis_context.mp4" or len(relative.parts) != 2:
        raise ValidationError("hybrid uploads must use committed analysis_context.mp4 artifacts")
    metadata_path = resolved.parent / "context.json"
    if not resolved.is_file() or not metadata_path.is_file():
        raise ValidationError("hybrid context proxy or provenance metadata is missing")
    try:
        context = HybridContextMedia.model_validate(read_json(metadata_path))
    except Exception as exc:
        raise ValidationError("hybrid context provenance metadata is invalid") from exc
    session_root = root.parent.parent
    expected_relative = resolved.relative_to(session_root).as_posix()
    if context.proxy_path.replace("\\", "/") != expected_relative:
        raise ValidationError("hybrid context proxy path does not match provenance metadata")
    if context.proxy_sha256 != hash_file(resolved):
        raise ValidationError("hybrid context proxy hash does not match provenance metadata")
    if (
        expected_parent_proxy_sha256 is not None
        and context.parent_proxy_sha256 != expected_parent_proxy_sha256
    ):
        raise ValidationError("hybrid context parent provenance does not match analysis proxy")
    return context


def deterministic_context_id(
    *,
    session_id: str,
    source_id: str,
    plan: ContextExpansionPlan,
) -> str:
    payload = {
        "planner_version": CONTEXT_PLANNER_VERSION,
        "session_id": session_id,
        "source_id": source_id,
        "proposal_id": plan.proposal_id,
        "context_start_ms": plan.context_start_ms,
        "context_end_ms": plan.context_end_ms,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"hctx_{hashlib.sha256(encoded).hexdigest()[:16]}"


def _materialization_key(
    *,
    plan: ContextExpansionPlan,
    parent_proxy_sha256: str,
    ffmpeg_version: str,
    ffprobe_version: str,
    video_codec: str,
    preset: str,
    audio_present: bool,
) -> str:
    payload = {
        "version": HYBRID_CONTEXT_MEDIA_VERSION,
        "plan": plan.model_dump(mode="json"),
        "parent_proxy_sha256": parent_proxy_sha256,
        "ffmpeg_version": ffmpeg_version,
        "ffprobe_version": ffprobe_version,
        "video_codec": video_codec,
        "preset": preset,
        "audio_present": audio_present,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_context(path: Path) -> HybridContextMedia | None:
    if not path.is_file():
        return None
    try:
        return HybridContextMedia.model_validate(read_json(path))
    except Exception:
        return None


__all__ = [
    "HYBRID_CONTEXT_MEDIA_VERSION",
    "HybridContextMedia",
    "HybridContextPreparationResult",
    "deterministic_context_id",
    "prepare_hybrid_context_media",
    "validate_hybrid_context_proxy",
]
