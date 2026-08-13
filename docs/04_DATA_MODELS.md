# Data Models and Validation

## 1. Modeling conventions

M7 presentation artifacts remain separate from the authoritative `SessionMap`:
`reports/ranking.json` stores the ranking version, ordered candidate IDs,
best-of IDs, and compact entries; `reports/index.html` is a derived local view.
Legacy manifests gain `rank` and `report` records additively on load.

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

For M5's single Gemini request, the provider artifacts are
`scout/raw/gemini_response.json`, `gemini_request_meta.json`, and
`gemini_remote_file.json`. The response is a sanitized final-output envelope:
interaction ID, exact model/status, structured output text, bounded current
Interactions usage totals/modality breakdown, and separate visible/thinking
tokens, finish/safety state, and safe remote
file name/deletion state. Signed URIs and thought steps are never persisted.
`scout/cost.json` is a derived display artifact; SQLite remains authoritative.

### M3 canonical domain

The implemented M3 runtime models are `Session`, `Match`, `Candidate`, `Evidence`,
`ScoutResponse`, and `SessionMap`. `Candidate` stores distinct `score` (`0..10`)
and `confidence` (`0..1`) values, compact bounded evidence, and source-relative
integer-millisecond event/clip intervals. `Match.candidate_ids` preserves the
hierarchy while `SessionMap.candidates` remains a complete, quota-free library.
M3 canonicalization leaves `best_of_candidate_ids` empty; later presentation
stages may populate that reference list without changing canonical storage.
Raw Scout/provider IDs are never authoritative; canonical IDs use deterministic
semantic hashes. The generic taxonomy plus bounded `GAME_<PROFILE>_<CATEGORY>`
extension form rejects arbitrary unknown categories without requiring a global
enum edit for every future game profile.

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
output_rate                 # optional in catalog; required for non-zero output usage
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
status                    # RESERVED, IN_FLIGHT, SETTLED, RELEASED, AMBIGUOUS
estimated_usage_json
actual_usage_json
rate_snapshot_json
fx_rate_snapshot
estimated_cost_thb
actual_or_best_cost_thb
provider_request_id
```

Rates and FX are snapshots so old totals do not change when configuration changes.

Provider usage counts are untrusted and bounded per modality before they reach
cost arithmetic. If a non-zero output count arrives without an output-rate
snapshot, the quote fails closed instead of treating output as free. When
settlement proves actual cost exceeded its reservation, the SQLite ledger
persists a global safety hold and blocks new reservations until an explicit
acknowledgement/reconciliation is recorded.

Gemini's billable output dimension is `visible output tokens + thinking tokens`;
both raw fields remain separately auditable while M4 arithmetic uses their
bounded sum.

The implemented M4 ledger uses `RESERVED`, `IN_FLIGHT`, `SETTLED`, `RELEASED`,
and `AMBIGUOUS`. Active, in-flight, and ambiguous reservations remain budget
exposure until settlement or evidence-backed release; pricing and FX snapshots
are stored with each call.

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

Store ranking configuration/version and component scores. `best_of_candidate_ids`
is reserved for later presentation ranking; M3 leaves it empty while the library
retains all qualifying candidates.

## 8. Schema evolution

- Increment major schema version for breaking changes and minor version for additive fields.
- Readers reject unknown major versions.
- Migrations write new files atomically and retain backups or regenerate derived artifacts.
- Raw provider responses are immutable evidence; canonical results may be regenerated from them when parser logic changes without repeating the paid call, provided the prompt/schema contract remains compatible.

## 9. M6 persisted models

### ScoutWindow / WindowPlan

`ScoutWindow` stores stable ID/ordinal, full source identity, absolute half-open
bounds, before/after overlap, relative committed proxy path/hash, parent analysis
proxy hash, bounded signal-summary hash, provider cache key, status, and warnings.
`WindowPlan` validates start at zero, exact tail coverage, no gaps, deterministic
overlap, maximum duration, contiguous ordinals, and a hard item-count ceiling.

### Reconciled SessionMap

M6 retains the M3 `SessionMap` schema and enriches `source_window_ids`, warnings,
normalization actions, candidate-to-match references, and statistics. Match
fragments merge only with compatible interval/label/ordinal evidence. Conflicts
are diagnostic rather than silently forced. Candidate dedupe requires compatible
category and high interval overlap or endpoint jitter across overlapping window
lineage. IDs are regenerated after final source-time normalization.

### ExtractionManifest / ExtractionRecord

The extraction manifest binds every output to source ID/hash, requested integer
source interval, accurate/copy mode and accuracy class, output and thumbnail
paths/hashes, probed duration, tool identity, extraction-config fingerprint,
status, and bounded warnings/errors. `COMPLETED` items are reusable only while
all semantic inputs and artifact hashes still match. Partial work never counts
as an aggregate completed extraction stage.
