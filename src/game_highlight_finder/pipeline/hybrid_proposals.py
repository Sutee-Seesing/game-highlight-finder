"""Provider-free hybrid event proposals for sparse semantic Scout review.

This module is intentionally a *proposal* stage, not a highlight classifier. It uses
source-normalized audio activity plus lightweight frame-difference motion to choose a
bounded, high-recall subset of the timeline. A later semantic judge may reject every
proposal; local signals never become ground truth or a semantic highlight label.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from game_highlight_finder.config import AppConfig
from game_highlight_finder.domain.models import (
    PersistedModel,
    Sha256,
    SourceAsset,
    TimestampMapping,
)
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.media.ffmpeg import (
    build_motion_signal_command,
    build_window_proxy_command,
    run_ffmpeg,
)
from game_highlight_finder.media.ffprobe import run_ffprobe
from game_highlight_finder.media.tools import tool_identity
from game_highlight_finder.pipeline.local_signals import LocalSignalsResult
from game_highlight_finder.pipeline.proxy import ProxyResult
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.hashing import hash_file
from game_highlight_finder.storage.sessions import session_paths

_MOTION_RE = re.compile(
    r"pts_time:\s*(?P<seconds>[-+]?\d+(?:\.\d+)?)"
    r".*?lavfi\.signalstats\.YDIF=(?P<ydif>[-+]?\d+(?:\.\d+)?)",
    re.IGNORECASE | re.DOTALL,
)


class HybridProposalPolicy(PersistedModel):
    """Small explicit policy for the first provider-free hybrid proposer."""

    strategy_version: Literal["hybrid-proposer-v1"] = "hybrid-proposer-v1"
    motion_sample_fps: int = Field(default=4, ge=1, le=30)
    motion_width: int = Field(default=320, ge=64, le=1920)
    audio_anchors_per_10min: int = Field(default=6, ge=0, le=100)
    fused_anchors_per_10min: int = Field(default=2, ge=0, le=100)
    nms_gap_ms: int = Field(default=5_000, ge=0, le=120_000)
    pre_roll_ms: int = Field(default=8_000, ge=0, le=120_000)
    post_roll_ms: int = Field(default=12_000, ge=1, le=120_000)
    merge_gap_ms: int = Field(default=2_500, ge=0, le=120_000)
    max_proposal_duration_ms: int = Field(default=60_000, ge=1_000, le=900_000)
    split_overlap_ms: int = Field(default=10_000, ge=0, le=120_000)
    max_anchors: int = Field(default=512, ge=1, le=10_000)

    @model_validator(mode="after")
    def proposal_bounds_are_safe(self) -> HybridProposalPolicy:
        if self.max_proposal_duration_ms < self.pre_roll_ms + self.post_roll_ms:
            raise ValueError("max proposal duration must fit one full event-centered window")
        if self.split_overlap_ms >= self.max_proposal_duration_ms:
            raise ValueError("split overlap must be smaller than max proposal duration")
        return self


class MotionActivitySample(PersistedModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    peak_ydif: float = Field(ge=0, le=255)


class MotionSignalArtifact(PersistedModel):
    schema_version: Literal[1] = 1
    source_duration_ms: int = Field(gt=0)
    parent_proxy_sha256: Sha256
    sample_fps: int = Field(ge=1, le=30)
    analysis_width: int = Field(ge=64, le=1920)
    ffmpeg_identity: str = Field(min_length=1, max_length=2_000)
    samples: list[MotionActivitySample] = Field(default_factory=list, max_length=20_000)


class HybridAnchor(PersistedModel):
    anchor_ms: int = Field(ge=0)
    audio_percentile: float = Field(ge=0, le=1)
    motion_percentile: float = Field(ge=0, le=1)
    fused_score: float = Field(ge=0, le=1.35)
    selected_by: Literal["AUDIO", "FUSED", "AUDIO_FALLBACK", "MOTION_FALLBACK"]


class HybridProposal(PersistedModel):
    proposal_id: str = Field(pattern=r"^proposal_[0-9a-f]{16}$")
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    anchors: list[HybridAnchor] = Field(min_length=1, max_length=256)


class HybridProposalPlan(PersistedModel):
    schema_version: Literal[1] = 1
    strategy_version: Literal["hybrid-proposer-v1"] = "hybrid-proposer-v1"
    session_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(pattern=r"^src_[0-9a-f]{16}$")
    source_duration_ms: int = Field(gt=0)
    parent_proxy_sha256: Sha256
    local_signals_sha256: Sha256
    policy: HybridProposalPolicy
    anchors: list[HybridAnchor] = Field(default_factory=list, max_length=10_000)
    proposals: list[HybridProposal] = Field(default_factory=list, max_length=10_000)
    total_proposed_duration_ms: int = Field(ge=0)
    proposal_ratio: float = Field(ge=0, le=1)
    semantic_labels_inferred: Literal[False] = False
    provider_calls: Literal[0] = 0
    plan_hash: Sha256 | None = None


class PreparedHybridProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal: HybridProposal
    proxy_path: Path
    proxy_sha256: Sha256
    cache_hit: bool


class HybridProposalPreparation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan: HybridProposalPlan
    plan_path: Path
    motion_path: Path
    motion_cache_hit: bool
    prepared: tuple[PreparedHybridProposal, ...]
    cache_hits: int = Field(ge=0)
    generated: int = Field(ge=0)


def parse_motion_activity(
    text: str,
    *,
    source_duration_ms: int,
    proxy_mapping: TimestampMapping | None = None,
) -> dict[int, float]:
    """Parse FFmpeg YDIF metadata into source-relative one-second peak buckets."""

    buckets: dict[int, float] = {}
    for match in _MOTION_RE.finditer(text):
        try:
            proxy_ms = round(float(match.group("seconds")) * 1_000)
            ydif = float(match.group("ydif"))
        except ValueError:
            continue
        if proxy_ms < 0 or not 0 <= ydif <= 255:
            continue
        source_ms = proxy_ms
        if proxy_mapping is not None:
            try:
                mapped = proxy_mapping.proxy_to_source_ms(proxy_ms)
                source_ms = mapped - int(proxy_mapping.source_start_ms)
            except (AttributeError, TypeError, ValueError):
                continue
        source_ms = max(0, min(source_duration_ms - 1, source_ms))
        second = source_ms // 1_000
        buckets[second] = max(buckets.get(second, 0.0), ydif)
    return buckets


def audio_activity_scores(result: LocalSignalsResult) -> dict[int, float]:
    """Return one-second source-relative audio peaks without using an absolute threshold."""

    buckets: dict[int, float] = {}
    for item in result.signals.audio_activity:
        value = item.peak_db if item.peak_db is not None else item.mean_db
        if value is None:
            continue
        second = item.start_ms // 1_000
        buckets[second] = max(buckets.get(second, -200.0), float(value))
    return buckets


def percentile_ranks(values: dict[int, float]) -> dict[int, float]:
    """Convert source-local signal magnitudes into an empirical 0..1 CDF rank."""

    if not values:
        return {}
    ordered = sorted(values.values())
    count = len(ordered)
    return {
        key: bisect.bisect_right(ordered, value) / count
        for key, value in values.items()
    }


def plan_hybrid_proposals(
    *,
    session_id: str,
    source_id: str,
    source_duration_ms: int,
    parent_proxy_sha256: str,
    local_signals_sha256: str,
    audio_scores: dict[int, float],
    motion_scores: dict[int, float],
    policy: HybridProposalPolicy | None = None,
) -> HybridProposalPlan:
    """Build deterministic sparse proposal windows from provider-free local signals."""

    if source_duration_ms <= 0:
        raise ValidationError("Hybrid proposal source duration must be positive")
    resolved_policy = policy or HybridProposalPolicy()
    audio_rank = percentile_ranks(audio_scores)
    motion_rank = percentile_ranks(motion_scores)
    all_seconds = set(audio_rank) | set(motion_rank)
    fused = {
        second: max(audio_rank.get(second, 0.0), motion_rank.get(second, 0.0))
        + 0.35 * min(audio_rank.get(second, 0.0), motion_rank.get(second, 0.0))
        for second in all_seconds
    }
    audio_budget = _scaled_anchor_budget(
        source_duration_ms, resolved_policy.audio_anchors_per_10min
    )
    fused_budget = _scaled_anchor_budget(
        source_duration_ms, resolved_policy.fused_anchors_per_10min
    )
    total_budget = min(resolved_policy.max_anchors, audio_budget + fused_budget)

    selected: list[tuple[int, str]] = []
    if audio_rank and motion_rank:
        audio_count = min(audio_budget, total_budget)
        audio_seconds = _select_seconds(
            audio_rank,
            audio_count,
            gap_ms=resolved_policy.nms_gap_ms,
        )
        selected.extend((second, "AUDIO") for second in audio_seconds)
        remaining = total_budget - len(selected)
        fused_seconds = _select_seconds(
            fused,
            min(fused_budget, remaining),
            gap_ms=resolved_policy.nms_gap_ms,
            blocked_seconds=audio_seconds,
        )
        selected.extend((second, "FUSED") for second in fused_seconds)
    elif audio_rank:
        selected.extend(
            (second, "AUDIO_FALLBACK")
            for second in _select_seconds(
                audio_rank,
                total_budget,
                gap_ms=resolved_policy.nms_gap_ms,
            )
        )
    elif motion_rank:
        selected.extend(
            (second, "MOTION_FALLBACK")
            for second in _select_seconds(
                motion_rank,
                total_budget,
                gap_ms=resolved_policy.nms_gap_ms,
            )
        )

    anchors = [
        HybridAnchor(
            anchor_ms=min(source_duration_ms - 1, second * 1_000),
            audio_percentile=audio_rank.get(second, 0.0),
            motion_percentile=motion_rank.get(second, 0.0),
            fused_score=fused.get(
                second,
                max(audio_rank.get(second, 0.0), motion_rank.get(second, 0.0)),
            ),
            selected_by=selection,  # type: ignore[arg-type]
        )
        for second, selection in selected
    ]
    anchors.sort(key=lambda item: (item.anchor_ms, item.selected_by))
    proposals = _proposal_windows(source_id, source_duration_ms, anchors, resolved_policy)
    total_proposed = _union_duration_ms(proposals)
    plan = HybridProposalPlan(
        session_id=session_id,
        source_id=source_id,
        source_duration_ms=source_duration_ms,
        parent_proxy_sha256=parent_proxy_sha256,
        local_signals_sha256=local_signals_sha256,
        policy=resolved_policy,
        anchors=anchors,
        proposals=proposals,
        total_proposed_duration_ms=total_proposed,
        proposal_ratio=total_proposed / source_duration_ms,
    )
    payload = plan.model_dump(mode="json", exclude={"plan_hash"})
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return plan.model_copy(update={"plan_hash": digest})


def prepare_hybrid_proposals(
    source: SourceAsset,
    proxy: ProxyResult,
    local_signals: LocalSignalsResult,
    config: AppConfig,
    *,
    policy: HybridProposalPolicy | None = None,
) -> HybridProposalPreparation:
    """Create/reuse sparse proposal clips from the committed analysis proxy only."""

    if proxy.session_id != local_signals.session_id:
        raise ValidationError("Hybrid proposal inputs belong to different sessions")
    if local_signals.signals.source_duration_ms != source.duration_ms:
        raise ValidationError("Hybrid proposal local signals do not match source duration")
    if not proxy.proxy_path.is_file() or proxy.proxy_path.name != "analysis_proxy.mp4":
        raise ValidationError("Hybrid proposals require the committed analysis_proxy.mp4")

    resolved_policy = policy or HybridProposalPolicy()
    paths = session_paths(config.storage.data_dir, proxy.session_id)
    root = paths.scout_dir / "proposals"
    clips_root = root / "clips"
    root.mkdir(parents=True, exist_ok=True)
    clips_root.mkdir(parents=True, exist_ok=True)
    parent_sha = hash_file(proxy.proxy_path)
    signals_sha = hash_file(local_signals.signals_path)
    motion_path = root / "motion.v1.json"
    motion, motion_cache_hit = _load_or_measure_motion(
        motion_path,
        source,
        proxy,
        config,
        resolved_policy,
        parent_sha,
    )
    motion_scores = {sample.start_ms // 1_000: sample.peak_ydif for sample in motion.samples}
    plan = plan_hybrid_proposals(
        session_id=proxy.session_id,
        source_id=source.source_id,
        source_duration_ms=source.duration_ms,
        parent_proxy_sha256=parent_sha,
        local_signals_sha256=signals_sha,
        audio_scores=audio_activity_scores(local_signals),
        motion_scores=motion_scores,
        policy=resolved_policy,
    )
    plan_path = root / "plan.json"
    atomic_write_json(plan_path, plan.model_dump(mode="json"))

    ffmpeg = tool_identity("ffmpeg", config.tools.ffmpeg_path)
    ffprobe = tool_identity("ffprobe", config.tools.ffprobe_path, include_capabilities=False)
    prepared: list[PreparedHybridProposal] = []
    generated = 0
    cache_hits = 0
    for proposal in plan.proposals:
        item_dir = clips_root / proposal.proposal_id
        item_dir.mkdir(parents=True, exist_ok=True)
        clip_path = item_dir / "analysis_proposal.mp4"
        metadata_path = item_dir / "proposal.json"
        cached_sha = _validated_clip_cache(metadata_path, clip_path, proposal, parent_sha)
        if cached_sha is not None:
            cache_hits += 1
            prepared.append(
                PreparedHybridProposal(
                    proposal=proposal,
                    proxy_path=clip_path,
                    proxy_sha256=cached_sha,
                    cache_hit=True,
                )
            )
            continue

        source_origin = source.timestamp_origin_ms or 0
        proxy_start_ms = max(
            0,
            proxy.metadata.timestamp_mapping.source_to_proxy_ms(proposal.start_ms + source_origin),
        )
        temp = item_dir / "analysis_proposal.partial.mp4"
        temp.unlink(missing_ok=True)
        run_ffmpeg(
            build_window_proxy_command(
                ffmpeg.path,
                proxy.proxy_path,
                temp,
                proxy_start_ms=proxy_start_ms,
                duration_ms=proposal.end_ms - proposal.start_ms,
                has_audio=proxy.metadata.audio_present,
                video_codec=config.media.proxy.video_codec,
                preset=config.media.proxy.preset,
            ),
            duration_ms=proposal.end_ms - proposal.start_ms,
            timeout_seconds=config.tools.ffmpeg_timeout_seconds,
            termination_grace_seconds=config.tools.termination_grace_seconds,
        )
        if not temp.is_file() or temp.stat().st_size <= 0:
            raise ValidationError(f"Hybrid proposal clip was not produced: {proposal.proposal_id}")
        run_ffprobe(ffprobe.path, temp, timeout_seconds=config.tools.probe_timeout_seconds)
        temp.replace(clip_path)
        clip_sha = hash_file(clip_path)
        atomic_write_json(
            metadata_path,
            {
                "schema_version": 1,
                "strategy_version": resolved_policy.strategy_version,
                "proposal": proposal.model_dump(mode="json"),
                "parent_proxy_sha256": parent_sha,
                "clip_sha256": clip_sha,
                "semantic_labels_inferred": False,
                "provider_calls": 0,
            },
        )
        generated += 1
        prepared.append(
            PreparedHybridProposal(
                proposal=proposal,
                proxy_path=clip_path,
                proxy_sha256=clip_sha,
                cache_hit=False,
            )
        )
    return HybridProposalPreparation(
        plan=plan,
        plan_path=plan_path,
        motion_path=motion_path,
        motion_cache_hit=motion_cache_hit,
        prepared=tuple(prepared),
        cache_hits=cache_hits,
        generated=generated,
    )


def _load_or_measure_motion(
    path: Path,
    source: SourceAsset,
    proxy: ProxyResult,
    config: AppConfig,
    policy: HybridProposalPolicy,
    parent_sha: str,
) -> tuple[MotionSignalArtifact, bool]:
    ffmpeg = tool_identity("ffmpeg", config.tools.ffmpeg_path)
    if path.is_file():
        try:
            cached = MotionSignalArtifact.model_validate(read_json(path))
            if (
                cached.source_duration_ms == source.duration_ms
                and cached.parent_proxy_sha256 == parent_sha
                and cached.sample_fps == policy.motion_sample_fps
                and cached.analysis_width == policy.motion_width
                and cached.ffmpeg_identity == ffmpeg.version
            ):
                return cached, True
        except Exception:
            pass
    result = run_ffmpeg(
        build_motion_signal_command(
            ffmpeg.path,
            proxy.proxy_path,
            sample_fps=policy.motion_sample_fps,
            width=policy.motion_width,
        ),
        duration_ms=proxy.metadata.duration_ms,
        timeout_seconds=config.tools.ffmpeg_timeout_seconds,
        termination_grace_seconds=config.tools.termination_grace_seconds,
        max_stderr_lines=160_000,
        max_stderr_chars=20_000_000,
    )
    buckets = parse_motion_activity(
        result.stderr,
        source_duration_ms=source.duration_ms,
        proxy_mapping=proxy.metadata.timestamp_mapping,
    )
    samples = [
        MotionActivitySample(
            start_ms=second * 1_000,
            end_ms=min(source.duration_ms, (second + 1) * 1_000),
            peak_ydif=value,
        )
        for second, value in sorted(buckets.items())
        if second * 1_000 < source.duration_ms
    ]
    artifact = MotionSignalArtifact(
        source_duration_ms=source.duration_ms,
        parent_proxy_sha256=parent_sha,
        sample_fps=policy.motion_sample_fps,
        analysis_width=policy.motion_width,
        ffmpeg_identity=ffmpeg.version,
        samples=samples,
    )
    atomic_write_json(path, artifact.model_dump(mode="json"))
    return artifact, False


def _scaled_anchor_budget(source_duration_ms: int, per_10min: int) -> int:
    if per_10min <= 0:
        return 0
    # Nearest-integer scaling keeps a ~10 minute source at exactly the declared budget.
    scaled = (source_duration_ms * per_10min + 300_000) // 600_000
    return max(1, scaled)


def _select_seconds(
    scores: dict[int, float],
    count: int,
    *,
    gap_ms: int,
    blocked_seconds: list[int] | None = None,
) -> list[int]:
    if count <= 0:
        return []
    blocked = blocked_seconds or []
    selected: list[int] = []
    for second, _score in sorted(scores.items(), key=lambda item: (-item[1], item[0])):
        anchor_ms = second * 1_000
        if any(abs(anchor_ms - item * 1_000) < gap_ms for item in blocked):
            continue
        if any(abs(anchor_ms - item * 1_000) < gap_ms for item in selected):
            continue
        selected.append(second)
        if len(selected) >= count:
            break
    return selected


def _proposal_windows(
    source_id: str,
    source_duration_ms: int,
    anchors: list[HybridAnchor],
    policy: HybridProposalPolicy,
) -> list[HybridProposal]:
    raw = [
        (
            max(0, anchor.anchor_ms - policy.pre_roll_ms),
            min(source_duration_ms, anchor.anchor_ms + policy.post_roll_ms),
            [anchor],
        )
        for anchor in anchors
    ]
    raw.sort(key=lambda item: (item[0], item[1]))
    merged: list[tuple[int, int, list[HybridAnchor]]] = []
    for start, end, items in raw:
        if not merged:
            merged.append((start, end, items))
            continue
        previous_start, previous_end, previous_items = merged[-1]
        proposed_end = max(previous_end, end)
        can_merge = (
            start <= previous_end + policy.merge_gap_ms
            and proposed_end - previous_start <= policy.max_proposal_duration_ms
        )
        if can_merge:
            merged[-1] = (
                previous_start,
                proposed_end,
                [*previous_items, *items],
            )
            continue
        if start <= previous_end + policy.merge_gap_ms:
            overlap_start = min(start, max(previous_start, previous_end - policy.split_overlap_ms))
            overlap_start = max(overlap_start, end - policy.max_proposal_duration_ms)
            merged.append((overlap_start, end, items))
        else:
            merged.append((start, end, items))
    proposals: list[HybridProposal] = []
    for start, end, items in merged:
        payload = {
            "strategy": policy.strategy_version,
            "source_id": source_id,
            "start_ms": start,
            "end_ms": end,
            "anchors": [item.anchor_ms for item in items],
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        proposals.append(
            HybridProposal(
                proposal_id=f"proposal_{digest}",
                start_ms=start,
                end_ms=end,
                anchors=items,
            )
        )
    return proposals


def _union_duration_ms(proposals: list[HybridProposal]) -> int:
    if not proposals:
        return 0
    intervals = sorted((item.start_ms, item.end_ms) for item in proposals)
    total = 0
    current_start, current_end = intervals[0]
    for start, end in intervals[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
            continue
        total += current_end - current_start
        current_start, current_end = start, end
    return total + current_end - current_start


def _validated_clip_cache(
    metadata_path: Path,
    clip_path: Path,
    proposal: HybridProposal,
    parent_sha: str,
) -> str | None:
    if not metadata_path.is_file() or not clip_path.is_file():
        return None
    try:
        metadata = read_json(metadata_path)
        if metadata.get("proposal") != proposal.model_dump(mode="json"):
            return None
        if metadata.get("parent_proxy_sha256") != parent_sha:
            return None
        clip_sha = hash_file(clip_path)
        if metadata.get("clip_sha256") != clip_sha:
            return None
        return clip_sha
    except Exception:
        return None


__all__ = [
    "HybridAnchor",
    "HybridProposal",
    "HybridProposalPlan",
    "HybridProposalPolicy",
    "HybridProposalPreparation",
    "MotionActivitySample",
    "MotionSignalArtifact",
    "PreparedHybridProposal",
    "audio_activity_scores",
    "parse_motion_activity",
    "percentile_ranks",
    "plan_hybrid_proposals",
    "prepare_hybrid_proposals",
]
