from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pydantic import ValidationError as PydanticValidationError

from game_highlight_finder.domain.canonical import (
    MAX_SCOUT_RESPONSE_BYTES,
    canonicalize_scout_response,
    deterministic_candidate_id,
    deterministic_match_id,
    normalize_interval,
    parse_scout_response,
)
from game_highlight_finder.domain.models import (
    Candidate,
    CandidateCategory,
    Match,
    SessionMap,
)
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.storage.schemas import m3_schema_snapshots


def _response(*, duration_ms: int = 10_000) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_duration_ms": duration_ms,
        "time_basis": "source_relative",
        "matches": [
            {
                "start_ms": 0,
                "end_ms": duration_ms,
                "confidence": 0.8,
                "ordinal": 0,
                "candidates": [
                    {
                        "start_ms": 2_000,
                        "end_ms": 3_000,
                        "category": "CLUTCH",
                        "score": 8.5,
                        "confidence": 0.9,
                        "reason": "A concise event explanation.",
                        "provider_id": "provider-arbitrary-id",
                        "evidence": [
                            {
                                "type": "audio spike",
                                "start_ms": 2_000,
                                "end_ms": 2_500,
                                "strength": 0.8,
                                "summary": "Activity rises around the event.",
                            }
                        ],
                    }
                ],
            }
        ],
        "warnings": [],
        "metadata": {"backend": "fake"},
    }


def test_valid_canonical_models_keep_score_and_confidence_separate() -> None:
    candidate = Candidate(
        candidate_id="cand_" + "a" * 16,
        category=CandidateCategory.CLUTCH,
        event_start_ms=100,
        event_end_ms=200,
        score=8.0,
        confidence=0.4,
        reason="Useful evidence.",
    )
    match = Match(
        match_id="match_" + "b" * 16,
        start_ms=0,
        end_ms=1_000,
        confidence=0.5,
        candidate_ids=[candidate.candidate_id],
    )
    assert candidate.score == 8.0
    assert candidate.confidence == 0.4
    assert match.candidate_ids == [candidate.candidate_id]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"event_start_ms": 200, "event_end_ms": 200},
        {"event_start_ms": True, "event_end_ms": 300},
        {"event_start_ms": 0, "event_end_ms": 300, "score": 11.0},
        {"event_start_ms": 0, "event_end_ms": 300, "confidence": float("nan")},
        {"event_start_ms": 0, "event_end_ms": 300, "category": "NOT_A_CATEGORY"},
    ],
)
def test_candidate_rejects_invalid_intervals_types_and_categories(
    kwargs: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "candidate_id": "cand_" + "a" * 16,
        "category": "OTHER",
        "event_start_ms": 100,
        "event_end_ms": 300,
        "score": 5.0,
        "confidence": 0.5,
        "reason": "Reason",
    }
    values.update(kwargs)
    with pytest.raises(PydanticValidationError):
        Candidate(**values)  # type: ignore[arg-type]


def test_interval_normalization_is_half_open_and_only_clamps_small_end_error() -> None:
    assert normalize_interval(100, 1_020, duration_ms=1_000) == (
        100,
        1_000,
        ["clamped interval.end_ms to source duration"],
    )
    with pytest.raises(ValidationError):
        normalize_interval(-1, 100, duration_ms=1_000)
    with pytest.raises(ValidationError):
        normalize_interval(100, 101, duration_ms=1_000, max_duration_ms=0)
    with pytest.raises(ValidationError):
        normalize_interval(900, 800, duration_ms=1_000)


def test_deterministic_ids_are_stable_and_semantic_changes_change_candidate_id() -> None:
    first = deterministic_match_id(session_id="session", start_ms=0, end_ms=100, ordinal=0)
    second = deterministic_match_id(session_id="session", start_ms=0, end_ms=100, ordinal=0)
    assert first == second
    base = deterministic_candidate_id(
        session_id="session",
        match_id=first,
        start_ms=10,
        end_ms=20,
        category="CLUTCH",
    )
    assert base == deterministic_candidate_id(
        session_id="session",
        match_id=first,
        start_ms=10,
        end_ms=20,
        category="CLUTCH",
    )
    assert base != deterministic_candidate_id(
        session_id="session",
        match_id=first,
        start_ms=11,
        end_ms=20,
        category="CLUTCH",
    )


def test_canonicalization_replaces_provider_ids_and_preserves_zero_match() -> None:
    result = canonicalize_scout_response(
        _response(),
        session_id="2026-08-12_unknown_aaaaaaaaaaaa",
        source_id="src_" + "a" * 16,
        source_duration_ms=10_000,
    )
    assert len(result.matches) == 1
    assert len(result.candidates) == 1
    assert result.matches[0].candidate_ids == [result.candidates[0].candidate_id]
    assert result.candidates[0].candidate_id != "provider-arbitrary-id"
    assert "ignored provider-supplied candidate ID" in result.candidates[0].normalization_actions


def test_window_relative_timestamps_are_normalized_once() -> None:
    payload = _response(duration_ms=10_000)
    payload["time_basis"] = "window_relative"
    payload["window_start_ms"] = 1_000
    match = cast(dict[str, Any], cast(list[Any], payload["matches"])[0])
    match["start_ms"] = 0
    match["end_ms"] = 2_000
    candidate = cast(dict[str, Any], cast(list[Any], match["candidates"])[0])
    candidate["start_ms"] = 500
    candidate["end_ms"] = 1_000
    result = canonicalize_scout_response(
        payload,
        session_id="session",
        source_id="src_" + "a" * 16,
        source_duration_ms=10_000,
    )
    assert (result.candidates[0].event_start_ms, result.candidates[0].event_end_ms) == (
        1_500,
        2_000,
    )


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps({"schema_version": 1, "source_duration_ms": 10_000, "unknown": True}),
        json.dumps({**_response(), "source_duration_ms": "10000"}),
        json.dumps({**_response(), "source_duration_ms": 10_000, "matches": [{"start_ms": 0}]}),
        json.dumps(
            {
                **_response(),
                "source_duration_ms": 10_000,
                "matches": [
                    {
                        **cast(dict[str, Any], cast(list[Any], _response()["matches"])[0]),
                        "candidates": [
                            {
                                **cast(
                                    dict[str, Any],
                                    cast(
                                        list[Any],
                                        cast(
                                            dict[str, Any],
                                            cast(list[Any], _response()["matches"])[0],
                                        )["candidates"],
                                    )[0],
                                ),
                                "score": 99,
                            }
                        ],
                    }
                ],
            }
        ),
    ],
)
def test_hostile_scout_responses_are_rejected(raw: str) -> None:
    with pytest.raises(ValidationError):
        canonicalize_scout_response(
            raw,
            session_id="session",
            source_id="src_" + "a" * 16,
            source_duration_ms=10_000,
        )


def test_oversized_scout_response_is_rejected_before_model_validation() -> None:
    raw = json.dumps({"padding": "x" * (MAX_SCOUT_RESPONSE_BYTES + 1)})
    with pytest.raises(ValidationError, match="safety limit"):
        parse_scout_response(raw)


def test_duplicate_json_keys_and_nonfinite_constants_are_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_scout_response('{"schema_version": 1, "schema_version": 1}')
    with pytest.raises(ValidationError):
        parse_scout_response('{"schema_version": 1, "source_duration_ms": NaN}')


def test_nonzero_source_offset_is_applied_once() -> None:
    payload = _response(duration_ms=10_000)
    result = canonicalize_scout_response(
        payload,
        session_id="session",
        source_id="src_" + "a" * 16,
        source_duration_ms=10_000,
        source_offset_ms=100,
    )
    assert result.matches[0].start_ms == 100
    assert result.candidates[0].event_start_ms == 2_100


def test_schema_snapshots_cover_core_m3_contracts() -> None:
    snapshots = m3_schema_snapshots()
    assert set(snapshots) == {"match", "candidate", "scout_response", "session_map"}
    assert "properties" in snapshots["session_map"]
    assert "candidates" in snapshots["session_map"]["properties"]


def test_canonical_storage_does_not_apply_a_product_candidate_quota() -> None:
    payload = _response(duration_ms=20_000)
    match = cast(dict[str, Any], cast(list[Any], payload["matches"])[0])
    match["candidates"] = [
        {
            "start_ms": 100 + index * 100,
            "end_ms": 150 + index * 100,
            "category": "OTHER",
            "score": 5.0,
            "confidence": 0.5,
            "reason": "A valid independent fixture moment.",
            "evidence": [],
        }
        for index in range(100)
    ]
    result = canonicalize_scout_response(
        payload,
        session_id="session",
        source_id="src_" + "a" * 16,
        source_duration_ms=20_000,
    )
    assert len(result.candidates) == 100


def test_duplicate_provider_ids_are_rejected_at_the_trust_boundary() -> None:
    payload = _response(duration_ms=10_000)
    match = cast(dict[str, Any], cast(list[Any], payload["matches"])[0])
    candidate = cast(dict[str, Any], cast(list[Any], match["candidates"])[0])
    duplicate = dict(candidate)
    duplicate["start_ms"] = 4_000
    duplicate["end_ms"] = 4_500
    match["candidates"] = [candidate, duplicate]
    with pytest.raises(ValidationError, match="duplicate candidate provider IDs"):
        canonicalize_scout_response(
            payload,
            session_id="session",
            source_id="src_" + "a" * 16,
            source_duration_ms=10_000,
        )


def test_persisted_session_map_rejects_out_of_bounds_or_broken_hierarchy() -> None:
    candidate = Candidate(
        candidate_id="cand_" + "a" * 16,
        match_id="match_" + "b" * 16,
        category="OTHER",
        event_start_ms=100,
        event_end_ms=200,
        score=5,
        confidence=0.5,
        reason="A valid candidate.",
    )
    match = Match(
        match_id="match_" + "b" * 16,
        start_ms=0,
        end_ms=1_000,
        confidence=0.5,
        candidate_ids=[],
    )
    with pytest.raises(PydanticValidationError, match="candidate is not listed"):
        SessionMap(
            created_at=datetime(2026, 8, 12, tzinfo=UTC),
            producer_version="0.3.0",
            canonicalization_version="m3-canonical-v1",
            session_id="session",
            source_id="src_" + "a" * 16,
            duration_ms=1_000,
            matches=[match],
            candidates=[candidate],
        )
