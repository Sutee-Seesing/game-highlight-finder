from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath

import pytest

from game_highlight_finder.errors import ValidationError
from game_highlight_finder.media.ffprobe import build_ffprobe_command, parse_source_asset

SHA = "a" * 64


def test_ffprobe_command_preserves_spaces_and_unicode() -> None:
    tool = Path(PureWindowsPath(r"C:\Program Files\FFmpeg\ffprobe.exe"))
    source = Path(PureWindowsPath(r"D:\คลิป เกม\recording one.mp4"))

    command = build_ffprobe_command(tool, source)

    assert command[-1] == str(source)
    assert command[0] == str(tool)
    assert "-show_streams" in command
    assert all(isinstance(argument, str) for argument in command)


def test_parse_complete_probe(sample_probe: dict[str, object], tmp_path: Path) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")

    asset = parse_source_asset(
        sample_probe,
        source_path=source,
        source_sha256=SHA,
        size_bytes=5,
        mtime_ns=source.stat().st_mtime_ns,
        probe_version="ffprobe version test",
        created_at=datetime(2026, 8, 11, tzinfo=UTC),
    )

    assert asset.source_id == f"src_{SHA[:16]}"
    assert asset.duration_ms == 2002
    assert asset.video_stream.average_frame_rate is not None
    assert asset.video_stream.average_frame_rate.text == "30000/1001"
    assert asset.selected_audio_stream == 1
    assert asset.audio_streams[0].sample_rate_hz == 48_000
    assert any("variable-frame-rate" in warning for warning in asset.warnings)


def test_missing_optional_metadata_produces_warnings(
    sample_probe: dict[str, object], tmp_path: Path
) -> None:
    raw = json.loads(json.dumps(sample_probe))
    raw["streams"] = [raw["streams"][0]]
    del raw["streams"][0]["avg_frame_rate"]
    del raw["streams"][0]["time_base"]
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")

    asset = parse_source_asset(
        raw,
        source_path=source,
        source_sha256=SHA,
        size_bytes=5,
        mtime_ns=source.stat().st_mtime_ns,
        probe_version="ffprobe version test",
    )

    assert asset.audio_streams == []
    assert "No audio stream found." in asset.warnings
    assert "Average video frame rate is missing or invalid." in asset.warnings


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"streams": [], "format": {}},
        {"streams": [{"codec_type": "audio"}], "format": {"duration": "1"}},
    ],
)
def test_invalid_probe_output_is_rejected(raw: dict[str, object], tmp_path: Path) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")

    with pytest.raises(ValidationError):
        parse_source_asset(
            raw,
            source_path=source,
            source_sha256=SHA,
            size_bytes=5,
            mtime_ns=source.stat().st_mtime_ns,
            probe_version="test",
        )
