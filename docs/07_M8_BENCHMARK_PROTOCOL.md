# M8A Benchmark Protocol

M8A builds the local benchmark ruler before any real-gameplay provider tuning. It
does not run Gemini, Qwen, GLM, DeepSeek, or any other provider. The evaluator
consumes immutable source metadata, a completed `SessionMap`, optional ranking and
extraction artifacts, the authoritative local cost ledger, and private human
annotations. It never calls Scout, uploads media, or changes inference cache keys.

## Private data boundary

Gameplay recordings, candidate clips, screenshots, audio, annotations, provider
responses, SQLite ledgers, credentials, and absolute personal paths stay under the
configured local data directory. They are never committed. A practical layout is:

```text
<data_dir>/benchmarks/
  datasets/
  annotations/
  results/
  reports/
```

Tracked examples contain placeholders only. A shareable aggregate report contains
case IDs and hashes, not source paths or media.

## Dataset and split policy

`BenchmarkDataset` is a strict versioned JSON manifest. Each `BenchmarkCase` names a
private source path, expected SHA-256, annotation path, game profile, tags, and one
split: `calibration` or `validation`.

Calibration cases may later guide tuning. Validation cases must remain untouched while
choosing prompts, thresholds, proxy settings, window policy, or profiles. Aggregates
always expose separate calibration and validation groups plus a count-weighted
`combined` group. Percentages are recomputed from underlying counts; they are never
averaged across recordings of different lengths.

### Full policy identity

The policy version is a label, not a complete ruler. The persisted
`EvaluationPolicy.semantic_payload()` contains exactly `schema_version`,
`policy_version`, `event_iou_threshold`, and `boundary_tolerance_ms`. Its canonical
JSON uses sorted keys and compact separators and is hashed to
`evaluation_policy_fingerprint`. Dataset, evaluation, result-set, and aggregate
artifacts persist this fingerprint. Same-version policies with different IoU or
boundary values, and different versions with the same numbers, are incompatible
and fail closed. A legacy manifest with no policy is migrated only when its version
is exactly `m8-eval-v1`, to the historical `0.25 / 3000 ms` values.

## Annotation guide

Create a template without provider calls:

```powershell
highlight benchmark template "D:\Recordings\session.mp4" `
  --game-profile meccha_chameleon `
  --case-id meccha-cal-01
```

The command hashes and probes the source locally and writes an empty valid annotation
document. It never modifies the video and never commits the result. Fill annotations
in a copy, then validate:

```powershell
highlight benchmark validate "<data_dir>\benchmarks\annotations\meccha-cal-01.json"
```

For the private owner workflow, open the empty template in the tiny local helper:

```powershell
highlight benchmark annotate "<data_dir>\benchmarks\annotations\meccha-cal-01.json"
```

The helper binds only to `127.0.0.1`, serves the exact SHA-verified source read-only
with byte ranges, and has no external resources, analytics, provider SDK, model
assistance, or network calls. Timestamp buttons persist integer milliseconds only when
the owner explicitly saves. Server-side `BenchmarkAnnotations` validation, atomic
writes, private meaningful-data backups, and a separate human-review sidecar protect
the benchmark truth. `VALID JSON` and `HUMAN_REVIEWED` are distinct from readiness for
any provider benchmark. Dataset overview is intentionally not implemented in this
tiny milestone; the single-case command keeps the annotation surface small.

For private manual review copies, use the accepted dataset manifest:

```powershell
highlight benchmark make-review-proxies "<data_dir>\benchmarks\datasets\dataset.json"
```

This is a local-only deterministic transcode into the private review-proxy directory.
The normal profile requires RTX/NVENC `h264_nvenc` (720p maximum, 30 fps maximum,
1000 kbps video, 96 kbps AAC); `--small` is an explicit compact profile and CPU
encoding requires `--allow-cpu-fallback`. The helper never trims, crops, speeds up,
or mutates the authoritative source. It verifies source SHA-256 before/after encoding,
retains audio, validates MP4/H.264 output and a strict 250 ms duration delta, and
persists a private cache manifest. Review-proxy timestamps are convenience guidance;
annotation JSON remains tied to the authoritative benchmark source and integer-
millisecond timeline. M8B2 remains **NOT RUN**.

All times are integer milliseconds on the original source timeline. Every interval is
half-open, `[start_ms, end_ms)`, and must be inside the source. Ground-truth errors
fail closed; the tool never silently clamps a hand-entered boundary.

Stable IDs are required across revisions. Use a different `annotation_version` when
the human ground truth is intentionally revised. A highlight may reference an
annotated match, but match annotation is optional when the session is difficult to
segment.

### Highlight importance

- `MUST_CATCH`: a moment that must appear in a useful shortlist.
- `WORTH_REVIEW`: worthwhile but less essential.
- `OPTIONAL`: interesting context; never let it hide misses in the primary product metric.

### Modality

Use `VISUAL`, `AUDIO`, `VISUAL_AND_AUDIO`, or `UNKNOWN`. A visual kill and a funny
voice reaction are different evaluation slices even when both are temporally found.

### Boring intervals

Mark intentionally uninteresting intervals with `boring_intervals`. A zero-candidate
boring session is valid. These intervals measure candidates overlapping boring footage,
false positives per source hour, and review time spent inside boring footage.

## Matching policy

The persisted policy version is `m8-eval-v1`:

- event IoU threshold: `0.25`;
- boundary tolerance: `3000` ms;
- one prediction can match at most one annotated highlight, and vice versa;
- qualifying pairs are ordered by IoU descending, combined boundary error ascending,
  prediction score descending, then stable IDs;
- category is not required for the primary temporal match; category correctness is a
  secondary confusion metric.

A pair qualifies with the configured IoU, or with both boundary errors within tolerance
and no larger-than-tolerance gap. If this policy changes, increment its version; never
change the number only after observing one provider's scores.

## Evaluation workflow

Run only against an already completed local session:

```powershell
highlight benchmark evaluate <session-id> `
  --annotations "<data_dir>\benchmarks\annotations\meccha-cal-01.json" `
  --split calibration
```

The evaluator verifies source hash and duration, SessionMap identity, completed
Scout/reconcile stages, extraction completeness when candidates exist, and annotation
hash. It does not call `resume` automatically. Results are atomic JSON artifacts and
become stale when annotation bytes change. Annotation bytes never participate in
Scout request fingerprints, paid provider cache keys, SessionMap construction, or
extraction cache keys.

The machine-readable result retains raw matched-pair boundary measurements and
diagnostic lists: missed annotations, extra candidates, matched pairs, and duplicate
candidates. It also records the immutable experiment identity (provider/model,
billing/media/thinking settings, prompt/schema/canonicalization versions, windows,
proxy/signal/extraction/ranking fingerprints, source hash, annotation hash, and
evaluation policy version).

## Metrics

Per-case and aggregate results retain raw counts and report:

- precision, recall, F1, and MUST/WORTH/OPTIONAL recall;
- VISUAL/AUDIO/VISUAL_AND_AUDIO/UNKNOWN recall;
- start/end absolute error, IoU median, and p90 boundary error;
- duplicate count/rate and explicit duplicate diagnostics;
- union-duration review ratio (overlapping clips count once);
- Best-of count, MUST/WORTH found, Best-of precision and useful-event recall;
- boring-interval false-positive behavior;
- secondary category confusion;
- manual match segmentation metrics, or explicit N/A when matches are absent;
- settled/reserved/in-flight/ambiguous cost, THB per source hour, and THB per true
  positive. Ambiguous exposure is never presented as settled actual cost;
- durable runtime timing where stage timestamps exist, and generated storage bytes
  excluding the original source.

There is no single magic quality score. For a product decision, the priority order is
quality/fun first, then MUST_CATCH recall, then precision and review burden, then cost
per source hour, and finally runtime/storage. Modality, timing, boring-footage behavior,
and cost exposure remain required slices rather than a license to trade away useful
highlights for a cheaper score.

Aggregate an existing private dataset after its evaluations exist:

```powershell
highlight benchmark aggregate "<data_dir>\benchmarks\datasets\m8.json"
```

This writes aggregate JSON and a privacy-safe Markdown comparison table. A case's
annotation hash, source hash, split, profile, and benchmark ID must match the dataset
manifest; mismatches block comparison.

## Multi-experiment comparison

Ground truth is not duplicated per model. A dataset owns cases and annotations;
each experiment owns a `BenchmarkResultSet` containing one evaluation reference per
case. A comparison manifest names the dataset and two or more result-set manifests:

```text
<data_dir>/benchmarks/
  datasets/m8-initial.json
  annotations/<case-id>.json
  experiments/
    gemini-3.5-fl/manifest.json
    gemini-2.5-fl/manifest.json
  comparisons/baseline-models.json
  reports/baseline-models.md
```

Run it locally with:

```powershell
highlight benchmark compare "<data_dir>\benchmarks\comparisons\baseline-models.json"
```

Every result set must cover exactly the same case IDs. For each ref, the evaluator
checks the dataset benchmark/case identity, expected source SHA, current annotation
file SHA, split, game profile, and full policy fingerprint. All refs within one set
must share one provider/model/billing/media/thinking/prompt/schema/window/proxy,
signal, extraction, ranking, canonicalization, and evaluator-policy fingerprint;
mixing models in one set is rejected. Changing an annotation revision requires
reevaluating every experiment in the comparison. Aggregate rows are grouped by
experiment, split, and profile and retain count-weighted primary, slice, boundary,
cost, runtime, storage, review, duplicate, and Best-of metrics. The Markdown output
contains labels, case IDs, hashes, and metrics only—never local paths, media,
credentials, authorization headers, signed URLs, or raw provider thoughts.

## M8B1 real-gameplay preparation (completed locally, private)

M8B1 performed a bounded, read-only inventory of the owner's local gameplay recordings
and selected four real-gameplay cases (two calibration and two validation) totaling
roughly forty minutes. The private corpus uses a profile-diverse competitive FPS pair;
where a title could not be identified reliably from local evidence, the profile remains
neutral for human confirmation. Each selected case has original-source provenance,
content hashes, measured duration, and an empty annotation template. Long recordings
were cut into private stream-copy derivatives outside the source directory; keyframe
approximation is recorded in provenance and the source recordings remain immutable.

The inventory, derived clips, selection manifest, dataset manifest, and annotation
templates stay under the configured ignored data directory. They are never committed,
and tracked documentation contains no source paths, filenames, media, or annotations.
Human owners must annotate genuine MUST_CATCH, WORTH_REVIEW, optional, modality, and
boring intervals; the model and selection script do not write ground truth. M8B1 ends
at `READY_FOR_HUMAN_ANNOTATION`. M8B2 provider benchmarking must wait until those
annotations are complete and validated.

## Intended M8B2 private dataset (documented target only)

M8B2 should use the completed M8B1 cases after human annotation, retaining boring and
high-event footage, audio/reaction coverage, and a contrasting profile. Try to cover an
obvious visual highlight, subtle smart play, funny reaction, failure, clutch, boring
interval, and overlapping setup/payoff story. Keep calibration and validation cases
distinct from the start. Its first comparison is the current Gemini baseline versus a
lower-cost Gemini baseline on the same sources, annotations, evaluation policy, prompt,
window/proxy/extraction/ranking settings, and only the model dimension changed. This
is a future plan, not an executed or implemented provider comparison.

Before full M8 acceptance, at least one representative 1–4 hour source must run
through analysis → report → evaluation while checking resume, source immutability,
budget, storage, and review ratio. M8A intentionally does not run that holdout.

## Status and safety

M8A benchmark foundation and pre-benchmark hardening: complete after offline
synthetic tests and local static validation. M8B1 real-gameplay dataset preparation:
complete locally, with human annotation pending. M8B2 provider benchmarking: **NOT
RUN**. V1 defaults: **NOT LOCKED**. M9 remains separately authorized and **NOT
STARTED**.

Real provider/API calls during M8A and M8B1: **ZERO**.
