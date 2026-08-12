from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from game_highlight_finder.config import (
    AppConfig,
    MediaConfig,
    ProxyConfig,
    StorageConfig,
    ToolsConfig,
)
from game_highlight_finder.domain.models import StageStatus
from game_highlight_finder.pipeline.ingest import ingest_source
from game_highlight_finder.pipeline.local_signals import generate_local_signals
from game_highlight_finder.pipeline.proxy import generate_proxy
from game_highlight_finder.status import get_session_status
from game_highlight_finder.storage.hashing import hash_file
from game_highlight_finder.storage.sessions import load_manifest, session_paths, write_manifest

pytestmark = pytest.mark.integration


def _config(data_dir: Path, ffmpeg: Path, ffprobe: Path) -> AppConfig:
    return AppConfig(
        storage=StorageConfig(data_dir=data_dir),
        tools=ToolsConfig(ffmpeg_path=ffmpeg, ffprobe_path=ffprobe),
    )


def _no_audio_video(tmp_path: Path, ffmpeg_path: Path) -> Path:
    source = tmp_path / "no audio à sample.mp4"
    subprocess.run(
        [
            str(ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=15",
            "-t",
            "1.5",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        capture_output=True,
        shell=False,
    )
    return source


def _vfr_video(tmp_path: Path, ffmpeg_path: Path) -> Path:
    source = tmp_path / "vfr gameplay.mp4"
    subprocess.run(
        [
            str(ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=640x360:rate=30",
            "-vf",
            "select=if(eq(n\\,0)\\,1\\,not(mod(n\\,2))),setpts=N/(30*TB)",
            "-fps_mode",
            "vfr",
            "-t",
            "1.5",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        capture_output=True,
        shell=False,
    )
    return source


def test_m2_end_to_end_cache_and_source_immutability(
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "library", ffmpeg_path, ffprobe_path)
    before = hash_file(tiny_video, source=True)
    ingest = ingest_source(tiny_video, config)
    proxy = generate_proxy(ingest.source, config)
    signals = generate_local_signals(ingest.source, proxy, config)
    assert proxy.proxy_path.is_file()
    assert proxy.metadata.width <= 854
    assert proxy.metadata.height <= 480
    assert proxy.metadata.timestamp_mapping.proxy_to_source_ms(0) == 0
    assert signals.signals.audio_present is True
    assert signals.signals.audio_activity
    assert hash_file(tiny_video, source=True) == before

    proxy_hit = generate_proxy(ingest.source, config)
    signal_hit = generate_local_signals(ingest.source, proxy_hit, config)
    assert proxy_hit.cache_hit is True
    assert signal_hit.cache_hit is True
    status = get_session_status(ingest.session_id, config)
    assert status.stages["ingest"] is StageStatus.COMPLETED
    assert status.stages["proxy"] is StageStatus.COMPLETED
    assert status.stages["local_signals"] is StageStatus.COMPLETED


def test_proxy_settings_invalidate_proxy_and_dependent_signals_only(
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "library"
    config = _config(data_dir, ffmpeg_path, ffprobe_path)
    ingest = ingest_source(tiny_video, config)
    proxy = generate_proxy(ingest.source, config)
    generate_local_signals(ingest.source, proxy, config)
    changed = config.model_copy(
        update={
            "media": MediaConfig(
                proxy=ProxyConfig(video_bitrate_kbps=900), audio=config.media.audio
            )
        }
    )
    regenerated = generate_proxy(ingest.source, changed)
    assert regenerated.cache_hit is False
    assert ingest.session_id == regenerated.session_id
    manifest = load_manifest(session_paths(data_dir, ingest.session_id).manifest)
    assert manifest.stages["ingest"].status is StageStatus.COMPLETED
    assert manifest.stages["local_signals"].status is StageStatus.STALE


def test_no_audio_completes_with_warning(
    ffmpeg_path: Path,
    ffprobe_path: Path,
    tmp_path: Path,
) -> None:
    source = _no_audio_video(tmp_path, ffmpeg_path)
    config = _config(tmp_path / "library", ffmpeg_path, ffprobe_path)
    ingest = ingest_source(source, config)
    proxy = generate_proxy(ingest.source, config)
    signals = generate_local_signals(ingest.source, proxy, config)
    assert proxy.audio_path is None
    assert signals.signals.audio_present is False
    assert signals.signals.warnings
    assert not (proxy.session_dir / "audio" / "analysis_audio.m4a").exists()
    manifest = load_manifest(session_paths(config.storage.data_dir, ingest.session_id).manifest)
    assert manifest.stages["local_signals"].item_states["audio"] == "SKIPPED"


def test_vfr_source_keeps_timestamp_mapping_within_tolerance(
    ffmpeg_path: Path,
    ffprobe_path: Path,
    tmp_path: Path,
) -> None:
    source = _vfr_video(tmp_path, ffmpeg_path)
    config = _config(tmp_path / "library", ffmpeg_path, ffprobe_path)
    ingest = ingest_source(source, config)
    proxy = generate_proxy(ingest.source, config)
    mapping = proxy.metadata.timestamp_mapping
    assert mapping.source_duration_ms == ingest.source.duration_ms
    assert abs(mapping.proxy_duration_ms - mapping.source_duration_ms) <= max(
        750, int(mapping.source_duration_ms * 0.02)
    )
    assert mapping.proxy_to_source_ms(0) == mapping.source_start_ms


def test_legacy_m1_manifest_gains_m2_stages_additively(
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "library", ffmpeg_path, ffprobe_path)
    ingest = ingest_source(tiny_video, config)
    paths = session_paths(config.storage.data_dir, ingest.session_id)
    manifest = load_manifest(paths.manifest)
    manifest.stages.pop("proxy")
    manifest.stages.pop("local_signals")
    write_manifest(paths.manifest, manifest)
    proxy = generate_proxy(ingest.source, config)
    migrated = load_manifest(paths.manifest)
    assert proxy.proxy_path.is_file()
    assert migrated.stages["ingest"].status is StageStatus.COMPLETED
    assert migrated.stages["proxy"].status is StageStatus.COMPLETED


def test_interrupted_proxy_and_signal_attempts_resume(
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "library", ffmpeg_path, ffprobe_path)
    ingest = ingest_source(tiny_video, config)
    proxy = generate_proxy(ingest.source, config)
    generate_local_signals(ingest.source, proxy, config)
    paths = session_paths(config.storage.data_dir, ingest.session_id)
    manifest = load_manifest(paths.manifest)
    for name in ("proxy", "local_signals"):
        stage = manifest.stages[name]
        stage.status = StageStatus.RUNNING
        stage.completed_at = None
        stage.attempts[-1].status = StageStatus.RUNNING
        stage.attempts[-1].completed_at = None
    write_manifest(paths.manifest, manifest)
    resumed_proxy = generate_proxy(ingest.source, config)
    resumed_signals = generate_local_signals(ingest.source, resumed_proxy, config)
    assert resumed_proxy.cache_hit is False
    assert resumed_signals.cache_hit is False
    recovered = load_manifest(paths.manifest)
    assert recovered.stages["proxy"].status is StageStatus.COMPLETED
    assert recovered.stages["local_signals"].status is StageStatus.COMPLETED
    assert recovered.stages["proxy"].attempts[0].status is StageStatus.FAILED
    assert recovered.stages["local_signals"].attempts[0].status is StageStatus.FAILED
