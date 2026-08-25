from __future__ import annotations

import json
from pathlib import Path

import pytest

from game_highlight_finder.config import (
    AppConfig,
    ExtractionConfig,
    LoggingConfig,
    MediaConfig,
    ProxyConfig,
    ScoutConfig,
    StorageConfig,
    ToolsConfig,
)
from game_highlight_finder.domain.models import StageStatus
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.pipeline.ingest import ingest_source
from game_highlight_finder.pipeline.local_signals import generate_local_signals
from game_highlight_finder.pipeline.proxy import generate_proxy
from game_highlight_finder.pipeline.scout import generate_scout
from game_highlight_finder.status import get_session_status
from game_highlight_finder.storage.hashing import hash_file
from game_highlight_finder.storage.sessions import load_manifest, session_paths

pytestmark = pytest.mark.integration


def _config(data_dir: Path, ffmpeg: Path, ffprobe: Path) -> AppConfig:
    return AppConfig(
        storage=StorageConfig(data_dir=data_dir),
        tools=ToolsConfig(ffmpeg_path=ffmpeg, ffprobe_path=ffprobe),
        media=MediaConfig(
            proxy=ProxyConfig(video_codec="libx264", preset="veryfast"),
            extraction=ExtractionConfig(video_codec="libx264", preset="veryfast"),
        ),
    )


def test_m3_fake_scout_end_to_end_cache_and_source_immutability(
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "library", ffmpeg_path, ffprobe_path)
    source_hash = hash_file(tiny_video, source=True)
    ingest = ingest_source(tiny_video, config)
    proxy = generate_proxy(ingest.source, config)
    signals = generate_local_signals(ingest.source, proxy, config)
    first = generate_scout(ingest.source, proxy, signals, config)

    assert first.cache_hit is False
    assert first.backend == "fake"
    assert first.raw_path.is_file()
    assert first.canonical_path.is_file()
    assert first.session_map_path.is_file()
    assert first.session_map.matches
    assert first.session_map.candidates
    assert any(not match.candidate_ids for match in first.session_map.matches)
    assert hash_file(tiny_video, source=True) == source_hash

    second = generate_scout(ingest.source, proxy, signals, config)
    assert second.cache_hit is True
    assert second.session_map.model_dump(mode="json") == first.session_map.model_dump(mode="json")
    manifest = load_manifest(session_paths(config.storage.data_dir, ingest.session_id).manifest)
    assert manifest.stages["ingest"].status is StageStatus.COMPLETED
    assert manifest.stages["proxy"].status is StageStatus.COMPLETED
    assert manifest.stages["local_signals"].status is StageStatus.COMPLETED
    assert manifest.stages["scout"].status is StageStatus.COMPLETED
    assert get_session_status(ingest.session_id, config).stages["scout"] is StageStatus.COMPLETED


def test_m3_logging_change_does_not_invalidate_scout(
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "library"
    config = _config(data_dir, ffmpeg_path, ffprobe_path)
    ingest = ingest_source(tiny_video, config)
    proxy = generate_proxy(ingest.source, config)
    signals = generate_local_signals(ingest.source, proxy, config)
    generate_scout(ingest.source, proxy, signals, config)
    changed = config.model_copy(update={"logging": LoggingConfig(level="DEBUG")})
    changed_ingest = ingest_source(tiny_video, changed)
    changed_proxy = generate_proxy(changed_ingest.source, changed)
    changed_signals = generate_local_signals(changed_ingest.source, changed_proxy, changed)
    result = generate_scout(changed_ingest.source, changed_proxy, changed_signals, changed)
    assert result.cache_hit is True


def test_corrupt_canonical_artifact_is_regenerated_from_deterministic_fake_output(
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "library", ffmpeg_path, ffprobe_path)
    ingest = ingest_source(tiny_video, config)
    proxy = generate_proxy(ingest.source, config)
    signals = generate_local_signals(ingest.source, proxy, config)
    first = generate_scout(ingest.source, proxy, signals, config)
    raw_before = first.raw_path.read_bytes()
    first.canonical_path.write_text('{"corrupt": true}\n', encoding="utf-8")
    regenerated = generate_scout(ingest.source, proxy, signals, config)
    assert regenerated.cache_hit is False
    assert regenerated.raw_path.read_bytes() == raw_before
    assert regenerated.session_map.candidates


def test_malformed_fixture_fails_safely_but_retains_raw_artifact(
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "malformed-scout.json"
    fixture.write_text(json.dumps({"schema_version": 1, "unknown": True}), encoding="utf-8")
    base = _config(tmp_path / "library", ffmpeg_path, ffprobe_path)
    config = base.model_copy(update={"scout": ScoutConfig(fixture_path=fixture)})
    ingest = ingest_source(tiny_video, config)
    proxy = generate_proxy(ingest.source, config)
    signals = generate_local_signals(ingest.source, proxy, config)
    with pytest.raises(ValidationError):
        generate_scout(ingest.source, proxy, signals, config)
    paths = session_paths(config.storage.data_dir, ingest.session_id)
    assert (paths.scout_raw_dir / "fake_response.json").read_bytes() == fixture.read_bytes()
    manifest = load_manifest(paths.manifest)
    assert manifest.stages["scout"].status is StageStatus.FAILED
