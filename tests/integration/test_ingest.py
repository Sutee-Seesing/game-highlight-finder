from __future__ import annotations

from pathlib import Path

import pytest

from game_highlight_finder.config import AppConfig, StorageConfig, ToolsConfig
from game_highlight_finder.domain.models import StageStatus
from game_highlight_finder.pipeline.ingest import ingest_source
from game_highlight_finder.status import get_session_status
from game_highlight_finder.storage.hashing import hash_file
from game_highlight_finder.storage.sessions import load_manifest, session_paths, write_manifest

pytestmark = pytest.mark.integration


def _config(data_dir: Path, ffmpeg: Path, ffprobe: Path) -> AppConfig:
    return AppConfig(
        storage=StorageConfig(data_dir=data_dir),
        tools=ToolsConfig(ffmpeg_path=ffmpeg, ffprobe_path=ffprobe),
    )


def test_ingest_cache_and_source_safety(
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "library"
    config = _config(data_dir, ffmpeg_path, ffprobe_path)
    before_hash = hash_file(tiny_video, source=True)

    first = ingest_source(tiny_video, config)
    after_first_hash = hash_file(tiny_video, source=True)
    assert first.cache_hit is False
    assert after_first_hash == before_hash
    assert first.source.sha256 == before_hash
    assert first.source.duration_ms == 2000
    assert set(path.name for path in first.artifact_paths) == {
        "source.json",
        "config.resolved.json",
        "environment.json",
        "manifest.json",
    }
    assert not (first.session_dir / "proxy").exists()
    assert not any(path.suffix == ".mp4" for path in first.session_dir.rglob("*"))

    def fail_if_probed(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("cache hit must not invoke ffprobe")

    monkeypatch.setattr("game_highlight_finder.pipeline.ingest.run_ffprobe", fail_if_probed)
    second = ingest_source(tiny_video, config)

    assert second.cache_hit is True
    assert second.session_id == first.session_id
    assert hash_file(tiny_video, source=True) == before_hash
    assert len(list((data_dir / "sessions").iterdir())) == 1

    status = get_session_status(first.session_id, config)
    assert status.cache_state == "VALID"
    assert status.ingest_status == "COMPLETED"


def test_content_modification_invalidates_cache_and_creates_new_identity(
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "library", ffmpeg_path, ffprobe_path)
    first = ingest_source(tiny_video, config)

    with tiny_video.open("ab") as handle:
        handle.write(b"changed")

    second = ingest_source(tiny_video, config)

    assert second.cache_hit is False
    assert second.source.sha256 != first.source.sha256
    assert second.session_id != first.session_id


def test_interrupted_running_ingest_is_recovered_and_retried(
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "library", ffmpeg_path, ffprobe_path)
    first = ingest_source(tiny_video, config)
    paths = session_paths(config.storage.data_dir, first.session_id)
    manifest = load_manifest(paths.manifest)
    stage = manifest.stages["ingest"]
    stage.status = StageStatus.RUNNING
    stage.completed_at = None
    stage.attempts[-1].status = StageStatus.RUNNING
    stage.attempts[-1].completed_at = None
    write_manifest(paths.manifest, manifest)

    resumed = ingest_source(tiny_video, config)
    recovered_manifest = load_manifest(paths.manifest)

    assert resumed.cache_hit is False
    assert recovered_manifest.stages["ingest"].status is StageStatus.COMPLETED
    assert len(recovered_manifest.stages["ingest"].attempts) == 2
    assert recovered_manifest.stages["ingest"].attempts[0].status is StageStatus.FAILED
