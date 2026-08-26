"""Aggregate preflight and injected-transport Gemini boundary-refiner batch execution."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from game_highlight_finder.config import AppConfig
from game_highlight_finder.cost.service import CostService
from game_highlight_finder.domain.models import (
    Candidate,
    SessionMap,
    Sha256,
    SourceAsset,
    model_json,
)
from game_highlight_finder.domain.reconcile import derive_clip_boundaries
from game_highlight_finder.errors import BudgetExceededError, ValidationError
from game_highlight_finder.pipeline.boundary_refinement import (
    BoundaryRefinementMediaResult,
    prepare_boundary_refinement_media,
)
from game_highlight_finder.pipeline.boundary_refinement_batch import (
    MAX_BOUNDARY_REFINEMENT_BATCH_CANDIDATES,
)
from game_highlight_finder.pipeline.boundary_refiner import canonical_payload_sha256
from game_highlight_finder.pipeline.boundary_refiner_gemini import (
    GeminiBoundaryRefinementPreflight,
    GeminiBoundaryRefinementResult,
    build_gemini_boundary_refinement_cost_service,
    preflight_gemini_boundary_refinement,
    run_gemini_boundary_refinement_with_transport,
)
from game_highlight_finder.pipeline.proxy import ProxyResult
from game_highlight_finder.providers.gemini import GeminiRemoteFile, GeminiTransport
from game_highlight_finder.storage.atomic import atomic_write_json
from game_highlight_finder.storage.sessions import session_paths

BOUNDARY_REFINER_GEMINI_BATCH_PREFLIGHT_VERSION = "boundary-refiner-gemini-batch-preflight-v1"
BOUNDARY_REFINER_GEMINI_BATCH_VERSION = "boundary-refiner-gemini-batch-v1"

GeminiBoundaryRefinementTransportFactory = Callable[[], GeminiTransport]


class _LazyGeminiTransport:
    """Create the real transport only when provider I/O is actually needed."""

    def __init__(self, factory: GeminiBoundaryRefinementTransportFactory) -> None:
        self._factory = factory
        self._transport: GeminiTransport | None = None

    def _get(self) -> GeminiTransport:
        if self._transport is None:
            self._transport = self._factory()
        return self._transport

    def upload(self, path: Path, *, mime_type: str) -> GeminiRemoteFile:
        return self._get().upload(path, mime_type=mime_type)

    def get_file(self, name: str) -> GeminiRemoteFile:
        return self._get().get_file(name)

    def create_interaction(
        self,
        *,
        model: str,
        remote_uri: str,
        prompt: str,
        response_schema: Mapping[str, Any],
        media_resolution: str,
        max_output_tokens: int,
        thinking_level: str | None,
        store: bool,
    ) -> Any:
        return self._get().create_interaction(
            model=model,
            remote_uri=remote_uri,
            prompt=prompt,
            response_schema=response_schema,
            media_resolution=media_resolution,
            max_output_tokens=max_output_tokens,
            thinking_level=thinking_level,
            store=store,
        )

    def delete_file(self, name: str) -> None:
        self._get().delete_file(name)


class GeminiBoundaryRefinementBatchPreflightItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(pattern=r"^cand_[0-9a-f]{16}$")
    preflight: GeminiBoundaryRefinementPreflight


class GeminiBoundaryRefinementBatchPreflight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = BOUNDARY_REFINER_GEMINI_BATCH_PREFLIGHT_VERSION
    session_id: str = Field(min_length=1, max_length=128)
    selected_candidate_ids: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_BOUNDARY_REFINEMENT_BATCH_CANDIDATES,
    )
    items: tuple[GeminiBoundaryRefinementBatchPreflightItem, ...]
    total_base_cost_micro_thb: int = Field(ge=0)
    total_reserved_cost_micro_thb: int = Field(ge=0)
    available_micro_thb: int = Field(ge=0)

    @model_validator(mode="after")
    def item_order_matches_selection(self) -> GeminiBoundaryRefinementBatchPreflight:
        if tuple(item.candidate_id for item in self.items) != self.selected_candidate_ids:
            raise ValueError(
                "Gemini boundary-refiner batch items must match selected candidate order"
            )
        return self


class GeminiBoundaryRefinementBatchItemArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(pattern=r"^cand_[0-9a-f]{16}$")
    call_id: str = Field(min_length=1, max_length=128)
    provider_request_fingerprint: Sha256
    boundary_request_fingerprint: Sha256
    response_status: Literal["REFINED", "UNCERTAIN"]
    response_confidence: float = Field(ge=0, le=1)
    original_candidate_sha256: Sha256
    output_candidate_sha256: Sha256
    boundary_changed: bool
    cache_hit: bool
    media_artifact_path: str = Field(min_length=1, max_length=1000)
    response_artifact_path: str = Field(min_length=1, max_length=1000)


class GeminiBoundaryRefinementBatchArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    version: str = BOUNDARY_REFINER_GEMINI_BATCH_VERSION
    backend: Literal["gemini"] = "gemini"
    execution_mode: Literal["injected_transport"] = "injected_transport"
    session_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(pattern=r"^src_[0-9a-f]{16}$")
    input_session_map_sha256: Sha256
    selected_candidate_ids: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_BOUNDARY_REFINEMENT_BATCH_CANDIDATES,
    )
    minimum_confidence: float = Field(ge=0, le=1)
    preflight_total_reserved_cost_micro_thb: int = Field(ge=0)
    items: tuple[GeminiBoundaryRefinementBatchItemArtifact, ...]
    refined_session_map_sha256: Sha256

    @model_validator(mode="after")
    def item_order_matches_selection(self) -> GeminiBoundaryRefinementBatchArtifact:
        if tuple(item.candidate_id for item in self.items) != self.selected_candidate_ids:
            raise ValueError(
                "Gemini boundary-refiner batch artifact items must match selected candidate order"
            )
        return self


class GeminiBoundaryRefinementBatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    artifact_path: Path
    refined_session_map_path: Path
    artifact: GeminiBoundaryRefinementBatchArtifact
    session_map: SessionMap
    preflight: GeminiBoundaryRefinementBatchPreflight
    media_cache_hits: int = Field(ge=0)
    response_cache_hits: int = Field(ge=0)
    generated_responses: int = Field(ge=0)


def preflight_gemini_boundary_refinement_batch(
    items: Sequence[tuple[Candidate, BoundaryRefinementMediaResult]],
    config: AppConfig,
    *,
    session_id: str,
    cost_service: CostService,
) -> GeminiBoundaryRefinementBatchPreflight:
    """Quote a whole explicit candidate batch without reservation, upload, or generation."""

    selected = tuple(items)
    if not selected:
        raise ValidationError("Gemini boundary-refiner batch preflight requires explicit items")
    if len(selected) > MAX_BOUNDARY_REFINEMENT_BATCH_CANDIDATES:
        raise ValidationError(
            "Gemini boundary-refiner batch preflight exceeds the maximum candidate count "
            f"({MAX_BOUNDARY_REFINEMENT_BATCH_CANDIDATES})"
        )
    candidate_ids = tuple(candidate.candidate_id for candidate, _ in selected)
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValidationError("Gemini boundary-refiner batch candidate IDs must be unique")

    preflight_items: list[GeminiBoundaryRefinementBatchPreflightItem] = []
    for candidate, media in selected:
        preflight = preflight_gemini_boundary_refinement(
            media,
            candidate,
            config,
            session_id=session_id,
            cost_service=cost_service,
        )
        preflight_items.append(
            GeminiBoundaryRefinementBatchPreflightItem(
                candidate_id=candidate.candidate_id,
                preflight=preflight,
            )
        )

    total_base = sum(item.preflight.quote.base_cost_micro_thb for item in preflight_items)
    total_reserved = sum(item.preflight.quote.reserved_cost_micro_thb for item in preflight_items)
    available = preflight_items[0].preflight.available_micro_thb
    if total_reserved > available:
        raise BudgetExceededError(
            hint=(
                f"Gemini boundary-refiner batch requires {total_reserved} micro-THB, "
                f"but only {available} is available."
            )
        )

    return GeminiBoundaryRefinementBatchPreflight(
        session_id=session_id,
        selected_candidate_ids=candidate_ids,
        items=tuple(preflight_items),
        total_base_cost_micro_thb=total_base,
        total_reserved_cost_micro_thb=total_reserved,
        available_micro_thb=available,
    )


def preflight_gemini_boundary_refinement_session_batch(
    source: SourceAsset,
    proxy: ProxyResult,
    session_map: SessionMap,
    config: AppConfig,
    *,
    candidate_ids: Sequence[str],
    cost_service: CostService | None = None,
) -> GeminiBoundaryRefinementBatchPreflight:
    """Prepare local media and quote a selected session batch without provider I/O."""

    selected_ids = _validate_batch_context(source, proxy, session_map, candidate_ids)
    prepared, _ = _prepare_batch(source, proxy, session_map, config, selected_ids)
    service = cost_service or build_gemini_boundary_refinement_cost_service(config)
    return preflight_gemini_boundary_refinement_batch(
        prepared,
        config,
        session_id=session_map.session_id,
        cost_service=service,
    )


def run_gemini_boundary_refinement_batch_with_transport_factory(
    source: SourceAsset,
    proxy: ProxyResult,
    session_map: SessionMap,
    config: AppConfig,
    *,
    candidate_ids: Sequence[str],
    transport_factory: GeminiBoundaryRefinementTransportFactory,
    cost_service: CostService | None = None,
    minimum_confidence: float = 0.5,
) -> GeminiBoundaryRefinementBatchResult:
    """Run a selected batch after aggregate preflight with lazily-created provider transport."""

    selected_ids = _validate_batch_context(source, proxy, session_map, candidate_ids)
    if not config.scout.allow_remote_upload:
        raise ValidationError(
            "Gemini boundary-refiner batch requires explicit remote-upload opt-in"
        )
    if not 0 <= minimum_confidence <= 1:
        raise ValidationError("boundary refinement minimum confidence must be between 0 and 1")

    prepared, media_cache_hits = _prepare_batch(source, proxy, session_map, config, selected_ids)
    service = cost_service or build_gemini_boundary_refinement_cost_service(config)
    preflight = preflight_gemini_boundary_refinement_batch(
        prepared,
        config,
        session_id=session_map.session_id,
        cost_service=service,
    )
    lazy_transport = _LazyGeminiTransport(transport_factory)
    transports: Mapping[str, GeminiTransport] = {
        candidate_id: lazy_transport for candidate_id in selected_ids
    }
    return _execute_prepared_batch(
        source,
        session_map,
        config,
        selected_ids=selected_ids,
        prepared=prepared,
        transports=transports,
        cost_service=service,
        preflight=preflight,
        media_cache_hits=media_cache_hits,
        minimum_confidence=minimum_confidence,
    )


def run_gemini_boundary_refinement_batch_with_transports(
    source: SourceAsset,
    proxy: ProxyResult,
    session_map: SessionMap,
    config: AppConfig,
    *,
    candidate_ids: Sequence[str],
    transports: Mapping[str, GeminiTransport],
    cost_service: CostService | None = None,
    minimum_confidence: float = 0.5,
) -> GeminiBoundaryRefinementBatchResult:
    """Run an explicit bounded batch using caller-injected transports only."""

    selected_ids = _validate_batch_context(source, proxy, session_map, candidate_ids)
    if not config.scout.allow_remote_upload:
        raise ValidationError(
            "Gemini boundary-refiner batch requires explicit remote-upload opt-in"
        )
    if not 0 <= minimum_confidence <= 1:
        raise ValidationError("boundary refinement minimum confidence must be between 0 and 1")
    missing_transports = [
        candidate_id for candidate_id in selected_ids if candidate_id not in transports
    ]
    if missing_transports:
        raise ValidationError(
            "Gemini boundary-refiner batch is missing injected transports for: "
            + ", ".join(missing_transports[:8])
        )

    prepared, media_cache_hits = _prepare_batch(source, proxy, session_map, config, selected_ids)
    service = cost_service or build_gemini_boundary_refinement_cost_service(config)
    preflight = preflight_gemini_boundary_refinement_batch(
        prepared,
        config,
        session_id=session_map.session_id,
        cost_service=service,
    )
    return _execute_prepared_batch(
        source,
        session_map,
        config,
        selected_ids=selected_ids,
        prepared=prepared,
        transports=transports,
        cost_service=service,
        preflight=preflight,
        media_cache_hits=media_cache_hits,
        minimum_confidence=minimum_confidence,
    )


def _validate_batch_context(
    source: SourceAsset,
    proxy: ProxyResult,
    session_map: SessionMap,
    candidate_ids: Sequence[str],
) -> tuple[str, ...]:
    selected_ids = _validate_candidate_selection(session_map, candidate_ids)
    if source.source_id != session_map.source_id:
        raise ValidationError("Gemini boundary-refiner batch source does not match the session map")
    if proxy.session_id != session_map.session_id:
        raise ValidationError("Gemini boundary-refiner batch proxy does not match the session map")
    if source.duration_ms != session_map.duration_ms:
        raise ValidationError(
            "Gemini boundary-refiner batch duration does not match the session map"
        )
    return selected_ids


def _prepare_batch(
    source: SourceAsset,
    proxy: ProxyResult,
    session_map: SessionMap,
    config: AppConfig,
    selected_ids: Sequence[str],
) -> tuple[tuple[tuple[Candidate, BoundaryRefinementMediaResult], ...], int]:
    by_id = {candidate.candidate_id: candidate for candidate in session_map.candidates}
    prepared: list[tuple[Candidate, BoundaryRefinementMediaResult]] = []
    media_cache_hits = 0
    for candidate_id in selected_ids:
        candidate = by_id[candidate_id]
        media = prepare_boundary_refinement_media(source, proxy, candidate, config)
        media_cache_hits += int(media.cache_hit)
        prepared.append((candidate, media))
    return tuple(prepared), media_cache_hits


def _execute_prepared_batch(
    source: SourceAsset,
    session_map: SessionMap,
    config: AppConfig,
    *,
    selected_ids: Sequence[str],
    prepared: Sequence[tuple[Candidate, BoundaryRefinementMediaResult]],
    transports: Mapping[str, GeminiTransport],
    cost_service: CostService,
    preflight: GeminiBoundaryRefinementBatchPreflight,
    media_cache_hits: int,
    minimum_confidence: float,
) -> GeminiBoundaryRefinementBatchResult:
    paths = session_paths(config.storage.data_dir, session_map.session_id)
    refined_by_id: dict[str, Candidate] = {}
    executions: list[
        tuple[Candidate, BoundaryRefinementMediaResult, GeminiBoundaryRefinementResult]
    ] = []
    response_cache_hits = 0
    generated_responses = 0
    for candidate, media in prepared:
        result = run_gemini_boundary_refinement_with_transport(
            media,
            candidate,
            config,
            session_id=session_map.session_id,
            transport=transports[candidate.candidate_id],
            cost_service=cost_service,
            minimum_confidence=minimum_confidence,
        )
        response_cache_hits += int(result.cache_hit)
        generated_responses += int(not result.cache_hit)
        refined_by_id[candidate.candidate_id] = result.candidate
        executions.append((candidate, media, result))

    updated_candidates = [
        refined_by_id.get(candidate.candidate_id, candidate) for candidate in session_map.candidates
    ]
    refined_map = session_map.model_copy(
        update={
            "candidates": updated_candidates,
            "scout_metadata": {
                **session_map.scout_metadata,
                "boundary_refinement": BOUNDARY_REFINER_GEMINI_BATCH_VERSION,
                "boundary_refinement_backend": "gemini",
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
    preflight_by_id = {item.candidate_id: item.preflight for item in preflight.items}

    item_artifacts = [
        GeminiBoundaryRefinementBatchItemArtifact(
            candidate_id=original.candidate_id,
            call_id=result.call_id,
            provider_request_fingerprint=preflight_by_id[
                original.candidate_id
            ].provider_request_fingerprint,
            boundary_request_fingerprint=result.request.request_fingerprint,
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
            cache_hit=result.cache_hit,
            media_artifact_path=media.artifact_path.relative_to(paths.root).as_posix(),
            response_artifact_path=result.response_path.relative_to(paths.root).as_posix(),
        )
        for original, media, result in executions
    ]

    output_dir = paths.scout_dir / "boundary_refinement"
    output_dir.mkdir(parents=True, exist_ok=True)
    refined_map_path = output_dir / "session_map.refined.gemini.json"
    batch_artifact_path = output_dir / "batch.gemini.json"
    input_map_sha = canonical_payload_sha256(session_map.model_dump(mode="json"))
    refined_map_sha = canonical_payload_sha256(refined_map.model_dump(mode="json"))
    artifact = GeminiBoundaryRefinementBatchArtifact(
        session_id=session_map.session_id,
        source_id=session_map.source_id,
        input_session_map_sha256=input_map_sha,
        selected_candidate_ids=tuple(selected_ids),
        minimum_confidence=minimum_confidence,
        preflight_total_reserved_cost_micro_thb=preflight.total_reserved_cost_micro_thb,
        items=tuple(item_artifacts),
        refined_session_map_sha256=refined_map_sha,
    )
    atomic_write_json(refined_map_path, model_json(refined_map))
    atomic_write_json(batch_artifact_path, model_json(artifact))
    return GeminiBoundaryRefinementBatchResult(
        artifact_path=batch_artifact_path,
        refined_session_map_path=refined_map_path,
        artifact=artifact,
        session_map=refined_map,
        preflight=preflight,
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
        raise ValidationError("Gemini boundary-refiner batch requires explicit candidate IDs")
    if len(selected) > MAX_BOUNDARY_REFINEMENT_BATCH_CANDIDATES:
        raise ValidationError(
            "Gemini boundary-refiner batch exceeds the maximum candidate count "
            f"({MAX_BOUNDARY_REFINEMENT_BATCH_CANDIDATES})"
        )
    if len(set(selected)) != len(selected):
        raise ValidationError("Gemini boundary-refiner batch candidate IDs must be unique")
    known = {candidate.candidate_id for candidate in session_map.candidates}
    unknown = [candidate_id for candidate_id in selected if candidate_id not in known]
    if unknown:
        raise ValidationError(
            "Gemini boundary-refiner batch references unknown candidates: " + ", ".join(unknown[:8])
        )
    return selected


__all__ = [
    "BOUNDARY_REFINER_GEMINI_BATCH_PREFLIGHT_VERSION",
    "BOUNDARY_REFINER_GEMINI_BATCH_VERSION",
    "GeminiBoundaryRefinementBatchArtifact",
    "GeminiBoundaryRefinementBatchItemArtifact",
    "GeminiBoundaryRefinementBatchPreflight",
    "GeminiBoundaryRefinementBatchPreflightItem",
    "GeminiBoundaryRefinementBatchResult",
    "GeminiBoundaryRefinementTransportFactory",
    "preflight_gemini_boundary_refinement_batch",
    "preflight_gemini_boundary_refinement_session_batch",
    "run_gemini_boundary_refinement_batch_with_transport_factory",
    "run_gemini_boundary_refinement_batch_with_transports",
]
