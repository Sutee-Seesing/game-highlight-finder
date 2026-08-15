# M8A benchmark schemas

The runtime source of truth for M8A schemas is the strict Pydantic model module
`game_highlight_finder.benchmark.models`:

- `BenchmarkDataset` / `BenchmarkCase`
- `BenchmarkAnnotations`, `AnnotatedMatch`, `AnnotatedHighlight`, `BoringInterval`
- `EvaluationPolicy` / `ExperimentIdentity`
- `BenchmarkEvaluation` / `BenchmarkAggregate`
- `BenchmarkResultRef` / `BenchmarkResultSet`
- `BenchmarkComparisonManifest`

All persisted documents carry a schema version, integer-millisecond half-open
intervals, bounded text/list fields, and fail-closed validation. The example JSON
documents under `examples/` contain placeholders only; private benchmark data belongs
under the configured data directory and must not be committed.

`EvaluationPolicy.semantic_payload()` is hashed as the canonical
`evaluation_policy_fingerprint`; `policy_version` alone is not sufficient for
comparison. A result set references one evaluation per dataset case for one
experiment. A comparison manifest requires identical case coverage, source and
annotation revision hashes, split/profile values, and policy identity across all
result sets. Examples include synthetic dataset, result-set, and comparison
manifests only; M8A does not contain gameplay or provider results.
