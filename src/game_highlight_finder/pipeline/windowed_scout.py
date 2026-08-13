"""Offline M6 window preparation, fake Scout execution, and cost preflight."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from game_highlight_finder.config import AppConfig
from game_highlight_finder.cost.fx import FxSnapshot
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
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.media.ffmpeg import build_window_proxy_command, run_ffmpeg
from game_highlight_finder.media.ffprobe import run_ffprobe
from game_highlight_finder.media.tools import tool_identity
from game_highlight_finder.pipeline.gemini_contract import gemini_window_scout_schema
from game_highlight_finder.pipeline.gemini_scout import (
    build_gemini_registry,
    estimate_gemini_usage,
)
from game_highlight_finder.pipeline.local_signals import LocalSignalsResult
from game_highlight_finder.pipeline.proxy import ProxyResult
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


class WindowedScoutRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    plan: WindowPlan
    results: tuple[WindowScoutResult, ...]
    missing_windows: tuple[str, ...] = ()
    aggregate_preflight: AggregateCostPreflight


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


def validate_window_proxy_upload(path: Path, session_windows_root: Path) -> ScoutWindow:
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
    return window


def prepare_scout_windows(
    source: SourceAsset,
    proxy: ProxyResult,
    local_signals: LocalSignalsResult,
    config: AppConfig,
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
            existing is not None
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
    prompt_version: str = "gemini-scout-window-v1",
) -> str:
    summary = json.dumps(
        dict(local_signal_summary), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return "\n".join(
        [
            f"Game Highlight Finder window Scout {prompt_version}.",
            "Return only JSON matching the supplied schema; do not emit hidden reasoning.",
            "Timestamps inside this request are window-relative integer milliseconds.",
            "The full source timeline is authoritative; overlapping windows are "
            "reconciled locally.",
            "Do not duplicate a boundary event unless evidence supports it; zero "
            "candidates is valid.",
            f"Full source duration_ms: {source_duration_ms}",
            f"Window absolute bounds_ms: {window.source_start_ms}-{window.source_end_ms}",
            f"Bounded local signals: {summary}",
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
            reserved_thinking_tokens=config.scout.reserved_thinking_tokens,
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
) -> WindowedScoutRun:
    """Generate or reuse one strictly window-relative fake response per window."""

    if config.scout.backend != "fake":
        raise ValidationError("M6 live Gemini windowed acceptance is not enabled")
    provider = fake_provider or FakeWindowScout()
    paths = session_paths(config.storage.data_dir, preparation.plan.session_id)
    results: list[WindowScoutResult] = []
    cached_ids: set[str] = set()
    missing: list[str] = []
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
        if raw_path.is_file() and canonical_path.is_file() and request_meta_path.is_file():
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
        if raw_path.is_file() and not valid_cache:
            try:
                raw_bytes = raw_path.read_bytes()
                parse_scout_response(raw_bytes, max_bytes=config.scout.response_max_bytes)
            except Exception:
                raw_bytes = provider.generate(
                    window=window,
                    source_duration_ms=source.duration_ms,
                    source_sha256=source.sha256,
                    summary=summary,
                )
        else:
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
        missing_windows=tuple(missing),
        aggregate_preflight=preflight,
    )


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
