"""Gemini provider boundary for bounded hybrid proposal judging.

This module intentionally never constructs a real network transport. Production wiring can
inject one later only after explicit authorization; automated verification uses
``FakeGeminiTransport``. Aggregate preflight performs no ledger write, upload, or generation.
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from game_highlight_finder.config import AppConfig
from game_highlight_finder.cost.fx import FxSnapshot
from game_highlight_finder.cost.production import production_pricing_catalog
from game_highlight_finder.cost.service import CostRequest, CostService
from game_highlight_finder.domain.models import Candidate, Sha256
from game_highlight_finder.errors import (
    BudgetExceededError,
    CostGateError,
    CostSafetyHoldError,
    ValidationError,
)
from game_highlight_finder.pipeline.gemini_scout import (
    build_gemini_registry,
    effective_gemini_media_resolution,
    effective_gemini_thinking,
    estimate_gemini_usage,
)
from game_highlight_finder.pipeline.hybrid_judge import (
    HYBRID_JUDGE_VERSION,
    HybridJudgeRequestArtifact,
    HybridJudgeResponse,
    build_hybrid_judge_request,
    canonical_payload_sha256,
    parse_hybrid_judge_response,
    reconcile_hybrid_candidates,
    response_to_candidates,
)
from game_highlight_finder.pipeline.hybrid_proposals import (
    HybridProposalPreparation,
    PreparedHybridProposal,
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

HYBRID_JUDGE_GEMINI_VERSION = "hybrid-judge-gemini-v1"
HYBRID_JUDGE_GEMINI_API_VERSION = "v1"
HYBRID_JUDGE_GEMINI_API_SURFACE = "generate_content"
HYBRID_JUDGE_MAX_OUTPUT_TOKENS = 1_024


class GeminiHybridJudgeItemPreflight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_id: str = Field(pattern=r"^proposal_[0-9a-f]{16}$")
    call_id: str = Field(min_length=1, max_length=128)
    judge_request_fingerprint: Sha256
    provider_request_fingerprint: Sha256
    media_sha256: Sha256
    usage_estimate: ProviderUsageEstimate
    cache_hit: bool
    maximum_reserved_micro_thb: int = Field(ge=0)


class GeminiHybridJudgeBatchPreflight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["hybrid-judge-gemini-v1"] = "hybrid-judge-gemini-v1"
    provider: Literal["gemini"] = "gemini"
    model: str
    billing_mode: str
    session_id: str = Field(min_length=1, max_length=128)
    media_resolution: str
    thinking_level: str | None
    items: tuple[GeminiHybridJudgeItemPreflight, ...]
    planned_generation_calls: int = Field(ge=0)
    cache_hit_count: int = Field(ge=0)
    aggregate_maximum_reserved_micro_thb: int = Field(ge=0)
    available_micro_thb: int = Field(ge=0)
    post_reservation_headroom_micro_thb: int = Field(ge=0)
    provider_calls: Literal[0] = 0
    remote_uploads: Literal[0] = 0
    ledger_reservations: Literal[0] = 0


class GeminiHybridJudgeProposalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    cache_hit: bool
    session_id: str = Field(min_length=1, max_length=128)
    proposal_id: str = Field(pattern=r"^proposal_[0-9a-f]{16}$")
    call_id: str
    request_meta_path: Path
    response_path: Path
    remote_metadata_path: Path
    cost_path: Path
    request: HybridJudgeRequestArtifact
    response: HybridJudgeResponse
    candidates: tuple[Candidate, ...]


class GeminiHybridJudgeBatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    session_id: str = Field(min_length=1, max_length=128)
    item_results: tuple[GeminiHybridJudgeProposalResult, ...]
    candidates: tuple[Candidate, ...]
    generation_calls: int = Field(ge=0)
    cache_hits: int = Field(ge=0)


def preflight_gemini_hybrid_judge_batch(
    preparation: HybridProposalPreparation,
    config: AppConfig,
    *,
    cost_service: CostService | None = None,
) -> GeminiHybridJudgeBatchPreflight:
    """Quote all new proposal judgments without reservation, upload, or provider call."""

    service = cost_service or build_gemini_hybrid_judge_cost_service(config)
    parts = [_request_parts(preparation, item, config) for item in preparation.prepared]
    summary = service.summary()
    if summary.safety_hold_active:
        raise CostSafetyHoldError(
            "Cost safety hold is active; hybrid-judge preflight is blocked.",
            hint=summary.safety_hold_reason,
        )

    items: list[GeminiHybridJudgeItemPreflight] = []
    aggregate = 0
    cache_hits = 0
    for prepared, (request, cost_request) in zip(preparation.prepared, parts, strict=True):
        cache_hit = (
            _load_cached_response(
                prepared,
                service,
                cost_request=cost_request,
                judge_request=request,
            )
            is not None
        )
        if cache_hit:
            reserved = 0
            cache_hits += 1
        else:
            quote = service.quote(cost_request)
            reserved = quote.reserved_cost_micro_thb
            aggregate += reserved
        items.append(
            GeminiHybridJudgeItemPreflight(
                proposal_id=request.proposal_id,
                call_id=cost_request.call_id,
                judge_request_fingerprint=request.request_fingerprint,
                provider_request_fingerprint=cost_request.request_fingerprint,
                media_sha256=request.media_sha256,
                usage_estimate=cost_request.usage_estimate,
                cache_hit=cache_hit,
                maximum_reserved_micro_thb=reserved,
            )
        )

    if aggregate > summary.available_micro_thb:
        raise BudgetExceededError(
            hint=(
                f"Hybrid judge requires {aggregate} micro-THB of new reservation exposure, "
                f"but only {summary.available_micro_thb} is available."
            )
        )
    thinking = effective_gemini_thinking(config)
    return GeminiHybridJudgeBatchPreflight(
        model=config.scout.model,
        billing_mode=config.scout.billing_mode,
        session_id=preparation.plan.session_id,
        media_resolution=config.scout.media_resolution,
        thinking_level=thinking.wire_level,
        items=tuple(items),
        planned_generation_calls=len(items) - cache_hits,
        cache_hit_count=cache_hits,
        aggregate_maximum_reserved_micro_thb=aggregate,
        available_micro_thb=summary.available_micro_thb,
        post_reservation_headroom_micro_thb=summary.available_micro_thb - aggregate,
    )


def run_gemini_hybrid_judge_with_transport(
    preparation: HybridProposalPreparation,
    prepared: PreparedHybridProposal,
    config: AppConfig,
    *,
    transport: GeminiTransport,
    cost_service: CostService | None = None,
    force: bool = False,
) -> GeminiHybridJudgeProposalResult:
    """Run one proposal through an explicitly injected Gemini transport."""

    if not config.scout.allow_remote_upload:
        raise ValidationError("Gemini hybrid judge requires explicit remote-upload opt-in.")
    if getattr(transport, "api_version", None) != HYBRID_JUDGE_GEMINI_API_VERSION:
        raise ValidationError(
            "Gemini hybrid judge requires an explicitly pinned stable v1 transport."
        )
    if getattr(transport, "api_surface", None) != HYBRID_JUDGE_GEMINI_API_SURFACE:
        raise ValidationError(
            "Gemini hybrid judge requires the stable-v1 generateContent transport."
        )
    request, cost_request = _request_parts(preparation, prepared, config)
    service = cost_service or build_gemini_hybrid_judge_cost_service(config)
    call_id = cost_request.call_id
    item_dir = prepared.proxy_path.parent
    request_meta_path = item_dir / "request.judge.gemini.json"
    response_path = item_dir / "response.judge.gemini.json"
    raw_response_path = item_dir / "response.judge.gemini.raw.json"
    remote_metadata_path = item_dir / "remote.judge.gemini.json"
    cost_path = item_dir / "cost.judge.gemini.json"

    if not force:
        cached = _load_cached_response(
            prepared,
            service,
            cost_request=cost_request,
            judge_request=request,
        )
        if cached is not None:
            return GeminiHybridJudgeProposalResult(
                cache_hit=True,
                session_id=preparation.plan.session_id,
                proposal_id=request.proposal_id,
                call_id=call_id,
                request_meta_path=request_meta_path,
                response_path=response_path,
                remote_metadata_path=remote_metadata_path,
                cost_path=cost_path,
                request=request,
                response=cached,
                candidates=response_to_candidates(request, cached),
            )

    existing = _existing_call(service, call_id)
    if existing is not None:
        state = existing.status.value
        if state in {"RESERVED", "IN_FLIGHT", "AMBIGUOUS"}:
            raise ValidationError("A previous Gemini hybrid-judge call has an unresolved state.")
        if state == "SETTLED":
            raise ValidationError(
                "A settled Gemini hybrid-judge call has no reusable response artifact."
            )
        if state == "RELEASED":
            raise ValidationError(
                "A released Gemini hybrid-judge call requires an explicitly changed "
                "request identity."
            )

    atomic_write_json(
        request_meta_path,
        {
            "version": HYBRID_JUDGE_GEMINI_VERSION,
            "execution_mode": "injected_transport",
            "provider": "gemini",
            "model": config.scout.model,
            "api_version": HYBRID_JUDGE_GEMINI_API_VERSION,
            "api_surface": HYBRID_JUDGE_GEMINI_API_SURFACE,
            "billing_mode": config.scout.billing_mode,
            "session_id": preparation.plan.session_id,
            "proposal_id": request.proposal_id,
            "call_id": call_id,
            "judge_request_fingerprint": request.request_fingerprint,
            "provider_request_fingerprint": cost_request.request_fingerprint,
            "media_sha256": request.media_sha256,
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
    settled = False

    def mark_in_flight() -> None:
        nonlocal in_flight
        service.mark_in_flight(call_id)
        in_flight = True

    def validate_proposal_upload(path: Path) -> None:
        _validate_hybrid_judge_upload(path, prepared)

    try:
        record = service.reserve(cost_request)
        reserved = record.status.value in {"RESERVED", "IN_FLIGHT"}
        result = provider.execute(
            ProviderRequest(
                call_id=call_id,
                provider="gemini",
                model_id=config.scout.model,
                billing_mode=config.scout.billing_mode,
                stage="hybrid_judge",
                session_id=preparation.plan.session_id,
                usage_estimate=cost_request.usage_estimate,
                request_payload={
                    **cost_request.request_payload,
                    "response_max_bytes": config.scout.response_max_bytes,
                },
            ),
            proxy_path=prepared.proxy_path,
            session_proxy_root=prepared.proxy_path.parent,
            prompt=request.prompt,
            response_schema=request.response_schema,
            media_resolution=config.scout.media_resolution,
            max_output_tokens=HYBRID_JUDGE_MAX_OUTPUT_TOKENS,
            thinking_level=thinking.wire_level,
            remote_metadata_path=remote_metadata_path,
            before_generation=mark_in_flight,
            upload_validator=validate_proposal_upload,
        )
        try:
            service.settle(
                call_id,
                result.usage,
                provider_request_id=result.provider_request_id,
            )
            settled = True
        finally:
            _write_cost_artifact(service, call_id, cost_path)
        envelope = GeminiInteractionEnvelope.model_validate(result.result)
        atomic_write_json(
            raw_response_path,
            {
                "version": HYBRID_JUDGE_GEMINI_VERSION,
                "backend": "gemini",
                "execution_mode": "injected_transport",
                "session_id": preparation.plan.session_id,
                "proposal_id": request.proposal_id,
                "provider_request_fingerprint": cost_request.request_fingerprint,
                "judge_request_fingerprint": request.request_fingerprint,
                "envelope": envelope.model_dump(mode="json"),
            },
        )
        response = parse_hybrid_judge_response(
            envelope.output_text,
            proposal_duration_ms=request.proposal_duration_ms,
        )
        atomic_write_json(
            response_path,
            {
                "version": HYBRID_JUDGE_GEMINI_VERSION,
                "backend": "gemini",
                "execution_mode": "injected_transport",
                "session_id": preparation.plan.session_id,
                "proposal_id": request.proposal_id,
                "provider_request_fingerprint": cost_request.request_fingerprint,
                "judge_request_fingerprint": request.request_fingerprint,
                "envelope": envelope.model_dump(mode="json"),
                "response": response.model_dump(mode="json"),
            },
        )
        return GeminiHybridJudgeProposalResult(
            cache_hit=False,
            session_id=preparation.plan.session_id,
            proposal_id=request.proposal_id,
            call_id=call_id,
            request_meta_path=request_meta_path,
            response_path=response_path,
            remote_metadata_path=remote_metadata_path,
            cost_path=cost_path,
            request=request,
            response=response,
            candidates=response_to_candidates(request, response),
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
            hint="No automatic Gemini hybrid-judge generation retry was attempted.",
        ) from exc
    except BaseException:
        if not settled:
            if in_flight:
                with suppress(Exception):
                    service.mark_ambiguous(call_id, "local-post-dispatch-failure")
            elif reserved:
                with suppress(Exception):
                    service.release(call_id)
        with suppress(Exception):
            _write_cost_artifact(service, call_id, cost_path)
        raise


def run_gemini_hybrid_judge_batch_with_transport(
    preparation: HybridProposalPreparation,
    config: AppConfig,
    *,
    transport: GeminiTransport,
    cost_service: CostService | None = None,
) -> GeminiHybridJudgeBatchResult:
    """Preflight the entire batch, then execute sequentially with no automatic retries."""

    service = cost_service or build_gemini_hybrid_judge_cost_service(config)
    preflight_gemini_hybrid_judge_batch(preparation, config, cost_service=service)
    item_results: list[GeminiHybridJudgeProposalResult] = []
    all_candidates: list[Candidate] = []
    generation_calls = 0
    cache_hits = 0
    for prepared in preparation.prepared:
        result = run_gemini_hybrid_judge_with_transport(
            preparation,
            prepared,
            config,
            transport=transport,
            cost_service=service,
        )
        item_results.append(result)
        all_candidates.extend(result.candidates)
        if result.cache_hit:
            cache_hits += 1
        else:
            generation_calls += 1
    proposal_ranges = {
        item.proposal.proposal_id: (item.proposal.start_ms, item.proposal.end_ms)
        for item in preparation.prepared
    }
    return GeminiHybridJudgeBatchResult(
        session_id=preparation.plan.session_id,
        item_results=tuple(item_results),
        candidates=reconcile_hybrid_candidates(
            preparation.plan.session_id,
            all_candidates,
            proposal_ranges,
        ),
        generation_calls=generation_calls,
        cache_hits=cache_hits,
    )


def _request_parts(
    preparation: HybridProposalPreparation,
    prepared: PreparedHybridProposal,
    config: AppConfig,
) -> tuple[HybridJudgeRequestArtifact, CostRequest]:
    request = build_hybrid_judge_request(preparation, prepared)
    thinking = effective_gemini_thinking(config)
    media_config = effective_gemini_media_resolution(config)
    estimate = estimate_gemini_usage(
        duration_ms=request.proposal_duration_ms,
        prompt=request.prompt,
        response_schema=request.response_schema,
        # Proposal clips retain proxy audio when present. Assuming audio here is
        # conservative for preflight and avoids under-reserving before provider I/O.
        audio_present=True,
        max_output_tokens=HYBRID_JUDGE_MAX_OUTPUT_TOKENS,
        reserved_thinking_tokens=thinking.reserved_thinking_tokens,
        model=config.scout.model,
        media_resolution=config.scout.media_resolution,
    )
    semantic_payload = {
        "version": HYBRID_JUDGE_GEMINI_VERSION,
        "hybrid_judge_version": HYBRID_JUDGE_VERSION,
        "judge_request_fingerprint": request.request_fingerprint,
        "proposal_id": request.proposal_id,
        "proposal_sha256": request.proposal_sha256,
        "media_sha256": request.media_sha256,
        "response_schema_sha256": canonical_payload_sha256(request.response_schema),
        "prompt_sha256": canonical_payload_sha256(request.prompt),
        "api_version": HYBRID_JUDGE_GEMINI_API_VERSION,
        "api_surface": HYBRID_JUDGE_GEMINI_API_SURFACE,
        "media_resolution": config.scout.media_resolution,
        "wire_media_resolution": media_config.wire_level,
        "thinking_level": thinking.wire_level,
        "thinking_policy": thinking.policy,
        "max_output_tokens": HYBRID_JUDGE_MAX_OUTPUT_TOKENS,
    }
    seed = CostRequest(
        call_id="hybrid-judge-fingerprint",
        provider="gemini",
        model=config.scout.model,
        billing_mode=config.scout.billing_mode,
        stage="hybrid_judge",
        session_id=preparation.plan.session_id,
        usage_estimate=estimate,
        request_payload=semantic_payload,
    )
    call_id = f"gemini-hjudge-{seed.request_fingerprint[:48]}"
    return request, seed.model_copy(update={"call_id": call_id})


def _validate_hybrid_judge_upload(path: Path, prepared: PreparedHybridProposal) -> None:
    try:
        resolved = path.resolve()
        expected = prepared.proxy_path.resolve()
    except OSError as exc:
        raise ValidationError("Cannot resolve hybrid-judge proposal media") from exc
    if resolved != expected or resolved.name != "analysis_proposal.mp4" or not resolved.is_file():
        raise ValidationError("Gemini hybrid judge may upload only the prepared proposal clip")
    if hash_file(resolved) != prepared.proxy_sha256:
        raise ValidationError("Gemini hybrid-judge upload hash does not match provenance")


def _artifact_paths(prepared: PreparedHybridProposal) -> tuple[Path, Path, Path, Path]:
    item_dir = prepared.proxy_path.parent
    return (
        item_dir / "request.judge.gemini.json",
        item_dir / "response.judge.gemini.json",
        item_dir / "remote.judge.gemini.json",
        item_dir / "cost.judge.gemini.json",
    )


def _load_cached_response(
    prepared: PreparedHybridProposal,
    service: CostService,
    *,
    cost_request: CostRequest,
    judge_request: HybridJudgeRequestArtifact,
) -> HybridJudgeResponse | None:
    request_meta_path, response_path, _, _ = _artifact_paths(prepared)
    if not request_meta_path.is_file() or not response_path.is_file():
        return None
    record = _existing_call(service, cost_request.call_id)
    if record is None or record.status.value != "SETTLED":
        return None
    try:
        meta = read_json(request_meta_path)
        raw = read_json(response_path)
        if meta.get("session_id") != judge_request.session_id:
            return None
        if meta.get("proposal_id") != judge_request.proposal_id:
            return None
        if meta.get("provider_request_fingerprint") != cost_request.request_fingerprint:
            return None
        if meta.get("judge_request_fingerprint") != judge_request.request_fingerprint:
            return None
        if raw.get("provider_request_fingerprint") != cost_request.request_fingerprint:
            return None
        if raw.get("judge_request_fingerprint") != judge_request.request_fingerprint:
            return None
        envelope = GeminiInteractionEnvelope.model_validate(raw.get("envelope"))
        usage_from_envelope(envelope)
        response = parse_hybrid_judge_response(
            raw.get("response"),
            proposal_duration_ms=judge_request.proposal_duration_ms,
        )
        _validate_hybrid_judge_upload(prepared.proxy_path, prepared)
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


def build_gemini_hybrid_judge_cost_service(config: AppConfig) -> CostService:
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


__all__ = [
    "HYBRID_JUDGE_GEMINI_API_SURFACE",
    "HYBRID_JUDGE_GEMINI_API_VERSION",
    "HYBRID_JUDGE_GEMINI_VERSION",
    "HYBRID_JUDGE_MAX_OUTPUT_TOKENS",
    "GeminiHybridJudgeBatchPreflight",
    "GeminiHybridJudgeBatchResult",
    "GeminiHybridJudgeItemPreflight",
    "GeminiHybridJudgeProposalResult",
    "build_gemini_hybrid_judge_cost_service",
    "preflight_gemini_hybrid_judge_batch",
    "run_gemini_hybrid_judge_batch_with_transport",
    "run_gemini_hybrid_judge_with_transport",
]
