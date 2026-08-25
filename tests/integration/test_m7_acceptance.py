from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import game_highlight_finder.pipeline.runner as runner_module
from game_highlight_finder.cli import app
from game_highlight_finder.config import (
    AppConfig,
    ExtractionConfig,
    MediaConfig,
    ProxyConfig,
    StorageConfig,
    ToolsConfig,
)
from game_highlight_finder.cost.ledger import CostLedger
from game_highlight_finder.pipeline.runner import analyze_v1_source
from game_highlight_finder.pipeline.windowed_scout import FakeWindowScout, run_windowed_scout
from game_highlight_finder.storage.hashing import hash_file
from game_highlight_finder.storage.sessions import load_manifest, session_paths


def test_m7_cold_warm_full_v1_is_deterministic_and_offline(
    tmp_path: Path,
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AppConfig(
        storage=StorageConfig(data_dir=tmp_path / "library"),
        tools=ToolsConfig(ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path),
        media=MediaConfig(
            proxy=ProxyConfig(video_codec="libx264", preset="veryfast"),
            extraction=ExtractionConfig(video_codec="libx264", preset="veryfast"),
        ),
    )
    original_source_hash = hash_file(tiny_video, source=True)
    providers: list[FakeWindowScout] = []
    original_runner = run_windowed_scout

    def instrumented_runner(*args: Any, **kwargs: Any) -> Any:
        provider = FakeWindowScout()
        providers.append(provider)
        kwargs["fake_provider"] = provider
        return original_runner(*args, **kwargs)

    monkeypatch.setattr(runner_module, "run_windowed_scout", instrumented_runner)
    cold = analyze_v1_source(tiny_video, config)
    assert cold.report is not None and cold.report.cache_hit is False
    assert cold.ranking is not None
    windows = cold.m6.windows
    assert windows is not None
    assert cold.m6.scout is not None
    assert cold.m6.session_map is not None
    extraction = cold.m6.extraction
    assert extraction is not None
    assert providers[0].calls
    assert cold.m6.scout.activity.provider_generation_calls == len(providers[0].calls)

    paths = session_paths(config.storage.data_dir, cold.m6.ingest.session_id)
    manifest = load_manifest(paths.manifest)
    assert all(stage.status.value == "COMPLETED" for stage in manifest.stages.values())
    assert paths.proxy_dir.joinpath("analysis_proxy.mp4").is_file()
    assert paths.signals_dir.joinpath("activity.json").is_file()
    assert all((paths.root / window.proxy_path).is_file() for window in windows.windows)
    assert paths.session_map.is_file()
    assert paths.extraction_manifest.is_file()
    assert paths.ranking_path.is_file()
    assert paths.report_path.is_file()
    assert paths.report_meta_path.is_file()
    assert (
        CostLedger(
            config.storage.data_dir / "cost" / "ledger.sqlite3", budget_micro_thb=100_000_000
        ).list_calls()
        == ()
    )

    proxy = paths.proxy_dir / "analysis_proxy.mp4"
    proxy_identity = (hash_file(proxy), proxy.stat().st_mtime_ns)
    window_identity = {
        window.window_id: (
            hash_file(paths.root / window.proxy_path),
            (paths.root / window.proxy_path).stat().st_mtime_ns,
        )
        for window in windows.windows
    }
    extraction_identity = {
        record.candidate_id: (
            hash_file(paths.root / record.output_path),
            (paths.root / record.output_path).stat().st_mtime_ns,
            hash_file(paths.root / record.thumbnail_path)
            if record.thumbnail_path is not None
            else None,
        )
        for record in extraction.manifest.records
        if record.status == "COMPLETED"
    }
    ranking_hash = hash_file(paths.ranking_path)
    report_hash = hash_file(paths.report_path)
    source_before_warm = hash_file(tiny_video, source=True)

    warm = analyze_v1_source(tiny_video, config)
    assert warm.report is not None and warm.report.cache_hit is True
    assert warm.m6.scout is not None
    warm_windows = warm.m6.windows
    assert warm_windows is not None
    warm_extraction = warm.m6.extraction
    assert warm_extraction is not None
    assert providers[1].calls == []
    assert warm.m6.scout.activity.provider_generation_calls == 0
    assert warm.m6.scout.activity.cache_hits == len(windows.windows)
    assert (hash_file(proxy), proxy.stat().st_mtime_ns) == proxy_identity
    assert {
        window.window_id: (
            hash_file(paths.root / window.proxy_path),
            (paths.root / window.proxy_path).stat().st_mtime_ns,
        )
        for window in warm_windows.windows
    } == window_identity
    assert {
        record.candidate_id: (
            hash_file(paths.root / record.output_path),
            (paths.root / record.output_path).stat().st_mtime_ns,
            hash_file(paths.root / record.thumbnail_path)
            if record.thumbnail_path is not None
            else None,
        )
        for record in warm_extraction.manifest.records
        if record.status == "COMPLETED"
    } == extraction_identity
    assert hash_file(paths.ranking_path) == ranking_hash
    assert hash_file(paths.report_path) == report_hash

    runner = CliRunner()
    resume_result = runner.invoke(
        app,
        ["--data-dir", str(config.storage.data_dir), "resume", cold.m6.ingest.session_id],
    )
    assert resume_result.exit_code == 0, resume_result.output
    assert "Real Gemini API calls: ZERO" in resume_result.output
    assert providers[2].calls == []

    paths.report_path.write_text("stale report", encoding="utf-8")
    provider_count = len(providers)
    report_result = runner.invoke(
        app,
        ["--data-dir", str(config.storage.data_dir), "report", cold.m6.ingest.session_id],
    )
    assert report_result.exit_code == 0, report_result.output
    assert len(providers) == provider_count
    assert hash_file(paths.report_path) == report_hash
    assert hash_file(tiny_video, source=True) == original_source_hash == source_before_warm
