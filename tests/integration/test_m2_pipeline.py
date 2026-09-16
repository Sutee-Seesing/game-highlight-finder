from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from game_highlight_finder.config import (
    AppConfig,
    AudioConfig,
    MediaConfig,
    ProxyConfig,
    StorageConfig,
    ToolsConfig,
)
from game_highlight_finder.domain.models import StageStatus
from game_highlight_finder.pipeline.ingest import ingest_source
from game_highlight_finder.pipeline.local_signals import generate_local_signals
from game_highlight_finder.pipeline.proposals import proposals_from_local_signals
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


def _multitrack_video(tmp_path: Path, ffmpeg_path: Path) -> Path:
    source = tmp_path / "multitrack obs sample.mkv"
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
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=48000",
            "-t",
            "2",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-map",
            "2:a:0",
            "-map",
            "3:a:0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
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


def test_multitrack_obs_audio_is_mixed_into_analysis_derivatives_by_default(
    ffmpeg_path: Path,
    ffprobe_path: Path,
    tmp_path: Path,
) -> None:
    source = _multitrack_video(tmp_path, ffmpeg_path)
    config = _config(tmp_path / "library", ffmpeg_path, ffprobe_path)
    ingest = ingest_source(source, config)

    assert len(ingest.source.audio_streams) == 3
    assert ingest.source.selected_audio_stream == ingest.source.audio_streams[0].index

    proxy = generate_proxy(ingest.source, config)
    signals = generate_local_signals(ingest.source, proxy, config)
    expected_indexes = sorted(stream.index for stream in ingest.source.audio_streams)

    assert proxy.metadata.audio_source_mode == "mix_all"
    assert proxy.metadata.audio_source_stream_indexes == expected_indexes
    assert not list((proxy.session_dir / "tmp").rglob("*.nut"))
    assert any("Mixed all source audio streams" in warning for warning in proxy.metadata.warnings)
    assert signals.signals.overall_loudness_lufs is not None
    assert signals.signals.overall_loudness_lufs > -60
    assert any(interval.active for interval in signals.signals.audio_activity)

    legacy = config.model_copy(
        update={
            "media": MediaConfig(
                proxy=config.media.proxy,
                audio=AudioConfig(source_mix_mode="first"),
                extraction=config.media.extraction,
            )
        }
    )
    legacy_proxy = generate_proxy(ingest.source, legacy)
    assert legacy_proxy.cache_hit is False
    assert legacy_proxy.metadata.audio_source_mode == "first"
    assert legacy_proxy.metadata.audio_source_stream_indexes == [
        ingest.source.selected_audio_stream
    ]


def test_bounded_multitrack_mix_joins_chunks_before_limiting(
    ffmpeg_path: Path,
    ffprobe_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import game_highlight_finder.pipeline.proxy as proxy_module

    monkeypatch.setattr(proxy_module, "MIX_CHUNK_TRIGGER_MS", 1000)
    monkeypatch.setattr(proxy_module, "MIX_CHUNK_DURATION_MS", 1000)
    source = _multitrack_video(tmp_path, ffmpeg_path)
    config = _config(tmp_path / "segmented-library", ffmpeg_path, ffprobe_path)
    ingest = ingest_source(source, config)
    proxy = generate_proxy(ingest.source, config)
    assert proxy.cache_hit is False
    assert proxy.audio_path is not None and proxy.audio_path.is_file()
    assert proxy.metadata.audio_source_stream_indexes == [1, 2, 3]
    assert not list((proxy.session_dir / "tmp").rglob("*.nut"))
    assert not list((proxy.session_dir / "tmp").rglob("*.ffconcat"))
    signals = generate_local_signals(ingest.source, proxy, config)
    assert signals.signals.audio_present is True
    assert signals.signals.audio_activity[-1].end_ms == ingest.source.duration_ms
    # A full-duration mixed audio file must also carry usable evidence into C1.
    proposals = proposals_from_local_signals(
        session_id=ingest.session_id,
        source_id=ingest.source.source_id,
        signals=signals.signals,
    )
    assert proposals.proposals
    assert any(proposal.signal_type.value == "AUDIO_ACTIVITY" for proposal in proposals.proposals)
    assert generate_proxy(ingest.source, config).cache_hit is True


@pytest.mark.skipif(
    os.environ.get("GHF_TEST_CUDA") != "1",
    reason="CUDA integration requires an explicitly enabled NVIDIA test machine",
)
def test_cuda_proxy_multitrack_preserves_audio_and_cache_isolation(
    ffmpeg_path: Path,
    ffprobe_path: Path,
    tmp_path: Path,
) -> None:
    source = _multitrack_video(tmp_path, ffmpeg_path)
    base = _config(tmp_path / "library", ffmpeg_path, ffprobe_path)
    ingest = ingest_source(source, base)
    cpu = generate_proxy(ingest.source, base)
    cuda_config = base.model_copy(
        update={
            "media": base.media.model_copy(
                update={"proxy": base.media.proxy.model_copy(
                    update={"video_acceleration": "cuda"}
                )}
            )
        }
    )
    cuda = generate_proxy(ingest.source, cuda_config)
    assert cuda.cache_hit is False
    assert cuda.metadata.duration_ms == cpu.metadata.duration_ms
    assert cuda.metadata.audio_source_stream_indexes == cpu.metadata.audio_source_stream_indexes
    assert cuda.metadata.audio_present is True
    assert cuda.proxy_path.is_file() and cuda.audio_path is not None
    assert cuda.audio_path.is_file()
    assert not list((cuda.session_dir / "tmp").rglob("*.nut"))
    assert generate_proxy(ingest.source, cuda_config).cache_hit is True
    assert generate_local_signals(ingest.source, cuda, cuda_config).signals.audio_present


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
