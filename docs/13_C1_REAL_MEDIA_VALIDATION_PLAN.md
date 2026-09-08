# C1 Real-Media Creator Validation Plan

Updated: 2026-09-09 Asia/Bangkok

> Architecture note: the archetypes, creator-review labels and real-media evaluation rules in this document remain valid, but the execution architecture is now superseded by `docs/16_C1_HYBRID_CREATOR_TRIAGE_PLAN.md`. B/C paid inference is paused until the hybrid proposal -> verify -> story -> rank contract is coherent provider-free.

## Purpose

Validate that Creator Candidate Pack Beta is useful on **real gameplay from different creator archetypes**, not only on the historical FPS calibration clips and not only on synthetic fixtures.

This plan deliberately separates two questions:

1. **Detection truth** — did the system describe a real event at the right place?
2. **Creator usefulness** — is the candidate actually worth reviewing/editing for short-form content?

Creator usefulness labels must not be written back into the locked GT v2 event benchmark. They are a separate product-feedback layer.

No provider call is authorized by this document.

## Validation set

Use at least three real source sessions before declaring C1 useful:

### V-C1-A — fast action / competitive

Purpose:
- protect the concrete-event recall lessons learned from v19-v21;
- test visually fast events, pressure, skill, clutch, fail and reaction;
- verify that the creator prompt did not regress obvious gameplay moments.

A historical calibration source may be reused for this archetype because its visual truth is already known, but its benchmark labels are regression evidence only and must not define creator quality.

### V-C1-B — sandbox / survival / exploration

Purpose:
- test quiet visual value;
- test `DISCOVERY`, emergent gameplay, buildup, danger, failure and unusual outcomes;
- specifically verify `silence != dead air` and `no speech != boring` on actual media.

Prefer a source with stretches of low audio activity so the system cannot pass merely by following loudness or voice signals.

### V-C1-C — co-op / social / personality-heavy

Purpose:
- test complete jokes, friend interactions, panic/recovery, reactions, arguments/callouts and personality beats;
- verify that a social moment can be selected because it has an audience payoff even when no important gameplay win occurs;
- verify that random chatter/laughter without a coherent beat does not flood the pack.

Prefer a source with real voice chat and a mix of useful and ordinary conversation.

## Window policy for this validation

Do **not** lock 90-second windows globally because v21 only established useful evidence for two fast FPS events.

First real-media creator pass:
- keep the current creator default window duration configurable;
- use one fixed configuration per source, recorded in the run manifest;
- do not tune window length after looking at the same source's creator labels and then report the retuned result as validation;
- if one archetype clearly needs a different context length, record that as evidence for a later profile-aware experiment.

The flat-Scout baseline default is `gemini-scout-window-v21-editorial-role`, but it is now historical comparison evidence rather than the target semantic architecture. In the hybrid path, fixed windows are transport/coarse-context units only; proposal-centered semantic context may expand dynamically until resolution is verified, contradicted, abandoned, or the context budget is reached. Historical A/B/C provider-free artifacts remain preserved for comparison and cost evidence.

## Provider-free preflight for each source

Before any paid inference:

1. ingest/probe the source locally;
2. build/reuse the local analysis proxy and signals;
3. plan Scout windows locally;
4. verify only analysis-window proxies would be eligible for upload;
5. calculate exact aggregate reservation/cost estimate for the intended windows;
6. record model, media resolution, thinking level, output-token cap, prompt version, window duration/overlap, source duration and number of windows;
7. verify provider generation calls = 0 and uploads = 0;
8. stop and ask for a **fresh explicit authorization + hard THB cap** for that specific real-media run.

If the exact reserve exceeds the authorized cap, do not call the provider.

## Live-run safety rules

When a real-media run is eventually authorized:

- analysis-window proxy only;
- raw source upload forbidden;
- exact scoped source(s) only;
- fresh hard THB cap;
- zero automatic generation retries;
- no retry after failed/ambiguous generation without fresh authorization;
- inspect ledger, provider response metadata and remote cleanup after the run;
- preserve the untouched extracted source candidates alongside reports/drafts.

## Creator review labels

For each candidate, record one primary decision:

- `KEEP` — would reasonably enter an editing pass;
- `MAYBE` — potentially useful but needs context/style judgment;
- `REJECT_BORING` — real interval but not creator-worthy;
- `REJECT_WRONG_EVENT` — description/event is materially wrong;
- `REJECT_BAD_BOUNDARY` — underlying moment is useful but the extracted story is materially cut too early/late;
- `REJECT_DUPLICATE` — redundant with a better candidate.

Also record an **editorial role** independently from the event category:

- `STANDALONE_STORY` — understandable and potentially postable as one self-contained clip after editing;
- `MONTAGE_BEAT` — useful visual/mechanical/social beat that may belong in a compilation but does not carry a complete story alone;
- `NONE` — should not consume creator editing time;
- optional `CONTEXT_ONLY` — useful only as setup/reaction context attached to another beat.

A candidate must not be labelled `STANDALONE_STORY` merely because a real kill/plant/ability event occurred. The evidence must include the actual resolution/payoff/reaction when the story depends on it; for example, a Spike plant is not itself proof of a round win or clutch while enemies remain and the round is unresolved.

Separately record obvious creator-worthy moments that were not surfaced as `MISS_OBVIOUS` with source timestamp and a short explanation.

Do not convert these labels into GT-v2 benchmark truth.

## Measurements

Record per source and aggregate:

- source duration;
- candidate count;
- total candidate review duration;
- **review ratio** = candidate review duration / source duration;
- KEEP / MAYBE / reject counts;
- obvious misses;
- duplicate count;
- bad-boundary count;
- creator-family distribution;
- whether quiet visual moments were represented where appropriate;
- whether social/personality moments were represented where appropriate;
- settled provider cost and cost/source-hour;
- time required for the owner to review the Candidate Pack versus source duration.

Creator score is an ordering signal, not proof of quality. Detection confidence is event/timing certainty, not creator value.

## C1 beta decision rule

C1 does not need benchmark perfection. It is ready to move into owner-selected AI rough editing (C2) when the three-archetype pass shows all of the following:

- end-to-end runs are reliable and auditable;
- reviewing the pack is substantially faster than watching the full source;
- obvious creator-worthy moments are not routinely missed;
- boring/duplicate burden is tolerable rather than dominating the pack;
- extracted boundaries usually preserve enough setup -> event -> payoff/reaction to understand the moment;
- the pack surfaces useful moments across action **and** quieter/social game styles;
- cost/source-hour is acceptable for personal creator use.

If a failure is isolated to one archetype, fix the specific root cause rather than globally retuning around the FPS benchmark.

## Parallel WorkLab execution policy

Use parallel delegation by default whenever the remaining work can be split safely. The owner should not need to remind the project to use parallel agents again.

Recommended operating model:

- **Coordinator + single writer:** the primary agent owns the plan, integrates findings, makes source edits, and decides which findings are real versus false positives.
- **Parallel read-only lanes:** delegate independent work such as plan/contract audit, provider-boundary audit, regression/test execution, diff/semantic review, cache/reuse inspection, and GPU/performance investigation while the main media job is running.
- **One heavy media worker per machine/storage path:** do not start multiple full-source FFmpeg/proxy/local-signal jobs against the same T-small machine or OBS drive merely to create artificial parallelism. Disk I/O, decode, CPU and GPU contention can make total runtime worse.
- **No shared-file writer races:** do not let multiple agents modify the same source/config/docs concurrently. If parallel implementation becomes useful later, assign disjoint ownership by file/module and integrate through the coordinator.
- **Use idle time:** while a long ingest/proxy/local-signals task runs, parallel agents should work on non-contentious audits, tests, preflight preparation, source-selection evidence, documentation, or performance plans instead of waiting serially.
- **C1 correctness before optimization:** GPU/NVDEC/NVENC performance work may be audited and prepared in parallel, but do not change the active C1 benchmark pipeline mid-validation. Merge performance changes only after correctness evidence is preserved and compare them against the baseline.
- **Quality gate for GPU-first work:** a faster RTX 4070 path is acceptable only if candidate recall, boundary quality, analysis semantics and final-source fidelity do not regress. Final clips must continue to come from the original source, not a lossy analysis proxy.

Default VDO concurrency target on T-small during C1 is therefore:

`1 heavy media worker + 2-3 parallel reasoning/audit agents`

not multiple simultaneous full-video workers.

If delegation is temporarily unavailable, continue serially rather than weakening validation or provider-safety rules.

## Execution order

1. Finish provider-free C1 source/tests/report gate.
2. Select/locate V-C1-A, V-C1-B and V-C1-C source media.
3. Run provider-free ingest/proxy/window/cost preflight for each source, using the parallel WorkLab policy above to overlap non-contentious audit/test work.
4. Present exact per-source call counts and caps for authorization.
5. Run one authorized source at a time so early creator evidence can stop a bad experiment cheaply.
6. Generate Candidate Packs and creator review sheets.
7. Review usefulness before any C2 implementation or further model/window tuning.

## Current authorization state

`NO_NEW_PROVIDER_CALL_AUTHORIZED`
