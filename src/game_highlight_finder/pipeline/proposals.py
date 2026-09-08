"""Deterministic provider-free proposal generation and clustering from local evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime

from game_highlight_finder import __version__
from game_highlight_finder.domain.models import LocalSignalsArtifact
from game_highlight_finder.domain.proposals import (
    ManualProposalMarkerSet,
    Proposal,
    ProposalArtifact,
    ProposalRoute,
    ProposalRoutingDecision,
    ProposalRoutingPlan,
    ProposalSignalType,
    ProposalSummary,
    TranscriptFixture,
)

ROUTING_POLICY_VERSION = "c1-proposal-routing-v1"
DEFAULT_MAX_PROPOSAL_INTERVAL_MS = 20_000
DEFAULT_MAX_PROPOSALS = 2_000
DEFAULT_CLUSTER_GAP_MS = 750
DEFAULT_MAX_CLUSTER_SPAN_MS = 12_000

_SIGNAL_PRIORITY = {
    ProposalSignalType.GAME_EVENT: 70,
    ProposalSignalType.MANUAL_MARKER: 60,
    ProposalSignalType.OCR_STATE_CHANGE: 50,
    ProposalSignalType.VISUAL_STATE_CHANGE: 40,
    ProposalSignalType.ASR_UTTERANCE: 30,
    ProposalSignalType.SCENE_ACTIVITY: 20,
    ProposalSignalType.AUDIO_ACTIVITY: 10,
}


def deterministic_proposal_id(
    *,
    source_id: str,
    start_ms: int,
    end_ms: int,
    signal_type: ProposalSignalType,
    event_hypothesis: str | None,
    sources: tuple[str, ...],
) -> str:
    payload = json.dumps(
        {
            "source_id": source_id,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "signal_type": signal_type.value,
            "event_hypothesis": event_hypothesis,
            "sources": sources,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"prop_{hashlib.sha256(payload).hexdigest()[:16]}"


def proposals_from_local_signals(
    *,
    session_id: str,
    source_id: str,
    signals: LocalSignalsArtifact,
    max_interval_ms: int = DEFAULT_MAX_PROPOSAL_INTERVAL_MS,
    max_proposals: int = DEFAULT_MAX_PROPOSALS,
    created_at: datetime | None = None,
) -> ProposalArtifact:
    """Map weak local activity evidence into factual anchors without creator scoring.

    This is intentionally an interface proof, not a claim that loudness or scene activity
    is a sufficient highlight detector. Broad fallback intervals are skipped instead of
    pretending that an entire active source is one useful proposal.
    """

    if max_interval_ms <= 0:
        raise ValueError("max_interval_ms must be positive")
    if max_proposals <= 0:
        raise ValueError("max_proposals must be positive")

    proposals: list[Proposal] = []
    warnings = list(signals.warnings)
    skipped_broad_audio = 0
    skipped_broad_scene = 0

    for interval in signals.audio_activity:
        if not interval.active:
            continue
        duration_ms = interval.end_ms - interval.start_ms
        if duration_ms > max_interval_ms:
            skipped_broad_audio += 1
            continue
        metadata: dict[str, str] = {}
        if interval.mean_db is not None:
            metadata["mean_db"] = f"{interval.mean_db:.3f}"
        proposals.append(
            _proposal(
                source_id=source_id,
                start_ms=interval.start_ms,
                end_ms=interval.end_ms,
                signal_type=ProposalSignalType.AUDIO_ACTIVITY,
                source="local_audio_activity",
                metadata=metadata,
            )
        )

    for scene_interval in signals.scene_activity:
        duration_ms = scene_interval.end_ms - scene_interval.start_ms
        if duration_ms > max_interval_ms:
            skipped_broad_scene += 1
            continue
        proposals.append(
            _proposal(
                source_id=source_id,
                start_ms=scene_interval.start_ms,
                end_ms=scene_interval.end_ms,
                signal_type=ProposalSignalType.SCENE_ACTIVITY,
                source="local_scene_activity",
            )
        )

    if skipped_broad_audio:
        warnings.append(
            f"Skipped {skipped_broad_audio} broad audio-activity interval(s); "
            "broad activity is navigation evidence, not a factual moment anchor."
        )
    if skipped_broad_scene:
        warnings.append(
            f"Skipped {skipped_broad_scene} broad scene-activity interval(s); "
            "broad activity is navigation evidence, not a factual moment anchor."
        )

    unique: dict[str, Proposal] = {proposal.proposal_id: proposal for proposal in proposals}
    ordered = _ordered(unique.values())
    if len(ordered) > max_proposals:
        warnings.append(
            f"Proposal cap applied: kept first {max_proposals} of {len(ordered)} factual anchors."
        )
        ordered = ordered[:max_proposals]

    return ProposalArtifact(
        created_at=created_at or datetime.now(UTC),
        producer_version=__version__,
        session_id=session_id,
        source_id=source_id,
        source_duration_ms=signals.source_duration_ms,
        proposals=ordered,
        warnings=warnings,
    )


def proposals_from_manual_markers(
    *,
    session_id: str,
    source_id: str,
    source_sha256: str,
    source_duration_ms: int,
    marker_set: ManualProposalMarkerSet,
    created_at: datetime | None = None,
) -> ProposalArtifact:
    """Bind source-checked manual markers into the same factual proposal contract."""

    if marker_set.source_sha256 != source_sha256:
        raise ValueError("manual marker source SHA-256 does not match the analyzed source")
    if marker_set.source_duration_ms != source_duration_ms:
        raise ValueError("manual marker source duration does not match the analyzed source")

    proposals = [
        _proposal(
            source_id=source_id,
            start_ms=marker.start_ms,
            end_ms=marker.end_ms,
            signal_type=ProposalSignalType.MANUAL_MARKER,
            source="manual_marker",
            event_hypothesis=marker.event_hypothesis,
            confidence=marker.confidence,
            metadata={"label": marker.label},
        )
        for marker in marker_set.markers
    ]
    return ProposalArtifact(
        created_at=created_at or datetime.now(UTC),
        producer_version=__version__,
        session_id=session_id,
        source_id=source_id,
        source_duration_ms=source_duration_ms,
        proposals=_ordered(proposals),
        warnings=list(marker_set.notes),
    )


def proposals_from_transcript(
    *,
    session_id: str,
    source_id: str,
    source_sha256: str,
    source_duration_ms: int,
    transcript: TranscriptFixture,
    created_at: datetime | None = None,
) -> ProposalArtifact:
    """Bind source-checked transcript utterances as factual speech anchors only."""

    if transcript.source_sha256 != source_sha256:
        raise ValueError("transcript source SHA-256 does not match the analyzed source")
    if transcript.source_duration_ms != source_duration_ms:
        raise ValueError("transcript source duration does not match the analyzed source")

    proposals: list[Proposal] = []
    for utterance in transcript.utterances:
        metadata = {"text": utterance.text}
        if utterance.speaker is not None:
            metadata["speaker"] = utterance.speaker
        if utterance.language is not None:
            metadata["language"] = utterance.language
        proposals.append(
            _proposal(
                source_id=source_id,
                start_ms=utterance.start_ms,
                end_ms=utterance.end_ms,
                signal_type=ProposalSignalType.ASR_UTTERANCE,
                source="transcript_fixture",
                confidence=utterance.confidence,
                metadata=metadata,
            )
        )
    return ProposalArtifact(
        created_at=created_at or datetime.now(UTC),
        producer_version=__version__,
        session_id=session_id,
        source_id=source_id,
        source_duration_ms=source_duration_ms,
        proposals=_ordered(proposals),
        warnings=list(transcript.notes),
    )


def summarize_proposals(artifact: ProposalArtifact) -> ProposalSummary:
    """Measure proposal density and clustering burden without creator-quality claims."""

    anchor_count = sum(_raw_anchor_count(proposal) for proposal in artifact.proposals)
    neighborhood_count = len(artifact.proposals)
    source_hours = artifact.source_duration_ms / 3_600_000
    by_signal_type: dict[str, int] = {}
    for proposal in artifact.proposals:
        key = proposal.signal_type.value
        by_signal_type[key] = by_signal_type.get(key, 0) + 1
    return ProposalSummary(
        created_at=artifact.created_at,
        session_id=artifact.session_id,
        source_id=artifact.source_id,
        source_duration_ms=artifact.source_duration_ms,
        factual_anchor_count_before_clustering=anchor_count,
        proposal_neighborhood_count=neighborhood_count,
        clustered_reduction_count=max(0, anchor_count - neighborhood_count),
        multi_source_neighborhood_count=sum(
            len(proposal.sources) > 1 for proposal in artifact.proposals
        ),
        explicit_hypothesis_count=sum(
            proposal.event_hypothesis is not None for proposal in artifact.proposals
        ),
        proposals_per_source_hour=(
            neighborhood_count / source_hours if source_hours > 0 else 0.0
        ),
        by_signal_type=by_signal_type,
    )


def route_proposals(
    artifact: ProposalArtifact,
    *,
    weak_sample_interval_ms: int,
    created_at: datetime | None = None,
) -> ProposalRoutingPlan:
    """Bound semantic-inspection density without deleting factual proposal evidence.

    Strong/explicit or multi-source neighborhoods route directly. Weak single-source
    neighborhoods are deterministically sampled for temporal coverage; all others remain
    preserved as deferred decisions in the routing plan.
    """

    if weak_sample_interval_ms <= 0:
        raise ValueError("weak_sample_interval_ms must be positive")

    route_by_id: dict[str, ProposalRoute] = {}
    reason_by_id: dict[str, str] = {}
    weak_by_bucket: dict[int, list[Proposal]] = {}
    covered_buckets: set[int] = set()

    for proposal in artifact.proposals:
        bucket = _routing_bucket(proposal, weak_sample_interval_ms)
        if proposal.event_hypothesis is not None or proposal.signal_type in {
            ProposalSignalType.GAME_EVENT,
            ProposalSignalType.MANUAL_MARKER,
        }:
            route_by_id[proposal.proposal_id] = ProposalRoute.MUST_INSPECT
            reason_by_id[proposal.proposal_id] = (
                "Explicit/manual/game-state factual anchor bypasses weak-evidence sampling."
            )
            covered_buckets.add(bucket)
        elif len(proposal.sources) > 1:
            route_by_id[proposal.proposal_id] = ProposalRoute.SUPPORTED
            reason_by_id[proposal.proposal_id] = (
                "Multiple independent evidence sources support this proposal neighborhood."
            )
            covered_buckets.add(bucket)
        else:
            weak_by_bucket.setdefault(bucket, []).append(proposal)

    for bucket, weak in sorted(weak_by_bucket.items()):
        ordered = sorted(weak, key=_weak_routing_key)
        sampled_id: str | None = None
        if bucket not in covered_buckets and ordered:
            sampled_id = ordered[0].proposal_id
        for proposal in ordered:
            if proposal.proposal_id == sampled_id:
                route_by_id[proposal.proposal_id] = ProposalRoute.SAMPLED_WEAK
                reason_by_id[proposal.proposal_id] = (
                    "Deterministic weak-evidence sample retained for temporal coverage."
                )
            else:
                route_by_id[proposal.proposal_id] = ProposalRoute.DEFERRED_WEAK
                reason_by_id[proposal.proposal_id] = (
                    "Weak single-source evidence retained for audit but deferred from this "
                    "semantic-inspection pass."
                )

    decisions = [
        ProposalRoutingDecision(
            proposal_id=proposal.proposal_id,
            route=route_by_id[proposal.proposal_id],
            reason=reason_by_id[proposal.proposal_id],
        )
        for proposal in artifact.proposals
    ]
    selected_ids = [
        decision.proposal_id
        for decision in decisions
        if decision.route is not ProposalRoute.DEFERRED_WEAK
    ]
    deferred_ids = [
        decision.proposal_id
        for decision in decisions
        if decision.route is ProposalRoute.DEFERRED_WEAK
    ]
    source_hours = artifact.source_duration_ms / 3_600_000
    route_counts = {
        route.value: sum(decision.route is route for decision in decisions)
        for route in ProposalRoute
    }
    return ProposalRoutingPlan(
        policy_version=ROUTING_POLICY_VERSION,
        created_at=created_at or artifact.created_at,
        session_id=artifact.session_id,
        source_id=artifact.source_id,
        source_duration_ms=artifact.source_duration_ms,
        weak_sample_interval_ms=weak_sample_interval_ms,
        decisions=decisions,
        selected_proposal_ids=selected_ids,
        deferred_proposal_ids=deferred_ids,
        selected_per_source_hour=(len(selected_ids) / source_hours if source_hours > 0 else 0.0),
        route_counts=route_counts,
    )


def selected_proposal_artifact(
    artifact: ProposalArtifact,
    routing: ProposalRoutingPlan,
) -> ProposalArtifact:
    """Materialize the routed semantic-inspection subset without mutating full evidence."""

    if (
        artifact.session_id != routing.session_id
        or artifact.source_id != routing.source_id
        or artifact.source_duration_ms != routing.source_duration_ms
    ):
        raise ValueError("proposal routing plan belongs to a different source session")
    proposal_ids = {proposal.proposal_id for proposal in artifact.proposals}
    decision_ids = {decision.proposal_id for decision in routing.decisions}
    if proposal_ids != decision_ids:
        raise ValueError("proposal routing plan does not cover the exact proposal artifact")
    selected = set(routing.selected_proposal_ids)
    return artifact.model_copy(
        update={
            "proposals": [
                proposal for proposal in artifact.proposals if proposal.proposal_id in selected
            ],
            "warnings": [
                *artifact.warnings,
                (
                    f"Routing {routing.policy_version}: selected {len(selected)} of "
                    f"{len(artifact.proposals)} proposal neighborhoods."
                ),
            ][:100],
        }
    )


def combine_and_cluster_proposals(
    artifacts: Iterable[ProposalArtifact],
    *,
    cluster_gap_ms: int = DEFAULT_CLUSTER_GAP_MS,
    max_cluster_span_ms: int = DEFAULT_MAX_CLUSTER_SPAN_MS,
    max_proposals: int = DEFAULT_MAX_PROPOSALS,
) -> ProposalArtifact:
    """Merge compatible nearby factual anchors while preserving evidence provenance.

    Clustering reduces duplicate review/inference neighborhoods. It does not assign creator
    value, and conflicting explicit event hypotheses are never merged.
    """

    if cluster_gap_ms < 0:
        raise ValueError("cluster_gap_ms cannot be negative")
    if max_cluster_span_ms <= 0:
        raise ValueError("max_cluster_span_ms must be positive")
    if max_proposals <= 0:
        raise ValueError("max_proposals must be positive")

    items = list(artifacts)
    if not items:
        raise ValueError("at least one proposal artifact is required")
    base = items[0]
    warnings: list[str] = []
    flattened: list[Proposal] = []
    for artifact in items:
        if (
            artifact.session_id != base.session_id
            or artifact.source_id != base.source_id
            or artifact.source_duration_ms != base.source_duration_ms
        ):
            raise ValueError("proposal artifacts must describe the same source session")
        flattened.extend(artifact.proposals)
        warnings.extend(artifact.warnings)

    ordered = _ordered({proposal.proposal_id: proposal for proposal in flattened}.values())
    clusters: list[list[Proposal]] = []
    for proposal in ordered:
        if not clusters or not _can_cluster(
            clusters[-1],
            proposal,
            cluster_gap_ms=cluster_gap_ms,
            max_cluster_span_ms=max_cluster_span_ms,
        ):
            clusters.append([proposal])
        else:
            clusters[-1].append(proposal)

    merged = [_merge_cluster(base.source_id, cluster) for cluster in clusters]
    if len(merged) < len(ordered):
        warnings.append(
            f"Clustered {len(ordered)} factual anchors into {len(merged)} proposal neighborhoods."
        )
    if len(merged) > max_proposals:
        warnings.append(
            f"Proposal cap applied after clustering: kept first {max_proposals} of {len(merged)}."
        )
        merged = merged[:max_proposals]

    return ProposalArtifact(
        created_at=base.created_at,
        producer_version=__version__,
        session_id=base.session_id,
        source_id=base.source_id,
        source_duration_ms=base.source_duration_ms,
        proposals=merged,
        warnings=list(dict.fromkeys(warnings))[:100],
    )


def _can_cluster(
    cluster: list[Proposal],
    proposal: Proposal,
    *,
    cluster_gap_ms: int,
    max_cluster_span_ms: int,
) -> bool:
    first = cluster[0]
    last = cluster[-1]
    if proposal.start_ms > last.end_ms + cluster_gap_ms:
        return False
    cluster_span_ms = max(last.end_ms, proposal.end_ms) - min(
        first.start_ms,
        proposal.start_ms,
    )
    if cluster_span_ms > max_cluster_span_ms:
        return False
    hypotheses = {
        item.event_hypothesis for item in [*cluster, proposal] if item.event_hypothesis is not None
    }
    return len(hypotheses) <= 1


def _merge_cluster(source_id: str, cluster: list[Proposal]) -> Proposal:
    if len(cluster) == 1:
        return cluster[0]
    start_ms = min(item.start_ms for item in cluster)
    end_ms = max(item.end_ms for item in cluster)
    signal_type = max(cluster, key=lambda item: _SIGNAL_PRIORITY[item.signal_type]).signal_type
    hypotheses = [item.event_hypothesis for item in cluster if item.event_hypothesis is not None]
    event_hypothesis = hypotheses[0] if hypotheses else None
    sources = tuple(sorted({source for item in cluster for source in item.sources}))
    metadata = _merge_metadata(cluster)
    metadata["cluster_size"] = str(sum(_raw_anchor_count(item) for item in cluster))
    metadata["signal_types"] = ",".join(sorted({item.signal_type.value for item in cluster}))
    return Proposal(
        proposal_id=deterministic_proposal_id(
            source_id=source_id,
            start_ms=start_ms,
            end_ms=end_ms,
            signal_type=signal_type,
            event_hypothesis=event_hypothesis,
            sources=sources,
        ),
        start_ms=start_ms,
        end_ms=end_ms,
        signal_type=signal_type,
        event_hypothesis=event_hypothesis,
        confidence=max(item.confidence for item in cluster),
        sources=list(sources),
        metadata=metadata,
    )


def _routing_bucket(proposal: Proposal, interval_ms: int) -> int:
    midpoint_ms = proposal.start_ms + (proposal.end_ms - proposal.start_ms) // 2
    return midpoint_ms // interval_ms


def _weak_routing_key(proposal: Proposal) -> tuple[int, float, int, str]:
    return (
        -_SIGNAL_PRIORITY[proposal.signal_type],
        -proposal.confidence,
        proposal.start_ms,
        proposal.proposal_id,
    )


def _raw_anchor_count(proposal: Proposal) -> int:
    raw = proposal.metadata.get("cluster_size")
    if raw is None:
        return 1
    try:
        value = int(raw)
    except ValueError:
        return 1
    return max(1, value)


def _merge_metadata(cluster: list[Proposal]) -> dict[str, str]:
    values: dict[str, list[str]] = {}
    for proposal in cluster:
        for key, value in proposal.metadata.items():
            bucket = values.setdefault(key, [])
            if value not in bucket:
                bucket.append(value)
    return {key: " | ".join(items) for key, items in list(values.items())[:30]}


def _ordered(proposals: Iterable[Proposal]) -> list[Proposal]:
    return sorted(
        proposals,
        key=lambda proposal: (
            proposal.start_ms,
            proposal.end_ms,
            -_SIGNAL_PRIORITY[proposal.signal_type],
            proposal.proposal_id,
        ),
    )


def _proposal(
    *,
    source_id: str,
    start_ms: int,
    end_ms: int,
    signal_type: ProposalSignalType,
    source: str,
    event_hypothesis: str | None = None,
    confidence: float = 1.0,
    metadata: dict[str, str] | None = None,
) -> Proposal:
    sources = (source,)
    return Proposal(
        proposal_id=deterministic_proposal_id(
            source_id=source_id,
            start_ms=start_ms,
            end_ms=end_ms,
            signal_type=signal_type,
            event_hypothesis=event_hypothesis,
            sources=sources,
        ),
        start_ms=start_ms,
        end_ms=end_ms,
        signal_type=signal_type,
        event_hypothesis=event_hypothesis,
        confidence=confidence,
        sources=list(sources),
        metadata=metadata or {},
    )


__all__ = [
    "DEFAULT_CLUSTER_GAP_MS",
    "DEFAULT_MAX_CLUSTER_SPAN_MS",
    "DEFAULT_MAX_PROPOSALS",
    "DEFAULT_MAX_PROPOSAL_INTERVAL_MS",
    "ROUTING_POLICY_VERSION",
    "combine_and_cluster_proposals",
    "deterministic_proposal_id",
    "proposals_from_local_signals",
    "proposals_from_manual_markers",
    "proposals_from_transcript",
    "route_proposals",
    "selected_proposal_artifact",
    "summarize_proposals",
]
