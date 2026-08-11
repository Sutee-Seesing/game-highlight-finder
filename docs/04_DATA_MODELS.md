# Data Models and Validation

## 1. Modeling conventions

- Pydantic models are the runtime source of truth; JSON Schema snapshots are versioned for fixtures and provider structured output.
- Every persisted document has `schema_version`, `created_at`, and producer version.
- Time is an integer number of milliseconds from the original source timeline. Use half-open intervals `[start_ms, end_ms)`.
- Scores are decimal numbers in `[0, 10]`; confidence is `[0, 1]`. Keep them separate.
- IDs are deterministic where possible and never derived from mutable display text alone.
- Unknown is represented explicitly (`null` or an enum), not invented.

## 2. Core models

### SourceAsset

```text
source_id
path                    # absolute local path; may later be relinked
sha256
size_bytes
mtime_ns
duration_ms
container
video_stream            # codec, width, height, avg/r frame rate, pix fmt, time base
audio_streams[]          # codec, channels, sample rate, language/index
selected_video_stream
selected_audio_stream
timestamp_origin
probe_tool_version
warnings[]
```

Validation: exactly one selected video stream; duration > 0; dimensions and rates plausible; source hash is a 64-character SHA-256; selected stream IDs exist.

### Session

```text
session_id
source_id
game_profile
title
recorded_at              # optional
created_at
resolved_config_hash
status                   # derived summary, not a substitute for stage states
stage_manifest_path
```

### Match

```text
match_id
ordinal                  # nullable if segmentation is uncertain
start_ms
end_ms
label
confidence
boundary_evidence[]
source_window_ids[]
warnings[]
```

Matches may overlap slightly during raw Scout output but canonical matches should not overlap after reconciliation unless the selected game profile explicitly permits nesting. Unassigned candidates belong to a synthetic `UNASSIGNED` group, not a fabricated match interval.

### Candidate

```text
candidate_id
match_id                 # nullable / UNASSIGNED
kind                     # MOMENT or STORY
category                 # required enum
event_start_ms
event_end_ms
setup_start_ms           # optional
payoff_end_ms            # optional
score
confidence
reason
evidence[]
source_window_ids[]
related_candidate_ids[]
clip_start_ms             # derived after roll/clamp
clip_end_ms               # derived after roll/clamp
normalization_actions[]
review                    # optional
rank                      # optional
```

`evidence` should be compact, such as visual event, spoken reaction, game-state change, and why the payoff is understandable. Do not store chain-of-thought.

### Review

```text
candidate_id
hook_score
setup_payoff_score
standalone_score
reaction_score
shareability_score
repetition_group          # optional
recommendation            # KEEP, MAYBE, REJECT, MERGE
merge_with_ids[]
confidence
reason
provider_run_id
```

Reviewer suggestions do not destructively rewrite candidates. A deterministic reconciliation step creates a new candidate/story revision and preserves lineage.

### SessionMap

```text
session_id
duration_ms
summary
matches[]
candidates[]
best_of_candidate_ids[]
statistics
warnings[]
```

The best-of list references candidates; it never contains the only copy.

## 3. Stage and artifact models

### StageRecord

```text
stage_name
status
attempts[]
cache_key
started_at
completed_at
input_artifacts[]         # path + sha256
output_artifacts[]        # path + sha256 + size
item_states{}             # Scout windows / extraction candidates
error                     # classified and redacted
reason                    # skip/budget/stale reason
```

### ProviderRun

```text
provider_run_id
provider
model
stage
session_id
window_or_batch_id
prompt_version
schema_version
remote_asset_id           # provider identifier, not secret
request_fingerprint
provider_request_id
started_at
completed_at
usage_estimate
actual_usage
response_artifact
status
```

### ScoutWindowResult

```text
window_id
window_start_ms
window_end_ms
session_observations[]
match_fragments[]
candidate_fragments[]
```

Provider output should use a flatter bounded schema than the final domain model. Local code supplies IDs, absolute transforms, clip boundaries, and ranking; the model does not.

## 4. Cost models

### PricingEntry

```text
provider
model
pricing_tier
effective_at
retrieved_at
source_url
currency                  # normally USD
input_rates_by_modality
cached_input_rate
output_rate
request_fees
notes
```

### CostEvent

```text
event_id
reservation_id
occurred_at
billing_month             # Asia/Bangkok calendar month by default
session_id
stage
provider
model
request_fingerprint
status                    # RESERVED, COMMITTED, RELEASED, RECONCILE_REQUIRED
estimated_usage_json
actual_usage_json
rate_snapshot_json
fx_rate_snapshot
estimated_cost_thb
actual_or_best_cost_thb
provider_request_id
```

Rates and FX are snapshots so old totals do not change when configuration changes.

## 5. Semantic validation pipeline

Provider structured output is only syntactically constrained. Apply these checks in order:

1. Reject oversized response bodies before parsing.
2. Parse JSON with no permissive code execution or object hooks.
3. Validate schema version, types, enum values, string lengths, and collection limits.
4. Reject NaN/infinity and booleans masquerading as integers.
5. Transform window-local time to source time exactly once.
6. Reject reversed/empty intervals; clamp only bounded errors and record every clamp.
7. Ensure candidate core events lie in or near the declared Scout window.
8. Ensure setup <= event start < event end <= payoff when optional arc fields exist.
9. Enforce configured candidate/clip maximum duration unless a story exception is explicit.
10. Check match assignment and source duration.
11. Deduplicate and reconcile overlaps deterministically.
12. Generate local IDs and clip boundaries.

If too many corrections are required or a response violates critical invariants, mark that window invalid rather than extracting speculative clips.

## 6. Candidate overlap and story rules

Calculate temporal intersection-over-union and containment on core intervals. Likely duplicates share a match, compatible category/evidence, and either high IoU or near-identical event centers. Keep the highest-confidence canonical candidate and retain all contributing fragment IDs.

A story merge requires more than overlap:

- the first moment supplies understandable setup;
- later moment supplies escalation/payoff;
- the gap is below a configured maximum;
- total clip duration is below a configured story maximum;
- the relationship is supported by Scout/Reviewer evidence or an explicit local rule.

V1 should favor under-merging over creating long, incoherent clips. Manual future merge/split annotations should be modeled as non-destructive revisions.

## 7. Ranking

Ranking is deterministic and explainable. Initial ranking can combine normalized Scout score, confidence, Reviewer score when present, standalone clarity, and penalties for duplication or excessive duration. Missing Reviewer results must not become zero; use a Scout-only formula and label the ranking basis.

Store ranking configuration/version and component scores. `best_of_candidate_ids` has a configurable maximum for presentation, while the library retains all qualifying candidates.

## 8. Schema evolution

- Increment major schema version for breaking changes and minor version for additive fields.
- Readers reject unknown major versions.
- Migrations write new files atomically and retain backups or regenerate derived artifacts.
- Raw provider responses are immutable evidence; canonical results may be regenerated from them when parser logic changes without repeating the paid call, provided the prompt/schema contract remains compatible.

