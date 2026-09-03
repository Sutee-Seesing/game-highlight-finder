# Implementation Plan, Tests, and Risks

## 1. Delivery strategy

Build in vertical, demonstrable increments. Every milestone must leave the CLI restart-safe and tests green. Paid integration comes only after local contracts, budget refusal, and fake-provider tests work.

## 2. Milestones and exit criteria

### M0 — Plan and environment audit (complete)

- Requirements, architecture, pipeline, models, budget, risks, and tests documented.
- Environment audited without installing or implementing.
- Owner approval gate recorded.

### M1 — Foundation and ingest (complete)

- Create repository, Python 3.12 environment, packaging, lint/type/test commands, `.gitignore`, `.env.example`, and example config.
- Implement `highlight doctor`, `config check`, `analyze --stop-after ingest`, and `status`.
- Discover/pin FFmpeg and validate ffprobe output.
- Implement source hashing, `SourceAsset`, session ID, atomic JSON, lock, and stage manifest.
- Exit: a fixture and real short clip ingest twice; second run is a verified cache hit; source hash unchanged.

Implemented on 2026-08-11 with uv-managed Python 3.12, Pydantic v2, Typer, PyYAML, pytest, and Scoop-resolved FFmpeg/ffprobe 9.0. Validation covers strict configuration, doctor, safe ffprobe parsing, source hashing, atomic artifacts, manifest transitions, per-session locking, interrupted-run recovery, cache invalidation, status output, Unicode paths, and source immutability. No M2 components were added.

### M2 — Proxy and local signals foundation

- Implement command builders, progress parsing, cancellation, disk preflight, proxy generation, audio extraction, silence/loudness/activity metadata, and timestamp mapping.
- Probe and hash generated media before commit.
- Add optional hooks—not a mandatory ML installation—for transcript and scene detectors.
- Exit: representative CFR/VFR/no-audio fixtures preserve source-time mapping within tolerance.

Implemented on 2026-08-12 with CPU-safe FFmpeg command adapters, machine-readable progress parsing, cancellation/timeout handling, conservative disk preflight, atomic temporary media commits, re-probe validation, versioned timestamp mapping, mono AAC audio extraction, deterministic silence/RMS/loudness parsing, additive M1 manifest migration, stage-specific cache keys, CLI stop boundaries, and 68 automated tests. M2 is local-only and makes no AI/API calls. The default proxy uses H.264/AAC, max 854x480, preserved aspect ratio, and a 2% duration tolerance with a 500 ms floor.

### M3 — Canonical domain and fake Scout

- Implement bounded Scout schemas, semantic validator, interval math, match reconciliation, candidate dedupe, conservative story merge, ranking, and fake provider.
- Drive the pipeline end-to-end with malformed and golden fake responses before network access.
- Exit: all required malformed JSON/timestamp/overlap tests pass and a fake session map is deterministic.

Implemented on 2026-08-12 as the local-only canonical contract slice. The runtime now has bounded Pydantic Match/Candidate/Evidence/SessionMap models, controlled generic and game-profile categories, distinct score/confidence validation, half-open integer-millisecond normalization, deterministic local IDs, immutable raw Fake Scout artifacts, canonical session-map persistence, M3 cache identity, additive manifest migration, CLI `--stop-after scout`, and hostile-response validation. The complete automated suite is **106 passed / 0 failed / 0 skipped**.

### M4 — Cost gate and provider contract (complete)

- Implement price catalog, FX snapshot, SQLite migrations, transactional reservations, ambiguous-call handling, monthly reports, and provider registry/capabilities.
- Exit: concurrent reservation test proves the hard limit cannot be crossed; unknown/stale pricing fails closed.

Implemented on 2026-08-12 as a provider-neutral, local-only cost boundary. The
runtime now has exact provider/model/billing-mode contracts and aliases, bounded
usage models, versioned pricing and FX snapshots, Decimal-to-integer micro-THB
quotes with conservative rounding, a WAL/FULL SQLite ledger with explicit
migrations, atomic monthly reservations, idempotent lifecycle transitions,
ambiguous-call persistence/reconciliation, global overage safety holds,
fail-closed missing output-rate handling, monthly CLI status/report/calls views,
and an offline Fake Provider contract fixture.
Production provider SDKs, pricing, API keys, and network calls are intentionally
absent. The complete automated suite is **131 passed / 0 failed / 0 skipped**.

### M5 — Gemini Scout integration (ACCEPTED)

- Implement the exact stable `gemini-3.5-flash-lite` adapter through the official
  `google-genai` SDK, with Files API upload/readiness/deletion lifecycle,
  `Interactions` `store=false`, per-video-item low media resolution, structured
  JSON output, current total/modality usage capture including separate thinking
  tokens, and no thought-step persistence. `thinking_level: minimal` is sent to
  the provider; the reserved thinking allowance is local cost-gate metadata.
- Reserve through the M4 cost gate before upload, persist request/cache identity,
  preserve `RESERVED -> IN_FLIGHT -> SETTLED/AMBIGUOUS`, and retry only remote
  cleanup on resume. Upload validation accepts only the committed session
  `analysis_proxy.mp4`, never the RAW source.
- Verify the dated Standard pricing entry (USD 0.30/M input and USD 2.50/M
  output including thinking) and require an explicit FX snapshot.
- The one-window implementation is bounded to 900 seconds by default. Long
  session windowing/checkpoint reconciliation is intentionally deferred to M6.
- Offline exit evidence: deterministic prompt/schema/estimate, privacy boundary,
  preflight without transport calls, fake upload/generation/delete lifecycle,
  verified paid cache hit, ambiguous no-retry, and source immutability. Live
  acceptance completed on 2026-08-13 with exactly one generation attempt using
  a deterministic synthetic 8-second proxy only. The accepted request used
  `gemini-3.5-flash-lite`, low video resolution, `thinking_level=minimal`, and
  `store=false`; remote cleanup completed with `deletion_status=deleted`.
  The conservative list-rate-equivalent reservation was **฿0.331074** and
  settlement was **฿0.021643**. The identical cache verification performed
  zero new generation calls and zero new reservations. The complete automated
  suite is **152 passed / 0 failed / 0 skipped**.

### M6 — Long-session reconciliation and extraction

- Implement overlapping window planner/stitcher and per-window resume.
- Implement precise candidate extraction from source, pre/post-roll, thumbnails, candidate manifest, and stream-copy alternative.
- Exit: boundary accuracy and dedupe pass on synthetic cross-window cases; interrupted extraction resumes at the missing candidate.

Implemented on 2026-08-13 as a local-first long-session foundation. The runtime
now has a deterministic bounded/overlapping window planner, strict persisted
window provenance, proxy-only window derivatives, bounded per-window signal
summaries, window-relative prompt/schema/canonicalization, semantic cache keys,
aggregate preflight support, conservative match stitching/conflict diagnostics,
candidate dedupe, bounded clip derivation, accurate source re-encode, an
explicit keyframe-approximate stream-copy alternative, thumbnails, per-candidate
manifests, and resume/source-immutability checks. The bounded live acceptance on
2026-08-13 used a deterministic synthetic ~10-second source, two 6-second
windows with 2-second overlap, and `gemini-3.5-flash-lite` at low resolution with
minimal thinking. Both window calls were `SETTLED`, remote cleanup deleted both
files, and the identical cache rerun made zero new generations or reservations.
List-rate-equivalent reservations were W0 THB 0.331454 and W1 THB 0.331478
(total THB 0.662932); settlements were W0 THB 0.022785 and W1 THB 0.022761
(total THB 0.045546). The full offline suite is 173 passed / 0 failed / 0
skipped. M7 is implemented locally; validation adds ranking/report coverage and
still makes zero real Gemini calls.

M7 implementation includes deterministic `m7-ranking-v1` ranking (default
best-of 3), atomic escaped offline HTML, cost/stage/warning diagnostics,
resume/report/candidates/cost-session commands, and safe force-stage invalidation.
Zero-candidate reports are valid; no AI Reviewer, publishing integration, or
M8 work is included. M7 acceptance hardening adds truthful per-invocation
provider activity, persisted report/cost-session configuration, HTML artifact
hash/size verification, and automated cold/warm, CLI, force-stage, and
corruption-rebuild regressions. Validation: 183 passed / 0 failed / 0 skipped,
Ruff and mypy clean; real Gemini API calls during maintenance: zero.

### M7 — Report and usable V1

- Generate self-contained HTML grouped by match with chronology/ranking/category data, cost, warnings, thumbnails, and local file links.
- Implement `analyze`, `resume`, `report`, `candidates`, `cost`, and force-stage UX.
- Exit: cold and warm end-to-end runs on a representative session; warm run performs no unnecessary work.

### M8 — Real gameplay validation and tuning

- M8A benchmark foundation: **implemented**. The local provider-neutral harness now
  provides strict dataset/annotation/result models, calibration/validation separation,
  deterministic temporal matching, importance/modality/boring labels, boundary and
  duplicate diagnostics, union-duration review metrics, Best-of/match/category
  slices, authoritative ledger cost metrics, durable runtime/storage metrics,
  experiment identity, ground-truth leakage protection, and atomic JSON/Markdown
  aggregation. `highlight benchmark template|validate|evaluate|aggregate` makes
  zero provider/API calls.
- M8B1 real-gameplay discovery and annotation preparation: **COMPLETE LOCALLY**.
  A bounded read-only inventory selected four private real-gameplay cases (two
  calibration and two validation, roughly forty minutes) with original provenance
  and derived-clip hashes. The initial templates were human-owned and empty; the
  bounded v13 validation later used two pre-locked validation annotation sets. This
  does not imply complete ground truth for every prepared case or for the separate
  63.48-minute structural source. No private gameplay or annotations are committed.
- M8B2 provider validation: **COMPLETED, QUALITY FAILED**. v13 evaluated two
  pre-locked validation cases in 10/10 attempts; calls settled and remote cleanup
  passed. Its 8 predictions produced 1 TP / 7 FP / 4 FN against 5 annotations
  (precision .125, recall .200, MUST_CATCH recall 0.0). M8 is **NOT ACCEPTED** and
  V1 defaults are **NOT LOCKED**. The revealed validation holdout is not tuning data;
  another unbiased decision needs a fresh locked holdout. Product decisions prioritize
  quality/fun, MUST_CATCH recall, precision/review burden, cost per source hour, then
  runtime/storage.
- M8B1.5 tiny local human annotation helper: **implemented and provider-free**.
  `highlight benchmark annotate <annotation.json>` binds only to `127.0.0.1`, supports
  range-streamed playback and timestamp buttons, keeps human edits in memory until
  Save, validates with `BenchmarkAnnotations`, writes atomically, and keeps review
  state in a private sidecar. It does not generate labels or expose a filesystem
  browsing endpoint.
- M8B1.6 review-proxy helper: **implemented and provider-free**. `highlight benchmark
  make-review-proxies <dataset.json>` resolves the accepted private dataset, verifies
  source SHA-256 and read-only fingerprints, and writes private full-timeline MP4 review
  copies with H.264 NVENC/AAC by default. The helper validates audio, aspect/FPS/scale
  constraints, and a strict 250 ms duration tolerance; proxies are convenience files,
  not benchmark sources, and never alter production encoder defaults.
- M8 v19 boundary-refinement remediation: **IMPLEMENTED OFFLINE / QUALITY NOT YET PROVEN**.
  Candidate-local slowed media, strict fake/Gemini contracts, aggregate batch preflight,
  injected/lazy transport execution, per-candidate cost/cache lifecycle, and the explicit
  `highlight refine-boundaries` CLI gate are implemented. A new provider-free
  `highlight benchmark boundary-feasibility` command is calibration-only and separates
  strict matches from anchor-overlap boundary headroom, context reachability, detection
  gaps, and MUST_CATCH detection gaps using the authoritative M8 evaluation policy. It
  rejects validation/holdout cases and labels any ground-truth-derived candidate IDs as
  diagnostic-only. Feasibility output carries Scout backend/model/prompt provenance plus a
  semantic-quality-applicability guard, so deterministic fake-Scout verdicts cannot be mistaken for
  semantic detection evidence. No live Gemini call is made by this feasibility gate.
  Cross-machine calibration evidence can be moved with `highlight benchmark
  pack-boundary-feasibility`, which emits a JSON-only single-calibration-case bundle with
  sanitized source path and no media, credentials, provider artifacts, machine config, or
  validation data; the bundle can be revalidated under a different local `data_dir`.
  Final reconciled SessionMaps now retain backend/model/window-prompt/config/plan provenance.
  Historical sessions that predate this fix recover prompt/model identity from local per-window
  request metadata during bundle packing; mixed identities fail closed and request/provider artifacts
  themselves are never copied. This prevents v11/v12 outputs from being mistaken for current v18.
- V1 defaults are **NOT LOCKED**. The provider-free 63.48-minute structural long-run
  is complete through report with Fake Scout and zero real Gemini calls: five windows,
  five candidates, best-of 3, resume/report exit 0 in 1329.587s, and a warm-cache
  rerun exit 0 in 260.907s with `report cache: HIT`. The original source remained
  immutable (size 20,250,210,757; mtime `2026-06-17T16:02:07.2383233Z`; SHA-256
  `d7c2c72db4c68ec419792888ad8138b8edba2e5d0e3482597ebf951f8da9572a`).
  Session-generated storage was 3,246,248,308 bytes, including the preserved
  636,747,824-byte interrupted proxy partial; all five clips total 55s review
  (1.444037%), and best-of 3 totals 33s (0.866422%). Its quality evaluation remains
  blocked because the full source has no legitimate complete human ground truth; do not
  fabricate ground truth or reuse partial calibration annotations as full truth.

#### M8A pre-benchmark hardening (accepted offline)

The benchmark ruler is now authoritative before any private gameplay is used:
semantic policy fingerprints are persisted and compared, legacy `m8-eval-v1`
manifests migrate only to the exact historical `0.25 / 3000 ms` policy, and
dataset policy mismatches fail closed. Ground truth remains separate from
experiment results through strict result-set and comparison manifests. Equal-case
coverage, source/annotation revision identity, split/profile identity, and
single-experiment result-set consistency are enforced. The aggregate report keeps
raw-count weighting and labels each experiment's calibration, validation, and
combined groups. Focused evaluator/metrics/privacy/CLI regression tests are
committed. The statement that real provider/API calls were zero is historical to the
offline hardening scope: v13 validation later completed and failed the quality gate.
M9 is **NOT STARTED** and V1 defaults are **NOT LOCKED**. The revealed validation
holdout cannot be tuned against; any future unbiased decision requires fresh locked
human ground truth.

#### M8 hybrid architecture amendment — 2026-09-01 onward

The additive plan in `docs/10_M8_HYBRID_PROPOSAL_PLAN.md` supersedes prompt-only
tuning as the active remediation path without deleting the earlier M8 history. H1/H2
now proposes overlapping event-centered clips from local audio/motion evidence and
covers 5/5 currently annotated calibration highlights while forwarding about one
quarter of each ten-minute calibration source. H4 now implements a provider-free
semantic-judge contract over only those bounded proposal clips: `KEEP | REJECT |
UNCERTAIN`, proposal-relative event bounds, visible evidence, source-time mapping,
local overlap dedupe, aggregate cost preflight, exact upload/hash validation,
`SETTLED`-only cache reuse, and ambiguous-call no-auto-retry behavior. H4 verification
was 29/29 targeted PASS, Ruff PASS, mypy PASS over 76 source files, and 364/364 full
pytest PASS in 111.83 s. The real calibration proposal-batch preflight on T used a
fresh Bank of Thailand USD/THB snapshot (33.2030, 01 Sep 2026); `gemini-3.7-flash`
quoted cal-01 at 7 proposal generations / THB 2.758416 maximum reservation and cal-02
at 6 / THB 2.374214.

The first authorized H5 cal-01 live run then stopped after 2/7 distinct proposal calls,
with no automatic retry. The 56-76 s proposal was correctly rejected as buy-phase/spawn
activity and settled at THB 0.056155 with remote cleanup deleted. The 115-135 s proposal
overlaps calibration highlight `hl-0001` (120-136 s), but Gemini emitted
`confidence=6.8` against the strict 0-1 contract, so the local parser refused the result;
the pre-hardening lifecycle conservatively left that call AMBIGUOUS at THB 0.384830
reserved and remote cleanup still deleted. The remaining five proposals were not sent.
Provider-free hardening now states score/confidence scales explicitly, settles known
completed provider usage before semantic parsing, persists the sanitized raw envelope
before parsing, and prevents a later local contract failure from being mislabeled as
provider ambiguity. Post-hardening verification is 14/14 targeted PASS, Ruff PASS,
mypy PASS over 76 source files, and 365/365 full pytest PASS in 99.85 s. A fresh
provider-free preflight of the hardened request identity makes zero calls/uploads/
reservations and quotes `gemini-3.7-flash` cal-01 at 7 calls / THB 2.761373 maximum
reservation, cal-02 at 6 / THB 2.376752, THB 5.138125 combined. Because the request
identity changed and the prior authorization explicitly prohibited automatic retry,
live continuation now stops at a fresh-authorization boundary; the old THB 2.76 cap
is also THB 0.001373 below the complete hardened cal-01 quote.

A newly authorized hardened cal-01 rerun then completed all 7/7 proposal calls under
THB 2.77 maximum exposure with zero automatic retries. All seven ledger rows are
SETTLED; actual settled usage is THB 0.517870 versus THB 2.761373 maximum reservation,
and all seven remote media objects were deleted. Against the three currently annotated
cal-01 highlights, the hybrid judge produced four candidates with 3 TP / 1 FP / 0 FN:
precision 0.75, recall 1.00, MUST_CATCH recall 1.00, and WORTH_REVIEW recall 1.00.
The proposer forwarded 161.0 s / 600.886 s = 26.7938% of the source and the four kept
event intervals total 55 s / 600.886 s = 9.1532%, at approximately THB 3.1026 per
source-hour of actual judge cost. The original source was re-hashed after the run and
still matches `7db9940058f764d1725f89340e8c1226d80b31671953739b7be9aeb06d2ac726`
with its original 6,282,414,778-byte size. This materially positive cal-01 evidence does
not accept M8 by itself: hardened cal-02 and a fresh locked holdout are still required,
and revealed v13 validation remains forbidden for tuning.

The separately authorized hardened cal-02 run used Gemini 3.7 Flash with a 6-attempt /
THB 2.39 cap and zero automatic retries. It stopped safely after 5/6 distinct proposal
calls when the fifth request returned HTTP 401 authentication after HTTP dispatch. The
ledger therefore contains four SETTLED rows plus one conservatively AMBIGUOUS row; actual
settled usage is THB 0.242824, the fifth-call reservation is THB 0.431596, and the sixth
proposal was never sent. All five uploaded remote media objects, including the failed
request's object, report cleanup `deleted`. The four completed proposals all returned
REJECT and are outside both currently annotated cal-02 highlight intervals. The fifth
proposal spans 386-421 s and overlaps `hl-0001` at 401-417 s, while the unsent sixth
proposal spans 574-594 s and overlaps MUST_CATCH `hl-0002` at 574-590 s. Therefore this
partial run is **semantic-inconclusive**, not a cal-02 quality failure: neither positive
was successfully adjudicated. The source was re-hashed after the stop and still matches
`8d973547b93d432a4deb5f4880ea08fe6cbb7466a6c08a5de0d2e94f0ace2126` with its original
2,805,344,323-byte size. No retry or sixth call is permitted without a fresh explicit
authorization boundary.

Provider-free remediation after the 401 first pinned the Hybrid Judge to stable Gemini
Interactions API `v1`, with `api_version=v1` included in provider-request/cost identity.
That path passed 35/35 targeted tests, Ruff, mypy over 76 source files, and 367/367 full
pytest before a separately authorized 2-call / THB 0.82 positive-subset run was attempted.
The stable-v1 Interactions run stopped after its first proposal with HTTP 400
`invalid_request`: the stable endpoint rejected `type=video` and listed document/image/
audio/text as supported input content types. The ledger conservatively records 1 AMBIGUOUS
call at THB 0.431596 reserved with THB 0 settled; remote cleanup is `deleted`, proposal 2
was never sent, and cal-02 source hash/size remain unchanged. This is an endpoint-contract
failure, not semantic evidence, and the consumed authorization must not be reused.

The next provider-free remediation keeps stable API `v1` but moves only Hybrid Judge
generation to `models.generate_content`, which supports Files/video input. Request identity
now includes both `api_version=v1` and `api_surface=generate_content`; non-matching transports
fail closed before reservation/upload/generation. The adapter preserves the existing Files
upload/cleanup lifecycle and maps authoritative generateContent usage metadata, including
VIDEO/AUDIO/TEXT prompt-token breakdown, back into the existing cost ledger contract.
Verification is 37/37 targeted PASS, Ruff PASS, mypy PASS over 76 source files, and
369/369 full pytest PASS in 147.38 s under durable task
`9ec4d11a-c5cb-45b5-900a-fbe3f53047f7`.

The narrowed cal-02 plan remains proposals `386-421s` and `574-594s`, the two unresolved
windows overlapping the annotated positives. Fresh generateContent request identity has 0
judge cache hits and provider-free preflight remains exactly 2 planned calls / THB 0.816844
maximum reservation with 0 calls / 0 uploads / 0 ledger reservations. The generateContent
live helper is syntax-valid, still hard-guards attempt cap 2 / THB 0.82 / no automatic retry,
and its dedicated ledger and summary do not exist. The separately authorized generateContent continuation then consumed both bounded application-level
calls. Proposal `386-421s` completed successfully as KEEP/FUNNY with a relative event at `13-25s`,
which maps to approximately `399-411s` in source time and overlaps WORTH_REVIEW `hl-0001`
(`401-417s`). That call is SETTLED at THB 0.108898 and its remote object is deleted. Proposal
`574-594s`, which covers MUST_CATCH `hl-0002` (`574-590s`), reached the provider but failed with
HTTP 503 `UNAVAILABLE` / temporary high demand; it remains conservatively AMBIGUOUS at THB
0.385248 reserved with no semantic response, and its remote object is also deleted. Source
SHA-256 remains `8d973547b93d432a4deb5f4880ea08fe6cbb7466a6c08a5de0d2e94f0ace2126`.
Thus cal-02 has one confirmed positive match but is still semantic-inconclusive because the
MUST_CATCH positive remains unresolved.

Audit of `google-genai 2.21.0` after the 503 found an SDK-level retry policy that defaults to up to
5 HTTP attempts for retryable statuses such as 408/429/5xx when no retry options are supplied.
That means the prior workflow's `no automatic retry` guarantee covered application/batch retries
but did not fully suppress SDK-internal HTTP retries. The exact number of HTTP attempts made inside
the failed 503 SDK call is not asserted. Provider-free hardening now constructs the Hybrid Judge
SDK client with `retry_options.attempts=1`, fail-closes before reservation/upload/generation unless
that invariant is present, and includes `sdk_http_attempts=1` in provider-request/cost identity.
Targeted verification is 39/39 PASS, Ruff PASS, mypy PASS over 76 source files, and canonical full
pytest is 371/371 PASS in 113.16 s under durable task `611d6a96-4bee-4196-bb5e-e4fc42e2af5a`.

The separately authorized MUST_CATCH-only continuation then ran exactly once under the new
no-SDK-retry identity: stable `v1` generateContent, Gemini 3.7 Flash, attempt cap 1, THB 0.39
exposure cap, SDK HTTP attempts 1, and no application retry. Proposal `574-594s` completed
successfully at the provider level but returned REJECT with zero events and the summary that it
was standard hide-and-seek exploration with no highlight-worthy moment. The call is SETTLED at
THB 0.056977, remote cleanup is `deleted`, and the source still matches the immutable
2,805,344,323-byte SHA-256 `8d973547b93d432a4deb5f4880ea08fe6cbb7466a6c08a5de0d2e94f0ace2126`.

H5 cal-02 is therefore closed as a **semantic quality failure**, not an infrastructure
inconclusive result. Combining the four historical settled REJECTs outside the annotations, the
successful KEEP/FUNNY prediction around source `399-411s`, and the final MUST_CATCH REJECT gives
1 prediction against 2 annotated highlights: TP=1, FP=0, FN=1, precision=1.00, recall=0.50,
WORTH_REVIEW recall=1.00, and MUST_CATCH recall=0.00. The local proposer did cover both known
positives, so this calibration isolates the failure to the Gemini semantic judge on the
MUST_CATCH example rather than proposal recall. H5 is closed; M8 remains NOT ACCEPTED and V1
defaults remain unlocked. Before any fresh locked holdout, the next sensible provider-free step
is to define a bounded same-calibration alternate-judge comparison rather than tune against the
revealed validation holdout.

Provider-free H5A now implements a live-capable **OpenRouter multimodal judge bake-off**
without making any paid call. The initial single-model slice used `z-ai/glm-5v-turbo`; OpenRouter's
current endpoint catalog exposes one endpoint, Z.AI, with 202,752-token context and exact USD
pricing of 1.20/M prompt tokens, 0.24/M cache-read tokens, and 4.00/M completion tokens. The
comparator reuses the provider-neutral `hybrid-judge-v1` semantic prompt/schema/parser/candidate
mapping. OpenRouter officially supports local/private MP4 as a base64 `video_url` data URL, so the
transport can send the committed proposal clip without public hosting. JSON-object mode is used
only as provider formatting assistance; the exact schema is appended as a formatting contract and
strict local Pydantic validation remains authoritative.

The transport performs exactly one stdlib HTTP POST with no client retry, reads the API key only
from `OPENROUTER_API_KEY`, applies a 16 MiB encoded-request guard, requests authoritative usage,
enables reasoning while excluding reasoning text, and pins routing to Z.AI with
`only=[z-ai]`, `allow_fallbacks=false`, and `require_parameters=true`. The max-price guard uses
OpenRouter's routing units in USD per million tokens (`1.2` prompt / `4.0` completion), while the
local reservation catalog continues to normalize list price to per-token units. Request/cost identity
records the OpenRouter provider, Z.AI upstream lock, attempts=1, no-fallback policy, and
`openrouter-base64-video-v1`. Completed responses settle authoritative usage first; routing
metadata must then prove exactly one upstream attempt and selected provider `Z.AI` before semantic
output can be reused. A post-settlement routing-policy mismatch therefore preserves billing
evidence but fails closed locally. Current proposal MP4s are approximately 4.6-11.0 MB; the
largest estimated base64 representation is about 14.7 MB and remains inside the local guard.

The apples-to-apples comparison scope remains the two existing calibration sessions only:
**13 proposal clips / 302 seconds** total (cal-01 7 / 161 s; cal-02 6 / 141 s), with revealed v13
validation excluded. The versioned conservative reservation heuristic still reserves 256 video
tokens/s plus prompt/schema text and 1,024 output + 1,024 thinking tokens per request; it is a
safety estimate, not a tokenizer claim. Using the existing 2026-09-01 USD/THB snapshot, the final
OpenRouter provider-free preflight is cal-01 max THB 4.461257, cal-02 max THB 3.861802, and
**THB 8.323059 combined** across 13 planned calls, with 0 provider calls / 0 uploads / 0 ledger
reservations / 0 cache hits and `live_media_transport_verified=true`. A first-stage positive-only
gate is also prepared over cal-02 proposals `386-421s` and `574-594s`: **2 calls / THB 1.385522
maximum reservation**, again with 0 provider calls / 0 uploads / 0 reservations / 0 cache hits.
This cheaply tests whether GLM fixes Gemini's MUST_CATCH miss before spending on the other 11
proposals needed for precision. Current targeted verification is 23/23 PASS, Ruff PASS over
`src + tests`, mypy PASS over 78 source files, and canonical full regression is **384/384 PASS in
106.24 s** under durable task `62037e5f-743c-4ba7-9942-c1d67b5a4872`. `OPENROUTER_API_KEY` is
currently absent from both the process environment and checked local `.env` declarations. No paid
OpenRouter/Z.AI call is authorized by this work; the next bounded live gate therefore requires
credential setup plus fresh explicit authorization for the two-proposal THB 1.385522 experiment.

Round A supersedes the earlier GLM-5V-Turbo-only first gate above. The current provider-free
bake-off locks seven OpenRouter profiles over the same two cal-02 positives: Qwen3.8 Flash ->
Alibaba, GLM-5.3-Flash -> Z.AI, MiMo-V2.5 -> Xiaomi direct, Seed-2.0-Mini -> Seed,
Seed-2.0-Lite -> Seed, GLM-5V-Turbo -> Z.AI, and Qwen3.8 Max -> Alibaba. Every profile pins its
exact upstream provider with fallback disabled, uses HTTP attempt=1, applies its own USD-per-million
routing max-price ceiling, and keeps the same `hybrid-judge-v1` semantic contract. Qwen/Seed profiles use
provider-enforced JSON Schema where the locked endpoint supports it; GLM/MiMo use JSON-object
formatting plus authoritative local schema validation. Request fingerprints, call IDs, artifacts,
and cache paths are model-specific so results from different models cannot overwrite or reuse each
other's evidence.

Provider-free Round A preflight is **7 models x 2 locked positive clips = 14 logical calls**. Using
the existing 2026-09-01 USD/THB snapshot, conservative maximum reservations are Qwen3.8 Flash
THB 0.168295, GLM-5.3-Flash THB 0.173191, MiMo-V2.5 THB 0.131181, Seed-2.0-Mini THB 0.126341,
Seed-2.0-Lite THB 0.479051, GLM-5V-Turbo THB 1.385522, and Qwen3.8 Max THB 2.200404, totaling
**THB 4.663985**. The preflight made 0 provider calls / 0 uploads / 0 ledger reservations / 0 cache
hits. GLM-5.3-Flash reservation deliberately uses its undiscounted list price so the budget gate
will not under-reserve after the temporary promotion ends; Seed Mini/Lite fail closed before
reaching their >=128K alternate price tier. Current verification is 32/32 targeted PASS, Ruff PASS
over `src + tests`, mypy PASS over 79 source files, and canonical full pytest **393/393 PASS in
114.72s** under durable task `9f673f76-ef08-4b8b-b6b3-4f5f5c740c4b`. The local `.t/` live runner
uses fresh dedicated ledger/preflight-ledger/summary paths, hard-caps exposure at THB 4.67, permits
at most 14 logical calls, fixes HTTP attempts at 1, and disables automatic retry. It evaluates
MUST_CATCH first per model and skips that model's WORTH_REVIEW call after a MUST_CATCH miss or call
failure, so THB 4.663985 is a conservative preflight maximum rather than a spend target. The original seven-model runner compiled/imported provider-free and its three dedicated live artifacts were absent before its first execution. That was the pre-live authorization boundary at the time: at most 14 logical calls / THB 4.67 hard exposure cap / one HTTP attempt per call / no automatic retry. The live outcomes and current recovery boundary are recorded below.

The first authorized live Round A dispatch on 2026-09-03 stopped after the seven MUST_CATCH-first router requests: every model received HTTP 404 `No endpoints found that satisfy the max price for this request`, so all seven WORTH_REVIEW requests were correctly skipped. This produced **zero semantic responses and THB 0 settled**, so it is a routing/configuration incident rather than a model-quality result. The transport had supplied `provider.max_price` in USD per token while OpenRouter routing expects USD per million tokens, filtering every endpoint with a ceiling 1,000,000x too low. The original ledger remains conservative at **7 AMBIGUOUS / THB 2.025090 unresolved reservation** because exact error routing metadata was not persisted. Provider-free remediation sends USD-per-million routing ceilings and treats explicit router `attempt=0` evidence as confirmed no-upstream-dispatch for release while preserving ambiguity when metadata is absent. Replacement request identity v3 fingerprints `routing_price_unit=usd_per_million_tokens`, preventing collision with the incident's v2 call IDs/artifacts; that remediation passed **49/49 targeted**, Ruff, mypy over **79 source files**, and full pytest **395/395** before the replacement live dispatch.

The fresh authorized replacement Round A then completed with **7 logical MUST_CATCH calls** and no WORTH_REVIEW calls because every model failed the MUST_CATCH gate. Routing was fixed: MiMo-V2.5 and GLM-5V-Turbo returned valid settled responses but both semantically returned `REJECT`; Seed-2.0-Mini returned a settled response truncated at `finish_reason=length`; Qwen3.8-Flash, GLM-5.3-Flash, Seed-2.0-Lite, and Qwen3.8-Max reached post-dispatch parsing with missing text and remained conservative AMBIGUOUS under the v3 lifecycle. Known settled cost was **THB 0.390358**, unresolved reservation was **THB 1.314330**, and known-plus-unresolved exposure was **THB 1.704688**, below the THB 4.67 hard cap. Seed Mini's preserved raw response proves the next transport defect: the local quote reserved **1,024 final-output + 1,024 thinking tokens**, but the wire request sent only `max_tokens=1024` total; Seed Mini consumed **1,016 thinking + 8 visible output tokens**, exhausting the completion ceiling and truncating the JSON. MiMo likewise used 936 thinking + 81 visible output tokens, while GLM-5V used 613 thinking + 78 visible output tokens. The four missing-text cases are compatible with the same exhaustion pattern, but exact response bodies were not persisted by the old transport on that parse path, so they are not retroactively classified as proven length exhaustion.

Provider-free v4 remediation now aligns the wire contract with the reservation: with reasoning enabled it sends a combined completion ceiling of **2,048 tokens** plus `reasoning.max_tokens=1,024`, while the cost quote remains unchanged at 1,024 final + 1,024 thinking. Successful HTTP responses can preserve authoritative usage even when final text is empty; the pipeline settles cost and writes the raw envelope before locally rejecting `finish_reason=length` or missing final text, preventing those successful-provider cases from becoming AMBIGUOUS solely because semantic parsing cannot proceed. Request identity is advanced to `hybrid-judge-openrouter-v4` and media transport contract `openrouter-base64-video-v2`. Final provider-free verification is **51/51 targeted PASS**, Ruff PASS over `src + tests`, mypy PASS over **79 source files**, and canonical full pytest **397/397 PASS in 98.23s** under durable task `c60cc148-6443-4275-a8fb-427f007b1ea5`. A full seven-model v4 preflight remains **14 planned calls / THB 4.663985 maximum reservation** with zero provider I/O, reservations, cache hits, or prior-call collisions, but it is no longer the recommended recovery scope: MiMo-V2.5 and GLM-5V-Turbo already produced complete settled `finish_reason=stop` MUST_CATCH responses and definitive semantic `REJECT` decisions, so rerunning them would be a stochastic reroll rather than transport recovery. The operational v4 recovery therefore contains only the five transport-inconclusive models (Qwen3.8-Flash, GLM-5.3-Flash, Seed-2.0-Mini, Seed-2.0-Lite, Qwen3.8-Max). Its provider-free preflight is **10 planned calls / THB 3.147282 maximum reservation**, with provider calls/uploads/reservations/cache hits all zero, zero prior-call collisions, and zero persisted call/event/control rows. The dedicated 5-model recovery runner and secure launcher compile successfully; their fresh live ledger/preflight-ledger/summary artifacts are absent. No additional paid dispatch was made during this remediation. Any live v4 recovery requires fresh explicit authorization for **at most 10 logical calls / THB 3.15 hard exposure cap / one HTTP attempt per call / no automatic retry / MUST_CATCH-first early stop**.

### M9 — Optional Reviewer

- Batch only extracted candidates; enforce independent reservations and cache keys.
- Add keep/maybe/reject/merge suggestions without destructive rewrites.
- Exit: measured improvement in shortlist precision justifies incremental cost; otherwise leave disabled.

The original milestone order is adjusted so schemas/fake AI and budget controls precede real API integration. Cache/resume is foundational from M1 rather than added late, preventing paid-stage rework. M6, M7, M8A, and M8B1 preparation are complete. v13 completed the bounded validation but did not meet the quality gate; future validation requires a fresh locked holdout, and M9 remains separately authorized work.

## 3. Test strategy

### Unit tests

- Config defaults, precedence, unknown keys, ranges, redaction, and stable relevant-subset hashes.
- Timecode parse/format; integer-millisecond conversions; half-open overlap/IoU.
- Pre/post-roll and duration clamping at start/end/zero-length edges.
- Match fragment reconciliation and cross-window boundary cases.
- Candidate dedupe, nested overlaps, incompatible categories, and deterministic IDs.
- Story merge relationship/gap/duration rules.
- Ranking with/without Reviewer and deterministic tie-breaking.
- Price lookup, multimodal estimates, FX/safety rounding, month boundary in Asia/Bangkok.
- Budget allow/refuse, exact-limit behavior, reservations, releases, and ambiguous outcomes.
- Cache key relevance, stale propagation, source changes, interrupted-stage recovery, and force-stage closure.
- Scout parsing: invalid JSON, schema-valid nonsense, huge lists/strings, NaN, negative/reversed/out-of-window timestamps, unknown enums, and excessive corrections.
- FFmpeg/ffprobe argument-list generation including spaces, Unicode, quotes, and Windows drive paths.

### Integration tests

- Generate tiny deterministic fixture media with FFmpeg: audio/no-audio, multiple audio streams, CFR/VFR, nonzero timestamps, short GOP/long GOP, portrait/landscape, and corrupt/truncated input.
- Run ffprobe ingest, proxy, timestamp mapping, exact extraction, thumbnail, and output re-probe.
- Kill a subprocess mid-stage and verify resume/temporary-file cleanup.
- Run two budget reservation processes against one SQLite ledger.
- Fake provider simulates rate limit, timeout-before-dispatch, ambiguous timeout-after-dispatch, malformed body, usage missing, deletion failure, and remote expiry.
- Golden report snapshot checks escaped reasons/paths to prevent HTML injection.

### Contract and live tests

- Provider contract suite runs against fake adapter on every test run.
- Opt-in live Gemini smoke tests are marked, excluded by default, tightly budget-capped, and use a synthetic non-private short proxy.
- Record SDK/model capability changes without putting API keys or response media in Git.

### Real evaluation

Create a private annotation format for matches and candidate intervals. Evaluate temporal tolerance rather than exact millisecond equality. Include boring sessions to verify legitimate zero-candidate output and high-event sessions to test non-quota behavior.

## 4. Key technical risks and mitigations

| Risk | Impact | Mitigation / validation |
|---|---|---|
| 1 FPS model sampling misses fast gameplay events | High recall loss | Combine audio/activity signals, overlap windows, test low vs higher resolution on annotated clips, and use candidate context. |
| Match UI differs by game/update | Incorrect hierarchy | Generic fallback, confidence/evidence, unassigned group, versioned game profiles. |
| Four-hour context/upload is unreliable | Timeouts, cost, impossible context | Bounded overlapping windows with item checkpoints; never one giant request. |
| Proxy/source timestamps drift, especially VFR | Wrong clips | Integer source timeline, stored transform, synthetic VFR tests, re-probe extracted clips. |
| Precise stream-copy cuts are keyframe-limited | Missing setup/event | Accurate high-quality re-encode default; label copy mode as approximate. |
| Python 3.14 package incompatibility | Install/runtime failures | Standardize on Python 3.12; test optional GPU stack separately. |
| FFmpeg absent or encoder capabilities vary | Pipeline blocked | `doctor`, pinned build guidance, encoder probe, CPU fallback. |
| Provider pricing/model/API changes | Budget errors or breakage | Dated external catalog, max age, capabilities, aliases resolved per run, fail closed. |
| Free-tier data use/privacy differs from paid tier | Personal footage exposure | Explicit consent/config, proxy only, tier documentation, deletion audit, no original upload. |
| Model output is plausible but wrong | Bad/unsafe extraction | Structured output plus semantic validation, correction limits, raw evidence, human final review. |
| Local storage grows without bound | Disk exhaustion | Preflight estimate, size reporting, configurable library location; no automatic deletion in V1. |
| Concurrent processes overspend | Hard-budget violation | SQLite transactional reservations and per-session locks. |
| Reviewer cost adds little value | Wasted budget | Disabled by default; enable only after Scout benchmark and measure incremental lift. |

## 5. Assumptions and unresolved decisions to validate

### Owner decisions before implementation

1. Is uploading a lightweight gameplay proxy (including voice chat) to the selected Gemini tier acceptable? Confirm account/tier data-use terms.
2. Where should the permanent `data/` library live? The current `C:` has ample space, but recordings may be on another drive.
3. Is Python 3.12 installation approved, and should project setup use `uv`, standard `venv`, or another preferred manager? Recommendation: `uv` if acceptable, otherwise `venv` plus a lock tool.
4. Which trusted FFmpeg distribution/install method is preferred? It must expose ffmpeg/ffprobe and NVENC where licensed/supported.
5. Is accurate re-encode acceptable as the default candidate export? Recommendation: yes; keep stream-copy opt-in.
6. Should voice-chat transcription be enabled later, given privacy and possible multilingual Thai/English speech? Recommendation: off initially, then benchmark locally.

### Experiments, not owner guesses

- Proxy bitrate/resolution and whether UI text is readable enough at 480p.
- Low media resolution versus highlight recall for fast motion and small HUD events.
- Optimal Scout window length/overlap and whether signal-driven subdivision improves quality.
- Whether local transcription materially improves funny/reaction detection on this hardware.
- Generic match detection quality for MECCHA CHAMELEON and which UI/audio cues are stable.
- NVENC versus libx264 quality/speed/file size for extracted review clips.
- Real output-token distribution and cost per analyzed hour.
- Candidate score threshold needed to reach 5–20 candidates without quotas.

## 6. Definition of V1 done

The 63.48-minute provider-free structural run has now exercised the long-source operational criteria: report completion, interrupted-run recovery, warm-cache reuse, source immutability, bounded storage, traceable candidates, and a small review fraction. V1 is still **NOT DONE / NOT LOCKED** because product quality has not passed: v13 failed the quality gate and the structural long source lacks legitimate complete human ground truth. Final acceptance requires an unbiased quality pass against properly locked ground truth; the revealed v13 holdout cannot be used for tuning.

## 7. Current next action after M8 v13

The evaluator additionally now rejects an adjudication sidecar with duplicate or unknown selected cases rather than silently narrowing the reported review scope. Canonical MCP takeover verified this hardening in the project `.venv`: targeted cross-case tests pass **9/9 in 1.29s**, Ruff passes `src` + `tests`, mypy passes **73 source files**, `git diff --check` passes, and the full provider-free regression passes **334/334 in 580.48s**. No provider/API call was made.

M7 implementation and acceptance hardening are complete. M8 v13 completed bounded validation but is **NOT ACCEPTED**. The fresh current-v18 OpenArena calibration and provider-free boundary-feasibility gate are now complete: the Scout recovered both currently human-confirmed calibration positives with no detection gap and no boundary-only headroom. The calibration annotation remains intentionally sparse, so source-level recall is not exhaustive and global/raw strict precision must not be used as a general threshold-tuning metric.

Human review is complete for all four predictions from this exact OpenArena Scout run. The early 0-5s candidate is an additional `WORTH_REVIEW` combat sequence, the existing 24-26s frag remains positive, and the 47-51s plus 57-62s candidates are confirmed boring/traversal predictions. For this adjudicated prediction set the raw strict result is 2/4 (0.5), but that number remains diagnostic rather than a general precision estimate because the rest of the source is not exhaustively annotated.

The provider-free feasibility tooling now separates those two claims explicitly: `precision_tuning_safe` stays false for sparse source annotations, while `false_positive_suppression_safe` becomes true only when every current candidate is accounted for by positive highlight overlap or a fully covering human-reviewed boring interval. The OpenArena case satisfies that narrower gate. A further threshold diagnostic proves that the existing Scout `score` and `confidence` fields have **no monotonic threshold headroom** on these four reviewed candidates: either confirmed negative would survive any lower-bound score/confidence rule that preserves both positives.

A new provider-free `benchmark suppression-feasibility` diagnostic therefore measures already-computed local audio features without changing production ranking or Scout capture. On this OpenArena calibration, the loudest 500 ms activity bin inside each reviewed positive yields a protected-positive floor of **-18.518034 dB**. That lower-bound diagnostic rejects **2/2** confirmed negatives while preserving **2/2** reviewed positives; weighted mean dB rejects only **1/2**. The absolute-peak result is calibration evidence only, not an authorized production threshold and not a new V1 default. After adding a source-loudness-normalized prominence feature, the current diagnostic verdict is `AUDIO_PEAK_OVER_LOUDNESS_HEADROOM`; that verdict is likewise calibration-only.

A provider-free cross-source audio-scale sanity check now shows why the OpenArena absolute peak floor must not be generalized: FreeDoom overall loudness is -21.0 LUFS and all 7 queued review intervals sit above the -18.518034 dB OpenArena floor, while Xonotic overall loudness is -31.6 LUFS and all 5 queued review intervals sit below it. This separation exists before semantic labels are considered, so absolute peak dB is source-level-confounded. The suppression diagnostic now also records peak-minus-source-overall-loudness as an exploratory prominence feature. A local-only `benchmark review-queue` helper is now implemented for the next gate: it serves only declared private review clips over loopback, supports explicit `POSITIVE` / `BORING` / `UNCERTAIN` decisions, writes a queue-hash-bound private sidecar, and never promotes decisions into `BenchmarkAnnotations` automatically. The live FreeDoom + Xonotic queue exposes 12 intervals with provider calls **ZERO**. The first 324-test full regression reached 323 passes but hit one transient Windows `WinError 5` while atomically renaming a temporary boundary-feasibility bundle directory. The isolated module immediately passed 12/12, so the bundle finalizer is now hardened with bounded retry for transient `PermissionError`, plus a deterministic regression test that injects one rename lock. Targeted boundary-feasibility + review-queue tests pass 19/19, Ruff passes over `src` + `tests`, mypy passes 72 source files, and the hardened full regression passes **325/325 in 313.44s**. The provider-free `benchmark cross-case-suppression` evaluator is now implemented ahead of that review: it is queue-SHA-bound, requires exact complete visual decision coverage, rejects any `UNCERTAIN` decision, consumes the existing provider-free audio-scale artifact, and keeps `production_threshold_locked=false`. A live smoke against the current private queue fails closed because the adjudication sidecar does not exist yet, so no semantic result or threshold is fabricated. Targeted cross-case/review/suppression tests pass 15/15, Ruff passes over `src` + `tests`, mypy passes 73 source files, and the full regression passes **331/331 in 307.33s**. The next remediation action remains visual adjudication of those 12 intervals. The sidecar now carries explicit reviewer provenance: the local human review UI writes `HUMAN`, while any assistant/model visual review must be recorded as `ASSISTANT_VISUAL` and remains excluded from M8 acceptance. Once a complete resolved sidecar exists, run the evaluator against those explicit labels before any production suppression rule is implemented. Do not tune against the revealed v13 validation set, do not reinterpret sparse calibration as exhaustive ground truth, and do not call Gemini again without new explicit attempt/exposure authorization. Any future unbiased M8 acceptance decision still requires a fresh locked holdout prepared before predictions. M9 remains **NOT STARTED**.

The 12-sheet provider-free assistant visual pass is now complete as `ASSISTANT_VISUAL` evidence (never human ground truth). Under the existing review decision rule, all **12/12** queued FreeDoom/Xonotic windows visibly contain combat/action; the clearest highlight candidates are FreeDoom 02/06 and Xonotic 01/02/04, while some longer windows include traversal or downtime around the action. A separate private queue-SHA-bound assistant sidecar records these decisions with `provider_calls=0`. Running `benchmark cross-case-suppression` against that exact sidecar fails closed with `Cross-case suppression requires at least one POSITIVE and one BORING label`. This is the correct result: the current high-audio review queue contains no visually boring control window, so it cannot identify or validate a negative-suppression threshold. Do **not** manufacture BORING labels or promote a threshold. The next provider-free calibration action is to add deterministic non-overlapping control windows from FreeDoom/Xonotic, visually adjudicate those controls with explicit provenance, and rerun the diagnostic only if both semantic classes are genuinely present. M8 remains **NOT ACCEPTED**, production threshold locking remains false, and any future acceptance still requires a fresh locked holdout.


The deterministic eight-control follow-up is now complete provider-free. Visual adjudication with explicit `ASSISTANT_VISUAL` provenance adds **5 BORING** and **3 POSITIVE** controls to the original 12 positives, giving a 20-window calibration diagnostic with both classes genuinely present. The queue-bound evaluator returns `NORMALIZED_AUDIO_PEAK_NO_CLEAN_SEPARATION`: the protected-positive normalized floor is **3.140291 dB** and rejects **0/5** boring controls. This closes the single-feature `audio_peak_over_loudness_db` suppression hypothesis as non-separating on the current cross-source calibration evidence; it must not be promoted to a production default or tuned further against the revealed v13 holdout. Next, inspect already-computed provider-free local signals for genuinely independent separation evidence, keeping assistant labels development-only and fail-closed. M8 remains **NOT ACCEPTED**, M9 remains **NOT STARTED**, and provider/API calls remain **ZERO**.


The control-audio provenance audit supersedes the initial 0/5 result: those eight control peaks had accidentally been measured in source-WebM audio while the baseline and original intervals were from runtime local-signals, so the scales were mixed. Recomputing all eight control peaks from the same persisted `activity.json` domain yields the valid 20-window diagnostic: the **3.140291 dB** protected-positive floor now rejects **3/5** BORING controls, with `freedoom-control-03` and `xonotic-control-03` surviving. The verdict remains `NORMALIZED_AUDIO_PEAK_NO_CLEAN_SEPARATION`; this feature has partial calibration value but cannot be the standalone production suppression rule. Next work should inspect independent existing provider-free local signals for those two survivors, with explicit signal-domain provenance.


Experimental `gemini-scout-window-v19` is regression-green offline rather than introducing another weak signal threshold. A 4 fps proxy YDIF exploration did not cleanly separate the remaining cross-source boring controls, so no production motion heuristic is added. The v19 prompt instead makes local signals navigation-only, requires a visible event/payoff and visible evidence, rejects pure traversal/idle/ambient/UI-only activity, and preserves uncertain real interactions at lower score/confidence. v18 behavior and the configured default remain unchanged. Verification is **26/26 targeted**, Ruff PASS, mypy **73 files PASS**, and **335/335 full regression in 440.29s**, with provider/API calls **ZERO**. The next step is a provider-free v19 preflight only; live quality evaluation still requires fresh explicit authorization, and unbiased M8 acceptance still requires a future fresh locked holdout.

## 8. 2026-09-01 plan amendment — hybrid local proposer + Gemini judge

The historical M8 plan above remains intact. A new additive amendment is recorded in `docs/10_M8_HYBRID_PROPOSAL_PLAN.md` after reviewing current open-source gameplay-clipping architectures and the project's own calibration evidence. The project will now evaluate a **hybrid proposal architecture** before spending on another prompt-only Scout revision: local audio/motion and optional game-profile HUD/OCR/structured-event cues produce high-recall anchor timestamps; anchors become event-centered overlapping proposal windows with merge/context safeguards; Gemini judges only those bounded proposals; final clips are cut from the original source using semantic event bounds plus setup/reaction context.

This amendment explicitly rejects arbitrary fixed-chunk semantics. Internal analysis/window boundaries must never become final clip boundaries, and H2 adds regression cases where a fight/round crosses every possible internal boundary. The generic proposer remains semantic-neutral (`semantic_labels_inferred=false`), per-game profiles are optional enhancers rather than hard dependencies, and a small bounded coverage/sentinel budget may be evaluated as a recall backstop for quiet but important events. The currently revealed v13 validation holdout remains excluded from tuning, no new provider call is authorized by this plan change, and V1 remains unlocked until the hybrid path passes fresh locked evidence.

The first H1/H2 implementation checkpoint is now provider-free and regression-green. On the two existing real calibration sessions, the current generic proposer covers **3/3 + 2/2 = 5/5** currently annotated highlights while selecting **26.7938%** and **23.4660%** of each source timeline respectively. H2 adds a 60 s proposal cap, bounded overlap on forced splits, and regression coverage proving event-centered context remains intact when an event crosses internal analysis boundaries. Verification: hybrid targeted **16/16 PASS**, Ruff PASS over `src` + `tests`, mypy **74 source files PASS**, full pytest **351/351 PASS in 101.98 s**, provider/API calls **ZERO**. This is calibration evidence only, not M8 acceptance.
