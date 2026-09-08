"""Vertical local pipeline runner: accepted M1-M6 plus local M7 presentation."""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from game_highlight_finder.config import AppConfig
from game_highlight_finder.domain.models import ErrorRecord, SessionMap, StageStatus
from game_highlight_finder.domain.proposals import (
    ManualProposalMarkerSet,
    ProposalArtifact,
    ProposalRoutingPlan,
    ProposalSummary,
    TranscriptFixture,
)
from game_highlight_finder.domain.reconcile import derive_clip_boundaries, reconcile_session_maps
from game_highlight_finder.errors import ConfigError
from game_highlight_finder.pipeline.creator_evaluation import (
    create_creator_review_template,
    persist_creator_review_template,
)
from game_highlight_finder.pipeline.extraction import ExtractionResult, extract_candidates
from game_highlight_finder.pipeline.hybrid_triage import (
    HybridFixtureBundle,
    HybridTriagePersistence,
    HybridTriageRun,
    persist_hybrid_triage,
    run_hybrid_fixture_bundle,
)
from game_highlight_finder.pipeline.ingest import IngestResult, ingest_source
from game_highlight_finder.pipeline.local_signals import LocalSignalsResult, generate_local_signals
from game_highlight_finder.pipeline.manifest import (
    complete_stage,
    ensure_m6_stages,
    ensure_m7_stages,
    fail_stage,
    invalidate_from,
    recover_interrupted,
    start_stage,
)
from game_highlight_finder.pipeline.proposals import (
    combine_and_cluster_proposals,
    proposals_from_local_signals,
    proposals_from_manual_markers,
    proposals_from_transcript,
    route_proposals,
    selected_proposal_artifact,
    summarize_proposals,
)
from game_highlight_finder.pipeline.proxy import ProxyResult, generate_proxy
from game_highlight_finder.pipeline.ranking import RankingArtifact, load_or_create_ranking
from game_highlight_finder.pipeline.report import ReportResult, render_report
from game_highlight_finder.pipeline.scout import ScoutResult, generate_scout
from game_highlight_finder.pipeline.windowed_scout import (
    WindowedScoutRun,
    WindowPreparationResult,
    prepare_scout_windows,
    run_windowed_scout,
)
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
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
V1StopAfter = Literal[
    "ingest", "proxy", "local_signals", "windows", "scout", "reconcile", "extract", "rank", "report"
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


class V1AnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)
    m6: M6AnalysisResult
    ranking: RankingArtifact | None = None
    report: ReportResult | None = None
    stop_after: V1StopAfter


class HybridProposalPreparationResult(BaseModel):
    """Provider-free local preparation through the factual proposal artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    ingest: IngestResult
    proxy: ProxyResult
    local_signals: LocalSignalsResult
    proposals: ProposalArtifact
    proposal_summary: ProposalSummary
    proposals_path: Path
    proposal_summary_path: Path


class HybridProposalRoutingResult(BaseModel):
    """Persisted provider-free routing plan plus its semantic-inspection subset."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    proposals: ProposalArtifact
    routing: ProposalRoutingPlan
    routed_proposals: ProposalArtifact
    routing_plan_path: Path
    routed_proposals_path: Path


class HybridFixtureAnalysisResult(BaseModel):
    """Local fixture-driven hybrid run with isolated review extraction artifacts."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    preparation: HybridProposalPreparationResult
    triage: HybridTriageRun
    persistence: HybridTriagePersistence
    extraction: ExtractionResult
    creator_review_template_path: Path


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


def prepare_hybrid_proposals(
    video: Path,
    config: AppConfig,
    *,
    manual_markers: ManualProposalMarkerSet | None = None,
    transcript: TranscriptFixture | None = None,
) -> HybridProposalPreparationResult:
    """Run local ingest/proxy/signals, enrich factual anchors, and persist proposals."""

    local = analyze_source(video, config, stop_after="local-signals")
    assert local.proxy is not None and local.local_signals is not None
    local_proposals = proposals_from_local_signals(
        session_id=local.ingest.session_id,
        source_id=local.ingest.source.source_id,
        signals=local.local_signals.signals,
        created_at=local.ingest.source.created_at,
    )
    artifacts = [local_proposals]
    if manual_markers is not None:
        artifacts.append(
            proposals_from_manual_markers(
                session_id=local.ingest.session_id,
                source_id=local.ingest.source.source_id,
                source_sha256=local.ingest.source.sha256,
                source_duration_ms=local.ingest.source.duration_ms,
                marker_set=manual_markers,
                created_at=local.ingest.source.created_at,
            )
        )
    if transcript is not None:
        artifacts.append(
            proposals_from_transcript(
                session_id=local.ingest.session_id,
                source_id=local.ingest.source.source_id,
                source_sha256=local.ingest.source.sha256,
                source_duration_ms=local.ingest.source.duration_ms,
                transcript=transcript,
                created_at=local.ingest.source.created_at,
            )
        )
    proposals = combine_and_cluster_proposals(artifacts)
    proposal_summary = summarize_proposals(proposals)
    paths = session_paths(config.storage.data_dir, local.ingest.session_id)
    paths.hybrid_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(paths.hybrid_proposals_path, proposals.model_dump(mode="json"))
    atomic_write_json(
        paths.hybrid_proposal_summary_path,
        proposal_summary.model_dump(mode="json"),
    )
    return HybridProposalPreparationResult(
        ingest=local.ingest,
        proxy=local.proxy,
        local_signals=local.local_signals,
        proposals=proposals,
        proposal_summary=proposal_summary,
        proposals_path=paths.hybrid_proposals_path,
        proposal_summary_path=paths.hybrid_proposal_summary_path,
    )


def prepare_hybrid_routing(
    config: AppConfig,
    session_id: str,
    *,
    weak_sample_interval_ms: int,
) -> HybridProposalRoutingResult:
    """Persist a deterministic cost/coverage routing plan over full factual proposals."""

    paths = session_paths(config.storage.data_dir, session_id)
    if not paths.hybrid_proposals_path.is_file():
        raise ConfigError(
            "Hybrid proposal artifact is missing.",
            hint=str(paths.hybrid_proposals_path),
        )
    proposals = ProposalArtifact.model_validate(read_json(paths.hybrid_proposals_path))
    routing = route_proposals(
        proposals,
        weak_sample_interval_ms=weak_sample_interval_ms,
    )
    routed = selected_proposal_artifact(proposals, routing)
    paths.hybrid_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(paths.hybrid_routing_plan_path, routing.model_dump(mode="json"))
    atomic_write_json(paths.hybrid_routed_proposals_path, routed.model_dump(mode="json"))
    return HybridProposalRoutingResult(
        proposals=proposals,
        routing=routing,
        routed_proposals=routed,
        routing_plan_path=paths.hybrid_routing_plan_path,
        routed_proposals_path=paths.hybrid_routed_proposals_path,
    )


def analyze_hybrid_fixture_source(
    video: Path,
    config: AppConfig,
    fixture: HybridFixtureBundle,
    *,
    manual_markers: ManualProposalMarkerSet | None = None,
    transcript: TranscriptFixture | None = None,
) -> HybridFixtureAnalysisResult:
    """Exercise the hybrid semantic center locally without any provider generation."""

    preparation = prepare_hybrid_proposals(
        video,
        config,
        manual_markers=manual_markers,
        transcript=transcript,
    )
    triage = run_hybrid_fixture_bundle(
        proposals=preparation.proposals,
        fixture=fixture,
        extraction_config=config.media.extraction,
        created_at=preparation.ingest.source.created_at,
    )
    paths = session_paths(config.storage.data_dir, preparation.ingest.session_id)
    persistence = persist_hybrid_triage(paths, triage)
    extraction = extract_candidates(
        preparation.ingest.source,
        triage.review_map,
        config,
        namespace="hybrid",
    )
    review_template = create_creator_review_template(triage.review_map)
    persist_creator_review_template(paths, review_template)
    return HybridFixtureAnalysisResult(
        preparation=preparation,
        triage=triage,
        persistence=persistence,
        extraction=extraction,
        creator_review_template_path=paths.creator_review_template_path,
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
    force_stage: str | None = None,
) -> M6AnalysisResult:
    """Run M6 windows, reconciliation, and extraction.

    Gemini remains opt-in at the configuration boundary; each enabled window
    then owns an independent paid lifecycle in ``run_windowed_scout``.
    """
    boundary = normalize_m6_stop_after(stop_after)
    normalized_force = (
        force_stage.strip().lower().replace("-", "_") if force_stage is not None else None
    )
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
    windows = prepare_scout_windows(
        local.ingest.source,
        local.proxy,
        local.local_signals,
        config,
        force=normalized_force == "windows",
    )
    if boundary == "windows":
        return M6AnalysisResult(
            ingest=local.ingest,
            proxy=local.proxy,
            local_signals=local.local_signals,
            windows=windows,
            stop_after=boundary,
        )
    scout = run_windowed_scout(
        local.ingest.source,
        windows,
        local.local_signals,
        config,
        force=normalized_force == "scout" and config.scout.backend == "fake",
    )
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


def normalize_v1_stop_after(value: str) -> V1StopAfter:
    normalized = value.strip().lower().replace("-", "_")
    if normalized not in {
        "ingest",
        "proxy",
        "local_signals",
        "windows",
        "scout",
        "reconcile",
        "extract",
        "rank",
        "report",
    }:
        raise ConfigError(
            "Unknown V1 stop-after stage.",
            hint="Use ingest, proxy, local-signals, windows, scout, reconcile, extract, rank, or report.",
        )
    return normalized  # type: ignore[return-value]


def analyze_v1_source(
    video: Path,
    config: AppConfig,
    *,
    stop_after: str = "report",
    force_stage: str | None = None,
) -> V1AnalysisResult:
    """Run the standard offline-first V1 journey through ranking and report."""

    boundary = normalize_v1_stop_after(stop_after)
    normalized_force = (
        force_stage.strip().lower().replace("-", "_") if force_stage is not None else None
    )
    if force_stage is not None:
        paths = session_paths(config.storage.data_dir, _session_id_for_video(video, config))
        if paths.manifest.is_file():
            with SessionLock(paths.lock):
                manifest = load_manifest(paths.manifest)
                ensure_m7_stages(manifest)
                invalidate_from(manifest, force_stage)
                write_manifest(paths.manifest, manifest)
    local_stages = {"ingest", "proxy", "local_signals", "windows", "scout", "reconcile", "extract"}
    m6_boundary = boundary if boundary in local_stages else "extract"
    m6 = analyze_m6_source(
        video,
        config,
        stop_after=m6_boundary,
        force_stage=normalized_force,
    )
    if boundary in local_stages:
        return V1AnalysisResult(m6=m6, stop_after=boundary)
    if m6.session_map is None:
        raise ConfigError(
            "Ranking requires a completed reconciled session map.",
            hint="Run: highlight resume " + m6.ingest.session_id,
        )
    paths = session_paths(config.storage.data_dir, m6.ingest.session_id)
    ranking, _ranking_hit = load_or_create_ranking(
        paths, m6.session_map, config, force=normalized_force == "rank"
    )
    _record_presentation_stage(
        config,
        m6.ingest.session_id,
        "rank",
        {"version": ranking.ranking_version, "ranking_cache_key": ranking.cache_key},
        inputs=[paths.session_map],
        outputs=[paths.ranking_path],
    )
    if boundary == "rank":
        return V1AnalysisResult(m6=m6, ranking=ranking, stop_after=boundary)
    force_report = normalized_force in {
        "report",
        "rank",
        "extract",
        "reconcile",
        "scout",
        "windows",
        "local_signals",
        "proxy",
        "ingest",
    }
    report = render_report(
        paths,
        m6.ingest.source,
        m6.session_map,
        ranking,
        load_manifest(paths.manifest),
        config,
        force=force_report,
    )
    _record_presentation_stage(
        config,
        m6.ingest.session_id,
        "report",
        {"version": "m7-report-v1", "report_cache_key": report.cache_key},
        inputs=[paths.session_map, paths.ranking_path, paths.extraction_manifest],
        outputs=[paths.report_path, paths.report_meta_path],
    )
    return V1AnalysisResult(m6=m6, ranking=ranking, report=report, stop_after="report")


def _session_id_for_video(video: Path, config: AppConfig) -> str:
    from game_highlight_finder.pipeline.ingest import ingest_source

    return ingest_source(video, config).session_id


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


def _record_presentation_stage(
    config: AppConfig,
    session_id: str,
    stage_name: str,
    cache_payload: object,
    *,
    inputs: list[Path],
    outputs: list[Path],
) -> None:
    """Record rank/report only after their complete atomic artifacts exist."""

    paths = session_paths(config.storage.data_dir, session_id)
    encoded = json.dumps(cache_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    cache_key = hashlib.sha256(encoded).hexdigest()
    with SessionLock(paths.lock):
        manifest = load_manifest(paths.manifest)
        ensure_m7_stages(manifest)
        recover_interrupted(manifest)
        valid, _ = completed_stage_cache_is_valid(
            paths, manifest, stage_name=stage_name, expected_cache_key=cache_key
        )
        if valid:
            return
        stage = manifest.stages[stage_name]
        if stage.status in {StageStatus.COMPLETED, StageStatus.RUNNING}:
            stage.status = StageStatus.STALE
            stage.reason = "M7 presentation inputs or outputs changed"
        start_stage(manifest, stage_name, cache_key)
        write_manifest(paths.manifest, manifest)
        input_identities = [
            artifact_identity(path, relative_to=paths.root) for path in inputs if path.is_file()
        ]
        output_identities = [artifact_identity(path, relative_to=paths.root) for path in outputs]
        complete_stage(
            manifest,
            stage_name,
            inputs=input_identities,
            outputs=output_identities,
            item_states={stage_name: "COMPLETED"},
        )
        write_manifest(paths.manifest, manifest)
