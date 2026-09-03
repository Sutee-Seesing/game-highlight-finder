"""OpenRouter multimodal bake-off comparator for bounded hybrid proposals.

The comparator reuses the provider-neutral HybridJudge semantics used by Gemini.
Each locked model profile pins an exact OpenRouter upstream, pricing boundary,
and response-format contract. Aggregate preflight remains provider-free and
performs zero ledger reservation.
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
from game_highlight_finder.openrouter_models import (
    GLM_5V_TURBO,
    OPENROUTER_ROUND_A_MODEL_IDS,
    get_openrouter_model_profile,
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
from game_highlight_finder.providers.base import (
    MAX_USAGE_TOKENS_PER_DIMENSION,
    ProviderRegistry,
    ProviderUsageEstimate,
)
from game_highlight_finder.providers.openrouter import (
    OPENROUTER_API_SURFACE,
    OPENROUTER_HTTP_ATTEMPTS,
    OPENROUTER_PROVIDER,
    OpenRouterCompletionEnvelope,
    OpenRouterProviderError,
    OpenRouterTransport,
    openrouter_provider_descriptor,
)
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.hashing import hash_file

ZAI_HYBRID_JUDGE_VERSION = "hybrid-judge-openrouter-v4"
ZAI_HYBRID_JUDGE_ESTIMATOR_VERSION = "openrouter-video-estimate-v1"
ZAI_HYBRID_JUDGE_VIDEO_TOKENS_PER_SECOND = 256
ZAI_HYBRID_JUDGE_MAX_OUTPUT_TOKENS = 1_024
ZAI_HYBRID_JUDGE_RESERVED_THINKING_TOKENS = 1_024
ZAI_HYBRID_JUDGE_MEDIA_TRANSPORT_CONTRACT = "openrouter-base64-video-v2"
ZAI_HYBRID_JUDGE_ROUTING_PRICE_UNIT = "usd_per_million_tokens"


class ZAIHybridJudgeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = GLM_5V_TURBO.model_id
    billing_mode: Literal["standard"] = "standard"
    thinking_mode: Literal["enabled", "disabled"] = "enabled"
    max_output_tokens: int = Field(default=ZAI_HYBRID_JUDGE_MAX_OUTPUT_TOKENS, ge=1, le=32_768)
    reserved_thinking_tokens: int = Field(
        default=ZAI_HYBRID_JUDGE_RESERVED_THINKING_TOKENS,
        ge=0,
        le=MAX_USAGE_TOKENS_PER_DIMENSION,
    )
    allow_remote_media: bool = False

    @field_validator("model")
    @classmethod
    def supported_bakeoff_model(cls, value: str) -> str:
        if value not in OPENROUTER_ROUND_A_MODEL_IDS:
            raise ValueError(f"unsupported OpenRouter bake-off model: {value}")
        return value


class ZAIHybridJudgeItemPreflight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_id: str = Field(pattern=r"^proposal_[0-9a-f]{16}$")
    call_id: str = Field(min_length=1, max_length=128)
    judge_request_fingerprint: Sha256
    provider_request_fingerprint: Sha256
    media_sha256: Sha256
    usage_estimate: ProviderUsageEstimate
    cache_hit: bool
    maximum_reserved_micro_thb: int = Field(ge=0)


class ZAIHybridJudgeBatchPreflight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["hybrid-judge-openrouter-v4"] = "hybrid-judge-openrouter-v4"
    provider: Literal["openrouter"] = "openrouter"
    model: str
    billing_mode: str
    session_id: str = Field(min_length=1, max_length=128)
    thinking_mode: str
    estimator_version: str = ZAI_HYBRID_JUDGE_ESTIMATOR_VERSION
    media_transport_contract: str = ZAI_HYBRID_JUDGE_MEDIA_TRANSPORT_CONTRACT
    routing_price_unit: str = ZAI_HYBRID_JUDGE_ROUTING_PRICE_UNIT
    items: tuple[ZAIHybridJudgeItemPreflight, ...]
    planned_generation_calls: int = Field(ge=0)
    cache_hit_count: int = Field(ge=0)
    aggregate_maximum_reserved_micro_thb: int = Field(ge=0)
    available_micro_thb: int = Field(ge=0)
    post_reservation_headroom_micro_thb: int = Field(ge=0)
    provider_calls: Literal[0] = 0
    remote_uploads: Literal[0] = 0
    ledger_reservations: Literal[0] = 0
    live_media_transport_verified: Literal[True] = True


class ZAIHybridJudgeProposalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    cache_hit: bool
    session_id: str = Field(min_length=1, max_length=128)
    proposal_id: str = Field(pattern=r"^proposal_[0-9a-f]{16}$")
    call_id: str
    request_meta_path: Path
    response_path: Path
    cost_path: Path
    request: HybridJudgeRequestArtifact
    response: HybridJudgeResponse
    candidates: tuple[Candidate, ...]


class ZAIHybridJudgeBatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    session_id: str = Field(min_length=1, max_length=128)
    item_results: tuple[ZAIHybridJudgeProposalResult, ...]
    candidates: tuple[Candidate, ...]
    generation_calls: int = Field(ge=0)
    cache_hits: int = Field(ge=0)


def build_zai_registry() -> ProviderRegistry:
    return ProviderRegistry([openrouter_provider_descriptor()])


def estimate_zai_hybrid_usage(
    *,
    duration_ms: int,
    prompt: str,
    response_schema: dict[str, object],
    settings: ZAIHybridJudgeSettings,
) -> ProviderUsageEstimate:
    """Conservative local quote heuristic; not a claim about provider tokenization."""

    if duration_ms <= 0:
        raise ValidationError("OpenRouter hybrid-judge proposal duration must be positive")
    seconds = (duration_ms + 999) // 1000
    schema_bytes = len(
        __import__("json").dumps(
            response_schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    )
    text_tokens = max(1, (len(prompt.encode("utf-8")) + 3) // 4)
    text_tokens += max(1, (schema_bytes + 3) // 4) + 128
    estimate = ProviderUsageEstimate(
        input_text_tokens=min(MAX_USAGE_TOKENS_PER_DIMENSION, text_tokens),
        input_video_tokens=min(
            MAX_USAGE_TOKENS_PER_DIMENSION,
            seconds * ZAI_HYBRID_JUDGE_VIDEO_TOKENS_PER_SECOND,
        ),
        output_tokens=settings.max_output_tokens,
        thinking_tokens=(
            settings.reserved_thinking_tokens if settings.thinking_mode == "enabled" else 0
        ),
    )
    _validate_profile_estimate(settings, estimate)
    return estimate


def _validate_profile_estimate(
    settings: ZAIHybridJudgeSettings,
    estimate: ProviderUsageEstimate,
) -> None:
    profile = get_openrouter_model_profile(settings.model)
    total_input = (
        estimate.input_text_tokens
        + estimate.input_image_tokens
        + estimate.input_video_tokens
        + estimate.input_audio_tokens
        + estimate.cached_input_tokens
    )
    if (
        profile.base_price_prompt_token_limit is not None
        and total_input >= profile.base_price_prompt_token_limit
    ):
        raise ValidationError(
            f"OpenRouter model {profile.model_id} estimate reaches the provider price-tier "
            "override; update the exact pricing profile before dispatch."
        )
    if total_input + estimate.output_tokens + estimate.thinking_tokens > profile.context_tokens:
        raise ValidationError(
            f"OpenRouter model {profile.model_id} estimate exceeds its locked context ceiling."
        )


def preflight_zai_hybrid_judge_batch(
    preparation: HybridProposalPreparation,
    config: AppConfig,
    *,
    settings: ZAIHybridJudgeSettings | None = None,
    cost_service: CostService | None = None,
) -> ZAIHybridJudgeBatchPreflight:
    """Quote a complete comparator batch with zero reservation or provider I/O."""

    resolved = settings or ZAIHybridJudgeSettings()
    service = cost_service or build_zai_hybrid_judge_cost_service(config)
    parts = [_request_parts(preparation, item, resolved) for item in preparation.prepared]
    summary = service.summary()
    if summary.safety_hold_active:
        raise CostSafetyHoldError(
            "Cost safety hold is active; OpenRouter hybrid-judge preflight is blocked.",
            hint=summary.safety_hold_reason,
        )

    items: list[ZAIHybridJudgeItemPreflight] = []
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
            ZAIHybridJudgeItemPreflight(
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
                f"OpenRouter hybrid judge requires {aggregate} micro-THB of new reservation "
                f"exposure, but only {summary.available_micro_thb} is available."
            )
        )
    return ZAIHybridJudgeBatchPreflight(
        model=resolved.model,
        billing_mode=resolved.billing_mode,
        session_id=preparation.plan.session_id,
        thinking_mode=resolved.thinking_mode,
        items=tuple(items),
        planned_generation_calls=len(items) - cache_hits,
        cache_hit_count=cache_hits,
        aggregate_maximum_reserved_micro_thb=aggregate,
        available_micro_thb=summary.available_micro_thb,
        post_reservation_headroom_micro_thb=summary.available_micro_thb - aggregate,
    )


def run_zai_hybrid_judge_with_transport(
    preparation: HybridProposalPreparation,
    prepared: PreparedHybridProposal,
    config: AppConfig,
    *,
    transport: OpenRouterTransport,
    settings: ZAIHybridJudgeSettings | None = None,
    cost_service: CostService | None = None,
    force: bool = False,
) -> ZAIHybridJudgeProposalResult:
    """Run one comparator request through an explicitly injected verified transport."""

    resolved = settings or ZAIHybridJudgeSettings()
    profile = get_openrouter_model_profile(resolved.model)
    if not resolved.allow_remote_media:
        raise ValidationError("OpenRouter judge requires explicit remote-media opt-in.")
    if getattr(transport, "api_surface", None) != OPENROUTER_API_SURFACE:
        raise ValidationError("OpenRouter judge requires the chat_completions API surface.")
    if getattr(transport, "http_retry_attempts", None) != OPENROUTER_HTTP_ATTEMPTS:
        raise ValidationError(
            "OpenRouter judge requires HTTP attempts=1 (no client automatic retry)."
        )
    if getattr(transport, "media_transport_verified", False) is not True:
        raise ValidationError("OpenRouter local-video media transport is not verified.")

    request, cost_request = _request_parts(preparation, prepared, resolved)
    service = cost_service or build_zai_hybrid_judge_cost_service(config)
    call_id = cost_request.call_id
    request_meta_path, response_path, raw_response_path, cost_path = _artifact_paths(
        prepared, resolved.model
    )

    if not force:
        cached = _load_cached_response(
            prepared,
            service,
            cost_request=cost_request,
            judge_request=request,
        )
        if cached is not None:
            return ZAIHybridJudgeProposalResult(
                cache_hit=True,
                session_id=preparation.plan.session_id,
                proposal_id=request.proposal_id,
                call_id=call_id,
                request_meta_path=request_meta_path,
                response_path=response_path,
                cost_path=cost_path,
                request=request,
                response=cached,
                candidates=response_to_candidates(request, cached),
            )

    existing = _existing_call(service, call_id)
    if existing is not None:
        state = existing.status.value
        if state in {"RESERVED", "IN_FLIGHT", "AMBIGUOUS"}:
            raise ValidationError(
                "A previous OpenRouter hybrid-judge call has an unresolved state."
            )
        if state == "SETTLED":
            raise ValidationError("A settled OpenRouter call has no reusable response artifact.")
        if state == "RELEASED":
            raise ValidationError("A released OpenRouter call requires changed request identity.")

    _validate_media(prepared.proxy_path, prepared)
    atomic_write_json(
        request_meta_path,
        {
            "version": ZAI_HYBRID_JUDGE_VERSION,
            "execution_mode": "injected_transport",
            "provider": OPENROUTER_PROVIDER,
            "upstream_provider": profile.upstream_provider_slug,
            "selected_provider_name": profile.selected_provider_name,
            "response_format_mode": profile.response_format_mode,
            "model": resolved.model,
            "api_surface": OPENROUTER_API_SURFACE,
            "http_attempts": OPENROUTER_HTTP_ATTEMPTS,
            "media_transport_contract": ZAI_HYBRID_JUDGE_MEDIA_TRANSPORT_CONTRACT,
            "routing_price_unit": ZAI_HYBRID_JUDGE_ROUTING_PRICE_UNIT,
            "estimator_version": ZAI_HYBRID_JUDGE_ESTIMATOR_VERSION,
            "thinking_mode": resolved.thinking_mode,
            "billing_mode": resolved.billing_mode,
            "session_id": preparation.plan.session_id,
            "proposal_id": request.proposal_id,
            "call_id": call_id,
            "judge_request_fingerprint": request.request_fingerprint,
            "provider_request_fingerprint": cost_request.request_fingerprint,
            "media_sha256": request.media_sha256,
            "usage_estimate": cost_request.usage_estimate.model_dump(mode="json"),
        },
    )

    reserved = False
    in_flight = False
    settled = False

    def mark_in_flight() -> None:
        nonlocal in_flight
        service.mark_in_flight(call_id)
        in_flight = True

    try:
        record = service.reserve(cost_request)
        reserved = record.status.value in {"RESERVED", "IN_FLIGHT"}
        envelope = transport.generate(
            media_path=prepared.proxy_path,
            prompt=request.prompt,
            response_schema=request.response_schema,
            model=resolved.model,
            max_output_tokens=resolved.max_output_tokens,
            reasoning_max_tokens=resolved.reserved_thinking_tokens,
            thinking_mode=resolved.thinking_mode,
            before_generation=mark_in_flight,
        )
        try:
            service.settle(
                call_id,
                envelope.usage,
                provider_request_id=envelope.usage.provider_request_id or envelope.id,
            )
            settled = True
        finally:
            _write_cost_artifact(service, call_id, cost_path)
        atomic_write_json(
            raw_response_path,
            {
                "version": ZAI_HYBRID_JUDGE_VERSION,
                "backend": OPENROUTER_PROVIDER,
                "upstream_provider": profile.upstream_provider_slug,
                "execution_mode": "injected_transport",
                "session_id": preparation.plan.session_id,
                "proposal_id": request.proposal_id,
                "provider_request_fingerprint": cost_request.request_fingerprint,
                "judge_request_fingerprint": request.request_fingerprint,
                "envelope": envelope.model_dump(mode="json"),
            },
        )
        if envelope.router_attempt_count != 1:
            raise ValidationError(
                "OpenRouter routing metadata must prove exactly one upstream provider attempt."
            )
        if envelope.selected_provider != profile.selected_provider_name:
            raise ValidationError(
                "OpenRouter routing metadata did not confirm the locked upstream endpoint."
            )
        if envelope.finish_reason == "length":
            raise ValidationError(
                "OpenRouter completion exhausted its combined reasoning/final-output token budget."
            )
        if not envelope.output_text.strip():
            raise ValidationError("OpenRouter completed response is missing text content.")
        response = parse_hybrid_judge_response(
            envelope.output_text,
            proposal_duration_ms=request.proposal_duration_ms,
        )
        atomic_write_json(
            response_path,
            {
                "version": ZAI_HYBRID_JUDGE_VERSION,
                "backend": OPENROUTER_PROVIDER,
                "upstream_provider": profile.upstream_provider_slug,
                "execution_mode": "injected_transport",
                "session_id": preparation.plan.session_id,
                "proposal_id": request.proposal_id,
                "provider_request_fingerprint": cost_request.request_fingerprint,
                "judge_request_fingerprint": request.request_fingerprint,
                "envelope": envelope.model_dump(mode="json"),
                "response": response.model_dump(mode="json"),
            },
        )
        return ZAIHybridJudgeProposalResult(
            cache_hit=False,
            session_id=preparation.plan.session_id,
            proposal_id=request.proposal_id,
            call_id=call_id,
            request_meta_path=request_meta_path,
            response_path=response_path,
            cost_path=cost_path,
            request=request,
            response=response,
            candidates=response_to_candidates(request, response),
        )
    except OpenRouterProviderError as exc:
        if exc.may_have_dispatched:
            if in_flight:
                with suppress(Exception):
                    service.mark_ambiguous(call_id, str(exc))
        elif in_flight:
            with suppress(Exception):
                service.release(call_id, confirmed_no_dispatch=True)
        elif reserved:
            with suppress(Exception):
                service.release(call_id)
        with suppress(Exception):
            _write_cost_artifact(service, call_id, cost_path)
        raise ValidationError(
            str(exc), hint="No automatic OpenRouter generation retry was attempted."
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


def run_zai_hybrid_judge_batch_with_transport(
    preparation: HybridProposalPreparation,
    config: AppConfig,
    *,
    transport: OpenRouterTransport,
    settings: ZAIHybridJudgeSettings | None = None,
    cost_service: CostService | None = None,
) -> ZAIHybridJudgeBatchResult:
    resolved = settings or ZAIHybridJudgeSettings()
    service = cost_service or build_zai_hybrid_judge_cost_service(config)
    preflight_zai_hybrid_judge_batch(
        preparation,
        config,
        settings=resolved,
        cost_service=service,
    )
    item_results: list[ZAIHybridJudgeProposalResult] = []
    all_candidates: list[Candidate] = []
    generation_calls = 0
    cache_hits = 0
    for prepared in preparation.prepared:
        result = run_zai_hybrid_judge_with_transport(
            preparation,
            prepared,
            config,
            transport=transport,
            settings=resolved,
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
    return ZAIHybridJudgeBatchResult(
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
    settings: ZAIHybridJudgeSettings,
) -> tuple[HybridJudgeRequestArtifact, CostRequest]:
    request = build_hybrid_judge_request(preparation, prepared)
    profile = get_openrouter_model_profile(settings.model)
    estimate = estimate_zai_hybrid_usage(
        duration_ms=request.proposal_duration_ms,
        prompt=request.prompt,
        response_schema=request.response_schema,
        settings=settings,
    )
    semantic_payload = {
        "version": ZAI_HYBRID_JUDGE_VERSION,
        "hybrid_judge_version": HYBRID_JUDGE_VERSION,
        "judge_request_fingerprint": request.request_fingerprint,
        "proposal_id": request.proposal_id,
        "proposal_sha256": request.proposal_sha256,
        "media_sha256": request.media_sha256,
        "response_schema_sha256": canonical_payload_sha256(request.response_schema),
        "prompt_sha256": canonical_payload_sha256(request.prompt),
        "provider": OPENROUTER_PROVIDER,
        "upstream_provider": profile.upstream_provider_slug,
        "selected_provider_name": profile.selected_provider_name,
        "response_format_mode": profile.response_format_mode,
        "pricing_source": profile.pricing_source,
        "api_surface": OPENROUTER_API_SURFACE,
        "http_attempts": OPENROUTER_HTTP_ATTEMPTS,
        "provider_fallbacks": False,
        "provider_require_parameters": True,
        "media_transport_contract": ZAI_HYBRID_JUDGE_MEDIA_TRANSPORT_CONTRACT,
        "routing_price_unit": ZAI_HYBRID_JUDGE_ROUTING_PRICE_UNIT,
        "estimator_version": ZAI_HYBRID_JUDGE_ESTIMATOR_VERSION,
        "thinking_mode": settings.thinking_mode,
        "max_output_tokens": settings.max_output_tokens,
        "reserved_thinking_tokens": settings.reserved_thinking_tokens,
    }
    seed = CostRequest(
        call_id="openrouter-hybrid-judge-fingerprint",
        provider=OPENROUTER_PROVIDER,
        model=settings.model,
        billing_mode=settings.billing_mode,
        stage="hybrid_judge",
        session_id=preparation.plan.session_id,
        usage_estimate=estimate,
        request_payload=semantic_payload,
    )
    call_id = f"openrouter-hjudge-{seed.request_fingerprint[:48]}"
    return request, seed.model_copy(update={"call_id": call_id})


def _validate_media(path: Path, prepared: PreparedHybridProposal) -> None:
    try:
        resolved = path.resolve()
        expected = prepared.proxy_path.resolve()
    except OSError as exc:
        raise ValidationError("Cannot resolve OpenRouter hybrid-judge proposal media") from exc
    if resolved != expected or resolved.name != "analysis_proposal.mp4" or not resolved.is_file():
        raise ValidationError("OpenRouter hybrid judge may send only the prepared proposal clip")
    if hash_file(resolved) != prepared.proxy_sha256:
        raise ValidationError("OpenRouter hybrid-judge media hash does not match provenance")


def _artifact_paths(
    prepared: PreparedHybridProposal,
    model: str,
) -> tuple[Path, Path, Path, Path]:
    item_dir = prepared.proxy_path.parent
    tag = model.replace("/", "__").replace(".", "_")
    return (
        item_dir / f"request.judge.openrouter.{tag}.json",
        item_dir / f"response.judge.openrouter.{tag}.json",
        item_dir / f"response.judge.openrouter.{tag}.raw.json",
        item_dir / f"cost.judge.openrouter.{tag}.json",
    )


def _load_cached_response(
    prepared: PreparedHybridProposal,
    service: CostService,
    *,
    cost_request: CostRequest,
    judge_request: HybridJudgeRequestArtifact,
) -> HybridJudgeResponse | None:
    request_meta_path, response_path, _, _ = _artifact_paths(prepared, cost_request.model)
    if not request_meta_path.is_file() or not response_path.is_file():
        return None
    record = _existing_call(service, cost_request.call_id)
    if record is None or record.status.value != "SETTLED":
        return None
    profile = get_openrouter_model_profile(cost_request.model)
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
        envelope = OpenRouterCompletionEnvelope.model_validate(raw.get("envelope"))
        if (
            envelope.router_attempt_count != 1
            or envelope.selected_provider != profile.selected_provider_name
            or envelope.model != profile.model_id
        ):
            return None
        response = parse_hybrid_judge_response(
            raw.get("response"),
            proposal_duration_ms=judge_request.proposal_duration_ms,
        )
        _validate_media(prepared.proxy_path, prepared)
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


def build_zai_hybrid_judge_cost_service(config: AppConfig) -> CostService:
    if config.cost.pricing_catalog_path is not None:
        return CostService.from_config(config, registry=build_zai_registry())
    fx = (
        FxSnapshot.from_file(config.cost.fx_snapshot_path) if config.cost.fx_snapshot_path else None
    )
    return CostService(
        config,
        registry=build_zai_registry(),
        pricing=production_pricing_catalog(),
        fx_snapshot=fx,
    )


__all__ = [
    "ZAI_HYBRID_JUDGE_ESTIMATOR_VERSION",
    "ZAI_HYBRID_JUDGE_MAX_OUTPUT_TOKENS",
    "ZAI_HYBRID_JUDGE_MEDIA_TRANSPORT_CONTRACT",
    "ZAI_HYBRID_JUDGE_RESERVED_THINKING_TOKENS",
    "ZAI_HYBRID_JUDGE_ROUTING_PRICE_UNIT",
    "ZAI_HYBRID_JUDGE_VERSION",
    "ZAI_HYBRID_JUDGE_VIDEO_TOKENS_PER_SECOND",
    "ZAIHybridJudgeBatchPreflight",
    "ZAIHybridJudgeBatchResult",
    "ZAIHybridJudgeItemPreflight",
    "ZAIHybridJudgeProposalResult",
    "ZAIHybridJudgeSettings",
    "build_zai_hybrid_judge_cost_service",
    "build_zai_registry",
    "estimate_zai_hybrid_usage",
    "preflight_zai_hybrid_judge_batch",
    "run_zai_hybrid_judge_batch_with_transport",
    "run_zai_hybrid_judge_with_transport",
]
