"""Provider-free readiness artifact for one calibration Scout execution."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from game_highlight_finder.benchmark.models import (
    BenchmarkAnnotations,
    BenchmarkCase,
    BenchmarkDataset,
    BenchmarkSplit,
)
from game_highlight_finder.config import AppConfig, config_hash
from game_highlight_finder.domain.models import Sha256, SourceAsset
from game_highlight_finder.domain.windows import ScoutWindow, plan_scout_windows
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.pipeline.windowed_scout import aggregate_window_preflight
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.hashing import hash_file
from game_highlight_finder.storage.sessions import session_paths, source_from_artifact


class ScoutReadinessModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ScoutReadinessWindow(ScoutReadinessModel):
    window_id: str = Field(pattern=r"^scout_window_[0-9a-f]{16}$")
    ordinal: int = Field(ge=0)
    source_start_ms: int = Field(ge=0)
    source_end_ms: int = Field(gt=0)
    proxy_sha256: Sha256
    signals_file_sha256: Sha256
    maximum_reserved_micro_thb: int = Field(ge=0)


class ScoutCalibrationReadiness(ScoutReadinessModel):
    schema_version: Literal[1] = 1
    version: Literal["scout-calibration-readiness-v1"] = "scout-calibration-readiness-v1"
    benchmark_id: str = Field(min_length=1, max_length=128)
    case_id: str = Field(min_length=1, max_length=128)
    split: Literal["calibration"] = "calibration"
    session_id: str = Field(min_length=1, max_length=128)
    dataset_sha256: Sha256
    annotation_sha256: Sha256
    source_sha256: Sha256
    source_duration_ms: int = Field(gt=0)
    config_sha256: Sha256
    provider: Literal["gemini"] = "gemini"
    model: str = Field(min_length=1, max_length=256)
    billing_mode: str = Field(min_length=1, max_length=64)
    media_resolution: str = Field(min_length=1, max_length=64)
    thinking_level: str | None = Field(default=None, max_length=64)
    window_prompt_version: str = Field(min_length=1, max_length=128)
    max_output_tokens: int = Field(gt=0)
    windows: tuple[ScoutReadinessWindow, ...] = Field(min_length=1, max_length=10_000)
    planned_provider_requests: int = Field(gt=0)
    aggregate_maximum_reserved_micro_thb: int = Field(ge=0)
    monthly_available_micro_thb: int = Field(ge=0)
    post_reservation_headroom_micro_thb: int
    budget_blocked: bool
    budget_reason: str = Field(min_length=1, max_length=500)
    paid_response_cache_assumption: Literal["ZERO"] = "ZERO"
    provider_calls: Literal[0] = 0
    remote_uploads: Literal[0] = 0
    ledger_reservations: Literal[0] = 0
    provider_clean_session: Literal[True] = True
    source_verified_unchanged: Literal[True] = True
    revealed_validation_used: Literal[False] = False
    semantic_quality_available: Literal[False] = False
    fresh_attempt_authorization_required: Literal[True] = True
    ready_for_authorized_execution: bool
    ground_truth_highlight_count: int = Field(ge=0)
    must_catch_highlight_count: int = Field(ge=0)


def _load_dataset(path: Path) -> BenchmarkDataset:
    try:
        return BenchmarkDataset.model_validate(read_json(path))
    except Exception as exc:
        raise ValidationError("Scout readiness dataset is invalid.", hint=str(exc)) from exc


def _load_annotations(path: Path) -> BenchmarkAnnotations:
    try:
        return BenchmarkAnnotations.model_validate(read_json(path))
    except Exception as exc:
        raise ValidationError("Scout readiness annotations are invalid.", hint=str(exc)) from exc


def _select_calibration_case(dataset: BenchmarkDataset, case_id: str | None) -> BenchmarkCase:
    if case_id is not None:
        matches = [case for case in dataset.cases if case.case_id == case_id]
        if len(matches) != 1:
            raise ValidationError(f"Benchmark case does not exist: {case_id}")
        selected = matches[0]
    else:
        calibration = [case for case in dataset.cases if case.split is BenchmarkSplit.CALIBRATION]
        if len(calibration) != 1:
            raise ValidationError(
                "Scout readiness requires --case-id unless the dataset has exactly one "
                "calibration case."
            )
        selected = calibration[0]
    if selected.split is not BenchmarkSplit.CALIBRATION:
        raise ValidationError(
            "Scout readiness accepts calibration cases only; validation/holdout data is forbidden."
        )
    return selected


def _validate_case_identity(
    dataset_path: Path,
    annotations_path: Path,
    dataset: BenchmarkDataset,
    case: BenchmarkCase,
    annotations: BenchmarkAnnotations,
) -> Path:
    declared_annotations = (dataset_path.parent / case.annotation_path).resolve()
    if annotations_path.resolve() != declared_annotations:
        raise ValidationError(
            "Scout readiness annotations must be the exact file declared by the dataset case."
        )
    if annotations.benchmark_id != dataset.benchmark_id or annotations.case_id != case.case_id:
        raise ValidationError("Scout readiness annotation benchmark/case identity does not match.")
    if annotations.source_sha256 != case.expected_source_sha256:
        raise ValidationError("Scout readiness annotation source hash does not match the dataset.")
    source_path = (dataset_path.parent / case.source_path).resolve()
    if not source_path.is_file():
        raise ValidationError("Scout readiness source file is missing.", hint=str(source_path))
    if hash_file(source_path, source=True) != case.expected_source_sha256:
        raise ValidationError("Scout readiness source bytes changed from the locked dataset hash.")
    return source_path


def _load_source(
    config: AppConfig,
    session_id: str,
    case: BenchmarkCase,
    annotations: BenchmarkAnnotations,
    declared_source_path: Path,
) -> SourceAsset:
    paths = session_paths(config.storage.data_dir, session_id)
    if not paths.source.is_file():
        raise ValidationError("Scout readiness session source artifact is missing.")
    source = source_from_artifact(paths.source)
    if source.sha256 != case.expected_source_sha256:
        raise ValidationError(
            "Scout readiness session source hash does not match the calibration case."
        )
    if source.duration_ms != annotations.source_duration_ms:
        raise ValidationError("Scout readiness session duration does not match annotations.")
    if source.path.resolve() != declared_source_path:
        raise ValidationError(
            "Scout readiness session source path does not match the dataset source."
        )
    if hash_file(source.path, source=True) != source.sha256:
        raise ValidationError(
            "Scout readiness source changed after ingest; refusing execution planning."
        )
    return source


def _load_provider_clean_windows(
    config: AppConfig,
    session_id: str,
    source: SourceAsset,
) -> tuple[tuple[ScoutWindow, ...], dict[str, dict[str, object]], dict[str, Sha256]]:
    paths = session_paths(config.storage.data_dir, session_id)
    if not paths.scout_windows_dir.is_dir():
        raise ValidationError("Scout readiness requires completed local M6 window preparation.")
    expected_plan = plan_scout_windows(
        source.duration_ms,
        max_duration_ms=config.scout.window_duration_seconds * 1000,
        overlap_ms=config.scout.window_overlap_seconds * 1000,
        session_id=session_id,
        source_id=source.source_id,
        max_windows=config.scout.max_windows,
    )
    loaded: list[ScoutWindow] = []
    summaries: dict[str, dict[str, object]] = {}
    signal_hashes: dict[str, Sha256] = {}
    parent_proxy = paths.proxy_dir / "analysis_proxy.mp4"
    if not parent_proxy.is_file():
        raise ValidationError("Scout readiness analysis proxy is missing.")
    parent_sha = hash_file(parent_proxy)
    paid_names = (
        "response.raw.json",
        "response.canonical.json",
        "request_meta.json",
        "gemini_remote_file.json",
        "cost.json",
    )
    for expected in expected_plan.windows:
        item_dir = paths.scout_windows_dir / expected.window_id
        metadata_path = item_dir / "window.json"
        proxy_path = item_dir / "analysis_window.mp4"
        signals_path = item_dir / "signals.json"
        if not metadata_path.is_file() or not proxy_path.is_file() or not signals_path.is_file():
            raise ValidationError(
                "Scout readiness window preparation is incomplete.", hint=str(item_dir)
            )
        window = ScoutWindow.model_validate(read_json(metadata_path))
        if (
            window.window_id != expected.window_id
            or window.ordinal != expected.ordinal
            or window.source_start_ms != expected.source_start_ms
            or window.source_end_ms != expected.source_end_ms
            or window.session_id != session_id
            or window.source_id != source.source_id
        ):
            raise ValidationError(
                "Scout readiness prepared window does not match current planning config."
            )
        if window.proxy_sha256 is None or hash_file(proxy_path) != window.proxy_sha256:
            raise ValidationError("Scout readiness window proxy hash validation failed.")
        if window.parent_proxy_sha256 != parent_sha:
            raise ValidationError("Scout readiness window parent-proxy lineage is invalid.")
        paid_found = [name for name in paid_names if (item_dir / name).exists()]
        if paid_found:
            raise ValidationError(
                "Scout readiness requires a provider-clean session; paid Scout artifacts "
                "already exist.",
                hint=", ".join(paid_found),
            )
        summary = read_json(signals_path)
        if not isinstance(summary, dict):
            raise ValidationError("Scout readiness signals.json must contain a JSON object.")
        loaded.append(window)
        summaries[window.window_id] = summary
        signal_hashes[window.window_id] = hash_file(signals_path)
    return tuple(loaded), summaries, signal_hashes


def run_scout_calibration_readiness(
    session_id: str,
    dataset_path: Path,
    annotations_path: Path,
    config: AppConfig,
    *,
    case_id: str | None = None,
    output_path: Path | None = None,
) -> tuple[ScoutCalibrationReadiness, Path]:
    """Persist a zero-call authorization/readiness artifact for one calibration Scout run."""

    if config.scout.backend != "gemini":
        raise ValidationError("Scout readiness requires the Gemini Scout backend.")
    dataset_path = dataset_path.expanduser().resolve()
    annotations_path = annotations_path.expanduser().resolve()
    dataset = _load_dataset(dataset_path)
    selected = _select_calibration_case(dataset, case_id)
    annotations = _load_annotations(annotations_path)
    declared_source = _validate_case_identity(
        dataset_path, annotations_path, dataset, selected, annotations
    )
    source = _load_source(config, session_id, selected, annotations, declared_source)
    windows, summaries, signal_hashes = _load_provider_clean_windows(config, session_id, source)
    preflight = aggregate_window_preflight(
        source,
        windows,
        config,
        cached_window_ids=set(),
        local_signal_summaries=summaries,
    )
    if preflight.available_micro_thb is None:
        raise ValidationError("Scout readiness could not determine monthly available budget.")
    window_artifact_items: list[ScoutReadinessWindow] = []
    for window in windows:
        assert window.proxy_sha256 is not None
        window_artifact_items.append(
            ScoutReadinessWindow(
                window_id=window.window_id,
                ordinal=window.ordinal,
                source_start_ms=window.source_start_ms,
                source_end_ms=window.source_end_ms,
                proxy_sha256=window.proxy_sha256,
                signals_file_sha256=signal_hashes[window.window_id],
                maximum_reserved_micro_thb=preflight.window_estimates_micro_thb[window.window_id],
            )
        )
    window_artifacts = tuple(window_artifact_items)
    artifact = ScoutCalibrationReadiness(
        benchmark_id=dataset.benchmark_id,
        case_id=selected.case_id,
        session_id=session_id,
        dataset_sha256=hash_file(dataset_path),
        annotation_sha256=hash_file(annotations_path),
        source_sha256=source.sha256,
        source_duration_ms=source.duration_ms,
        config_sha256=config_hash(config),
        model=config.scout.model,
        billing_mode=config.scout.billing_mode,
        media_resolution=config.scout.media_resolution,
        thinking_level=config.scout.thinking_level,
        window_prompt_version=config.scout.window_prompt_version,
        max_output_tokens=config.scout.max_output_tokens,
        windows=window_artifacts,
        planned_provider_requests=len(windows),
        aggregate_maximum_reserved_micro_thb=preflight.estimated_micro_thb,
        monthly_available_micro_thb=preflight.available_micro_thb,
        post_reservation_headroom_micro_thb=(
            preflight.available_micro_thb - preflight.estimated_micro_thb
        ),
        budget_blocked=preflight.blocked,
        budget_reason=preflight.reason,
        ready_for_authorized_execution=not preflight.blocked,
        ground_truth_highlight_count=len(annotations.highlights),
        must_catch_highlight_count=sum(
            1 for item in annotations.highlights if item.importance.value == "MUST_CATCH"
        ),
    )
    target = (
        output_path.expanduser().resolve()
        if output_path is not None
        else (
            config.storage.data_dir
            / "benchmarks"
            / "private"
            / f"{selected.case_id}.scout-readiness.json"
        ).resolve()
    )
    atomic_write_json(target, artifact.model_dump(mode="json"))
    return artifact, target
