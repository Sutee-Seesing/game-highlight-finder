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


def test_m6_dry_run_fails_closed_before_pipeline_or_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    called = False

    def unexpected_pipeline(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("M6 pipeline must not run for --dry-run --m6")

    monkeypatch.setattr("game_highlight_finder.cli.analyze_m6_source", unexpected_pipeline)
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "data"),
            "analyze",
            str(tmp_path / "synthetic.mp4"),
            "--scout-backend",
            "gemini",
            "--allow-remote-upload",
            "--dry-run",
            "--m6",
            "--stop-after",
            "scout",
        ],
    )

    assert result.exit_code == 2
    assert called is False
    assert "refusing provider execution" in result.output
    assert "No provider call or upload was made" in result.output


def test_m6_windows_stage_is_allowed_without_remote_upload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: dict[str, object] = {}

    def fake_pipeline(video: Path, config: AppConfig, *, stop_after: str) -> object:
        observed["video"] = video
        observed["allow_remote_upload"] = config.scout.allow_remote_upload
        observed["backend"] = config.scout.backend
        observed["stop_after"] = stop_after
        return SimpleNamespace(
            windows=None,
            scout=None,
            session_map=None,
            extraction=None,
            ingest=SimpleNamespace(
                session_id="local-windows-only",
                session_dir=tmp_path / "data" / "sessions" / "local-windows-only",
            ),
        )

    monkeypatch.setattr("game_highlight_finder.cli.analyze_m6_source", fake_pipeline)
    source = tmp_path / "synthetic.mp4"
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "data"),
            "analyze",
            str(source),
            "--scout-backend",
            "gemini",
            "--m6",
            "--stop-after",
            "windows",
        ],
    )

    assert result.exit_code == 0
    assert "[PASS] M6 windowed analysis completed" in result.output
    assert observed == {
        "video": source,
        "allow_remote_upload": False,
        "backend": "gemini",
        "stop_after": "windows",
    }


def test_m6_gemini_scout_still_requires_remote_upload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    called = False

    def unexpected_pipeline(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("Scout must remain blocked before remote-upload authorization")

    monkeypatch.setattr("game_highlight_finder.cli.analyze_m6_source", unexpected_pipeline)
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "data"),
            "analyze",
            str(tmp_path / "synthetic.mp4"),
            "--scout-backend",
            "gemini",
            "--m6",
            "--stop-after",
            "scout",
        ],
    )

    assert result.exit_code == 2
    assert called is False
    assert "requires --allow-remote-upload before Scout execution" in result.output
