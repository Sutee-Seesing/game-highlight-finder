"""Offline M6 window preparation, fake Scout execution, and cost preflight."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from game_highlight_finder.config import AppConfig
from game_highlight_finder.cost.fx import FxSnapshot
from game_highlight_finder.cost.ledger import LedgerRecord, LifecycleStatus
from game_highlight_finder.cost.production import production_pricing_catalog
from game_highlight_finder.cost.service import CostRequest, CostService
from game_highlight_finder.domain.canonical import canonicalize_scout_response, parse_scout_response
from game_highlight_finder.domain.models import (
    LocalSignalsArtifact,
    SessionMap,
    SourceAsset,
    model_json,
)
from game_highlight_finder.domain.windows import ScoutWindow, WindowPlan, plan_scout_windows
from game_highlight_finder.errors import CostGateError, ValidationError
from game_highlight_finder.media.ffmpeg import build_window_proxy_command, run_ffmpeg
from game_highlight_finder.media.ffprobe import run_ffprobe
from game_highlight_finder.media.tools import tool_identity
from game_highlight_finder.pipeline.gemini_contract import gemini_window_scout_schema
from game_highlight_finder.pipeline.gemini_scout import (
    build_gemini_registry,
    effective_gemini_media_resolution,
    effective_gemini_thinking,
    estimate_gemini_usage,
)
from game_highlight_finder.pipeline.local_signals import LocalSignalsResult
from game_highlight_finder.pipeline.proxy import ProxyResult
from game_highlight_finder.providers.base import ProviderRequest
from game_highlight_finder.providers.gemini import (
    GeminiInteractionEnvelope,
    GeminiProvider,
    GeminiProviderError,
)
from game_highlight_finder.storage.atomic import atomic_write_bytes, atomic_write_json, read_json
from game_highlight_finder.storage.hashing import hash_file
from game_highlight_finder.storage.sessions import session_paths


class WindowPreparationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    plan: WindowPlan
    windows: tuple[ScoutWindow, ...]
    cache_hits: int = 0
    generated: int = 0
    session_dir: Path


class WindowScoutResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    window: ScoutWindow
    cache_hit: bool
    cache_reason: str
    raw_path: Path
    canonical_path: Path
    session_map: SessionMap


class ExecutionActivity(BaseModel):
    """Local, per-invocation provider activity observed by the window runner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scout_backend: str = Field(min_length=1, max_length=32)
    provider_generation_calls: int = Field(default=0, ge=0)
    provider_uploads: int = Field(default=0, ge=0)
    paid_reservations_created: int = Field(default=0, ge=0)
    cache_hits: int = Field(default=0, ge=0)


class WindowedScoutRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    plan: WindowPlan
    results: tuple[WindowScoutResult, ...]
    missing_windows: tuple[str, ...] = ()
    aggregate_preflight: AggregateCostPreflight
    activity: ExecutionActivity = Field(
        default_factory=lambda: ExecutionActivity(scout_backend="unknown")
    )


class AggregateCostPreflight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    total_windows: int = Field(ge=0)
    cached_windows: int = Field(ge=0)
    missing_windows: int = Field(ge=0)
    estimated_micro_thb: int = Field(ge=0)
    available_micro_thb: int | None = Field(default=None, ge=0)
    blocked: bool = False
    reason: str = "offline fake provider"
    window_estimates_micro_thb: dict[str, int] = Field(default_factory=dict, max_length=10_000)


@dataclass(frozen=True)
class _PaidWindowRecovery:
    """Ledger-authoritative recovery decision for one Gemini window."""

    call_id: str
    ledger: LedgerRecord | None = None
    envelope: GeminiInteractionEnvelope | None = None
    full_cache: bool = False
    retry_call_id: str | None = None


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _signal_summary(signals: LocalSignalsArtifact, window: ScoutWindow) -> dict[str, Any]:
    """Intersect source-relative signals and cap each list before prompt use."""

    def interval_payload(item: Any) -> dict[str, Any] | None:
        start = max(window.source_start_ms, item.start_ms)
        end = min(window.source_end_ms, item.end_ms)
        if end <= start:
            return None
        payload = dict(item.model_dump(mode="json"))
        payload["start_ms"] = start - window.source_start_ms
        payload["end_ms"] = end - window.source_start_ms
        return payload

    silence = [
        x for item in signals.silence_intervals if (x := interval_payload(item)) is not None
    ][:64]
    activity = [x for item in signals.audio_activity if (x := interval_payload(item)) is not None][
        :128
    ]
    scene = [x for item in signals.scene_activity if (x := interval_payload(item)) is not None][:64]
    return {
        "window_start_ms": window.source_start_ms,
        "window_end_ms": window.source_end_ms,
        "silence_intervals": silence,
        "audio_activity": activity,
        "scene_activity": scene,
        "overall_loudness_lufs": signals.overall_loudness_lufs,
        "warnings": signals.warnings[:16],
    }


def _summary_hash(summary: Mapping[str, Any]) -> str:
    return _sha256_bytes(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def validate_window_proxy_upload(
    path: Path,
    session_windows_root: Path,
    *,
    expected_parent_proxy_sha256: str | None = None,
) -> ScoutWindow:
    """Accept only a committed M6 window proxy with matching provenance."""

    resolved = path.resolve()
    root = session_windows_root.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ValidationError("M6 upload path escapes the committed Scout-window root") from exc
    if relative.name != "analysis_window.mp4" or len(relative.parts) != 2:
        raise ValidationError("M6 uploads must be committed analysis_window.mp4 artifacts")
    metadata_path = resolved.parent / "window.json"
    if not resolved.is_file() or not metadata_path.is_file():
        raise ValidationError("M6 window proxy or provenance metadata is missing")
    try:
        window = ScoutWindow.model_validate(read_json(metadata_path))
    except Exception as exc:
        raise ValidationError("M6 window provenance metadata is invalid") from exc
    if window.proxy_path.replace("\\", "/") != str(
        resolved.relative_to(root.parent.parent).as_posix()
    ):
        raise ValidationError("M6 window proxy path does not match provenance metadata")
    if window.proxy_sha256 != hash_file(resolved) or window.parent_proxy_sha256 is None:
        raise ValidationError("M6 window proxy hash/provenance does not match")
    if (
        expected_parent_proxy_sha256 is not None
        and window.parent_proxy_sha256 != expected_parent_proxy_sha256
    ):
        raise ValidationError("M6 window proxy parent provenance does not match analysis proxy")
    return window


def prepare_scout_windows(
    source: SourceAsset,
    proxy: ProxyResult,
    local_signals: LocalSignalsResult,
    config: AppConfig,
    *,
    force: bool = False,
) -> WindowPreparationResult:
    """Create/reuse bounded window proxies from the committed analysis proxy."""

    if proxy.session_id != local_signals.session_id:
        raise ValidationError("window inputs belong to different sessions")
    if not proxy.proxy_path.is_file():
        raise ValidationError("window planning requires a committed analysis proxy")
    paths = session_paths(config.storage.data_dir, proxy.session_id)
    paths.scout_windows_dir.mkdir(parents=True, exist_ok=True)
    paths.tmp_dir.mkdir(parents=True, exist_ok=True)
    parent_sha = hash_file(proxy.proxy_path)
    plan = plan_scout_windows(
        source.duration_ms,
        max_duration_ms=config.scout.window_duration_seconds * 1_000,
        overlap_ms=config.scout.window_overlap_seconds * 1_000,
        session_id=proxy.session_id,
        source_id=source.source_id,
        max_windows=config.scout.max_windows,
    )
    ffmpeg = tool_identity("ffmpeg", config.tools.ffmpeg_path)
    ffprobe = tool_identity("ffprobe", config.tools.ffprobe_path, include_capabilities=False)
    windows: list[ScoutWindow] = []
    cache_hits = 0
    generated = 0
    for planned in plan.windows:
        item_dir = paths.scout_windows_dir / planned.window_id
        item_dir.mkdir(parents=True, exist_ok=True)
        proxy_rel = f"scout/windows/{planned.window_id}/analysis_window.mp4"
        window_proxy = paths.root / proxy_rel
        summary = _signal_summary(local_signals.signals, planned)
        summary_path = item_dir / "signals.json"
        window_json = item_dir / "window.json"
        expected_summary_hash = _summary_hash(summary)
        existing: ScoutWindow | None = None
        if window_json.is_file() and window_proxy.is_file() and summary_path.is_file():
            try:
                existing = ScoutWindow.model_validate(read_json(window_json))
            except Exception:
                existing = None
        if (
            not force
            and existing is not None
            and existing.parent_proxy_sha256 == parent_sha
            and existing.proxy_sha256 == hash_file(window_proxy)
            and existing.signal_summary_hash == expected_summary_hash
        ):
            windows.append(existing)
            cache_hits += 1
            continue
        start_proxy_ms = max(
            0,
            proxy.metadata.timestamp_mapping.source_to_proxy_ms(
                planned.source_start_ms + (source.timestamp_origin_ms or 0)
            ),
        )
        temp = paths.tmp_dir / f"window-{planned.window_id}.partial.mp4"
        temp.unlink(missing_ok=True)
        run_ffmpeg(
            build_window_proxy_command(
                ffmpeg.path,
                proxy.proxy_path,
                temp,
                proxy_start_ms=start_proxy_ms,
                duration_ms=planned.duration_ms,
                has_audio=proxy.metadata.audio_present,
                video_codec=config.media.proxy.video_codec,
                preset=config.media.proxy.preset,
            ),
            duration_ms=planned.duration_ms,
            timeout_seconds=config.tools.ffmpeg_timeout_seconds,
            termination_grace_seconds=config.tools.termination_grace_seconds,
        )
        if not temp.is_file() or temp.stat().st_size <= 0:
            raise ValidationError(f"window proxy was not produced: {planned.window_id}")
        # Probe is deliberately local and validates the output before commit.
        run_ffprobe(ffprobe.path, temp, timeout_seconds=config.tools.probe_timeout_seconds)
        temp.replace(window_proxy)
        atomic_write_json(summary_path, summary)
        committed = planned.model_copy(
            update={
                "proxy_path": proxy_rel,
                "proxy_sha256": hash_file(window_proxy),
                "parent_proxy_sha256": parent_sha,
                "signal_summary_hash": expected_summary_hash,
                "warnings": list(
                    dict.fromkeys([*planned.warnings, *local_signals.signals.warnings])
                )[:32],
            }
        )
        atomic_write_json(window_json, model_json(committed))
        windows.append(committed)
        generated += 1
    committed_plan = plan.model_copy(update={"windows": windows})
    return WindowPreparationResult(
        plan=committed_plan,
        windows=tuple(windows),
        cache_hits=cache_hits,
        generated=generated,
        session_dir=paths.root,
    )


class FakeWindowScout:
    """Deterministic offline provider-shaped window fixture.

    ``calls`` is intentionally observable by tests to prove cache reuse and
    interrupted recovery never trigger duplicate generation.
    """

    def __init__(self, responses: Mapping[str, bytes | Mapping[str, Any]] | None = None) -> None:
        self.responses = dict(responses or {})
        self.calls: list[str] = []

    def generate(
        self,
        *,
        window: ScoutWindow,
        source_duration_ms: int,
        source_sha256: str,
        summary: Mapping[str, Any],
    ) -> bytes:
        self.calls.append(window.window_id)
        custom = self.responses.get(window.window_id)
        if custom is not None:
            if isinstance(custom, bytes):
                return custom
            return json.dumps(dict(custom), ensure_ascii=False, sort_keys=True).encode("utf-8")
        length = window.duration_ms
        matches: list[dict[str, Any]] = [
            {
                "start_ms": 0,
                "end_ms": length,
                "confidence": 0.55,
                "label": f"window {window.ordinal}",
                "ordinal": window.ordinal,
                "evidence": [],
                "candidates": [],
            }
        ]
        candidates: list[dict[str, Any]] = []
        if length >= 2_000:
            start = max(0, length // 2 - 500)
            end = min(length, start + 1_000)
            candidates.append(
                {
                    "start_ms": start,
                    "end_ms": end,
                    "category": "OTHER" if source_sha256[0] in "0123" else "CLUTCH",
                    "score": 6.0,
                    "confidence": 0.60,
                    "reason": "Deterministic offline window fixture.",
                    "evidence": [],
                    "match_index": 0,
                }
            )
        payload = {
            "schema_version": 1,
            "source_duration_ms": source_duration_ms,
            "time_basis": "window_relative",
            "window_start_ms": window.source_start_ms,
            "window_end_ms": window.source_end_ms,
            "matches": matches,
            "candidates": candidates,
            "warnings": [],
            "metadata": {"backend": "fake-window", "window_id": window.window_id},
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def build_window_prompt(
    *,
    source_duration_ms: int,
    window: ScoutWindow,
    local_signal_summary: Mapping[str, Any],
    prompt_version: str = "gemini-scout-window-v2",
) -> str:
    summary = json.dumps(
        dict(local_signal_summary), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return "\n".join(
        [
            f"Game Highlight Finder window Scout {prompt_version}.",
            "Return only JSON matching the supplied schema; do not emit hidden reasoning.",
            (
                "Inspect the entire supplied video AND audio window before deciding "
                "there are no highlights."
            ),
            (
                "Primary objective: find moments a human short-form editor would plausibly "
                "want to review or share, while avoiding routine gameplay."
            ),
            (
                "Strong candidates include clutch/skill/smart play, funny or failed moments, "
                "visible or audible reactions, friend interactions, surprising/WTF events, "
                "and tension with a payoff."
            ),
            (
                "Treat voice chat, laughter, shouting, surprise, and other audible reactions "
                "as real evidence when audio is present; do not judge only from local signal "
                "hints."
            ),
            (
                "If match or round boundaries are uncertain, return zero matches if needed "
                "but STILL return worthwhile top-level candidates; uncertain segmentation "
                "must not suppress highlights."
            ),
            (
                "Prefer recall for clearly salient moments: when a plausible strong event is "
                "visible or audible, include it with appropriately lower confidence rather "
                "than omitting it; never invent an event or timestamp."
            ),
            (
                "Preserve the useful story arc. Candidate start_ms/end_ms should cover the "
                "core event; setup_start_ms and payoff_end_ms should extend to causally useful "
                "setup/reaction when present, with setup <= start < end <= payoff."
            ),
            (
                "Do not reduce a multi-step clutch, chase, fail, joke, or reaction to an "
                "isolated frame if the setup or aftermath is needed to understand why it is "
                "worth watching."
            ),
            "Candidate categories must use only values allowed by the supplied schema.",
            "Timestamps inside this request are window-relative integer milliseconds.",
            (
                "The full source timeline is authoritative; overlapping windows are "
                "reconciled locally."
            ),
            "Do not duplicate the same boundary event unless evidence supports separate moments.",
            (
                "Return zero candidates only after scanning the whole window and finding no "
                "plausible worthwhile moment."
            ),
            f"Full source duration_ms: {source_duration_ms}",
            f"Window absolute bounds_ms: {window.source_start_ms}-{window.source_end_ms}",
            f"Bounded local signals (hints only, not ground truth): {summary}",
        ]
    )


def aggregate_window_preflight(
    source: SourceAsset,
    windows: Sequence[ScoutWindow],
    config: AppConfig,
    *,
    cached_window_ids: set[str] | None = None,
    available_micro_thb: int | None = None,
    cost_service: CostService | None = None,
    local_signal_summaries: Mapping[str, Mapping[str, Any]] | None = None,
) -> AggregateCostPreflight:
    cached = cached_window_ids or set()
    missing = max(0, len(windows) - len(cached))
    if config.scout.backend == "fake":
        return AggregateCostPreflight(
            total_windows=len(windows),
            cached_windows=len(cached.intersection({window.window_id for window in windows})),
            missing_windows=missing,
            estimated_micro_thb=0,
            available_micro_thb=available_micro_thb,
            blocked=False,
            reason="offline fake provider",
        )

    service = cost_service or _build_window_cost_service(config)
    thinking = effective_gemini_thinking(config)
    summary = service.summary()
    available = summary.available_micro_thb
    estimates: dict[str, int] = {}
    total = 0
    schema = gemini_window_scout_schema()
    summaries = local_signal_summaries or {}
    for window in windows:
        if window.window_id in cached:
            continue
        prompt = build_window_prompt(
            source_duration_ms=source.duration_ms,
            window=window,
            local_signal_summary=summaries.get(window.window_id, {}),
            prompt_version=config.scout.window_prompt_version,
        )
        estimate = estimate_gemini_usage(
            duration_ms=window.duration_ms,
            prompt=prompt,
            response_schema=schema,
            audio_present=source.selected_audio_stream is not None,
            max_output_tokens=config.scout.max_output_tokens,
            reserved_thinking_tokens=thinking.reserved_thinking_tokens,
            model=config.scout.model,
            media_resolution=config.scout.media_resolution,
        )
        request = CostRequest(
            call_id=f"preflight-{window.window_id}",
            provider="gemini",
            model=config.scout.model,
            billing_mode=config.scout.billing_mode,
            stage="scout",
            session_id=window.session_id,
            usage_estimate=estimate,
            request_payload={
                "window_id": window.window_id,
                "window_start_ms": window.source_start_ms,
                "window_end_ms": window.source_end_ms,
                "window_proxy_sha256": window.proxy_sha256,
                "signal_summary_hash": window.signal_summary_hash,
                "prompt_version": config.scout.window_prompt_version,
            },
        )
        amount = service.quote(request).reserved_cost_micro_thb
        estimates[window.window_id] = amount
        total += amount
    blocked = summary.safety_hold_active or total > available
    reason = (
        summary.safety_hold_reason or "cost safety hold active"
        if summary.safety_hold_active
        else "aggregate estimate exceeds available monthly budget"
        if total > available
        else "aggregate Gemini window preflight passed"
    )
    return AggregateCostPreflight(
        total_windows=len(windows),
        cached_windows=len(cached.intersection({window.window_id for window in windows})),
        missing_windows=missing,
        estimated_micro_thb=total,
        available_micro_thb=available,
        blocked=blocked,
        reason=reason,
        window_estimates_micro_thb=estimates,
    )


def _build_window_cost_service(config: AppConfig) -> CostService:
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


def run_windowed_scout(
    source: SourceAsset,
    preparation: WindowPreparationResult,
    local_signals: LocalSignalsResult,
    config: AppConfig,
    *,
    fake_provider: FakeWindowScout | None = None,
    gemini_transport: Any | None = None,
    cost_service: CostService | None = None,
    force: bool = False,
) -> WindowedScoutRun:
    """Run each window through Fake or the fully ledger-backed Gemini path."""

    if config.scout.backend == "gemini":
        return _run_gemini_windowed_scout(
            source,
            preparation,
            config,
            transport=gemini_transport,
            cost_service=cost_service,
        )
    if config.scout.backend != "fake":
        raise ValidationError("Unsupported M6 Scout backend")
    provider = fake_provider or FakeWindowScout()
    paths = session_paths(config.storage.data_dir, preparation.plan.session_id)
    results: list[WindowScoutResult] = []
    cached_ids: set[str] = set()
    missing: list[str] = []
    generation_calls = 0
    for window in preparation.windows:
        item_dir = paths.scout_windows_dir / window.window_id
        raw_path = item_dir / "response.raw.json"
        canonical_path = item_dir / "response.canonical.json"
        request_meta_path = item_dir / "request_meta.json"
        summary_path = item_dir / "signals.json"
        summary = read_json(summary_path) if summary_path.is_file() else {}
        prompt = build_window_prompt(
            source_duration_ms=source.duration_ms,
            window=window,
            local_signal_summary=summary,
            prompt_version=config.scout.window_prompt_version,
        )
        request_payload = {
            "source_sha256": source.sha256,
            "window_id": window.window_id,
            "window_start_ms": window.source_start_ms,
            "window_end_ms": window.source_end_ms,
            "window_proxy_sha256": window.proxy_sha256,
            "signal_summary_hash": window.signal_summary_hash,
            "model": config.scout.model,
            "billing_mode": config.scout.billing_mode,
            "media_resolution": config.scout.media_resolution,
            "thinking_level": config.scout.thinking_level,
            "reserved_thinking_tokens": config.scout.reserved_thinking_tokens,
            "prompt_version": config.scout.window_prompt_version,
            "prompt_hash": _sha256_bytes(prompt.encode("utf-8")),
            "schema_version": config.scout.schema_version,
            "schema_hash": _sha256_bytes(
                json.dumps(
                    gemini_window_scout_schema(), sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ),
            "output_ceiling": config.scout.response_max_bytes,
        }
        cache_key = _sha256_bytes(
            json.dumps(request_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        valid_cache = False
        if (
            not force
            and raw_path.is_file()
            and canonical_path.is_file()
            and request_meta_path.is_file()
        ):
            try:
                meta = read_json(request_meta_path)
                valid_cache = meta.get("cache_key") == cache_key
                if valid_cache:
                    session_map = SessionMap.model_validate(read_json(canonical_path))
                    cached_ids.add(window.window_id)
                    results.append(
                        WindowScoutResult(
                            window=window.model_copy(update={"provider_cache_key": cache_key}),
                            cache_hit=True,
                            cache_reason="verified window cache",
                            raw_path=raw_path,
                            canonical_path=canonical_path,
                            session_map=session_map,
                        )
                    )
                    continue
            except Exception:
                valid_cache = False
        if raw_path.is_file() and not valid_cache and not force:
            try:
                raw_bytes = raw_path.read_bytes()
                parse_scout_response(raw_bytes, max_bytes=config.scout.response_max_bytes)
            except Exception:
                generation_calls += 1
                raw_bytes = provider.generate(
                    window=window,
                    source_duration_ms=source.duration_ms,
                    source_sha256=source.sha256,
                    summary=summary,
                )
        else:
            generation_calls += 1
            raw_bytes = provider.generate(
                window=window,
                source_duration_ms=source.duration_ms,
                source_sha256=source.sha256,
                summary=summary,
            )
        if len(raw_bytes) > config.scout.response_max_bytes:
            raise ValidationError(
                f"window response exceeds configured safety limit: {window.window_id}"
            )
        atomic_write_bytes(raw_path, raw_bytes)
        session_map = canonicalize_scout_response(
            raw_bytes,
            session_id=preparation.plan.session_id,
            source_id=source.source_id,
            source_duration_ms=source.duration_ms,
            created_at=source.created_at,
            max_response_bytes=config.scout.response_max_bytes,
            source_window_id=window.window_id,
        )
        atomic_write_json(canonical_path, model_json(session_map))
        atomic_write_json(request_meta_path, {"cache_key": cache_key, "request": request_payload})
        committed_window = window.model_copy(update={"provider_cache_key": cache_key})
        results.append(
            WindowScoutResult(
                window=committed_window,
                cache_hit=False,
                cache_reason="generated offline fake response",
                raw_path=raw_path,
                canonical_path=canonical_path,
                session_map=session_map,
            )
        )
    preflight = aggregate_window_preflight(
        source, preparation.windows, config, cached_window_ids=cached_ids
    )
    if missing:
        preflight = preflight.model_copy(update={"missing_windows": len(missing)})
    return WindowedScoutRun(
        plan=preparation.plan,
        results=tuple(results),
        activity=ExecutionActivity(
            scout_backend="fake",
            provider_generation_calls=generation_calls,
            cache_hits=len(cached_ids),
        ),
        missing_windows=tuple(missing),
        aggregate_preflight=preflight,
    )


def _run_gemini_windowed_scout(
    source: SourceAsset,
    preparation: WindowPreparationResult,
    config: AppConfig,
    *,
    transport: Any | None,
    cost_service: CostService | None,
) -> WindowedScoutRun:
    """Execute one paid, recoverable Gemini interaction for each M6 window.

    Each window has its own immutable request identity, raw response, remote
    cleanup receipt, and M4 ledger call.  A durable raw result is always
    canonicalized locally rather than regenerated; unresolved calls fail
    closed before upload or generation.
    """
    paths = session_paths(config.storage.data_dir, preparation.plan.session_id)
    parent_proxy = paths.proxy_dir / "analysis_proxy.mp4"
    if not parent_proxy.is_file():
        raise ValidationError("M6 Gemini windows require the committed analysis proxy.")
    parent_sha = hash_file(parent_proxy)
    service = cost_service or _build_window_cost_service(config)
    thinking = effective_gemini_thinking(config)
    provider = GeminiProvider(
        transport=transport,
        api_key_env=config.scout.api_key_env,
        readiness_timeout_seconds=config.scout.readiness_timeout_seconds,
        readiness_poll_initial_seconds=config.scout.readiness_poll_initial_seconds,
        readiness_poll_max_seconds=config.scout.readiness_poll_max_seconds,
        cleanup_retry_limit=config.scout.cleanup_retry_limit,
    )
    cache_keys: dict[str, str] = {}
    payloads: dict[str, tuple[dict[str, Any], str, dict[str, Any], CostRequest]] = {}
    recoveries: dict[str, _PaidWindowRecovery] = {}
    cached_ids: set[str] = set()
    generation_calls = 0
    provider_uploads = 0
    paid_reservations_created = 0
    schema = gemini_window_scout_schema()
    schema_digest = _sha256_bytes(
        json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    for window in preparation.windows:
        item_dir = paths.scout_windows_dir / window.window_id
        summary_path = item_dir / "signals.json"
        summary = read_json(summary_path) if summary_path.is_file() else {}
        prompt = build_window_prompt(
            source_duration_ms=source.duration_ms,
            window=window,
            local_signal_summary=summary,
            prompt_version=config.scout.window_prompt_version,
        )
        payload = _window_request_payload(source, window, config, prompt, schema_digest)
        cache_key = _sha256_bytes(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        cache_keys[window.window_id] = cache_key
        estimate = estimate_gemini_usage(
            duration_ms=window.duration_ms,
            prompt=prompt,
            response_schema=schema,
            audio_present=source.selected_audio_stream is not None,
            max_output_tokens=config.scout.max_output_tokens,
            reserved_thinking_tokens=thinking.reserved_thinking_tokens,
            model=config.scout.model,
            media_resolution=config.scout.media_resolution,
        )
        request = CostRequest(
            call_id=f"gemini-window-{cache_key[:44]}",
            provider="gemini",
            model=config.scout.model,
            billing_mode=config.scout.billing_mode,
            stage="scout",
            session_id=preparation.plan.session_id,
            usage_estimate=estimate,
            request_payload=payload,
        )
        payloads[window.window_id] = (payload, prompt, summary, request)
        recovery = _inspect_paid_window_recovery(
            item_dir=item_dir,
            cache_key=cache_key,
            request=request,
            service=service,
            provider=provider,
        )
        recoveries[window.window_id] = recovery
        if recovery.envelope is not None:
            # A SETTLED raw result is a paid-result cache hit even when local
            # canonicalization must be rerun.  It must not enter preflight as
            # a new billable window.
            cached_ids.add(window.window_id)
    if not config.scout.allow_remote_upload and len(cached_ids) < len(preparation.windows):
        raise ValidationError(
            "M6 Gemini Scout requires fresh explicit remote-upload opt-in for missing work."
        )
    preflight = aggregate_window_preflight(
        source,
        preparation.windows,
        config,
        cached_window_ids=cached_ids,
        cost_service=service,
    )
    if preflight.blocked:
        raise ValidationError("M6 aggregate Gemini preflight is blocked.", hint=preflight.reason)

    results: list[WindowScoutResult] = []
    for window in preparation.windows:
        item_dir = paths.scout_windows_dir / window.window_id
        raw_path = item_dir / "response.raw.json"
        canonical_path = item_dir / "response.canonical.json"
        request_meta_path = item_dir / "request_meta.json"
        remote_meta_path = item_dir / "gemini_remote_file.json"
        cost_path = item_dir / "cost.json"
        proxy_path = paths.root / window.proxy_path
        payload, prompt, _summary, base_request = payloads[window.window_id]
        cache_key = cache_keys[window.window_id]
        recovery = recoveries[window.window_id]
        committed_window = window.model_copy(update={"provider_cache_key": cache_key})
        if recovery.full_cache:
            _retry_window_cleanup(provider, remote_meta_path)
            results.append(
                WindowScoutResult(
                    window=committed_window,
                    cache_hit=True,
                    cache_reason="verified paid window cache",
                    raw_path=raw_path,
                    canonical_path=canonical_path,
                    session_map=SessionMap.model_validate(read_json(canonical_path)),
                )
            )
            continue
        if recovery.envelope is not None:
            _retry_window_cleanup(provider, remote_meta_path)
            session_map = _canonicalize_window_envelope(
                recovery.envelope, source, committed_window, canonical_path, config
            )
            results.append(
                WindowScoutResult(
                    window=committed_window,
                    cache_hit=False,
                    cache_reason="paid-result-reused-for-local-canonicalization",
                    raw_path=raw_path,
                    canonical_path=canonical_path,
                    session_map=session_map,
                )
            )
            continue
        # This exact check is repeated at the boundary immediately before the
        # provider may open the upload path.  Recovery decisions above never
        # call Gemini; only this genuinely new-request branch reaches it.
        validate_window_proxy_upload(
            proxy_path,
            paths.scout_windows_dir,
            expected_parent_proxy_sha256=parent_sha,
        )

        def validate_upload(path: Path) -> None:
            validate_window_proxy_upload(
                path,
                paths.scout_windows_dir,
                expected_parent_proxy_sha256=parent_sha,
            )

        call_id = recovery.retry_call_id or recovery.call_id
        request = base_request.model_copy(update={"call_id": call_id})
        atomic_write_json(
            request_meta_path,
            {
                "cache_key": cache_key,
                "call_id": call_id,
                "request_fingerprint": request.request_fingerprint,
                "request": payload,
            },
        )
        reserved = False
        in_flight = False
        try:
            record = service.reserve(request)
            paid_reservations_created += 1
            reserved = record.status.value in {"RESERVED", "IN_FLIGHT"}

            def mark_in_flight(current_call_id: str = call_id) -> None:
                nonlocal generation_calls, in_flight
                service.mark_in_flight(current_call_id)
                in_flight = True
                generation_calls += 1

            provider_uploads += 1
            response = provider.execute(
                ProviderRequest(
                    call_id=call_id,
                    provider="gemini",
                    model_id=config.scout.model,
                    billing_mode=config.scout.billing_mode,
                    stage="scout",
                    session_id=preparation.plan.session_id,
                    usage_estimate=request.usage_estimate,
                    request_payload={
                        **payload,
                        "response_max_bytes": config.scout.response_max_bytes,
                    },
                ),
                proxy_path=proxy_path,
                session_proxy_root=paths.scout_windows_dir,
                upload_validator=validate_upload,
                prompt=prompt,
                response_schema=schema,
                media_resolution=config.scout.media_resolution,
                max_output_tokens=config.scout.max_output_tokens,
                thinking_level=thinking.wire_level,
                remote_metadata_path=remote_meta_path,
                before_generation=mark_in_flight,
            )
            envelope = GeminiInteractionEnvelope.model_validate(response.result)
            atomic_write_json(raw_path, envelope.model_dump(mode="json"))
            try:
                service.settle(
                    call_id, response.usage, provider_request_id=response.provider_request_id
                )
            finally:
                _write_window_cost_artifact(service, call_id, cost_path)
            session_map = _canonicalize_window_envelope(
                envelope, source, committed_window, canonical_path, config
            )
            results.append(
                WindowScoutResult(
                    window=committed_window,
                    cache_hit=False,
                    cache_reason="generated Gemini window response",
                    raw_path=raw_path,
                    canonical_path=canonical_path,
                    session_map=session_map,
                )
            )
        except GeminiProviderError as exc:
            if exc.response:
                atomic_write_json(raw_path, dict(exc.response))
            if exc.may_have_dispatched or in_flight:
                with suppress(Exception):
                    service.mark_ambiguous(call_id, str(exc))
            elif reserved:
                with suppress(Exception):
                    service.release(call_id)
            _write_window_cost_artifact(
                service,
                call_id,
                cost_path,
                failure_diagnostic=exc.safe_diagnostic(),
            )
            raise ValidationError(
                str(exc),
                hint=(
                    "No automatic Gemini window retry was attempted."
                    if exc.diagnostic is None
                    else "No automatic Gemini window retry was attempted; "
                    f"phase={exc.diagnostic.phase}, dispatch={exc.diagnostic.dispatch}."
                ),
            ) from exc
        except BaseException:
            if in_flight:
                with suppress(Exception):
                    service.mark_ambiguous(call_id, "local failure after dispatch")
            elif reserved:
                with suppress(Exception):
                    service.release(call_id)
            _write_window_cost_artifact(service, call_id, cost_path)
            raise
    return WindowedScoutRun(
        plan=preparation.plan,
        results=tuple(results),
        activity=ExecutionActivity(
            scout_backend="gemini",
            provider_generation_calls=generation_calls,
            provider_uploads=provider_uploads,
            paid_reservations_created=paid_reservations_created,
            cache_hits=len(cached_ids),
        ),
        aggregate_preflight=preflight,
    )


def _window_request_payload(
    source: SourceAsset, window: ScoutWindow, config: AppConfig, prompt: str, schema_digest: str
) -> dict[str, Any]:
    thinking = effective_gemini_thinking(config)
    media = effective_gemini_media_resolution(config)
    return {
        "source_sha256": source.sha256,
        "window_id": window.window_id,
        "window_start_ms": window.source_start_ms,
        "window_end_ms": window.source_end_ms,
        "window_proxy_sha256": window.proxy_sha256,
        "parent_proxy_sha256": window.parent_proxy_sha256,
        "signal_summary_hash": window.signal_summary_hash,
        "model": config.scout.model,
        "billing_mode": config.scout.billing_mode,
        "media_resolution": config.scout.media_resolution,
        "wire_media_resolution": media.wire_level,
        "effective_media_resolution": media.effective_mode,
        "media_resolution_policy": media.policy,
        "configured_thinking_level": thinking.configured_level,
        "thinking_level": thinking.wire_level,
        "effective_thinking_mode": thinking.effective_mode,
        "thinking_policy": thinking.policy,
        "reserved_thinking_tokens": thinking.reserved_thinking_tokens,
        "prompt_version": config.scout.window_prompt_version,
        "prompt_hash": _sha256_bytes(prompt.encode("utf-8")),
        "schema_version": config.scout.schema_version,
        "schema_hash": schema_digest,
        "output_ceiling": config.scout.response_max_bytes,
    }


def _window_artifacts_valid(item_dir: Path, cache_key: str) -> bool:
    try:
        meta = read_json(item_dir / "request_meta.json")
        if meta.get("cache_key") != cache_key:
            return False
        GeminiInteractionEnvelope.model_validate(read_json(item_dir / "response.raw.json"))
        SessionMap.model_validate(read_json(item_dir / "response.canonical.json"))
        return True
    except Exception:
        return False


def _window_cache_valid(
    item_dir: Path,
    cache_key: str,
    *,
    ledger_record: LedgerRecord | None = None,
) -> bool:
    """Validate local artifacts and the authoritative paid lifecycle together."""

    return (
        ledger_record is not None
        and ledger_record.status is LifecycleStatus.SETTLED
        and _window_artifacts_valid(item_dir, cache_key)
    )


def _reusable_window_envelope(
    raw_path: Path,
    request_meta_path: Path,
    cache_key: str,
    *,
    ledger_record: LedgerRecord | None = None,
    expected_call_id: str | None = None,
    expected_request_fingerprint: str | None = None,
) -> GeminiInteractionEnvelope | None:
    """Return a raw result only when its paid call is authoritatively settled."""

    if ledger_record is None or ledger_record.status is not LifecycleStatus.SETTLED:
        return None
    try:
        metadata = read_json(request_meta_path)
        if metadata.get("cache_key") != cache_key:
            return None
        if expected_call_id is not None and metadata.get("call_id") != expected_call_id:
            return None
        if (
            expected_request_fingerprint is not None
            and metadata.get("request_fingerprint") != expected_request_fingerprint
        ):
            return None
        if ledger_record.call_id != metadata.get("call_id"):
            return None
        if (
            expected_request_fingerprint is not None
            and ledger_record.request_fingerprint != expected_request_fingerprint
        ):
            return None
        envelope = GeminiInteractionEnvelope.model_validate(read_json(raw_path))
        return envelope if envelope.status.lower() == "completed" else None
    except Exception:
        return None


def _load_completed_window_envelope(path: Path) -> GeminiInteractionEnvelope | None:
    if not path.is_file():
        return None
    try:
        envelope = GeminiInteractionEnvelope.model_validate(read_json(path))
        return envelope if envelope.status.lower() == "completed" else None
    except Exception:
        return None


def _lookup_window_ledger(service: CostService, call_id: str) -> LedgerRecord | None:
    try:
        return service.ledger.get(call_id)
    except CostGateError:
        return None


def _retry_window_cleanup(provider: GeminiProvider, remote_meta_path: Path) -> None:
    if remote_meta_path.is_file() and _cleanup_pending(remote_meta_path):
        with suppress(Exception):
            provider.retry_remote_cleanup(remote_meta_path)


def _inspect_paid_window_recovery(
    *,
    item_dir: Path,
    cache_key: str,
    request: CostRequest,
    service: CostService,
    provider: GeminiProvider,
) -> _PaidWindowRecovery:
    """Inspect request identity and ledger before looking at paid raw output.

    A completed provider envelope is evidence, not authorization to reuse a
    paid result.  The persisted request metadata identifies the exact ledger
    call; lifecycle state is checked before any canonicalization decision.
    """

    request_meta_path = item_dir / "request_meta.json"
    raw_path = item_dir / "response.raw.json"
    remote_meta_path = item_dir / "gemini_remote_file.json"
    cost_path = item_dir / "cost.json"
    metadata: Mapping[str, Any] | None = None
    metadata_matches = False
    if request_meta_path.is_file():
        try:
            raw_metadata = read_json(request_meta_path)
            if not isinstance(raw_metadata, Mapping):
                raise ValueError("request metadata must be an object")
            metadata = raw_metadata
            metadata_matches = metadata.get("cache_key") == cache_key
        except Exception as exc:
            if raw_path.is_file() and _load_completed_window_envelope(raw_path) is not None:
                raise ValidationError(
                    "M6 Gemini window has a completed provider result but invalid request "
                    "metadata; refusing paid-result reuse."
                ) from exc
            metadata = None

    # A semantically stale metadata file belongs to an older paid request.  It
    # cannot authorize reuse of the current window, but it also must not make
    # the old raw artifact look like a current cache hit.
    if metadata is not None and not metadata_matches:
        metadata = None

    persisted_call_id: str | None = None
    if metadata is not None:
        value = metadata.get("call_id")
        if not isinstance(value, str) or not value:
            if raw_path.is_file() and _load_completed_window_envelope(raw_path) is not None:
                raise ValidationError(
                    "M6 Gemini window completed output has no persisted ledger call_id; "
                    "refusing paid-result reuse."
                )
            raise ValidationError("M6 Gemini window request metadata is missing call_id.")
        persisted_call_id = value
        if metadata.get("request_fingerprint") != request.request_fingerprint:
            raise ValidationError(
                "M6 Gemini window request fingerprint does not match the current request; "
                "refusing paid-result reuse."
            )
        persisted_payload = metadata.get("request")
        if isinstance(persisted_payload, Mapping) and dict(persisted_payload) != dict(
            request.request_payload
        ):
            raise ValidationError(
                "M6 Gemini window request metadata conflicts with the current request; "
                "refusing paid-result reuse."
            )

    call_id = persisted_call_id or request.call_id
    ledger = _lookup_window_ledger(service, call_id)
    envelope = (
        _load_completed_window_envelope(raw_path)
        if metadata_matches and metadata is not None
        else None
    )

    if ledger is not None:
        if ledger.call_id != call_id or ledger.request_fingerprint != request.request_fingerprint:
            raise ValidationError(
                "M6 Gemini window call identity or request fingerprint conflicts with the "
                "persisted cost ledger; refusing reuse."
            )
        if cost_path.is_file():
            try:
                cost_metadata = read_json(cost_path)
                if isinstance(cost_metadata, Mapping) and cost_metadata.get("call_id") != call_id:
                    raise ValidationError(
                        "M6 Gemini window cost artifact call_id conflicts with the ledger."
                    )
            except ValidationError:
                raise
            except Exception as exc:
                raise ValidationError(
                    "M6 Gemini window cost artifact is invalid; refusing paid-result reuse."
                ) from exc

        if ledger.status is LifecycleStatus.SETTLED:
            if envelope is None:
                raise ValidationError(
                    "A settled M6 Gemini window is missing its completed provider artifact; "
                    "refusing automatic regeneration."
                )
            reusable = _reusable_window_envelope(
                raw_path,
                request_meta_path,
                cache_key,
                ledger_record=ledger,
                expected_call_id=call_id,
                expected_request_fingerprint=request.request_fingerprint,
            )
            if reusable is None:
                raise ValidationError(
                    "A settled M6 Gemini window provider artifact failed identity validation; "
                    "refusing paid-result reuse."
                )
            return _PaidWindowRecovery(
                call_id=call_id,
                ledger=ledger,
                envelope=reusable,
                full_cache=_window_cache_valid(item_dir, cache_key, ledger_record=ledger),
            )

        if ledger.status in {
            LifecycleStatus.AMBIGUOUS,
            LifecycleStatus.IN_FLIGHT,
            LifecycleStatus.RESERVED,
        }:
            _retry_window_cleanup(provider, remote_meta_path)
            raise ValidationError(
                "M6 Gemini window has an unresolved cost lifecycle "
                f"({ledger.status.value}); reconcile the call before reuse or retry."
            )

        if ledger.status is LifecycleStatus.RELEASED:
            if envelope is not None:
                _retry_window_cleanup(provider, remote_meta_path)
                raise ValidationError(
                    "M6 Gemini window has a RELEASED ledger call with a completed provider "
                    "result; refusing inconsistent paid-result reuse."
                )
            return _PaidWindowRecovery(
                call_id=call_id,
                ledger=ledger,
                retry_call_id=f"{call_id}-retry",
            )

    if envelope is not None:
        _retry_window_cleanup(provider, remote_meta_path)
        raise ValidationError(
            "M6 Gemini window has a completed provider result without a corresponding cost "
            "ledger record; refusing paid-result reuse."
        )

    # A raw completed envelope without metadata cannot be associated with a
    # current call.  It is not safe to guess that it was free or to regenerate
    # merely to legitimize it.
    if metadata is None and not metadata_matches and not request_meta_path.is_file():
        orphan = _load_completed_window_envelope(raw_path)
        if orphan is not None:
            raise ValidationError(
                "M6 Gemini window has a completed provider result without request metadata or "
                "a cost ledger record; refusing paid-result reuse."
            )

    return _PaidWindowRecovery(call_id=call_id, ledger=None)


def _canonicalize_window_envelope(
    envelope: GeminiInteractionEnvelope,
    source: SourceAsset,
    window: ScoutWindow,
    canonical_path: Path,
    config: AppConfig,
) -> SessionMap:
    thinking = effective_gemini_thinking(config)
    media = effective_gemini_media_resolution(config)
    session_map = canonicalize_scout_response(
        envelope.output_text.encode("utf-8"),
        session_id=window.session_id,
        source_id=source.source_id,
        source_duration_ms=source.duration_ms,
        created_at=source.created_at,
        max_response_bytes=config.scout.response_max_bytes,
        source_window_id=window.window_id,
    ).model_copy(
        update={
            "scout_backend": "gemini",
            "scout_metadata": {
                "backend": "gemini",
                "model": envelope.model,
                "interaction_id": envelope.interaction_id or "unknown",
                "media_resolution": media.configured_level,
                "wire_media_resolution": media.wire_level or "omitted",
                "effective_media_resolution": media.effective_mode,
                "media_resolution_policy": media.policy,
                "configured_thinking_level": thinking.configured_level,
                "thinking_level": thinking.wire_level or "omitted",
                "effective_thinking_mode": thinking.effective_mode,
                "thinking_policy": thinking.policy,
                "thinking_identity": f"{thinking.policy}:{thinking.effective_mode}",
                "remote_cleanup": envelope.remote_cleanup_status,
                "window_id": window.window_id,
            },
        }
    )
    atomic_write_json(canonical_path, model_json(session_map))
    return session_map


def _cleanup_pending(path: Path) -> bool:
    try:
        metadata = read_json(path)
        return not isinstance(metadata, Mapping) or metadata.get("deletion_status") != "deleted"
    except Exception:
        return False


def _write_window_cost_artifact(
    service: CostService,
    call_id: str,
    path: Path,
    *,
    failure_diagnostic: dict[str, object] | None = None,
) -> None:
    with suppress(Exception):
        record = service.ledger.get(call_id)
        payload = record.model_dump(mode="json")
        if failure_diagnostic is not None:
            payload["failure_diagnostic"] = failure_diagnostic
        atomic_write_json(path, payload)


__all__ = [
    "AggregateCostPreflight",
    "FakeWindowScout",
    "WindowPreparationResult",
    "WindowScoutResult",
    "WindowedScoutRun",
    "aggregate_window_preflight",
    "build_window_prompt",
    "prepare_scout_windows",
    "run_windowed_scout",
    "validate_window_proxy_upload",
]
