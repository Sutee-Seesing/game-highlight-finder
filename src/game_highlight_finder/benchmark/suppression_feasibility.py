"""Provider-free candidate-level false-positive suppression diagnostics for calibration data."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from game_highlight_finder.benchmark.boundary_feasibility import BoundaryRefinementFeasibility
from game_highlight_finder.config import AppConfig
from game_highlight_finder.domain.models import LocalSignalsArtifact, SessionMap, Sha256
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.hashing import hash_file
from game_highlight_finder.storage.sessions import session_paths, source_from_artifact

CANDIDATE_SUPPRESSION_FEASIBILITY_VERSION = "candidate-suppression-feasibility-v1"
SuppressionVerdict = Literal[
    "AUDIO_PEAK_OVER_LOUDNESS_HEADROOM",
    "AUDIO_PEAK_DB_HEADROOM",
    "AUDIO_MEAN_DB_HEADROOM",
    "NO_EXISTING_LOCAL_SIGNAL_HEADROOM",
]
Adjudication = Literal["POSITIVE", "CONFIRMED_NEGATIVE"]


class CandidateSuppressionFeature(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(min_length=1, max_length=128)
    adjudication: Adjudication
    event_start_ms: int = Field(ge=0)
    event_end_ms: int = Field(gt=0)
    score: float = Field(ge=0, le=10)
    confidence: float = Field(ge=0, le=1)
    audio_coverage_ms: int = Field(ge=0)
    audio_active_fraction: float | None = Field(default=None, ge=0, le=1)
    audio_mean_db: float | None = Field(default=None, ge=-200, le=20)
    audio_peak_db: float | None = Field(default=None, ge=-200, le=20)
    audio_peak_over_loudness_db: float | None = Field(default=None, ge=-200, le=220)


class CandidateSuppressionFeasibility(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    version: str = CANDIDATE_SUPPRESSION_FEASIBILITY_VERSION
    benchmark_id: str = Field(min_length=1, max_length=128)
    case_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    source_sha256: Sha256
    boundary_feasibility_sha256: Sha256
    scout_backend: str = Field(min_length=1, max_length=64)
    scout_model: str | None = Field(default=None, max_length=128)
    scout_prompt_version: str | None = Field(default=None, max_length=64)
    candidate_count: int = Field(ge=0)
    protected_positive_count: int = Field(ge=0)
    confirmed_negative_count: int = Field(ge=0)
    score_confidence_threshold_suppression_headroom: bool
    protected_positive_min_audio_peak_db: float | None = Field(default=None, ge=-200, le=20)
    audio_peak_db_threshold_rejectable_negative_candidate_ids: tuple[str, ...] = ()
    audio_peak_db_threshold_suppression_headroom: bool = False
    protected_positive_min_audio_peak_over_loudness_db: float | None = Field(
        default=None, ge=-200, le=220
    )
    audio_peak_over_loudness_threshold_rejectable_negative_candidate_ids: tuple[str, ...] = ()
    audio_peak_over_loudness_threshold_suppression_headroom: bool = False
    protected_positive_min_audio_mean_db: float | None = Field(default=None, ge=-200, le=20)
    audio_mean_db_threshold_rejectable_negative_candidate_ids: tuple[str, ...] = ()
    audio_mean_db_threshold_suppression_headroom: bool = False
    diagnostic_verdict: SuppressionVerdict
    candidates: tuple[CandidateSuppressionFeature, ...] = ()
    warning: str = (
        "Calibration diagnostic only. Thresholds are derived from reviewed calibration candidates; "
        "do not promote them to production defaults or evaluate them against a revealed validation "
        "holdout. Validate any promising local feature on additional calibration data first."
    )
    provider_calls: Literal[0] = 0


def _overlap_ms(left_start: int, left_end: int, right_start: int, right_end: int) -> int:
    return max(0, min(left_end, right_end) - max(left_start, right_start))


def _audio_features(
    start_ms: int,
    end_ms: int,
    signals: LocalSignalsArtifact,
) -> tuple[int, float | None, float | None, float | None]:
    coverage_ms = 0
    active_ms = 0
    mean_weighted_sum = 0.0
    mean_weight_ms = 0
    peaks: list[float] = []
    for interval in signals.audio_activity:
        overlap = _overlap_ms(start_ms, end_ms, interval.start_ms, interval.end_ms)
        if not overlap:
            continue
        coverage_ms += overlap
        if interval.active:
            active_ms += overlap
        if interval.mean_db is not None:
            mean_weighted_sum += interval.mean_db * overlap
            mean_weight_ms += overlap
        if interval.peak_db is not None:
            peaks.append(interval.peak_db)
    active_fraction = active_ms / coverage_ms if coverage_ms else None
    mean_db = mean_weighted_sum / mean_weight_ms if mean_weight_ms else None
    peak_db = max(peaks) if peaks else None
    return coverage_ms, active_fraction, mean_db, peak_db


def _lower_bound_headroom(
    rows: tuple[CandidateSuppressionFeature, ...],
    *,
    field: Literal["audio_peak_db", "audio_mean_db", "audio_peak_over_loudness_db"],
) -> tuple[float | None, tuple[str, ...]]:
    positives = [getattr(row, field) for row in rows if row.adjudication == "POSITIVE"]
    if not positives or any(value is None for value in positives):
        return None, ()
    threshold = min(value for value in positives if value is not None)
    rejectable = tuple(
        row.candidate_id
        for row in rows
        if row.adjudication == "CONFIRMED_NEGATIVE"
        and (getattr(row, field) is None or getattr(row, field) < threshold)
    )
    return threshold, rejectable


def assess_candidate_suppression_feasibility(
    session_map: SessionMap,
    signals: LocalSignalsArtifact,
    boundary: BoundaryRefinementFeasibility,
    *,
    boundary_feasibility_sha256: str,
) -> CandidateSuppressionFeasibility:
    """Measure whether already-computed local audio features separate reviewed candidates."""

    if not boundary.semantic_quality_applicable:
        raise ValidationError("Candidate suppression feasibility requires semantic Scout evidence")
    if not boundary.false_positive_suppression_safe:
        raise ValidationError(
            "Candidate suppression feasibility requires completed candidate adjudication with "
            "confirmed negatives"
        )
    if boundary.human_review_required_candidate_ids:
        raise ValidationError("Candidate suppression feasibility still has human-review candidates")
    if boundary.session_id != session_map.session_id:
        raise ValidationError("Candidate suppression feasibility session identity mismatch")
    if abs(signals.source_duration_ms - session_map.duration_ms) > 1_000:
        raise ValidationError("Candidate suppression local-signal duration mismatch")

    positive_ids = set(boundary.ground_truth_derived_candidate_ids)
    negative_ids = set(boundary.confirmed_negative_candidate_ids)
    adjudicated_ids = positive_ids | negative_ids
    candidate_ids = {candidate.candidate_id for candidate in session_map.candidates}
    if adjudicated_ids != candidate_ids:
        raise ValidationError(
            "Candidate suppression adjudication does not cover exactly the current session "
            "candidates"
        )

    rows: list[CandidateSuppressionFeature] = []
    for candidate in session_map.candidates:
        coverage_ms, active_fraction, mean_db, peak_db = _audio_features(
            candidate.event_start_ms,
            candidate.event_end_ms,
            signals,
        )
        peak_over_loudness = (
            peak_db - signals.overall_loudness_lufs
            if peak_db is not None and signals.overall_loudness_lufs is not None
            else None
        )
        rows.append(
            CandidateSuppressionFeature(
                candidate_id=candidate.candidate_id,
                adjudication=(
                    "POSITIVE" if candidate.candidate_id in positive_ids else "CONFIRMED_NEGATIVE"
                ),
                event_start_ms=candidate.event_start_ms,
                event_end_ms=candidate.event_end_ms,
                score=candidate.score,
                confidence=candidate.confidence,
                audio_coverage_ms=coverage_ms,
                audio_active_fraction=active_fraction,
                audio_mean_db=mean_db,
                audio_peak_db=peak_db,
                audio_peak_over_loudness_db=peak_over_loudness,
            )
        )
    ordered_rows = tuple(rows)
    peak_threshold, peak_rejectable = _lower_bound_headroom(
        ordered_rows,
        field="audio_peak_db",
    )
    relative_peak_threshold, relative_peak_rejectable = _lower_bound_headroom(
        ordered_rows,
        field="audio_peak_over_loudness_db",
    )
    mean_threshold, mean_rejectable = _lower_bound_headroom(
        ordered_rows,
        field="audio_mean_db",
    )
    if relative_peak_rejectable:
        verdict: SuppressionVerdict = "AUDIO_PEAK_OVER_LOUDNESS_HEADROOM"
    elif peak_rejectable:
        verdict = "AUDIO_PEAK_DB_HEADROOM"
    elif mean_rejectable:
        verdict = "AUDIO_MEAN_DB_HEADROOM"
    else:
        verdict = "NO_EXISTING_LOCAL_SIGNAL_HEADROOM"

    return CandidateSuppressionFeasibility(
        benchmark_id=boundary.benchmark_id,
        case_id=boundary.case_id,
        session_id=boundary.session_id,
        source_sha256=boundary.source_sha256,
        boundary_feasibility_sha256=boundary_feasibility_sha256,
        scout_backend=boundary.scout_backend,
        scout_model=boundary.scout_model,
        scout_prompt_version=boundary.scout_prompt_version,
        candidate_count=len(ordered_rows),
        protected_positive_count=len(positive_ids),
        confirmed_negative_count=len(negative_ids),
        score_confidence_threshold_suppression_headroom=(
            boundary.score_confidence_threshold_suppression_headroom
        ),
        protected_positive_min_audio_peak_db=peak_threshold,
        audio_peak_db_threshold_rejectable_negative_candidate_ids=peak_rejectable,
        audio_peak_db_threshold_suppression_headroom=bool(peak_rejectable),
        protected_positive_min_audio_peak_over_loudness_db=relative_peak_threshold,
        audio_peak_over_loudness_threshold_rejectable_negative_candidate_ids=(
            relative_peak_rejectable
        ),
        audio_peak_over_loudness_threshold_suppression_headroom=bool(relative_peak_rejectable),
        protected_positive_min_audio_mean_db=mean_threshold,
        audio_mean_db_threshold_rejectable_negative_candidate_ids=mean_rejectable,
        audio_mean_db_threshold_suppression_headroom=bool(mean_rejectable),
        diagnostic_verdict=verdict,
        candidates=ordered_rows,
    )


def run_candidate_suppression_feasibility(
    session_id: str,
    boundary_feasibility_path: Path,
    config: AppConfig,
    *,
    output_path: Path | None = None,
) -> tuple[CandidateSuppressionFeasibility, Path]:
    """Load one reviewed calibration session and persist provider-free suppression diagnostics."""

    resolved_boundary = boundary_feasibility_path.expanduser().resolve()
    if not resolved_boundary.is_file():
        raise ValidationError("Candidate suppression boundary-feasibility artifact does not exist")
    try:
        boundary = BoundaryRefinementFeasibility.model_validate(read_json(resolved_boundary))
    except Exception as exc:
        raise ValidationError(
            "Candidate suppression boundary-feasibility artifact is invalid", hint=str(exc)
        ) from exc
    if boundary.session_id != session_id:
        raise ValidationError("Candidate suppression boundary-feasibility session mismatch")

    paths = session_paths(config.storage.data_dir, session_id)
    signals_path = paths.signals_dir / "activity.json"
    if not paths.source.is_file() or not paths.session_map.is_file() or not signals_path.is_file():
        raise ValidationError(
            "Candidate suppression requires committed source, session map, and local signals"
        )
    source = source_from_artifact(paths.source)
    if source.sha256 != boundary.source_sha256:
        raise ValidationError("Candidate suppression source identity mismatch")
    try:
        session_map = SessionMap.model_validate(read_json(paths.session_map))
        signals = LocalSignalsArtifact.model_validate(read_json(signals_path))
    except Exception as exc:
        raise ValidationError(
            "Candidate suppression local artifact is invalid", hint=str(exc)
        ) from exc
    if session_map.source_id != source.source_id:
        raise ValidationError("Candidate suppression session-map source identity mismatch")

    result = assess_candidate_suppression_feasibility(
        session_map,
        signals,
        boundary,
        boundary_feasibility_sha256=hash_file(resolved_boundary),
    )
    target = (
        output_path.expanduser().resolve()
        if output_path is not None
        else config.storage.data_dir.resolve()
        / "benchmarks"
        / "private"
        / "suppression"
        / f"{boundary.case_id}.suppression-feasibility.json"
    )
    atomic_write_json(target, result.model_dump(mode="json"))
    return result, target


__all__ = [
    "CANDIDATE_SUPPRESSION_FEASIBILITY_VERSION",
    "CandidateSuppressionFeasibility",
    "CandidateSuppressionFeature",
    "assess_candidate_suppression_feasibility",
    "run_candidate_suppression_feasibility",
]
