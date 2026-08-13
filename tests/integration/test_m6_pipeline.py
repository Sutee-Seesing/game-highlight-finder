from __future__ import annotations

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
from game_highlight_finder.media.ffmpeg import FFmpegCancelled
from game_highlight_finder.pipeline.extraction import extract_candidates
from game_highlight_finder.pipeline.gemini_scout import build_gemini_registry
from game_highlight_finder.pipeline.runner import analyze_m6_source
from game_highlight_finder.pipeline.windowed_scout import (
    FakeWindowScout,
    aggregate_window_preflight,
    run_windowed_scout,
    validate_window_proxy_upload,
)
from game_highlight_finder.providers.gemini import FakeGeminiTransport, GeminiProviderError


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
    assert first.results[0].cache_hit is False
    assert service.ledger.list_calls()[0].status.value == "SETTLED"
    second = run_windowed_scout(
        local.ingest.source,
        local.windows,
        local.local_signals,
        config,
        gemini_transport=transport,
        cost_service=service,
    )
    assert second.results[0].cache_hit is True
    assert transport.generation_count == 1


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
