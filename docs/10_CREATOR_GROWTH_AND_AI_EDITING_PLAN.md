# Creator Growth + AI Editing Plan

Updated: 2026-09-06 Asia/Bangkok

## 1. North Star

The project is not an FPS kill detector and not a generic gameplay event logger.

The product exists to help the owner build an audience from zero by turning long, multi-game gameplay sessions into a small stream of short-form content opportunities for TikTok first, with the longer-term goal of growing an audience that can later follow the creator into livestreaming.

North Star outcome:

> Play normally -> the system finds moments that are worth turning into short-form content -> the owner receives a ranked creator-candidate pack -> AI can prepare editable draft versions -> the owner makes the final editorial decision and publishes -> real audience feedback improves future ranking.

The creator, personality, reaction, humor, surprise, tension, failure, social interaction, and story payoff matter as much as or more than pure mechanical skill. A kill is only one possible highlight type.

## 2. Multi-game principle

Production behavior must be game-agnostic by default. Game-specific profiles may improve detection later, but they must not define the product.

The Scout/Reviewer must be able to consider at least these creator-relevant moment families across different games:

- funny / absurd / unexpected moments;
- failure, panic, mistakes, and self-own moments;
- genuine reactions and emotional spikes;
- friend/co-op banter and social interaction;
- tension -> payoff stories;
- skill, clutch, boss wins, escapes, discoveries, or satisfying execution;
- unusual emergent gameplay or "what just happened" moments;
- short self-contained stories that need setup before the payoff;
- moments whose value comes primarily from creator personality rather than game importance.

A benchmark that proves FPS kill recall is useful engineering evidence, but it must not become the definition of creator quality.

## 3. Product role: candidate finder first

The first usable product is a "VOD watcher for the creator", not a finished TikTok generator.

Expected journey:

1. Owner records or streams gameplay normally.
2. `Game Highlight Finder` analyzes a 1-4+ hour source locally-first.
3. Scout finds creator-relevant candidate moments from the full timeline.
4. Reconcile/dedupe preserves setup -> event -> payoff and avoids duplicate fragments.
5. High-quality candidate clips are extracted from the original source.
6. Ranking/Reviewer produces a creator shortlist with reasons such as why a moment may work as short-form content.
7. Owner reviews only the shortlist instead of rewatching the whole VOD.
8. Selected candidates move to an editing stage.
9. Owner approves the final post.

The system must never require every candidate to be a standalone finished TikTok. Its job is to surface good raw material with enough context for editing.

## 4. AI editing decision

AI editing belongs in the roadmap, but initially as an **AI rough editor / draft generator**, not as an autonomous final editor.

Why:

- Early in a new channel there is no proven house style yet.
- Humor and creator personality are easy to damage with over-aggressive automatic cuts.
- The best hook can differ from the chronologically first event.
- Captions, punch-ins, silence removal, pacing, and 9:16 reframing are automatable without surrendering final editorial control.
- Keeping a human approval step lets the channel discover its identity while still saving most of the tedious work.

### Phase A - Candidate pack (current priority)

Output per source:

- extracted candidate clips;
- ranked shortlist;
- category / reason / confidence;
- setup, core event, and payoff timing;
- optional "why this may work as short-form" note;
- no destructive auto-editing.

### Phase B - AI rough-edit drafts

For an owner-selected candidate, generate one or more editable drafts:

- tighten true dead air while preserving setup/payoff;
- choose a suggested hook/opening beat;
- create a vertical 9:16 version with safe game-aware reframing;
- add draft subtitles/captions;
- optionally add punch-ins/zooms only when justified;
- normalize loudness and optionally duck background audio under speech;
- suggest title/on-screen hook/caption text;
- preserve the untouched extracted candidate beside every draft.

`silence != dead air` and `no speech != boring` are hard editorial principles. A quiet section can still be the best part of a clip when the visible gameplay carries skill, danger, discovery, suspense, comedy, anticipation, or necessary setup. Dialogue/laughter is evidence, not an inclusion gate. The rough editor may remove a quiet interval only when visual, audio, and story context together indicate that it adds no useful information or pacing value. When uncertain, keep the interval rather than destroy setup or payoff.

The rough editor should re-inspect the selected candidate at finer temporal detail than the long-VOD Scout and produce an explicit non-destructive edit plan (for example KEEP/TRIM segments, hook suggestion, protected setup/payoff/reaction ranges) before deterministic transforms are applied. The first implementation should prefer deterministic/local FFmpeg transforms plus model-generated edit decisions rather than fully generative video rewriting.

### Conservative editing policy

The first AI editor must optimize for **"clean and hard to ruin" before "flashy"**.

Rules:

- Never modify or overwrite the untouched extracted candidate.
- Prefer trimming dead air over aggressive jump-cutting.
- Preserve setup -> event -> payoff unless a draft explicitly offers an alternate hook structure.
- Keep reaction/laughter/payoff tail when it contributes to the moment.
- Avoid automatic effects, zooms, memes, sound effects, or transitions unless the edit decision has a concrete reason.
- Treat 9:16 reframing as a safe-crop problem: keep the gameplay subject, important HUD/evidence, and creator/reaction region visible where applicable.
- If edit confidence is low, create multiple draft variants instead of pretending there is one correct cut.
- Drafts should remain reversible and traceable to source timestamps.
- Human approval is required before publish-ready status.

Initial draft styles should be intentionally small in scope, for example:

1. **Clean** — minimal cuts, safe reframing, captions, audio cleanup.
2. **Balanced** — tighter pacing plus restrained punch-ins where justified.
3. **Story/Hook** — optional alternate opening beat while preserving enough context to understand the payoff.

The goal of C2 is not to replace an experienced editor. It is to reduce the owner's editing burden from a full manual edit to a short review/touch-up pass while preserving creator personality and timing.

### Phase C - Creator feedback loop

After posts exist, store owner feedback and publish outcomes separately from Scout truth:

- selected / rejected candidate;
- draft chosen;
- final posted duration;
- views and watch behavior when available;
- completion/retention proxies;
- shares, saves, comments, likes;
- follows attributable to a post when available;
- game, content family, hook/edit style.

These signals can later train or calibrate a **creator-specific ranking layer**. They must not be confused with event-detection ground truth.

### Phase D - Higher automation only after evidence

Only after enough posted examples exist should the project consider:

- automatic selection of the best draft;
- learned creator-specific editing templates;
- automatic caption/hook style selection;
- batch export ready for upload;
- optional publishing integration.

Automatic publishing should remain later than automatic draft generation.

## 5. Creator beta milestone

The next product milestone is **Creator Candidate Pack Beta**.

A beta is useful when the owner can give the system a real long VOD from different game styles and receive a small enough pack that reviewing the pack is clearly easier than scrubbing the original.

Beta acceptance should focus on:

- multi-game usefulness, not one-game benchmark perfection;
- MUST_NOT_MISS creator moments where practical;
- creator-worthy candidate precision / low boring burden;
- preservation of setup and payoff;
- source-to-candidate boundary quality good enough for a human/AI editing pass;
- review-duration reduction;
- cost per source hour;
- reliable end-to-end extraction and report generation.

Before calling creator beta broadly usable, validate on multiple gameplay archetypes rather than only competitive FPS. Prefer at least:

1. a fast competitive/action game;
2. a survival/sandbox/exploration game;
3. a social/co-op or personality-heavy session.

These are archetypes, not permanent game restrictions.

## 6. Content flywheel for a new channel

The project should support this operating loop:

```text
play/record
  -> candidate pack
  -> pick best moments
  -> AI rough-edit drafts
  -> human final edit/approval
  -> publish TikTok
  -> record what actually worked
  -> improve creator ranking/editing
  -> repeat
```

The early goal is not to predict virality perfectly. It is to lower the cost of producing enough good experiments that the channel can discover what viewers respond to.

## 7. Metrics that matter

Engineering metrics remain useful, but creator metrics become first-class:

- source hours -> candidate review minutes;
- creator-worthy candidates / all presented candidates;
- known important moments missed;
- candidate -> selected-for-edit conversion;
- selected -> published conversion;
- time from finished gameplay session -> publish-ready draft;
- AI draft -> owner acceptance/edit distance;
- post performance by content family and edit style;
- follows generated per published clip when measurable.

`short_form_score` remains an editorial prior, not a virality prediction.

## 8. What we deliberately do not optimize yet

- perfect automatic viral prediction;
- one specific game or HUD;
- automatic publication;
- replacing the creator's editorial taste;
- fully generative video effects;
- benchmark perfection that blocks shipping the candidate-pack beta.

Benchmarks remain regression tools. They should guide product quality, not become the product.
