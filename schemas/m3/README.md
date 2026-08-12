# M3 JSON Schema snapshots

The Pydantic models in `src/game_highlight_finder/domain/models.py` remain the
runtime source of truth.  The deterministic `m3_schema_snapshots()` helper in
`game_highlight_finder.storage.schemas` emits snapshots for `Match`, `Candidate`,
`ScoutResponse`, and `SessionMap` without adding a schema-generation framework.

Consumers may persist those JSON objects as versioned fixtures.  Unknown major
schema versions are rejected by the runtime models.
