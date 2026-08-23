"""M3 Scout trust boundary and deterministic canonicalization helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from game_highlight_finder import __version__
from game_highlight_finder.domain.models import (
    Candidate,
    Evidence,
    Match,
    ScoutCandidateFragment,
    ScoutEvidence,
    ScoutResponse,
    SessionMap,
)
from game_highlight_finder.errors import ValidationError

CANONICALIZATION_VERSION = "m3-canonical-v1"
MAX_SCOUT_RESPONSE_BYTES = 1 * 1024 * 1024
MAX_CANDIDATE_DURATION_MS = 15 * 60 * 1000
MAX_BOUNDARY_CLAMP_MS = 500
MAX_MATCHES = 256
MAX_CANDIDATES = 10_000


def deterministic_id(prefix: str, payload: Mapping[str, Any], *, length: int = 16) -> str:
    """Hash canonical semantic JSON into a stable local ID."""

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def deterministic_match_id(
    *, session_id: str, start_ms: int, end_ms: int, ordinal: int | None = None
) -> str:
    return deterministic_id(
        "match",
        {
            "version": CANONICALIZATION_VERSION,
            "session_id": session_id,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "ordinal": ordinal,
        },
    )


def deterministic_candidate_id(
    *,
    session_id: str,
    match_id: str | None,
    start_ms: int,
    end_ms: int,
    category: str,
    kind: str = "MOMENT",
) -> str:
    return deterministic_id(
        "cand",
        {
            "version": CANONICALIZATION_VERSION,
            "session_id": session_id,
            "match_id": match_id,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "category": category,
            "kind": kind,
        },
    )


def normalize_timestamp_ms(
    value: object,
    *,
    duration_ms: int,
    offset_ms: int = 0,
    field: str = "timestamp",
    allow_end_clamp: bool = False,
    relative_duration_ms: int | None = None,
) -> tuple[int, str | None]:
    """Convert one provider integer timestamp to source-relative milliseconds once."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer number of milliseconds")
    if value < 0:
        raise ValidationError(f"{field} cannot be negative")
    if relative_duration_ms is not None:
        if isinstance(relative_duration_ms, bool) or not isinstance(relative_duration_ms, int):
            raise ValidationError("relative_duration_ms must be an integer")
        if relative_duration_ms <= 0:
            raise ValidationError("relative_duration_ms must be positive")
        if value > relative_duration_ms:
            raise ValidationError(f"{field} exceeds window duration")
    normalized = value + offset_ms
    if normalized < 0:
        raise ValidationError(f"{field} resolves before the source timeline")
    if normalized <= duration_ms:
        return normalized, None
    if allow_end_clamp and normalized - duration_ms <= MAX_BOUNDARY_CLAMP_MS:
        return duration_ms, f"clamped {field} to source duration"
    raise ValidationError(f"{field} exceeds source duration")


def normalize_interval(
    start_ms: object,
    end_ms: object,
    *,
    duration_ms: int,
    offset_ms: int = 0,
    field: str = "interval",
    max_duration_ms: int | None = None,
    relative_duration_ms: int | None = None,
) -> tuple[int, int, list[str]]:
    """Normalize and validate a half-open ``[start_ms, end_ms)`` interval."""

    start, start_action = normalize_timestamp_ms(
        start_ms,
        duration_ms=duration_ms,
        offset_ms=offset_ms,
        field=f"{field}.start_ms",
        relative_duration_ms=relative_duration_ms,
    )
    end, end_action = normalize_timestamp_ms(
        end_ms,
        duration_ms=duration_ms,
        offset_ms=offset_ms,
        field=f"{field}.end_ms",
        allow_end_clamp=True,
        relative_duration_ms=relative_duration_ms,
    )
    if end <= start:
        raise ValidationError(f"{field} must use a non-empty half-open interval")
    if max_duration_ms is not None and end - start > max_duration_ms:
        raise ValidationError(f"{field} exceeds the maximum supported duration")
    actions = [action for action in (start_action, end_action) if action is not None]
    return start, end, actions


def parse_scout_response(
    raw: bytes | str | Mapping[str, Any], *, max_bytes: int = MAX_SCOUT_RESPONSE_BYTES
) -> ScoutResponse:
    """Parse a bounded JSON response with no code execution or permissive coercion."""

    if isinstance(raw, bytes):
        raw_bytes = raw
        if len(raw_bytes) > max_bytes:
            raise ValidationError(f"Scout response exceeds the {max_bytes} byte safety limit.")
        try:
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValidationError("Scout response is not valid UTF-8 JSON.", hint=str(exc)) from exc
    elif isinstance(raw, str):
        raw_bytes = raw.encode("utf-8")
        if len(raw_bytes) > max_bytes:
            raise ValidationError(f"Scout response exceeds the {max_bytes} byte safety limit.")
        try:
            payload = json.loads(
                raw,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValidationError("Scout response is not valid JSON.", hint=str(exc)) from exc
    elif isinstance(raw, Mapping):
        try:
            raw_bytes = json.dumps(
                raw,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(raw_bytes) > max_bytes:
                raise ValidationError(f"Scout response exceeds the {max_bytes} byte safety limit.")
            payload = json.loads(
                raw_bytes,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite,
            )
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "Scout response contains non-JSON values.", hint=str(exc)
            ) from exc
    else:
        raise ValidationError("Scout response must be JSON text, bytes, or an object.")

    if not isinstance(payload, dict):
        raise ValidationError("Scout response top level must be a JSON object.")
    try:
        return ScoutResponse.model_validate(payload)
    except PydanticValidationError as exc:
        raise ValidationError("Scout response failed schema validation.", hint=str(exc)) from exc


def canonicalize_scout_response(
    raw: bytes | str | Mapping[str, Any],
    *,
    session_id: str,
    source_id: str,
    source_duration_ms: int,
    game_profile: str = "unknown",
    source_offset_ms: int = 0,
    created_at: datetime | None = None,
    max_response_bytes: int = MAX_SCOUT_RESPONSE_BYTES,
    source_window_id: str | None = None,
    source_window_start_ms: int | None = None,
    source_window_end_ms: int | None = None,
) -> SessionMap:
    """Turn one untrusted Scout response into a deterministic canonical map."""

    if isinstance(source_duration_ms, bool) or not isinstance(source_duration_ms, int):
        raise ValidationError("source_duration_ms must be an integer")
    if source_duration_ms <= 0:
        raise ValidationError("source_duration_ms must be positive")
    if (source_window_start_ms is None) != (source_window_end_ms is None):
        raise ValidationError("source window bounds must be supplied together")
    if source_window_start_ms is not None and source_window_end_ms is not None:
        if (
            isinstance(source_window_start_ms, bool)
            or not isinstance(source_window_start_ms, int)
            or isinstance(source_window_end_ms, bool)
            or not isinstance(source_window_end_ms, int)
        ):
            raise ValidationError("source window bounds must be integers")
        if (
            source_window_start_ms < 0
            or source_window_end_ms <= source_window_start_ms
            or source_window_end_ms > source_duration_ms
        ):
            raise ValidationError("source window bounds must be within the source duration")
    response = parse_scout_response(raw, max_bytes=max_response_bytes)
    if response.source_duration_ms != source_duration_ms:
        raise ValidationError(
            "Scout response duration does not match the authoritative source duration."
        )
    offset = source_offset_ms
    relative_duration_ms: int | None = None
    if response.time_basis == "window_relative":
        if source_window_start_ms is not None and source_window_end_ms is not None:
            if response.window_end_ms is None:
                raise ValidationError("window-relative Scout response must include window_end_ms")
            if (
                response.window_start_ms != source_window_start_ms
                or response.window_end_ms != source_window_end_ms
            ):
                raise ValidationError(
                    "Scout response window bounds do not match the authoritative request window"
                )
        elif response.window_end_ms is None:
            # Preserve the legacy M3 fixture contract when no authoritative
            # request window was supplied.  The M6 window runner always passes
            # the expected bounds and therefore takes the strict branch above.
            relative_duration_ms = None
        if response.window_end_ms is not None:
            if response.window_end_ms > source_duration_ms:
                raise ValidationError("Scout response window end exceeds source duration")
            relative_duration_ms = response.window_end_ms - response.window_start_ms
        offset += response.window_start_ms
        if (
            response.window_end_ms is not None
            and response.window_end_ms <= response.window_start_ms
        ):
            raise ValidationError("Scout response window end must be greater than its start")

    nested_candidate_count = sum(len(match.candidates) for match in response.matches)
    total_candidate_count = nested_candidate_count + len(response.candidates)
    if (
        len(response.matches) > MAX_MATCHES
        or len(response.candidates) > MAX_CANDIDATES
        or total_candidate_count > MAX_CANDIDATES
    ):
        raise ValidationError("Scout response exceeds the supported collection limits")

    warnings = list(response.warnings)
    canonical_matches: list[Match] = []
    provider_match_ids: dict[str, str] = {}
    match_indexes: dict[int, str] = {}
    for match_number, match_fragment in enumerate(response.matches):
        match_confidence = match_fragment.confidence
        if not 0 <= match_confidence <= 1:
            raise ValidationError(f"matches[{match_number}] confidence must be between 0 and 1")
        start, end, actions = normalize_interval(
            match_fragment.start_ms,
            match_fragment.end_ms,
            duration_ms=source_duration_ms,
            offset_ms=offset,
            field=f"matches[{match_number}]",
            relative_duration_ms=relative_duration_ms,
        )
        ordinal = match_fragment.ordinal if match_fragment.ordinal is not None else match_number
        match_id = deterministic_match_id(
            session_id=session_id, start_ms=start, end_ms=end, ordinal=ordinal
        )
        try:
            match = Match(
                match_id=match_id,
                ordinal=ordinal,
                start_ms=start,
                end_ms=end,
                label=match_fragment.label,
                confidence=match_fragment.confidence,
                evidence=_canonical_evidence(
                    match_fragment.evidence,
                    duration_ms=source_duration_ms,
                    offset_ms=offset,
                    field=f"matches[{match_number}].evidence",
                    relative_duration_ms=relative_duration_ms,
                ),
                source_window_ids=[source_window_id or "fake-window"],
                warnings=actions,
            )
        except PydanticValidationError as exc:
            raise ValidationError(
                f"Match fragment {match_number} failed canonical validation.",
                hint=str(exc),
            ) from exc
        canonical_matches.append(match)
        match_indexes[match_number] = match_id
        provider_match_id = match_fragment.provider_id or match_fragment.match_id
        if provider_match_id:
            if provider_match_id in provider_match_ids:
                raise ValidationError("Scout response contains duplicate match provider IDs")
            provider_match_ids[provider_match_id] = match_id
    if len({match.match_id for match in canonical_matches}) != len(canonical_matches):
        raise ValidationError("Scout response contains duplicate canonical match identities")

    fragments: list[tuple[ScoutCandidateFragment, str | None, int]] = []
    for match_index, match_fragment in enumerate(response.matches):
        containing_match_id = match_indexes[match_index]
        for candidate_fragment in match_fragment.candidates:
            resolved_match_id = _candidate_match_id(
                candidate_fragment,
                provider_match_ids,
                match_indexes,
                containing_match_id=containing_match_id,
            )
            fragments.append((candidate_fragment, resolved_match_id, match_index))
    fragments.extend(
        (
            candidate_fragment,
            _candidate_match_id(candidate_fragment, provider_match_ids, match_indexes),
            -1,
        )
        for candidate_fragment in response.candidates
    )

    canonical_candidates: list[Candidate] = []
    seen_semantics: set[tuple[object, ...]] = set()
    seen_provider_candidate_ids: set[str] = set()
    for candidate_number, (fragment, candidate_match_id, _match_index) in enumerate(fragments):
        if relative_duration_ms is not None and _candidate_has_timestamp_outside_window(
            fragment, relative_duration_ms
        ):
            warnings.append(f"dropped out-of-window candidate fragment at index {candidate_number}")
            continue
        provider_candidate_id = fragment.provider_id or fragment.candidate_id
        if provider_candidate_id:
            if provider_candidate_id in seen_provider_candidate_ids:
                raise ValidationError("Scout response contains duplicate candidate provider IDs")
            seen_provider_candidate_ids.add(provider_candidate_id)
        start, end, actions = normalize_interval(
            fragment.start_ms,
            fragment.end_ms,
            duration_ms=source_duration_ms,
            offset_ms=offset,
            field=f"candidates[{candidate_number}]",
            max_duration_ms=MAX_CANDIDATE_DURATION_MS,
            relative_duration_ms=relative_duration_ms,
        )
        category = fragment.category.strip().upper()
        if not 0 <= fragment.score <= 10:
            raise ValidationError(f"candidates[{candidate_number}] score must be between 0 and 10")
        if not 0 <= fragment.confidence <= 1:
            raise ValidationError(
                f"candidates[{candidate_number}] confidence must be between 0 and 1"
            )
        semantics = (candidate_match_id, start, end, category, "MOMENT")
        if semantics in seen_semantics:
            warnings.append(
                f"dropped exact duplicate candidate fragment at index {candidate_number}"
            )
            continue
        seen_semantics.add(semantics)
        setup = _optional_timestamp(
            fragment.setup_start_ms,
            duration_ms=source_duration_ms,
            offset_ms=offset,
            field=f"candidates[{candidate_number}].setup_start_ms",
            relative_duration_ms=relative_duration_ms,
        )
        payoff = _optional_timestamp(
            fragment.payoff_end_ms,
            duration_ms=source_duration_ms,
            offset_ms=offset,
            field=f"candidates[{candidate_number}].payoff_end_ms",
            allow_end_clamp=True,
            relative_duration_ms=relative_duration_ms,
        )
        if setup is not None and setup > start:
            raise ValidationError(f"candidates[{candidate_number}] setup starts after the event")
        if payoff is not None and payoff < end:
            raise ValidationError(f"candidates[{candidate_number}] payoff ends before the event")
        candidate_id = deterministic_candidate_id(
            session_id=session_id,
            match_id=candidate_match_id,
            start_ms=start,
            end_ms=end,
            category=category,
        )
        candidate_actions = list(actions)
        if provider_candidate_id:
            candidate_actions.append("ignored provider-supplied candidate ID")
        try:
            canonical_candidate = Candidate(
                candidate_id=candidate_id,
                match_id=candidate_match_id,
                category=category,
                event_start_ms=start,
                event_end_ms=end,
                setup_start_ms=setup,
                payoff_end_ms=payoff,
                score=fragment.score,
                confidence=fragment.confidence,
                reason=fragment.reason.strip(),
                evidence=_canonical_evidence(
                    fragment.evidence,
                    duration_ms=source_duration_ms,
                    offset_ms=offset,
                    field=f"candidates[{candidate_number}].evidence",
                    relative_duration_ms=relative_duration_ms,
                ),
                source_window_ids=[source_window_id or "fake-window"],
                clip_start_ms=setup if setup is not None else start,
                clip_end_ms=payoff if payoff is not None else end,
                normalization_actions=candidate_actions,
            )
        except PydanticValidationError as exc:
            raise ValidationError(
                f"Candidate fragment {candidate_number} failed canonical validation.",
                hint=str(exc),
            ) from exc
        canonical_candidates.append(canonical_candidate)

    match_by_id = {match.match_id: match for match in canonical_matches}
    for canonical_candidate in canonical_candidates:
        candidate_match_id = canonical_candidate.match_id
        if candidate_match_id is None:
            continue
        matched_match = match_by_id.get(candidate_match_id)
        if matched_match is not None:
            matched_match.candidate_ids.append(canonical_candidate.candidate_id)

    try:
        session_map = SessionMap(
            created_at=created_at or datetime.now(UTC),
            producer_version=__version__,
            canonicalization_version=CANONICALIZATION_VERSION,
            session_id=session_id,
            source_id=source_id,
            duration_ms=source_duration_ms,
            game_profile=game_profile,
            matches=canonical_matches,
            candidates=canonical_candidates,
            best_of_candidate_ids=[],
            statistics={
                "match_count": len(canonical_matches),
                "candidate_count": len(canonical_candidates),
                "zero_candidate_match_count": sum(
                    1 for match in canonical_matches if not match.candidate_ids
                ),
            },
            warnings=warnings,
            scout_backend=response.metadata.get("backend", "fake"),
            scout_metadata=dict(response.metadata),
        )
    except PydanticValidationError as exc:
        raise ValidationError("Canonical session map validation failed.", hint=str(exc)) from exc
    return session_map


def _candidate_match_id(
    candidate: ScoutCandidateFragment,
    provider_match_ids: Mapping[str, str],
    match_indexes: Mapping[int, str],
    *,
    containing_match_id: str | None = None,
) -> str | None:
    resolved_by_index: str | None = None
    resolved_by_id: str | None = None
    if candidate.match_index is not None:
        resolved_by_index = match_indexes.get(candidate.match_index)
        if resolved_by_index is None:
            raise ValidationError(
                f"Candidate references unknown match index {candidate.match_index}"
            )
    if candidate.match_id is not None:
        resolved_by_id = provider_match_ids.get(candidate.match_id)
        if resolved_by_id is None:
            raise ValidationError(
                f"Candidate references unknown provider match ID {candidate.match_id!r}"
            )
    if (
        resolved_by_index is not None
        and resolved_by_id is not None
        and resolved_by_index != resolved_by_id
    ):
        raise ValidationError("Candidate match_id and match_index resolve to different matches")
    resolved = resolved_by_index or resolved_by_id
    if containing_match_id is not None and resolved is not None and resolved != containing_match_id:
        raise ValidationError("Candidate match reference conflicts with its containing match")
    return containing_match_id or resolved


def _candidate_has_timestamp_outside_window(
    candidate: ScoutCandidateFragment, relative_duration_ms: int
) -> bool:
    timestamps = [
        candidate.start_ms,
        candidate.end_ms,
        candidate.setup_start_ms,
        candidate.payoff_end_ms,
    ]
    for evidence in candidate.evidence:
        timestamps.extend((evidence.start_ms, evidence.end_ms))
    return any(
        timestamp is not None and timestamp > relative_duration_ms for timestamp in timestamps
    )


def _optional_timestamp(
    value: int | None,
    *,
    duration_ms: int,
    offset_ms: int,
    field: str,
    allow_end_clamp: bool = False,
    relative_duration_ms: int | None = None,
) -> int | None:
    if value is None:
        return None
    normalized, _ = normalize_timestamp_ms(
        value,
        duration_ms=duration_ms,
        offset_ms=offset_ms,
        field=field,
        allow_end_clamp=allow_end_clamp,
        relative_duration_ms=relative_duration_ms,
    )
    return normalized


def _canonical_evidence(
    evidence: list[ScoutEvidence],
    *,
    duration_ms: int,
    offset_ms: int,
    field: str,
    relative_duration_ms: int | None = None,
) -> list[Evidence]:
    result: list[Evidence] = []
    for index, item in enumerate(evidence):
        start = _optional_timestamp(
            item.start_ms,
            duration_ms=duration_ms,
            offset_ms=offset_ms,
            field=f"{field}[{index}].start_ms",
            relative_duration_ms=relative_duration_ms,
        )
        end = _optional_timestamp(
            item.end_ms,
            duration_ms=duration_ms,
            offset_ms=offset_ms,
            field=f"{field}[{index}].end_ms",
            allow_end_clamp=True,
            relative_duration_ms=relative_duration_ms,
        )
        if start is not None and end is not None and end <= start:
            raise ValidationError(f"{field}[{index}] has an invalid interval")
        try:
            result.append(
                Evidence(
                    type=item.type,
                    start_ms=start,
                    end_ms=end,
                    strength=item.strength,
                    summary=item.summary.strip(),
                    source=item.source,
                )
            )
        except PydanticValidationError as exc:
            raise ValidationError(
                f"{field}[{index}] failed canonical validation.", hint=str(exc)
            ) from exc
    return result


def canonical_json_bytes(value: Any) -> bytes:
    """Stable JSON bytes for cache fingerprints and immutable raw artifacts."""

    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError("Value cannot be encoded as bounded JSON.", hint=str(exc)) from exc
    return encoded.encode("utf-8")


canonicalize = canonicalize_scout_response
canonicalize_scout_output = canonicalize_scout_response


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")
