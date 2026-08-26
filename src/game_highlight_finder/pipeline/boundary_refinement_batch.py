"""Bounded provider-free batch orchestration for candidate boundary refinement."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from game_highlight_finder.config import AppConfig
from game_highlight_finder.domain.models import (
    Candidate,
    SessionMap,
    Sha256,
    SourceAsset,
    model_json,
)
from game_highlight_finder.domain.reconcile import derive_clip_boundaries
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.pipeline.boundary_refinement import (
    BoundaryRefinementMediaResult,
    prepare_boundary_refinement_media,
)
from game_highlight_finder.pipeline.boundary_refiner import (
    BoundaryRefinementFakeResult,
    FakeBoundaryRefiner,
    canonical_payload_sha256,
    run_fake_boundary_refinement,
)
from game_highlight_finder.pipeline.proxy import ProxyResult
from game_highlight_finder.storage.atomic import atomic_write_json
from game_highlight_finder.storage.sessions import session_paths

BOUNDARY_REFINEMENT_BATCH_VERSION = "boundary-refiner-batch-v1"
MAX_BOUNDARY_REFINEMENT_BATCH_CANDIDATES = 32


class BoundaryRefinementBatchItemArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(pattern=r"^cand_[0-9a-f]{16}$")
    request_fingerprint: Sha256
    response_status: Literal["REFINED", "UNCERTAIN"]
    response_confidence: float = Field(ge=0, le=1)
    original_candidate_sha256: Sha256
    output_candidate_sha256: Sha256
    boundary_changed: bool
    media_artifact_path: str = Field(min_length=1, max_length=1000)
    response_artifact_path: str = Field(min_length=1, max_length=1000)


class BoundaryRefinementBatchArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    version: str = BOUNDARY_REFINEMENT_BATCH_VERSION
    backend: Literal["fake"] = "fake"
    session_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(pattern=r"^src_[0-9a-f]{16}$")
    input_session_map_sha256: Sha256
    selected_candidate_ids: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_BOUNDARY_REFINEMENT_BATCH_CANDIDATES,
    )
    minimum_confidence: float = Field(ge=0, le=1)
    items: tuple[BoundaryRefinementBatchItemArtifact, ...]
    refined_session_map_sha256: Sha256

    @model_validator(mode="after")
    def items_match_selection(self) -> BoundaryRefinementBatchArtifact:
        item_ids = tuple(item.candidate_id for item in self.items)
        if item_ids != self.selected_candidate_ids:
            raise ValueError("boundary refinement batch items must match selected candidate order")
        return self


class BoundaryRefinementBatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    artifact_path: Path
    refined_session_map_path: Path
    artifact: BoundaryRefinementBatchArtifact
    session_map: SessionMap
    media_cache_hits: int = Field(ge=0)
    response_cache_hits: int = Field(ge=0)
    generated_responses: int = Field(ge=0)


def run_fake_boundary_refinement_batch(
    source: SourceAsset,
    proxy: ProxyResult,
    session_map: SessionMap,
    config: AppConfig,
    *,
    candidate_ids: Sequence[str],
    fake_refiners: Mapping[str, FakeBoundaryRefiner] | None = None,
    minimum_confidence: float = 0.5,
    force: bool = False,
) -> BoundaryRefinementBatchResult:
    """Refine an explicit bounded candidate set without changing the production session map."""

    selected_ids = _validate_candidate_selection(session_map, candidate_ids)
    if source.source_id != session_map.source_id:
        raise ValidationError("boundary refinement batch source does not match the session map")
    if proxy.session_id != session_map.session_id:
        raise ValidationError("boundary refinement batch proxy does not match the session map")
    if source.duration_ms != session_map.duration_ms:
        raise ValidationError("boundary refinement batch duration does not match the session map")
    if not 0 <= minimum_confidence <= 1:
        raise ValidationError("boundary refinement minimum confidence must be between 0 and 1")

    by_id = {candidate.candidate_id: candidate for candidate in session_map.candidates}
    refiners = fake_refiners or {}
    refined_by_id: dict[str, Candidate] = {}
    executions: list[
        tuple[Candidate, BoundaryRefinementMediaResult, BoundaryRefinementFakeResult]
    ] = []
    media_cache_hits = 0
    response_cache_hits = 0
    generated_responses = 0
    paths = session_paths(config.storage.data_dir, session_map.session_id)

    for candidate_id in selected_ids:
        candidate = by_id[candidate_id]
        media = prepare_boundary_refinement_media(
            source,
            proxy,
            candidate,
            config,
            force=force,
        )
        media_cache_hits += int(media.cache_hit)
        result = run_fake_boundary_refinement(
            media,
            candidate,
            refiners.get(candidate_id),
            minimum_confidence=minimum_confidence,
            force=force,
        )
        response_cache_hits += int(result.cache_hit)
        generated_responses += int(not result.cache_hit)
        refined_by_id[candidate_id] = result.candidate
        executions.append((candidate, media, result))

    updated_candidates = [
        refined_by_id.get(candidate.candidate_id, candidate) for candidate in session_map.candidates
    ]
    refined_map = session_map.model_copy(
        update={
            "candidates": updated_candidates,
            "scout_metadata": {
                **session_map.scout_metadata,
                "boundary_refinement": BOUNDARY_REFINEMENT_BATCH_VERSION,
                "boundary_refinement_backend": "fake",
            },
        }
    )
    refined_map = SessionMap.model_validate(refined_map.model_dump(mode="json"))
    refined_map = derive_clip_boundaries(
        refined_map,
        source.duration_ms,
        config.media.extraction,
    )
    refined_map = SessionMap.model_validate(refined_map.model_dump(mode="json"))
    final_by_id = {candidate.candidate_id: candidate for candidate in refined_map.candidates}

    item_artifacts = [
        BoundaryRefinementBatchItemArtifact(
            candidate_id=original.candidate_id,
            request_fingerprint=result.request.request_fingerprint,
            response_status=result.response.status,
            response_confidence=result.response.confidence,
            original_candidate_sha256=canonical_payload_sha256(original.model_dump(mode="json")),
            output_candidate_sha256=canonical_payload_sha256(
                final_by_id[original.candidate_id].model_dump(mode="json")
            ),
            boundary_changed=(
                result.candidate.event_start_ms != original.event_start_ms
                or result.candidate.event_end_ms != original.event_end_ms
            ),
            media_artifact_path=media.artifact_path.relative_to(paths.root).as_posix(),
            response_artifact_path=result.response_path.relative_to(paths.root).as_posix(),
        )
        for original, media, result in executions
    ]

    output_dir = paths.scout_dir / "boundary_refinement"
    output_dir.mkdir(parents=True, exist_ok=True)
    refined_map_path = output_dir / "session_map.refined.fake.json"
    batch_artifact_path = output_dir / "batch.fake.json"
    input_map_sha = canonical_payload_sha256(session_map.model_dump(mode="json"))
    refined_map_sha = canonical_payload_sha256(refined_map.model_dump(mode="json"))
    artifact = BoundaryRefinementBatchArtifact(
        session_id=session_map.session_id,
        source_id=session_map.source_id,
        input_session_map_sha256=input_map_sha,
        selected_candidate_ids=selected_ids,
        minimum_confidence=minimum_confidence,
        items=tuple(item_artifacts),
        refined_session_map_sha256=refined_map_sha,
    )
    atomic_write_json(refined_map_path, model_json(refined_map))
    atomic_write_json(batch_artifact_path, model_json(artifact))
    return BoundaryRefinementBatchResult(
        artifact_path=batch_artifact_path,
        refined_session_map_path=refined_map_path,
        artifact=artifact,
        session_map=refined_map,
        media_cache_hits=media_cache_hits,
        response_cache_hits=response_cache_hits,
        generated_responses=generated_responses,
    )


def _validate_candidate_selection(
    session_map: SessionMap,
    candidate_ids: Sequence[str],
) -> tuple[str, ...]:
    selected = tuple(candidate_ids)
    if not selected:
        raise ValidationError("boundary refinement batch requires explicit candidate IDs")
    if len(selected) > MAX_BOUNDARY_REFINEMENT_BATCH_CANDIDATES:
        raise ValidationError(
            "boundary refinement batch exceeds the maximum candidate count "
            f"({MAX_BOUNDARY_REFINEMENT_BATCH_CANDIDATES})"
        )
    if len(set(selected)) != len(selected):
        raise ValidationError("boundary refinement batch candidate IDs must be unique")
    known = {candidate.candidate_id for candidate in session_map.candidates}
    unknown = [candidate_id for candidate_id in selected if candidate_id not in known]
    if unknown:
        raise ValidationError(
            "boundary refinement batch references unknown candidates: " + ", ".join(unknown[:8])
        )
    return selected
