"""Small deterministic JSON Schema snapshot helpers for externally persisted M3 models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from game_highlight_finder.domain.models import Candidate, Match, ScoutResponse, SessionMap

M3_SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "match": Match,
    "candidate": Candidate,
    "scout_response": ScoutResponse,
    "session_map": SessionMap,
}


def m3_schema_snapshots() -> dict[str, dict[str, Any]]:
    """Return stable Pydantic-generated schemas for fixtures and provider contracts."""

    return {
        name: model.model_json_schema(by_alias=True, ref_template="#/$defs/{model}")
        for name, model in M3_SCHEMA_MODELS.items()
    }
