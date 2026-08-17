"""M5 one-window Gemini Scout pipeline and offline preflight."""

from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from game_highlight_finder.config import AppConfig
from game_highlight_finder.cost.fx import FxSnapshot
from game_highlight_finder.cost.models import CostQuote
from game_highlight_finder.cost.production import production_pricing_catalog
from game_highlight_finder.cost.service import CostRequest, CostService
from game_highlight_finder.domain.canonical import canonicalize_scout_response
from game_highlight_finder.domain.models import (
    ArtifactIdentity,
    ErrorRecord,
    SessionMap,
    SourceAsset,
    StageStatus,
    model_json,
)
from game_highlight_finder.errors import (
    AppError,
    BudgetExceededError,
    CostGateError,
    CostSafetyHoldError,
    ErrorCategory,
    ValidationError,
)
from game_highlight_finder.logging import RunLogger
from game_highlight_finder.pipeline.gemini_contract import (
    build_gemini_prompt,
    gemini_scout_schema,
    schema_hash,
)
from game_highlight_finder.pipeline.local_signals import LocalSignalsResult
from game_highlight_finder.pipeline.manifest import (
    complete_stage,
    ensure_m3_stages,
    fail_stage,
    recover_interrupted,
    start_stage,
)
from game_highlight_finder.pipeline.proxy import ProxyResult
from game_highlight_finder.providers.base import (
    MAX_USAGE_TOKENS_PER_DIMENSION,
    ProviderRegistry,
    ProviderRequest,
    ProviderUsageEstimate,
)
from game_highlight_finder.providers.gemini import (
    FakeGeminiTransport,
    GeminiInteractionEnvelope,
    GeminiProvider,
    GeminiProviderError,
    gemini_provider_descriptor,
    usage_from_envelope,
)
from game_highlight_finder.providers.gemini_capabilities import (
    MODEL_DEFAULT_MINIMUM_THINKING,
    GeminiThinkingConfig,
    resolve_gemini_thinking_config,
)
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.hashing import hash_file
from game_highlight_finder.storage.lock import SessionLock
from game_highlight_finder.storage.sessions import (
    artifact_identity,
    completed_stage_cache_is_valid,
    compute_gemini_provider_cache_key,
    load_manifest,
    session_paths,
    write_manifest,
)

if TYPE_CHECKING:
    from game_highlight_finder.pipeline.scout import ScoutResult


class GeminiPreflightResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = "gemini"
    model: str
    billing_mode: str
    media_resolution: str
    thinking_level: str | None
    reserved_thinking_tokens: int
    effective_thinking_mode: str
    thinking_policy: str = MODEL_DEFAULT_MINIMUM_THINKING
    usage_estimate: ProviderUsageEstimate
    quote: CostQuote
    available_micro_thb: int
    proxy_only: bool = True


def build_gemini_registry() -> ProviderRegistry:
    return ProviderRegistry([gemini_provider_descriptor()])


def effective_gemini_thinking(config: AppConfig) -> GeminiThinkingConfig:
    """Resolve model-aware thinking semantics before cost or provider work."""

    try:
        return resolve_gemini_thinking_config(
            config.scout.model,
            config.scout.thinking_level,
            config.scout.reserved_thinking_tokens,
        )
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def summarize_local_signals(local_signals: LocalSignalsResult) -> dict[str, Any]:
    """Bound local hints to a small deterministic prompt payload."""

    artifact = local_signals.signals
    return {
        "audio_present": artifact.audio_present,
        "overall_loudness_lufs": artifact.overall_loudness_lufs,
        "silence_intervals": [
            {"start_ms": item.start_ms, "end_ms": item.end_ms}
            for item in artifact.silence_intervals[:32]
        ],
        "active_intervals": [
            {
                "start_ms": item.start_ms,
                "end_ms": item.end_ms,
                "mean_db": item.mean_db,
                "active": item.active,
            }
            for item in artifact.audio_activity[:64]
        ],
        "warnings": artifact.warnings[:16],
    }


def estimate_gemini_usage(
    *,
    duration_ms: int,
    prompt: str,
    response_schema: dict[str, Any],
    audio_present: bool,
    max_output_tokens: int,
    reserved_thinking_tokens: int,
) -> ProviderUsageEstimate:
    """Conservatively estimate low-resolution video plus bounded text context.

    Google's current video guidance is approximately 66 vision tokens/second
    plus 32 audio tokens/second at low resolution.  Prompt/schema text and the
    complete configured response and local thinking reservation allowances are
    reserved as well.  The latter is not a provider-enforced hard ceiling.
    """

    if duration_ms <= 0:
        raise ValidationError("Gemini Scout duration must be positive")
    if max_output_tokens < 0 or reserved_thinking_tokens < 0:
        raise ValidationError("Gemini output and thinking reservation cannot be negative")
    if max_output_tokens + reserved_thinking_tokens > MAX_USAGE_TOKENS_PER_DIMENSION:
        raise ValidationError("Gemini output and thinking reservation exceed the safety bound")
    seconds = (duration_ms + 999) // 1000
    schema_bytes = len(schema_json_for(response_schema).encode("utf-8"))
    text_tokens = max(1, (len(prompt.encode("utf-8")) + 3) // 4)
    text_tokens += max(1, (schema_bytes + 3) // 4) + 128
    return ProviderUsageEstimate(
        input_text_tokens=min(10_000_000, text_tokens),
        input_video_tokens=min(10_000_000, seconds * 66),
        input_audio_tokens=min(10_000_000, seconds * 32 if audio_present else 0),
        output_tokens=max_output_tokens,
        thinking_tokens=reserved_thinking_tokens,
    )


def preflight_gemini_scout(
    source: SourceAsset,
    proxy: ProxyResult,
    local_signals: LocalSignalsResult,
    config: AppConfig,
    *,
    cost_service: CostService | None = None,
) -> GeminiPreflightResult:
    """Quote a Gemini request without reserving, uploading, or generating."""

    _validate_window(source, config)
    _, _, semantic_payload, estimate = _request_parts(source, proxy, local_signals, config)
    thinking = effective_gemini_thinking(config)
    service = cost_service or _build_cost_service(config)
    request = CostRequest(
        call_id="preflight-gemini",
        provider="gemini",
        model=config.scout.model,
        billing_mode=config.scout.billing_mode,
        stage="scout",
        session_id=None,
        usage_estimate=estimate,
        request_payload=semantic_payload,
    )
    quote = service.quote(request)
    summary = service.summary()
    if summary.safety_hold_active:
        raise CostSafetyHoldError(
            "Cost safety hold is active; Gemini preflight is blocked until reconciliation.",
            hint=summary.safety_hold_reason,
        )
    if quote.reserved_cost_micro_thb > summary.available_micro_thb:
        raise BudgetExceededError(
            hint=(
                f"Gemini preflight requires {quote.reserved_cost_micro_thb} micro-THB, "
                f"but only {summary.available_micro_thb} is available."
            )
        )
    return GeminiPreflightResult(
        model=config.scout.model,
        billing_mode=config.scout.billing_mode,
        media_resolution=config.scout.media_resolution,
        thinking_level=thinking.wire_level,
        reserved_thinking_tokens=thinking.reserved_thinking_tokens,
        effective_thinking_mode=thinking.effective_mode,
        thinking_policy=thinking.policy,
        usage_estimate=estimate,
        quote=quote,
        available_micro_thb=summary.available_micro_thb,
    )


def generate_gemini_scout(
    source: SourceAsset,
    proxy: ProxyResult,
    local_signals: LocalSignalsResult,
    config: AppConfig,
    *,
    transport: Any | None = None,
    cost_service: CostService | None = None,
) -> ScoutResult:
    """Run one bounded, explicitly-authorized Gemini Scout request.

    The return type is the existing ``pipeline.scout.ScoutResult`` class,
    imported lazily to avoid a module cycle while keeping Fake and Gemini
    callers on one result contract.
    """

    from game_highlight_finder.pipeline.scout import ScoutResult

    if not config.scout.allow_remote_upload:
        raise ValidationError(
            "Gemini Scout requires explicit remote-upload opt-in.",
            hint="Set scout.allow_remote_upload=true or pass --allow-remote-upload.",
        )
    thinking = effective_gemini_thinking(config)
    _validate_window(source, config)
    if proxy.session_id != _session_id(source) or local_signals.session_id != _session_id(source):
        raise ValidationError("Gemini Scout inputs belong to different sessions.")
    paths = session_paths(config.storage.data_dir, proxy.session_id)
    for directory in (paths.scout_raw_dir, paths.scout_canonical_dir, paths.tmp_dir):
        directory.mkdir(parents=True, exist_ok=True)
    if not proxy.proxy_path.is_file():
        raise ValidationError("Gemini Scout requires a committed analysis proxy.")
    try:
        proxy.proxy_path.resolve().relative_to(paths.proxy_dir.resolve())
    except ValueError as exc:
        raise ValidationError(
            "Gemini Scout proxy path escapes the session proxy directory."
        ) from exc

    prompt, schema, semantic_payload, estimate = _request_parts(
        source, proxy, local_signals, config
    )
    proxy_hash = hash_file(proxy.proxy_path)
    summary_hash = _hash_json(summarize_local_signals(local_signals))
    paid_cache_key = compute_gemini_provider_cache_key(
        source,
        config,
        proxy_artifact_sha256=proxy_hash,
        local_signals_summary_hash=summary_hash,
        prompt_hash=_sha256_text(prompt),
        schema_hash=schema_hash(),
    )
    raw_path = paths.scout_raw_dir / "gemini_response.json"
    request_meta_path = paths.scout_raw_dir / "gemini_request_meta.json"
    remote_meta_path = paths.scout_raw_dir / "gemini_remote_file.json"
    canonical_path = paths.scout_canonical_dir / "scout_result.json"
    log = RunLogger(
        paths.logs / f"run-scout-gemini-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.jsonl"
    )
    service = cost_service or _build_cost_service(config)
    provider = GeminiProvider(
        transport=transport,
        api_key_env=config.scout.api_key_env,
        readiness_timeout_seconds=config.scout.readiness_timeout_seconds,
        readiness_poll_initial_seconds=config.scout.readiness_poll_initial_seconds,
        readiness_poll_max_seconds=config.scout.readiness_poll_max_seconds,
        cleanup_retry_limit=config.scout.cleanup_retry_limit,
    )

    with SessionLock(paths.lock):
        manifest = load_manifest(paths.manifest)
        changed = ensure_m3_stages(manifest)
        if recover_interrupted(manifest):
            changed = True
        if changed:
            write_manifest(paths.manifest, manifest)
        stage = manifest.stages["scout"]
        # A completed provider response with pending cleanup is reusable.  Retry
        # deletion before accepting a cache hit and never regenerate for cleanup.
        if stage.status is StageStatus.COMPLETED and stage.cache_key == paid_cache_key:
            if stage.item_states.get("remote_cleanup") != "COMPLETED":
                provider.retry_remote_cleanup(remote_meta_path)
                if remote_meta_path.is_file():
                    metadata = read_json(remote_meta_path)
                    if metadata.get("deletion_status") == "deleted":
                        stage.item_states["remote_cleanup"] = "COMPLETED"
                        write_manifest(paths.manifest, manifest)
            valid, reason = completed_stage_cache_is_valid(
                paths, manifest, stage_name="scout", expected_cache_key=paid_cache_key
            )
            if valid:
                session_map = _load_session_map(paths.session_map)
                return ScoutResult(
                    session_id=manifest.session_id,
                    backend="gemini",
                    cache_hit=True,
                    cache_reason=reason,
                    raw_path=raw_path,
                    canonical_path=canonical_path,
                    session_map_path=paths.session_map,
                    session_map=session_map,
                    session_dir=paths.root,
                )
        if stage.status is StageStatus.COMPLETED:
            stage.status = StageStatus.STALE
            stage.reason = "Gemini paid-request cache identity changed"

        # Reuse a persisted paid provider response when canonicalization or a
        # previous cleanup attempt failed.  This branch performs no reservation,
        # upload, or generation.
        existing_envelope = _load_reusable_envelope(request_meta_path, raw_path, paid_cache_key)
        if existing_envelope is not None:
            start_stage(manifest, "scout", paid_cache_key)
            write_manifest(paths.manifest, manifest)
            try:
                session_map = _canonicalize_envelope(
                    existing_envelope,
                    source,
                    canonical_path,
                    paths,
                    media_resolution=config.scout.media_resolution,
                    thinking=thinking,
                    max_response_bytes=config.scout.response_max_bytes,
                )
                _complete_gemini_stage(
                    manifest,
                    paths,
                    paid_cache_key,
                    raw_path,
                    canonical_path,
                    proxy_hash,
                    local_signals.signals_path,
                    item_states={
                        "generation": "COMPLETED",
                        "remote_cleanup": _cleanup_state(remote_meta_path),
                    },
                )
                write_manifest(paths.manifest, manifest)
                return ScoutResult(
                    session_id=manifest.session_id,
                    backend="gemini",
                    cache_hit=False,
                    cache_reason="paid-result-reused-for-local-canonicalization",
                    raw_path=raw_path,
                    canonical_path=canonical_path,
                    session_map_path=paths.session_map,
                    session_map=session_map,
                    session_dir=paths.root,
                )
            except BaseException as exc:
                _record_stage_failure(manifest, paths, exc)
                raise

        start_stage(manifest, "scout", paid_cache_key)
        write_manifest(paths.manifest, manifest)
        base_call_id = f"gemini-{paid_cache_key[:48]}"
        call_id = base_call_id
        try:
            existing_call = service.ledger.get(base_call_id)
        except CostGateError:
            existing_call = None
        if existing_call is not None:
            if existing_call.status.value in {"AMBIGUOUS", "IN_FLIGHT", "RESERVED"}:
                raise ValidationError(
                    "A previous Gemini call has an unresolved cost lifecycle state.",
                    hint="Reconcile the call before attempting another paid request.",
                )
            if existing_call.status.value == "SETTLED":
                raise ValidationError(
                    "A settled Gemini call has no reusable provider artifact.",
                    hint="Restore the session artifact or reconcile the ledger before retrying.",
                )
            if existing_call.status.value == "RELEASED":
                call_id = f"{base_call_id}-retry"
        request = CostRequest(
            call_id=call_id,
            provider="gemini",
            model=config.scout.model,
            billing_mode=config.scout.billing_mode,
            stage="scout",
            session_id=manifest.session_id,
            usage_estimate=estimate,
            request_payload=semantic_payload,
        )
        atomic_write_json(
            request_meta_path,
            {
                "cache_key": paid_cache_key,
                "provider": "gemini",
                "model": config.scout.model,
                "billing_mode": config.scout.billing_mode,
                "call_id": request.call_id,
                "request_fingerprint": request.request_fingerprint,
                "prompt_version": config.scout.prompt_version,
                "prompt_hash": _sha256_text(prompt),
                "schema_hash": schema_hash(),
                "media_resolution": config.scout.media_resolution,
                "thinking": thinking.payload(),
                "usage_estimate": estimate.model_dump(mode="json"),
                "proxy_artifact_sha256": proxy_hash,
                "local_signals_summary_hash": summary_hash,
            },
        )
        reserved = False
        in_flight = False

        def mark_in_flight_before_generation() -> None:
            nonlocal in_flight
            _mark_in_flight(service, request.call_id)
            in_flight = True

        try:
            # M4 is authoritative: quote/reserve before any upload.
            record = service.reserve(request)
            reserved = record.status.value in {"RESERVED", "IN_FLIGHT"}

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
                        **semantic_payload,
                        "response_max_bytes": config.scout.response_max_bytes,
                    },
                ),
                proxy_path=proxy.proxy_path,
                session_proxy_root=paths.proxy_dir,
                prompt=prompt,
                response_schema=schema,
                media_resolution=config.scout.media_resolution,
                max_output_tokens=config.scout.max_output_tokens,
                thinking_level=thinking.wire_level,
                remote_metadata_path=remote_meta_path,
                before_generation=mark_in_flight_before_generation,
            )
            envelope = GeminiInteractionEnvelope.model_validate(result.result)
            atomic_write_json(raw_path, envelope.model_dump(mode="json"))
            try:
                service.settle(
                    request.call_id,
                    result.usage,
                    provider_request_id=result.provider_request_id,
                )
            finally:
                _write_cost_artifact(service, request.call_id, paths.scout_dir / "cost.json")
            session_map = _canonicalize_envelope(
                envelope,
                source,
                canonical_path,
                paths,
                media_resolution=config.scout.media_resolution,
                thinking=thinking,
                max_response_bytes=config.scout.response_max_bytes,
            )
            _complete_gemini_stage(
                manifest,
                paths,
                paid_cache_key,
                raw_path,
                canonical_path,
                proxy_hash,
                local_signals.signals_path,
                item_states={
                    "generation": "COMPLETED",
                    "remote_cleanup": envelope.remote_cleanup_status.upper(),
                },
            )
            write_manifest(paths.manifest, manifest)
            log.write(
                "INFO",
                "gemini_scout_completed",
                "Gemini Scout completed using the analysis proxy only.",
                model=config.scout.model,
                call_id=request.call_id,
                remote_cleanup=envelope.remote_cleanup_status,
            )
            return ScoutResult(
                session_id=manifest.session_id,
                backend="gemini",
                cache_hit=False,
                cache_reason="generated",
                raw_path=raw_path,
                canonical_path=canonical_path,
                session_map_path=paths.session_map,
                session_map=session_map,
                session_dir=paths.root,
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
            diagnostic = exc.safe_diagnostic()
            _write_cost_artifact(
                service,
                request.call_id,
                paths.scout_dir / "cost.json",
                failure_diagnostic=diagnostic,
            )
            _record_stage_failure(manifest, paths, exc)
            raise ValidationError(
                str(exc),
                hint=(
                    "No automatic Gemini generation retry was attempted."
                    if diagnostic is None
                    else "No automatic Gemini generation retry was attempted; "
                    f"phase={diagnostic.get('phase')}, dispatch={diagnostic.get('dispatch')}."
                ),
            ) from exc
        except BaseException as exc:
            if in_flight:
                with suppress(Exception):
                    service.mark_ambiguous(request.call_id, type(exc).__name__)
            elif reserved:
                with suppress(Exception):
                    service.release(request.call_id)
            _record_stage_failure(manifest, paths, exc)
            raise


def _request_parts(
    source: SourceAsset,
    proxy: ProxyResult,
    local_signals: LocalSignalsResult,
    config: AppConfig,
) -> tuple[str, dict[str, Any], dict[str, Any], ProviderUsageEstimate]:
    thinking = effective_gemini_thinking(config)
    summary = summarize_local_signals(local_signals)
    prompt = build_gemini_prompt(
        duration_ms=source.duration_ms,
        game_profile="unknown",
        local_signal_summary=summary,
        prompt_version=config.scout.prompt_version,
    )
    schema = gemini_scout_schema()
    estimate = estimate_gemini_usage(
        duration_ms=source.duration_ms,
        prompt=prompt,
        response_schema=schema,
        audio_present=bool(summary.get("audio_present")),
        max_output_tokens=config.scout.max_output_tokens,
        reserved_thinking_tokens=thinking.reserved_thinking_tokens,
    )
    payload = {
        "prompt_version": config.scout.prompt_version,
        "prompt_hash": _sha256_text(prompt),
        "schema_hash": schema_hash(),
        "schema_version": config.scout.schema_version,
        "media_resolution": config.scout.media_resolution,
        "input_duration_ms": source.duration_ms,
        "proxy_artifact_sha256": hash_file(proxy.proxy_path),
        "local_signals_summary_hash": _hash_json(summary),
        "response_schema": schema,
        "max_output_tokens": config.scout.max_output_tokens,
        "configured_thinking_level": thinking.configured_level,
        "thinking_level": thinking.wire_level,
        "effective_thinking_mode": thinking.effective_mode,
        "thinking_policy": thinking.policy,
        "reserved_thinking_tokens": thinking.reserved_thinking_tokens,
    }
    return prompt, schema, payload, estimate


def _build_cost_service(config: AppConfig) -> CostService:
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


def _validate_window(source: SourceAsset, config: AppConfig) -> None:
    if source.duration_ms > config.scout.max_duration_seconds * 1000:
        raise ValidationError(
            "Long-session Gemini Scout requires M6 windowing.",
            hint=(
                f"The M5 single-request limit is {config.scout.max_duration_seconds} seconds; "
                "the source is longer."
            ),
        )


def _canonicalize_envelope(
    envelope: GeminiInteractionEnvelope,
    source: SourceAsset,
    canonical_path: Path,
    paths: Any,
    *,
    media_resolution: str,
    thinking: GeminiThinkingConfig,
    max_response_bytes: int,
) -> SessionMap:
    session_map = canonicalize_scout_response(
        envelope.output_text.encode("utf-8"),
        session_id=_session_id(source),
        source_id=source.source_id,
        source_duration_ms=source.duration_ms,
        game_profile="unknown",
        source_offset_ms=0,
        created_at=source.created_at,
        max_response_bytes=max_response_bytes,
    ).model_copy(
        update={
            "scout_backend": "gemini",
            "scout_metadata": {
                "backend": "gemini",
                "model": envelope.model,
                "interaction_id": envelope.interaction_id or "unknown",
                "media_resolution": media_resolution,
                "configured_thinking_level": thinking.configured_level,
                "thinking_level": thinking.wire_level or "omitted",
                "effective_thinking_mode": thinking.effective_mode,
                "thinking_policy": thinking.policy,
                "thinking_identity": f"{thinking.policy}:{thinking.effective_mode}",
                "remote_cleanup": envelope.remote_cleanup_status,
            },
        }
    )
    atomic_write_json(canonical_path, model_json(session_map))
    atomic_write_json(paths.session_map, model_json(session_map))
    return session_map


def _complete_gemini_stage(
    manifest: Any,
    paths: Any,
    cache_key: str,
    raw_path: Path,
    canonical_path: Path,
    proxy_hash: str,
    signals_path: Path,
    *,
    item_states: dict[str, str],
) -> None:
    inputs = [
        ArtifactIdentity(
            path="proxy/analysis_proxy.mp4",
            sha256=proxy_hash,
            size_bytes=(paths.root / "proxy" / "analysis_proxy.mp4").stat().st_size,
        ),
        artifact_identity(signals_path, relative_to=paths.root),
    ]
    outputs = [
        artifact_identity(raw_path, relative_to=paths.root),
        artifact_identity(canonical_path, relative_to=paths.root),
        artifact_identity(paths.session_map, relative_to=paths.root),
    ]
    complete_stage(
        manifest,
        "scout",
        inputs=inputs,
        outputs=outputs,
        item_states=item_states,
    )


def _record_stage_failure(manifest: Any, paths: Any, exc: BaseException) -> None:
    try:
        fail_stage(manifest, "scout", _error_record(exc))
        write_manifest(paths.manifest, manifest)
    except Exception:
        pass


def _mark_in_flight(service: CostService, call_id: str) -> None:
    service.mark_in_flight(call_id)


def _load_reusable_envelope(
    request_meta_path: Path, raw_path: Path, cache_key: str
) -> GeminiInteractionEnvelope | None:
    if not request_meta_path.is_file() or not raw_path.is_file():
        return None
    try:
        metadata = read_json(request_meta_path)
        if metadata.get("cache_key") != cache_key:
            return None
        envelope = GeminiInteractionEnvelope.model_validate(read_json(raw_path))
        if envelope.status.lower() != "completed":
            return None
        usage_from_envelope(envelope)
        return envelope
    except Exception:
        return None


def _cleanup_state(path: Path) -> str:
    if not path.is_file():
        return "PENDING"
    try:
        return "COMPLETED" if read_json(path).get("deletion_status") == "deleted" else "PENDING"
    except Exception:
        return "PENDING"


def _load_session_map(path: Path) -> SessionMap:
    try:
        return SessionMap.model_validate(read_json(path))
    except Exception as exc:
        raise ValidationError(
            "Stored Gemini canonical session map is invalid.", hint=str(exc)
        ) from exc


def _write_cost_artifact(
    service: CostService,
    call_id: str,
    path: Path,
    *,
    failure_diagnostic: dict[str, object] | None = None,
) -> None:
    record = service.ledger.get(call_id)
    payload: dict[str, object] = {
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
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }
    if failure_diagnostic is not None:
        payload["failure_diagnostic"] = failure_diagnostic
    atomic_write_json(path, payload)


def schema_json_for(schema: dict[str, Any]) -> str:
    return json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hash_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _session_id(source: SourceAsset) -> str:
    from game_highlight_finder.storage.sessions import make_session_id

    return make_session_id(source)


def _error_record(exc: BaseException) -> ErrorRecord:
    if isinstance(exc, AppError):
        return ErrorRecord(
            category=exc.category.value,
            message=exc.message,
            hint=exc.hint,
            retryable=exc.category in {ErrorCategory.STORAGE, ErrorCategory.INTERNAL},
        )
    if isinstance(exc, GeminiProviderError) and exc.diagnostic is not None:
        diagnostic = exc.diagnostic
        return ErrorRecord(
            category=ErrorCategory.PROVIDER.value,
            message="Gemini Scout attempt did not complete.",
            hint=(
                f"phase={diagnostic.phase}; dispatch={diagnostic.dispatch}; "
                f"exception={diagnostic.exception_class}"
            ),
            retryable=False,
        )
    return ErrorRecord(
        category=ErrorCategory.PROVIDER.value,
        message="Gemini Scout attempt did not complete.",
        hint=type(exc).__name__,
        retryable=False,
    )


__all__ = [
    "FakeGeminiTransport",
    "GeminiPreflightResult",
    "build_gemini_registry",
    "effective_gemini_thinking",
    "estimate_gemini_usage",
    "generate_gemini_scout",
    "preflight_gemini_scout",
    "summarize_local_signals",
]
