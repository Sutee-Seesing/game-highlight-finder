from pathlib import Path

import pytest

from game_highlight_finder.domain.models import Candidate
from game_highlight_finder.media.ffmpeg import build_slow_motion_proxy_command
from game_highlight_finder.pipeline.boundary_refinement import (
    BoundaryRefinementResponse,
    apply_boundary_refinement,
    boundary_refinement_schema,
    build_boundary_refinement_prompt,
    plan_boundary_refinement,
    refined_interval_in_source,
)


def _candidate() -> Candidate:
    return Candidate(
        candidate_id="cand_0123456789abcdef",
        category="SKILL",
        event_start_ms=133_000,
        event_end_ms=141_000,
        score=6.5,
        confidence=0.9,
        reason="Leer, kill, then Devour",
    )


def test_boundary_plan_wraps_anchor_and_slows_time() -> None:
    plan = plan_boundary_refinement(_candidate(), 600_000)
    assert (plan.source_start_ms, plan.source_end_ms) == (113_000, 151_000)
    assert plan.source_duration_ms == 38_000
    assert plan.proxy_duration_ms == 76_000
    assert (plan.anchor_proxy_start_ms, plan.anchor_proxy_end_ms) == (40_000, 56_000)


def test_boundary_schema_is_strict_and_bounded_to_proxy() -> None:
    plan = plan_boundary_refinement(_candidate(), 600_000)
    schema = boundary_refinement_schema(plan)
    assert schema["additionalProperties"] is False
    properties = schema["properties"]
    assert properties["event_start_ms"]["maximum"] == 76_000
    assert properties["event_end_ms"]["maximum"] == 76_000
    assert properties["confidence"] == {"type": "number", "minimum": 0, "maximum": 1}


def test_boundary_mapping_can_recover_earlier_core_event() -> None:
    candidate = _candidate()
    plan = plan_boundary_refinement(candidate, 600_000)
    response = BoundaryRefinementResponse(
        status="REFINED",
        event_start_ms=14_000,
        event_end_ms=46_000,
        confidence=0.9,
        reason="same fight",
    )
    assert refined_interval_in_source(plan, response) == (120_000, 136_000)
    refined = apply_boundary_refinement(candidate, plan, response)
    assert (refined.event_start_ms, refined.event_end_ms) == (120_000, 136_000)
    assert refined.clip_start_ms is None and refined.clip_end_ms is None
    assert refined.metadata["boundary_refinement"] == "boundary-refiner-v1"


def test_boundary_refinement_rejects_drift_to_adjacent_event() -> None:
    candidate = _candidate()
    plan = plan_boundary_refinement(candidate, 600_000)
    response = BoundaryRefinementResponse(
        status="REFINED",
        event_start_ms=0,
        event_end_ms=10_000,
        confidence=0.9,
        reason="different event",
    )
    with pytest.raises(ValueError, match="overlap"):
        apply_boundary_refinement(candidate, plan, response)


def test_uncertain_boundary_does_not_mutate_candidate() -> None:
    candidate = _candidate()
    plan = plan_boundary_refinement(candidate, 600_000)
    response = BoundaryRefinementResponse(
        status="UNCERTAIN",
        event_start_ms=20_000,
        event_end_ms=50_000,
        confidence=0.4,
        reason="unclear",
    )
    assert apply_boundary_refinement(candidate, plan, response) == candidate


def test_boundary_prompt_identifies_slowed_anchor_without_benchmark_labels() -> None:
    candidate = _candidate()
    plan = plan_boundary_refinement(candidate, 600_000)
    prompt = build_boundary_refinement_prompt(plan, candidate)
    assert "slowed 2x" in prompt
    assert "40000-56000 ms" in prompt
    assert "SAME gameplay event" in prompt
    assert "ground truth" not in prompt.lower()


def test_slow_motion_command_preserves_audio_and_never_uses_shell() -> None:
    command = build_slow_motion_proxy_command(
        Path("ffmpeg"), Path("context.mp4"), Path("slow.mp4"), slowdown_factor=4, has_audio=True
    )
    assert command[0] == "ffmpeg"
    assert "-vf" in command and "setpts=4*(PTS-STARTPTS)" in command
    assert "-af" in command and "asetpts=PTS-STARTPTS,atempo=0.5,atempo=0.5" in command
    assert "shell" not in " ".join(command).lower()
