from __future__ import annotations

from pathlib import Path

import pytest

from game_highlight_finder.media.ffmpeg import build_motion_signal_command
from game_highlight_finder.pipeline.hybrid_proposals import (
    HybridProposalPolicy,
    parse_motion_activity,
    percentile_ranks,
    plan_hybrid_proposals,
)


def test_motion_signal_command_is_local_bounded_and_video_only() -> None:
    command = build_motion_signal_command(
        Path("ffmpeg"), Path("analysis_proxy.mp4"), sample_fps=4, width=320
    )

    assert command[0] == "ffmpeg"
    assert "analysis_proxy.mp4" in command
    assert "fps=4" in command[command.index("-vf") + 1]
    assert "signalstats" in command[command.index("-vf") + 1]
    assert "lavfi.signalstats.YDIF" in command[command.index("-vf") + 1]
    assert "-an" in command
    assert command[-1] == "-"
    with pytest.raises(ValueError):
        build_motion_signal_command(Path("ffmpeg"), Path("proxy.mp4"), sample_fps=0)


def test_parse_motion_activity_uses_one_second_peak_buckets() -> None:
    text = """
    [metadata] frame:0 pts:0 pts_time:0
    [metadata] lavfi.signalstats.YDIF=0
    [metadata] frame:1 pts:1 pts_time:0.25
    [metadata] lavfi.signalstats.YDIF=12.5
    [metadata] frame:2 pts:2 pts_time:0.75
    [metadata] lavfi.signalstats.YDIF=19.5
    [metadata] frame:3 pts:3 pts_time:1.25
    [metadata] lavfi.signalstats.YDIF=7.0
    """

    assert parse_motion_activity(text, source_duration_ms=2_000) == {0: 19.5, 1: 7.0}


def test_percentile_ranks_are_source_local_and_deterministic() -> None:
    values = {0: -30.0, 1: -10.0, 2: -20.0, 3: -10.0}

    first = percentile_ranks(values)
    second = percentile_ranks(values)

    assert first == second
    assert first[0] == 0.25
    assert first[2] == 0.5
    assert first[1] == 1.0
    assert first[3] == 1.0


def test_hybrid_plan_uses_audio_wide_net_then_fused_rescue_without_semantic_labels() -> None:
    policy = HybridProposalPolicy(
        audio_anchors_per_10min=6,
        fused_anchors_per_10min=2,
        nms_gap_ms=5_000,
        pre_roll_ms=8_000,
        post_roll_ms=12_000,
        merge_gap_ms=2_500,
    )
    audio_scores = {
        30: 10.0,
        90: 9.0,
        150: 8.0,
        210: 7.0,
        270: 6.0,
        330: 5.0,
        390: 1.0,
        450: 0.5,
    }
    motion_scores = {
        30: 1.0,
        90: 1.0,
        150: 1.0,
        210: 1.0,
        270: 1.0,
        330: 1.0,
        390: 20.0,
        450: 19.0,
    }

    first = plan_hybrid_proposals(
        session_id="2026-01-01_unknown_aaaaaaaaaaaa",
        source_id="src_" + "b" * 16,
        source_duration_ms=600_000,
        parent_proxy_sha256="c" * 64,
        local_signals_sha256="d" * 64,
        audio_scores=audio_scores,
        motion_scores=motion_scores,
        policy=policy,
    )
    second = plan_hybrid_proposals(
        session_id="2026-01-01_unknown_aaaaaaaaaaaa",
        source_id="src_" + "b" * 16,
        source_duration_ms=600_000,
        parent_proxy_sha256="c" * 64,
        local_signals_sha256="d" * 64,
        audio_scores=audio_scores,
        motion_scores=motion_scores,
        policy=policy,
    )

    assert first == second
    assert first.plan_hash == second.plan_hash
    assert len(first.anchors) == 8
    assert sum(item.selected_by == "AUDIO" for item in first.anchors) == 6
    assert sum(item.selected_by == "FUSED" for item in first.anchors) == 2
    assert {item.anchor_ms for item in first.anchors} == {
        30_000,
        90_000,
        150_000,
        210_000,
        270_000,
        330_000,
        390_000,
        450_000,
    }
    assert first.semantic_labels_inferred is False
    assert first.provider_calls == 0
    assert 0 < first.proposal_ratio < 0.5
    assert all(item.end_ms <= first.source_duration_ms for item in first.proposals)


def test_hybrid_plan_fails_open_for_proposal_recall_when_one_modality_is_missing() -> None:
    policy = HybridProposalPolicy(audio_anchors_per_10min=2, fused_anchors_per_10min=1)
    plan = plan_hybrid_proposals(
        session_id="2026-01-01_unknown_aaaaaaaaaaaa",
        source_id="src_" + "e" * 16,
        source_duration_ms=600_000,
        parent_proxy_sha256="f" * 64,
        local_signals_sha256="a" * 64,
        audio_scores={},
        motion_scores={10: 1.0, 100: 2.0, 200: 3.0, 300: 4.0},
        policy=policy,
    )

    assert len(plan.anchors) == 3
    assert all(item.selected_by == "MOTION_FALLBACK" for item in plan.anchors)
    assert plan.provider_calls == 0


@pytest.mark.parametrize(
    "boundary_ms",
    [60_000, 120_000, 180_000, 240_000, 300_000, 360_000, 420_000, 480_000, 540_000],
)
def test_event_context_is_not_cut_by_internal_analysis_boundaries(boundary_ms: int) -> None:
    policy = HybridProposalPolicy(
        audio_anchors_per_10min=20,
        fused_anchors_per_10min=0,
        nms_gap_ms=0,
        pre_roll_ms=8_000,
        post_roll_ms=12_000,
        merge_gap_ms=2_500,
        max_proposal_duration_ms=60_000,
        split_overlap_ms=10_000,
    )
    plan = plan_hybrid_proposals(
        session_id="2026-01-01_unknown_aaaaaaaaaaaa",
        source_id="src_" + "1" * 16,
        source_duration_ms=600_000,
        parent_proxy_sha256="2" * 64,
        local_signals_sha256="3" * 64,
        audio_scores={boundary_ms // 1_000: 1.0},
        motion_scores={},
        policy=policy,
    )

    event_start_ms = boundary_ms - 7_000
    event_end_ms = boundary_ms + 9_000
    assert any(
        proposal.start_ms <= event_start_ms and proposal.end_ms >= event_end_ms
        for proposal in plan.proposals
    )


def test_long_anchor_cluster_splits_with_overlap_without_cutting_anchor_context() -> None:
    policy = HybridProposalPolicy(
        audio_anchors_per_10min=40,
        fused_anchors_per_10min=0,
        nms_gap_ms=0,
        pre_roll_ms=8_000,
        post_roll_ms=12_000,
        merge_gap_ms=2_500,
        max_proposal_duration_ms=45_000,
        split_overlap_ms=10_000,
    )
    anchor_seconds = [20, 40, 60, 80, 100, 120, 140, 160]
    plan = plan_hybrid_proposals(
        session_id="2026-01-01_unknown_aaaaaaaaaaaa",
        source_id="src_" + "4" * 16,
        source_duration_ms=180_000,
        parent_proxy_sha256="5" * 64,
        local_signals_sha256="6" * 64,
        audio_scores={second: float(200 - second) for second in anchor_seconds},
        motion_scores={},
        policy=policy,
    )

    assert len(plan.proposals) > 1
    assert all(
        proposal.end_ms - proposal.start_ms <= policy.max_proposal_duration_ms
        for proposal in plan.proposals
    )
    assert all(
        right.start_ms < left.end_ms
        for left, right in zip(plan.proposals, plan.proposals[1:], strict=False)
    )
    for anchor in plan.anchors:
        expected_start = max(0, anchor.anchor_ms - policy.pre_roll_ms)
        expected_end = min(plan.source_duration_ms, anchor.anchor_ms + policy.post_roll_ms)
        assert any(
            proposal.start_ms <= expected_start and proposal.end_ms >= expected_end
            for proposal in plan.proposals
        )
