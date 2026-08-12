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
  -> optional Reviewer batches
  -> rank
  -> report
```

Ranking and reporting are local. Reviewer is not a dependency for extraction or a valid report.

## 2. Stages and exit artifacts

| Stage | Paid | Key output | Dependencies |
|---|---:|---|---|
| `ingest` | No | `source.json`, environment snapshot | readable source, ffprobe |
| `proxy` | No | `proxy/analysis_proxy.mp4`, proxy metadata | ingest, FFmpeg |
| `local_signals` | No | activity/silence/scene metadata; optional transcript | ingest; proxy/audio as configured |
| `scout` | No in M3 | raw Fake Scout response, validated canonical result, and `session_map.json` | proxy, signals; deterministic offline fixture |
| `reconcile` | No | canonical `session_map.json`, `scout_results.json` | completed Scout windows |
| `extract` | No | candidate media + extraction manifest | reconcile, source present and verified |
| `reviewer` | Potentially | validated candidate reviews | extract, reviewer enabled, budget approval |
| `rank` | No | ranking fields / best-of manifest | reconcile; reviewer optional |
| `report` | No | `reports/report.html` and thumbnails | rank, extraction metadata |

If Scout is disabled or budget-blocked, the run stops successfully-with-attention at that boundary; it must not claim full analysis completion.

In M3 the `scout` stage is deliberately offline. It persists the exact Fake Scout
response under `scout/raw/`, validates it as hostile input, and writes the
canonical `scout/canonical/scout_result.json` plus `session_map.json`. A malformed
response fails the stage while retaining the raw bytes for parser-only recovery.
The stage cache includes only source/proxy/signal identities and Scout fixture/
schema inputs; logging or storage settings do not invalidate M3.

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

`--dry-run` performs no writes beyond optional read-only provider token counting only if explicitly allowed; preferably it uses local conservative estimation.

## 7. Long-video Scout strategy

Do not send a four-hour proxy as one request. Plan windows from local signals and hard maximum duration, initially around 45 minutes with 30 seconds overlap. Exact defaults need benchmark validation.

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
