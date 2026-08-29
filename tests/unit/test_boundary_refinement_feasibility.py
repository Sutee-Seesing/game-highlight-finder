from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from game_highlight_finder import __version__
from game_highlight_finder.benchmark.boundary_feasibility import (
    assess_boundary_refinement_feasibility,
    run_boundary_refinement_feasibility,
)
from game_highlight_finder.benchmark.boundary_feasibility_bundle import (
    BoundaryFeasibilityBundleManifest,
    pack_boundary_refinement_feasibility_bundle,
)
from game_highlight_finder.benchmark.models import (
    AnnotatedHighlight,
    BenchmarkAnnotations,
    BenchmarkCase,
    BenchmarkDataset,
    BenchmarkSplit,
    EvaluationPolicy,
    Importance,
    Modality,
)
from game_highlight_finder.cli import app
from game_highlight_finder.config import AppConfig, StorageConfig, config_payload
from game_highlight_finder.domain.models import (
    Candidate,
    Rational,
    SessionMap,
    SourceAsset,
    VideoStream,
    model_json,
)
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.hashing import hash_file
from game_highlight_finder.storage.sessions import session_paths

SESSION_ID = "2026-08-26_unknown_aaaaaaaaaaaa"
SOURCE_ID = "src_aaaaaaaaaaaaaaaa"


def _candidate(candidate_id: str, start_ms: int, end_ms: int) -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        category="SKILL",
        event_start_ms=start_ms,
        event_end_ms=end_ms,
        score=8.0,
        confidence=0.9,
        reason=f"candidate {candidate_id}",
    )


def _annotations(source_sha256: str) -> BenchmarkAnnotations:
    return BenchmarkAnnotations(
        benchmark_id="m8-real-v1",
        case_id="cal-01",
        source_sha256=source_sha256,
        source_duration_ms=80_000,
        game_profile="valorant",
        highlights=(
            AnnotatedHighlight(
                annotation_id="hl-1",
                event_start_ms=1_100,
                event_end_ms=2_100,
                importance=Importance.WORTH_REVIEW,
                modality=Modality.VISUAL,
            ),
            AnnotatedHighlight(
                annotation_id="hl-2",
                event_start_ms=10_500,
                event_end_ms=20_000,
                importance=Importance.MUST_CATCH,
                modality=Modality.VISUAL_AND_AUDIO,
            ),
            AnnotatedHighlight(
                annotation_id="hl-3",
                event_start_ms=50_000,
                event_end_ms=51_000,
                importance=Importance.MUST_CATCH,
                modality=Modality.VISUAL,
            ),
        ),
    )


def _session_map(source_sha256: str) -> SessionMap:
    del source_sha256
    return SessionMap(
        created_at=datetime(2026, 8, 26, tzinfo=UTC),
        producer_version=__version__,
        canonicalization_version="feasibility-test-v1",
        session_id=SESSION_ID,
        source_id=SOURCE_ID,
        duration_ms=80_000,
        candidates=[
            _candidate("cand_1111111111111111", 1_000, 2_000),
            _candidate("cand_2222222222222222", 10_000, 11_000),
            _candidate("cand_3333333333333333", 75_000, 76_000),
        ],
        scout_backend="gemini",
        game_profile="valorant",
    )


def test_feasibility_separates_boundary_headroom_from_detection_gaps() -> None:
    source_sha = "a" * 64
    result = assess_boundary_refinement_feasibility(
        _session_map(source_sha),
        _annotations(source_sha),
        EvaluationPolicy(),
        dataset_sha256="b" * 64,
        annotation_document_sha256="c" * 64,
    )

    assert result.strict_match_count == 1
    assert result.strict_false_positive_count == 2
    assert result.strict_false_negative_count == 2
    assert result.anchor_overlap_annotation_count == 2
    assert result.boundary_headroom_count == 1
    assert result.detection_gap_count == 1
    assert result.context_unreachable_count == 1
    assert result.must_catch_boundary_headroom_count == 1
    assert result.must_catch_detection_gap_count == 1
    assert result.diagnostic_verdict == "MUST_CATCH_DETECTION_GAP"
    assert result.scout_backend == "gemini"
    assert result.semantic_quality_applicable is True
    assert result.annotation_coverage == "exhaustive"
    assert result.precision_tuning_safe is True
    assert result.quality_interpretation_warning is None
    assert result.ground_truth_derived_candidate_ids == (
        "cand_1111111111111111",
        "cand_2222222222222222",
    )
    assert result.unmatched_candidate_ids == (
        "cand_2222222222222222",
        "cand_3333333333333333",
    )
    by_id = {item.annotation_id: item for item in result.annotations}
    assert by_id["hl-1"].strict_matched_candidate_id == "cand_1111111111111111"
    assert by_id["hl-2"].boundary_headroom is True
    assert by_id["hl-3"].detection_gap is True
    assert by_id["hl-3"].context_unreachable is True
    assert result.provider_calls == 0


def test_feasibility_marks_fake_scout_as_non_semantic_quality() -> None:
    source_sha = "a" * 64
    fake_map = _session_map(source_sha).model_copy(
        update={
            "scout_backend": "fake",
            "scout_metadata": {
                "model": "fake",
                "window_prompt_version": "gemini-scout-window-v18",
                "scout_provenance_source": "reconciled_current_config",
            },
        }
    )
    result = assess_boundary_refinement_feasibility(
        fake_map,
        _annotations(source_sha),
        EvaluationPolicy(),
        dataset_sha256="b" * 64,
        annotation_document_sha256="c" * 64,
    )

    assert result.scout_backend == "fake"
    assert result.scout_model == "fake"
    assert result.scout_prompt_version == "gemini-scout-window-v18"
    assert result.scout_provenance_source == "reconciled_current_config"
    assert result.semantic_quality_applicable is False
    assert result.quality_interpretation_warning is not None
    assert "must not be interpreted as semantic Scout detection quality" in (
        result.quality_interpretation_warning
    )


def _write_private_case(
    tmp_path: Path,
    *,
    split: BenchmarkSplit,
    sparse_annotations: bool = False,
) -> tuple[Path, Path, AppConfig]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    data_dir = tmp_path / "library"
    source_path = tmp_path / "calibration-source.mp4"
    source_path.write_bytes(b"private calibration source")
    source_sha = hash_file(source_path, source=True)
    annotations = _annotations(source_sha)
    annotation_path = tmp_path / "cal-01.annotations.json"
    atomic_write_json(annotation_path, annotations.model_dump(mode="json"))
    dataset = BenchmarkDataset(
        benchmark_id="m8-real-v1",
        name="private calibration fixture",
        cases=(
            BenchmarkCase(
                case_id="cal-01",
                source_path=source_path,
                expected_source_sha256=source_sha,
                annotation_path=annotation_path,
                game_profile="valorant",
                split=split,
                tags=(("sparse-annotations",) if sparse_annotations else ()),
            ),
        ),
    )
    dataset_path = tmp_path / "dataset.json"
    atomic_write_json(dataset_path, dataset.model_dump(mode="json"))

    config = AppConfig(storage=StorageConfig(data_dir=data_dir))
    if split is BenchmarkSplit.CALIBRATION:
        stat = source_path.stat()
        source = SourceAsset(
            created_at=datetime(2026, 8, 26, tzinfo=UTC),
            producer_version=__version__,
            source_id=SOURCE_ID,
            path=source_path.resolve(),
            sha256=source_sha,
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            duration_ms=80_000,
            container="mp4",
            video_stream=VideoStream(
                index=0,
                codec_name="h264",
                width=1920,
                height=1080,
                average_frame_rate=Rational(numerator=60, denominator=1),
            ),
            selected_video_stream=0,
            probe_version="unit-test",
        )
        paths = session_paths(data_dir, SESSION_ID)
        paths.root.mkdir(parents=True)
        atomic_write_json(paths.source, model_json(source))
        atomic_write_json(paths.session_map, model_json(_session_map(source_sha)))
        atomic_write_json(
            paths.config,
            {
                "schema_version": 1,
                "config": config_payload(config, redacted=True),
            },
        )
    return dataset_path, annotation_path, config


def test_feasibility_runner_persists_private_calibration_artifact(tmp_path: Path) -> None:
    dataset_path, annotation_path, config = _write_private_case(
        tmp_path,
        split=BenchmarkSplit.CALIBRATION,
    )

    result, output = run_boundary_refinement_feasibility(
        SESSION_ID,
        dataset_path,
        annotation_path,
        config,
    )

    assert output.is_file()
    assert (
        output
        == (
            config.storage.data_dir
            / "benchmarks"
            / "private"
            / "boundary_refinement"
            / "cal-01.feasibility.json"
        ).resolve()
    )
    assert read_json(output) == result.model_dump(mode="json")
    assert result.split == "calibration"
    assert result.provider_calls == 0
    assert "production candidate-selection" in result.selection_warning


def test_feasibility_marks_sparse_calibration_precision_as_not_tuning_safe(
    tmp_path: Path,
) -> None:
    dataset_path, annotation_path, config = _write_private_case(
        tmp_path,
        split=BenchmarkSplit.CALIBRATION,
        sparse_annotations=True,
    )

    result, _ = run_boundary_refinement_feasibility(
        SESSION_ID,
        dataset_path,
        annotation_path,
        config,
    )

    assert result.annotation_coverage == "sparse"
    assert result.precision_tuning_safe is False
    assert result.strict_precision == pytest.approx(1 / 3)
    assert result.unmatched_candidate_ids == (
        "cand_2222222222222222",
        "cand_3333333333333333",
    )
    assert result.quality_interpretation_warning is not None
    assert "not confirmed false positives" in result.quality_interpretation_warning
    assert "must not drive Scout suppression" in result.quality_interpretation_warning


def test_feasibility_cli_is_provider_free_without_persisted_session_config(
    tmp_path: Path,
) -> None:
    dataset_path, annotation_path, config = _write_private_case(
        tmp_path,
        split=BenchmarkSplit.CALIBRATION,
    )
    paths = session_paths(config.storage.data_dir, SESSION_ID)
    paths.config.unlink()
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(config.storage.data_dir),
            "benchmark",
            "boundary-feasibility",
            SESSION_ID,
            "--dataset",
            str(dataset_path),
            "--annotations",
            str(annotation_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "provider/API calls: ZERO" in result.output
    assert "MUST_CATCH_DETECTION_GAP" in result.output
    assert "Scout provenance: backend=gemini" in result.output
    assert "NOT APPLICABLE" not in result.output
    assert "never production selection" in result.output
    output = (
        config.storage.data_dir
        / "benchmarks"
        / "private"
        / "boundary_refinement"
        / "cal-01.feasibility.json"
    )
    assert output.is_file()


def test_feasibility_runner_rejects_validation_before_session_access(tmp_path: Path) -> None:
    dataset_path, annotation_path, config = _write_private_case(
        tmp_path,
        split=BenchmarkSplit.VALIDATION,
    )

    with pytest.raises(ValidationError, match="calibration-only"):
        run_boundary_refinement_feasibility(
            SESSION_ID,
            dataset_path,
            annotation_path,
            config,
        )


def test_feasibility_bundle_is_media_free_portable_and_rerunnable(tmp_path: Path) -> None:
    dataset_path, annotation_path, config = _write_private_case(
        tmp_path / "source-machine",
        split=BenchmarkSplit.CALIBRATION,
    )
    bundle_root = tmp_path / "transfer" / "cal-01"
    source_sha = read_json(annotation_path)["source_sha256"]
    paths = session_paths(config.storage.data_dir, SESSION_ID)
    for ordinal, cache_key in enumerate(("1" * 64, "2" * 64)):
        item_dir = paths.scout_windows_dir / f"historical-window-{ordinal}"
        item_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            item_dir / "request_meta.json",
            {
                "cache_key": cache_key,
                "request": {
                    "source_sha256": source_sha,
                    "window_id": f"historical-window-{ordinal}",
                    "model": "gemini-3.5-flash-lite",
                    "prompt_version": "gemini-scout-window-v6",
                    "media_resolution": "high",
                },
            },
        )
        atomic_write_json(item_dir / "cost.json", {"historical_test": True})

    packed = pack_boundary_refinement_feasibility_bundle(
        SESSION_ID,
        dataset_path,
        annotation_path,
        config,
        output_dir=bundle_root,
    )

    assert packed.root == bundle_root.resolve()
    manifest = BoundaryFeasibilityBundleManifest.model_validate(read_json(packed.manifest_path))
    assert manifest.provider_calls == 0
    assert manifest.media_files_included == 0
    assert manifest.calibration_only is True
    assert manifest.validation_data_included is False
    assert manifest.source_path_sanitized is True
    assert manifest.diagnostic_verdict == "MUST_CATCH_DETECTION_GAP"
    assert manifest.scout_backend == "gemini"
    assert manifest.scout_model == "gemini-3.5-flash-lite"
    assert manifest.scout_prompt_version == "gemini-scout-window-v6"
    assert manifest.scout_provenance_source == "window_request_meta"
    assert manifest.scout_identity_fingerprint is not None
    assert len(manifest.scout_identity_fingerprint) == 64
    assert not any(
        path.suffix.lower() in {".mp4", ".mkv", ".mov", ".avi", ".webm"}
        for path in bundle_root.rglob("*")
        if path.is_file()
    )

    bundled_text = "\n".join(
        path.read_text(encoding="utf-8") for path in bundle_root.rglob("*.json")
    )
    assert str(tmp_path.resolve()) not in bundled_text
    bundled_annotation = read_json(bundle_root / "annotations" / "cal-01.json")
    assert bundled_annotation["source_path"] is None
    bundled_source = read_json(bundle_root / "data" / "sessions" / SESSION_ID / "source.json")
    assert Path(bundled_source["path"]).is_absolute()
    assert "__game_highlight_finder_private_source_not_bundled__" in bundled_source["path"]
    assert "calibration-source.mp4" not in bundled_source["path"]
    bundled_dataset = BenchmarkDataset.model_validate(read_json(bundle_root / "dataset.json"))
    assert len(bundled_dataset.cases) == 1
    assert bundled_dataset.cases[0].split is BenchmarkSplit.CALIBRATION
    assert bundled_dataset.cases[0].result_path is None
    bundled_session_map = SessionMap.model_validate(
        read_json(bundle_root / "data" / "sessions" / SESSION_ID / "session_map.json")
    )
    assert bundled_session_map.scout_backend == "gemini"
    assert bundled_session_map.scout_metadata["window_prompt_version"] == "gemini-scout-window-v6"
    assert bundled_session_map.scout_metadata["scout_provenance_source"] == "window_request_meta"
    assert not list(bundle_root.rglob("request_meta.json"))
    assert not list(bundle_root.rglob("cost.json"))

    replay_config = AppConfig(storage=StorageConfig(data_dir=bundle_root / "data"))
    replay, replay_path = run_boundary_refinement_feasibility(
        SESSION_ID,
        bundle_root / "dataset.json",
        bundle_root / "annotations" / "cal-01.json",
        replay_config,
        output_path=bundle_root / "replayed.feasibility.json",
    )
    assert replay == packed.feasibility
    assert replay_path.is_file()


def test_feasibility_bundle_rejects_validation_without_output(tmp_path: Path) -> None:
    dataset_path, annotation_path, config = _write_private_case(
        tmp_path / "source-machine",
        split=BenchmarkSplit.VALIDATION,
    )
    output_dir = tmp_path / "transfer" / "blocked"

    with pytest.raises(ValidationError, match="calibration-only"):
        pack_boundary_refinement_feasibility_bundle(
            SESSION_ID,
            dataset_path,
            annotation_path,
            config,
            output_dir=output_dir,
        )
    assert not output_dir.exists()


def test_pack_feasibility_cli_creates_portable_json_only_bundle(tmp_path: Path) -> None:
    dataset_path, annotation_path, config = _write_private_case(
        tmp_path / "source-machine",
        split=BenchmarkSplit.CALIBRATION,
    )
    bundle_root = tmp_path / "transfer" / "cli-bundle"
    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(config.storage.data_dir),
            "benchmark",
            "pack-boundary-feasibility",
            SESSION_ID,
            "--dataset",
            str(dataset_path),
            "--annotations",
            str(annotation_path),
            "--output-dir",
            str(bundle_root),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "provider/API calls: ZERO" in result.output
    assert "media files: ZERO" in result.output
    assert "Validation/holdout included: NO" in result.output
    assert "Portable rerun:" in result.output
    assert (bundle_root / "bundle.json").is_file()
    assert not (bundle_root / "data" / "sessions" / SESSION_ID / "config.resolved.json").exists()
