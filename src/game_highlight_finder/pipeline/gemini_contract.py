"""Deterministic M5 Gemini prompt and provider-schema projection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from game_highlight_finder.domain.models import CandidateCategory

GEMINI_PROMPT_VERSION = "gemini-scout-v1"
GEMINI_SCHEMA_VERSION = 1


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
            "schema_version": {"type": "integer"},
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
    """Project the M6 window-relative contract for a provider request."""

    schema = gemini_scout_schema()
    properties = schema["properties"]
    properties["time_basis"] = {"type": "string", "enum": ["window_relative"]}
    properties["window_start_ms"] = {"type": "integer"}
    properties["window_end_ms"] = {"type": "integer"}
    required = schema["required"]
    required.extend(["window_start_ms", "window_end_ms"])
    return schema


def schema_json() -> str:
    return json.dumps(gemini_scout_schema(), sort_keys=True, separators=(",", ":"))


def schema_hash() -> str:
    return hashlib.sha256(schema_json().encode("utf-8")).hexdigest()


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
            f"Source duration (milliseconds): {duration_ms}",
            f"Game profile: {game_profile}",
            f"Bounded local-signal hints: {summary_json}",
        )
    )


__all__ = [
    "GEMINI_PROMPT_VERSION",
    "GEMINI_SCHEMA_VERSION",
    "build_gemini_prompt",
    "gemini_scout_schema",
    "gemini_window_scout_schema",
    "schema_hash",
    "schema_json",
]
