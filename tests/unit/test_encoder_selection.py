from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from game_highlight_finder.domain.models import Candidate
from game_highlight_finder.errors import DependencyError
from game_highlight_finder.media import tools
from game_highlight_finder.pipeline.boundary_refinement import (
    build_boundary_refinement_proxy_command,
    plan_boundary_refinement,
)


def _completed(
    command: list[str], returncode: int, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout="", stderr=stderr)


def test_encoder_selection_uses_qsv_when_nvenc_is_advertised_but_cannot_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted: list[str] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        encoder = command[command.index("-c:v") + 1]
        attempted.append(encoder)
        if encoder == "h264_nvenc":
            return _completed(command, 1, "Cannot load nvcuda.dll")
        return _completed(command, 0)

    monkeypatch.setattr(tools.subprocess, "run", fake_run)
    selected = tools.select_usable_h264_encoder(Path("ffmpeg"))

    assert selected.encoder == "h264_qsv"
    assert selected.preset == "veryfast"
    assert selected.hardware_accelerated is True
    assert attempted == ["h264_nvenc", "h264_qsv"]


def test_encoder_selection_falls_back_to_cpu_only_after_hardware_encoders_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted: list[str] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        encoder = command[command.index("-c:v") + 1]
        attempted.append(encoder)
        return _completed(command, 0 if encoder == "libx264" else 1, "hardware unavailable")

    monkeypatch.setattr(tools.subprocess, "run", fake_run)
    selected = tools.select_usable_h264_encoder(Path("ffmpeg"))

    assert selected.encoder == "libx264"
    assert selected.preset == "veryfast"
    assert selected.hardware_accelerated is False
    assert attempted == ["h264_nvenc", "h264_qsv", "libx264"]


def test_encoder_selection_fails_closed_when_no_h264_encoder_can_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return _completed(command, 1, "encoder unavailable")

    monkeypatch.setattr(tools.subprocess, "run", fake_run)
    with pytest.raises(DependencyError, match=r"No usable H\.264 encoder"):
        tools.select_usable_h264_encoder(Path("ffmpeg"))


def test_boundary_refinement_proxy_uses_machine_selected_qsv_codec() -> None:
    candidate = Candidate(
        candidate_id="cand_0123456789abcdef",
        category="SKILL",
        event_start_ms=133_000,
        event_end_ms=141_000,
        score=6.5,
        confidence=0.9,
        reason="same fight",
    )
    plan = plan_boundary_refinement(candidate, 600_000)
    selected = tools.H264EncoderChoice(
        encoder="h264_qsv", preset="veryfast", hardware_accelerated=True
    )

    command = build_boundary_refinement_proxy_command(
        Path("ffmpeg"),
        Path("context.mp4"),
        Path("slow.mp4"),
        plan,
        has_audio=True,
        encoder=selected,
    )

    assert "h264_qsv" in command
    assert "veryfast" in command
    assert "h264_nvenc" not in command
    assert "setpts=2*(PTS-STARTPTS)" in command
