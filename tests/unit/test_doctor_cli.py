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


def test_hybrid_proposals_cli_is_local_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: dict[str, object] = {}
    source = tmp_path / "synthetic.mp4"
    artifact = tmp_path / "data" / "sessions" / "fixture" / "hybrid" / "proposals.json"

    def fake_prepare(
        video: Path,
        config: AppConfig,
        *,
        manual_markers: object | None = None,
        transcript: object | None = None,
    ) -> object:
        observed["video"] = video
        observed["allow_remote_upload"] = config.scout.allow_remote_upload
        observed["manual_markers"] = manual_markers
        observed["transcript"] = transcript
        return SimpleNamespace(
            proposals=SimpleNamespace(proposals=[object(), object()]),
            proposal_summary=SimpleNamespace(
                proposals_per_source_hour=12.0,
                clustered_reduction_count=1,
            ),
            proposals_path=artifact,
            proposal_summary_path=artifact.with_name("proposal_summary.json"),
            ingest=SimpleNamespace(
                session_id="fixture",
                source=SimpleNamespace(sha256="a" * 64),
            ),
        )

    monkeypatch.setattr("game_highlight_finder.cli.prepare_hybrid_proposals", fake_prepare)
    result = runner.invoke(
        app,
        ["--data-dir", str(tmp_path / "data"), "hybrid", "proposals", str(source)],
    )

    assert result.exit_code == 0
    assert observed == {
        "video": source,
        "allow_remote_upload": False,
        "manual_markers": None,
        "transcript": None,
    }
    assert "proposals: 2" in result.output
    assert "proposal density/source-hour: 12.000" in result.output
    assert "clustered reduction: 1" in result.output
    assert "provider calls: ZERO" in result.output


def test_hybrid_proposals_cli_accepts_source_bound_manual_markers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}
    source = tmp_path / "synthetic.mp4"
    marker_file = tmp_path / "manual_markers.json"
    marker_file.write_text(
        '{"schema_version":1,"source_sha256":"'
        + "b" * 64
        + '","source_duration_ms":1000,"markers":[{'
        '"start_ms":100,"end_ms":200,"label":"owner marker",'
        '"event_hypothesis":"OBJECTIVE_PLANTED","confidence":1.0}],"notes":[]}',
        encoding="utf-8",
    )

    def fake_prepare(
        video: Path,
        config: AppConfig,
        *,
        manual_markers: object | None = None,
        transcript: object | None = None,
    ) -> object:
        observed["video"] = video
        observed["allow_remote_upload"] = config.scout.allow_remote_upload
        observed["manual_markers"] = manual_markers
        observed["transcript"] = transcript
        proposal_path = tmp_path / "proposals.json"
        return SimpleNamespace(
            proposals=SimpleNamespace(proposals=[object()]),
            proposal_summary=SimpleNamespace(
                proposals_per_source_hour=1.0,
                clustered_reduction_count=0,
            ),
            proposals_path=proposal_path,
            proposal_summary_path=proposal_path.with_name("proposal_summary.json"),
            ingest=SimpleNamespace(
                session_id="fixture",
                source=SimpleNamespace(sha256="b" * 64),
            ),
        )

    monkeypatch.setattr("game_highlight_finder.cli.prepare_hybrid_proposals", fake_prepare)
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "data"),
            "hybrid",
            "proposals",
            str(source),
            "--manual-markers",
            str(marker_file),
        ],
    )

    assert result.exit_code == 0
    marker_set = observed["manual_markers"]
    assert marker_set is not None
    assert marker_set.source_sha256 == "b" * 64
    assert marker_set.markers[0].event_hypothesis == "OBJECTIVE_PLANTED"
    assert observed["transcript"] is None
    assert "provider calls: ZERO" in result.output


def test_hybrid_proposals_cli_accepts_source_bound_transcript(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}
    source = tmp_path / "synthetic.mp4"
    transcript_file = tmp_path / "transcript.json"
    transcript_file.write_text(
        '{"schema_version":1,"source_sha256":"'
        + "c" * 64
        + '","source_duration_ms":1000,"utterances":[{'
        '"start_ms":100,"end_ms":300,"text":"that was close",'
        '"speaker":"owner","language":"en","confidence":0.9}],"notes":[]}',
        encoding="utf-8",
    )

    def fake_prepare(
        video: Path,
        config: AppConfig,
        *,
        manual_markers: object | None = None,
        transcript: object | None = None,
    ) -> object:
        observed["video"] = video
        observed["allow_remote_upload"] = config.scout.allow_remote_upload
        observed["manual_markers"] = manual_markers
        observed["transcript"] = transcript
        proposal_path = tmp_path / "proposals.json"
        return SimpleNamespace(
            proposals=SimpleNamespace(proposals=[object()]),
            proposal_summary=SimpleNamespace(
                proposals_per_source_hour=3.0,
                clustered_reduction_count=0,
            ),
            proposals_path=proposal_path,
            proposal_summary_path=proposal_path.with_name("proposal_summary.json"),
            ingest=SimpleNamespace(
                session_id="fixture",
                source=SimpleNamespace(sha256="c" * 64),
            ),
        )

    monkeypatch.setattr("game_highlight_finder.cli.prepare_hybrid_proposals", fake_prepare)
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "data"),
            "hybrid",
            "proposals",
            str(source),
            "--transcript",
            str(transcript_file),
        ],
    )

    assert result.exit_code == 0
    transcript = observed["transcript"]
    assert transcript is not None
    assert transcript.source_sha256 == "c" * 64
    assert transcript.utterances[0].text == "that was close"
    assert observed["manual_markers"] is None
    assert "provider calls: ZERO" in result.output


def test_hybrid_route_cli_is_provider_free_and_requires_explicit_sampling_interval(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}
    session_id = "2026-09-09_unknown_aaaaaaaaaaaa"

    def fake_route(
        config: AppConfig,
        observed_session_id: str,
        *,
        weak_sample_interval_ms: int,
    ) -> object:
        observed["allow_remote_upload"] = config.scout.allow_remote_upload
        observed["session_id"] = observed_session_id
        observed["weak_sample_interval_ms"] = weak_sample_interval_ms
        return SimpleNamespace(
            proposals=SimpleNamespace(proposals=[object(), object(), object(), object()]),
            routing=SimpleNamespace(
                selected_proposal_ids=["a", "b"],
                deferred_proposal_ids=["c", "d"],
                selected_per_source_hour=12.0,
                route_counts={"SUPPORTED": 1, "SAMPLED_WEAK": 1, "DEFERRED_WEAK": 2},
            ),
            routing_plan_path=tmp_path / "routing_plan.json",
            routed_proposals_path=tmp_path / "routed_proposals.json",
        )

    monkeypatch.setattr("game_highlight_finder.cli.prepare_hybrid_routing", fake_route)
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "data"),
            "hybrid",
            "route",
            session_id,
            "--weak-sample-interval-seconds",
            "60",
        ],
    )

    assert result.exit_code == 0
    assert observed == {
        "allow_remote_upload": False,
        "session_id": session_id,
        "weak_sample_interval_ms": 60_000,
    }
    assert "full proposal neighborhoods: 4" in result.output
    assert "selected for semantic inspection: 2" in result.output
    assert "provider calls: ZERO" in result.output


def test_hybrid_contexts_cli_materializes_local_contexts_without_provider_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}
    session_id = "2026-09-09_unknown_bbbbbbbbbbbb"

    def fake_contexts(
        config: AppConfig,
        observed_session_id: str,
        *,
        force: bool = False,
    ) -> object:
        observed["allow_remote_upload"] = config.scout.allow_remote_upload
        observed["session_id"] = observed_session_id
        observed["force"] = force
        return SimpleNamespace(
            contexts=(object(), object()),
            generated=2,
            cache_hits=0,
            contexts_dir=tmp_path / "data" / "sessions" / session_id / "hybrid" / "contexts",
        )

    monkeypatch.setattr("game_highlight_finder.cli.prepare_hybrid_contexts", fake_contexts)
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "data"),
            "hybrid",
            "contexts",
            session_id,
        ],
    )

    assert result.exit_code == 0
    assert observed == {
        "allow_remote_upload": False,
        "session_id": session_id,
        "force": False,
    }
    assert "contexts: 2" in result.output
    assert "source upload: FORBIDDEN" in result.output
    assert "provider calls: ZERO" in result.output


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
