from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from game_highlight_finder.cli import app
from game_highlight_finder.config import AppConfig, StorageConfig
from game_highlight_finder.doctor import CheckLevel, run_doctor

runner = CliRunner()


def test_doctor_reports_missing_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("game_highlight_finder.doctor.resolve_executable", lambda *_args: None)
    config = AppConfig(storage=StorageConfig(data_dir=tmp_path / "data"))

    report = run_doctor(config)

    tool_checks = {check.name: check for check in report.checks}
    assert tool_checks["ffmpeg"].level is CheckLevel.FAIL
    assert tool_checks["ffprobe"].level is CheckLevel.FAIL
    assert "scoop install ffmpeg" in tool_checks["ffmpeg"].message


def test_config_check_cli_success(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--data-dir", str(tmp_path / "data"), "config", "check"])

    assert result.exit_code == 0
    assert "[PASS] configuration is valid" in result.stdout


def test_analyze_missing_file_has_expected_error(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["--data-dir", str(tmp_path / "data"), "analyze", str(tmp_path / "missing.mp4")],
    )

    assert result.exit_code == 2
    assert "[FAIL] source/input" in result.output
    assert "Traceback" not in result.output


def test_analyze_reports_missing_source_for_m2_stop_boundary(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["analyze", str(tmp_path / "video.mp4"), "--stop-after", "proxy"],
    )

    assert result.exit_code == 2
    assert "[FAIL] source/input" in result.output


def test_m6_dry_run_preflights_local_windows_without_provider_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session_id = "2026-08-28_unknown_aaaaaaaaaaaa"
    window_id = "scout_window_1111111111111111"
    signals_path = tmp_path / "data" / "sessions" / session_id / "scout" / "windows" / window_id
    signals_path.mkdir(parents=True)
    (signals_path / "signals.json").write_text("{}", encoding="utf-8")
    source = SimpleNamespace()
    windows = (SimpleNamespace(window_id=window_id),)
    local_result = SimpleNamespace(
        ingest=SimpleNamespace(session_id=session_id, source=source),
        windows=SimpleNamespace(windows=windows),
    )
    observed: dict[str, object] = {}

    def local_windows_only(*_args: object, **kwargs: object) -> object:
        observed["stop_after"] = kwargs.get("stop_after")
        return local_result

    def fake_preflight(*args: object, **kwargs: object) -> object:
        observed["source"] = args[0]
        observed["windows"] = args[1]
        observed["cached_window_ids"] = kwargs.get("cached_window_ids")
        observed["local_signal_summaries"] = kwargs.get("local_signal_summaries")
        blocked = bool(observed.get("return_blocked", False))
        return SimpleNamespace(
            total_windows=1,
            estimated_micro_thb=123_456,
            available_micro_thb=100_000 if blocked else 10_000_000,
            blocked=blocked,
            reason=(
                "aggregate estimate exceeds available monthly budget"
                if blocked else "aggregate Gemini window preflight passed"
            ),
            window_estimates_micro_thb={window_id: 123_456},
        )

    def unexpected_transport(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("M6 dry-run must not construct a Gemini transport")

    monkeypatch.setattr("game_highlight_finder.cli.analyze_m6_source", local_windows_only)
    monkeypatch.setattr("game_highlight_finder.cli.aggregate_window_preflight", fake_preflight)
    monkeypatch.setattr("game_highlight_finder.cli.GenAITransport", unexpected_transport)
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "data"),
            "analyze",
            str(tmp_path / "synthetic.mp4"),
            "--scout-backend",
            "gemini",
            "--dry-run",
            "--m6",
            "--stop-after",
            "scout",
        ],
    )

    assert result.exit_code == 0
    assert observed["stop_after"] == "windows"
    assert observed["source"] is source
    assert observed["windows"] == windows
    assert observed["cached_window_ids"] == set()
    assert observed["local_signal_summaries"] == {window_id: {}}
    assert "provider/API calls ZERO" in result.output
    assert "Paid-response cache assumption: ZERO" in result.output
    assert "Aggregate maximum reserved: ฿0.12" in result.output
    assert "Aggregate maximum reserved micro-THB: 123456" in result.output
    assert "Monthly available budget micro-THB: 10000000" in result.output
    assert "Post-reservation headroom micro-THB: 9876544" in result.output
    assert "Provider transport constructed: NO" in result.output
    assert "Remote upload: NO" in result.output
    assert "Ledger reservation: NO" in result.output

    observed["return_blocked"] = True
    blocked_result = runner.invoke(
        app,
        [
            "--data-dir", str(tmp_path / "data"), "analyze", str(tmp_path / "synthetic.mp4"),
            "--scout-backend", "gemini", "--dry-run", "--m6", "--stop-after", "scout",
        ],
    )
    assert blocked_result.exit_code == 2
    assert "Budget gate: BLOCKED" in blocked_result.output
    assert "M6 Gemini window preflight is blocked" in blocked_result.output
    assert "Provider transport constructed: NO" in blocked_result.output
