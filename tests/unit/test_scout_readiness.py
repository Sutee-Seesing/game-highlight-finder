from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from game_highlight_finder.benchmark.models import (
    AnnotatedHighlight,
    BenchmarkAnnotations,
    BenchmarkCase,
    BenchmarkDataset,
    BenchmarkSplit,
    Importance,
    Modality,
)
from game_highlight_finder.benchmark.scout_readiness import run_scout_calibration_readiness
from game_highlight_finder.config import AppConfig, ScoutConfig, StorageConfig
from game_highlight_finder.domain.models import AudioStream, SourceAsset, VideoStream
from game_highlight_finder.domain.windows import plan_scout_windows
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.pipeline.windowed_scout import AggregateCostPreflight
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.hashing import hash_file
from game_highlight_finder.storage.sessions import session_paths


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fixture(tmp_path: Path, *, split: BenchmarkSplit = BenchmarkSplit.CALIBRATION) -> tuple[
    AppConfig, str, Path, Path, str
]:
    benchmark_root = tmp_path / "benchmark"
    annotations_dir = benchmark_root / "annotations"
    annotations_dir.mkdir(parents=True)
    source_path = benchmark_root / "source.webm"
    source_path.write_bytes(b"provider-free calibration source")
    source_sha = hash_file(source_path, source=True)
    session_id = f"2026-08-28_unknown_{source_sha[:12]}"
    case_id = "cal-case-01"
    annotation_path = annotations_dir / "case.json"
    annotations = BenchmarkAnnotations(
        benchmark_id="calibration-benchmark",
        case_id=case_id,
        source_sha256=source_sha,
        source_duration_ms=10_000,
        game_profile="arena_fps",
        annotated_by="human",
        highlights=(
            AnnotatedHighlight(
                annotation_id="highlight-01",
                event_start_ms=2_000,
                event_end_ms=3_000,
                importance=Importance.WORTH_REVIEW,
                modality=Modality.VISUAL,
            ),
        ),
        source_path=source_path.resolve(),
    )
    atomic_write_json(annotation_path, annotations.model_dump(mode="json"))
    dataset = BenchmarkDataset(
        benchmark_id="calibration-benchmark",
        name="Calibration",
        cases=(
            BenchmarkCase(
                case_id=case_id,
                source_path=Path("source.webm"),
                expected_source_sha256=source_sha,
                annotation_path=Path("annotations/case.json"),
                game_profile="arena_fps",
                split=split,
            ),
        ),
    )
    dataset_path = benchmark_root / "dataset.json"
    atomic_write_json(dataset_path, dataset.model_dump(mode="json"))

    data_dir = tmp_path / "data"
    config = AppConfig(
        storage=StorageConfig(data_dir=data_dir),
        scout=ScoutConfig(
            backend="gemini",
            model="gemini-3.5-flash-lite",
            window_duration_seconds=300,
            window_overlap_seconds=30,
            max_windows=1,
            max_output_tokens=3_720,
            window_prompt_version="gemini-scout-window-v18",
        ),
    )
    paths = session_paths(data_dir, session_id)
    paths.root.mkdir(parents=True)
    source = SourceAsset(
        created_at=datetime.now(UTC),
        producer_version="test",
        source_id=f"src_{source_sha[:16]}",
        path=source_path.resolve(),
        sha256=source_sha,
        size_bytes=source_path.stat().st_size,
        mtime_ns=source_path.stat().st_mtime_ns,
        duration_ms=10_000,
        container="matroska,webm",
        video_stream=VideoStream(index=0, codec_name="vp9", width=960, height=540),
        audio_streams=(AudioStream(index=1, codec_name="opus", channels=2, sample_rate_hz=48_000),),
        selected_video_stream=0,
        selected_audio_stream=1,
        probe_version="test",
    )
    atomic_write_json(paths.source, source.model_dump(mode="json"))
    paths.proxy_dir.mkdir(parents=True)
    parent_proxy = paths.proxy_dir / "analysis_proxy.mp4"
    parent_proxy.write_bytes(b"parent proxy")
    parent_sha = hash_file(parent_proxy)
    plan = plan_scout_windows(
        source.duration_ms,
        max_duration_ms=300_000,
        overlap_ms=30_000,
        session_id=session_id,
        source_id=source.source_id,
        max_windows=1,
    )
    window = plan.windows[0]
    item_dir = paths.scout_windows_dir / window.window_id
    item_dir.mkdir(parents=True)
    proxy_path = item_dir / "analysis_window.mp4"
    proxy_path.write_bytes(b"prepared window")
    signals_path = item_dir / "signals.json"
    atomic_write_json(signals_path, {"audio_activity": [], "warnings": []})
    prepared = window.model_copy(
        update={
            "proxy_path": f"scout/windows/{window.window_id}/analysis_window.mp4",
            "proxy_sha256": hash_file(proxy_path),
            "parent_proxy_sha256": parent_sha,
            "signal_summary_hash": _sha(b"semantic signal hash"),
        }
    )
    atomic_write_json(item_dir / "window.json", prepared.model_dump(mode="json"))
    return config, session_id, dataset_path, annotation_path, window.window_id


def test_scout_readiness_persists_zero_call_calibration_authorization_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, session_id, dataset_path, annotation_path, window_id = _fixture(tmp_path)

    def fake_preflight(*_args: object, **_kwargs: object) -> AggregateCostPreflight:
        return AggregateCostPreflight(
            total_windows=1,
            cached_windows=0,
            missing_windows=1,
            estimated_micro_thb=649_624,
            available_micro_thb=650_000,
            blocked=False,
            reason="aggregate Gemini window preflight passed",
            window_estimates_micro_thb={window_id: 649_624},
        )

    monkeypatch.setattr(
        "game_highlight_finder.benchmark.scout_readiness.aggregate_window_preflight",
        fake_preflight,
    )
    artifact, target = run_scout_calibration_readiness(
        session_id, dataset_path, annotation_path, config
    )

    assert artifact.case_id == "cal-case-01"
    assert artifact.split == "calibration"
    assert artifact.planned_provider_requests == 1
    assert artifact.aggregate_maximum_reserved_micro_thb == 649_624
    assert artifact.monthly_available_micro_thb == 650_000
    assert artifact.post_reservation_headroom_micro_thb == 376
    assert artifact.provider_calls == 0
    assert artifact.remote_uploads == 0
    assert artifact.ledger_reservations == 0
    assert artifact.fresh_attempt_authorization_required is True
    assert artifact.semantic_quality_available is False
    assert artifact.ready_for_authorized_execution is True
    assert target.is_file()
    persisted = read_json(target)
    assert persisted["provider_calls"] == 0
    assert persisted["windows"][0]["window_id"] == window_id


def test_scout_readiness_rejects_validation_case_before_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, session_id, dataset_path, annotation_path, _window_id = _fixture(
        tmp_path, split=BenchmarkSplit.VALIDATION
    )
    called = False

    def unexpected_preflight(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("validation data must be rejected before preflight")

    monkeypatch.setattr(
        "game_highlight_finder.benchmark.scout_readiness.aggregate_window_preflight",
        unexpected_preflight,
    )
    with pytest.raises(ValidationError, match="calibration cases only"):
        run_scout_calibration_readiness(
            session_id, dataset_path, annotation_path, config, case_id="cal-case-01"
        )
    assert called is False


def test_scout_readiness_rejects_existing_paid_window_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, session_id, dataset_path, annotation_path, window_id = _fixture(tmp_path)
    paid_path = session_paths(config.storage.data_dir, session_id).scout_windows_dir / window_id
    atomic_write_json(paid_path / "request_meta.json", {"paid": True})
    called = False

    def unexpected_preflight(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("provider-dirty session must fail before preflight")

    monkeypatch.setattr(
        "game_highlight_finder.benchmark.scout_readiness.aggregate_window_preflight",
        unexpected_preflight,
    )
    with pytest.raises(ValidationError, match="provider-clean session"):
        run_scout_calibration_readiness(session_id, dataset_path, annotation_path, config)
    assert called is False
