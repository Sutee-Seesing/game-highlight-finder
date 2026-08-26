# Pipeline, State, Cache, and Resume

## 1. Pipeline overview

```text
source (read-only)
  -> ingest/ffprobe
  -> proxy
  -> local signals [and optional transcript]
  -> Scout windows
  -> reconcile session/matches/candidates/stories
  -> extract candidates from source
  -> rank (local deterministic M7)
  -> report (local self-contained HTML)
```

Ranking and reporting are local. Reviewer is not a dependency for extraction or a valid report.

## 2. Stages and exit artifacts

| Stage | Paid | Key output | Dependencies |
|---|---:|---|---|
| `ingest` | No | `source.json`, environment snapshot | readable source, ffprobe |
| `proxy` | No | `proxy/analysis_proxy.mp4`, proxy metadata | ingest, FFmpeg |
| `local_signals` | No | activity/silence/scene metadata; optional transcript | ingest; proxy/audio as configured |
| `scout` | No in M3; yes when Gemini is explicitly enabled | raw provider response, validated canonical result, and `session_map.json` | proxy, signals; Fake fixture or one bounded Gemini request |
| `reconcile` | No | canonical `session_map.json`, `scout_results.json` | completed Scout windows |
| `extract` | No | candidate media + extraction manifest | reconcile, source present and verified |
| `reviewer` | Potentially | validated candidate reviews | extract, reviewer enabled, budget approval |
| `rank` | No | `reports/ranking.json` | canonical session map |
| `report` | No | `reports/index.html` and cache metadata | rank, extraction metadata, session cost |

If Scout is disabled or budget-blocked, the run stops successfully-with-attention at that boundary; it must not claim full analysis completion.

M7 ranking and reporting are local-only. Ranking uses stable score/confidence/
event-time/ID ordering and a best-of limit of three while retaining every
candidate. Reports are atomic offline HTML with escaped text, hash-verified
thumbnails, relative clip links, stage/warning diagnostics, and session-specific
ledger cost. Zero-candidate sessions are valid. `report.meta.json` records the
semantic cache key plus the published HTML version, SHA-256, and byte size;
manual edits, truncation, or corrupt metadata force a local rebuild. Report and
session-cost commands load persisted session semantics while retaining the
current data-directory locator. Provider-call output is based on observed
per-invocation activity, never on the configured backend alone.

In M3 the `scout` stage is deliberately offline. It persists the exact Fake Scout
response under `scout/raw/`, validates it as hostile input, and writes the
canonical `scout/canonical/scout_result.json` plus `session_map.json`. A malformed
response fails the stage while retaining the raw bytes for parser-only recovery.
The stage cache includes only source/proxy/signal identities and Scout fixture/
schema inputs; logging or storage settings do not invalidate M3.
M3 canonicalization does not rank candidates or populate `best_of_candidate_ids`;
presentation ranking remains a later stage.

With `scout.backend: gemini`, M5 keeps the same canonical boundary but replaces
the fixture with one explicitly authorized provider call. The local cost gate
quotes and reserves before the Files API upload; the adapter marks the ledger
`IN_FLIGHT` immediately before generation, settles visible/thinking usage on a
completed response, and marks a post-send failure `AMBIGUOUS`. Only the committed
analysis proxy is uploadable. The M5 cache key covers source/proxy/signal summary,
prompt/schema, model, billing mode, media resolution, thinking level, and the
local reserved thinking allowance; a
verified cache hit performs no upload or generation. Long-session window planning
and reconciliation are M6 work.

## 3. Stage state machine

Allowed persistent statuses:

- `PENDING`: known but never attempted.
- `RUNNING`: attempt started; includes run ID and heartbeat/start timestamp.
- `COMPLETED`: artifact committed and verified against its cache key.
- `FAILED`: attempt ended with a classified error and retryability flag.
- `SKIPPED`: intentionally disabled or not applicable, with reason.
- `BLOCKED_BUDGET`: paid request refused before dispatch.
- `STALE`: prior output exists but its cache key no longer matches.

Transitions:

```text
PENDING -> RUNNING -> COMPLETED
                   -> FAILED -> RUNNING
                   -> BLOCKED_BUDGET -> RUNNING
PENDING -> SKIPPED
COMPLETED -> STALE -> RUNNING
SKIPPED -> STALE -> RUNNING        (configuration changed)
RUNNING left by crash -> FAILED    (recovered on next invocation)
```

`RUNNING` is never automatically trusted after process death. On resume, if its run lock is absent/stale, mark the attempt failed as `INTERRUPTED`, remove only uncommitted temp files, and re-enter the stage.

Scout and extraction are composite stages. Each window/candidate has its own item state, so a failure does not repeat completed items.

## 4. Atomicity and manifests

Each stage follows:

1. Acquire a per-session lock.
2. Compute and persist the proposed cache key.
3. Mark the attempt `RUNNING` via atomic manifest replacement.
4. Write outputs into `tmp/<run_id>/`.
5. Validate and hash all outputs.
6. Atomically move completed files to final paths on the same volume.
7. Mark `COMPLETED` with output hashes and release the lock.

JSON is written to a temporary sibling, flushed, and atomically replaced. A completed stage with a missing/hash-mismatched artifact is changed to `STALE`, never silently regenerated under the old state.

## 5. Source identity and cache keys

At first ingest, compute:

- canonical path and file identity metadata;
- byte size and high-resolution modified time;
- a streaming SHA-256 of the full source;
- ffprobe metadata and selected stream IDs.

The full hash costs local I/O once but gives reliable identity for a permanent library and moved-source relinking. On routine resume, size/mtime provide a fast guard. Recompute the full hash if they change; refuse extraction until source identity is confirmed.

Every stage cache key is a SHA-256 over canonical JSON containing:

- schema/cache-key version;
- relevant upstream artifact hashes;
- only the configuration subset used by the stage;
- prompt/schema version for AI stages;
- resolved provider/model and relevant capability flags;
- application version/commit for logic that changes output;
- relevant external tool/model versions.

Do not hash irrelevant settings. For example, changing report colors must not invalidate Scout; changing pre-roll invalidates extraction/rank/report but not Scout.

## 6. Force and invalidation rules

`--force-stage scout` marks Scout and all semantic downstream stages stale, but retains old artifacts until replacements commit. `--force-stage report` regenerates only the report. For composite stages, a future `--force-item` can target a window/candidate.

Before execution, print the plan:

```text
ingest: cache hit
proxy: cache hit
local_signals: cache hit
scout: 4/5 windows cached; 1 request planned; projected 1.82 THB
reconcile: stale (Scout input changed)
extract: stale (candidate set may change)
reviewer: disabled
rank/report: stale
```

`--dry-run` performs local conservative Gemini estimation and quoting only; it
does not upload, instantiate the SDK, reserve a ledger call, or generate.

## 7. Long-video Scout strategy

Do not send a four-hour proxy as one request. Plan windows with a 300-second hard maximum and 30 seconds overlap. Shorter windows increase provider calls and cost, but provide tighter coverage and timestamp localization; they are not a claim that quality has passed validation.

Each window includes:

- source-relative `[start_ms, end_ms)`;
- proxy clip or provider clip offsets;
- compact local activity/transcript hints if available;
- instruction to return match fragments and highlight evidence in window-relative or absolute milliseconds (one canonical contract only);
- the same versioned response schema.

Reconciliation then:

1. Converts all times to source milliseconds using the stored transform.
2. Rejects non-finite, negative, reversed, implausibly long, or grossly out-of-window values.
3. Clamps small boundary errors to source/window duration and records corrections.
4. Joins match fragments crossing window overlap based on time continuity and boundary evidence.
5. Deduplicates candidates using temporal overlap/IoU, category compatibility, semantic evidence, and shared match.
6. Preserves alternative evidence rather than silently dropping it.
7. Builds story candidates when nearby events share a causal/setup-payoff relationship; V1 starts conservatively.
8. Assigns deterministic IDs after normalization.

The original raw response is retained for debugging, but only validated canonical output may drive extraction.

## 8. Clip boundary logic

For a simple candidate:

```text
desired_start = min(setup_start_ms, event_start_ms) - pre_roll_ms
desired_end   = max(payoff_end_ms, event_end_ms) + post_roll_ms
clip_start    = clamp(desired_start, 0, source_duration_ms)
clip_end      = clamp(desired_end, clip_start + minimum_duration, source_duration_ms)
```

For stories, union the related intervals before roll. Merge candidates only when rules pass configured maximum gap/duration and semantic relationship; temporal proximity alone is insufficient. Record the source evidence IDs used for the story.

Use half-open intervals `[start_ms, end_ms)` internally. Clamp twice: after model normalization and immediately before command generation. FFmpeg receives decimal seconds derived from integer milliseconds without floating-point accumulation.

## 9. Failures and retries

- Classify errors as configuration, dependency, source, validation, transient provider, permanent provider, budget, storage, or internal.
- Retry only transient provider/network errors with bounded exponential backoff and jitter.
- Never retry a paid generation blindly when dispatch outcome is unknown. First query provider state if possible; otherwise leave the reservation pending for reconciliation and require an explicit safe retry policy.
- Malformed/semantically invalid AI output gets at most one repair retry if separately budgeted; prefer structured output and local failure visibility.
- Preserve stderr tail, provider request ID, attempt count, and redacted context.
- A Reviewer failure does not invalidate Scout or extracted candidates.

## 10. Remote lifecycle

Local cache is authoritative. A provider upload record contains remote ID, content hash, created/expiry times, and deletion state. Reuse only when provider, project, content hash, purpose, and validity match. On success or terminal failure, attempt explicit deletion; if deletion fails, record it and show a privacy warning. Never depend on a remote file surviving a resume beyond its documented retention period.

## 11. Implemented M6 flow and resume rules

The explicit `--m6` flow is:

```text
ingest -> proxy -> local_signals -> window proxies -> window Scout
       -> reconcile -> derive clip bounds -> extract -> thumbnails
```

Window planning uses `[start_ms, end_ms)` with a maximum of 300,000 ms and a
30,000 ms overlap by default. The tail ends exactly at source duration and the
planner rejects non-forward progress or excessive window counts. Each window
directory commits `analysis_window.mp4`, `window.json`, bounded `signals.json`,
raw/canonical responses, and request metadata. Cache identity includes the
source, parent/window proxy, exact bounds, signal summary, provider/model,
billing/media/thinking settings, prompt/schema hashes, and output ceiling.

An existing valid raw response can be canonicalized again without generation.
A fully verified canonical response is a cache hit. Aggregate paid-window
preflight quotes all missing windows together and compares the sum with current
available exposure before an upload; cached windows are excluded. The M6 CLI
defaults to Fake Scout; Gemini window execution requires explicit opt-in and
uses the same per-window ledger/cache boundary validated by the bounded smoke.

The v18 window Scout prompt uses a detection-first coverage sweep, including the beginning,
middle, and end before a rescan: it captures concrete gameplay anchors before optional
social or reaction moments. Reveal/fight/engagement candidates begin at the first useful
setup and extend through the immediate shooting or outcome instead of collapsing to a
later banner-only result. The provider contract emits only the core event interval for
window candidates; optional setup/payoff timestamps remain supported by the canonical
domain for legacy data, while local clip derivation supplies bounded pre/post-roll context.
The v18 provider schema also forbids undeclared object keys and constrains window-relative
timestamps and scoring/confidence values to bounded ranges before local canonicalization. Its `score` is editorial
short-form potential and its `confidence` is detection/timestamp/evidence certainty.
The local v2 ranking preserves every canonical candidate and records these separately
as `short_form_score` and `detection_confidence`; the current short-form score is not
a virality prediction. Future Reviewer or publish-performance metrics may improve
ranking without changing detection capture.

After window reconciliation, the final `session_map.json` now preserves Scout provenance instead of
falling back to generic fake metadata: backend/provider, model, exact window prompt version, current
Scout config fingerprint, and window-plan hash are persisted. This identity is diagnostic evidence;
quality comparisons must not treat an older prompt revision as current v18 behavior.

Reconcile completes only when all expected windows have canonical results.
Extraction records each candidate independently and atomically. On interruption,
verified completed clip/thumbnail hashes are reused and only missing/incomplete
items retry. The aggregate extract stage is complete only when every candidate
record is complete. Accurate extraction is the default; copy mode is explicitly
labelled approximate because keyframes can shift boundaries.

## 12. M8A benchmark stages (local measurement only)

Benchmark evaluation is deliberately downstream of the completed M7 journey:

```text
completed session + private annotations -> evaluate -> atomic result JSON
dataset manifest + result JSONs       -> aggregate -> JSON + Markdown
```

These commands never resume an incomplete session and never call Scout or a provider.
They fail closed when source/annotation identity, SessionMap, required stage status,
extraction completeness, or annotation schema is invalid. The evaluator reads the
settled/active/ambiguous cost lifecycle from SQLite; unresolved exposure remains
explicit and is not reported as settled actual cost. The original source is hashed
and checked but is never copied into benchmark artifacts.

For one experiment, `highlight benchmark aggregate <dataset.json>` retains the
legacy workflow. For apples-to-apples model comparison, use a versioned comparison
manifest and `highlight benchmark compare <comparison.json>`. Every result set must
contain every dataset case exactly once. The loader blocks unknown/missing case IDs,
source or annotation revision changes, split/profile changes, policy mismatches,
and a result set that mixes semantic experiment configurations. Reports contain
labels, hashes, and metrics only; private paths, media, credentials, and raw
provider responses remain local.

### Candidate-local boundary refinement (v19 remediation)

The v19 path prepares a narrow context clip around an existing Scout candidate, then creates a 2x
slow-motion proxy with audio preserved. Local preparation cuts from the committed analysis proxy,
validates both derivatives with ffprobe, persists parent/derivative hashes plus the refinement plan,
and reuses only hash-valid cached artifacts. The refiner proxy runtime-probes H.264 encoders in
NVENC -> Intel QSV -> libx264 order instead of trusting FFmpeg registry presence.

The same strict prompt/schema/media contract is exercised by a provider-free fake refiner and by the
Gemini provider boundary. Real execution remains explicit: candidate-local `slowed.mp4` is the only
accepted upload, aggregate cost preflight runs before provider work, per-candidate ledger/cache
lifecycle is authoritative, and ambiguous post-dispatch calls are never regenerated automatically.
`highlight refine-boundaries SESSION_ID CANDIDATE_ID...` defaults to provider-free preflight; live
execution requires both `--execute` and a fresh `--allow-remote-upload`. The original
`session_map.json` is never overwritten.

Because boundary refinement cannot invent a highlight that Scout never detected, calibration now has
a provider-free feasibility gate: `highlight benchmark boundary-feasibility SESSION_ID --dataset ...
--annotations ...`. It is calibration-only and rejects validation/holdout cases. It reuses the
authoritative M8 temporal ruler, then reports strict matches, anchor-overlap coverage, refinement-
context reachability, MUST_CATCH detection gaps, and boundary headroom. Candidate IDs emitted by this
artifact are derived from calibration ground truth and are diagnostic only; they must never become a
production selection policy. This gate makes zero provider/API calls and is the required decision
point before spending on a boundary-refinement calibration experiment.
