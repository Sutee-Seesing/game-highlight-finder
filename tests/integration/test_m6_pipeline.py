from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from game_highlight_finder.config import (
    AppConfig,
    CostConfig,
    ScoutConfig,
    StorageConfig,
    ToolsConfig,
)
from game_highlight_finder.cost.fx import FxSnapshot
from game_highlight_finder.cost.production import production_pricing_catalog
from game_highlight_finder.cost.service import CostService
from game_highlight_finder.domain.proposals import (
    ManualProposalMarker,
    ManualProposalMarkerSet,
    ProposalSignalType,
    TranscriptFixture,
    TranscriptUtterance,
)
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.media.ffmpeg import FFmpegCancelled
from game_highlight_finder.pipeline.extraction import extract_candidates
from game_highlight_finder.pipeline.gemini_scout import build_gemini_registry
from game_highlight_finder.pipeline.hybrid_context_media import validate_hybrid_context_proxy
from game_highlight_finder.pipeline.runner import (
    analyze_m6_source,
    prepare_hybrid_contexts,
    prepare_hybrid_proposals,
    prepare_hybrid_routing,
)
from game_highlight_finder.pipeline.windowed_scout import (
    FakeWindowScout,
    aggregate_window_preflight,
    run_windowed_scout,
    validate_window_proxy_upload,
)
from game_highlight_finder.providers.gemini import FakeGeminiTransport, GeminiProviderError
from game_highlight_finder.storage.atomic import read_json
from game_highlight_finder.storage.sessions import session_paths


def _config(data_dir: Path, ffmpeg: Path, ffprobe: Path) -> AppConfig:
    return AppConfig(
        storage=StorageConfig(data_dir=data_dir),
        tools=ToolsConfig(ffmpeg_path=ffmpeg, ffprobe_path=ffprobe),
    )


def _window_response(
    source_duration_ms: int, window_start_ms: int, window_end_ms: int
) -> dict[str, object]:
    import json

    return {
        "status": "completed",
        "id": "window-interaction",
        "output_text": json.dumps(
            {
                "schema_version": 1,
                "source_duration_ms": source_duration_ms,
                "time_basis": "window_relative",
                "window_start_ms": window_start_ms,
                "window_end_ms": window_end_ms,
                "matches": [],
                "candidates": [],
                "warnings": [],
                "metadata": {"backend": "gemini"},
            }
        ),
        "usage": {
            "prompt_token_count": 1_000,
            "candidates_token_count": 20,
            "thoughts_token_count": 5,
        },
    }


def _gemini_cost_service(config: AppConfig) -> CostService:
    return CostService(
        config,
        registry=build_gemini_registry(),
        pricing=production_pricing_catalog(),
        fx_snapshot=FxSnapshot(
            base_currency="USD",
            quote_currency="THB",
            rate=Decimal("36"),
            captured_at=datetime.now(UTC),
            source="offline-test",
        ),
    )


def _settled_window_fixture(
    tmp_path: Path,
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    *,
    delete_error: Exception | None = None,
) -> tuple[AppConfig, object, object, FakeGeminiTransport, CostService, Path]:
    base = _config(tmp_path / "library", ffmpeg_path, ffprobe_path)
    local = analyze_m6_source(tiny_video, base, stop_after="windows")
    assert local.windows is not None and local.local_signals is not None
    config = base.model_copy(
        update={
            "scout": ScoutConfig(backend="gemini", allow_remote_upload=True),
            "cost": CostConfig(monthly_budget_thb=Decimal("100")),
        }
    )
    window = local.windows.windows[0]
    transport = FakeGeminiTransport(
        response=_window_response(
            local.ingest.source.duration_ms, window.source_start_ms, window.source_end_ms
        ),
        delete_error=delete_error,
    )
    service = _gemini_cost_service(config)
    first = run_windowed_scout(
        local.ingest.source,
        local.windows,
        local.local_signals,
        config,
        gemini_transport=transport,
        cost_service=service,
    )
    assert first.results[0].cache_hit is False
    assert service.ledger.list_calls()[0].status.value == "SETTLED"
    item_dir = local.windows.session_dir / "scout" / "windows" / window.window_id
    return config, local, window, transport, service, item_dir


def _set_ledger_status(service: CostService, status: str) -> None:
    call_id = service.ledger.list_calls()[0].call_id
    with sqlite3.connect(service.ledger.path) as connection:
        connection.execute("UPDATE calls SET status=? WHERE call_id=?", (status, call_id))


def test_m6_offline_window_reconcile_extract_resume(
    tmp_path: Path, tiny_video: Path, ffmpeg_path: Path, ffprobe_path: Path
) -> None:
    config = _config(tmp_path / "library", ffmpeg_path, ffprobe_path)
    original = tiny_video.read_bytes()
    first = analyze_m6_source(tiny_video, config)
    assert first.stop_after == "extract"
    assert first.windows is not None
    assert first.scout is not None
    assert first.session_map is not None
    assert first.extraction is not None
    assert first.scout.aggregate_preflight.estimated_micro_thb == 0
    assert first.extraction.incomplete == 0
    assert first.extraction.completed == len(first.session_map.candidates)
    assert tiny_video.read_bytes() == original

    second = analyze_m6_source(tiny_video, config)
    assert second.windows is not None
    assert second.windows.cache_hits == len(second.windows.windows)
    assert second.scout is not None
    assert all(item.cache_hit for item in second.scout.results)
    assert second.extraction is not None
    assert second.extraction.cache_hits == second.extraction.completed
    assert tiny_video.read_bytes() == original


def test_hybrid_extraction_namespace_does_not_overwrite_legacy_artifacts(
    tmp_path: Path, tiny_video: Path, ffmpeg_path: Path, ffprobe_path: Path
) -> None:
    config = _config(tmp_path / "library", ffmpeg_path, ffprobe_path)
    baseline = analyze_m6_source(tiny_video, config)
    assert baseline.session_map is not None and baseline.extraction is not None

    hybrid = extract_candidates(
        baseline.ingest.source,
        baseline.session_map,
        config,
        namespace="hybrid",
    )
    paths = session_paths(config.storage.data_dir, baseline.ingest.session_id)

    assert baseline.extraction.manifest_path == paths.extraction_manifest
    assert hybrid.manifest_path == paths.hybrid_extraction_manifest
    assert paths.extraction_manifest.is_file()
    assert paths.hybrid_extraction_manifest.is_file()
    assert baseline.extraction.manifest_path != hybrid.manifest_path
    for record in hybrid.manifest.records:
        assert record.output_path.startswith("hybrid/candidates/")
        assert (paths.root / record.output_path).is_file()


def test_hybrid_manual_marker_enrichment_stays_local_and_persists_clustered_proposals(
    tmp_path: Path,
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
) -> None:
    config = _config(tmp_path / "library", ffmpeg_path, ffprobe_path)
    ingested = analyze_m6_source(tiny_video, config, stop_after="ingest")
    source = ingested.ingest.source
    marker_set = ManualProposalMarkerSet(
        source_sha256=source.sha256,
        source_duration_ms=source.duration_ms,
        markers=[
            ManualProposalMarker(
                start_ms=500,
                end_ms=900,
                label="fixture factual marker",
                event_hypothesis="OBJECTIVE_PLANTED",
            )
        ],
    )

    prepared = prepare_hybrid_proposals(
        tiny_video,
        config,
        manual_markers=marker_set,
    )
    paths = session_paths(config.storage.data_dir, prepared.ingest.session_id)

    assert prepared.proposals_path == paths.hybrid_proposals_path
    assert paths.hybrid_proposals_path.is_file()
    marker_proposals = [
        proposal for proposal in prepared.proposals.proposals if "manual_marker" in proposal.sources
    ]
    assert len(marker_proposals) == 1
    assert marker_proposals[0].event_hypothesis == "OBJECTIVE_PLANTED"
    assert marker_proposals[0].signal_type is ProposalSignalType.MANUAL_MARKER
    assert prepared.proposal_summary_path == paths.hybrid_proposal_summary_path
    assert paths.hybrid_proposal_summary_path.is_file()
    assert prepared.proposal_summary.proposal_neighborhood_count == len(
        prepared.proposals.proposals
    )
    assert config.scout.allow_remote_upload is False


def test_hybrid_transcript_enrichment_stays_local_and_persists_density_summary(
    tmp_path: Path,
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
) -> None:
    config = _config(tmp_path / "library", ffmpeg_path, ffprobe_path)
    ingested = analyze_m6_source(tiny_video, config, stop_after="ingest")
    source = ingested.ingest.source
    transcript = TranscriptFixture(
        source_sha256=source.sha256,
        source_duration_ms=source.duration_ms,
        utterances=[
            TranscriptUtterance(
                start_ms=600,
                end_ms=1_000,
                text="wait, what just happened?",
                speaker="owner",
                language="en",
                confidence=0.9,
            )
        ],
    )

    prepared = prepare_hybrid_proposals(
        tiny_video,
        config,
        transcript=transcript,
    )
    paths = session_paths(config.storage.data_dir, prepared.ingest.session_id)

    transcript_proposals = [
        proposal
        for proposal in prepared.proposals.proposals
        if "transcript_fixture" in proposal.sources
    ]
    assert len(transcript_proposals) == 1
    assert transcript_proposals[0].signal_type is ProposalSignalType.ASR_UTTERANCE
    assert transcript_proposals[0].metadata["text"] == "wait, what just happened?"
    assert prepared.proposal_summary_path == paths.hybrid_proposal_summary_path
    assert paths.hybrid_proposal_summary_path.is_file()
    assert prepared.proposal_summary.proposal_neighborhood_count == len(
        prepared.proposals.proposals
    )
    assert prepared.proposal_summary.proposals_per_source_hour > 0

    routing = prepare_hybrid_routing(
        config,
        prepared.ingest.session_id,
        weak_sample_interval_ms=1_000,
    )
    assert routing.routing_plan_path == paths.hybrid_routing_plan_path
    assert routing.routed_proposals_path == paths.hybrid_routed_proposals_path
    assert paths.hybrid_routing_plan_path.is_file()
    assert paths.hybrid_routed_proposals_path.is_file()
    assert len(routing.routing.decisions) == len(prepared.proposals.proposals)
    assert (
        len(routing.routing.selected_proposal_ids)
        + len(routing.routing.deferred_proposal_ids)
        == len(prepared.proposals.proposals)
    )
    assert len(routing.routed_proposals.proposals) == len(
        routing.routing.selected_proposal_ids
    )
    assert config.scout.allow_remote_upload is False


def test_hybrid_routed_context_media_is_local_committed_cacheable_and_upload_scoped(
    tmp_path: Path,
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
) -> None:
    config = _config(tmp_path / "library", ffmpeg_path, ffprobe_path)
    ingested = analyze_m6_source(tiny_video, config, stop_after="ingest")
    source = ingested.ingest.source
    transcript = TranscriptFixture(
        source_sha256=source.sha256,
        source_duration_ms=source.duration_ms,
        utterances=[
            TranscriptUtterance(
                start_ms=500,
                end_ms=min(1_000, source.duration_ms),
                text="that was unexpected",
                speaker="owner",
                language="en",
                confidence=0.95,
            )
        ],
    )
    prepared = prepare_hybrid_proposals(tiny_video, config, transcript=transcript)
    routed = prepare_hybrid_routing(
        config,
        prepared.ingest.session_id,
        weak_sample_interval_ms=1_000,
    )
    assert routed.routing.selected_proposal_ids

    first = prepare_hybrid_contexts(config, prepared.ingest.session_id)
    paths = session_paths(config.storage.data_dir, prepared.ingest.session_id)
    parent_sha = read_json(paths.proxy_dir / "metadata.json")

    assert len(first.contexts) == len(routed.routing.selected_proposal_ids)
    assert first.generated == len(first.contexts)
    assert first.cache_hits == 0
    assert first.contexts_dir == paths.hybrid_contexts_dir
    for context in first.contexts:
        context_path = paths.root / context.proxy_path
        assert context_path.is_file()
        validated = validate_hybrid_context_proxy(
            context_path,
            paths.hybrid_contexts_dir,
        )
        assert validated == context
        assert context.plan.context_start_ms <= context.plan.anchor_start_ms
        assert context.plan.context_end_ms >= context.plan.anchor_end_ms
        assert context.proxy_path.startswith("hybrid/contexts/")
    assert parent_sha["proxy_path"] == "proxy/analysis_proxy.mp4"
    assert config.scout.allow_remote_upload is False

    second = prepare_hybrid_contexts(config, prepared.ingest.session_id)
    assert second.generated == 0
    assert second.cache_hits == len(first.contexts)
    assert second.contexts == first.contexts

    with pytest.raises(ValidationError, match="escapes the committed context root"):
        validate_hybrid_context_proxy(tiny_video, paths.hybrid_contexts_dir)


def test_m6_stops_before_reconcile_without_extracting(
    tmp_path: Path, tiny_video: Path, ffmpeg_path: Path, ffprobe_path: Path
) -> None:
    config = _config(tmp_path / "library", ffmpeg_path, ffprobe_path)
    result = analyze_m6_source(tiny_video, config, stop_after="scout")
    assert result.scout is not None
    assert result.session_map is None
    assert result.extraction is None
    assert result.scout.aggregate_preflight.blocked is False


def test_m6_window_privacy_cache_and_aggregate_cost_preflight(
    tmp_path: Path, tiny_video: Path, ffmpeg_path: Path, ffprobe_path: Path
) -> None:
    config = _config(tmp_path / "library", ffmpeg_path, ffprobe_path)
    local = analyze_m6_source(tiny_video, config, stop_after="windows")
    assert local.proxy is not None and local.local_signals is not None
    assert local.windows is not None
    window = local.windows.windows[0]
    window_path = local.windows.session_dir / window.proxy_path
    validated = validate_window_proxy_upload(
        window_path, local.windows.session_dir / "scout" / "windows"
    )
    assert validated.window_id == window.window_id

    provider = FakeWindowScout()
    first = run_windowed_scout(
        local.ingest.source,
        local.windows,
        local.local_signals,
        config,
        fake_provider=provider,
    )
    assert provider.calls == [window.window_id]
    assert first.activity.provider_generation_calls == 1
    assert first.activity.provider_uploads == 0
    assert first.activity.paid_reservations_created == 0
    provider2 = FakeWindowScout()
    second = run_windowed_scout(
        local.ingest.source,
        local.windows,
        local.local_signals,
        config,
        fake_provider=provider2,
    )
    assert all(item.cache_hit for item in second.results)
    assert provider2.calls == []
    assert second.activity.provider_generation_calls == 0
    assert second.activity.cache_hits == 1

    forced_provider = FakeWindowScout()
    forced = run_windowed_scout(
        local.ingest.source,
        local.windows,
        local.local_signals,
        config,
        fake_provider=forced_provider,
        force=True,
    )
    assert forced_provider.calls == [window.window_id]
    assert forced.activity.provider_generation_calls == 1

    gemini_config = config.model_copy(
        update={
            "scout": ScoutConfig(backend="gemini"),
            "cost": CostConfig(monthly_budget_thb=Decimal("100")),
        }
    )
    service = CostService(
        gemini_config,
        registry=build_gemini_registry(),
        pricing=production_pricing_catalog(),
        fx_snapshot=FxSnapshot(
            base_currency="USD",
            quote_currency="THB",
            rate=Decimal("36"),
            captured_at=datetime.now(UTC),
            source="offline-test",
        ),
    )
    quoted = aggregate_window_preflight(
        local.ingest.source,
        local.windows.windows,
        gemini_config,
        cost_service=service,
    )
    assert quoted.estimated_micro_thb > 0
    assert quoted.blocked is False
    cached = aggregate_window_preflight(
        local.ingest.source,
        local.windows.windows,
        gemini_config,
        cached_window_ids={window.window_id},
        cost_service=service,
    )
    assert cached.estimated_micro_thb == 0
    assert first.aggregate_preflight.estimated_micro_thb == 0


def test_m6_gemini_windows_settle_cache_and_cleanup_without_network(
    tmp_path: Path, tiny_video: Path, ffmpeg_path: Path, ffprobe_path: Path
) -> None:
    base = _config(tmp_path / "library", ffmpeg_path, ffprobe_path)
    local = analyze_m6_source(tiny_video, base, stop_after="windows")
    assert local.windows is not None and local.local_signals is not None
    config = base.model_copy(
        update={
            "scout": ScoutConfig(backend="gemini", allow_remote_upload=True),
            "cost": CostConfig(monthly_budget_thb=Decimal("100")),
        }
    )
    window = local.windows.windows[0]
    transport = FakeGeminiTransport(
        response=_window_response(
            local.ingest.source.duration_ms, window.source_start_ms, window.source_end_ms
        )
    )
    service = _gemini_cost_service(config)
    first = run_windowed_scout(
        local.ingest.source,
        local.windows,
        local.local_signals,
        config,
        gemini_transport=transport,
        cost_service=service,
    )
    assert transport.generation_count == 1
    assert first.activity.provider_generation_calls == 1
    assert first.activity.provider_uploads == 1
    assert first.activity.paid_reservations_created == 1
    assert first.results[0].cache_hit is False
    assert service.ledger.list_calls()[0].status.value == "SETTLED"
    second = run_windowed_scout(
        local.ingest.source,
        local.windows,
        local.local_signals,
        config,
        gemini_transport=transport,
        cost_service=service,
        force=True,
    )
    assert second.results[0].cache_hit is True
    assert transport.generation_count == 1
    assert second.activity.provider_generation_calls == 0
    assert second.activity.provider_uploads == 0
    assert second.activity.paid_reservations_created == 0


def test_m6_gemini_ambiguous_window_is_never_retried(
    tmp_path: Path, tiny_video: Path, ffmpeg_path: Path, ffprobe_path: Path
) -> None:
    base = _config(tmp_path / "library", ffmpeg_path, ffprobe_path)
    local = analyze_m6_source(tiny_video, base, stop_after="windows")
    assert local.windows is not None and local.local_signals is not None
    config = base.model_copy(
        update={
            "scout": ScoutConfig(backend="gemini", allow_remote_upload=True),
            "cost": CostConfig(monthly_budget_thb=Decimal("100")),
        }
    )
    transport = FakeGeminiTransport(
        generation_error=GeminiProviderError("dispatched timeout", may_have_dispatched=True)
    )
    service = _gemini_cost_service(config)
    with pytest.raises(Exception, match="dispatched timeout"):
        run_windowed_scout(
            local.ingest.source,
            local.windows,
            local.local_signals,
            config,
            gemini_transport=transport,
            cost_service=service,
        )
    assert service.ledger.list_calls()[0].status.value == "AMBIGUOUS"
    calls = transport.generation_count
    with pytest.raises(Exception, match="unresolved cost lifecycle"):
        run_windowed_scout(
            local.ingest.source,
            local.windows,
            local.local_signals,
            config,
            gemini_transport=transport,
            cost_service=service,
        )
    assert transport.generation_count == calls


def test_m6_completed_output_missing_usage_becomes_ambiguous_and_is_never_reused(
    tmp_path: Path, tiny_video: Path, ffmpeg_path: Path, ffprobe_path: Path
) -> None:
    base = _config(tmp_path / "library", ffmpeg_path, ffprobe_path)
    local = analyze_m6_source(tiny_video, base, stop_after="windows")
    assert local.windows is not None and local.local_signals is not None
    config = base.model_copy(
        update={
            "scout": ScoutConfig(backend="gemini", allow_remote_upload=True),
            "cost": CostConfig(monthly_budget_thb=Decimal("100")),
        }
    )
    window = local.windows.windows[0]
    response = _window_response(
        local.ingest.source.duration_ms, window.source_start_ms, window.source_end_ms
    )
    response["usage"] = {}
    transport = FakeGeminiTransport(response=response)
    service = _gemini_cost_service(config)
    with pytest.raises(Exception, match="missing, conflicting"):
        run_windowed_scout(
            local.ingest.source,
            local.windows,
            local.local_signals,
            config,
            gemini_transport=transport,
            cost_service=service,
        )
    assert transport.generation_count == 1
    assert service.ledger.list_calls()[0].status.value == "AMBIGUOUS"
    item_dir = local.windows.session_dir / "scout" / "windows" / window.window_id
    assert (item_dir / "response.raw.json").is_file()
    assert not (item_dir / "response.canonical.json").exists()
    with pytest.raises(Exception, match="unresolved cost lifecycle"):
        run_windowed_scout(
            local.ingest.source,
            local.windows,
            local.local_signals,
            config,
            gemini_transport=transport,
            cost_service=service,
        )
    assert transport.generation_count == 1
    assert service.ledger.list_calls()[0].status.value == "AMBIGUOUS"
    assert not (item_dir / "response.canonical.json").exists()


@pytest.mark.parametrize(
    "status, message",
    [
        ("IN_FLIGHT", "IN_FLIGHT"),
        ("RESERVED", "RESERVED"),
        ("RELEASED", "RELEASED"),
    ],
)
def test_m6_completed_raw_conflicting_ledger_state_blocks_without_generation(
    tmp_path: Path,
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    status: str,
    message: str,
) -> None:
    config, local, _window, transport, service, item_dir = _settled_window_fixture(
        tmp_path, tiny_video, ffmpeg_path, ffprobe_path
    )
    (item_dir / "response.canonical.json").unlink()
    _set_ledger_status(service, status)
    generation_count = transport.generation_count
    with pytest.raises(Exception, match=message):
        run_windowed_scout(
            local.ingest.source,
            local.windows,
            local.local_signals,
            config,
            gemini_transport=transport,
            cost_service=service,
        )
    assert transport.generation_count == generation_count
    assert not (item_dir / "response.canonical.json").exists()
    assert service.ledger.list_calls()[0].status.value == status


def test_m6_completed_raw_without_ledger_record_blocks_without_generation(
    tmp_path: Path, tiny_video: Path, ffmpeg_path: Path, ffprobe_path: Path
) -> None:
    config, local, _window, transport, service, item_dir = _settled_window_fixture(
        tmp_path, tiny_video, ffmpeg_path, ffprobe_path
    )
    (item_dir / "response.canonical.json").unlink()
    with sqlite3.connect(service.ledger.path) as connection:
        connection.execute("DELETE FROM ledger_events")
        connection.execute("DELETE FROM calls")
    generation_count = transport.generation_count
    with pytest.raises(Exception, match="without a corresponding cost ledger"):
        run_windowed_scout(
            local.ingest.source,
            local.windows,
            local.local_signals,
            config,
            gemini_transport=transport,
            cost_service=service,
        )
    assert transport.generation_count == generation_count
    assert not (item_dir / "response.canonical.json").exists()
    assert service.ledger.list_calls() == ()


def test_m6_settled_raw_recanonicalizes_without_generation(
    tmp_path: Path, tiny_video: Path, ffmpeg_path: Path, ffprobe_path: Path
) -> None:
    config, local, _window, transport, service, item_dir = _settled_window_fixture(
        tmp_path, tiny_video, ffmpeg_path, ffprobe_path
    )
    (item_dir / "response.canonical.json").unlink()
    second = run_windowed_scout(
        local.ingest.source,
        local.windows,
        local.local_signals,
        config,
        gemini_transport=transport,
        cost_service=service,
    )
    assert transport.generation_count == 1
    assert second.results[0].cache_hit is False
    assert second.results[0].cache_reason == "paid-result-reused-for-local-canonicalization"
    assert (item_dir / "response.canonical.json").is_file()
    assert service.ledger.list_calls()[0].status.value == "SETTLED"


def test_m6_settled_full_cache_retries_cleanup_without_generation(
    tmp_path: Path, tiny_video: Path, ffmpeg_path: Path, ffprobe_path: Path
) -> None:
    config, local, _window, first_transport, service, item_dir = _settled_window_fixture(
        tmp_path,
        tiny_video,
        ffmpeg_path,
        ffprobe_path,
        delete_error=RuntimeError("synthetic cleanup outage"),
    )
    assert first_transport.generation_count == 1
    assert read_json(item_dir / "gemini_remote_file.json")["deletion_status"] == "pending"
    recovery_transport = FakeGeminiTransport(
        response={"status": "completed", "output_text": "{}", "usage": {}}
    )
    second = run_windowed_scout(
        local.ingest.source,
        local.windows,
        local.local_signals,
        config,
        gemini_transport=recovery_transport,
        cost_service=service,
    )
    assert recovery_transport.generation_count == 0
    assert second.results[0].cache_hit is True
    assert read_json(item_dir / "gemini_remote_file.json")["deletion_status"] == "deleted"
    assert service.ledger.list_calls()[0].status.value == "SETTLED"


def test_m6_interrupted_extraction_retries_only_incomplete_candidate(
    tmp_path: Path,
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path / "library", ffmpeg_path, ffprobe_path)
    reconciled = analyze_m6_source(tiny_video, config, stop_after="reconcile")
    assert reconciled.session_map is not None

    import game_highlight_finder.pipeline.extraction as extraction_module

    real_run = extraction_module.run_ffmpeg

    def interrupted(*args: object, **kwargs: object) -> object:
        raise FFmpegCancelled("synthetic interruption")

    monkeypatch.setattr(extraction_module, "run_ffmpeg", interrupted)
    incomplete = extract_candidates(reconciled.ingest.source, reconciled.session_map, config)
    assert incomplete.incomplete == len(reconciled.session_map.candidates)

    monkeypatch.setattr(extraction_module, "run_ffmpeg", real_run)
    resumed = extract_candidates(reconciled.ingest.source, reconciled.session_map, config)
    assert resumed.incomplete == 0
    assert resumed.completed == len(reconciled.session_map.candidates)
