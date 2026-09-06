# VDO Next Action

Updated: 2026-09-07 Asia/Bangkok

## Current gate

Stop the paid window-length experiment loop. v20/v21 supplied enough evidence that shorter context helps fast-event detection, but the product North Star is broader: multi-game creator content discovery for audience growth.

No new provider call is currently authorized.

## Exact next objective

Finish provider-free hardening of **Creator Candidate Pack Beta (C1)**, then prepare a real multi-game validation proposal. Do not start paid inference or C2 AI rough editing until C1's local contract is stable.

### C1 implementation progress

Completed provider-free:

1. Added generic `DISCOVERY` to the controlled candidate taxonomy.
2. Added backward-compatible `moment_summary` (what happened) and `creator_reason` (why review it for short-form); legacy responses fall back to `reason`.
3. Kept creator/editorial score separate from detection/timing confidence and exposed an explicit `creator_score` in ranking artifacts.
4. Added creator-balanced `gemini-scout-window-v20-creator` and made it the default prompt version. Gameplay, funny, reaction, friend/social, discovery, WTF/unexpected, fail, tension/payoff, skill/clutch and personality-driven stories are peer targets.
5. Added hard prompt/editorial principles: `silence != dead air`, `no speech != boring`, and audio activity is evidence rather than an inclusion gate.
6. Preserved setup -> event -> payoff behavior and creator-facing semantics through canonicalization/reconciliation.
7. Converted report cards toward `Creator Candidate Pack`: `What happened`, `Why review this`, creator score, detection confidence, Open Clip, with technical lineage secondary.
8. Added focused creator tests plus offline archetype fixtures for quiet visual discovery, social/co-op payoff without gameplay payoff, tension/fail story, boring-zero-candidate behavior, and legacy compatibility.
9. Added a provider-free end-to-end C1 integration smoke test covering window payload -> canonicalization -> reconcile -> clip boundaries -> ranking -> extraction manifest -> Creator Candidate Pack report. Focused result: **4 passed in 0.56s**.
10. Generated an actual local five-family Creator Candidate Pack demo (`DISCOVERY`, `FUNNY`, `FAIL`, `REACTION`, `SKILL`) with playable synthetic MP4 fixtures and thumbnails; provider calls **0**.
11. Fresh complete local suite including the newest integration test is green: **278 passed in 222.38s (0:03:42)** via durable task `9d34102e-75e6-4b17-a751-02b343a33ed6`.
12. Synced `config.example.yaml` to the creator prompt default `gemini-scout-window-v20-creator` so copied configs do not silently fall back to v18.
13. Added `docs/13_C1_REAL_MEDIA_VALIDATION_PLAN.md` with separate creator-review labels/metrics and three required archetypes.
14. Prepared V-C1-A fast-action cost preflight using the known cal-01 real source: 3 creator-prompt windows, **estimated aggregate reserve ฿2.514798**, provider calls **0**, uploads **0**, live authorization **false**.

### Immediate provider-free work

1. C1 local implementation/test gate is green at **278 passed**; preserve this checkpoint and do not retune source/ranking from FPS-only evidence.
2. Keep V-C1-A as the ready fast-action source. Its three local analysis-window proxies are already generated provider-free; after migration, copy/recreate the ignored local media artifacts before continuing real-media validation and recompute the A reserve from the actual proxy files before any live authorization.
3. Creator-review and obvious-miss templates are ready locally under `.t/c1-real-media-validation/`; keep them separate from GT v2 and recreate/copy them on the next machine if needed because `.t/` is intentionally not committed.
4. Continue pixel-based source scouting and select owner-approved real media for V-C1-B sandbox/survival/exploration and V-C1-C social/co-op/personality-heavy without using the sealed validation holdout as tuning data; then run provider-free ingest/proxy/window/cost preflight for B and C.
5. Only then present exact per-source call counts and proposed hard caps. Any provider call requires a fresh explicit authorization and cap; run one source at a time.

Do not lock 90-second windows as a universal multi-game default solely from the FPS v21 result; keep duration configurable until creator validation spans multiple gameplay archetypes.

## Creator-beta acceptance

Do not require benchmark perfection. Creator Candidate Pack Beta is useful when:

- a long VOD can complete end-to-end reliably;
- the pack is much faster to review than the full source;
- boring burden is tolerable;
- obvious creator-worthy moments are not routinely missed;
- setup/payoff context is good enough for an editing pass;
- behavior remains useful across different game styles;
- cost/source-hour is reasonable.

## AI rough editor (C2) — immediately after C1 is usable

Do not build a fully autonomous TikTok editor first. Add an owner-selected-candidate rough-editor path that produces non-destructive draft variants.

First target features:

- remove/tighten dead air;
- suggest/select a hook beat;
- 9:16 safe reframing;
- draft subtitles;
- loudness cleanup;
- restrained punch-ins/zooms;
- optional on-screen hook/caption suggestions;
- preserve original extracted candidate beside all drafts.

C2 must be conservative by default: clean/minimal first, preserve setup/event/payoff and reaction tails, avoid unjustified effects, and keep every draft reversible/traceable to source timestamps. If edit confidence is low, produce a small set of variants (for example Clean / Balanced / Story-Hook) rather than forcing one cut. Human approval is required before publish-ready output.

Prefer model-generated edit decisions + deterministic FFmpeg transforms before fully generative video editing.

## Later creator feedback loop (C3)

Once the channel has real posts, collect candidate selected/rejected, edit choices, final duration, and post-performance signals. Use them to tune creator-specific ranking/editing. Keep these labels separate from event-detection benchmark truth.

Canonical roadmap:

`docs/10_CREATOR_GROWTH_AND_AI_EDITING_PLAN.md`

## Hard rules

- No new provider call without fresh explicit authorization.
- No automatic provider generation retry.
- Never upload raw source.
- Do not overwrite GT history.
- Do not treat FPS kill benchmarks as the product definition.
- Do not clean/reset/stash/drop unrelated dirty work.
