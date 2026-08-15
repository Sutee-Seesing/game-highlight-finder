from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from game_highlight_finder.benchmark.evaluator import load_evaluation
from game_highlight_finder.benchmark.models import (
    AnnotatedHighlight,
    BenchmarkAnnotations,
    Importance,
    Modality,
)
from game_highlight_finder.cli import app
from game_highlight_finder.config import AppConfig, StorageConfig, ToolsConfig
from game_highlight_finder.pipeline.runner import analyze_v1_source
from game_highlight_finder.storage.atomic import atomic_write_json
from game_highlight_finder.storage.hashing import hash_file
from game_highlight_finder.storage.sessions import session_paths

pytestmark = pytest.mark.integration


def test_full_benchmark_evaluate_cli_is_offline_and_persists_metrics(
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    tmp_path: Path,
) -> None:
    config = AppConfig(
        storage=StorageConfig(data_dir=tmp_path / "library"),
        tools=ToolsConfig(ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path),
    )
    source_before = hash_file(tiny_video, source=True)
    analysis = analyze_v1_source(tiny_video, config)
    session_map = analysis.m6.session_map
    assert session_map is not None
    source = analysis.m6.ingest.source
    highlights = tuple(
        AnnotatedHighlight(
            annotation_id=f"truth-{index}",
            event_start_ms=candidate.event_start_ms,
            event_end_ms=candidate.event_end_ms,
            category=candidate.category,
            importance=(Importance.MUST_CATCH if index == 0 else Importance.WORTH_REVIEW),
            modality=(Modality.VISUAL if index == 0 else Modality.AUDIO),
        )
        for index, candidate in enumerate(session_map.candidates)
    )
    annotation_path = tmp_path / "library" / "benchmarks" / "annotations" / "synthetic.json"
    annotations = BenchmarkAnnotations(
        benchmark_id="m8-integration",
        case_id="synthetic-evaluate-01",
        source_sha256=source.sha256,
        source_duration_ms=source.duration_ms,
        game_profile="unknown",
        source_path=source.path,
        highlights=highlights,
    )
    atomic_write_json(annotation_path, annotations.model_dump(mode="json"))
    output_path = tmp_path / "library" / "benchmarks" / "results" / "synthetic.json"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(config.storage.data_dir),
            "benchmark",
            "evaluate",
            analysis.m6.ingest.session_id,
            "--annotations",
            str(annotation_path),
            "--output",
            str(output_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "provider calls: ZERO" in result.output
    evaluation = load_evaluation(output_path)
    assert evaluation.source_sha256 == source.sha256
    assert evaluation.annotation_sha256
    assert evaluation.evaluation_policy_fingerprint == evaluation.evaluation_policy.fingerprint()
    assert (
        evaluation.experiment.evaluator_policy_fingerprint
        == evaluation.evaluation_policy_fingerprint
    )
    assert evaluation.evaluation_fingerprint
    assert evaluation.counts.predictions == len(session_map.candidates)
    assert evaluation.primary_metrics.precision is not None or not session_map.candidates
    assert evaluation.importance_metrics
    assert evaluation.modality_metrics
    assert evaluation.boundary_metrics.matched_count == evaluation.counts.true_positives
    assert evaluation.duplicate_metrics.duplicate_prediction_count >= 0
    assert evaluation.review_metrics.candidate_review_ms >= 0
    assert evaluation.best_of_metrics.best_of_count >= 0
    assert evaluation.cost_metrics.call_count >= 0
    assert evaluation.runtime_metrics.source_duration_ms == source.duration_ms
    assert evaluation.storage_metrics.source_duration_ms == source.duration_ms
    assert session_paths(config.storage.data_dir, analysis.m6.ingest.session_id).source.is_file()
    assert hash_file(tiny_video, source=True) == source_before
