# M8 Real-Gameplay Benchmark Checkpoint

Date: 2026-08-21
Task: `m8-v13-live-validation-2026-08-21`
Outcome: **SEALED VALIDATION EXECUTED; M8 QUALITY GATE NOT ACCEPTED.**

## Decision

The provider execution path, cost ledger, exactly-once controls, cleanup, local
reconciliation/extraction, and benchmark evaluator all completed successfully. The
locked validation quality is not good enough to lock V1 defaults or claim M8
acceptance. The validation set is now observed and must not be used for prompt or
threshold tuning as though it were still an untouched holdout.

## Calibration checkpoint

The accepted functional Scout semantics were frozen before sealed validation. The
v12 retry changed the revision/cache namespace only; after normalizing that revision
token, the prompt text matched the preceding frozen semantics exactly. Completed
calibration evidence included v11 `m8-real-cal-01` (precision 0.200, recall 0.333,
MUST_CATCH recall 0.000) and the clean v12 `m8-real-cal-02` retry (precision 0.000,
recall 0.000). Historical ambiguous calls remain untouched audit evidence.

## Sealed v13 validation

Both authorized validation generation attempts ran exactly once with
`gemini-3.5-flash-lite`, the frozen 1280x720 analysis-proxy path, retained audio,
`media_resolution=high`, and the v13 identity namespace. No automatic retry ran.
Both provider calls were `SETTLED`, and both remote media objects were deleted.
Evaluation ran locally only after provider execution completed.

| Case | Precision | Recall | F1 | MUST recall | WORTH recall | Review ratio | Settled cost |
|---|---:|---:|---:|---:|---:|---:|---:|
| `m8-real-val-01` | 0.200 | 0.333 | 0.250 | 0.000 | 0.500 | 13.49% | 1.900338 THB |
| `m8-real-val-02` | 0.000 | 0.000 | N/A | N/A | 0.000 | 2.74% | 1.856908 THB |

Across the two validation cases there were 1 TP, 7 FP, and 4 FN over 8
predictions and 5 locked highlights: precision **0.125**, recall **0.200**, F1
**0.154**, and locked MUST_CATCH recall **0.000**. Count-weighted review time was
about **8.11%** of source duration. This is a product-quality failure, not an
infrastructure or evaluator failure.

## Cost and attempt accounting

- Authorized cumulative generation-attempt cap: **10**; used: **10 / 10**.
- Known settled exposure across the v9-v13 task sequence: **15.041438 THB**.
- Preserved ambiguous reserved exposure: v11 **2.601384 THB** plus v12
  **2.586075 THB** = **5.187459 THB**.
- Cumulative worst-case tracked exposure: **20.228897 THB**, below the authorized
  **23 THB** cap.
- v13 validation settled exposure: **3.757246 THB**.
- No v13 call is reserved, in-flight, or ambiguous; both remote-file cleanup
  receipts say `deleted`.

## Verification

After sealed evaluation, the provider-free verification suite passed:

- Full pytest: **256 passed in 102.30s**.
- Ruff: **passed**.
- mypy: **passed, 62 source files**.
- `git diff --check`: **passed**.
- Private media, annotations, provider responses, ledgers, and benchmark artifacts
  remain ignored/uncommitted.

## Remaining M8 gate

M8 is not accepted. The locked validation results miss the required quality bar, and
the protocol also requires a representative 1–4 hour source to complete
analysis -> report -> evaluation with resume, source-immutability, budget, storage,
and review-ratio evidence. A real 63.48-minute OBS source has verified provenance and
SHA-256, but no complete human-reviewed ground truth exists for the entire long source.
The model must not fabricate that ground truth. A future quality iteration therefore
needs a new calibration strategy and a fresh human-owned holdout before another
scientifically valid acceptance claim.

The production revision-planner change remains on feature branch
`m8-v12-revision-planner` at `bbb696b273f6a1d3522f9d1939ec9efaa925d5b3` before
this checkpoint update. No PR is created by this checkpoint.
