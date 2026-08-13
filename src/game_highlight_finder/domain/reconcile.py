"""Pure deterministic reconciliation of per-window canonical Scout maps."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from game_highlight_finder import __version__
from game_highlight_finder.config import ExtractionConfig
from game_highlight_finder.domain.canonical import (
    CANONICALIZATION_VERSION,
    deterministic_candidate_id,
    deterministic_match_id,
)
from game_highlight_finder.domain.models import Candidate, Evidence, Match, SessionMap


def _iou(a_start: int, a_end: int, b_start: int, b_end: int) -> float:
    intersection = max(0, min(a_end, b_end) - max(a_start, b_start))
    union = max(a_end, b_end) - min(a_start, b_start)
    return intersection / union if union else 0.0


def _merge_evidence(items: Iterable[Evidence], *, limit: int) -> list[Evidence]:
    result: list[Evidence] = []
    seen: set[tuple[Any, ...]] = set()
    for item in items:
        key = (item.type, item.start_ms, item.end_ms, item.summary, item.source)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= limit:
            break
    return result


def _compatible_match(left: Match, right: Match) -> bool:
    overlap = max(0, min(left.end_ms, right.end_ms) - max(left.start_ms, right.start_ms))
    if overlap <= 0:
        return (
            left.label is not None
            and right.label is not None
            and left.label.strip().casefold() == right.label.strip().casefold()
            and abs(left.end_ms - right.start_ms) <= 1_000
        )
    same_label = (
        left.label is not None
        and right.label is not None
        and left.label.strip().casefold() == right.label.strip().casefold()
    )
    same_ordinal = left.ordinal is not None and left.ordinal == right.ordinal
    return (
        same_label
        or same_ordinal
        or _iou(left.start_ms, left.end_ms, right.start_ms, right.end_ms) >= 0.25
    )


def _merge_match(left: Match, right: Match) -> Match:
    ordinal = left.ordinal if left.ordinal is not None else right.ordinal
    if left.ordinal is not None and right.ordinal is not None:
        ordinal = min(left.ordinal, right.ordinal)
    return left.model_copy(
        update={
            "match_id": left.match_id,
            "ordinal": ordinal,
            "start_ms": min(left.start_ms, right.start_ms),
            "end_ms": max(left.end_ms, right.end_ms),
            "label": left.label or right.label,
            "confidence": max(left.confidence, right.confidence),
            "evidence": _merge_evidence([*left.evidence, *right.evidence], limit=32),
            "source_window_ids": list(
                dict.fromkeys([*left.source_window_ids, *right.source_window_ids])
            )[:32],
            "candidate_ids": [],
            "warnings": list(dict.fromkeys([*left.warnings, *right.warnings]))[:32],
            "metadata": {**right.metadata, **left.metadata},
        }
    )


def _effective_interval(candidate: Candidate) -> tuple[int, int]:
    start = min(
        value for value in (candidate.event_start_ms, candidate.setup_start_ms) if value is not None
    )
    end = max(
        value for value in (candidate.event_end_ms, candidate.payoff_end_ms) if value is not None
    )
    return start, end


def _candidate_compatible(
    left: Candidate, right: Candidate, *, overlapping_lineage: bool = False
) -> bool:
    if left.category != right.category:
        return False
    left_start, left_end = _effective_interval(left)
    right_start, right_end = _effective_interval(right)
    same_window = bool(set(left.source_window_ids) & set(right.source_window_ids))
    endpoint_jitter = abs(left_start - right_start) <= 500 and abs(left_end - right_end) <= 500
    return (same_window or overlapping_lineage) and (
        _iou(left_start, left_end, right_start, right_end) >= 0.5 or endpoint_jitter
    )


def _merge_candidate(left: Candidate, right: Candidate) -> Candidate:
    start = min(left.event_start_ms, right.event_start_ms)
    end = max(left.event_end_ms, right.event_end_ms)
    setup_values = [
        value for value in (left.setup_start_ms, right.setup_start_ms) if value is not None
    ]
    payoff_values = [
        value for value in (left.payoff_end_ms, right.payoff_end_ms) if value is not None
    ]
    return left.model_copy(
        update={
            "candidate_id": left.candidate_id,
            "event_start_ms": start,
            "event_end_ms": end,
            "setup_start_ms": min(setup_values) if setup_values else None,
            "payoff_end_ms": max(payoff_values) if payoff_values else None,
            "score": max(left.score, right.score),
            "confidence": max(left.confidence, right.confidence),
            "reason": left.reason
            if (left.confidence, left.score) >= (right.confidence, right.score)
            else right.reason,
            "evidence": _merge_evidence([*left.evidence, *right.evidence], limit=16),
            "source_window_ids": list(
                dict.fromkeys([*left.source_window_ids, *right.source_window_ids])
            )[:32],
            "normalization_actions": list(
                dict.fromkeys([*left.normalization_actions, *right.normalization_actions])
            )[:32],
        }
    )


def reconcile_session_maps(
    session_id: str,
    source_id: str,
    source_duration_ms: int,
    window_maps: Sequence[tuple[object, SessionMap]],
    *,
    game_profile: str = "unknown",
    created_at: datetime | None = None,
) -> SessionMap:
    """Reconcile overlapping window maps conservatively and deterministically."""

    warnings: list[str] = []
    matches: list[Match] = []
    candidates: list[Candidate] = []
    window_ranges: dict[str, tuple[int, int]] = {}
    for window, session_map in window_maps:
        window_id = getattr(window, "window_id", None)
        if window_id:
            window_ranges[window_id] = (
                int(getattr(window, "source_start_ms", 0)),
                int(getattr(window, "source_end_ms", 0)),
            )
        if session_map.duration_ms != source_duration_ms:
            warnings.append("window map duration mismatch; fragment omitted")
            continue
        for match in session_map.matches:
            matches.append(
                match.model_copy(
                    update={
                        "source_window_ids": list(
                            dict.fromkeys(
                                [*(match.source_window_ids or []), window_id or "unknown-window"]
                            )
                        )[:32]
                    }
                )
            )
        for candidate in session_map.candidates:
            candidates.append(
                candidate.model_copy(
                    update={
                        "source_window_ids": list(
                            dict.fromkeys(
                                [
                                    *(candidate.source_window_ids or []),
                                    window_id or "unknown-window",
                                ]
                            )
                        )[:32]
                    }
                )
            )

    # Merge only compatible overlaps/continuations; conflicting overlaps are
    # retained as the higher-confidence fragment and recorded as a diagnostic.
    merged_matches: list[Match] = []
    for fragment in sorted(
        matches, key=lambda item: (item.start_ms, item.end_ms, item.ordinal or 0)
    ):
        if not merged_matches:
            merged_matches.append(fragment)
            continue
        previous = merged_matches[-1]
        overlap = max(
            0, min(previous.end_ms, fragment.end_ms) - max(previous.start_ms, fragment.start_ms)
        )
        if _compatible_match(previous, fragment):
            merged_matches[-1] = _merge_match(previous, fragment)
        elif overlap > 0:
            warnings.append("conflicting overlapping match fragments were conservatively separated")
            if fragment.confidence > previous.confidence:
                merged_matches[-1] = fragment
        else:
            merged_matches.append(fragment)

    # Enforce non-overlap after conservative conflict handling.
    normalized_matches: list[Match] = []
    for fragment in sorted(merged_matches, key=lambda item: (item.start_ms, item.end_ms)):
        if normalized_matches and fragment.start_ms < normalized_matches[-1].end_ms:
            warnings.append("overlapping match fragment dropped during final normalization")
            continue
        normalized_matches.append(fragment)
    normalized_matches = [
        item.model_copy(
            update={
                "match_id": deterministic_match_id(
                    session_id=session_id,
                    start_ms=item.start_ms,
                    end_ms=item.end_ms,
                    ordinal=item.ordinal,
                )
            }
        )
        for item in normalized_matches
    ]

    # Deduplicate candidate fragments by category and overlap-window lineage.
    merged_candidates: list[Candidate] = []
    for candidate_fragment in sorted(
        candidates, key=lambda item: (item.event_start_ms, item.event_end_ms, item.category)
    ):
        candidate_match = next(
            (
                item
                for item in normalized_matches
                if min(
                    candidate_fragment.event_start_ms,
                    candidate_fragment.setup_start_ms or candidate_fragment.event_start_ms,
                )
                >= item.start_ms
                and max(
                    candidate_fragment.event_end_ms,
                    candidate_fragment.payoff_end_ms or candidate_fragment.event_end_ms,
                )
                <= item.end_ms
            ),
            None,
        )
        candidate_fragment = candidate_fragment.model_copy(
            update={"match_id": candidate_match.match_id if candidate_match else None}
        )
        duplicate_index = next(
            (
                index
                for index, existing in enumerate(merged_candidates)
                if _candidate_compatible(
                    existing,
                    candidate_fragment,
                    overlapping_lineage=any(
                        _iou(
                            *window_ranges[left_id],
                            *window_ranges[right_id],
                        )
                        > 0
                        for left_id in existing.source_window_ids
                        for right_id in candidate_fragment.source_window_ids
                        if left_id in window_ranges and right_id in window_ranges
                    ),
                )
            ),
            None,
        )
        if duplicate_index is None:
            merged_candidates.append(candidate_fragment)
        else:
            merged_candidates[duplicate_index] = _merge_candidate(
                merged_candidates[duplicate_index], candidate_fragment
            )

    final_candidates: list[Candidate] = []
    for candidate in merged_candidates:
        candidate_match = next(
            (item for item in normalized_matches if item.match_id == candidate.match_id), None
        )
        start = candidate.event_start_ms
        end = candidate.event_end_ms
        if candidate_match is not None and (
            start < candidate_match.start_ms or end > candidate_match.end_ms
        ):
            candidate = candidate.model_copy(update={"match_id": None})
        candidate_id = deterministic_candidate_id(
            session_id=session_id,
            match_id=candidate.match_id,
            start_ms=candidate.event_start_ms,
            end_ms=candidate.event_end_ms,
            category=candidate.category,
            kind=candidate.kind,
        )
        final_candidates.append(candidate.model_copy(update={"candidate_id": candidate_id}))

    candidate_ids_by_match: dict[str, list[str]] = {
        item.match_id: [] for item in normalized_matches
    }
    for candidate in final_candidates:
        if candidate.match_id is not None:
            candidate_ids_by_match[candidate.match_id].append(candidate.candidate_id)
    final_matches = [
        item.model_copy(update={"candidate_ids": candidate_ids_by_match[item.match_id]})
        for item in normalized_matches
    ]
    return SessionMap(
        created_at=created_at or datetime.now(UTC),
        producer_version=__version__,
        canonicalization_version="m6-reconcile-v1",
        session_id=session_id,
        source_id=source_id,
        duration_ms=source_duration_ms,
        game_profile=game_profile,
        matches=final_matches,
        candidates=final_candidates,
        best_of_candidate_ids=[],
        statistics={
            "window_count": len(window_maps),
            "match_count": len(final_matches),
            "candidate_count": len(final_candidates),
        },
        warnings=list(dict.fromkeys(warnings))[:100],
        scout_backend="fake",
        scout_metadata={
            "reconciliation": "conservative",
            "canonicalization": CANONICALIZATION_VERSION,
        },
    )


def derive_clip_boundaries(
    session_map: SessionMap,
    source_duration_ms: int,
    extraction_config: ExtractionConfig,
) -> SessionMap:
    """Derive bounded clips around event/setup/payoff timestamps."""

    pre_ms = extraction_config.pre_roll_seconds * 1_000
    post_ms = extraction_config.post_roll_seconds * 1_000
    minimum_ms = extraction_config.minimum_duration_seconds * 1_000
    maximum_ms = extraction_config.maximum_duration_seconds * 1_000
    by_match = {item.match_id: item for item in session_map.matches}
    updated: list[Candidate] = []
    for candidate in session_map.candidates:
        event_start = min(
            candidate.event_start_ms, candidate.setup_start_ms or candidate.event_start_ms
        )
        event_end = max(candidate.event_end_ms, candidate.payoff_end_ms or candidate.event_end_ms)
        start = max(0, event_start - pre_ms)
        end = min(source_duration_ms, event_end + post_ms)
        actions = list(candidate.normalization_actions)
        match = by_match.get(candidate.match_id or "")
        if match is not None:
            start = max(start, match.start_ms)
            end = min(end, match.end_ms)
            actions.append("clip bounded to matched interval")
        if end - start < minimum_ms:
            center = (event_start + event_end) // 2
            start = max(0, center - minimum_ms // 2)
            end = min(source_duration_ms, start + minimum_ms)
            start = max(0, end - minimum_ms)
            if match is not None:
                start = max(start, match.start_ms)
                end = min(end, match.end_ms)
            actions.append("expanded clip to minimum duration")
        if end - start > maximum_ms:
            start = max(0, event_start - maximum_ms // 2)
            end = min(source_duration_ms, start + maximum_ms)
            start = max(0, end - maximum_ms)
            if match is not None:
                start = max(start, match.start_ms)
                end = min(end, match.end_ms)
            actions.append("clamped clip to maximum duration")
        if end <= start:
            start, end = candidate.event_start_ms, candidate.event_end_ms
            actions.append("fallback event interval used")
        updated.append(
            candidate.model_copy(
                update={
                    "clip_start_ms": max(0, start),
                    "clip_end_ms": min(source_duration_ms, end),
                    "normalization_actions": list(dict.fromkeys(actions))[:32],
                }
            )
        )
    return session_map.model_copy(update={"candidates": updated})


reconcile_window_maps = reconcile_session_maps

__all__ = ["derive_clip_boundaries", "reconcile_session_maps", "reconcile_window_maps"]
