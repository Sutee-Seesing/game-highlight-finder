"""Deterministic local M7 candidate ranking and cache."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from game_highlight_finder import __version__
from game_highlight_finder.config import AppConfig
from game_highlight_finder.domain.models import Candidate, SessionMap
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.hashing import hash_file
from game_highlight_finder.storage.sessions import SessionPaths

RANKING_VERSION = "m7-ranking-v1"
RANKING_SCHEMA_VERSION = 1


class RankingEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str
    rank: int = Field(gt=0)
    score: float = Field(ge=0, le=10)
    confidence: float = Field(ge=0, le=1)
    ranking_key: str = Field(min_length=1, max_length=300)


class RankingArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    producer_version: str
    ranking_version: str
    session_id: str
    source_id: str
    candidate_count: int = Field(ge=0)
    ordered_candidate_ids: list[str] = Field(default_factory=list)
    best_of_candidate_ids: list[str] = Field(default_factory=list)
    entries: list[RankingEntry] = Field(default_factory=list)
    cache_key: str = Field(pattern=r"^[0-9a-f]{64}$")


def _candidate_key(candidate: Candidate) -> tuple[float, float, int, str]:
    return (
        -candidate.score,
        -candidate.confidence,
        candidate.event_start_ms,
        candidate.candidate_id,
    )


def ranking_cache_payload(session_map: SessionMap, *, best_of_limit: int) -> dict[str, Any]:
    canonical = json.dumps(
        session_map.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return {
        "ranking_version": RANKING_VERSION,
        "session_map_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "best_of_limit": best_of_limit,
    }


def ranking_cache_key(session_map: SessionMap, *, best_of_limit: int) -> str:
    encoded = json.dumps(
        ranking_cache_payload(session_map, best_of_limit=best_of_limit),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def rank_session_map(session_map: SessionMap, *, best_of_limit: int = 3) -> RankingArtifact:
    """Return a separate ranking artifact; never mutate the canonical map."""

    ordered = sorted(session_map.candidates, key=_candidate_key)
    entries = [
        RankingEntry(
            candidate_id=candidate.candidate_id,
            rank=index,
            score=candidate.score,
            confidence=candidate.confidence,
            ranking_key=(
                f"score={candidate.score:.6f};confidence={candidate.confidence:.6f};"
                f"event_start_ms={candidate.event_start_ms};candidate_id={candidate.candidate_id}"
            ),
        )
        for index, candidate in enumerate(ordered, start=1)
    ]
    return RankingArtifact(
        producer_version=__version__,
        ranking_version=RANKING_VERSION,
        session_id=session_map.session_id,
        source_id=session_map.source_id,
        candidate_count=len(ordered),
        ordered_candidate_ids=[candidate.candidate_id for candidate in ordered],
        best_of_candidate_ids=[candidate.candidate_id for candidate in ordered[:best_of_limit]],
        entries=entries,
        cache_key=ranking_cache_key(session_map, best_of_limit=best_of_limit),
    )


def load_or_create_ranking(
    paths: SessionPaths,
    session_map: SessionMap,
    config: AppConfig,
    *,
    force: bool = False,
) -> tuple[RankingArtifact, bool]:
    artifact = rank_session_map(session_map, best_of_limit=config.report.best_of_limit)
    if not force and paths.ranking_path.is_file():
        try:
            existing = RankingArtifact.model_validate(read_json(paths.ranking_path))
            if existing.cache_key == artifact.cache_key and existing.model_dump(
                mode="json"
            ) == artifact.model_dump(mode="json"):
                return existing, True
        except Exception:
            pass
    atomic_write_json(paths.ranking_path, artifact.model_dump(mode="json"))
    return artifact, False


def ranking_hash(path: Path) -> str:
    return hash_file(path)


__all__ = [
    "RANKING_VERSION",
    "RankingArtifact",
    "RankingEntry",
    "load_or_create_ranking",
    "rank_session_map",
    "ranking_cache_key",
    "ranking_cache_payload",
    "ranking_hash",
]
