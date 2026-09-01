# M8 v13 Current Status

This note is the authoritative M8 status checkpoint for the `m8-v12-revision-planner`
branch. Historical statements such as `M8B2 NOT RUN`, `v3 NOT RUN`, or `validation
remains sealed` are superseded where they conflict.

## Sealed provider validation

The bounded v13 validation sequence completed on the two pre-locked validation cases.
Both Gemini generation calls settled successfully and both remote files were deleted.
No v13 calls remain ambiguous or in flight. The run consumed the authorized attempt
budget exactly: **10 / 10 cumulative attempts**. No further provider generation is
authorized under that attempt cap.

The v13 settled cost was **THB 3.757246**. Cumulative known settled exposure across the
preserved M8 live history is **THB 15.041438**. Historical unresolved ambiguity reserves
from earlier revisions remain preserved and are not silently released or retried; with
those reserves included, cumulative worst-case exposure is **THB 20.228897** against the
authorized **THB 23.00** cumulative cap.

## Quality verdict

Execution, cost accounting, cleanup, and sealed evaluation mechanics passed. Product
quality did **not** pass the M8 acceptance gate. The two validation cases together
produced 8 predictions against 5 annotated highlights, with 1 temporal true positive,
7 false positives, and 4 false negatives: aggregate precision **0.125**, recall
**0.200**, and MUST_CATCH recall **0.000**. One validation case missed its MUST_CATCH
highlight; the other missed all useful annotated events.

Therefore V1 defaults are **NOT LOCKED** and M8 is **NOT ACCEPTED**. The revealed v13
validation holdout must not be used for tuning. Any remediation must use calibration or
new calibration data; a future unbiased validation decision requires a fresh holdout
prepared and locked before predictions.

## Fresh OpenArena calibration checkpoint — 2026-08-29

A newly authorized **calibration-only** OpenArena Scout call ran after the sealed v13
validation. It used `gemini-3.5-flash-lite` with `gemini-scout-window-v18` on the single
99,008 ms calibration window. The authorization allowed exactly **1 attempt** and
**THB 0.65** exposure. Exactly **1 generation attempt** ran, with no automatic retry.
The ledger settled the call at **THB 0.251294** after reserving **THB 0.649624**, and
the Gemini remote file records `deletion_status=deleted`.

The semantic Scout produced four candidates. The only currently human-confirmed positive
(24,000–26,000 ms) was strictly matched by the 23,000–30,500 ms candidate. The provider-free
boundary diagnostic therefore reports known-positive recall **1.0**, zero detection gaps,
zero boundary-headroom cases, and `NO_OBVIOUS_BOUNDARY_HEADROOM`. The raw strict precision
number is **0.25**, but it is **not safe to use for precision tuning**.

The OpenArena annotation is explicitly a sparse visual review with one confirmed frag, not
an exhaustive full-source annotation. The dataset now marks this case `sparse-annotations`,
and the provider-free feasibility tool records `annotation_coverage=sparse`,
`precision_tuning_safe=false`, plus the exact unmatched candidate IDs. Those three unmatched
candidates are **human-review-required**, not confirmed false positives. Do not suppress
Scout candidates, raise score/confidence thresholds, or change ranking defaults from this
0.25 number. In particular, all four candidates have confidence 0.95 and the known-positive
score (8.0) is shared by another unmatched candidate, so a simple score/confidence cutoff
would not separate the current labels safely.

The private review queue is persisted at
`data/external_dev/fps-open-001/openarena-unmatched-candidate-review.json`. It identifies:

- `cand_357fd964f750ee93` (0–5 s): human review required; overlaps one high-audio review interval.
- `cand_00a30c7a3b46451f` (47–51 s): human review required; no high-audio review-queue overlap.
- `cand_e7a545c860337a96` (57–62 s): human review required; overlaps two high-audio review intervals.

No new provider call is needed for this review: the existing source, candidate clips/sheets,
canonical response, and settled Scout output are sufficient. If review confirms additional
highlights, expand **calibration** annotations and rerun provider-free evaluation. Only if
review rejects candidates as genuine non-highlights should false-positive suppression or
ranking changes be tuned against them.

## Offline verification

After the v13 provider phase, the local maintenance verification passed:

- pytest: **256 passed**
- Ruff: **passed**
- mypy: **62 source files clean**
- `git diff --check`: **passed**

These checks made no provider calls.

## Representative long-source requirement

The provider-free 63.48-minute structural long-run is complete through report, using
Fake Scout and zero real Gemini calls. Session `2026-06-17_unknown_d7c2c72db4c6` used
a 3,808,767 ms source, produced 5 windows and 5 candidates, and selected best-of 3.
The resume/report run exited 0 in 1329.587s. Its warm-cache rerun exited 0 in 260.907s
with `report cache: HIT`.

The source remained immutable before and after: size **20,250,210,757**, mtime
`2026-06-17T16:02:07.2383233Z`, and SHA-256
`d7c2c72db4c68ec419792888ad8138b8edba2e5d0e3482597ebf951f8da9572a`.
Session-generated storage was **3,246,248,308 bytes**, including the preserved
**636,747,824-byte** interrupted proxy partial. All five clips total 55 seconds of
review (**1.444037%** of source duration); best-of 3 totals 33 seconds
(**0.866422%**).

Structural long-run quality evaluation remains blocked because the full source has no
legitimate complete human ground truth. Do not fabricate ground truth or reuse partial
calibration annotations as full-source truth.

## Safety and next gate

- Do not make additional provider generation calls without a new explicit attempt and
  exposure authorization.
- Preserve all historical ambiguous ledger entries unless separate evidence supports
  reconciliation.
- Keep private benchmark media, annotations, ledgers, predictions, and absolute source
  paths under ignored local `data/`; never commit them.
- Do not represent the completed structural run as a quality evaluation; complete
  full-source human ground truth is required first.
- Preserve the completed resume, warm-cache, immutability, storage, and review-ratio
  evidence with the private session artifacts.

## 2026-08-28 provider-free remediation readiness

The current remediation branch now has a provider-free M6 dry-run and a separate
`highlight benchmark scout-readiness` authorization checkpoint. The M6 dry-run checkpoint
is commit `5d5afa1`; its full provider-free regression passed **308/308**, Ruff passed, and
mypy reported **69 source files clean**. The readiness addition is verified by targeted
tests plus Ruff and mypy with **70 source files clean**; it does not authorize a live call.

On CPFLE, calibration case `external-fps-openarena-01` is provider-clean and locked to the
human-reviewed external calibration source (not validation/holdout). The source SHA-256 is
`b5365144c0a4a3270877b1796ee980ce2ccbbec215c9450b642181bc9778f77c`; the prepared current
Scout identity is `gemini-3.5-flash-lite` with `gemini-scout-window-v18`, low media
resolution, minimal thinking, one 99,008 ms window, and one planned provider request.

The exact provider-free preflight is **649,624 micro-THB (THB 0.649624)** against
**650,000 micro-THB (THB 0.650000)** available, leaving **376 micro-THB** headroom. The
readiness artifact records zero provider calls, zero remote uploads, zero ledger
reservations, `revealed_validation_used=false`, and `semantic_quality_available=false`.
Independent ledger inspection after readiness also found zero rows in calls, events, and
control. The private readiness JSON remains under ignored calibration data and is not
committed.

This does **not** change the M8 quality verdict. The old live attempt authorization remains
exhausted at **10/10**. A fresh explicit attempt/exposure authorization is required before
the one planned current-v18 Scout generation. Only after that calibration result exists may
provider-free `boundary-feasibility` be used to distinguish current semantic detection gaps
from boundary-only timing headroom. The revealed v13 validation cases remain forbidden for
tuning.

## 2026-08-29 OpenArena candidate adjudication and suppression diagnostic

The sparse OpenArena calibration review is complete for every prediction from the fresh
current-v18 Scout run, without any additional provider call. Local visual review promoted
the 0-5s candidate to an additional `WORTH_REVIEW` combat sequence, retained the existing
24-26s frag as positive, and marked the 47-51s plus 57-62s candidates as boring/traversal
predictions. The private annotation therefore contains two highlights and two fully
covering boring intervals while remaining explicitly **sparse** rather than pretending to
be exhaustive source ground truth.

Re-running provider-free boundary feasibility gives 2/2 currently reviewed highlights
matched, strict precision 0.5, recall 1.0 over the annotated positives, detection gaps 0,
and boundary headroom 0. The artifact now records `precision_tuning_safe=false`,
`candidate_review_complete=true`, and `false_positive_suppression_safe=true`. Existing
Scout `score`/`confidence` lower-bound thresholds have no headroom: neither confirmed
negative can be removed without also dropping at least one reviewed positive.

A new provider-free `benchmark suppression-feasibility` diagnostic then evaluated the
already-computed local audio activity over those same four event intervals. The minimum
positive event peak is **-18.518034 dB**; a calibration-only lower-bound at that value
would retain 2/2 reviewed positives and reject 2/2 confirmed negatives. Weighted mean dB
retains both positives but rejects only 1/2 negatives. The resulting diagnostic verdict is
`AUDIO_PEAK_DB_HEADROOM`, with provider/API calls still **ZERO** for this remediation
stage. This threshold is deliberately not wired into production ranking or Scout capture.

The next gate is cross-case calibration: visually adjudicate additional external-dev
FreeDoom/Xonotic calibration material, then test whether the same local feature remains
useful. Any fresh Gemini Scout generation requires a new explicit attempt/exposure
authorization. The revealed v13 validation set remains forbidden for tuning, and a future
M8 acceptance claim still requires a fresh locked holdout.

## 2026-08-30 cross-source audio-scale sanity

Before assigning any semantic labels to the existing FreeDoom/Xonotic review queues, a
provider-free scale check invalidated the idea of promoting the OpenArena **absolute**
`audio_peak_db` floor globally. FreeDoom has source overall loudness -21.0 LUFS and all
7 queued review intervals exceed the OpenArena -18.518034 dB floor; Xonotic is -31.6
LUFS and all 5 queued intervals fall below it. That all-or-nothing split is already
present without semantic adjudication, so it is a source-mix/loudness confound rather
than cross-game evidence of highlight quality.

The suppression diagnostic therefore also records `audio_peak_over_loudness_db`
(peak activity minus source integrated loudness) as an exploratory prominence feature.
On the reviewed OpenArena candidates, the normalized lower-bound floor is **10.681966 dB**
and still rejects **2/2** confirmed negatives while preserving **2/2** reviewed positives;
the diagnostic verdict is `AUDIO_PEAK_OVER_LOUDNESS_HEADROOM`. This remains
calibration-only and is **not** a production rule. FreeDoom/Xonotic still require visual
semantic adjudication before the normalized feature can be accepted or rejected; no labels
were fabricated and no provider call was made.

## 2026-08-31 provider-free visual adjudication helper

The FreeDoom/Xonotic cross-case gate no longer depends on an ad-hoc static review page.
A local-only `benchmark review-queue` helper now validates the private development queue,
serves only declared review clips over loopback, and persists explicit `POSITIVE`,
`BORING`, or `UNCERTAIN` decisions to a queue-hash-bound sidecar. It supports case
filtering, byte-range video playback, strict review-ID validation, and same-origin
state-changing requests. It never calls a provider and never promotes a review decision
into `BenchmarkAnnotations` automatically.

The live helper was started for `xonotic` + `freedoom` only and exposes **12** review
intervals (5 + 7), with `provider_calls=0`; the initial sidecar has no fabricated semantic
labels. The first full **324-test** regression reached **323 passed / 1 failed** because Windows returned a transient `WinError 5` while renaming the temporary portable boundary-feasibility bundle directory. The affected module then passed **12/12** in isolation. Bundle finalization is now hardened with bounded retry on transient `PermissionError`, and a deterministic regression test injects one failed rename before success. Targeted boundary-feasibility + review-queue tests pass **19/19**, Ruff passes over `src` + `tests`, mypy passes **72 source files**, and the hardened full regression passes **325/325 in 313.44s**. The semantic gate is still
open until those 12 intervals receive real visual decisions; only then may
`audio_peak_over_loudness_db` be evaluated across cases.

## 2026-08-31 provider-free cross-case evaluator

A separate `benchmark cross-case-suppression` gate is now ready before any visual label is
entered. It binds the adjudication sidecar to the exact review-queue SHA, requires complete
coverage of the selected FreeDoom/Xonotic intervals, rejects `UNCERTAIN`, and joins only the
existing provider-free `audio_peak_over_loudness_db` evidence. It reports whether a
positive-preserving lower bound rejects reviewed boring intervals but always keeps
`production_threshold_locked=false`.

The current private queue has no adjudication sidecar yet. A live smoke therefore fails
closed with `Cross-case adjudication sidecar does not exist`; no output threshold or semantic
label is invented and provider/API calls remain **ZERO**. Targeted cross-case + review-queue
+ suppression tests pass **15/15**, Ruff passes over `src` + `tests`, and mypy passes **73
source files**, and the full regression passes **331/331 in 307.33s**. The remaining gate is still real visual review of all 12 intervals, followed
by this evaluator; the revealed v13 validation set remains forbidden for tuning.

## 2026-09-01 visual-review provenance hardening

The review sidecar and cross-case evaluator now preserve reviewer provenance explicitly.
The local loopback review UI writes `reviewer_kind=HUMAN`; assistant/model visual decisions
can instead be stored as `reviewer_kind=ASSISTANT_VISUAL`. The evaluator carries that value
into its private diagnostic artifact while still enforcing `provider_calls=0` and
`production_threshold_locked=false`. Assistant/model labels are development diagnostics
only and must not be represented as human ground truth or used to accept M8.

Focused review-queue + cross-case tests pass **13/13 in 2.43s**, Ruff passes `src` + `tests`,
mypy still passes **73 source files**, and the full regression passes **332/332 in 297.80s**.
No provider/API call was made. The 12 FreeDoom/Xonotic intervals remain semantically
unlabeled until actual visual adjudication is completed.

## 2026-09-01 cross-case scope-binding hardening

The provider-free evaluator now fails closed if an adjudication sidecar repeats a selected
case or names a case absent from the exact review queue. Previously either malformed scope
could be silently reduced to the matching queue cases, while the result still reported the
original selected-case tuple. New regression coverage locks both failure modes down; no
visual decision, media access, provider/API call, or production-threshold change occurred.

The isolated Codex worktree passed changed-file syntax parsing and `git diff --check` but did
not contain the project development environment. Canonical MCP takeover then reran the new
scope-binding regression under the project `.venv`: targeted cross-case tests pass **9/9 in
1.29s**, Ruff passes `src` + `tests`, mypy passes **73 source files**, `git diff --check`
passes, and the full provider-free regression passes **334/334 in 580.48s**. No provider/API
call was made.
