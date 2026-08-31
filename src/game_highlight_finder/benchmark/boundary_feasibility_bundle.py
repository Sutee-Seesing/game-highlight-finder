"""Portable, media-free calibration bundle for boundary-feasibility diagnostics."""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from game_highlight_finder.benchmark.boundary_feasibility import (
    BoundaryRefinementFeasibility,
    DiagnosticVerdict,
    run_boundary_refinement_feasibility,
)
from game_highlight_finder.benchmark.evaluator import load_annotations
from game_highlight_finder.benchmark.models import (
    BenchmarkAnnotations,
    BenchmarkCase,
    BenchmarkDataset,
    BenchmarkSplit,
)
from game_highlight_finder.config import AppConfig
from game_highlight_finder.domain.models import SessionMap, Sha256, SourceAsset, model_json
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.hashing import hash_file
from game_highlight_finder.storage.sessions import session_paths, source_from_artifact

BOUNDARY_FEASIBILITY_BUNDLE_VERSION = "boundary-feasibility-bundle-v1"


class BoundaryFeasibilityBundleManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    version: str = BOUNDARY_FEASIBILITY_BUNDLE_VERSION
    case_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    source_sha256: Sha256
    evaluation_policy_fingerprint: Sha256
    diagnostic_verdict: DiagnosticVerdict
    files: dict[str, Sha256] = Field(min_length=5, max_length=5)
    provider_calls: Literal[0] = 0
    media_files_included: Literal[0] = 0
    calibration_only: Literal[True] = True
    validation_data_included: Literal[False] = False
    source_path_sanitized: Literal[True] = True
    scout_backend: str = Field(min_length=1, max_length=64)
    scout_model: str | None = Field(default=None, min_length=1, max_length=128)
    scout_prompt_version: str | None = Field(default=None, min_length=1, max_length=64)
    scout_provenance_source: str = Field(min_length=1, max_length=64)
    scout_identity_fingerprint: Sha256 | None = None

    @model_validator(mode="after")
    def exact_portable_file_set(self) -> BoundaryFeasibilityBundleManifest:
        expected = {
            "dataset.json",
            f"annotations/{self.case_id}.json",
            f"data/sessions/{self.session_id}/source.json",
            f"data/sessions/{self.session_id}/session_map.json",
            "feasibility.json",
        }
        if set(self.files) != expected:
            raise ValueError("boundary-feasibility bundle file set is incomplete or unexpected")
        return self


class BoundaryFeasibilityBundleResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    root: Path
    manifest_path: Path
    manifest: BoundaryFeasibilityBundleManifest
    feasibility: BoundaryRefinementFeasibility


def _load_case_inputs(
    session_id: str,
    dataset_path: Path,
    annotations_path: Path,
    config: AppConfig,
) -> tuple[BenchmarkDataset, BenchmarkAnnotations, BenchmarkCase, SessionMap, SourceAsset]:
    resolved_dataset = dataset_path.expanduser().resolve()
    resolved_annotations = annotations_path.expanduser().resolve()
    if not resolved_dataset.is_file():
        raise ValidationError("Boundary-feasibility bundle dataset manifest does not exist")
    if not resolved_annotations.is_file():
        raise ValidationError("Boundary-feasibility bundle annotation file does not exist")
    try:
        dataset = BenchmarkDataset.model_validate(read_json(resolved_dataset))
    except Exception as exc:
        raise ValidationError(
            "Boundary-feasibility bundle dataset manifest is invalid", hint=str(exc)
        ) from exc
    annotations = load_annotations(resolved_annotations)
    matching = [case for case in dataset.cases if case.case_id == annotations.case_id]
    if len(matching) != 1:
        raise ValidationError(
            "Boundary-feasibility bundle annotation case is not uniquely declared"
        )
    case = matching[0]
    if case.split is not BenchmarkSplit.CALIBRATION:
        raise ValidationError(
            "Boundary-feasibility bundle is calibration-only; validation/holdout data is forbidden"
        )
    if dataset.benchmark_id != annotations.benchmark_id:
        raise ValidationError("Boundary-feasibility bundle benchmark identity mismatch")
    if case.expected_source_sha256 != annotations.source_sha256:
        raise ValidationError("Boundary-feasibility bundle source identity mismatch")
    if case.game_profile != annotations.game_profile:
        raise ValidationError("Boundary-feasibility bundle game profile mismatch")
    declared_annotation = case.annotation_path.expanduser()
    if not declared_annotation.is_absolute():
        declared_annotation = (resolved_dataset.parent / declared_annotation).resolve()
    else:
        declared_annotation = declared_annotation.resolve()
    if declared_annotation != resolved_annotations:
        raise ValidationError(
            "Boundary-feasibility bundle annotation path differs from dataset declaration"
        )

    paths = session_paths(config.storage.data_dir, session_id)
    if not paths.source.is_file() or not paths.session_map.is_file():
        raise ValidationError(
            "Boundary-feasibility bundle requires committed source and session map artifacts"
        )
    source = source_from_artifact(paths.source)
    if source.sha256 != case.expected_source_sha256:
        raise ValidationError(
            "Boundary-feasibility bundle session source does not match dataset case"
        )
    try:
        session_map = SessionMap.model_validate(read_json(paths.session_map))
    except Exception as exc:
        raise ValidationError(
            "Boundary-feasibility bundle session map is invalid", hint=str(exc)
        ) from exc
    if session_map.session_id != session_id:
        raise ValidationError("Boundary-feasibility bundle session ID mismatch")
    if session_map.source_id != source.source_id:
        raise ValidationError("Boundary-feasibility bundle session map source identity mismatch")
    if session_map.duration_ms != source.duration_ms:
        raise ValidationError("Boundary-feasibility bundle session map duration mismatch")
    if abs(annotations.source_duration_ms - source.duration_ms) > 1_000:
        raise ValidationError("Boundary-feasibility bundle annotation duration mismatch")
    return dataset, annotations, case, session_map, source


def _sha256_json(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _recover_window_scout_provenance(
    config: AppConfig,
    session_id: str,
    session_map: SessionMap,
    source: SourceAsset,
) -> SessionMap:
    metadata = dict(session_map.scout_metadata)
    if metadata.get("window_prompt_version"):
        metadata.setdefault("scout_provenance_source", "session_map")
        return session_map.model_copy(update={"scout_metadata": metadata})

    paths = session_paths(config.storage.data_dir, session_id)
    request_entries: list[dict[str, Any]] = []
    gemini_artifact_present = False
    if paths.scout_windows_dir.is_dir():
        for request_path in sorted(paths.scout_windows_dir.glob("*/request_meta.json")):
            raw = read_json(request_path)
            if not isinstance(raw, dict):
                raise ValidationError("Window Scout request metadata is not a JSON object")
            request = raw.get("request")
            cache_key = raw.get("cache_key")
            if not isinstance(request, dict) or not isinstance(cache_key, str) or not cache_key:
                raise ValidationError("Window Scout request metadata is incomplete")
            if request.get("source_sha256") != source.sha256:
                raise ValidationError("Window Scout request metadata source identity mismatch")
            for key in ("model", "prompt_version"):
                if not isinstance(request.get(key), str) or not request.get(key):
                    raise ValidationError(f"Window Scout request metadata lacks {key}")
            request_entries.append({"cache_key": cache_key, "request": request})
            item_dir = request_path.parent
            gemini_artifact_present = gemini_artifact_present or (
                (item_dir / "cost.json").is_file()
                or (item_dir / "gemini_remote_file.json").is_file()
            )

    if not request_entries:
        metadata.setdefault("scout_provenance_source", "unknown")
        return session_map.model_copy(update={"scout_metadata": metadata})

    prompts = {str(item["request"]["prompt_version"]) for item in request_entries}
    models = {str(item["request"]["model"]) for item in request_entries}
    if len(prompts) != 1 or len(models) != 1:
        raise ValidationError(
            "Window Scout request metadata mixes prompt/model identities within one session"
        )
    backend = "gemini" if gemini_artifact_present else session_map.scout_backend
    if backend not in {"fake", "gemini"}:
        raise ValidationError("Window Scout request metadata backend cannot be recovered safely")
    metadata.update(
        {
            "backend": backend,
            "provider": backend,
            "model": next(iter(models)),
            "window_prompt_version": next(iter(prompts)),
            "window_request_set_fingerprint": _sha256_json(request_entries),
            "scout_provenance_source": "window_request_meta",
        }
    )
    return session_map.model_copy(
        update={
            "scout_backend": backend,
            "scout_metadata": metadata,
        }
    )


def _finalize_bundle_directory(temp_root: Path, target_root: Path) -> None:
    for attempt in range(5):
        try:
            temp_root.rename(target_root)
            return
        except PermissionError:
            if target_root.exists() or attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def pack_boundary_refinement_feasibility_bundle(
    session_id: str,
    dataset_path: Path,
    annotations_path: Path,
    config: AppConfig,
    *,
    output_dir: Path,
) -> BoundaryFeasibilityBundleResult:
    """Create a calibration-only JSON bundle that can be evaluated on another machine."""

    dataset, annotations, case, session_map, source = _load_case_inputs(
        session_id,
        dataset_path,
        annotations_path,
        config,
    )
    session_map = _recover_window_scout_provenance(config, session_id, session_map, source)
    target_root = output_dir.expanduser().resolve()
    if target_root.exists():
        raise ValidationError("Boundary-feasibility bundle output directory must not already exist")
    target_root.parent.mkdir(parents=True, exist_ok=True)
    temp_root = target_root.parent / f".{target_root.name}.partial-{uuid4().hex[:8]}"
    temp_root.mkdir(parents=False, exist_ok=False)
    try:
        annotation_rel = Path("annotations") / f"{annotations.case_id}.json"
        source_rel = Path("data") / "sessions" / session_id / "source.json"
        session_map_rel = Path("data") / "sessions" / session_id / "session_map.json"
        dataset_out = temp_root / "dataset.json"
        annotation_out = temp_root / annotation_rel
        source_out = temp_root / source_rel
        session_map_out = temp_root / session_map_rel
        feasibility_out = temp_root / "feasibility.json"

        safe_source_locator = (
            Path("C:/__game_highlight_finder_private_source_not_bundled__")
            / f"{source.source_id}.media"
        )
        safe_source = source.model_copy(update={"path": safe_source_locator})
        safe_annotations = annotations.model_copy(update={"source_path": None})
        safe_case = case.model_copy(
            update={
                "source_path": safe_source_locator,
                "annotation_path": annotation_rel,
                "result_path": None,
            }
        )
        safe_dataset = BenchmarkDataset.model_validate(
            {
                **dataset.model_dump(mode="python"),
                "cases": (safe_case,),
            }
        )

        atomic_write_json(dataset_out, safe_dataset.model_dump(mode="json"))
        atomic_write_json(annotation_out, safe_annotations.model_dump(mode="json"))
        atomic_write_json(source_out, model_json(safe_source))
        atomic_write_json(session_map_out, model_json(session_map))

        bundle_config = config.model_copy(
            update={
                "storage": config.storage.model_copy(
                    update={"data_dir": (temp_root / "data").resolve()}
                )
            }
        )
        feasibility, _ = run_boundary_refinement_feasibility(
            session_id,
            dataset_out,
            annotation_out,
            bundle_config,
            output_path=feasibility_out,
        )

        files = {
            "dataset.json": hash_file(dataset_out),
            annotation_rel.as_posix(): hash_file(annotation_out),
            source_rel.as_posix(): hash_file(source_out),
            session_map_rel.as_posix(): hash_file(session_map_out),
            "feasibility.json": hash_file(feasibility_out),
        }
        identity_fingerprint = session_map.scout_metadata.get(
            "scout_config_fingerprint"
        ) or session_map.scout_metadata.get("window_request_set_fingerprint")
        manifest = BoundaryFeasibilityBundleManifest(
            case_id=annotations.case_id,
            session_id=session_id,
            source_sha256=source.sha256,
            evaluation_policy_fingerprint=feasibility.evaluation_policy_fingerprint,
            diagnostic_verdict=feasibility.diagnostic_verdict,
            files=files,
            scout_backend=session_map.scout_backend,
            scout_model=session_map.scout_metadata.get("model"),
            scout_prompt_version=session_map.scout_metadata.get("window_prompt_version"),
            scout_provenance_source=session_map.scout_metadata.get(
                "scout_provenance_source", "unknown"
            ),
            scout_identity_fingerprint=identity_fingerprint,
        )
        atomic_write_json(temp_root / "bundle.json", manifest.model_dump(mode="json"))
        _finalize_bundle_directory(temp_root, target_root)
        return BoundaryFeasibilityBundleResult(
            root=target_root,
            manifest_path=target_root / "bundle.json",
            manifest=manifest,
            feasibility=feasibility,
        )
    finally:
        if temp_root.exists():
            shutil.rmtree(temp_root, ignore_errors=True)


__all__ = [
    "BOUNDARY_FEASIBILITY_BUNDLE_VERSION",
    "BoundaryFeasibilityBundleManifest",
    "BoundaryFeasibilityBundleResult",
    "pack_boundary_refinement_feasibility_bundle",
]
