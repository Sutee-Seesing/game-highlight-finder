"""Provider-free M8B2A Gemini calibration planning.

This module deliberately stops at a reproducible plan.  It verifies the
owner-confirmed benchmark lock, derives the same production Scout window
contract for both model arms, and estimates paid-equivalent exposure locally.
It does not import an SDK, create media, upload files, reserve budget, or run a
provider request.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from game_highlight_finder.benchmark.evaluator import (
    validate_annotations_file,
)
from game_highlight_finder.benchmark.models import (
    BenchmarkCase,
    BenchmarkDataset,
    BenchmarkModel,
    BenchmarkSplit,
)
from game_highlight_finder.config import AppConfig
from game_highlight_finder.domain.windows import plan_scout_windows
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.pipeline.gemini_contract import gemini_window_scout_schema
from game_highlight_finder.pipeline.gemini_scout import estimate_gemini_usage
from game_highlight_finder.pipeline.windowed_scout import build_window_prompt
from game_highlight_finder.providers.base import ProviderUsageEstimate
from game_highlight_finder.providers.gemini_capabilities import (
    MODEL_COMPATIBLE_MEDIA_RESOLUTION,
    MODEL_DEFAULT_MINIMUM_THINKING,
    resolve_gemini_media_resolution,
    resolve_gemini_thinking_config,
)
from game_highlight_finder.storage.atomic import atomic_write_json, read_json

CALIBRATION_CASE_IDS = ("m8-real-cal-01", "m8-real-cal-02")
VALIDATION_CASE_IDS = ("m8-real-val-01", "m8-real-val-02")
CalibrationModel = Literal["gemini-2.5-flash-lite", "gemini-3.5-flash-lite"]
CALIBRATION_MODEL_IDS: tuple[CalibrationModel, CalibrationModel] = (
    "gemini-2.5-flash-lite",
    "gemini-3.5-flash-lite",
)
EXPECTED_BENCHMARK_ID = "m8-real-v1"
EXPECTED_POLICY_FINGERPRINT = "13f2a750beb4e8bfb3a8288e6974db38aa4354c6d8e456c95716bf4f680853b2"
EXPECTED_AGGREGATE_COUNTS = {
    "highlights": 10,
    "must_catch": 3,
    "worth_review": 6,
    "optional": 1,
    "boring_intervals": 4,
}
CALIBRATION_EXPERIMENT_REVISION = "v5"


def _sha256_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _strict_decimal(value: object, *, field_name: str, minimum: Decimal = Decimal("0")) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"{field_name} must be a finite decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite decimal") from exc
    if not parsed.is_finite() or parsed < minimum:
        raise ValueError(f"{field_name} must be a finite decimal >= {minimum}")
    return parsed


class CalibrationPricingEntry(BenchmarkModel):
    """Planning-only paid-equivalent rates, never an authoritative ledger rate."""

    model: Literal["gemini-2.5-flash-lite", "gemini-3.5-flash-lite"]
    currency: Literal["USD"] = "USD"
    unit: Literal["USD_per_million_tokens"] = "USD_per_million_tokens"
    input_rates_by_modality: dict[str, Decimal] = Field(min_length=1, max_length=8)
    output_rate: Decimal
    source: str = Field(min_length=1, max_length=500)
    snapshot_version: str = Field(min_length=1, max_length=128)
    verified_for_live: bool = False
    reverify_before_live: bool = True

    @field_validator("input_rates_by_modality", mode="before")
    @classmethod
    def validate_input_rates(cls, value: object) -> dict[str, Decimal]:
        if not isinstance(value, Mapping) or not value:
            raise ValueError("pricing input rates must be a non-empty object")
        result: dict[str, Decimal] = {}
        for key, rate in value.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("pricing modality names must be non-empty strings")
            result[key.strip()] = _strict_decimal(rate, field_name=f"input rate {key}")
        return result

    @field_validator("output_rate", mode="before")
    @classmethod
    def validate_output_rate(cls, value: object) -> Decimal:
        return _strict_decimal(value, field_name="output rate")


class CalibrationPricingSnapshot(BenchmarkModel):
    """Versioned reference metadata used only by the offline planner."""

    snapshot_version: str = Field(min_length=1, max_length=128)
    status: Literal["PLANNING_REFERENCE_NOT_LIVE_VERIFIED"] = "PLANNING_REFERENCE_NOT_LIVE_VERIFIED"
    entries: tuple[CalibrationPricingEntry, ...] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def exactly_target_models(self) -> CalibrationPricingSnapshot:
        models = tuple(entry.model for entry in self.entries)
        if models != CALIBRATION_MODEL_IDS:
            raise ValueError("pricing snapshot must contain exactly the two calibration models")
        if any(entry.snapshot_version != self.snapshot_version for entry in self.entries):
            raise ValueError("pricing entry and snapshot versions must match")
        return self

    def for_model(self, model: str) -> CalibrationPricingEntry:
        for entry in self.entries:
            if entry.model == model:
                return entry
        raise ValidationError(f"No calibration pricing reference exists for model {model!r}.")


class CalibrationLockVerification(BenchmarkModel):
    """The non-secret, machine-readable result of the ground-truth lock check."""

    status: Literal["PASS"] = "PASS"
    benchmark_id: str
    policy_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    case_count: int = Field(ge=0)
    calibration_case_ids: tuple[str, ...]
    validation_case_ids: tuple[str, ...]
    annotation_sha256: dict[str, str]
    source_sha256: dict[str, str]
    source_duration_ms: dict[str, int]
    aggregate_counts: dict[str, int]
    owner_confirmed: bool
    locked_before_provider_benchmark: bool
    provider_predictions_exist: bool


class CalibrationWindowPlan(BenchmarkModel):
    """One hypothetical production Scout-window upload (not a created file)."""

    window_id: str = Field(pattern=r"^scout_window_[0-9a-f]{16}$")
    ordinal: int = Field(ge=0)
    source_start_ms: int = Field(ge=0)
    source_end_ms: int = Field(gt=0)
    duration_ms: int = Field(gt=0)
    proxy_path: str = Field(min_length=1, max_length=500)
    parent_analysis_proxy_path: str = Field(min_length=1, max_length=500)
    audio_retained: bool
    would_upload_in_live_run: bool = True


class CalibrationCasePlan(BenchmarkModel):
    case_id: str
    split: Literal["calibration"] = "calibration"
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    annotation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_duration_ms: int = Field(gt=0)
    audio_retained: bool
    windows: tuple[CalibrationWindowPlan, ...] = Field(min_length=1)
    total_media_duration_ms: int = Field(gt=0)
    planned_provider_requests: int = Field(ge=0)
    cache_hits_known: int = Field(ge=0)
    raw_upload_planned: bool = False


class CalibrationResultSetTemplate(BenchmarkModel):
    """A future result-set reference which is intentionally not completed."""

    result_set_id: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=240)
    model: Literal["gemini-2.5-flash-lite", "gemini-3.5-flash-lite"]
    status: Literal["NOT_CREATED"] = "NOT_CREATED"
    experiment_fingerprint: None = None
    evaluation_path: None = None


class CalibrationComparisonManifest(BenchmarkModel):
    """Private future comparison rules without placeholder completed results."""

    schema_version: Literal[1] = 1
    manifest_type: Literal["planned_comparison"] = "planned_comparison"
    status: Literal["PLANNED_NOT_EXECUTED"] = "PLANNED_NOT_EXECUTED"
    comparison_id: str = f"m8b2-calibration-comparison-{CALIBRATION_EXPERIMENT_REVISION}"
    benchmark_id: str
    evaluation_policy_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    case_ids: tuple[str, ...] = Field(min_length=2, max_length=2)
    annotation_revision: dict[str, str]
    source_revision: dict[str, str]
    required_equal_fields: tuple[str, ...] = (
        "case_coverage",
        "annotation_revision",
        "source_revision",
        "evaluation_policy_fingerprint",
    )
    result_sets: tuple[CalibrationResultSetTemplate, ...] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def compare_exactly_two_arms(self) -> CalibrationComparisonManifest:
        if tuple(item.model for item in self.result_sets) != CALIBRATION_MODEL_IDS:
            raise ValueError("comparison manifest must contain exactly the two model arms")
        if tuple(self.annotation_revision) != self.case_ids:
            raise ValueError("annotation revision must cover calibration cases in declared order")
        if tuple(self.source_revision) != self.case_ids:
            raise ValueError("source revision must cover calibration cases in declared order")
        return self


class CalibrationArmPlan(BenchmarkModel):
    arm: Literal["A", "B"]
    model: Literal["gemini-2.5-flash-lite", "gemini-3.5-flash-lite"]
    label: str
    result_set_id: str
    shared_config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    experiment_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    cases: tuple[CalibrationCasePlan, ...] = Field(min_length=2, max_length=2)
    planned_scout_windows: int = Field(ge=1)
    planned_media_duration_ms: int = Field(gt=0)
    planned_provider_requests: int = Field(ge=0)
    cache_hits_known: int = Field(ge=0)
    usage_estimate: ProviderUsageEstimate
    effective_thinking_config: dict[str, Any] = Field(default_factory=dict, max_length=32)
    effective_media_config: dict[str, Any] = Field(default_factory=dict, max_length=32)
    estimated_paid_equivalent_cost_usd: Decimal
    estimated_paid_equivalent_cost_thb: Decimal | None = None
    actual_settled_cost_thb: Decimal | None = None
    raw_upload_planned: bool = False
    audio_retained: bool


class CalibrationPlan(BenchmarkModel):
    """Complete private M8B2A preparation artifact."""

    schema_version: Literal[1] = 1
    manifest_type: Literal["calibration_plan"] = "calibration_plan"
    status: Literal["PLANNED_NOT_EXECUTED"] = "PLANNED_NOT_EXECUTED"
    benchmark_id: str
    policy_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    ground_truth_lock: CalibrationLockVerification
    calibration_case_ids: tuple[str, ...] = Field(min_length=2, max_length=2)
    validation_case_ids_sealed: tuple[str, ...] = Field(min_length=2, max_length=2)
    models: tuple[str, ...] = Field(min_length=2, max_length=2)
    shared_semantic_config: dict[str, Any]
    shared_config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    arms: tuple[CalibrationArmPlan, ...] = Field(min_length=2, max_length=2)
    pricing_snapshot: CalibrationPricingSnapshot
    free_tier_intent: bool = True
    paid_fallback_authorized: bool = False
    raw_upload_planned: bool = False
    review_proxies_provider_inputs: bool = False
    audio_retained: bool = True
    actual_provider_calls: int = 0
    media_uploads: int = 0
    validation_predictions_exposed: bool = False
    comparison_manifest: CalibrationComparisonManifest

    @model_validator(mode="after")
    def plan_is_calibration_only(self) -> CalibrationPlan:
        if self.benchmark_id != EXPECTED_BENCHMARK_ID:
            raise ValueError("M8B2A only permits the locked m8-real-v1 benchmark")
        if self.models != CALIBRATION_MODEL_IDS:
            raise ValueError("M8B2A must contain exactly the two approved Gemini model IDs")
        if self.calibration_case_ids != CALIBRATION_CASE_IDS:
            raise ValueError("M8B2A must contain exactly the two calibration cases")
        if self.actual_provider_calls != 0 or self.media_uploads != 0:
            raise ValueError("M8B2A plan must be provider- and upload-free")
        if self.raw_upload_planned or self.review_proxies_provider_inputs:
            raise ValueError("M8B2A cannot plan raw or review-proxy provider input")
        if not self.ground_truth_lock.owner_confirmed:
            raise ValueError("M8B2A requires an owner-confirmed ground-truth lock")
        return self


def _resolve_manifest_path(path: Path, *, base_dir: Path) -> Path:
    candidate = path.expanduser()
    return candidate if candidate.is_absolute() else (base_dir / candidate).resolve()


def _load_dataset(path: Path) -> BenchmarkDataset:
    try:
        value = read_json(path)
        return BenchmarkDataset.model_validate(value)
    except Exception as exc:
        raise ValidationError("Benchmark dataset manifest is invalid.", hint=str(path)) from exc


def _load_lock(path: Path) -> Mapping[str, Any]:
    try:
        value = read_json(path)
    except Exception as exc:
        raise ValidationError("Ground-truth lock cannot be loaded.", hint=str(path)) from exc
    if not isinstance(value, Mapping):
        raise ValidationError("Ground-truth lock must be a JSON object.")
    return value


def verify_ground_truth_lock(dataset_path: Path, lock_path: Path) -> CalibrationLockVerification:
    """Verify every locked source, annotation, split, policy, and aggregate count."""

    dataset_path = dataset_path.expanduser().resolve()
    lock_path = lock_path.expanduser().resolve()
    dataset = _load_dataset(dataset_path)
    lock = _load_lock(lock_path)
    if (
        dataset.benchmark_id != EXPECTED_BENCHMARK_ID
        or lock.get("benchmark_id") != EXPECTED_BENCHMARK_ID
    ):
        raise ValidationError("Ground-truth lock benchmark identity does not match m8-real-v1.")
    if dataset.policy_fingerprint != EXPECTED_POLICY_FINGERPRINT:
        raise ValidationError(
            "Dataset evaluation policy fingerprint differs from the accepted lock."
        )
    if lock.get("policy_fingerprint") != EXPECTED_POLICY_FINGERPRINT:
        raise ValidationError(
            "Ground-truth lock policy fingerprint differs from the accepted lock."
        )
    expected_ids = CALIBRATION_CASE_IDS + VALIDATION_CASE_IDS
    if tuple(case.case_id for case in dataset.cases) != expected_ids:
        raise ValidationError("Dataset cases do not match the sealed calibration/validation lock.")
    if lock.get("status") != "OWNER_CONFIRMED_GROUND_TRUTH":
        raise ValidationError("Ground-truth lock is not owner-confirmed.")
    if (
        lock.get("owner_confirmed") is not True
        or lock.get("locked_before_provider_benchmark") is not True
    ):
        raise ValidationError("Ground-truth lock is not sealed before provider benchmarking.")
    if lock.get("provider_predictions_exist") is not False:
        raise ValidationError("Ground-truth lock indicates provider predictions already exist.")

    locked_cases = lock.get("cases")
    if not isinstance(locked_cases, Sequence) or isinstance(locked_cases, (str, bytes)):
        raise ValidationError("Ground-truth lock cases are missing or malformed.")
    locked_by_id = {item.get("case_id"): item for item in locked_cases if isinstance(item, Mapping)}
    if set(locked_by_id) != set(expected_ids):
        raise ValidationError("Ground-truth lock case identities are incomplete or unexpected.")

    annotation_hashes: dict[str, str] = {}
    source_hashes: dict[str, str] = {}
    source_durations: dict[str, int] = {}
    aggregate = {key: 0 for key in EXPECTED_AGGREGATE_COUNTS}
    dataset_by_id = {case.case_id: case for case in dataset.cases}
    for case_id in expected_ids:
        case = dataset_by_id[case_id]
        locked = locked_by_id[case_id]
        if locked.get("split") != case.split.value:
            raise ValidationError(f"Ground-truth split mismatch for {case_id}.")
        if locked.get("source_sha256") != case.expected_source_sha256:
            raise ValidationError(f"Ground-truth source identity mismatch for {case_id}.")
        annotation_path = _resolve_manifest_path(case.annotation_path, base_dir=dataset_path.parent)
        summary = validate_annotations_file(annotation_path)
        if summary.case_id != case_id:
            raise ValidationError(f"Annotation case identity mismatch for {case_id}.")
        if summary.annotation_sha256 != locked.get("annotation_sha256"):
            raise ValidationError(f"Annotation SHA-256 mismatch for {case_id}.")
        if summary.source_duration_ms <= 0:
            raise ValidationError(f"Annotation duration is invalid for {case_id}.")
        annotation_hashes[case_id] = summary.annotation_sha256
        source_hashes[case_id] = case.expected_source_sha256
        source_durations[case_id] = summary.source_duration_ms
        aggregate["highlights"] += summary.highlight_count
        aggregate["must_catch"] += summary.must_catch_count
        aggregate["worth_review"] += summary.worth_review_count
        aggregate["optional"] += summary.optional_count
        aggregate["boring_intervals"] += summary.boring_interval_count
    if aggregate != EXPECTED_AGGREGATE_COUNTS:
        raise ValidationError(f"Locked aggregate counts differ: {aggregate!r}")
    locked_aggregate = lock.get("aggregate_counts")
    if locked_aggregate != EXPECTED_AGGREGATE_COUNTS:
        raise ValidationError("Ground-truth lock aggregate counts differ from the accepted lock.")
    return CalibrationLockVerification(
        benchmark_id=dataset.benchmark_id,
        policy_fingerprint=dataset.policy_fingerprint,
        case_count=len(dataset.cases),
        calibration_case_ids=CALIBRATION_CASE_IDS,
        validation_case_ids=VALIDATION_CASE_IDS,
        annotation_sha256=annotation_hashes,
        source_sha256=source_hashes,
        source_duration_ms=source_durations,
        aggregate_counts=aggregate,
        owner_confirmed=True,
        locked_before_provider_benchmark=True,
        provider_predictions_exist=False,
    )


def _sum_usage(usages: Sequence[ProviderUsageEstimate]) -> ProviderUsageEstimate:
    fields = (
        "input_text_tokens",
        "input_image_tokens",
        "input_video_tokens",
        "input_audio_tokens",
        "cached_input_tokens",
        "output_tokens",
        "thinking_tokens",
    )
    return ProviderUsageEstimate(
        **{field: sum(getattr(usage, field) for usage in usages) for field in fields}
    )


def _estimated_usd(usage: ProviderUsageEstimate, entry: CalibrationPricingEntry) -> Decimal:
    rates = entry.input_rates_by_modality
    total = Decimal("0")
    modality_for_dimension = {
        "input_text_tokens": "text",
        "input_image_tokens": "image",
        "input_video_tokens": "video",
        "input_audio_tokens": "audio",
    }
    for dimension, modality in modality_for_dimension.items():
        count = getattr(usage, dimension)
        if count:
            if modality not in rates:
                raise ValidationError(
                    f"Pricing snapshot lacks {modality} input rate for {entry.model}."
                )
            total += Decimal(count) * rates[modality] / Decimal(1_000_000)
    if usage.cached_input_tokens:
        raise ValidationError("Calibration planning does not assume cached input tokens.")
    total += Decimal(usage.billable_output_tokens) * entry.output_rate / Decimal(1_000_000)
    return total


def _pricing_snapshot() -> CalibrationPricingSnapshot:
    version = "m8b2a-gemini-pricing-reference-v1"
    source = (
        "Owner-supplied planning reference; reverify official Google pricing before any live run."
    )
    return CalibrationPricingSnapshot(
        snapshot_version=version,
        entries=(
            CalibrationPricingEntry(
                model="gemini-2.5-flash-lite",
                input_rates_by_modality={
                    "text": Decimal("0.10"),
                    "image": Decimal("0.10"),
                    "video": Decimal("0.10"),
                    "audio": Decimal("0.30"),
                },
                output_rate=Decimal("0.40"),
                source=source,
                snapshot_version=version,
            ),
            CalibrationPricingEntry(
                model="gemini-3.5-flash-lite",
                input_rates_by_modality={
                    "text": Decimal("0.30"),
                    "image": Decimal("0.30"),
                    "video": Decimal("0.30"),
                    "audio": Decimal("0.30"),
                },
                output_rate=Decimal("2.50"),
                source=source,
                snapshot_version=version,
            ),
        ),
    )


def _shared_config(
    config: AppConfig, *, prompt_fingerprint: str, schema_fingerprint: str
) -> dict[str, Any]:
    scout = config.scout
    return {
        "provider": "gemini",
        "backend": "gemini",
        "billing_mode": scout.billing_mode,
        "media_resolution": scout.media_resolution,
        "media_resolution_policy": MODEL_COMPATIBLE_MEDIA_RESOLUTION,
        "thinking_policy": MODEL_DEFAULT_MINIMUM_THINKING,
        "configured_thinking_level": scout.thinking_level,
        "thinking_level": scout.thinking_level,
        "max_output_tokens": scout.max_output_tokens,
        "reserved_thinking_tokens": scout.reserved_thinking_tokens,
        "prompt_version": scout.window_prompt_version,
        "prompt_fingerprint": prompt_fingerprint,
        "schema_version": scout.schema_version,
        "schema_fingerprint": schema_fingerprint,
        "window_duration_seconds": scout.window_duration_seconds,
        "window_overlap_seconds": scout.window_overlap_seconds,
        "max_windows": scout.max_windows,
        "provider_media_policy": "production-analysis-proxy-window",
        "review_proxies_are_provider_inputs": False,
        "raw_originals_are_provider_inputs": False,
        "audio_retained": True,
        "reconciliation_extraction_ranking": "accepted-production-pipeline-unchanged",
        "remote_upload_requires_explicit_future_authorization": True,
        "experiment_revision": CALIBRATION_EXPERIMENT_REVISION,
    }


def _case_plan(
    case: BenchmarkCase,
    annotation_hash: str,
    source_duration_ms: int,
    config: AppConfig,
) -> tuple[CalibrationCasePlan, str]:
    if case.split is not BenchmarkSplit.CALIBRATION:
        raise ValidationError(f"Non-calibration case reached the M8B2A planner: {case.case_id}")
    audio_retained = "audio" in case.tags
    if not audio_retained:
        raise ValidationError(
            f"Calibration case does not declare an audio-capable source: {case.case_id}"
        )
    session_id = f"m8b2a-{case.case_id}"
    source_id = f"src_{case.expected_source_sha256[:16]}"
    window_plan = plan_scout_windows(
        source_duration_ms,
        max_duration_ms=config.scout.window_duration_seconds * 1_000,
        overlap_ms=config.scout.window_overlap_seconds * 1_000,
        session_id=session_id,
        source_id=source_id,
        max_windows=config.scout.max_windows,
    )
    prompt_parts: list[str] = []
    windows: list[CalibrationWindowPlan] = []
    total_duration = 0
    parent_path = f"sessions/{session_id}/proxy/analysis_proxy.mp4"
    for window in window_plan.windows:
        prompt = build_window_prompt(
            source_duration_ms=source_duration_ms,
            window=window,
            local_signal_summary={},
            prompt_version=config.scout.window_prompt_version,
        )
        prompt_parts.append(prompt)
        proxy_path = f"sessions/{session_id}/scout/windows/{window.window_id}/analysis_window.mp4"
        windows.append(
            CalibrationWindowPlan(
                window_id=window.window_id,
                ordinal=window.ordinal,
                source_start_ms=window.source_start_ms,
                source_end_ms=window.source_end_ms,
                duration_ms=window.duration_ms,
                proxy_path=proxy_path,
                parent_analysis_proxy_path=parent_path,
                audio_retained=audio_retained,
            )
        )
        total_duration += window.duration_ms
    return (
        CalibrationCasePlan(
            case_id=case.case_id,
            source_sha256=case.expected_source_sha256,
            annotation_sha256=annotation_hash,
            source_duration_ms=source_duration_ms,
            audio_retained=audio_retained,
            windows=tuple(windows),
            total_media_duration_ms=total_duration,
            planned_provider_requests=len(windows),
            cache_hits_known=0,
        ),
        _sha256_text("\n".join(prompt_parts)),
    )


def build_calibration_plan(
    dataset_path: Path,
    config: AppConfig,
    *,
    lock_path: Path | None = None,
    fx_usd_thb: Decimal | str | int | float | None = None,
) -> CalibrationPlan:
    """Build a deterministic, provider-free two-arm calibration plan."""

    dataset_path = dataset_path.expanduser().resolve()
    lock_path = lock_path or (
        dataset_path.parent.parent / "private" / "m8-real-v1-ground-truth-lock.json"
    )
    verification = verify_ground_truth_lock(dataset_path, lock_path)
    dataset = _load_dataset(dataset_path)
    by_id = {case.case_id: case for case in dataset.cases}
    provisional_config = config.model_copy(
        update={"scout": config.scout.model_copy(update={"backend": "gemini"})}
    )
    case_plans: list[CalibrationCasePlan] = []
    case_prompt_hashes: list[str] = []
    for case_id in CALIBRATION_CASE_IDS:
        case_plan, prompt_hash = _case_plan(
            by_id[case_id],
            verification.annotation_sha256[case_id],
            verification.source_duration_ms[case_id],
            provisional_config,
        )
        case_plans.append(case_plan)
        case_prompt_hashes.append(prompt_hash)
    window_schema = gemini_window_scout_schema()
    schema_fingerprint = _sha256_json(window_schema)
    prompt_fingerprint = _sha256_json(
        {
            "prompt_version": provisional_config.scout.window_prompt_version,
            "case_window_prompt_hashes": dict(
                zip(CALIBRATION_CASE_IDS, case_prompt_hashes, strict=True)
            ),
        }
    )
    shared = _shared_config(
        provisional_config,
        prompt_fingerprint=prompt_fingerprint,
        schema_fingerprint=schema_fingerprint,
    )
    shared_fingerprint = _sha256_json(shared)
    pricing = _pricing_snapshot()
    fx: Decimal | None = None
    if fx_usd_thb is not None:
        fx = _strict_decimal(
            fx_usd_thb, field_name="USD/THB planning FX rate", minimum=Decimal("0.000001")
        )

    arms: list[CalibrationArmPlan] = []
    for ordinal, model in enumerate(CALIBRATION_MODEL_IDS):
        model_id = model
        model_config = provisional_config.model_copy(
            update={"scout": provisional_config.scout.model_copy(update={"model": model_id})}
        )
        thinking = resolve_gemini_thinking_config(
            model_id,
            model_config.scout.thinking_level,
            model_config.scout.reserved_thinking_tokens,
        )
        media = resolve_gemini_media_resolution(model_id, model_config.scout.media_resolution)
        usage_items: list[ProviderUsageEstimate] = []
        for case_plan in case_plans:
            for window in case_plan.windows:
                prompt = build_window_prompt(
                    source_duration_ms=case_plan.source_duration_ms,
                    window=window_plan_scout_window(window, case_plan),
                    local_signal_summary={},
                    prompt_version=model_config.scout.window_prompt_version,
                )
                usage_items.append(
                    estimate_gemini_usage(
                        duration_ms=window.duration_ms,
                        prompt=prompt,
                        response_schema=window_schema,
                        audio_present=window.audio_retained,
                        max_output_tokens=model_config.scout.max_output_tokens,
                        reserved_thinking_tokens=thinking.reserved_thinking_tokens,
                        model=model_id,
                        media_resolution=model_config.scout.media_resolution,
                    )
                )
        usage = _sum_usage(usage_items)
        estimated_usd = _estimated_usd(usage, pricing.for_model(model_id))
        estimated_thb = estimated_usd * fx if fx is not None else None
        experiment_fingerprint = _sha256_json(
            {
                "benchmark_id": EXPECTED_BENCHMARK_ID,
                "policy_fingerprint": verification.policy_fingerprint,
                "model": model_id,
                "thinking": thinking.payload(),
                "media": media.payload(),
                "shared_config_fingerprint": shared_fingerprint,
                "source_revision": verification.source_sha256,
                "annotation_revision": verification.annotation_sha256,
            }
        )
        arms.append(
            CalibrationArmPlan(
                arm="A" if ordinal == 0 else "B",
                model=model_id,
                label=model_id,
                result_set_id=f"m8b2-cal-{model_id}-{CALIBRATION_EXPERIMENT_REVISION}",
                shared_config_fingerprint=shared_fingerprint,
                prompt_fingerprint=prompt_fingerprint,
                schema_fingerprint=schema_fingerprint,
                experiment_fingerprint=experiment_fingerprint,
                cases=tuple(case_plans),
                planned_scout_windows=sum(len(case.windows) for case in case_plans),
                planned_media_duration_ms=sum(case.total_media_duration_ms for case in case_plans),
                planned_provider_requests=sum(
                    case.planned_provider_requests for case in case_plans
                ),
                cache_hits_known=0,
                usage_estimate=usage,
                effective_thinking_config=thinking.payload(),
                effective_media_config=media.payload(),
                estimated_paid_equivalent_cost_usd=estimated_usd,
                estimated_paid_equivalent_cost_thb=estimated_thb,
                audio_retained=all(case.audio_retained for case in case_plans),
            )
        )
    comparison = CalibrationComparisonManifest(
        benchmark_id=EXPECTED_BENCHMARK_ID,
        evaluation_policy_fingerprint=verification.policy_fingerprint,
        case_ids=CALIBRATION_CASE_IDS,
        annotation_revision={
            case_id: verification.annotation_sha256[case_id] for case_id in CALIBRATION_CASE_IDS
        },
        source_revision={
            case_id: verification.source_sha256[case_id] for case_id in CALIBRATION_CASE_IDS
        },
        result_sets=tuple(
            CalibrationResultSetTemplate(
                result_set_id=arm.result_set_id,
                label=arm.label,
                model=arm.model,
            )
            for arm in arms
        ),
    )
    return CalibrationPlan(
        benchmark_id=EXPECTED_BENCHMARK_ID,
        policy_fingerprint=verification.policy_fingerprint,
        ground_truth_lock=verification,
        calibration_case_ids=CALIBRATION_CASE_IDS,
        validation_case_ids_sealed=VALIDATION_CASE_IDS,
        models=CALIBRATION_MODEL_IDS,
        shared_semantic_config=shared,
        shared_config_fingerprint=shared_fingerprint,
        prompt_fingerprint=prompt_fingerprint,
        schema_fingerprint=schema_fingerprint,
        arms=tuple(arms),
        pricing_snapshot=pricing,
        audio_retained=all(arm.audio_retained for arm in arms),
        comparison_manifest=comparison,
    )


def window_plan_scout_window(window: CalibrationWindowPlan, case: CalibrationCasePlan) -> Any:
    """Reconstruct the domain window needed solely to hash the frozen prompt."""

    # Import locally to keep the public planner model independent of execution.
    from game_highlight_finder.domain.windows import ScoutWindow

    return ScoutWindow(
        window_id=window.window_id,
        session_id=f"m8b2a-{case.case_id}",
        source_id=f"src_{case.source_sha256[:16]}",
        ordinal=window.ordinal,
        source_start_ms=window.source_start_ms,
        source_end_ms=window.source_end_ms,
        source_duration_ms=case.source_duration_ms,
        proxy_path=window.proxy_path,
    )


def write_calibration_artifacts(
    plan: CalibrationPlan,
    plan_path: Path,
    comparison_path: Path,
) -> None:
    """Persist private planning artifacts; callers may choose a temp path in tests."""

    atomic_write_json(plan_path, plan.model_dump(mode="json"))
    atomic_write_json(comparison_path, plan.comparison_manifest.model_dump(mode="json"))


__all__ = [
    "CALIBRATION_CASE_IDS",
    "CALIBRATION_MODEL_IDS",
    "EXPECTED_AGGREGATE_COUNTS",
    "EXPECTED_BENCHMARK_ID",
    "EXPECTED_POLICY_FINGERPRINT",
    "CalibrationArmPlan",
    "CalibrationCasePlan",
    "CalibrationComparisonManifest",
    "CalibrationLockVerification",
    "CalibrationPlan",
    "CalibrationPricingEntry",
    "CalibrationPricingSnapshot",
    "CalibrationResultSetTemplate",
    "CalibrationWindowPlan",
    "build_calibration_plan",
    "verify_ground_truth_lock",
    "write_calibration_artifacts",
]
