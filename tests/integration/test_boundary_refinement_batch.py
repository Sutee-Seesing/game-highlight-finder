from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from game_highlight_finder import __version__
from game_highlight_finder.config import (
    AppConfig,
    ExtractionConfig,
    MediaConfig,
    ProxyConfig,
    StorageConfig,
    ToolsConfig,
)
from game_highlight_finder.domain.models import Candidate, SessionMap, model_json
from game_highlight_finder.pipeline.boundary_refinement_batch import (
    run_fake_boundary_refinement_batch,
)
from game_highlight_finder.pipeline.boundary_refiner import FakeBoundaryRefiner
from game_highlight_finder.pipeline.ingest import ingest_source
from game_highlight_finder.pipeline.proxy import generate_proxy
from game_highlight_finder.storage.atomic import read_json
from game_highlight_finder.storage.hashing import hash_file

pytestmark = pytest.mark.integration


def test_fake_boundary_refinement_batch_is_bounded_persisted_and_cacheable(
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    tmp_path: Path,
) -> None:
    config = AppConfig(
        storage=StorageConfig(data_dir=tmp_path / "library"),
        tools=ToolsConfig(ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path),
        media=MediaConfig(
            proxy=ProxyConfig(video_codec="libx264", preset="veryfast"),
            extraction=ExtractionConfig(video_codec="libx264", preset="veryfast"),
        ),
    )
    source_before = hash_file(tiny_video, source=True)
    ingest = ingest_source(tiny_video, config)
    proxy = generate_proxy(ingest.source, config)
    first_candidate = Candidate(
        candidate_id="cand_1111111111111111",
        category="SKILL",
        event_start_ms=500,
        event_end_ms=1_000,
        score=8.0,
        confidence=0.9,
        reason="first synthetic Scout candidate",
    )
    second_candidate = Candidate(
        candidate_id="cand_2222222222222222",
        category="FUNNY",
        event_start_ms=1_200,
        event_end_ms=1_700,
        score=7.0,
        confidence=0.8,
        reason="second synthetic Scout candidate",
    )
    session_map = SessionMap(
        created_at=datetime.now(UTC),
        producer_version=__version__,
        canonicalization_version="batch-test-v1",
        session_id=ingest.session_id,
        source_id=ingest.source.source_id,
        duration_ms=ingest.source.duration_ms,
        candidates=[first_candidate, second_candidate],
        scout_backend="fake",
    )
    first_fake = FakeBoundaryRefiner(
        {
            "status": "REFINED",
            "event_start_ms": 800,
            "event_end_ms": 2_200,
            "confidence": 0.9,
            "reason": "same event with a wider local boundary",
        }
    )
    second_fake = FakeBoundaryRefiner(
        {
            "status": "UNCERTAIN",
            "event_start_ms": 2_200,
            "event_end_ms": 3_600,
            "confidence": 0.3,
            "reason": "synthetic boundary remains uncertain",
        }
    )

    first = run_fake_boundary_refinement_batch(
        ingest.source,
        proxy,
        session_map,
        config,
        candidate_ids=(first_candidate.candidate_id, second_candidate.candidate_id),
        fake_refiners={
            first_candidate.candidate_id: first_fake,
            second_candidate.candidate_id: second_fake,
        },
    )

    assert first.generated_responses == 2
    assert first.response_cache_hits == 0
    assert len(first_fake.calls) == 1
    assert len(second_fake.calls) == 1
    assert first.artifact.backend == "fake"
    assert first.artifact.selected_candidate_ids == (
        first_candidate.candidate_id,
        second_candidate.candidate_id,
    )
    items = {item.candidate_id: item for item in first.artifact.items}
    assert items[first_candidate.candidate_id].boundary_changed is True
    assert items[second_candidate.candidate_id].boundary_changed is False
    refined = {candidate.candidate_id: candidate for candidate in first.session_map.candidates}
    assert (
        refined[first_candidate.candidate_id].event_start_ms,
        refined[first_candidate.candidate_id].event_end_ms,
    ) == (400, 1_100)
    assert (
        refined[second_candidate.candidate_id].event_start_ms,
        refined[second_candidate.candidate_id].event_end_ms,
    ) == (1_200, 1_700)
    assert all(candidate.clip_start_ms is not None for candidate in first.session_map.candidates)
    assert all(candidate.clip_end_ms is not None for candidate in first.session_map.candidates)
    assert first.session_map.scout_metadata["boundary_refinement_backend"] == "fake"
    assert read_json(first.artifact_path) == model_json(first.artifact)
    assert read_json(first.refined_session_map_path) == model_json(first.session_map)
    assert session_map.scout_metadata == {}
    assert all(candidate.clip_start_ms is None for candidate in session_map.candidates)
    assert hash_file(tiny_video, source=True) == source_before

    second = run_fake_boundary_refinement_batch(
        ingest.source,
        proxy,
        session_map,
        config,
        candidate_ids=(first_candidate.candidate_id, second_candidate.candidate_id),
    )

    assert second.generated_responses == 0
    assert second.media_cache_hits == 2
    assert second.response_cache_hits == 2
    assert second.session_map == first.session_map
    assert second.artifact == first.artifact
