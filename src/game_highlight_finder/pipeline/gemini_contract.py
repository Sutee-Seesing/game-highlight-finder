"""Deterministic M5 Gemini prompt and provider-schema projection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from game_highlight_finder.domain.models import CandidateCategory

GEMINI_PROMPT_VERSION = "gemini-scout-v1"
GEMINI_WINDOW_CALIBRATION_PROMPT_VERSION = "gemini-scout-window-v19"
GEMINI_SCHEMA_VERSION = 1
# ScoutConfig permits windows up to 3,600 seconds. Keep provider-side
# relative timestamps bounded to the same ceiling so malformed generations
# cannot emit pathological integers before local validation runs.
MAX_WINDOW_RELATIVE_TIMESTAMP_MS = 3_600_000


def gemini_scout_schema() -> dict[str, Any]:
    """Return the intentionally small JSON-schema subset accepted by Gemini.

    The M3 Pydantic parser remains authoritative after the provider returns.
    This projection avoids provider-specific schema features such as unions,
    numeric bounds, or references that some Gemini API surfaces do not accept.
    """

    evidence = {
        "type": "object",
        "properties": {
            "type": {"type": "string"},
            "start_ms": {"type": "integer"},
            "end_ms": {"type": "integer"},
            "strength": {"type": "number"},
            "summary": {"type": "string"},
            "source": {"type": "string"},
        },
        "required": ["type", "summary"],
    }
    candidate = {
        "type": "object",
        "properties": {
            "start_ms": {"type": "integer"},
            "end_ms": {"type": "integer"},
            "category": {
                "type": "string",
                "enum": [category.value for category in CandidateCategory],
            },
            "score": {"type": "number"},
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
            "setup_start_ms": {"type": "integer"},
            "payoff_end_ms": {"type": "integer"},
            "evidence": {"type": "array", "items": evidence},
            "match_index": {"type": "integer"},
        },
        "required": [
            "start_ms",
            "end_ms",
            "category",
            "score",
            "confidence",
            "reason",
            "evidence",
        ],
    }
    match = {
        "type": "object",
        "properties": {
            "start_ms": {"type": "integer"},
            "end_ms": {"type": "integer"},
            "confidence": {"type": "number"},
            "label": {"type": "string"},
            "ordinal": {"type": "integer"},
            "evidence": {"type": "array", "items": evidence},
            "candidates": {"type": "array", "items": candidate},
        },
        "required": ["start_ms", "end_ms", "confidence", "evidence", "candidates"],
    }
    return {
        "type": "object",
        "properties": {
            "schema_version": {"type": "integer", "enum": [1]},
            "source_duration_ms": {"type": "integer"},
            "time_basis": {"type": "string", "enum": ["source_relative"]},
            "matches": {"type": "array", "items": match},
            "candidates": {"type": "array", "items": candidate},
            "warnings": {"type": "array", "items": {"type": "string"}},
            "metadata": {
                "type": "object",
                "properties": {"backend": {"type": "string"}},
                "required": ["backend"],
            },
        },
        "required": [
            "schema_version",
            "source_duration_ms",
            "time_basis",
            "matches",
            "candidates",
            "warnings",
            "metadata",
        ],
    }


def gemini_window_scout_schema() -> dict[str, Any]:
    """Project the strict M6 window-relative provider contract.

    Window Scout omits optional setup/payoff timestamps and hardens the
    remaining structured output with JSON Schema features supported by the
    current Gemini API. The canonical domain remains backward-compatible.
    """

    schema = gemini_scout_schema()
    properties = schema["properties"]
    candidate_schemas = (
        properties["candidates"]["items"],
        properties["matches"]["items"]["properties"]["candidates"]["items"],
    )
    for candidate_schema in candidate_schemas:
        candidate_properties = candidate_schema["properties"]
        candidate_properties.pop("setup_start_ms", None)
        candidate_properties.pop("payoff_end_ms", None)
        for field in ("start_ms", "end_ms"):
            candidate_properties[field].update(minimum=0, maximum=MAX_WINDOW_RELATIVE_TIMESTAMP_MS)
        candidate_properties["score"].update(minimum=0, maximum=10)
        candidate_properties["confidence"].update(minimum=0, maximum=1)

    match_schema = properties["matches"]["items"]
    match_properties = match_schema["properties"]
    for field in ("start_ms", "end_ms"):
        match_properties[field].update(minimum=0, maximum=MAX_WINDOW_RELATIVE_TIMESTAMP_MS)
    match_properties["confidence"].update(minimum=0, maximum=1)

    evidence_schemas = (
        properties["candidates"]["items"]["properties"]["evidence"]["items"],
        match_properties["evidence"]["items"],
    )
    for evidence_schema in evidence_schemas:
        evidence_properties = evidence_schema["properties"]
        for field in ("start_ms", "end_ms"):
            evidence_properties[field].update(minimum=0, maximum=MAX_WINDOW_RELATIVE_TIMESTAMP_MS)
        evidence_properties["strength"].update(minimum=0, maximum=1)

    properties["source_duration_ms"].update(minimum=0)
    properties["window_start_ms"] = {"type": "integer", "minimum": 0}
    properties["window_end_ms"] = {"type": "integer", "minimum": 0}
    properties["time_basis"] = {"type": "string", "enum": ["window_relative"]}
    required = schema["required"]
    required.extend(["window_start_ms", "window_end_ms"])

    def forbid_extra_keys(value: object) -> None:
        if isinstance(value, dict):
            if value.get("type") == "object":
                value["additionalProperties"] = False
            for child in value.values():
                forbid_extra_keys(child)
        elif isinstance(value, list):
            for child in value:
                forbid_extra_keys(child)

    forbid_extra_keys(schema)
    return schema


def schema_json() -> str:
    return json.dumps(gemini_scout_schema(), sort_keys=True, separators=(",", ":"))


def schema_hash() -> str:
    return hashlib.sha256(schema_json().encode("utf-8")).hexdigest()


def _calibration_semantic_guidance(prompt_version: str) -> tuple[str, ...]:
    if prompt_version != GEMINI_WINDOW_CALIBRATION_PROMPT_VERSION:
        return ()
    return (
        (
            "Treat bounded local-signal hints as navigation cues only; loudness or activity "
            "is never semantic proof that a moment is a highlight."
        ),
        (
            "A Candidate Moment needs visible on-screen evidence of a distinct clip-worthy "
            "event, interaction, payoff, skill display, reaction, fail, or unexpected moment."
        ),
        (
            "Do not emit a candidate for pure traversal, rotation, idle weapon movement, "
            "ambient effects, or UI-only activity when no visible event or payoff occurs, "
            "even if the local audio signal is strong."
        ),
        (
            "Preserve recall for visibly real interactions: when an event is clearly present "
            "but clip-worthiness is uncertain, include it with a lower score or confidence "
            "rather than omitting the event."
        ),
        (
            "Candidate reasons and evidence summaries must name the visible on-screen cue "
            "that makes the interval eventful; do not cite loudness alone as evidence."
        ),
    )


def build_gemini_prompt(
    *,
    duration_ms: int,
    game_profile: str = "unknown",
    local_signal_summary: Mapping[str, Any] | None = None,
    prompt_version: str = GEMINI_PROMPT_VERSION,
) -> str:
    """Build a bounded, deterministic Scout instruction with no hidden reasoning request."""

    if duration_ms <= 0:
        raise ValueError("duration_ms must be positive")
    summary = dict(local_signal_summary or {})
    summary_json = json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    semantic_guidance = _calibration_semantic_guidance(prompt_version)
    return "\n".join(
        (
            f"You are Game Highlight Finder Scout prompt {prompt_version}.",
            (
                "Analyze the supplied gameplay analysis proxy and return only JSON "
                "matching the provided schema."
            ),
            (
                "Understand session structure and identify match or round boundaries "
                "only when visible enough to be safe."
            ),
            "Return zero matches when boundaries cannot be determined safely.",
            (
                "Return zero or more worthwhile Candidate Moments; a boring session "
                "may legitimately contain zero candidates."
            ),
            (
                "Do not invent precise events, timestamps, scores, or confidence "
                "when the evidence is weak."
            ),
            "Use source-relative integer milliseconds and distinguish score from confidence.",
            (
                "Use concise user-facing evidence and reasons. Do not emit hidden "
                "reasoning or thought steps."
            ),
            "Candidate categories must use the existing M3 taxonomy.",
            *semantic_guidance,
            f"Source duration (milliseconds): {duration_ms}",
            f"Game profile: {game_profile}",
            f"Bounded local-signal hints: {summary_json}",
        )
    )


__all__ = [
    "GEMINI_PROMPT_VERSION",
    "GEMINI_SCHEMA_VERSION",
    "GEMINI_WINDOW_CALIBRATION_PROMPT_VERSION",
    "build_gemini_prompt",
    "gemini_scout_schema",
    "gemini_window_scout_schema",
    "schema_hash",
    "schema_json",
]
