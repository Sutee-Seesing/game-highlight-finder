# Deep Research Context — Game Highlight Finder / Creator Assistant

Updated: 2026-09-08 Asia/Bangkok

## Why this document exists

Use this file for a **targeted implementation audit after external research**, not as a substitute for external research.

Recommended research order:

1. First determine from current products, papers, and production evidence what architecture would make sense **if this project were started from scratch today**.
2. Only then inspect this repository and compare the implementation against that external conclusion.
3. Do not preserve the current architecture merely because work has already been invested in it. Explicitly recommend KEEP / REMOVE / CHANGE / ADD / REBUILD where justified.

The goal is to avoid two opposite errors:

- reading the whole repo first and becoming anchored on its current implementation;
- doing only market research and never checking whether the actual code fails for a fixable local reason.

## North Star

This project exists to help one creator turn long gameplay VODs into useful short-form content opportunities.

Desired workflow:

`play / record normally -> AI watches the VOD -> find creator-worthy raw material -> extract candidates from the original source -> rank shortlist -> owner chooses -> AI rough-edit later -> owner final edit / publish -> real audience feedback improves future ranking/editing`

Typical source length is about 1–4+ hours.

Primary publishing target is TikTok / short-form. The longer-term goal is to build an audience that can later follow the creator into livestreaming.

This project is **not intended to be an FPS kill detector** and must not be defined by Valorant benchmarks. It should work across multiple game styles.

Creator-worthy moments may include:

- funny / absurd / unexpected moments;
- failure, panic, mistakes, self-own;
- reaction / emotion;
- friend, Discord, co-op, personality moments;
- tension -> payoff;
- discovery / emergent gameplay;
- skill / clutch / boss win / escape;
- short stories whose value depends on setup -> event -> payoff;
- visually valuable quiet gameplay with little or no speech.

Hard editorial principle: `silence != dead air` and `no speech != boring`.

## Current pipeline at a glance

The implementation broadly follows:

`source VOD -> ingest/probe -> local analysis proxy -> local audio/activity signals -> overlapping analysis windows -> multimodal Scout -> canonicalize -> reconcile/dedupe -> derive boundaries -> extract from original source -> rank -> creator report`

Important current characteristics:

- final candidate clips are extracted from the original source, not the lossy analysis proxy;
- local signals are intended as navigation hints rather than semantic ground truth;
- current window policy is configurable; recent creator validation used 300 s windows with 30 s overlap;
- Gemini/provider use is opt-in and guarded by a cost ledger, hard budget, upload restrictions, and no automatic generation retry;
- raw source upload is forbidden; only bounded analysis-window proxies may cross the provider boundary when explicitly authorized;
- human approval remains required before anything becomes publish-ready.

## Critical real-world evidence: V-C1-A

The most important recent evidence is not a benchmark score. It is an owner review of actual extracted clips.

A short competitive/FPS source of about 10 minutes was processed with the creator-oriented v20 Scout contract:

- 3 analysis windows;
- 3 paid Gemini generation calls;
- 3 analysis-window uploads;
- 0 automatic retries;
- settled provider cost: **THB 0.996318**;
- 6 raw creator candidates;
- reconcile/extraction/ranking/report completed for 6/6 candidates;
- owner verdict: **0/6 were useful as standalone creator clips**.

A high-value failure example occurred in Valorant:

- the player/team planted the Spike;
- opponents were still alive;
- the round had not resolved;
- the model described the moment in terms equivalent to a clutch / round win / celebration and ended the candidate too early.

This is evidence of **premature semantic closure**: an intermediate event was treated as the final payoff before the source actually demonstrated the outcome.

Other candidates included real gunfights / kills that may be useful mechanical footage but did not constitute complete standalone short-form stories.

Do not reinterpret this 0/6 result as GT-v2 event truth. It is creator-usefulness evidence.

## Current architectural hypothesis after the A failure

The current working hypothesis is that the system should separate **event existence** from **editorial usefulness**.

A candidate may have one of four editorial roles:

- `STANDALONE_STORY` — enough setup + actual resolution/payoff/reaction to be understandable and useful on its own;
- `MONTAGE_BEAT` — real useful ingredient such as a clean kill, aim play, reaction, or visual beat, but not a complete standalone story;
- `CONTEXT_ONLY` — setup, reaction, or supporting context that belongs with another beat;
- `NONE` — a real interval that should not consume creator review time.

The distinction is intentionally separate from event category. For example:

- one clean headshot may be `SKILL + MONTAGE_BEAT`;
- a complete 1v3 ending in a visible round resolution and strong reaction may be `CLUTCH + STANDALONE_STORY`;
- planting an objective while enemies remain must **not** be treated as proof of a win or clutch;
- an isolated setup fragment may be `CONTEXT_ONLY` even if it is relevant to a later story.

The repository currently contains an experimental `gemini-scout-window-v21-editorial-role` contract implementing this hypothesis. Treat it as **an unvalidated architectural experiment**, not as a proven solution.

## Current provider-free status of v21 experiment

After the v21 editorial-role change:

- focused creator / Scout / presentation checks passed;
- full local suite passed **282/282**;
- Ruff passed;
- no new provider generation was used to validate the v21 semantics;
- V-C1-B provider-free preflight: 17 windows, exact estimated reserve **THB 15.224148**, 0 calls / 0 uploads;
- V-C1-C provider-free preflight: 26 windows, exact estimated reserve **THB 23.218504**, 0 calls / 0 uploads.

B and C remain unvalidated by live inference under v21. Do not infer quality from the green local tests or cost preflight.

## What the external research should challenge

Do not assume any of the following are correct:

- that a multimodal LLM/VLM should be the first-pass detector;
- that 300 s + 30 s overlap is the right decomposition;
- that each analysis window should independently produce publish-worthy candidates;
- that proposal generation and creator-value judgment belong in one prompt/model call;
- that candidate boundaries should be produced by the same pass that detects semantic events;
- that event taxonomies and editorial-role taxonomies should be combined;
- that all games should use the same detector path;
- that game-specific telemetry/CV is worth implementing broadly;
- that the current ranking score is meaningful enough to drive review order;
- that AI rough editing should wait until candidate finding is near-perfect.

Compare seriously against architectures such as:

`cheap coarse proposal scan -> event/semantic proposals -> context expansion -> resolution verifier -> creator-value judge -> standalone/montage grouping -> ranking`

and hybrids using:

- local CV/audio heuristics;
- OCR/HUD/game telemetry when available;
- ASR + text reasoning;
- multimodal VLM/LLM;
- game-specific optional plugins;
- generic fallback path for unsupported games.

## Targeted repository audit order

Do **not** read the entire repository indiscriminately. Start with the following files in this order.

### 1. Product intent and current decisions

Read first:

- `docs/10_CREATOR_GROWTH_AND_AI_EDITING_PLAN.md`
- `docs/13_C1_REAL_MEDIA_VALIDATION_PLAN.md`
- `docs/CURRENT_STATE.md`
- `docs/NEXT_ACTION.md`
- `docs/DECISIONS.md`

Useful earlier design context if needed:

- `docs/01_PRODUCT_REQUIREMENTS.md`
- `docs/02_ARCHITECTURE.md`
- `docs/03_PIPELINE.md`
- `docs/04_DATA_MODELS.md`
- `docs/05_COST_STRATEGY.md`

Question to answer after reading these: **Does the implementation still serve the creator-growth North Star, or has it drifted toward event-detection/benchmark optimization?**

### 2. Scout prompt, schema, and model contract

Primary files:

- `src/game_highlight_finder/pipeline/windowed_scout.py`
  - inspect `build_window_prompt(...)`;
  - inspect window request/caching flow;
  - inspect paid Gemini execution boundary.
- `src/game_highlight_finder/pipeline/gemini_contract.py`
  - inspect provider output schema and required/optional fields.
- `src/game_highlight_finder/config.py`
  - inspect Scout defaults, windows, overlap, model, media resolution, output limits.
- `config.example.yaml`
  - compare intended public/default configuration against code.

Questions:

- Does one model call currently conflate proposal generation, semantic interpretation, creator-value scoring, and boundary selection?
- Can the schema represent uncertainty about unresolved outcomes?
- Can it represent `standalone vs montage vs context` cleanly without forcing fake confidence?
- Does the prompt create incentives to over-select laughter, action, or apparent payoff?
- Is the long-window instruction compatible with reliable temporal localization?

### 3. Canonical model and trust boundary

Read:

- `src/game_highlight_finder/domain/models.py`
- `src/game_highlight_finder/domain/canonical.py`

Questions:

- What provider claims become canonical facts?
- Which assertions are locally verifiable and which are merely model-generated text?
- Are event category, editorial role, detection confidence, creator value, and story completeness represented as distinct concepts?
- Can unresolved / incomplete story candidates survive safely without being misrepresented as completed stories?

### 4. Windowing, reconciliation, and temporal boundaries

Read:

- `src/game_highlight_finder/domain/windows.py`
- `src/game_highlight_finder/domain/reconcile.py`
- `src/game_highlight_finder/pipeline/boundary_refinement.py`
- `src/game_highlight_finder/pipeline/boundary_refinement_runner.py`
- `src/game_highlight_finder/pipeline/extraction.py`

Questions:

- Can a story that crosses a window edge be completed correctly?
- Does reconcile merge only duplicate fragments, or can it assemble setup/event/payoff from related but non-overlapping observations?
- Is a fixed pre/post roll being used where semantic expansion/verifier logic would be better?
- Should event localization, story expansion, and final clip-boundary selection be separate stages?
- Is a second-pass resolution verifier required before a candidate is allowed to be `STANDALONE_STORY`?

### 5. Ranking and report behavior

Read:

- `src/game_highlight_finder/pipeline/ranking.py`
- `src/game_highlight_finder/pipeline/report.py`

Questions:

- What does the ranking score actually measure?
- Is creator score merely the Scout's self-reported score?
- Should standalone stories and montage beats be ranked separately rather than in one list?
- Should `CONTEXT_ONLY` / `NONE` be hidden from normal creator review while remaining auditable?
- Is the report optimized for the owner's real task: deciding quickly what to edit/publish?

### 6. Local signals / cheap proposal layer

Read:

- `src/game_highlight_finder/pipeline/local_signals.py`
- `src/game_highlight_finder/media/ffmpeg.py`
- `src/game_highlight_finder/pipeline/proxy.py`

Questions:

- Are existing local signals strong enough only for navigation, or can they form part of a cheap proposal generator?
- What additional generic cheap signals would have good ROI: ASR, OCR, scene/activity transitions, optical flow, audio emotion, HUD change, etc.?
- Which signals should remain game-agnostic and which should be optional game-profile plugins?

### 7. Cost / provider safety

Read:

- `src/game_highlight_finder/cost/ledger.py`
- `src/game_highlight_finder/cost/service.py`
- `src/game_highlight_finder/pipeline/gemini_scout.py`
- `src/game_highlight_finder/providers/gemini.py`
- `src/game_highlight_finder/cli.py`

The cost/safety system is not the current product bottleneck, but audit whether the architecture makes expensive model calls only where they provide unique value.

Questions:

- Could a hierarchical pipeline reduce paid video-minutes materially?
- Could the same or better quality be obtained by expensive judging only around cheap proposals?
- Are there failure modes where cost safety architecture constrains experimentation unnecessarily?

### 8. Tests that encode current assumptions

Read at minimum:

- `tests/unit/test_c1_creator_candidate_pack.py`
- `tests/unit/test_m6.py`
- `tests/unit/test_m7_presentation.py`
- `tests/unit/test_m7_report.py`
- `tests/integration/test_m6_pipeline.py`
- `tests/integration/test_m7_acceptance.py`

For historical benchmark context if needed:

- `tests/unit/test_m8_benchmark.py`
- `docs/07_M8_BENCHMARK_PROTOCOL.md`
- `docs/11_M8_GT_V2_EXHAUSTIVE_AUDIT_PLAN.md`

Question: **Which tests prove engineering correctness, and which accidentally encode a product assumption that external research says should change?**

## Optional local-only evidence if the audit environment can access ignored files

These paths may not exist in a GitHub-only view because `.t/` is local working evidence.

Useful A evidence:

- `.t/c1-real-media-validation/C1_A_LIVE_RESULT.json`
- `.t/v20-window120-preflight/data/sessions/2026-08-15_unknown_7db9940058f7/session_map.json`
- `.t/v20-window120-preflight/data/sessions/2026-08-15_unknown_7db9940058f7/reports/ranking.json`
- `.t/v20-window120-preflight/data/sessions/2026-08-15_unknown_7db9940058f7/reports/index.html`
- per-window `response.raw.json` / `response.canonical.json` below that session's `scout/windows/` folders.

Useful v21 provider-free evidence:

- `.t/c1-real-media-validation/C1_B_PREFLIGHT.json`
- `.t/c1-real-media-validation/C1_C_PREFLIGHT.json`

If these files are unavailable, use the evidence summary in this document rather than assuming a result.

## Files and areas to avoid unless specifically needed

Do not spend audit time crawling:

- `.venv/`;
- caches or archived pytest artifacts;
- unrelated local temp directories;
- secrets / `.env` / API keys;
- large private/raw media merely to understand code architecture;
- old benchmark run logs unless a concrete historical claim needs verification.

Never expose or quote credentials/secrets even if the environment can access them.

## Specific root-cause analysis requested for the plant -> win failure

Determine how much of this failure is attributable to each layer:

1. **Model capability** — the VLM failed to maintain/understand game state.
2. **Prompt incentives** — creator-highlight language encouraged premature narrative completion.
3. **Schema/role conflation** — the same candidate record represented event existence and editorial story quality.
4. **Windowing** — the decisive resolution may have been outside the model's effective attention or candidate interval.
5. **Boundary design** — the Scout was asked to decide clip start/end before the outcome was verified.
6. **Missing verification stage** — there was no independent rule/model pass requiring evidence of actual resolution.
7. **Game-state knowledge** — generic VLM knowledge may be insufficient for objective/round semantics without optional game-specific cues.
8. **Ranking/reporting** — an uncertain/incomplete candidate may have been surfaced as if it were a high-value standalone clip.

Do not stop at "improve the prompt" if the failure is architectural.

## Required comparison to first-principles architecture

After completing external research and the targeted code audit, produce a gap table with columns:

- capability / stage;
- best-practice or evidence-backed design from external research;
- what this repo currently does;
- evidence from specific source files;
- risk / failure caused by the gap;
- recommendation: KEEP / REMOVE / CHANGE / ADD / REBUILD;
- expected quality impact;
- cost/latency/complexity impact;
- priority.

Then propose a clean architecture **as if historical implementation cost were zero**.

Only after that, provide the lowest-risk migration path from the current repo to the recommended architecture.

## Product decision criteria

The system should not be declared useful because it can complete the pipeline or because event precision/recall improves.

Creator-facing evidence should include:

- source hours -> candidate review minutes;
- percentage of surfaced items the owner would actually send to editing;
- standalone-story precision;
- montage-beat usefulness;
- obvious creator-worthy moments missed;
- bad-boundary / premature-payoff rate;
- duplicate/redundant burden;
- time from finished VOD to a publishable draft;
- provider cost per source hour and per accepted candidate;
- candidate -> selected -> edited -> published conversion;
- owner edit distance / manual touch-up time for future AI drafts;
- post performance and creator-specific learning once enough real posts exist.

A technically correct event that the creator would never use is not a successful creator candidate.

## Questions the final audit must answer

1. Is this project direction viable for one creator's real workflow?
2. If viable, what is the differentiated value compared with SteelSeries Moments / Medal / Outplayed / other event auto-clippers and generic AI clippers?
3. Which current modules are genuine assets worth preserving?
4. Which current modules solve commodity problems that should be replaced/reduced?
5. Should proposal generation and creator/story judging be split?
6. Should expensive multimodal inference be applied to full windows or only proposed semantic neighborhoods?
7. Is `STANDALONE_STORY / MONTAGE_BEAT / CONTEXT_ONLY / NONE` a useful production taxonomy, or should it be replaced?
8. What is the most robust generic method for verifying a story's actual resolution before calling it standalone?
9. How should optional game-specific telemetry/HUD/event detectors plug into a multi-game fallback architecture without turning the project into per-game maintenance hell?
10. Where should ASR/text reasoning fit relative to video VLM inference?
11. Should boundary selection be a separate fine-resolution pass?
12. When should AI rough editing happen: before or after human candidate selection?
13. What is the smallest MVP that would save the owner meaningful time even if AI never predicts virality?
14. Based on evidence, should the project **continue, pivot, simplify, partially rebuild, or stop**?

## Desired final deliverable

Make the result decision-oriented, not a literature dump.

At minimum include:

1. Executive verdict.
2. External competitive/research findings.
3. Current-repo architecture map.
4. Plant->win root-cause decomposition.
5. First-principles recommended architecture.
6. Current vs recommended gap table.
7. KEEP / REMOVE / CHANGE / ADD / REBUILD decisions.
8. Recommended taxonomy and stage contracts.
9. MVP and beta acceptance criteria.
10. Cost/latency implications.
11. Major risks and reasons this may fail.
12. 30-day implementation plan ordered by learning value, not coding convenience.
13. A clear recommendation: continue / pivot / simplify / rebuild / stop.

The audit should explicitly distinguish:

- facts supported by repo code;
- facts supported by real owner-review evidence;
- external research/product evidence;
- hypotheses that still require an experiment.
