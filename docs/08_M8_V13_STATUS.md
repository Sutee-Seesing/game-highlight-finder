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
