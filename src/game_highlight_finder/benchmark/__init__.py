"""Provider-neutral M8A benchmark and annotation foundation."""

from game_highlight_finder.benchmark.aggregate import (
    aggregate_dataset,
    aggregate_evaluations,
    render_markdown,
)
from game_highlight_finder.benchmark.evaluator import (
    deterministic_temporal_match,
    evaluate_session,
    validate_annotations_file,
)
from game_highlight_finder.benchmark.models import (
    ANNOTATION_SCHEMA_VERSION,
    BENCHMARK_SCHEMA_VERSION,
    EVALUATION_POLICY_VERSION,
    EVALUATOR_VERSION,
    AnnotatedHighlight,
    AnnotatedMatch,
    BenchmarkAnnotations,
    BenchmarkCase,
    BenchmarkDataset,
    BenchmarkEvaluation,
    BoringInterval,
    EvaluationPolicy,
    ExperimentIdentity,
    Importance,
    Modality,
)
from game_highlight_finder.benchmark.template import create_annotation_template

__all__ = [
    "ANNOTATION_SCHEMA_VERSION",
    "BENCHMARK_SCHEMA_VERSION",
    "EVALUATION_POLICY_VERSION",
    "EVALUATOR_VERSION",
    "AnnotatedHighlight",
    "AnnotatedMatch",
    "BenchmarkAnnotations",
    "BenchmarkCase",
    "BenchmarkDataset",
    "BenchmarkEvaluation",
    "BoringInterval",
    "EvaluationPolicy",
    "ExperimentIdentity",
    "Importance",
    "Modality",
    "aggregate_dataset",
    "aggregate_evaluations",
    "create_annotation_template",
    "deterministic_temporal_match",
    "evaluate_session",
    "render_markdown",
    "validate_annotations_file",
]
