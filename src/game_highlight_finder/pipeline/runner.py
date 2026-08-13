"""Vertical local pipeline runner: M1-M5 plus the offline M6 window flow."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from game_highlight_finder.config import AppConfig
from game_highlight_finder.domain.models import ErrorRecord, SessionMap, StageStatus
from game_highlight_finder.domain.reconcile import derive_clip_boundaries, reconcile_session_maps
from game_highlight_finder.errors import ConfigError
from game_highlight_finder.pipeline.extraction import ExtractionResult, extract_candidates
from game_highlight_finder.pipeline.ingest import IngestResult, ingest_source
from game_highlight_finder.pipeline.local_signals import LocalSignalsResult, generate_local_signals
from game_highlight_finder.pipeline.manifest import (
    complete_stage,
    ensure_m6_stages,
    fail_stage,
    recover_interrupted,
    start_stage,
)
from game_highlight_finder.pipeline.proxy import ProxyResult, generate_proxy
from game_highlight_finder.pipeline.scout import ScoutResult, generate_scout
from game_highlight_finder.pipeline.windowed_scout import (
    WindowedScoutRun,
    WindowPreparationResult,
    prepare_scout_windows,
    run_windowed_scout,
)
from game_highlight_finder.storage.atomic import atomic_write_json
from game_highlight_finder.storage.lock import SessionLock
from game_highlight_finder.storage.sessions import (
    artifact_identity,
    completed_stage_cache_is_valid,
    load_manifest,
    session_paths,
    write_manifest,
)

StopAfter = Literal["ingest", "proxy", "local_signals", "scout"]
M6StopAfter = Literal[
    "ingest", "proxy", "local_signals", "windows", "scout", "reconcile", "extract"
]


class AnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ingest: IngestResult
    proxy: ProxyResult | None = None
    local_signals: LocalSignalsResult | None = None
    scout: ScoutResult | None = None
    stop_after: StopAfter


class M6AnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ingest: IngestResult
    proxy: ProxyResult | None = None
    local_signals: LocalSignalsResult | None = None
    windows: WindowPreparationResult | None = None
    scout: WindowedScoutRun | None = None
    session_map: SessionMap | None = None
    extraction: ExtractionResult | None = None
    stop_after: M6StopAfter


def normalize_stop_after(value: str) -> StopAfter:
    normalized = value.strip().lower().replace("-", "_")
    if normalized == "fake_scout":
        normalized = "scout"
    if normalized not in {"ingest", "proxy", "local_signals", "scout"}:
        raise ConfigError(
            "Unknown stop-after stage.", hint="Use ingest, proxy, local-signals, or scout."
        )
    return normalized  # type: ignore[return-value]


def analyze_source(
    video: Path,
    config: AppConfig,
    *,
    stop_after: str = "local-signals",
) -> AnalysisResult:
    boundary = normalize_stop_after(stop_after)
    ingest = ingest_source(video, config)
    if boundary == "ingest":
        return AnalysisResult(ingest=ingest, stop_after=boundary)
    proxy = generate_proxy(ingest.source, config)
    if boundary == "proxy":
        return AnalysisResult(ingest=ingest, proxy=proxy, stop_after=boundary)
    signals = generate_local_signals(ingest.source, proxy, config)
    if boundary == "local_signals":
        return AnalysisResult(
            ingest=ingest,
            proxy=proxy,
            local_signals=signals,
            stop_after=boundary,
        )
    scout = generate_scout(ingest.source, proxy, signals, config)
    return AnalysisResult(
        ingest=ingest,
        proxy=proxy,
        local_signals=signals,
        scout=scout,
        stop_after=boundary,
    )


def normalize_m6_stop_after(value: str) -> M6StopAfter:
    normalized = value.strip().lower().replace("-", "_")
    if normalized not in {
        "ingest",
        "proxy",
        "local_signals",
        "windows",
        "scout",
        "reconcile",
        "extract",
    }:
        raise ConfigError(
            "Unknown M6 stop-after stage. Use ingest, proxy, local-signals, windows, "
            "scout, reconcile, or extract."
        )
    return normalized  # type: ignore[return-value]


def analyze_m6_source(
    video: Path,
    config: AppConfig,
    *,
    stop_after: str = "extract",
) -> M6AnalysisResult:
    """Run the local-only M6 window/reconcile/extract pipeline.

    Gemini is intentionally rejected at this boundary; this milestone's live
    windowed acceptance is not run and cannot accidentally dispatch a network
    request through the M5 provider path.
    """

    if config.scout.backend != "fake":
        raise ConfigError(
            "M6 requires the offline fake Scout backend; live Gemini acceptance is not enabled."
        )
    boundary = normalize_m6_stop_after(stop_after)
    local = analyze_source(video, config, stop_after="local-signals")
    if boundary == "ingest":
        return M6AnalysisResult(ingest=local.ingest, stop_after=boundary)
    if boundary == "proxy":
        return M6AnalysisResult(ingest=local.ingest, proxy=local.proxy, stop_after=boundary)
    if boundary == "local_signals":
        return M6AnalysisResult(
            ingest=local.ingest,
            proxy=local.proxy,
            local_signals=local.local_signals,
            stop_after=boundary,
        )
    assert local.proxy is not None and local.local_signals is not None
    windows = prepare_scout_windows(local.ingest.source, local.proxy, local.local_signals, config)
    if boundary == "windows":
        return M6AnalysisResult(
            ingest=local.ingest,
            proxy=local.proxy,
            local_signals=local.local_signals,
            windows=windows,
            stop_after=boundary,
        )
    scout = run_windowed_scout(local.ingest.source, windows, local.local_signals, config)
    scout_outputs = [
        path
        for result in scout.results
        for path in (
            result.raw_path,
            result.canonical_path,
            result.raw_path.parent / "request_meta.json",
        )
    ]
    _record_completed_stage(
        config,
        local.ingest.session_id,
        "scout",
        {
            "version": "m6-window-scout-v1",
            "plan_hash": windows.plan.plan_hash,
            "window_cache_keys": [result.window.provider_cache_key for result in scout.results],
        },
        inputs=[local.proxy.proxy_path, local.local_signals.signals_path],
        outputs=scout_outputs,
        item_states={result.window.window_id: "COMPLETED" for result in scout.results},
    )
    if boundary == "scout":
        return M6AnalysisResult(
            ingest=local.ingest,
            proxy=local.proxy,
            local_signals=local.local_signals,
            windows=windows,
            scout=scout,
            stop_after=boundary,
        )
    session_map = reconcile_session_maps(
        local.ingest.session_id,
        local.ingest.source.source_id,
        local.ingest.source.duration_ms,
        [(result.window, result.session_map) for result in scout.results],
        created_at=local.ingest.source.created_at,
    )
    session_map = derive_clip_boundaries(
        session_map, local.ingest.source.duration_ms, config.media.extraction
    )
    paths = session_paths(config.storage.data_dir, local.ingest.session_id)
    paths.reconcile_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(paths.session_map, session_map.model_dump(mode="json"))
    atomic_write_json(
        paths.reconcile_dir / "diagnostics.json",
        {
            "window_count": len(scout.results),
            "warnings": session_map.warnings,
            "created_at": session_map.created_at.isoformat(),
        },
    )
    _record_completed_stage(
        config,
        local.ingest.session_id,
        "reconcile",
        {
            "version": "m6-reconcile-v1",
            "window_canonical_hashes": [
                _file_hash(result.canonical_path) for result in scout.results
            ],
        },
        inputs=[result.canonical_path for result in scout.results],
        outputs=[paths.session_map, paths.reconcile_dir / "diagnostics.json"],
        item_states={"session_map": "COMPLETED", "diagnostics": "COMPLETED"},
    )
    if boundary == "reconcile":
        return M6AnalysisResult(
            ingest=local.ingest,
            proxy=local.proxy,
            local_signals=local.local_signals,
            windows=windows,
            scout=scout,
            session_map=session_map,
            stop_after=boundary,
        )
    extraction = extract_candidates(local.ingest.source, session_map, config)
    extraction_outputs = [paths.extraction_manifest]
    extraction_outputs.extend(
        paths.root / record.output_path
        for record in extraction.manifest.records
        if record.status == "COMPLETED"
    )
    extraction_outputs.extend(
        paths.root / record.thumbnail_path
        for record in extraction.manifest.records
        if record.status == "COMPLETED" and record.thumbnail_path is not None
    )
    if extraction.incomplete == 0:
        _record_completed_stage(
            config,
            local.ingest.session_id,
            "extract",
            {
                "version": "m6-extract-v1",
                "session_map_sha256": _file_hash(paths.session_map),
                "extraction": config.media.extraction.model_dump(mode="json"),
                "source_sha256": local.ingest.source.sha256,
            },
            inputs=[paths.session_map],
            outputs=extraction_outputs,
            item_states={
                record.candidate_id: record.status for record in extraction.manifest.records
            },
        )
    else:
        _record_incomplete_stage(
            config,
            local.ingest.session_id,
            "extract",
            {
                "version": "m6-extract-v1",
                "session_map_sha256": _file_hash(paths.session_map),
                "extraction": config.media.extraction.model_dump(mode="json"),
                "source_sha256": local.ingest.source.sha256,
            },
            item_states={
                record.candidate_id: record.status for record in extraction.manifest.records
            },
        )
    return M6AnalysisResult(
        ingest=local.ingest,
        proxy=local.proxy,
        local_signals=local.local_signals,
        windows=windows,
        scout=scout,
        session_map=session_map,
        extraction=extraction,
        stop_after="extract",
    )


def _file_hash(path: Path) -> str:
    from game_highlight_finder.storage.hashing import hash_file

    return hash_file(path)


def _record_completed_stage(
    config: AppConfig,
    session_id: str,
    stage_name: str,
    cache_payload: object,
    *,
    inputs: list[Path],
    outputs: list[Path],
    item_states: dict[str, str],
) -> None:
    """Commit an M6 aggregate stage only after all item artifacts are durable."""

    paths = session_paths(config.storage.data_dir, session_id)
    encoded = json.dumps(cache_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    cache_key = hashlib.sha256(encoded).hexdigest()
    with SessionLock(paths.lock):
        manifest = load_manifest(paths.manifest)
        ensure_m6_stages(manifest)
        recover_interrupted(manifest)
        valid, _ = completed_stage_cache_is_valid(
            paths, manifest, stage_name=stage_name, expected_cache_key=cache_key
        )
        if valid:
            return
        stage = manifest.stages[stage_name]
        if stage.status in {StageStatus.COMPLETED, StageStatus.RUNNING}:
            stage.status = StageStatus.STALE
            stage.reason = "M6 aggregate inputs or outputs changed"
        start_stage(manifest, stage_name, cache_key)
        write_manifest(paths.manifest, manifest)
        input_identities = [artifact_identity(path, relative_to=paths.root) for path in inputs]
        output_identities = [artifact_identity(path, relative_to=paths.root) for path in outputs]
        complete_stage(
            manifest,
            stage_name,
            inputs=input_identities,
            outputs=output_identities,
            item_states=item_states,
        )
        write_manifest(paths.manifest, manifest)


def _record_incomplete_stage(
    config: AppConfig,
    session_id: str,
    stage_name: str,
    cache_payload: object,
    *,
    item_states: dict[str, str],
) -> None:
    paths = session_paths(config.storage.data_dir, session_id)
    encoded = json.dumps(cache_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    cache_key = hashlib.sha256(encoded).hexdigest()
    with SessionLock(paths.lock):
        manifest = load_manifest(paths.manifest)
        ensure_m6_stages(manifest)
        recover_interrupted(manifest)
        stage = manifest.stages[stage_name]
        if stage.status in {StageStatus.COMPLETED, StageStatus.RUNNING}:
            stage.status = StageStatus.STALE
            stage.reason = "M6 aggregate is incomplete"
        start_stage(manifest, stage_name, cache_key)
        fail_stage(
            manifest,
            stage_name,
            ErrorRecord(
                category="storage",
                message="One or more M6 candidate extractions are incomplete.",
                retryable=True,
            ),
        )
        manifest.stages[stage_name].item_states = item_states
        write_manifest(paths.manifest, manifest)
