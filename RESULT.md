# M8 Autonomous Remediation + Benchmark Run

Date: 2026-08-19
Task: `m8-autonomous-remediation-2026-08-19`
Outcome: **BLOCKED before calibration freeze; no sealed validation calls were made.**

## Decision

The run does not have an evidence-backed M8 pass. Calibration could not be
frozen because the first v11 call for `m8-real-cal-02` ended with provider
status `incomplete`; the hard cost ledger correctly recorded it as
`AMBIGUOUS`, not as a completed prediction. A same-revision semantic retry is
forbidden. The task had six generation attempts in this run, leaving only two
of the eight allowed attempts, while a clean retry plus the two required
one-time validation calls would require three attempts. The remaining
paid-equivalent exposure is also insufficient for that sequence.

Validation therefore remains sealed and was intentionally not run.

## Investigation and local remediation

- The locked v8 artifacts were audited before new calls. Both high-resolution
  calls completed and were cleaned remotely, but their raw window-relative
  outputs included impossible timestamps such as `905000` ms and `932000` ms
  in roughly 600-second windows. The old canonical path rejected the whole
  response.
- The production source frames are 2560x1440. The v8 analysis proxy was
  854x480 with audio retained; local source/proxy frame inspection showed
  material HUD and detail loss. Provider `media_resolution: high` changes
  provider tokenization, not the pixels in the uploaded proxy. Clean v9-v11
  runs used a derived 1280x720 H.264/NVENC proxy with AAC audio retained.
  Raw originals and full-timeline review proxies were not provider inputs.
- Prompt v6 explicitly requires full-window visual inspection, visible anchors,
  visual outcomes ahead of routine audio banter, and window-relative timestamps
  inside the exact supplied bounds. It contains no locked timestamp or
  category information.
- The canonical trust boundary now validates authoritative requested window
  bounds and rejects out-of-window timestamps for matches, candidates, setup,
  payoff, and evidence. A candidate fragment with any impossible timestamp is
  dropped with a warning while valid sibling candidates survive; it is not
  clamped into a misleading event. Recovering v11 case01 dropped the invalid
  905000-ms fragment and retained five valid candidates without another
  provider call.
- Fixed exact whole-second FFmpeg extraction formatting (`20_000` ms now
  becomes `20`, not `2`).
- Dataset `benchmark_id=m8-real-v1` versus immutable annotation
  `benchmark_id=m8-private` compatibility is now fail-closed: it requires
  matching case ID, split, source hash, annotation hash, and the corresponding
  locked case entry. No annotation contents or hashes were changed.

## Lock and calibration evidence

The ground-truth lock verification passed: four cases, 10 highlights, three
`MUST_CATCH`, six `WORTH_REVIEW`, one optional, and four boring intervals;
owner-confirmed, locked before provider benchmarking, and no provider
predictions in the lock.

Completed clean calibration evaluations were:

| Revision | Case | Precision | Recall | MUST recall | WORTH recall | FP / predictions | Review ratio | Best-of useful recall |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| v9 | cal-01 | 0.250 | 0.333 | 0.000 | 0.500 | 3 / 4 | 11.98% | 0.333 |
| v9 | cal-02 | 0.000 | 0.000 | 0.000 | 0.000 | 4 / 4 | 15.31% | 0.000 |
| v10 | cal-01 | 0.200 | 0.333 | 0.000 | 0.500 | 4 / 5 | 28.96% | 0.333 |
| v10 | cal-02 | 0.000 | 0.000 | 0.000 | 0.000 | 2 / 2 | 13.23% | 0.000 |
| v11 | cal-01 | 0.200 | 0.333 | 0.000 | 0.500 | 4 / 5 | 14.01% | 0.333 |

The v11 cal-01 median matched-event IoU was 0.3125, with median end-boundary
error 8000 ms. Its four false positives were not inside boring annotations.
The v11 cal-01 output still missed the locked MUST_CATCH moment. v11 cal-02
has no completed evaluation: its partial response contained a routine audio
candidate and an enormous out-of-window setup timestamp, then ended
`incomplete`; its manifest remains pending and its cost remains ambiguous.

## Provider accounting and cleanup

- Task generation attempts: 6 / 8. This includes two settled v9 calls, two
  settled v10 calls, one settled v11 call, and one v11 ambiguous call. A v9
  released pre-generation ledger record was not counted as a generation
  attempt; its provider-side recovery produced the settled call recorded above.
- Settled task exposure: **9.391976 THB**.
- v11 ambiguous reserved exposure: **2.601384 THB**; no settled amount was
  invented. Worst-case tracked exposure is **11.993360 THB**, below the
  15-THB cap, but not enough for the required three remaining calls.
- FX snapshot used: 1 USD = 33.067 THB, captured
  `2026-08-19T11:58:00Z`, source `chatgpt_currency_exchange_rate_source`.
- Every v8-v11 provider attempt has local remote-file metadata with
  `deletion_status: deleted`; requests used `store=false`. The provider
  metadata's post-delete state field is retained as an audit snapshot and was
  not treated as a deletion failure.

## Verification

- Latest focused temporal-salvage regression: 13 passed.
- Full pytest: **255 passed in 134.08s**.
- Ruff: passed.
- mypy on `src`: passed, 62 source files.
- `git diff --check`: passed.
- Private provider/benchmark artifacts remain untracked and uncommitted.
  The pre-existing `.t/` directory was not touched or added.

## Blocker

The remaining blocker is external provider completion plus the hard attempt
and exposure limits, not a local evaluator or media-boundary defect. Local
remediation, lock verification, regression coverage, and truthful ledger /
cleanup accounting are complete. A future run needs a new authorized budget or
attempt allowance before it can make a clean calibration retry and then execute
both sealed validation cases exactly once.

Commit and push parity are recorded in `status.json` after publication.
