# M8 v13 Current Status

This note is the authoritative M8 status checkpoint for the `m8-v12-revision-planner`
branch until older milestone text in README and the M8 planning documents is normalized.
Historical statements such as `M8B2 NOT RUN`, `v3 NOT RUN`, or `validation remains sealed`
are superseded by this checkpoint where they conflict.

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

Full M8 acceptance still requires a representative 1-4 hour source through
analysis -> report -> evaluation with resume, source immutability, budget, storage,
and review-ratio evidence. A provider-free structural run was started on a roughly
63.5-minute source using Fake Scout. An interrupted proxy attempt was recovered on the
same session; the interrupted attempt was preserved as `FAILED / INTERRUPTED`, and a
second proxy attempt completed together with local signals. The latest observed state
before remote-control access became unavailable was downstream window preparation in
progress. This checkpoint does **not** claim report or long-source evaluation complete.

The long source does not yet have human-complete ground truth for its full duration.
Partial annotations from a shorter derived calibration clip must not be treated as
full-source truth. Structural pipeline evidence may be completed provider-free, but the
long-source quality evaluation remains gated on genuine human review.

## Safety and next gate

- Do not make additional provider generation calls without a new explicit attempt and
  exposure authorization.
- Preserve all historical ambiguous ledger entries unless separate evidence supports
  reconciliation.
- Keep private benchmark media, annotations, ledgers, predictions, and absolute source
  paths under ignored local `data/`; never commit them.
- Finish the long-source structural run and warm-cache/source-immutability/storage
  checks locally when remote access is available.
- Normalize stale README / `00_START_HERE.md` / `06_IMPLEMENTATION_PLAN.md` /
  `07_M8_BENCHMARK_PROTOCOL.md` status text after reviewing the local diff.
