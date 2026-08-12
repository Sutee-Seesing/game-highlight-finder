from __future__ import annotations

from pathlib import Path

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
