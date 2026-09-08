from __future__ import annotations

# ruff: noqa: E501
from datetime import UTC, datetime
from pathlib import Path

from game_highlight_finder import __version__
from game_highlight_finder.config import AppConfig, ExtractionConfig
from game_highlight_finder.domain.canonical import canonicalize_scout_response
from game_highlight_finder.domain.models import (
    Rational,
    SourceAsset,
    VideoStream,
)
from game_highlight_finder.domain.reconcile import derive_clip_boundaries, reconcile_session_maps
from game_highlight_finder.domain.windows import plan_scout_windows
from game_highlight_finder.pipeline.extraction import ExtractionManifest, ExtractionRecord
from game_highlight_finder.pipeline.manifest import new_manifest
from game_highlight_finder.pipeline.ranking import rank_session_map
from game_highlight_finder.pipeline.report import render_report
from game_highlight_finder.storage.hashing import hash_file
from game_highlight_finder.storage.sessions import load_manifest, session_paths

SOURCE_ID = "src_" + "c" * 16
SESSION_ID = "c1_creator_fixture"
DURATION_MS = 180_000


def _payload(
    *,
    window_start_ms: int,
    window_end_ms: int,
    candidates: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_duration_ms": DURATION_MS,
        "time_basis": "window_relative",
        "window_start_ms": window_start_ms,
        "window_end_ms": window_end_ms,
        "matches": [],
        "candidates": candidates,
        "warnings": [],
        "metadata": {"backend": "c1-offline-fixture"},
    }


def _candidate(
    *,
    start_ms: int,
    end_ms: int,
    category: str,
    score: float,
    confidence: float,
    reason: str,
    moment_summary: str | None = None,
    creator_reason: str | None = None,
    editorial_role: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "start_ms": start_ms,
        "end_ms": end_ms,
        "category": category,
        "score": score,
        "confidence": confidence,
        "reason": reason,
        "evidence": [],
    }
    if moment_summary is not None:
        payload["moment_summary"] = moment_summary
    if creator_reason is not None:
        payload["creator_reason"] = creator_reason
    if editorial_role is not None:
        payload["editorial_role"] = editorial_role
    return payload


def test_c1_multi_archetype_creator_pack_keeps_quiet_visual_social_and_story_moments() -> None:
    plan = plan_scout_windows(
        DURATION_MS,
        max_duration_ms=90_000,
        overlap_ms=0,
        session_id=SESSION_ID,
        source_id=SOURCE_ID,
    )
    first_window, second_window = plan.windows

    first_map = canonicalize_scout_response(
        _payload(
            window_start_ms=0,
            window_end_ms=90_000,
            candidates=[
                _candidate(
                    start_ms=12_000,
                    end_ms=20_000,
                    category="DISCOVERY",
                    score=8.8,
                    confidence=0.92,
                    reason="Clear visual discovery despite quiet audio.",
                    moment_summary="Player silently discovers a hidden cave behind the waterfall.",
                    creator_reason="The reveal is visually understandable and has a clean surprise payoff without dialogue.",
                ),
                _candidate(
                    start_ms=45_000,
                    end_ms=57_000,
                    category="FRIEND_MOMENT",
                    score=9.2,
                    confidence=0.88,
                    reason="Self-contained friend joke and reaction.",
                    moment_summary="A friend confidently gives the wrong instruction and the group immediately realizes it.",
                    creator_reason="The setup, wrong call, and group reaction form a complete joke even without a gameplay win.",
                ),
            ],
        ),
        session_id=SESSION_ID,
        source_id=SOURCE_ID,
        source_duration_ms=DURATION_MS,
        source_window_id=first_window.window_id,
        source_window_start_ms=first_window.source_start_ms,
        source_window_end_ms=first_window.source_end_ms,
    )

    second_map = canonicalize_scout_response(
        _payload(
            window_start_ms=90_000,
            window_end_ms=180_000,
            candidates=[
                _candidate(
                    start_ms=18_000,
                    end_ms=36_000,
                    category="TENSION_PAYOFF",
                    score=8.5,
                    confidence=0.9,
                    reason="Tension resolves into an obvious failure/reaction payoff.",
                    moment_summary="Player carefully escapes danger, relaxes too early, then immediately falls into a trap.",
                    creator_reason="The quiet buildup makes the sudden failure funnier, so the setup must survive editing.",
                )
            ],
        ),
        session_id=SESSION_ID,
        source_id=SOURCE_ID,
        source_duration_ms=DURATION_MS,
        source_window_id=second_window.window_id,
        source_window_start_ms=second_window.source_start_ms,
        source_window_end_ms=second_window.source_end_ms,
    )

    reconciled = reconcile_session_maps(
        SESSION_ID,
        SOURCE_ID,
        DURATION_MS,
        [(first_window, first_map), (second_window, second_map)],
    )
    with_clips = derive_clip_boundaries(reconciled, DURATION_MS, ExtractionConfig())
    ranking = rank_session_map(with_clips, best_of_limit=3)

    assert [candidate.category for candidate in with_clips.candidates] == [
        "DISCOVERY",
        "FRIEND_MOMENT",
        "TENSION_PAYOFF",
    ]
    assert all(candidate.moment_summary for candidate in with_clips.candidates)
    assert all(candidate.creator_reason for candidate in with_clips.candidates)
    assert all(
        candidate.clip_start_ms is not None
        and candidate.clip_end_ms is not None
        and candidate.clip_start_ms <= candidate.event_start_ms
        and candidate.clip_end_ms >= candidate.event_end_ms
        for candidate in with_clips.candidates
    )

    ranked_categories = [
        next(candidate.category for candidate in with_clips.candidates if candidate.candidate_id == candidate_id)
        for candidate_id in ranking.ordered_candidate_ids
    ]
    assert ranked_categories == ["FRIEND_MOMENT", "DISCOVERY", "TENSION_PAYOFF"]
    assert ranking.entries[0].creator_score == 9.2
    assert ranking.entries[0].detection_confidence == 0.88


def test_c1_legacy_candidate_falls_back_to_reason_for_creator_pack_text() -> None:
    window = plan_scout_windows(
        30_000,
        session_id=SESSION_ID,
        source_id=SOURCE_ID,
    ).windows[0]
    legacy_reason = "Legacy Scout explanation remains usable."
    session_map = canonicalize_scout_response(
        {
            "schema_version": 1,
            "source_duration_ms": 30_000,
            "time_basis": "window_relative",
            "window_start_ms": 0,
            "window_end_ms": 30_000,
            "matches": [],
            "candidates": [
                _candidate(
                    start_ms=5_000,
                    end_ms=8_000,
                    category="FUNNY",
                    score=7.0,
                    confidence=0.75,
                    reason=legacy_reason,
                )
            ],
            "warnings": [],
            "metadata": {"backend": "legacy-offline-fixture"},
        },
        session_id=SESSION_ID,
        source_id=SOURCE_ID,
        source_duration_ms=30_000,
        source_window_id=window.window_id,
        source_window_start_ms=0,
        source_window_end_ms=30_000,
    )

    candidate = session_map.candidates[0]
    assert candidate.moment_summary == legacy_reason
    assert candidate.creator_reason == legacy_reason


def test_c1_boring_window_can_return_zero_candidates_without_synthetic_fill() -> None:
    window = plan_scout_windows(
        30_000,
        session_id=SESSION_ID,
        source_id=SOURCE_ID,
    ).windows[0]
    session_map = canonicalize_scout_response(
        {
            "schema_version": 1,
            "source_duration_ms": 30_000,
            "time_basis": "window_relative",
            "window_start_ms": 0,
            "window_end_ms": 30_000,
            "matches": [],
            "candidates": [],
            "warnings": [],
            "metadata": {"backend": "boring-offline-fixture"},
        },
        session_id=SESSION_ID,
        source_id=SOURCE_ID,
        source_duration_ms=30_000,
        source_window_id=window.window_id,
        source_window_start_ms=0,
        source_window_end_ms=30_000,
    )

    assert session_map.candidates == []
    assert rank_session_map(session_map).candidate_count == 0


def test_c1_editorial_role_separates_standalone_montage_and_non_review_items() -> None:
    window = plan_scout_windows(
        DURATION_MS,
        max_duration_ms=60_000,
        overlap_ms=0,
        session_id=SESSION_ID,
        source_id=SOURCE_ID,
    ).windows[0]
    session_map = canonicalize_scout_response(
        _payload(
            window_start_ms=0,
            window_end_ms=60_000,
            candidates=[
                _candidate(
                    start_ms=5_000,
                    end_ms=12_000,
                    category="CLUTCH",
                    score=8.0,
                    confidence=0.9,
                    reason="Round fully resolves after the final elimination.",
                    editorial_role="STANDALONE_STORY",
                ),
                _candidate(
                    start_ms=20_000,
                    end_ms=24_000,
                    category="SKILL",
                    score=9.5,
                    confidence=0.98,
                    reason="Clean mechanical kill without a complete story arc.",
                    editorial_role="MONTAGE_BEAT",
                ),
                _candidate(
                    start_ms=30_000,
                    end_ms=35_000,
                    category="OTHER",
                    score=10.0,
                    confidence=0.99,
                    reason="Real interval but not useful to the creator.",
                    editorial_role="NONE",
                ),
                _candidate(
                    start_ms=40_000,
                    end_ms=45_000,
                    category="OTHER",
                    score=9.0,
                    confidence=0.9,
                    reason="Setup context that belongs to another beat.",
                    editorial_role="CONTEXT_ONLY",
                ),
            ],
        ),
        session_id=SESSION_ID,
        source_id=SOURCE_ID,
        source_duration_ms=DURATION_MS,
        source_window_id=window.window_id,
        source_window_start_ms=0,
        source_window_end_ms=60_000,
    )

    ranking = rank_session_map(session_map)

    assert [candidate.editorial_role.value for candidate in session_map.candidates] == [
        "STANDALONE_STORY",
        "MONTAGE_BEAT",
        "NONE",
        "CONTEXT_ONLY",
    ]
    # A provider-proposed standalone role is no longer enough by itself. Until a
    # separate verifier supplies COMPLETE + VERIFIED/NOT_APPLICABLE state, only
    # the montage beat is creator-review eligible.
    assert ranking.candidate_count == 1
    assert [entry.editorial_role.value for entry in ranking.entries] == ["MONTAGE_BEAT"]
    assert ranking.entries[0].creator_score == 9.5


def test_c1_provider_free_pipeline_renders_creator_pack_from_window_payloads(tmp_path: Path) -> None:
    session_id = "2026-09-07_unknown_c1c1c1c1c1c1"
    source_id = "src_" + "d" * 16
    duration_ms = 180_000
    plan = plan_scout_windows(
        duration_ms,
        max_duration_ms=90_000,
        overlap_ms=0,
        session_id=session_id,
        source_id=source_id,
    )
    first_window, second_window = plan.windows

    first_map = canonicalize_scout_response(
        {
            "schema_version": 1,
            "source_duration_ms": duration_ms,
            "time_basis": "window_relative",
            "window_start_ms": 0,
            "window_end_ms": 90_000,
            "matches": [],
            "candidates": [
                _candidate(
                    start_ms=14_000,
                    end_ms=21_000,
                    category="DISCOVERY",
                    score=9.0,
                    confidence=0.87,
                    reason="Quiet visual reveal.",
                    moment_summary="Player silently finds a hidden passage behind a waterfall.",
                    creator_reason="The reveal is visually clear and gives a compact curiosity-to-payoff story without speech.",
                )
            ],
            "warnings": [],
            "metadata": {"backend": "c1-offline-integration"},
        },
        session_id=session_id,
        source_id=source_id,
        source_duration_ms=duration_ms,
        source_window_id=first_window.window_id,
        source_window_start_ms=first_window.source_start_ms,
        source_window_end_ms=first_window.source_end_ms,
    )
    second_map = canonicalize_scout_response(
        {
            "schema_version": 1,
            "source_duration_ms": duration_ms,
            "time_basis": "window_relative",
            "window_start_ms": 90_000,
            "window_end_ms": 180_000,
            "matches": [],
            "candidates": [
                _candidate(
                    start_ms=12_000,
                    end_ms=20_000,
                    category="FUNNY",
                    score=9.4,
                    confidence=0.9,
                    reason="Self-contained co-op joke.",
                    moment_summary="A teammate gives a confident wrong call and the whole group immediately hits the same trap.",
                    creator_reason="The setup and group reaction form a complete joke even though there is no important gameplay win.",
                )
            ],
            "warnings": [],
            "metadata": {"backend": "c1-offline-integration"},
        },
        session_id=session_id,
        source_id=source_id,
        source_duration_ms=duration_ms,
        source_window_id=second_window.window_id,
        source_window_start_ms=second_window.source_start_ms,
        source_window_end_ms=second_window.source_end_ms,
    )

    reconciled = reconcile_session_maps(
        session_id,
        source_id,
        duration_ms,
        [(first_window, first_map), (second_window, second_map)],
        created_at=datetime(2026, 9, 7, tzinfo=UTC),
    )
    with_clips = derive_clip_boundaries(reconciled, duration_ms, ExtractionConfig())
    ranking = rank_session_map(with_clips)

    source_path = tmp_path / "synthetic-source.mp4"
    source_path.write_bytes(b"provider-free-source-fixture")
    source_stat = source_path.stat()
    source = SourceAsset(
        created_at=datetime(2026, 9, 7, tzinfo=UTC),
        producer_version=__version__,
        source_id=source_id,
        path=source_path.resolve(),
        sha256=hash_file(source_path),
        size_bytes=source_stat.st_size,
        mtime_ns=source_stat.st_mtime_ns,
        duration_ms=duration_ms,
        container="mp4",
        video_stream=VideoStream(
            index=0,
            codec_name="h264",
            width=640,
            height=360,
            average_frame_rate=Rational(numerator=30, denominator=1),
        ),
        selected_video_stream=0,
        probe_version="c1-offline-integration",
    )
    config = AppConfig.model_validate({"storage": {"data_dir": str(tmp_path / "data")}})
    paths = session_paths(config.storage.data_dir, session_id)
    paths.root.mkdir(parents=True)
    paths.source.write_text(source.model_dump_json(), encoding="utf-8")
    paths.session_map.write_text(with_clips.model_dump_json(), encoding="utf-8")

    records: list[ExtractionRecord] = []
    for candidate in with_clips.candidates:
        output = paths.root / "candidates" / f"{candidate.candidate_id}.mp4"
        thumbnail = paths.root / "thumbnails" / f"{candidate.candidate_id}.jpg"
        output.parent.mkdir(parents=True, exist_ok=True)
        thumbnail.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"offline-clip-" + candidate.candidate_id.encode())
        thumbnail.write_bytes(b"offline-thumbnail")
        records.append(
            ExtractionRecord(
                candidate_id=candidate.candidate_id,
                source_id=source_id,
                source_sha256=source.sha256,
                requested_start_ms=candidate.clip_start_ms or 0,
                requested_end_ms=candidate.clip_end_ms or 1,
                mode="accurate",
                accuracy_class="frame-accurate",
                output_path=str(output.relative_to(paths.root)).replace("\\", "/"),
                output_sha256=hash_file(output),
                output_size_bytes=output.stat().st_size,
                thumbnail_path=str(thumbnail.relative_to(paths.root)).replace("\\", "/"),
                thumbnail_sha256=hash_file(thumbnail),
                ffmpeg_identity="offline-fixture",
                config_fingerprint="d" * 64,
                status="COMPLETED",
            )
        )
    extraction = ExtractionManifest(
        created_at=source.created_at,
        updated_at=source.created_at,
        producer_version=__version__,
        session_id=session_id,
        source_id=source_id,
        source_sha256=source.sha256,
        records=tuple(records),
        status="COMPLETED",
    )
    paths.extraction_manifest.write_text(extraction.model_dump_json(), encoding="utf-8")
    paths.manifest.write_text(new_manifest(session_id, now=source.created_at).model_dump_json(), encoding="utf-8")

    result = render_report(paths, source, with_clips, ranking, load_manifest(paths.manifest), config)
    html = result.path.read_text(encoding="utf-8")

    assert result.cache_hit is False
    assert "Creator Candidate Pack" in html
    assert "DISCOVERY" in html and "FUNNY" in html
    assert "Player silently finds a hidden passage behind a waterfall." in html
    assert "The reveal is visually clear and gives a compact curiosity-to-payoff story without speech." in html
    assert "A teammate gives a confident wrong call and the whole group immediately hits the same trap." in html
    assert "The setup and group reaction form a complete joke even though there is no important gameplay win." in html
    assert "What happened:" in html
    assert "Why review this:" in html
    assert "Open Clip" in html
    assert [entry.creator_score for entry in ranking.entries] == [9.4, 9.0]
