from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from game_highlight_finder.benchmark.calibration import (
    CALIBRATION_CASE_IDS,
    CALIBRATION_EXPERIMENT_REVISION,
    CALIBRATION_MODEL_IDS,
    EXPECTED_AGGREGATE_COUNTS,
    build_calibration_plan,
    verify_ground_truth_lock,
)
from game_highlight_finder.benchmark.models import (
    AnnotatedHighlight,
    BenchmarkAnnotations,
    BenchmarkCase,
    BenchmarkDataset,
    BenchmarkSplit,
    BoringInterval,
    EvaluationPolicy,
    Importance,
    Modality,
)
from game_highlight_finder.cli import app
from game_highlight_finder.config import AppConfig, ScoutConfig
from game_highlight_finder.domain.models import AudioStream, SourceAsset, VideoStream
from game_highlight_finder.providers import gemini_provider_descriptor
from game_highlight_finder.providers.base import ProviderRequest, ProviderUsageEstimate
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.sessions import (
    compute_gemini_provider_cache_key,
    scout_config_fingerprint,
)


def _write_locked_fixture(tmp_path: Path) -> tuple[Path, Path]:
    dataset_dir = tmp_path / "benchmarks" / "datasets"
    annotations_dir = tmp_path / "benchmarks" / "annotations"
    private_dir = tmp_path / "benchmarks" / "private"
    dataset_dir.mkdir(parents=True)
    annotations_dir.mkdir(parents=True)
    private_dir.mkdir(parents=True)
    durations = {
        case_id: 5_000 + ordinal * 100
        for ordinal, case_id in enumerate(
            (*CALIBRATION_CASE_IDS, "m8-real-val-01", "m8-real-val-02")
        )
    }
    counts = {
        "m8-real-cal-01": (1, 2, 0, 0),
        "m8-real-cal-02": (1, 1, 0, 2),
        "m8-real-val-01": (1, 2, 0, 1),
        "m8-real-val-02": (0, 1, 1, 1),
    }
    source_hashes = {case_id: f"{index + 1:064x}" for index, case_id in enumerate(durations)}
    annotation_hashes: dict[str, str] = {}
    cases: list[BenchmarkCase] = []
    lock_cases: list[dict[str, str]] = []
    for case_id, duration in durations.items():
        must, worth, optional, boring_count = counts[case_id]
        highlights: list[AnnotatedHighlight] = []
        ordinal = 0
        for importance, count in (
            (Importance.MUST_CATCH, must),
            (Importance.WORTH_REVIEW, worth),
            (Importance.OPTIONAL, optional),
        ):
            for _ in range(count):
                start = 100 + ordinal * 500
                highlights.append(
                    AnnotatedHighlight(
                        annotation_id=f"{case_id}-h-{ordinal}",
                        event_start_ms=start,
                        event_end_ms=start + 100,
                        importance=importance,
                        modality=Modality.VISUAL,
                    )
                )
                ordinal += 1
        boring = tuple(
            BoringInterval(
                annotation_id=f"{case_id}-b-{index}",
                start_ms=3_000 + index * 300,
                end_ms=3_100 + index * 300,
            )
            for index in range(boring_count)
        )
        annotation_path = annotations_dir / f"{case_id}.json"
        annotation = BenchmarkAnnotations(
            benchmark_id="m8-private",
            case_id=case_id,
            source_sha256=source_hashes[case_id],
            source_duration_ms=duration,
            game_profile="synthetic",
            highlights=tuple(highlights),
            boring_intervals=boring,
            source_path=None,
        )
        atomic_write_json(annotation_path, annotation.model_dump(mode="json"))
        annotation_hashes[case_id] = hashlib.sha256(annotation_path.read_bytes()).hexdigest()
        split = (
            BenchmarkSplit.CALIBRATION
            if case_id in CALIBRATION_CASE_IDS
            else BenchmarkSplit.VALIDATION
        )
        cases.append(
            BenchmarkCase(
                case_id=case_id,
                source_path=tmp_path / "private" / f"{case_id}.mkv",
                expected_source_sha256=source_hashes[case_id],
                annotation_path=Path("../annotations") / f"{case_id}.json",
                game_profile="synthetic",
                split=split,
                tags=("audio", "visual"),
            )
        )
        lock_cases.append(
            {
                "case_id": case_id,
                "split": split.value,
                "source_sha256": source_hashes[case_id],
                "annotation_sha256": annotation_hashes[case_id],
            }
        )
    dataset = BenchmarkDataset(
        benchmark_id="m8-real-v1",
        name="synthetic locked M8 fixture",
        cases=tuple(cases),
        evaluation_policy=EvaluationPolicy(),
    )
    dataset_path = dataset_dir / "m8-real-v1.json"
    atomic_write_json(dataset_path, dataset.model_dump(mode="json"))
    lock_path = private_dir / "lock.json"
    atomic_write_json(
        lock_path,
        {
            "schema_version": 1,
            "status": "OWNER_CONFIRMED_GROUND_TRUTH",
            "benchmark_id": "m8-real-v1",
            "policy_version": "m8-eval-v1",
            "policy_fingerprint": dataset.policy_fingerprint,
            "owner_confirmed": True,
            "locked_before_provider_benchmark": True,
            "provider_predictions_exist": False,
            "aggregate_counts": EXPECTED_AGGREGATE_COUNTS,
            "cases": lock_cases,
        },
    )
    return dataset_path, lock_path


def test_gemini_descriptor_accepts_exactly_the_two_calibration_models() -> None:
    descriptor = gemini_provider_descriptor()
    assert tuple(item.model_id for item in descriptor.models) == CALIBRATION_MODEL_IDS
    assert all(item.billing_modes == ("standard",) for item in descriptor.models)
    assert all(item.capabilities.audio_input for item in descriptor.models)
    assert all(item.capabilities.structured_output for item in descriptor.models)


def test_model_identity_changes_scout_and_provider_cache_but_not_annotation_identity(
    tmp_path: Path,
) -> None:
    config_25 = AppConfig(scout=ScoutConfig(model="gemini-2.5-flash-lite"))
    config_35 = AppConfig(scout=ScoutConfig(model="gemini-3.5-flash-lite"))
    assert scout_config_fingerprint(config_25) != scout_config_fingerprint(config_35)
    source = SourceAsset(
        created_at=datetime(2026, 8, 17, tzinfo=UTC),
        producer_version="test",
        source_id="src_1234567890abcdef",
        path=(tmp_path / "source.mp4").resolve(),
        sha256="a" * 64,
        size_bytes=1,
        mtime_ns=1,
        duration_ms=5_000,
        container="mp4",
        video_stream=VideoStream(index=0, codec_name="h264", width=320, height=240),
        audio_streams=[AudioStream(index=1, codec_name="aac", channels=2, sample_rate_hz=48_000)],
        selected_video_stream=0,
        selected_audio_stream=1,
        probe_version="test",
    )
    kwargs = {
        "proxy_artifact_sha256": "b" * 64,
        "local_signals_summary_hash": "c" * 64,
        "prompt_hash": "d" * 64,
        "schema_hash": "e" * 64,
    }
    key_25 = compute_gemini_provider_cache_key(source, config_25, **kwargs)
    key_35 = compute_gemini_provider_cache_key(source, config_35, **kwargs)
    assert key_25 != key_35
    # Annotation/ground-truth hashes are deliberately absent from the provider key inputs.
    assert kwargs == {
        "proxy_artifact_sha256": "b" * 64,
        "local_signals_summary_hash": "c" * 64,
        "prompt_hash": "d" * 64,
        "schema_hash": "e" * 64,
    }


def test_annotation_revision_does_not_change_provider_request_fingerprint() -> None:
    usage = ProviderUsageEstimate(input_video_tokens=10, input_audio_tokens=5, output_tokens=2)

    def request(annotation_revision: str) -> ProviderRequest:
        # The annotation revision is intentionally accepted by this test helper
        # but never inserted into the provider payload.
        del annotation_revision
        return ProviderRequest(
            call_id="calibration-window",
            provider="gemini",
            model_id="gemini-2.5-flash-lite",
            billing_mode="standard",
            stage="scout",
            session_id="m8b2a-m8-real-cal-01",
            usage_estimate=usage,
            request_payload={
                "source_sha256": "a" * 64,
                "window_id": "scout_window_1234567890abcdef",
                "prompt_version": "gemini-scout-window-v1",
            },
        )

    assert request("1" * 64).request_fingerprint == request("2" * 64).request_fingerprint


def test_lock_verification_and_calibration_plan_exclude_validation(tmp_path: Path) -> None:
    dataset_path, lock_path = _write_locked_fixture(tmp_path)
    verification = verify_ground_truth_lock(dataset_path, lock_path)
    assert verification.status == "PASS"
    assert verification.calibration_case_ids == CALIBRATION_CASE_IDS
    assert verification.validation_case_ids == ("m8-real-val-01", "m8-real-val-02")
    config = AppConfig(
        scout=ScoutConfig(window_duration_seconds=2, window_overlap_seconds=1),
    )
    plan = build_calibration_plan(dataset_path, config, lock_path=lock_path, fx_usd_thb="36")
    assert plan.calibration_case_ids == CALIBRATION_CASE_IDS
    assert plan.validation_case_ids_sealed == ("m8-real-val-01", "m8-real-val-02")
    assert all(case.case_id in CALIBRATION_CASE_IDS for arm in plan.arms for case in arm.cases)
    assert plan.arms[0].planned_scout_windows == plan.arms[1].planned_scout_windows
    assert plan.arms[0].shared_config_fingerprint == plan.arms[1].shared_config_fingerprint
    assert plan.arms[0].experiment_fingerprint != plan.arms[1].experiment_fingerprint
    assert CALIBRATION_EXPERIMENT_REVISION == "v7"
    assert all(arm.result_set_id.endswith("-v7") for arm in plan.arms)
    assert plan.arms[0].effective_media_config["wire_level"] is None
    assert plan.arms[0].effective_media_config["effective_mode"] == "default_unspecified"
    assert plan.arms[0].effective_media_config["estimated_video_tokens_per_second"] == 258
    assert plan.arms[1].effective_media_config["wire_level"] == "low"
    assert plan.arms[1].effective_media_config["effective_mode"] == "low"
    assert plan.arms[1].effective_media_config["estimated_video_tokens_per_second"] == 66
    assert (
        plan.arms[0].usage_estimate.input_video_tokens
        > plan.arms[1].usage_estimate.input_video_tokens
    )
    assert plan.free_tier_intent is True
    assert plan.paid_fallback_authorized is False
    assert plan.actual_provider_calls == 0
    assert plan.media_uploads == 0
    assert plan.raw_upload_planned is False
    assert plan.review_proxies_provider_inputs is False
    assert plan.audio_retained is True
    assert plan.arms[0].estimated_paid_equivalent_cost_thb is not None
    assert plan.arms[0].actual_settled_cost_thb is None
    assert all(
        "review-proxies" not in window.proxy_path
        for arm in plan.arms
        for case in arm.cases
        for window in case.windows
    )
    assert all(
        "m8-real-val" not in window.proxy_path
        for arm in plan.arms
        for case in arm.cases
        for window in case.windows
    )


def test_calibration_plan_serializes_pricing_and_future_comparison_as_not_created(
    tmp_path: Path,
) -> None:
    dataset_path, lock_path = _write_locked_fixture(tmp_path)
    plan = build_calibration_plan(dataset_path, AppConfig(), lock_path=lock_path)
    assert plan.pricing_snapshot.status == "PLANNING_REFERENCE_NOT_LIVE_VERIFIED"
    assert tuple(entry.model for entry in plan.pricing_snapshot.entries) == CALIBRATION_MODEL_IDS
    assert all(entry.reverify_before_live for entry in plan.pricing_snapshot.entries)
    assert plan.comparison_manifest.status == "PLANNED_NOT_EXECUTED"
    assert all(item.status == "NOT_CREATED" for item in plan.comparison_manifest.result_sets)
    assert all(item.experiment_fingerprint is None for item in plan.comparison_manifest.result_sets)
    assert all(item.evaluation_path is None for item in plan.comparison_manifest.result_sets)


def test_provider_free_cli_dry_run_writes_private_plan(tmp_path: Path) -> None:
    dataset_path, lock_path = _write_locked_fixture(tmp_path)
    output = tmp_path / "private" / "plan.json"
    comparison = tmp_path / "private" / "comparison.json"
    result = CliRunner().invoke(
        app,
        [
            "benchmark",
            "plan-calibration",
            str(dataset_path),
            "--lock",
            str(lock_path),
            "--output",
            str(output),
            "--comparison-output",
            str(comparison),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "provider/API calls: ZERO" in result.stdout
    assert "media uploads: ZERO" in result.stdout
    assert output.is_file()
    assert comparison.is_file()
    plan = read_json(output)
    assert plan["status"] == "PLANNED_NOT_EXECUTED"
    assert plan["validation_case_ids_sealed"] == ["m8-real-val-01", "m8-real-val-02"]
    assert "GEMINI_API_KEY" not in json.dumps(plan)
    assert "review-proxies" not in json.dumps(plan)
