# M8A benchmark schemas

The runtime source of truth for M8A schemas is the strict Pydantic model module
`game_highlight_finder.benchmark.models`:

- `BenchmarkDataset` / `BenchmarkCase`
- `BenchmarkAnnotations`, `AnnotatedMatch`, `AnnotatedHighlight`, `BoringInterval`
- `EvaluationPolicy` / `ExperimentIdentity`
- `BenchmarkEvaluation` / `BenchmarkAggregate`

All persisted documents carry a schema version, integer-millisecond half-open
intervals, bounded text/list fields, and fail-closed validation. The example JSON
documents under `examples/` contain placeholders only; private benchmark data belongs
under the configured data directory and must not be committed.
