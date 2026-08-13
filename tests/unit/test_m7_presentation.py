from __future__ import annotations

from datetime import UTC, datetime

from game_highlight_finder.domain.models import Candidate, SessionMap
from game_highlight_finder.pipeline.ranking import rank_session_map
from game_highlight_finder.pipeline.report import _esc, review_duration_ms


def _candidate(candidate_id: str, *, score: float, confidence: float, start: int) -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        category="FUNNY",
        event_start_ms=start,
        event_end_ms=start + 1_000,
        score=score,
        confidence=confidence,
        reason="deterministic fixture",
        clip_start_ms=start,
        clip_end_ms=start + 2_000,
    )


def _map(*candidates: Candidate) -> SessionMap:
    return SessionMap(
        created_at=datetime(2026, 8, 13, tzinfo=UTC),
        producer_version="test",
        canonicalization_version="test-v1",
        session_id="2026-08-13_unknown_aaaaaaaaaaaa",
        source_id="src_aaaaaaaaaaaaaaaa",
        duration_ms=60_000,
        candidates=list(candidates),
    )


def test_ranking_is_stable_and_keeps_all_candidates() -> None:
    session_map = _map(
        _candidate("cand_0000000000000002", score=8, confidence=0.9, start=2_000),
        _candidate("cand_0000000000000001", score=8, confidence=0.9, start=1_000),
        _candidate("cand_0000000000000003", score=7, confidence=1, start=0),
        _candidate("cand_0000000000000004", score=6, confidence=1, start=0),
    )
    before = session_map.model_dump(mode="json")
    artifact = rank_session_map(session_map, best_of_limit=3)

    assert artifact.ordered_candidate_ids == [
        "cand_0000000000000001",
        "cand_0000000000000002",
        "cand_0000000000000003",
        "cand_0000000000000004",
    ]
    assert artifact.best_of_candidate_ids == artifact.ordered_candidate_ids[:3]
    assert session_map.model_dump(mode="json") == before


def test_review_duration_uses_integer_union() -> None:
    candidates = (
        _candidate("cand_0000000000000001", score=1, confidence=1, start=10_000),
        _candidate("cand_0000000000000002", score=1, confidence=1, start=15_000),
        _candidate("cand_0000000000000003", score=1, confidence=1, start=30_000),
    )
    assert review_duration_ms(candidates) == 6_000


def test_empty_ranking_is_valid() -> None:
    artifact = rank_session_map(_map(), best_of_limit=3)
    assert artifact.candidate_count == 0
    assert artifact.ordered_candidate_ids == []
    assert artifact.best_of_candidate_ids == []


def test_report_text_is_escaped_before_html_rendering() -> None:
    escaped = _esc('</script><script>alert(1)</script> "quoted"')
    assert "<script>" not in escaped
    assert "&lt;/script&gt;" in escaped
    assert "&quot;quoted&quot;" in escaped
