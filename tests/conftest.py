from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def sample_probe() -> dict[str, object]:
    return {
        "streams": [
            {
                "index": 0,
                "codec_name": "h264",
                "codec_type": "video",
                "width": 320,
                "height": 240,
                "pix_fmt": "yuv420p",
                "r_frame_rate": "30/1",
                "avg_frame_rate": "30000/1001",
                "time_base": "1/90000",
                "start_time": "0.000000",
                "duration": "2.002000",
                "disposition": {"attached_pic": 0},
            },
            {
                "index": 1,
                "codec_name": "aac",
                "codec_type": "audio",
                "sample_rate": "48000",
                "channels": 2,
                "time_base": "1/48000",
                "start_time": "0.000000",
                "duration": "2.000000",
                "tags": {"language": "eng"},
            },
        ],
        "format": {
            "filename": "fixture.mp4",
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "start_time": "0.000000",
            "duration": "2.002000",
            "size": "12345",
        },
    }


@pytest.fixture
def ffmpeg_path() -> Path:
    located = shutil.which("ffmpeg")
    if not located:
        pytest.skip("FFmpeg is not installed")
    return Path(located).resolve()


@pytest.fixture
def ffprobe_path() -> Path:
    located = shutil.which("ffprobe")
    if not located:
        pytest.skip("ffprobe is not installed")
    return Path(located).resolve()


@pytest.fixture
def tiny_video(tmp_path: Path, ffmpeg_path: Path) -> Path:
    source_dir = tmp_path / "recordings with spaces" / "เกม"
    source_dir.mkdir(parents=True)
    video = source_dir / "ตัวอย่าง gameplay.mp4"
    command = [
        str(ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=320x240:rate=10",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=1000:sample_rate=48000",
        "-t",
        "2",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(video),
    ]
    subprocess.run(command, check=True, capture_output=True, shell=False)
    return video
