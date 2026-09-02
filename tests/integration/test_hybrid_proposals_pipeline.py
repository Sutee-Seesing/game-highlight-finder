from __future__ import annotations

from pathlib import Path

from game_highlight_finder.config import (
    AppConfig,
    ExtractionConfig,
    MediaConfig,
    ProxyConfig,
    StorageConfig,
    ToolsConfig,
)
from game_highlight_finder.pipeline.hybrid_proposals import (
    HybridProposalPolicy,
    prepare_hybrid_proposals,
)
from game_highlight_finder.pipeline.runner import analyze_m6_source


def test_hybrid_proposal_media_is_provider_free_bounded_and_reusable(
    tmp_path: Path,
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
) -> None:
    config = AppConfig(
        storage=StorageConfig(data_dir=tmp_path / "library"),
        tools=ToolsConfig(ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path),
        media=MediaConfig(
            proxy=ProxyConfig(video_codec="libx264", preset="veryfast"),
            extraction=ExtractionConfig(video_codec="libx264", preset="veryfast"),
        ),
    )
    original = tiny_video.read_bytes()
    local = analyze_m6_source(tiny_video, config, stop_after="windows")
    assert local.proxy is not None and local.local_signals is not None
    policy = HybridProposalPolicy(
        motion_sample_fps=2,
        audio_anchors_per_10min=1,
        fused_anchors_per_10min=1,
        max_anchors=4,
    )

    first = prepare_hybrid_proposals(
        local.ingest.source,
        local.proxy,
        local.local_signals,
        config,
        policy=policy,
    )

    assert first.plan.provider_calls == 0
    assert first.plan.semantic_labels_inferred is False
    assert first.plan.plan_hash is not None
    assert first.plan_path.is_file()
    assert first.motion_path.is_file()
    assert first.generated == len(first.prepared)
    assert all(item.proxy_path.is_file() for item in first.prepared)
    assert tiny_video.read_bytes() == original

    second = prepare_hybrid_proposals(
        local.ingest.source,
        local.proxy,
        local.local_signals,
        config,
        policy=policy,
    )

    assert second.plan.plan_hash == first.plan.plan_hash
    assert second.motion_cache_hit is True
    assert second.generated == 0
    assert second.cache_hits == len(second.prepared)
    assert all(item.cache_hit for item in second.prepared)
    assert tiny_video.read_bytes() == original
