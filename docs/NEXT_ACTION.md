# VDO Next Action

Updated: 2026-09-09 Asia/Bangkok

## Current gate

Stop the paid window-length experiment loop. v20/v21 supplied enough evidence that shorter context helps fast-event detection, but the product North Star is broader: multi-game creator content discovery for audience growth.

No new provider call is currently authorized.

## Exact next objective

Implement the **C1 Hybrid Creator Triage + Story Verification** pivot from `docs/16_C1_HYBRID_CREATOR_TRIAGE_PLAN.md`. Preserve the existing media/extraction/cost/report substrate, but replace the monolithic semantic Scout center with provider-neutral proposals -> focused semantic judgment -> factual resolution verification -> story assembly/boundaries -> creator ranking. Do not start paid inference or C2 AI rough editing until this hybrid contract is stable provider-free.

### C1 implementation progress

Completed provider-free:

1. Added generic `DISCOVERY` to the controlled candidate taxonomy.
2. Added backward-compatible `moment_summary` (what happened) and `creator_reason` (why review it for short-form); legacy responses fall back to `reason`.
3. Kept creator/editorial score separate from detection/timing confidence and exposed an explicit `creator_score` in ranking artifacts.
4. Added creator-balanced `gemini-scout-window-v20-creator` as the first creator prompt; after A owner review failed standalone usefulness, provider-free revision `gemini-scout-window-v21-editorial-role` became the default contract with explicit standalone-vs-montage roles and real-resolution requirements.
5. Added hard prompt/editorial principles: `silence != dead air`, `no speech != boring`, and audio activity is evidence rather than an inclusion gate.
6. Preserved setup -> event -> payoff behavior and creator-facing semantics through canonicalization/reconciliation.
7. Converted report cards toward `Creator Candidate Pack`: `What happened`, `Why review this`, creator score, detection confidence, Open Clip, with technical lineage secondary.
8. Added focused creator tests plus offline archetype fixtures for quiet visual discovery, social/co-op payoff without gameplay payoff, tension/fail story, boring-zero-candidate behavior, and legacy compatibility.
9. Added a provider-free end-to-end C1 integration smoke test covering window payload -> canonicalization -> reconcile -> clip boundaries -> ranking -> extraction manifest -> Creator Candidate Pack report. Focused result: **4 passed in 0.56s**.
10. Generated an actual local five-family Creator Candidate Pack demo (`DISCOVERY`, `FUNNY`, `FAIL`, `REACTION`, `SKILL`) with playable synthetic MP4 fixtures and thumbnails; provider calls **0**.
11. Fresh complete local suite after the M6 local-windows safety fix is green: **280 passed in 226.41s (0:03:46)** via durable task `9c4044f7-41fb-4918-9bd1-6d26029b02c6`.
12. Synced `config.example.yaml` to the current creator contract; it now defaults to `gemini-scout-window-v21-editorial-role` so copied configs include editorial-role and resolution rules.
13. Added `docs/13_C1_REAL_MEDIA_VALIDATION_PLAN.md` with separate creator-review labels/metrics and three required archetypes.
14. Prepared V-C1-A fast-action cost preflight using the known cal-01 real source: 3 creator-prompt windows, **estimated aggregate reserve ฿2.514798**, provider calls **0**, uploads **0**, live authorization **false**.
15. Deep Research pivot plan is now canonical at `docs/16_C1_HYBRID_CREATOR_TRIAGE_PLAN.md`; architecture/pipeline/data-model/implementation docs are marked to preserve historical infrastructure while superseding the monolithic semantic Scout center.
16. Hybrid H1 implemented provider-free: `StoryState`, `ResolutionState`, evidence-backed `CandidateClaim`/`ClaimStatus`, and ranking gate that blocks unresolved/contradicted `STANDALONE_STORY` candidates from normal creator review.
17. Hybrid H2 implemented provider-free: provider-neutral `Proposal` / `ProposalArtifact` contracts plus deterministic local activity-signal adapter. Proposal records intentionally contain no creator score or editorial role.
18. Hybrid H3 implemented provider-free: proposal-centered context planner with bounded initial context, explicit `needs_more_context` expansion, source-edge clamping, and maximum-context exhaustion.
19. Hybrid H4/H5 contracts are implemented provider-free: sequential fake semantic judgments can request bounded context expansion, while a separate `CandidateVerification` / fake verifier contract alone may attach verified or contradicted terminal evidence.
20. Hybrid H6 is implemented provider-free: `StoryAssembly` records setup/event/payoff/reaction semantic boundaries and refuses standalone story assembly until the candidate is `COMPLETE` with `VERIFIED` or `NOT_APPLICABLE` resolution.
21. Added provider-free hybrid orchestration and persistence: proposal -> dynamic context -> semantic judgment -> factual verification -> story assembly -> creator-review eligibility -> clip-boundary derivation, with full/review maps persisted under `session/hybrid/`.
22. Added a separate creator-evaluation corpus for `KEEP` / `MAYBE` / rejection reasons, owner editorial-role corrections, desired boundary corrections, review time and `MISS_OBVIOUS`; these product labels do not mutate event GT.
23. Provider-free CLI/session workflow is integrated without replacing the historical flat-Scout path: `hybrid proposals`, `hybrid route`, `hybrid run-fixture`, `hybrid review-template`, and `hybrid review-summary` persist/consume hybrid artifacts deterministically.
24. H2 evidence enrichment/routing is implemented: source-bound `ManualProposalMarkerSet`, source-bound `TranscriptFixture` / `ASR_UTTERANCE`, compatible-neighborhood clustering, `ProposalSummary`, and explicit routing decisions that preserve deferred weak evidence for audit.
25. Creator review round-trips deterministically from review map -> editable worksheet -> separate creator-evaluation corpus/summary with KEEP/MAYBE/rejection, role/boundary corrections, review time and `MISS_OBVIOUS` metrics; the historical Spike replay is a mandatory provider-free semantic regression.
26. Transcript/routing full-suite validation is green: **309 passed in 177.92s (0:02:57)** via task `3c26aa87-0ab2-4e11-8f3d-30f7a2e62ad5`. Latest focused Hybrid/CLI/M6 regression is **47 passed in 56.37s**; Ruff is green and `mypy src` is green across **72 source files**.

### Immediate provider-free work

**C1 EXECUTION RESUMED UNDER RESTRICTED FILE-SCOPE MODE.** Continue normal VDO source/test/media work, but do **not** mass-rename, move a repo/worktree, move a large folder, run recursive cleanup, or create a new workspace structure until the Supervisor Cleanup/Scope fix is ready. Keep the existing canonical paths stable. The pre-cleanup preservation record remains in `docs/14_PRE_REORG_CHECKPOINT_2026-09-07.md`; the scoped cleanup record is in `docs/15_VDO_FOLDER_CLEANUP_2026-09-07.md`.

1. Preserve the live A baseline exactly: 3 calls, settled cost **฿0.996318**, 6 extracted candidates, owner standalone usefulness **0/6**. Treat it as the regression baseline for the architecture pivot.
2. H1 is implemented: preserve the new story/resolution/claim invariants and extend tests rather than weakening the standalone-review gate for convenience.
3. Keep the Spike case as a mandatory provider-free semantic regression: plant/objective state change is allowed, but `ROUND_WON` / `CLUTCH_WIN` cannot become verified without terminal evidence.
4. H2 is implemented as an interface proof. Next enrich proposal sources (ASR/visual/OCR/game adapters) only after the core orchestration is coherent; do not promote loudness/activity into creator truth.
5. Keep v21 flat Scout artifacts/configs as historical baseline only. Do not spend on a v22/v23 monolithic prompt loop as the main fix.
6. B and C remain fully prepared but paused: B **17 windows / ฿15.224148 preflight**, C **26 windows / ฿23.218504 preflight**, provider calls 0 / uploads 0 for both. These cost artifacts are historical planning evidence until the hybrid architecture reaches a new provider boundary.
7. CLI/session integration and deterministic owner-review round-trip are implemented. Preserve those contracts and use them for local learning rather than rebuilding another parallel review path.
8. H2.1/H2.2 evidence enrichment is implemented: source-bound manual markers, source-bound transcript/ASR fixtures, compatible-neighborhood clustering, persisted `ProposalSummary`, and auditable proposal routing. Transcript is factual retrieval evidence for social/personality/joke setup; do not assume speech is required for highlights or that speech presence implies creator value.
9. Historical A proposal-density evidence is **860 weak audio anchors -> 84 clustered neighborhoods over 600.886s (~503.26 neighborhoods/source-hour)**. Coverage-only routing diagnostics select ~119.8/hour at 30s, ~59.9/hour at 60s, or ~30.0/hour at 120s. These are experiment points only; do not lock a universal interval.
10. Existing B and C local-signal artifacts produce **0 proposal neighborhoods for B and 0 for C** under the current factual activity adapter. Treat the A-too-dense/B-C-empty asymmetry as evidence that local audio/activity is only optional navigation evidence, not as a request to tune one universal loudness threshold.
11. Preserve the historical Spike replay invariant: a semantic `ROUND_WON` / `CLUTCH_WIN` hypothesis cannot enter standalone review when the independent verifier lacks terminal evidence.
12. Next provider-free learning step: add or exercise one richer non-audio factual evidence source on a non-FPS/social source (transcript first where a local transcript fixture is available; otherwise a bounded visual/OCR fixture) and measure whether it raises proposal recall without exploding selected semantic-inspection density.
13. Design the future real semantic-judge/verifier provider boundary and exact cost preflight separately from the old full-window Scout contract. The provider boundary must consume routed proposal-centered context, never infer authorization from normal chat continuation, and remain blocked until a fresh explicit hard THB authorization exists.
14. Keep existing ingest/proxy/source identity/original-source extraction/reconcile/cost ledger/report infrastructure unless a concrete regression proves it must change.
15. Keep one coordinator/single writer and preserve current benchmark/config paths. Do not optimize GPU/media behavior mid-semantic validation.
16. Run focused + full regression, final handoff/diff checks, and checkpoint each coherent provider-free slice before any paid inference.

Do not lock 90-second, 120-second or 300-second windows as a universal semantic unit. Window duration remains transport/coarse-context infrastructure until the hybrid pipeline has real creator evidence.

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
