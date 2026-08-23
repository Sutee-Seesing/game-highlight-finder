from __future__ import annotations

from pathlib import Path

import pytest

from game_highlight_finder.config import AppConfig
from game_highlight_finder.domain.canonical import canonicalize_scout_response
from game_highlight_finder.domain.reconcile import derive_clip_boundaries, reconcile_session_maps
from game_highlight_finder.domain.windows import plan_scout_windows
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.media.ffmpeg import (
    build_extraction_command,
    build_thumbnail_command,
    build_window_proxy_command,
)
from game_highlight_finder.pipeline.windowed_scout import FakeWindowScout, build_window_prompt


def _window_response(
    *, duration_ms: int, start_ms: int, end_ms: int, event_start: int, event_end: int
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_duration_ms": duration_ms,
        "time_basis": "window_relative",
        "window_start_ms": start_ms,
        "window_end_ms": end_ms,
        "matches": [
            {
                "start_ms": 0,
                "end_ms": end_ms - start_ms,
                "confidence": 0.8,
                "label": "same round",
                "ordinal": 0,
                "evidence": [],
                "candidates": [],
            }
        ],
        "candidates": [
            {
                "start_ms": event_start - start_ms,
                "end_ms": event_end - start_ms,
                "category": "CLUTCH",
                "score": 8.0,
                "confidence": 0.9,
                "reason": "same event across overlapping windows",
                "evidence": [],
                "match_index": 0,
            }
        ],
        "warnings": [],
        "metadata": {"backend": "fake-window"},
    }


def test_window_prompt_v17_is_detection_first_and_keeps_safe_compact_output() -> None:
    source_id = "src_" + "a" * 16
    window = plan_scout_windows(20_000, session_id="session", source_id=source_id).windows[0]
    prompt = build_window_prompt(
        source_duration_ms=20_000,
        window=window,
        local_signal_summary={"loudness_peak_db": -4.0},
        prompt_version="gemini-scout-window-v17",
    )
    assert "entire supplied video AND audio window" in prompt
    assert "STILL return worthwhile top-level candidates" in prompt
    assert "detection-first pass" in prompt
    assert "chronological coverage sweep through the beginning, middle, and end" in prompt
    assert "then rescan before returning" in prompt
    assert "Concrete gameplay anchors include" in prompt
    assert "Do not use score as an inclusion gate for concrete anchors" in prompt
    assert (
        "Social or audio moments must never replace or crowd out visible gameplay anchors" in prompt
    )
    assert "score is editorial or short-form potential from 0 to 10" in prompt
    assert "confidence is certainty from 0 to 1" in prompt
    assert "Prefer lower score or confidence over omitting a real concrete anchor" in prompt
    assert "Do not emit setup_start_ms or payoff_end_ms" in prompt
    assert "local pre/post-roll adds extra clip context" in prompt
    assert "hints only, not ground truth" in prompt
    assert "audio-heavy and is only a seek hint" in prompt
    assert "Generic laughter, menu interaction, banter, hiding, or searching alone" in prompt
    assert "meaningful visual sequence" in prompt
    assert "first clearly useful reveal, engagement, or setup" in prompt
    assert "immediate shooting, payoff, or outcome" in prompt
    assert "do not replace it with a later round-win or banner-only candidate" in prompt
    assert "not the full source duration" in prompt
    assert "schema_version to exactly 1" in prompt
    assert "top-level candidates array" in prompt
    assert "matches[].candidates array empty" in prompt
    assert "never as one wrapper per highlight" in prompt
    assert "one candidate per distinct story/event" in prompt
    assert "inclusive range 0-20000" in prompt
    assert "window_start_ms=0" in prompt and "window_end_ms=20000" in prompt
    assert "m8-real" not in prompt
    assert "MUST_CATCH" not in prompt
    assert "WORTH_REVIEW" not in prompt
    assert "cal-02" not in prompt
    assert "574000" not in prompt
    assert "590000" not in prompt


def test_window_planner_is_bounded_contiguous_and_deterministic() -> None:
    first = plan_scout_windows(900_001, session_id="session", source_id="src_" + "a" * 16)
    second = plan_scout_windows(900_001, session_id="session", source_id="src_" + "a" * 16)
    assert first.plan_hash == second.plan_hash
    assert len(first.windows) == 4
    assert first.windows[0].source_start_ms == 0
    assert first.windows[-1].source_end_ms == 900_001
    assert [(item.source_start_ms, item.source_end_ms) for item in first.windows] == [
        (0, 300_000),
        (270_000, 570_000),
        (540_000, 840_000),
        (810_000, 900_001),
    ]
    for left, right in zip(first.windows, first.windows[1:], strict=False):
        assert right.source_start_ms == left.source_end_ms - 30_000
        assert left.duration_ms <= 300_000


@pytest.mark.parametrize("overlap", [-1, 300_000])
def test_window_planner_rejects_invalid_overlap(overlap: int) -> None:
    with pytest.raises(ValidationError):
        plan_scout_windows(100_000, overlap_ms=overlap)


def test_window_relative_canonicalization_preserves_absolute_event() -> None:
    payload = _window_response(
        duration_ms=20_000,
        start_ms=10_000,
        end_ms=20_000,
        event_start=12_000,
        event_end=13_000,
    )
    session_map = canonicalize_scout_response(
        payload,
        session_id="session",
        source_id="src_" + "a" * 16,
        source_duration_ms=20_000,
        source_window_id="scout_window_" + "b" * 16,
    )
    assert session_map.candidates[0].event_start_ms == 12_000
    assert session_map.candidates[0].event_end_ms == 13_000
    assert session_map.candidates[0].source_window_ids == ["scout_window_" + "b" * 16]


def test_window_relative_canonicalization_drops_timestamps_outside_requested_window() -> None:
    payload = _window_response(
        duration_ms=1_800_000,
        start_ms=0,
        end_ms=600_000,
        event_start=905_000,
        event_end=922_000,
    )
    payload["candidates"].append(
        {
            "start_ms": 10_000,
            "end_ms": 12_000,
            "category": "CLUTCH",
            "score": 8.0,
            "confidence": 0.9,
            "reason": "valid in-window candidate",
            "evidence": [],
        }
    )
    session_map = canonicalize_scout_response(
        payload,
        session_id="session",
        source_id="src_" + "a" * 16,
        source_duration_ms=1_800_000,
        source_window_id="scout_window_" + "b" * 16,
        source_window_start_ms=0,
        source_window_end_ms=600_000,
    )
    assert len(session_map.candidates) == 1
    assert all(item.event_end_ms <= 600_000 for item in session_map.candidates)
    assert "dropped out-of-window candidate fragment at index 0" in session_map.warnings


def test_window_relative_canonicalization_rejects_mismatched_requested_bounds() -> None:
    payload = _window_response(
        duration_ms=20_000,
        start_ms=10_000,
        end_ms=20_000,
        event_start=12_000,
        event_end=13_000,
    )
    with pytest.raises(ValidationError, match="do not match"):
        canonicalize_scout_response(
            payload,
            session_id="session",
            source_id="src_" + "a" * 16,
            source_duration_ms=20_000,
            source_window_id="scout_window_" + "b" * 16,
            source_window_start_ms=0,
            source_window_end_ms=10_000,
        )


def test_reconcile_stitches_overlap_and_deduplicates_candidate() -> None:
    source_id = "src_" + "a" * 16
    plan = plan_scout_windows(
        20_000, max_duration_ms=15_000, overlap_ms=5_000, session_id="session", source_id=source_id
    )
    maps = []
    for window in plan.windows:
        payload = _window_response(
            duration_ms=20_000,
            start_ms=window.source_start_ms,
            end_ms=window.source_end_ms,
            event_start=12_000,
            event_end=13_000,
        )
        maps.append(
            (
                window,
                canonicalize_scout_response(
                    payload,
                    session_id="session",
                    source_id=source_id,
                    source_duration_ms=20_000,
                    source_window_id=window.window_id,
                ),
            )
        )
    reconciled = reconcile_session_maps("session", source_id, 20_000, maps)
    assert len(reconciled.candidates) == 1
    assert len(reconciled.matches) == 1
    assert len(reconciled.candidates[0].source_window_ids) == 2
    assert reconciled.matches[0].candidate_ids == [reconciled.candidates[0].candidate_id]


def test_clip_boundaries_apply_pre_post_roll_and_bounds() -> None:
    source_id = "src_" + "a" * 16
    window = plan_scout_windows(10_000, session_id="session", source_id=source_id).windows[0]
    payload = _window_response(
        duration_ms=10_000,
        start_ms=0,
        end_ms=10_000,
        event_start=500,
        event_end=700,
    )
    session_map = canonicalize_scout_response(
        payload,
        session_id="session",
        source_id=source_id,
        source_duration_ms=10_000,
        source_window_id=window.window_id,
    )
    config = AppConfig().media.extraction.model_copy(
        update={"pre_roll_seconds": 1, "post_roll_seconds": 1}
    )
    bounded = derive_clip_boundaries(session_map, 10_000, config)
    candidate = bounded.candidates[0]
    assert candidate.clip_start_ms == 0
    assert candidate.clip_end_ms == 1_700


def test_command_builders_use_integer_seconds_and_never_shell() -> None:
    class Extraction:
        mode = "accurate"
        video_codec = "libx264"
        crf = 18
        preset = "medium"
        audio_codec = "aac"

    window = build_window_proxy_command(
        Path("ffmpeg"),
        Path("analysis proxy.mp4"),
        Path("window.mp4"),
        proxy_start_ms=1_001,
        duration_ms=2_003,
        has_audio=True,
    )
    accurate = build_extraction_command(
        Path("ffmpeg"),
        Path("raw source.mp4"),
        Path("candidate.mp4"),
        start_ms=1_001,
        end_ms=3_004,
        extraction=Extraction(),
        has_audio=False,
    )
    accurate_whole_seconds = build_extraction_command(
        Path("ffmpeg"),
        Path("raw source.mp4"),
        Path("candidate-whole-seconds.mp4"),
        start_ms=535_000,
        end_ms=555_000,
        extraction=Extraction(),
        has_audio=False,
    )
    thumbnail = build_thumbnail_command(
        Path("ffmpeg"), Path("candidate.mp4"), Path("thumb.jpg"), at_ms=1_001
    )
    assert "1.001" in window and "2.003" in window
    assert "1.001" in accurate and "2.003" in accurate
    assert accurate_whole_seconds[accurate_whole_seconds.index("-t") + 1] == "20"
    assert "-c:v" in accurate and "libx264" in accurate
    assert "-frames:v" in thumbnail and "1.001" in thumbnail
    assert all(isinstance(arg, str) for arg in accurate)


def test_nvenc_window_and_accurate_extraction_defaults_are_gpu_first() -> None:
    config = AppConfig()
    window = build_window_proxy_command(
        Path("ffmpeg"),
        Path("analysis.mp4"),
        Path("window.mp4"),
        proxy_start_ms=0,
        duration_ms=2_000,
        has_audio=True,
        video_codec=config.media.proxy.video_codec,
        preset=config.media.proxy.preset,
    )
    extraction = build_extraction_command(
        Path("ffmpeg"),
        Path("source.mp4"),
        Path("candidate.mp4"),
        start_ms=0,
        end_ms=2_000,
        extraction=config.media.extraction,
        has_audio=True,
    )
    assert "h264_nvenc" in window and "p4" in window
    assert "h264_nvenc" in extraction and "p5" in extraction
    assert "-rc" in extraction and "vbr" in extraction
    assert "-cq" in extraction and "18" in extraction
    assert "-b:v" in extraction and "0" in extraction
    assert "-crf" not in extraction


def test_accurate_extraction_retains_explicit_libx264_fallback() -> None:
    config = AppConfig()
    extraction_config = config.media.extraction.model_copy(
        update={"video_codec": "libx264", "preset": "medium"}
    )
    command = build_extraction_command(
        Path("ffmpeg"),
        Path("source.mp4"),
        Path("candidate.mp4"),
        start_ms=0,
        end_ms=2_000,
        extraction=extraction_config,
        has_audio=False,
    )
    assert "libx264" in command
    assert "-crf" in command and "18" in command
    assert "-cq" not in command


def test_fake_window_scout_is_observable_and_deterministic() -> None:
    source_id = "src_" + "a" * 16
    window = plan_scout_windows(5_000, session_id="session", source_id=source_id).windows[0]
    provider = FakeWindowScout()
    one = provider.generate(
        window=window, source_duration_ms=5_000, source_sha256="a" * 64, summary={}
    )
    two = provider.generate(
        window=window, source_duration_ms=5_000, source_sha256="a" * 64, summary={}
    )
    assert one == two
    assert provider.calls == [window.window_id, window.window_id]
