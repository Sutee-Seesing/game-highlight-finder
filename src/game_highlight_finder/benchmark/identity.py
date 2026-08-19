"""Fail-closed identity compatibility for locked benchmark annotations."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from game_highlight_finder.benchmark.models import (
    BenchmarkCase,
    BenchmarkDataset,
    BenchmarkEvaluation,
)
from game_highlight_finder.storage.atomic import read_json

M8_DATASET_BENCHMARK_ID = "m8-real-v1"
M8_LOCKED_ANNOTATION_BENCHMARK_ID = "m8-private"
M8_GROUND_TRUTH_LOCK_FILENAME = "m8-real-v1-ground-truth-lock.json"


def default_ground_truth_lock_path(dataset_path: Path) -> Path:
    """Return the repository-private lock path convention for an M8 dataset."""

    resolved = dataset_path.expanduser().resolve()
    return resolved.parent.parent / "private" / M8_GROUND_TRUTH_LOCK_FILENAME


def _locked_case(lock_path: Path, case_id: str) -> Mapping[str, object] | None:
    try:
        raw = read_json(lock_path.expanduser().resolve())
    except Exception:
        return None
    if not isinstance(raw, Mapping):
        return None
    cases = raw.get("cases")
    if not isinstance(cases, list):
        return None
    for item in cases:
        if isinstance(item, Mapping) and item.get("case_id") == case_id:
            return item
    return None


def benchmark_identity_compatible(
    dataset_path: Path,
    dataset: BenchmarkDataset,
    case: BenchmarkCase,
    *,
    evaluation_benchmark_id: str,
    evaluation_case_id: str,
    evaluation_source_sha256: str,
    evaluation_annotation_sha256: str,
    expected_annotation_sha256: str,
    lock_path: Path | None = None,
) -> bool:
    """Check an evaluation's benchmark identity against a declared case.

    Normal datasets require exact benchmark-ID equality.  The locked M8 dataset
    intentionally declares ``m8-real-v1`` while its immutable annotation files
    carry the private annotation namespace ``m8-private``.  That one exception
    is accepted only when the case ID, source hash, annotation hash, split, and
    lock entry all agree.  A missing or malformed lock therefore fails closed.
    """

    if evaluation_benchmark_id == dataset.benchmark_id:
        return True
    if (
        dataset.benchmark_id != M8_DATASET_BENCHMARK_ID
        or evaluation_benchmark_id != M8_LOCKED_ANNOTATION_BENCHMARK_ID
        or evaluation_case_id != case.case_id
        or evaluation_source_sha256 != case.expected_source_sha256
        or evaluation_annotation_sha256 != expected_annotation_sha256
    ):
        return False
    lock_entry = _locked_case(
        lock_path.expanduser().resolve()
        if lock_path is not None
        else default_ground_truth_lock_path(dataset_path),
        case.case_id,
    )
    if lock_entry is None:
        return False
    return (
        lock_entry.get("case_id") == case.case_id
        and lock_entry.get("split") == case.split.value
        and lock_entry.get("source_sha256") == case.expected_source_sha256
        and lock_entry.get("source_sha256") == evaluation_source_sha256
        and lock_entry.get("annotation_sha256") == expected_annotation_sha256
        and lock_entry.get("annotation_sha256") == evaluation_annotation_sha256
    )


def rebind_evaluation_to_dataset(
    evaluation: BenchmarkEvaluation, *, dataset_benchmark_id: str
) -> BenchmarkEvaluation:
    """Canonicalize a proven compatible private ID for aggregate artifacts."""

    if evaluation.benchmark_id == dataset_benchmark_id:
        return evaluation
    payload = evaluation.model_dump(mode="json")
    payload["benchmark_id"] = dataset_benchmark_id
    payload["evaluation_fingerprint"] = ""
    return BenchmarkEvaluation.model_validate(payload)


__all__ = [
    "M8_DATASET_BENCHMARK_ID",
    "M8_GROUND_TRUTH_LOCK_FILENAME",
    "M8_LOCKED_ANNOTATION_BENCHMARK_ID",
    "benchmark_identity_compatible",
    "default_ground_truth_lock_path",
    "rebind_evaluation_to_dataset",
]
