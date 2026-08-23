from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from game_highlight_finder.config import AppConfig
from game_highlight_finder.domain.models import Candidate, SessionMap
from game_highlight_finder.pipeline.ranking import (
    RANKING_BASIS,
    RANKING_VERSION,
    load_or_create_ranking,
    rank_session_map,
    ranking_cache_payload,
)
from game_highlight_finder.pipeline.report import _esc, review_duration_ms
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.sessions import session_paths


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
    assert artifact.candidate_count == len(session_map.candidates)
    assert artifact.ranking_version == RANKING_VERSION
    assert artifact.ranking_basis == RANKING_BASIS
    cache_payload = ranking_cache_payload(session_map, best_of_limit=3)
    assert cache_payload["ranking_version"] == RANKING_VERSION
    assert cache_payload["ranking_basis"] == RANKING_BASIS
    first_entry = artifact.entries[0]
    assert first_entry.score == first_entry.short_form_score == 8
    assert first_entry.confidence == first_entry.detection_confidence == 0.9
    assert "short_form_score=8.000000" in first_entry.ranking_key
    assert "detection_confidence=0.900000" in first_entry.ranking_key
    assert session_map.model_dump(mode="json") == before


def test_stale_v1_ranking_is_recreated_safely(tmp_path: Path) -> None:
    session_map = _map(_candidate("cand_0000000000000001", score=8, confidence=0.9, start=1_000))
    config = AppConfig.model_validate({"storage": {"data_dir": str(tmp_path)}})
    paths = session_paths(tmp_path, session_map.session_id)
    current = rank_session_map(session_map, best_of_limit=config.report.best_of_limit)
    legacy = current.model_dump(mode="json")
    legacy["schema_version"] = 1
    legacy["ranking_version"] = "m7-ranking-v1"
    legacy.pop("ranking_basis")
    legacy["cache_key"] = "0" * 64
    for entry in legacy["entries"]:
        entry.pop("short_form_score")
        entry.pop("detection_confidence")
    atomic_write_json(paths.ranking_path, legacy)

    recreated, cache_hit = load_or_create_ranking(paths, session_map, config)
    persisted = read_json(paths.ranking_path)
    assert cache_hit is False
    assert recreated.ranking_version == RANKING_VERSION
    assert recreated.ranking_basis == RANKING_BASIS
    assert persisted["schema_version"] == 2
    assert persisted["ranking_version"] == RANKING_VERSION
    assert persisted["ranking_basis"] == RANKING_BASIS


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
