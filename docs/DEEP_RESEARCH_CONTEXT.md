# Deep Research Context — Game Highlight Finder / Creator Assistant

Purpose: give an external research/audit agent enough project context to evaluate this repository from first principles, without being anchored to the current implementation.

## 1. Research objective

Evaluate whether this project should continue, pivot, simplify, or stop.

Do **not** assume that multimodal LLM/VLM inference is the best solution, and do **not** preserve the current architecture merely because it already exists. Compare the implementation against the best architecture one would choose if starting today.

The desired product is not an FPS kill detector. It is a multi-game creator assistant for long gameplay VODs (roughly 1–4+ hours) that reduces manual review and helps a creator find raw material worth turning into short-form content.

Target workflow:

```text
play / record normally
  -> AI/local systems review VOD instead of creator
  -> find creator-worthy raw material
  -> cut candidates from original source
  -> rank / classify candidates
  -> human selects
  -> optional AI rough edit later
  -> human final edit / publish
```

The long-term creator goal is to build an audience on TikTok/short-form and eventually convert that audience into livestream viewers.

## 2. What counts as a useful highlight

Highlights are broader than combat events. Useful material can include:

- funny / comedy
- fail
- reaction
- friend / co-op / social moment
- WTF / unexpected event
- tension -> payoff
- discovery
- personality-driven moment
- skill / clutch
- boss win
- escape / near miss
- emergent gameplay
- a short self-contained story with setup -> event -> payoff

Silence is not disqualifying. A moment with little/no speech can still be valuable when gameplay itself contains tension, danger, discovery, skill, comedy, or story value.

## 3. Current architecture baseline

Repository docs describe a local-first modular Python CLI with versioned JSON artifacts, a SQLite cost ledger, FFmpeg/ffprobe adapters, optional transcription, provider abstraction, overlapping Scout windows, deterministic reconciliation, extraction from the original source, local ranking/reporting, and a separate benchmark layer.

High-level implemented/current pipeline concept:

```text
long VOD
  -> local ingest / source identity
  -> local analysis proxy
  -> local audio/activity signals
  -> overlapping video windows
  -> multimodal Scout
  -> canonicalize / validate
  -> reconcile / dedupe
  -> derive candidate context/boundaries
  -> extract candidate from original source
  -> rank
  -> creator report / human review
```

Important principles already present and worth evaluating rather than blindly keeping:

- original source stays local and unchanged
- cloud model receives only derivative/proxy media
- final candidate extraction uses original source rather than lossy proxy
- provider output is treated as untrusted input
- cost ledger / reservation / hard budget / ambiguous-call accounting
- no blind automatic retry after ambiguous paid calls
- resumable file-based pipeline
- game-specific profiles are optional hints; generic path should still work
- benchmark/evaluation should not trigger provider calls

## 4. Current Scout/window assumptions to challenge

Latest project direction uses approximately:

- 300-second Scout windows
- 30-second overlap
- multimodal video + audio input
- local signals as navigation hints rather than truth
- human review before publish

Historical repository docs contain older/default window values (including 15-minute/900-second limits). Treat the latest project direction above as the intended experiment, and treat repository defaults as historical implementation details unless current code proves otherwise.

Do not assume a single model should both discover events and judge editorial value.

A central research question is whether the architecture should become hierarchical, e.g.:

```text
cheap coarse scan
  -> concrete event / scene proposals
  -> semantic context expansion
  -> fine-resolution candidate judge
  -> payoff/resolution verifier
  -> creator-value ranker
```

## 5. Critical real-world failure that motivated this audit

A recent real competitive/FPS test used a roughly 10-minute source split into 3 multimodal windows. The system returned 6 candidates.

After human review, **0/6 were judged useful as standalone short-form clips**.

Most important failure:

- Valorant player planted the Spike
- enemies were still alive (2 remaining)
- round had not resolved
- model summarized the moment as if it were a clutch / round win / celebration
- candidate boundary ended too early

This is not merely a wording bug. Audit it as a possible combination of:

- intermediate-event vs terminal-state confusion
- temporal reasoning failure
- hallucinated outcome
- premature payoff assumption
- weak game-state tracking
- insufficient look-ahead
- boundary derivation before resolution is known
- one-pass role conflation (proposal + interpretation + quality judgment + boundary)
- prompt/schema incentives that encourage complete-sounding stories
- window/context loss

The desired safety/editorial rule is closer to:

```text
Do not label a clutch/win until terminal evidence is observed.
Do not stop the story at an intermediate event when unresolved state remains.
```

Generalize this beyond Valorant, e.g. do not call a boss kill until defeat/transition/loot/other terminal evidence is visible; do not call an escape until danger actually resolves.

## 6. Commodity-vs-differentiation hypothesis to test

The project owner suspects concrete event capture is increasingly commodity:

- kill / death / headshot / multi-kill
- objective events
- audio spikes
- explicit game telemetry/event hooks when available

Existing products such as SteelSeries Moments, Medal, Outplayed, Razer and other game-event auto-clipping systems may already solve much of this cheaply.

The possible differentiated value is instead:

- creator-worthiness
- standalone story completeness
- setup -> payoff / resolution
- humor and social context
- personality value
- long-term/local context
- semantic boundary selection
- montage-vs-standalone judgment
- creator personalization
- reducing review burden without missing obvious moments

Challenge this hypothesis with current product/research evidence.

## 7. Editorial taxonomy currently under consideration

Candidate roles being considered:

- `STANDALONE_STORY`: understandable alone; enough setup/payoff/resolution to enter editing
- `MONTAGE_BEAT`: useful shot/beat (kill, aim play, short reaction, etc.) but not a complete standalone story
- `CONTEXT_ONLY`: setup, reaction, or connective material useful only when paired with another event
- `NONE`: real event/activity but not worth creator review time

Examples:

- one clean headshot -> likely `MONTAGE_BEAT`
- 1v3 -> all enemies eliminated -> round actually ends -> Discord/friend reaction -> likely `STANDALONE_STORY`
- Spike plant while enemies remain -> unresolved; must not be promoted to completed clutch/win

Evaluate whether this taxonomy matches real editing workflows or whether another taxonomy would be better.

## 8. Architecture options the audit must compare

Compare at least these approaches across quality, latency, cost, complexity, and maintainability:

1. local CV/audio heuristics
2. ASR + text reasoning
3. game telemetry / event logs / APIs
4. generic multimodal VLM/LLM over long windows
5. multimodal VLM/LLM only on proposed candidates
6. hybrid local + telemetry + multimodal pipeline
7. optional game-specific detector/plugin architecture

For multi-game support, investigate a design where the generic pipeline always works, while optional game adapters can contribute structured evidence when available, without requiring bespoke support for every game.

## 9. Specific architectural questions

The audit should answer:

- Should proposal generation and candidate judging be separate stages?
- Should a second-pass verifier inspect context before/after an event and require terminal evidence?
- Should candidate boundary be split into event localization -> context expansion -> story boundary verification?
- Should 300s+30s overlapping windows remain, shrink, become adaptive, or be replaced with hierarchical scanning?
- Should local/game-specific detectors propose concrete events and multimodal models judge story/editorial value only?
- How should state/resolution requirements be represented generically across games?
- How much value does ASR add for personality/social moments compared with raw audio/video inference?
- When should a full-window multimodal pass still be used to discover non-event moments that detectors miss?

## 10. Creator-facing success metrics

Do not optimize only for event precision/recall.

Evaluate and propose acceptance criteria around:

- source hours -> creator review minutes
- candidate KEEP rate
- obvious creator moments missed (`MISS_OBVIOUS`)
- standalone candidate precision
- montage usefulness
- boundary quality / missing setup / missing payoff / excessive padding
- time-to-publish
- model/provider cost per source hour
- local runtime per source hour
- owner edit distance after candidate selection
- selected -> published conversion
- post-publish performance, interpreted cautiously because distribution is noisy

A useful system should make creator work materially easier even before it achieves perfect automated editing.

## 11. MVP/product constraint

The owner is currently one creator without an established audience. Therefore the MVP should solve a real workflow problem, not prove an impressive technical architecture.

Prefer the smallest system that turns hours of VOD into a short, trustworthy review queue with enough context to make fast decisions.

AI rough editing should be evaluated separately. A likely decision point is whether rough editing should happen only after human KEEP/selection, rather than spending compute editing many candidates the creator will reject.

## 12. Repository reading order for the audit

Start with these documents:

1. `docs/00_START_HERE.md` — architecture/history and milestone summary
2. `docs/01_PRODUCT_REQUIREMENTS.md` — product scope and intended success criteria
3. `docs/02_ARCHITECTURE.md` — component boundaries, provider abstraction, privacy/cost design
4. `docs/03_PIPELINE.md` — stage semantics, resume/cache/failure behavior
5. `docs/04_DATA_MODELS.md` — Scout/candidate/session contracts
6. `docs/05_COST_STRATEGY.md` — cost ledger and hard-budget design
7. `docs/06_IMPLEMENTATION_PLAN.md` — milestone history and intended experiments
8. `docs/07_M8_BENCHMARK_PROTOCOL.md` — evaluation boundary and benchmark assumptions
9. `README.md` and `RESULT.md`
10. `status.json` — historical publication/checkpoint status only; do not assume it reflects the latest local work

Then inspect the current implementation for the real equivalents of:

- Scout prompt construction / response schema
- window planner and windowed Scout runner
- local signal generation and how signals are injected into model requests
- canonicalization / semantic validation
- reconciliation / dedupe
- clip-context / boundary derivation
- extraction
- ranking/report generation
- benchmark evaluator and annotation/review tooling
- provider cost reservation/settlement and retry semantics

Useful module areas described in repository architecture include:

- `src/game_highlight_finder/domain/`
- `src/game_highlight_finder/pipeline/`
- `src/game_highlight_finder/providers/`
- `src/game_highlight_finder/cost/`
- `src/game_highlight_finder/benchmark/`
- `tests/`

Do not trust documentation over code when they disagree. Record the discrepancy.

## 13. Version/staleness warning

At the time this context file was prepared, GitHub `main` contains published work through 2026-08-19, while the project continued locally afterward.

Newer project evidence reported after that published snapshot includes:

- later exhaustive calibration/ground-truth work
- provider-free regression work
- creator-review labels such as KEEP / MAYBE / rejection reasons / `MISS_OBVIOUS`
- real-media creator review experiments
- the 3-window competitive/FPS test described above with 6 candidates and 0 useful standalone clips
- the plant -> assumed win/clutch failure

Therefore:

- use GitHub `main` to understand architecture and code lineage
- if a newer local branch/worktree is available, inspect it and prefer it for implementation facts
- treat the real human review failure above as newer product evidence than the older published benchmark docs

## 14. Expected final audit output

The research report should be decision-oriented and include:

1. executive verdict: viable or not, and in what form
2. competitive landscape with product comparison
3. relevant academic/technical research
4. what is already solved/commodity vs still hard
5. root-cause analysis of plant -> win
6. recommended architecture diagram
7. recommended candidate taxonomy
8. MVP and beta definition
9. metrics and beta acceptance criteria
10. cost/latency implications
11. risks / reasons the project may fail
12. concrete 30-day development plan
13. explicit `KEEP / REMOVE / CHANGE / ADD`
14. final recommendation: continue / pivot / simplify / stop

For product claims, distinguish official marketing/docs from independent evidence. For research, prefer primary sources (paper/arXiv/conference/project page). Cite important claims and note evidence limits.

## 15. First-principles evaluation rule

The correct question is not:

> How do we improve the current Scout?

The correct question is:

> If a single gaming creator with 1–4+ hour VODs wanted the most useful, trustworthy, maintainable and affordable highlight assistant in 2026, what system would we build from scratch — and how far is this repository from that design?
