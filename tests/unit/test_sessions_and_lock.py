from __future__ import annotations

import json
import multiprocessing as mp
import os
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from game_highlight_finder.config import AppConfig, LoggingConfig, StorageConfig, ToolsConfig
from game_highlight_finder.domain.models import SourceAsset, VideoStream
from game_highlight_finder.errors import StorageError, ValidationError
from game_highlight_finder.storage.lock import SessionLock
from game_highlight_finder.storage.sessions import (
    compute_ingest_cache_key,
    ingest_config_fingerprint,
    make_session_id,
    session_paths,
    source_path_key,
)


def _cross_process_lock_holder(lock_path: str, ready: Any, release: Any) -> None:
    with SessionLock(Path(lock_path)):
        ready.set()
        release.wait(10)


def _cross_process_lock_contender(lock_path: str, result: Any) -> None:
    try:
        with SessionLock(Path(lock_path)):
            result.put("acquired")
    except StorageError:
        result.put("locked")
    except Exception as exc:  # pragma: no cover - diagnostic path for child process
        result.put(f"unexpected:{type(exc).__name__}")


def _asset(path: Path) -> SourceAsset:
    return SourceAsset(
        created_at=datetime(2026, 8, 11, tzinfo=UTC),
        producer_version="0.1.0",
        source_id="src_" + "a" * 16,
        path=path.resolve(),
        sha256="a" * 64,
        size_bytes=10,
        mtime_ns=1_786_396_800_000_000_000,
        duration_ms=1000,
        container="mp4",
        video_stream=VideoStream(index=0, codec_name="h264", width=320, height=240),
        selected_video_stream=0,
        probe_version="test",
    )


def test_deterministic_source_and_session_identity(tmp_path: Path) -> None:
    source = tmp_path / "เกม one.mp4"
    first = _asset(source)
    second = _asset(source)
    config = AppConfig(storage=StorageConfig(data_dir=tmp_path / "data"))

    assert source_path_key(source) == source_path_key(source)
    assert make_session_id(first) == make_session_id(second)
    assert make_session_id(first).endswith("_unknown_aaaaaaaaaaaa")
    assert compute_ingest_cache_key(first, config) == compute_ingest_cache_key(second, config)


def test_ingest_fingerprint_excludes_non_ingest_configuration(tmp_path: Path) -> None:
    source = _asset(tmp_path / "recording.mp4")
    base = AppConfig(storage=StorageConfig(data_dir=tmp_path / "data"))
    changed_logging = base.model_copy(update={"logging": LoggingConfig(level="DEBUG")})

    assert ingest_config_fingerprint(base) == ingest_config_fingerprint(changed_logging)
    assert compute_ingest_cache_key(source, base) == compute_ingest_cache_key(
        source, changed_logging
    )


def test_ingest_fingerprint_changes_for_configured_probe_executable(tmp_path: Path) -> None:
    source = _asset(tmp_path / "recording.mp4")
    base = AppConfig(storage=StorageConfig(data_dir=tmp_path / "data"))
    changed_probe = base.model_copy(
        update={"tools": ToolsConfig(ffprobe_path=tmp_path / "alternate-ffprobe.exe")}
    )

    assert ingest_config_fingerprint(base) != ingest_config_fingerprint(changed_probe)
    assert compute_ingest_cache_key(source, base) != compute_ingest_cache_key(source, changed_probe)


def test_session_id_rejects_path_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        session_paths(tmp_path, "../escape")


def test_second_lock_is_rejected(tmp_path: Path) -> None:
    lock_path = tmp_path / "session.lock"
    with (
        SessionLock(lock_path),
        pytest.raises(StorageError, match="locked by another process"),
    ):
        SessionLock(lock_path).acquire()


def test_dead_same_host_lock_is_recovered(tmp_path: Path) -> None:
    lock_path = tmp_path / "session.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": 2_147_483_647,
                "hostname": socket.gethostname(),
                "created_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    with SessionLock(lock_path):
        owner = json.loads(lock_path.read_text(encoding="utf-8"))
        assert owner["pid"] == os.getpid()

    assert not lock_path.exists()


def test_cross_process_lock_rejects_live_owner(tmp_path: Path) -> None:
    lock_path = tmp_path / "session.lock"
    context = mp.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    result = context.Queue()
    holder = context.Process(
        target=_cross_process_lock_holder,
        args=(str(lock_path), ready, release),
    )
    contender: Any = None
    holder.start()
    try:
        assert ready.wait(5), "lock holder did not become ready in time"
        contender = context.Process(
            target=_cross_process_lock_contender,
            args=(str(lock_path), result),
        )
        contender.start()
        assert result.get(timeout=5) == "locked"
    finally:
        if contender is not None:
            contender.join(5)
            if contender.is_alive():
                contender.terminate()
                contender.join(5)
        release.set()
        holder.join(5)
        if holder.is_alive():
            holder.terminate()
            holder.join(5)

    assert not holder.is_alive()
    assert not lock_path.exists()
