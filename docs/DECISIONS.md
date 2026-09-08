# VDO Durable Decisions

This file contains decisions that should survive chat rollover. Add new entries; do not erase history merely because a new conversation starts.

## 2026-09-04 — Chat is not the source of truth

The project repeatedly hit ChatGPT maximum conversation length during the eight-clip review task. From now on, long-running VDO work must be resumable without the original chat transcript.

Decision:

- Repository checkpoints are the operational source of truth.
- `docs/CURRENT_STATE.md` is the short human-readable state.
- `docs/NEXT_ACTION.md` contains the single next objective and stop conditions.
- `.agent/handoff.json` is the machine-readable handoff.
- This file stores durable decisions/rationale.
- Historical milestone/status documents remain evidence but are not automatically current.

## 2026-09-04 — Proactive conversation rollover

Do not wait for a conversation to hit maximum length. After a meaningful milestone, before a risky/long tool sequence, and before deliberately switching chats, refresh the checkpoint files.

A fresh conversation in the ChatGPT `vdo` project should be able to continue from the short command `ต่อ VDO`. The assistant should read the checkpoint and inspect current git/task state before asking the user to repeat old context.

## 2026-09-04 — Keep tool transcript small

Long-running commands should execute as durable tasks/processes and write results to repository/local artifacts. The chat should receive bounded status/result summaries rather than continuous low-value logs. Preserve task IDs and output paths in the checkpoint when a task remains active across a turn or conversation.

## 2026-09-04 — Semantic video review requires pixels

Motion/audio/activity signals are useful proposal hints, not semantic adjudication. A clip cannot be reliably labelled as a kill, clutch, funny moment, setup, payoff, or boring merely from signal intensity.

For candidate/ground-truth review, use actual visual content from the MP4/dense temporal review artifacts. Audio can support the judgment but does not replace visible semantic evidence.

## 2026-09-04 — No montage assumption

The current pipeline primarily reconciles/deduplicates proposals that overlap or represent the same event, then derives clip context around that candidate. It should not be assumed to combine unrelated moments far apart in the source into a TikTok montage. Therefore a self-contained candidate with no real setup/event/payoff should not be kept merely on the theory that unrelated later clips will rescue it in editing.

## 2026-09-04 — Preserve unrelated dirty work

The canonical and media worktrees may contain active uncommitted/private benchmark work. Never use blanket clean/reset/stash/drop operations as part of chat handoff maintenance. Stage/commit only explicitly reviewed files when it is safe to do so.

## 2026-09-04 — Eight-candidate visual adjudication is evidence, not ground truth

The eight previously unlabelled candidates were reviewed from actual pixels and produced 1 POSITIVE / 7 BORING / 0 UNCERTAIN. This is useful evidence that the current shortlist can contain substantial semantic false-positive burden, but the queue was explicitly not ground truth and was not an exhaustive full-video sample.

Decision: preserve this result as candidate-quality evidence only. Do not convert these eight labels into benchmark truth by themselves.

## 2026-09-04 — Repair calibration ground truth before tuning

The old calibration annotation provenance was not based on a complete full-video human review. During direct visual review a genuine cal-01 kill around 439–459 s was found outside the old annotation set.

Decision:

- Freeze Scout/ranking/threshold tuning until both calibration videos receive exhaustive full-timeline visual review.
- Create a new private GT revision rather than overwriting the old annotations.
- An `exhaustive` claim requires whole-timeline coverage, not review of model proposals only.
- Re-evaluate existing experiment outputs offline against the new revision before paying for new inference.
- Only after re-scoring should errors be attributed to detection, reconcile/boundary, or ranking/filtering.

## 2026-09-04 — Separate event truth from clip-boundary preference

A benchmark highlight should preserve the semantic event and enough setup/payoff context, but event existence and ideal short-form clip boundaries are related rather than identical questions.

Decision: during GT v2 audit, record setup/event/payoff-aware intervals and evidence. Do not stretch event truth merely to make a historical candidate count as a match, and do not tune evaluator thresholds after seeing one experiment's output.

## 2026-09-06 — North Star is multi-game creator growth

The project exists to help the owner build an audience from zero with short-form gaming content and eventually convert that audience into livestream viewers. It is not an FPS kill detector and must not optimize itself around one game merely because that game provides easy benchmark labels.

Decision:

- Production highlight quality is creator-oriented and multi-game.
- Funny, fail, reaction, friend/social, unexpected, tension/payoff, discovery, personality, skill, and clutch moments are all first-class.
- FPS kill benchmarks remain engineering diagnostics, not the product definition.
- The next product milestone is a usable Creator Candidate Pack Beta across multiple gameplay archetypes.
- Benchmarks are regression tools and must not indefinitely block shipping a useful candidate pack.

## 2026-09-06 — AI editing is a rough-editor layer, not the first gate

AI editing is desirable, but it should initially operate on owner-selected candidate clips and produce non-destructive draft variants rather than autonomously deciding and publishing final videos.

Decision:

- Candidate discovery comes first; AI rough editing is the next creator-facing layer.
- Preserve the untouched extracted candidate beside every generated draft.
- Prioritize dead-air tightening, hook suggestion, 9:16 reframing, caption drafts, audio cleanup, and restrained punch-ins before fully generative video editing.
- `silence != dead air` and `no speech != boring`: visible gameplay can carry the entire value of a moment, including skill, tension, discovery, comedy, danger, anticipation, or setup.
- Dialogue, laughter, shouting, and audio activity are evidence, not mandatory gates for inclusion or retention.
- The editor must inspect visual + audio + story context before trimming a quiet interval; uncertainty defaults to keeping context.
- The editor should produce an explicit non-destructive edit plan before deterministic FFmpeg transforms.
- Keep human final approval while the new channel discovers its own style.
- After real posts exist, use publish-performance feedback to improve creator-specific ranking/editing without contaminating event-detection ground truth.
- Automatic publishing remains later than automatic draft generation.

## 2026-09-06 — First AI editor must be conservative and reversible

The first editing layer should optimize for predictable, non-destructive assistance rather than flashy autonomous editing.

Decision:

- Default style is clean/minimal: trim dead air, safe 9:16 reframe, draft captions, loudness cleanup.
- Preserve setup -> event -> payoff and keep reaction/payoff tails when they matter.
- Effects, zooms, transitions, memes, or sound effects require an explicit reason; do not sprinkle them automatically.
- If edit confidence is low, generate multiple draft variants instead of forcing a single answer.
- Every draft remains traceable to source timestamps and the untouched candidate is always preserved.
- Human approval remains required before publish-ready output.
- Success for C2 means reducing manual editing time substantially without making timing/personality feel artificial.

## 2026-09-08 — Do not compete with event-trigger recorders; separate creator stories from montage beats

The first live C1-A creator pass produced technically valid extracted clips, but the owner judged **0/6 as usable standalone creator clips**. One candidate treated a Spike plant as if the round payoff had already happened even though the round was still unresolved, while several ordinary gunfight moments were potentially useful only as ingredients in a montage/highlight reel rather than as self-contained posts.

Decision:

- Do not define product value as "find kills/shots". SteelSeries/Razer-style event capture already handles many obvious FPS triggers cheaply and reliably; those events may be used as signals or candidate seeds rather than the product's final judgment.
- Add an explicit editorial role separate from event category: at minimum `STANDALONE_STORY`, `MONTAGE_BEAT`, and `NONE` (a `CONTEXT_ONLY` role may be added if implementation benefits from it).
- A valid gameplay event is not automatically a creator-worthy standalone clip. A kill/headshot/multi-kill may be useful as `MONTAGE_BEAT` even when it lacks a self-contained setup/payoff arc.
- A `STANDALONE_STORY` must preserve enough temporal context to establish setup -> event -> real resolution/payoff/reaction. Do not declare a win/clutch/payoff before the round/encounter outcome is actually visible or otherwise evidenced.
- Prefer a hybrid hierarchy: cheap/local/game-event signals propose moments; multimodal AI judges story/social/personality value, resolves context, groups related beats, and decides standalone-vs-montage role.
- Do not fund B/C live inference until this contract is implemented and tested provider-free. The A result is a product-quality failure that should change the contract, not be hidden by running more paid sources.
- Preserve A's 6 clips and owner review as diagnostic evidence; do not convert the owner preference labels into GT-v2 event truth.

## 2026-09-09 — Deep Research pivot: proposal -> verify -> story -> rank

External Deep Research and a targeted repository audit concluded that the product direction remains viable, but the monolithic long-window Scout should not remain the semantic source of truth. Current long-video multimodal models are useful for contextual interpretation but are not reliable enough to own dense event capture, terminal outcome verification, exact temporal truth and creator ranking in one call.

Decision:

- Continue the project; preserve the North Star and existing engineering substrate.
- Keep source identity, local analysis proxies, original-source extraction, canonical artifacts, reconcile/dedupe, cost ledger/provider safety and creator report.
- Retire the one-Scout-owns-everything semantic architecture as the long-term design.
- Make `docs/16_C1_HYBRID_CREATOR_TRIAGE_PLAN.md` the canonical next implementation plan.
- Build a provider-neutral factual proposal layer first. Proposals are anchors/evidence and must not contain creator/editorial scores.
- Add explicit story state, resolution state and evidence-backed claim status. A standalone story is not creator-review eligible until its story is complete and its required resolution is verified (or explicitly not applicable).
- Make the unresolved Spike plant case a permanent semantic regression: objective plant may be detected, but `ROUND_WON` / `CLUTCH_WIN` cannot be verified without terminal evidence.
- Separate semantic judgment from factual resolution verification; separate story assembly from overlap dedupe; run boundary refinement after verification.
- Treat 300s/120s/90s windows as transport/coarse-context policy rather than semantic story units.
- Pause B/C paid inference while the hybrid contract is implemented provider-free. Do not spend on another monolithic prompt iteration merely to collect more examples of the known A failure.
- Defer C2 AI rough editing until C1 materially reduces creator review burden and produces useful verified candidates.
