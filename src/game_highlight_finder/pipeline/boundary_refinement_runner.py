from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from game_highlight_finder.config import AppConfig
from game_highlight_finder.cost.fx import FxSnapshot
from game_highlight_finder.cost.ledger import LifecycleStatus
from game_highlight_finder.cost.models import CostQuote
from game_highlight_finder.cost.production import production_pricing_catalog
from game_highlight_finder.cost.service import CostRequest, CostService
from game_highlight_finder.domain.models import Candidate, SourceAsset
from game_highlight_finder.errors import CostGateError, ValidationError
from game_highlight_finder.media.ffmpeg import (
    build_slow_motion_proxy_command,
    build_window_proxy_command,
    run_ffmpeg,
)
from game_highlight_finder.media.tools import tool_identity
from game_highlight_finder.pipeline.boundary_refinement import (
    BoundaryRefinementPlan,
    BoundaryRefinementResponse,
    apply_boundary_refinement,
    boundary_refinement_schema,
    build_boundary_refinement_prompt,
    plan_boundary_refinement,
)
from game_highlight_finder.pipeline.gemini_scout import (
    build_gemini_registry,
    effective_gemini_media_resolution,
    effective_gemini_thinking,
    estimate_gemini_usage,
)
from game_highlight_finder.providers.base import ProviderRequest
from game_highlight_finder.providers.gemini import (
    GeminiInteractionEnvelope,
    GeminiProvider,
    GeminiProviderError,
)
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.hashing import hash_file

BOUNDARY_REFINEMENT_MAX_OUTPUT_TOKENS = 512


class BoundaryRefinementPreparation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan: BoundaryRefinementPlan
    root: Path
    context_path: Path
    slow_proxy_path: Path
    parent_proxy_sha256: str
    context_sha256: str
    slow_proxy_sha256: str
    has_audio: bool
    cache_key: str
    cache_hit: bool = False


class BoundaryRefinementPreflight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request: CostRequest
    quote: CostQuote
    available_micro_thb: int
    blocked: bool


class BoundaryRefinementRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    preparation: BoundaryRefinementPreparation
    preflight: BoundaryRefinementPreflight
    candidate_before: Candidate
    candidate_after: Candidate
    response: BoundaryRefinementResponse
    cache_hit: bool
    provider_generation_calls: int
    provider_uploads: int
    paid_reservations_created: int
    raw_path: Path
    applied_path: Path


def _sha256_json(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _build_cost_service(config: AppConfig) -> CostService:
    if config.cost.pricing_catalog_path is not None:
        return CostService.from_config(config, registry=build_gemini_registry())
    fx = (
        FxSnapshot.from_file(config.cost.fx_snapshot_path)
        if config.cost.fx_snapshot_path is not None
        else None
    )
    return CostService(
        config,
        registry=build_gemini_registry(),
        pricing=production_pricing_catalog(),
        fx_snapshot=fx,
    )


def _preparation_payload(
    plan: BoundaryRefinementPlan, parent_proxy_sha256: str, has_audio: bool
) -> dict[str, object]:
    return {
        "plan": plan.model_dump(mode="json"),
        "parent_proxy_sha256": parent_proxy_sha256,
        "has_audio": has_audio,
        "video_codec": "libx264",
        "preset": "veryfast",
    }


def prepare_boundary_refinement_proxy(
    candidate: Candidate,
    source: SourceAsset,
    analysis_proxy_path: Path,
    output_dir: Path,
    config: AppConfig,
) -> BoundaryRefinementPreparation:
    if not analysis_proxy_path.is_file():
        raise ValidationError("Boundary refinement requires the committed analysis proxy")
    try:
        if analysis_proxy_path.resolve() == source.path.resolve():
            raise ValidationError("Boundary refinement must never use the RAW source")
    except OSError as exc:
        raise ValidationError("Boundary refinement paths could not be resolved") from exc
    if analysis_proxy_path.name != "analysis_proxy.mp4":
        raise ValidationError("Boundary refinement requires the committed analysis_proxy.mp4")
    plan = plan_boundary_refinement(candidate, source.duration_ms)
    parent_sha = hash_file(analysis_proxy_path)
    has_audio = source.selected_audio_stream is not None
    prep_payload = _preparation_payload(plan, parent_sha, has_audio)
    cache_key = _sha256_json(prep_payload)
    root = output_dir / candidate.candidate_id
    root.mkdir(parents=True, exist_ok=True)
    context_path = root / "boundary_context.mp4"
    slow_path = root / "boundary_slow.mp4"
    manifest_path = root / "preparation.json"
    if manifest_path.is_file() and context_path.is_file() and slow_path.is_file():
        try:
            manifest = read_json(manifest_path)
            if (
                manifest.get("cache_key") == cache_key
                and manifest.get("context_sha256") == hash_file(context_path)
                and manifest.get("slow_proxy_sha256") == hash_file(slow_path)
            ):
                return BoundaryRefinementPreparation(
                    plan=plan,
                    root=root,
                    context_path=context_path,
                    slow_proxy_path=slow_path,
                    parent_proxy_sha256=parent_sha,
                    context_sha256=manifest["context_sha256"],
                    slow_proxy_sha256=manifest["slow_proxy_sha256"],
                    has_audio=has_audio,
                    cache_key=cache_key,
                    cache_hit=True,
                )
        except Exception:
            pass
    ffmpeg = tool_identity("ffmpeg", config.tools.ffmpeg_path)
    context_tmp = root / "boundary_context.partial.mp4"
    slow_tmp = root / "boundary_slow.partial.mp4"
    for path in (context_tmp, slow_tmp):
        with suppress(OSError):
            path.unlink()
    run_ffmpeg(
        build_window_proxy_command(
            ffmpeg.path,
            analysis_proxy_path,
            context_tmp,
            proxy_start_ms=plan.source_start_ms,
            duration_ms=plan.source_duration_ms,
            has_audio=has_audio,
            video_codec="libx264",
            preset="veryfast",
        ),
        duration_ms=plan.source_duration_ms,
        timeout_seconds=config.tools.ffmpeg_timeout_seconds,
        termination_grace_seconds=config.tools.termination_grace_seconds,
    )
    if not context_tmp.is_file() or context_tmp.stat().st_size <= 0:
        raise ValidationError("Boundary refinement context proxy was not produced")
    run_ffmpeg(
        build_slow_motion_proxy_command(
            ffmpeg.path,
            context_tmp,
            slow_tmp,
            slowdown_factor=plan.slowdown_factor,
            has_audio=has_audio,
            video_codec="libx264",
            preset="veryfast",
        ),
        duration_ms=plan.proxy_duration_ms,
        timeout_seconds=config.tools.ffmpeg_timeout_seconds,
        termination_grace_seconds=config.tools.termination_grace_seconds,
    )
    if not slow_tmp.is_file() or slow_tmp.stat().st_size <= 0:
        raise ValidationError("Boundary refinement slow proxy was not produced")
    context_tmp.replace(context_path)
    slow_tmp.replace(slow_path)
    context_sha = hash_file(context_path)
    slow_sha = hash_file(slow_path)
    atomic_write_json(
        manifest_path,
        {
            **prep_payload,
            "cache_key": cache_key,
            "context_sha256": context_sha,
            "slow_proxy_sha256": slow_sha,
        },
    )
    return BoundaryRefinementPreparation(
        plan=plan,
        root=root,
        context_path=context_path,
        slow_proxy_path=slow_path,
        parent_proxy_sha256=parent_sha,
        context_sha256=context_sha,
        slow_proxy_sha256=slow_sha,
        has_audio=has_audio,
        cache_key=cache_key,
        cache_hit=False,
    )


def _request_parts(
    preparation: BoundaryRefinementPreparation,
    candidate: Candidate,
    config: AppConfig,
    session_id: str | None,
) -> tuple[str, dict[str, object], CostRequest]:
    prompt = build_boundary_refinement_prompt(preparation.plan, candidate)
    schema = boundary_refinement_schema(preparation.plan)
    thinking = effective_gemini_thinking(config)
    media = effective_gemini_media_resolution(config)
    estimate = estimate_gemini_usage(
        duration_ms=preparation.plan.proxy_duration_ms,
        prompt=prompt,
        response_schema=schema,
        audio_present=preparation.has_audio,
        max_output_tokens=BOUNDARY_REFINEMENT_MAX_OUTPUT_TOKENS,
        reserved_thinking_tokens=thinking.reserved_thinking_tokens,
        model=config.scout.model,
        media_resolution=config.scout.media_resolution,
    )
    payload: dict[str, object] = {
        "candidate_id": candidate.candidate_id,
        "source_interval_ms": [preparation.plan.source_start_ms, preparation.plan.source_end_ms],
        "anchor_interval_ms": [candidate.event_start_ms, candidate.event_end_ms],
        "slowdown_factor": preparation.plan.slowdown_factor,
        "slow_proxy_sha256": preparation.slow_proxy_sha256,
        "model": config.scout.model,
        "billing_mode": config.scout.billing_mode,
        "media_resolution": config.scout.media_resolution,
        "wire_media_resolution": media.wire_level,
        "thinking_level": thinking.wire_level,
        "prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "schema_hash": _sha256_json(schema),
        "version": preparation.plan.version,
    }
    cache_key = _sha256_json(payload)
    request = CostRequest(
        call_id=f"gemini-boundary-{cache_key[:44]}",
        provider="gemini",
        model=config.scout.model,
        billing_mode=config.scout.billing_mode,
        stage="boundary_refine",
        session_id=session_id,
        usage_estimate=estimate,
        request_payload=payload,
    )
    return prompt, schema, request


def preflight_boundary_refinement(
    preparation: BoundaryRefinementPreparation,
    candidate: Candidate,
    config: AppConfig,
    *,
    session_id: str | None = None,
    cost_service: CostService | None = None,
) -> BoundaryRefinementPreflight:
    service = cost_service or _build_cost_service(config)
    _prompt, _schema, request = _request_parts(preparation, candidate, config, session_id)
    quote = service.quote(request)
    summary = service.summary()
    blocked = quote.reserved_cost_micro_thb > summary.available_micro_thb
    return BoundaryRefinementPreflight(
        request=request,
        quote=quote,
        available_micro_thb=summary.available_micro_thb,
        blocked=blocked,
    )


def _parse_refinement_envelope(envelope: GeminiInteractionEnvelope) -> BoundaryRefinementResponse:
    if envelope.status.lower() != "completed" or not envelope.output_text:
        raise ValidationError("Boundary refinement has no completed provider output")
    try:
        payload = json.loads(envelope.output_text)
        return BoundaryRefinementResponse.model_validate(payload)
    except Exception as exc:
        raise ValidationError(
            "Boundary refinement provider output failed local validation"
        ) from exc


def _write_cost_artifact(service: CostService, call_id: str, path: Path) -> None:
    try:
        record = service.ledger.get(call_id)
    except CostGateError:
        return
    atomic_write_json(path, record.model_dump(mode="json"))


def _validate_refinement_upload(preparation: BoundaryRefinementPreparation, path: Path) -> None:
    try:
        resolved = path.resolve()
        resolved.relative_to(preparation.root.resolve())
    except (OSError, ValueError) as exc:
        raise ValidationError(
            "Boundary refinement may upload only its candidate-local proxy"
        ) from exc
    if resolved != preparation.slow_proxy_path.resolve():
        raise ValidationError("Boundary refinement upload path is not the committed slow proxy")
    if not resolved.is_file() or hash_file(resolved) != preparation.slow_proxy_sha256:
        raise ValidationError("Boundary refinement slow proxy failed integrity validation")


def _reuse_settled_result(
    preparation: BoundaryRefinementPreparation,
    candidate: Candidate,
    preflight: BoundaryRefinementPreflight,
    config: AppConfig,
    service: CostService,
    provider: GeminiProvider,
) -> BoundaryRefinementRun | None:
    request = preflight.request
    try:
        record = service.ledger.get(request.call_id)
    except CostGateError:
        return None
    if record.request_fingerprint != request.request_fingerprint:
        raise ValidationError("Boundary refinement ledger identity conflicts with this request")
    if record.status is not LifecycleStatus.SETTLED:
        raise ValidationError(
            f"Boundary refinement has unresolved cost lifecycle: {record.status.value}"
        )
    raw_path = preparation.root / "response.raw.json"
    applied_path = preparation.root / "response.applied.json"
    remote_path = preparation.root / "gemini_remote_file.json"
    if not raw_path.is_file():
        raise ValidationError("Settled boundary refinement is missing its raw provider result")
    if remote_path.is_file():
        with suppress(Exception):
            provider.retry_remote_cleanup(remote_path)
    envelope = GeminiInteractionEnvelope.model_validate(read_json(raw_path))
    response = _parse_refinement_envelope(envelope)
    candidate_after = apply_boundary_refinement(candidate, preparation.plan, response)
    atomic_write_json(applied_path, candidate_after.model_dump(mode="json"))
    return BoundaryRefinementRun(
        preparation=preparation,
        preflight=preflight,
        candidate_before=candidate,
        candidate_after=candidate_after,
        response=response,
        cache_hit=True,
        provider_generation_calls=0,
        provider_uploads=0,
        paid_reservations_created=0,
        raw_path=raw_path,
        applied_path=applied_path,
    )


def run_boundary_refinement_diagnostic(
    preparation: BoundaryRefinementPreparation,
    candidate: Candidate,
    config: AppConfig,
    *,
    session_id: str | None = None,
    allow_remote_upload: bool = False,
    gemini_transport: Any | None = None,
    cost_service: CostService | None = None,
) -> BoundaryRefinementRun:
    service = cost_service or _build_cost_service(config)
    preflight = preflight_boundary_refinement(
        preparation,
        candidate,
        config,
        session_id=session_id,
        cost_service=service,
    )
    if preflight.blocked:
        raise ValidationError("Boundary refinement preflight is blocked by the cost gate")
    provider = GeminiProvider(
        transport=gemini_transport,
        api_key_env=config.scout.api_key_env,
        readiness_timeout_seconds=config.scout.readiness_timeout_seconds,
        readiness_poll_initial_seconds=config.scout.readiness_poll_initial_seconds,
        readiness_poll_max_seconds=config.scout.readiness_poll_max_seconds,
        cleanup_retry_limit=config.scout.cleanup_retry_limit,
    )
    reused = _reuse_settled_result(preparation, candidate, preflight, config, service, provider)
    if reused is not None:
        return reused
    if not allow_remote_upload:
        raise ValidationError("Boundary refinement requires fresh explicit remote-upload opt-in")

    prompt, schema, request = _request_parts(preparation, candidate, config, session_id)
    raw_path = preparation.root / "response.raw.json"
    applied_path = preparation.root / "response.applied.json"
    request_meta_path = preparation.root / "request_meta.json"
    remote_meta_path = preparation.root / "gemini_remote_file.json"
    cost_path = preparation.root / "cost.json"
    atomic_write_json(
        request_meta_path,
        {
            "call_id": request.call_id,
            "request_fingerprint": request.request_fingerprint,
            "request": request.request_payload,
        },
    )
    reserved = False
    in_flight = False
    settled = False
    generation_calls = 0
    provider_uploads = 0
    reservations = 0
    try:
        record = service.reserve(request, quote=preflight.quote)
        reservations += 1
        reserved = record.status in {LifecycleStatus.RESERVED, LifecycleStatus.IN_FLIGHT}
        thinking = effective_gemini_thinking(config)

        def mark_in_flight() -> None:
            nonlocal in_flight, generation_calls
            service.mark_in_flight(request.call_id)
            in_flight = True
            generation_calls += 1

        provider_uploads += 1
        result = provider.execute(
            ProviderRequest(
                call_id=request.call_id,
                provider=request.provider,
                model_id=request.model,
                billing_mode=request.billing_mode,
                stage=request.stage,
                session_id=request.session_id,
                usage_estimate=request.usage_estimate,
                request_payload={
                    **request.request_payload,
                    "response_max_bytes": config.scout.response_max_bytes,
                },
            ),
            proxy_path=preparation.slow_proxy_path,
            session_proxy_root=preparation.root,
            upload_validator=lambda path: _validate_refinement_upload(preparation, path),
            prompt=prompt,
            response_schema=schema,
            media_resolution=config.scout.media_resolution,
            max_output_tokens=BOUNDARY_REFINEMENT_MAX_OUTPUT_TOKENS,
            thinking_level=thinking.wire_level,
            remote_metadata_path=remote_meta_path,
            before_generation=mark_in_flight,
        )
        envelope = GeminiInteractionEnvelope.model_validate(result.result)
        atomic_write_json(raw_path, envelope.model_dump(mode="json"))
        try:
            service.settle(
                request.call_id,
                result.usage,
                provider_request_id=result.provider_request_id,
            )
            settled = True
        finally:
            _write_cost_artifact(service, request.call_id, cost_path)
        response = _parse_refinement_envelope(envelope)
        candidate_after = apply_boundary_refinement(candidate, preparation.plan, response)
        atomic_write_json(applied_path, candidate_after.model_dump(mode="json"))
        return BoundaryRefinementRun(
            preparation=preparation,
            preflight=preflight,
            candidate_before=candidate,
            candidate_after=candidate_after,
            response=response,
            cache_hit=False,
            provider_generation_calls=generation_calls,
            provider_uploads=provider_uploads,
            paid_reservations_created=reservations,
            raw_path=raw_path,
            applied_path=applied_path,
        )
    except GeminiProviderError as exc:
        if exc.response:
            atomic_write_json(raw_path, dict(exc.response))
        if exc.may_have_dispatched or in_flight:
            with suppress(Exception):
                service.mark_ambiguous(request.call_id, str(exc))
        elif reserved:
            with suppress(Exception):
                service.release(request.call_id)
        _write_cost_artifact(service, request.call_id, cost_path)
        raise ValidationError(
            "Boundary refinement provider call failed; no automatic retry was attempted"
        ) from exc
    except BaseException:
        if in_flight and not settled:
            with suppress(Exception):
                service.mark_ambiguous(request.call_id, "local failure after provider dispatch")
        elif reserved and not settled:
            with suppress(Exception):
                service.release(request.call_id)
        _write_cost_artifact(service, request.call_id, cost_path)
        raise
