from __future__ import annotations

from pathlib import Path

import pytest

from game_highlight_finder.config import (
    AppConfig,
    MediaConfig,
    ProxyConfig,
    StorageConfig,
    ToolsConfig,
)
from game_highlight_finder.domain.models import Candidate, model_json
from game_highlight_finder.media.tools import H264EncoderChoice
from game_highlight_finder.pipeline.boundary_refinement import prepare_boundary_refinement_media
from game_highlight_finder.pipeline.ingest import ingest_source
from game_highlight_finder.pipeline.proxy import generate_proxy
from game_highlight_finder.storage.atomic import read_json
from game_highlight_finder.storage.hashing import hash_file

pytestmark = pytest.mark.integration


def test_boundary_refinement_media_is_local_validated_and_cached(
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    tmp_path: Path,
) -> None:
    config = AppConfig(
        storage=StorageConfig(data_dir=tmp_path / "library"),
        tools=ToolsConfig(ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path),
        media=MediaConfig(proxy=ProxyConfig(video_codec="libx264", preset="veryfast")),
    )
    source_before = hash_file(tiny_video, source=True)
    ingest = ingest_source(tiny_video, config)
    proxy = generate_proxy(ingest.source, config)
    candidate = Candidate(
        candidate_id="cand_0123456789abcdef",
        category="SKILL",
        event_start_ms=500,
        event_end_ms=1_200,
        score=7.0,
        confidence=0.9,
        reason="synthetic candidate for local boundary-media preparation",
    )
    cpu_encoder = H264EncoderChoice(
        encoder="libx264",
        preset="veryfast",
        hardware_accelerated=False,
    )

    first = prepare_boundary_refinement_media(
        ingest.source,
        proxy,
        candidate,
        config,
        pre_context_ms=250,
        post_context_ms=300,
        encoder=cpu_encoder,
    )

    assert first.cache_hit is False
    assert first.context_path.is_file()
    assert first.slowed_proxy_path.is_file()
    assert first.artifact_path.is_file()
    assert first.artifact.encoder == "libx264"
    assert first.artifact.hardware_accelerated is False
    assert first.artifact.audio_present is True
    assert first.artifact.plan.source_start_ms == 250
    assert first.artifact.plan.source_end_ms == 1_500
    assert first.artifact.context_duration_ms > 0
    assert first.artifact.slowed_proxy_duration_ms > first.artifact.context_duration_ms
    assert (
        abs(first.artifact.slowed_proxy_duration_ms - first.artifact.context_duration_ms * 2) <= 250
    )
    assert read_json(first.artifact_path) == model_json(first.artifact)
    assert hash_file(tiny_video, source=True) == source_before

    second = prepare_boundary_refinement_media(
        ingest.source,
        proxy,
        candidate,
        config,
        pre_context_ms=250,
        post_context_ms=300,
    )
    assert second.cache_hit is True
    assert second.artifact == first.artifact
    assert second.context_path == first.context_path
    assert second.slowed_proxy_path == first.slowed_proxy_path
