"""Gemini boundary-refiner provider boundary with injected-transport execution only.

This module deliberately does not construct a network transport. Callers must inject a
``GeminiTransport``; offline tests use ``FakeGeminiTransport``. The boundary reuses the M4
cost lifecycle and the existing Gemini adapter while permitting only the hash-verified slowed
candidate proxy to cross the upload seam.
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from game_highlight_finder.config import AppConfig
from game_highlight_finder.cost.fx import FxSnapshot
from game_highlight_finder.cost.models import CostQuote
from game_highlight_finder.cost.production import production_pricing_catalog
from game_highlight_finder.cost.service import CostRequest, CostService
from game_highlight_finder.domain.models import Candidate, Sha256
from game_highlight_finder.errors import (
    BudgetExceededError,
    CostGateError,
    CostSafetyHoldError,
    ValidationError,
)
from game_highlight_finder.pipeline.boundary_refinement import (
    BoundaryRefinementMediaResult,
    BoundaryRefinementResponse,
    apply_boundary_refinement,
)
from game_highlight_finder.pipeline.boundary_refiner import (
    BoundaryRefinementRequestArtifact,
    build_boundary_refinement_request,
    canonical_payload_sha256,
    parse_boundary_refinement_response,
)
from game_highlight_finder.pipeline.gemini_scout import (
    build_gemini_registry,
    effective_gemini_media_resolution,
    effective_gemini_thinking,
    estimate_gemini_usage,
)
from game_highlight_finder.providers.base import ProviderRequest, ProviderUsageEstimate
from game_highlight_finder.providers.gemini import (
    GeminiInteractionEnvelope,
    GeminiProvider,
    GeminiProviderError,
    GeminiTransport,
    usage_from_envelope,
)
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.hashing import hash_file
from game_highlight_finder.storage.sessions import session_paths

BOUNDARY_REFINER_GEMINI_VERSION = "boundary-refiner-gemini-v1"
BOUNDARY_REFINER_MAX_OUTPUT_TOKENS = 512


class GeminiBoundaryRefinementPreflight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = BOUNDARY_REFINER_GEMINI_VERSION
    provider: Literal["gemini"] = "gemini"
    model: str
    billing_mode: str
    session_id: str = Field(min_length=1, max_length=128)
    boundary_request_fingerprint: Sha256
    provider_request_fingerprint: Sha256
    media_sha256: Sha256
    media_resolution: str
    thinking_level: str | None
    usage_estimate: ProviderUsageEstimate
    quote: CostQuote
    available_micro_thb: int = Field(ge=0)


class GeminiBoundaryRefinementResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    cache_hit: bool
    session_id: str = Field(min_length=1, max_length=128)
    request_meta_path: Path
    response_path: Path
    remote_metadata_path: Path
    cost_path: Path
    request: BoundaryRefinementRequestArtifact
    response: BoundaryRefinementResponse
    candidate: Candidate
    call_id: str


def preflight_gemini_boundary_refinement(
    media: BoundaryRefinementMediaResult,
    candidate: Candidate,
    config: AppConfig,
    *,
    session_id: str,
    cost_service: CostService | None = None,
) -> GeminiBoundaryRefinementPreflight:
    """Quote one candidate refinement without reservation, upload, or generation."""

    request, cost_request = _request_parts(
        media,
        candidate,
        config,
        session_id=session_id,
    )
    service = cost_service or build_gemini_boundary_refinement_cost_service(config)
    quote = service.quote(cost_request)
    summary = service.summary()
    if summary.safety_hold_active:
        raise CostSafetyHoldError(
            "Cost safety hold is active; boundary-refiner preflight is blocked.",
            hint=summary.safety_hold_reason,
        )
    if quote.reserved_cost_micro_thb > summary.available_micro_thb:
        raise BudgetExceededError(
            hint=(
                f"Boundary refinement requires {quote.reserved_cost_micro_thb} micro-THB, "
                f"but only {summary.available_micro_thb} is available."
            )
        )
    thinking = effective_gemini_thinking(config)
    return GeminiBoundaryRefinementPreflight(
        model=config.scout.model,
        billing_mode=config.scout.billing_mode,
        session_id=session_id,
        boundary_request_fingerprint=request.request_fingerprint,
        provider_request_fingerprint=cost_request.request_fingerprint,
        media_sha256=request.media_sha256,
        media_resolution=config.scout.media_resolution,
        thinking_level=thinking.wire_level,
        usage_estimate=cost_request.usage_estimate,
        quote=quote,
        available_micro_thb=summary.available_micro_thb,
    )


def run_gemini_boundary_refinement_with_transport(
    media: BoundaryRefinementMediaResult,
    candidate: Candidate,
    config: AppConfig,
    *,
    session_id: str,
    transport: GeminiTransport,
    cost_service: CostService | None = None,
    minimum_confidence: float = 0.5,
    force: bool = False,
) -> GeminiBoundaryRefinementResult:
    """Exercise the Gemini provider lifecycle using only an explicitly injected transport."""

    if not config.scout.allow_remote_upload:
        raise ValidationError("Gemini boundary refinement requires explicit remote-upload opt-in.")
    if not 0 <= minimum_confidence <= 1:
        raise ValidationError("boundary refinement minimum confidence must be between 0 and 1")

    boundary_request, cost_request = _request_parts(
        media,
        candidate,
        config,
        session_id=session_id,
    )
    service = cost_service or build_gemini_boundary_refinement_cost_service(config)
    call_id = cost_request.call_id
    item_dir = media.artifact_path.parent
    request_meta_path = item_dir / "request.gemini.json"
    response_path = item_dir / "response.gemini.json"
    remote_metadata_path = item_dir / "remote.gemini.json"
    cost_path = item_dir / "cost.gemini.json"

    if not force:
        cached = _load_cached_response(
            request_meta_path,
            response_path,
            service,
            call_id=call_id,
            session_id=session_id,
            provider_request_fingerprint=cost_request.request_fingerprint,
            boundary_request=boundary_request,
        )
        if cached is not None:
            refined = apply_boundary_refinement(
                candidate,
                boundary_request.plan,
                cached,
                minimum_confidence=minimum_confidence,
            )
            return GeminiBoundaryRefinementResult(
                cache_hit=True,
                session_id=session_id,
                request_meta_path=request_meta_path,
                response_path=response_path,
                remote_metadata_path=remote_metadata_path,
                cost_path=cost_path,
                request=boundary_request,
                response=cached,
                candidate=refined,
                call_id=call_id,
            )

    existing = _existing_call(service, call_id)
    if existing is not None:
        state = existing.status.value
        if state in {"RESERVED", "IN_FLIGHT", "AMBIGUOUS"}:
            raise ValidationError(
                "A previous Gemini boundary-refiner call has an unresolved cost lifecycle state."
            )
        if state == "SETTLED":
            raise ValidationError(
                "A settled Gemini boundary-refiner call has no reusable response artifact."
            )
        if state == "RELEASED":
            raise ValidationError(
                "A released Gemini boundary-refiner call requires an explicit changed "
                "request identity."
            )

    atomic_write_json(
        request_meta_path,
        {
            "version": BOUNDARY_REFINER_GEMINI_VERSION,
            "execution_mode": "injected_transport",
            "provider": "gemini",
            "model": config.scout.model,
            "billing_mode": config.scout.billing_mode,
            "session_id": session_id,
            "call_id": call_id,
            "boundary_request_fingerprint": boundary_request.request_fingerprint,
            "provider_request_fingerprint": cost_request.request_fingerprint,
            "media_sha256": boundary_request.media_sha256,
            "usage_estimate": cost_request.usage_estimate.model_dump(mode="json"),
        },
    )

    provider = GeminiProvider(
        transport=transport,
        api_key_env=config.scout.api_key_env,
        readiness_timeout_seconds=config.scout.readiness_timeout_seconds,
        readiness_poll_initial_seconds=config.scout.readiness_poll_initial_seconds,
        readiness_poll_max_seconds=config.scout.readiness_poll_max_seconds,
        cleanup_retry_limit=config.scout.cleanup_retry_limit,
    )
    thinking = effective_gemini_thinking(config)
    reserved = False
    in_flight = False

    def mark_in_flight() -> None:
        nonlocal in_flight
        service.mark_in_flight(call_id)
        in_flight = True

    def validate_slowed_proxy(path: Path) -> None:
        _validate_boundary_refiner_upload(path, media)

    try:
        record = service.reserve(cost_request)
        reserved = record.status.value in {"RESERVED", "IN_FLIGHT"}
        result = provider.execute(
            ProviderRequest(
                call_id=call_id,
                provider="gemini",
                model_id=config.scout.model,
                billing_mode=config.scout.billing_mode,
                stage="boundary_refiner",
                session_id=session_id,
                usage_estimate=cost_request.usage_estimate,
                request_payload={
                    **cost_request.request_payload,
                    "response_max_bytes": config.scout.response_max_bytes,
                },
            ),
            proxy_path=media.slowed_proxy_path,
            session_proxy_root=media.artifact_path.parent,
            prompt=boundary_request.prompt,
            response_schema=boundary_request.response_schema,
            media_resolution=config.scout.media_resolution,
            max_output_tokens=BOUNDARY_REFINER_MAX_OUTPUT_TOKENS,
            thinking_level=thinking.wire_level,
            remote_metadata_path=remote_metadata_path,
            before_generation=mark_in_flight,
            upload_validator=validate_slowed_proxy,
        )
        envelope = GeminiInteractionEnvelope.model_validate(result.result)
        response = parse_boundary_refinement_response(
            envelope.output_text,
            boundary_request.plan,
        )
        atomic_write_json(
            response_path,
            {
                "version": BOUNDARY_REFINER_GEMINI_VERSION,
                "backend": "gemini",
                "execution_mode": "injected_transport",
                "session_id": session_id,
                "provider_request_fingerprint": cost_request.request_fingerprint,
                "boundary_request_fingerprint": boundary_request.request_fingerprint,
                "envelope": envelope.model_dump(mode="json"),
                "response": response.model_dump(mode="json"),
            },
        )
        try:
            service.settle(
                call_id,
                result.usage,
                provider_request_id=result.provider_request_id,
            )
        finally:
            _write_cost_artifact(service, call_id, cost_path)
        refined = apply_boundary_refinement(
            candidate,
            boundary_request.plan,
            response,
            minimum_confidence=minimum_confidence,
        )
        return GeminiBoundaryRefinementResult(
            cache_hit=False,
            session_id=session_id,
            request_meta_path=request_meta_path,
            response_path=response_path,
            remote_metadata_path=remote_metadata_path,
            cost_path=cost_path,
            request=boundary_request,
            response=response,
            candidate=refined,
            call_id=call_id,
        )
    except GeminiProviderError as exc:
        if exc.may_have_dispatched or in_flight:
            with suppress(Exception):
                service.mark_ambiguous(call_id, str(exc))
        elif reserved:
            with suppress(Exception):
                service.release(call_id)
        with suppress(Exception):
            _write_cost_artifact(service, call_id, cost_path)
        raise ValidationError(
            str(exc),
            hint="No automatic Gemini boundary-refiner generation retry was attempted.",
        ) from exc
    except BaseException:
        if in_flight:
            with suppress(Exception):
                service.mark_ambiguous(call_id, "local-post-dispatch-failure")
        elif reserved:
            with suppress(Exception):
                service.release(call_id)
        with suppress(Exception):
            _write_cost_artifact(service, call_id, cost_path)
        raise


def _request_parts(
    media: BoundaryRefinementMediaResult,
    candidate: Candidate,
    config: AppConfig,
    *,
    session_id: str,
) -> tuple[BoundaryRefinementRequestArtifact, CostRequest]:
    normalized_session_id = session_id.strip()
    if normalized_session_id != session_id:
        raise ValidationError("boundary-refiner session_id must already be normalized")
    paths = session_paths(config.storage.data_dir, normalized_session_id)
    expected_item_dir = paths.scout_dir / "boundary_refinement" / candidate.candidate_id
    _validate_session_media_binding(media, expected_item_dir)

    boundary_request = build_boundary_refinement_request(media, candidate)
    thinking = effective_gemini_thinking(config)
    media_config = effective_gemini_media_resolution(config)
    estimate = estimate_gemini_usage(
        duration_ms=media.artifact.slowed_proxy_duration_ms,
        prompt=boundary_request.prompt,
        response_schema=boundary_request.response_schema,
        audio_present=media.artifact.audio_present,
        max_output_tokens=BOUNDARY_REFINER_MAX_OUTPUT_TOKENS,
        reserved_thinking_tokens=thinking.reserved_thinking_tokens,
        model=config.scout.model,
        media_resolution=config.scout.media_resolution,
    )
    semantic_payload = {
        "version": BOUNDARY_REFINER_GEMINI_VERSION,
        "boundary_request_fingerprint": boundary_request.request_fingerprint,
        "candidate_sha256": boundary_request.candidate_sha256,
        "media_sha256": boundary_request.media_sha256,
        "plan_sha256": canonical_payload_sha256(boundary_request.plan.model_dump(mode="json")),
        "response_schema_sha256": canonical_payload_sha256(boundary_request.response_schema),
        "prompt_sha256": canonical_payload_sha256(boundary_request.prompt),
        "media_resolution": config.scout.media_resolution,
        "wire_media_resolution": media_config.wire_level,
        "thinking_level": thinking.wire_level,
        "thinking_policy": thinking.policy,
        "max_output_tokens": BOUNDARY_REFINER_MAX_OUTPUT_TOKENS,
    }
    fingerprint_seed = CostRequest(
        call_id="boundary-refiner-fingerprint",
        provider="gemini",
        model=config.scout.model,
        billing_mode=config.scout.billing_mode,
        stage="boundary_refiner",
        session_id=normalized_session_id,
        usage_estimate=estimate,
        request_payload=semantic_payload,
    )
    call_id = f"gemini-boundary-{fingerprint_seed.request_fingerprint[:48]}"
    return boundary_request, fingerprint_seed.model_copy(update={"call_id": call_id})


def _validate_session_media_binding(
    media: BoundaryRefinementMediaResult,
    expected_item_dir: Path,
) -> None:
    try:
        expected_artifact = (expected_item_dir / "artifact.json").resolve()
        expected_slowed = (expected_item_dir / "slowed.mp4").resolve()
        actual_artifact = media.artifact_path.resolve()
        actual_slowed = media.slowed_proxy_path.resolve()
    except OSError as exc:
        raise ValidationError("Cannot resolve boundary-refiner session media paths") from exc
    if actual_artifact != expected_artifact or actual_slowed != expected_slowed:
        raise ValidationError("boundary-refiner media does not belong to the requested session")


def _validate_boundary_refiner_upload(
    path: Path,
    media: BoundaryRefinementMediaResult,
) -> None:
    try:
        resolved = path.resolve()
        expected = media.slowed_proxy_path.resolve()
    except OSError as exc:
        raise ValidationError("Cannot resolve boundary-refiner upload artifact") from exc
    if resolved != expected or resolved.name != "slowed.mp4" or not resolved.is_file():
        raise ValidationError("Gemini boundary refiner may upload only the prepared slowed.mp4")
    if hash_file(resolved) != media.artifact.slowed_proxy_sha256:
        raise ValidationError("Gemini boundary-refiner upload hash does not match provenance")


def _load_cached_response(
    request_meta_path: Path,
    response_path: Path,
    service: CostService,
    *,
    call_id: str,
    session_id: str,
    provider_request_fingerprint: str,
    boundary_request: BoundaryRefinementRequestArtifact,
) -> BoundaryRefinementResponse | None:
    if not request_meta_path.is_file() or not response_path.is_file():
        return None
    record = _existing_call(service, call_id)
    if record is None or record.status.value != "SETTLED":
        return None
    try:
        meta = read_json(request_meta_path)
        raw = read_json(response_path)
        if meta.get("session_id") != session_id:
            return None
        if meta.get("provider_request_fingerprint") != provider_request_fingerprint:
            return None
        if meta.get("boundary_request_fingerprint") != boundary_request.request_fingerprint:
            return None
        if raw.get("session_id") != session_id:
            return None
        if raw.get("provider_request_fingerprint") != provider_request_fingerprint:
            return None
        if raw.get("boundary_request_fingerprint") != boundary_request.request_fingerprint:
            return None
        envelope = GeminiInteractionEnvelope.model_validate(raw.get("envelope"))
        usage_from_envelope(envelope)
        response = parse_boundary_refinement_response(raw.get("response"), boundary_request.plan)
    except Exception:
        return None
    return response


def _existing_call(service: CostService, call_id: str):  # type: ignore[no-untyped-def]
    try:
        return service.ledger.get(call_id)
    except CostGateError:
        return None


def _write_cost_artifact(service: CostService, call_id: str, path: Path) -> None:
    record = service.ledger.get(call_id)
    atomic_write_json(
        path,
        {
            "provider": record.provider,
            "model": record.model,
            "billing_mode": record.billing_mode,
            "call_id": record.call_id,
            "state": record.status.value,
            "reserved_micro_thb": record.reserved_cost_micro_thb,
            "settled_micro_thb": record.settled_cost_micro_thb,
            "estimated_usage": record.estimated_usage.model_dump(mode="json"),
            "actual_usage": (
                record.actual_usage.model_dump(mode="json") if record.actual_usage else None
            ),
        },
    )


def build_gemini_boundary_refinement_cost_service(config: AppConfig) -> CostService:
    if config.cost.pricing_catalog_path is not None:
        return CostService.from_config(config, registry=build_gemini_registry())
    fx = (
        FxSnapshot.from_file(config.cost.fx_snapshot_path) if config.cost.fx_snapshot_path else None
    )
    return CostService(
        config,
        registry=build_gemini_registry(),
        pricing=production_pricing_catalog(),
        fx_snapshot=fx,
    )
