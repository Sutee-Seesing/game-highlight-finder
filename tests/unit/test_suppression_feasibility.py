from __future__ import annotations

from datetime import UTC, datetime

import pytest

from game_highlight_finder.benchmark.boundary_feasibility import (
    assess_boundary_refinement_feasibility,
)
from game_highlight_finder.benchmark.models import (
    AnnotatedHighlight,
    BenchmarkAnnotations,
    BoringInterval,
    EvaluationPolicy,
    Importance,
    Modality,
)
from game_highlight_finder.benchmark.suppression_feasibility import (
    assess_candidate_suppression_feasibility,
)
from game_highlight_finder.domain.models import (
    AudioActivityInterval,
    Candidate,
    LocalSignalsArtifact,
    SessionMap,
)


def _candidate(candidate_id: str, start_ms: int, end_ms: int, *, score: float) -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        category="SKILL",
        event_start_ms=start_ms,
        event_end_ms=end_ms,
        score=score,
        confidence=0.95,
        reason=f"fixture {candidate_id}",
    )


def _session_map() -> SessionMap:
    return SessionMap(
        created_at=datetime(2026, 8, 29, tzinfo=UTC),
        producer_version="test",
        canonicalization_version="test-v1",
        session_id="2026-08-26_unknown_b5365144c0a4",
        source_id="src_b5365144c0a4a327",
        duration_ms=99_008,
        game_profile="unknown",
        scout_backend="gemini",
        scout_metadata={
            "model": "gemini-3.5-flash-lite",
            "window_prompt_version": "gemini-scout-window-v18",
        },
        candidates=[
            _candidate("cand_357fd964f750ee93", 0, 5_000, score=7.5),
            _candidate("cand_b36f8c078a6e4a22", 23_000, 30_500, score=8.0),
            _candidate("cand_00a30c7a3b46451f", 47_000, 51_000, score=7.5),
            _candidate("cand_e7a545c860337a96", 57_000, 62_000, score=8.0),
        ],
    )


def _annotations() -> BenchmarkAnnotations:
    return BenchmarkAnnotations(
        benchmark_id="external-fps-dev-v1",
        case_id="external-fps-openarena-01",
        source_sha256="a" * 64,
        source_duration_ms=99_008,
        game_profile="openarena",
        highlights=(
            AnnotatedHighlight(
                annotation_id="openarena-frag-000",
                event_start_ms=0,
                event_end_ms=5_000,
                importance=Importance.WORTH_REVIEW,
                modality=Modality.VISUAL,
            ),
            AnnotatedHighlight(
                annotation_id="openarena-frag-001",
                event_start_ms=24_000,
                event_end_ms=26_000,
                importance=Importance.WORTH_REVIEW,
                modality=Modality.VISUAL,
            ),
        ),
        boring_intervals=(
            BoringInterval(
                annotation_id="boring-candidate-3",
                start_ms=47_000,
                end_ms=51_000,
            ),
            BoringInterval(
                annotation_id="boring-candidate-4",
                start_ms=57_000,
                end_ms=62_000,
            ),
        ),
    )


def _boundary(session_map: SessionMap):
    return assess_boundary_refinement_feasibility(
        session_map,
        _annotations(),
        EvaluationPolicy(),
        dataset_sha256="b" * 64,
        annotation_document_sha256="c" * 64,
        annotation_coverage="sparse",
    )


def _signals(*, equal_audio: bool = False) -> LocalSignalsArtifact:
    peaks = (-18.148951, -18.518034, -18.518034, -18.518034) if equal_audio else (
        -18.148951,
        -18.518034,
        -22.539,
        -21.582,
    )
    means = (-26.659, -25.604, -26.0, -25.21) if equal_audio else (
        -26.659,
        -25.604,
        -28.329,
        -25.21,
    )
    spans = ((0, 5_000), (23_000, 30_500), (47_000, 51_000), (57_000, 62_000))
    return LocalSignalsArtifact(
        created_at=datetime(2026, 8, 29, tzinfo=UTC),
        producer_version="test",
        source_duration_ms=99_008,
        audio_present=True,
        overall_loudness_lufs=-29.2,
        audio_activity=[
            AudioActivityInterval(
                start_ms=start,
                end_ms=end,
                mean_db=mean,
                peak_db=peak,
                active=True,
            )
            for (start, end), mean, peak in zip(spans, means, peaks, strict=True)
        ],
    )


def test_audio_peak_diagnostic_separates_reviewed_openarena_candidates() -> None:
    session_map = _session_map()
    boundary = _boundary(session_map)
    assert boundary.false_positive_suppression_safe is True
    assert boundary.score_confidence_threshold_suppression_headroom is False

    result = assess_candidate_suppression_feasibility(
        session_map,
        _signals(),
        boundary,
        boundary_feasibility_sha256="d" * 64,
    )

    assert result.protected_positive_count == 2
    assert result.confirmed_negative_count == 2
    assert result.score_confidence_threshold_suppression_headroom is False
    assert result.protected_positive_min_audio_peak_db == pytest.approx(-18.518034)
    assert result.audio_peak_db_threshold_rejectable_negative_candidate_ids == (
        "cand_00a30c7a3b46451f",
        "cand_e7a545c860337a96",
    )
    assert result.audio_peak_db_threshold_suppression_headroom is True
    assert result.protected_positive_min_audio_peak_over_loudness_db == pytest.approx(10.681966)
    assert result.audio_peak_over_loudness_threshold_rejectable_negative_candidate_ids == (
        "cand_00a30c7a3b46451f",
        "cand_e7a545c860337a96",
    )
    assert result.audio_peak_over_loudness_threshold_suppression_headroom is True
    assert result.protected_positive_min_audio_mean_db == pytest.approx(-26.659)
    assert result.audio_mean_db_threshold_rejectable_negative_candidate_ids == (
        "cand_00a30c7a3b46451f",
    )
    assert result.audio_mean_db_threshold_suppression_headroom is True
    assert result.diagnostic_verdict == "AUDIO_PEAK_OVER_LOUDNESS_HEADROOM"
    assert result.provider_calls == 0


def test_relative_peak_diagnostic_is_not_applicable_without_overall_loudness() -> None:
    session_map = _session_map()
    signals = _signals().model_copy(update={"overall_loudness_lufs": None})
    result = assess_candidate_suppression_feasibility(
        session_map,
        signals,
        _boundary(session_map),
        boundary_feasibility_sha256="d" * 64,
    )

    assert result.protected_positive_min_audio_peak_over_loudness_db is None
    assert result.audio_peak_over_loudness_threshold_rejectable_negative_candidate_ids == ()
    assert result.audio_peak_over_loudness_threshold_suppression_headroom is False
    assert result.audio_peak_db_threshold_suppression_headroom is True
    assert result.diagnostic_verdict == "AUDIO_PEAK_DB_HEADROOM"


def test_audio_diagnostic_reports_no_headroom_when_negatives_match_positive_floor() -> None:
    session_map = _session_map()
    result = assess_candidate_suppression_feasibility(
        session_map,
        _signals(equal_audio=True),
        _boundary(session_map),
        boundary_feasibility_sha256="d" * 64,
    )

    assert result.audio_peak_db_threshold_rejectable_negative_candidate_ids == ()
    assert result.audio_peak_db_threshold_suppression_headroom is False
    assert result.audio_peak_over_loudness_threshold_rejectable_negative_candidate_ids == ()
    assert result.audio_peak_over_loudness_threshold_suppression_headroom is False
    assert result.audio_mean_db_threshold_rejectable_negative_candidate_ids == ()
    assert result.audio_mean_db_threshold_suppression_headroom is False
    assert result.diagnostic_verdict == "NO_EXISTING_LOCAL_SIGNAL_HEADROOM"
