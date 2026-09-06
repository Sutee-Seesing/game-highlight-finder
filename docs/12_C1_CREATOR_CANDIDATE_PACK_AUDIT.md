# C1 Creator Candidate Pack Audit

Updated: 2026-09-07 Asia/Bangkok

## Goal

Convert the current highlight pipeline from an engineering-oriented gameplay-event finder into the first usable **multi-game Creator Candidate Pack Beta** without discarding the reliable ingest/proxy/window/reconcile/extract/report foundation.

This audit is provider-free. It inspects current source behavior and identifies the smallest creator-facing implementation slice.

## What is already reusable

The pipeline foundation is largely compatible with the creator product:

- long source -> local analysis proxy/signals;
- windowed Scout;
- overlap reconciliation/deduplication;
- setup/event/payoff-aware clip derivation;
- source-quality candidate extraction;
- deterministic ranking artifact;
- self-contained HTML report;
- cost ledger / provider safety gates.

The generic taxonomy is also broader than FPS already. Existing first-class categories include:

- FUNNY
- FAIL
- CLUTCH
- REACTION
- SMART_PLAY
- FRIEND_MOMENT
- WTF_UNEXPECTED
- TENSION_PAYOFF
- SKILL
- OTHER

Game-specific categories must remain optional additions, not the definition of product quality.

## Creator-product gaps found in current source

### 1. The v19 window prompt still prioritizes gameplay anchors over creator moments

Current `windowed_scout.py` instructs the model to cover concrete gameplay anchors first and only **after** that add social/funny/reaction candidates. It further says social/audio moments must never replace or crowd out visible gameplay anchors.

That was useful for the FPS recall experiment, but it is wrong as a permanent creator policy. A funny conversation, panic reaction, friend interaction, or personality moment can be more valuable for TikTok than a mechanically important game event.

Required change:

- creator-worthiness families are parallel first-class targets;
- gameplay significance must not outrank personality by definition;
- local audio/activity signals remain navigation hints only;
- however, an intelligible self-contained social/audio moment may itself be a valid creator candidate when the actual media supports it.

### 2. "Generic banter/laughter needs a visual gameplay payoff" is too restrictive

Current prompt rejects generic laughter/banter/hiding/searching unless there is a distinct self-contained payoff. The anti-false-positive intent is good, but requiring a gameplay-style visual payoff can suppress personality clips.

Required change:

A social/personality candidate needs a **self-contained audience payoff**, not necessarily a gameplay payoff. Examples include a complete joke, surprising line + reaction, escalating friend exchange, panic/recovery, or a short understandable story.

### 3. Silence must not be treated as boredom

No speech, laughter, or loud audio does not imply that a moment is weak. Visual gameplay can carry the full creator value through skill, tension, danger, discovery, anticipation, absurdity, or necessary setup.

Required change:

- `silence != dead air` and `no speech != boring` are explicit Scout and editor principles;
- audio/activity signals remain hints only and must not gate inclusion;
- quiet visual moments must be judged from actual gameplay/story content;
- later C2 trimming may remove a quiet interval only when visual + audio + story context together show that it adds no useful information or pacing value;
- when uncertain, preserve the interval rather than destroy setup/payoff.

### 4. Ranking names two concepts but does not actually model them independently

`ranking.py` exposes:

- `short_form_score = candidate.score`
- `detection_confidence = candidate.confidence`

This separation is conceptually correct and should be preserved. But `candidate.score` is still the only editorial value, and the report does not explain the creator judgment separately from event evidence.

For C1, do not add a complex learned ranker yet. Keep deterministic ordering by creator/short-form score, then confidence, but make the semantics explicit in the candidate/report contract.

### 5. The candidate report is an engineering report, not yet a creator pack

The current report card shows category, score, confidence, event/clip bounds, one `reason`, evidence, normalization actions, window lineage, and an Open Clip link.

For Creator Candidate Pack Beta the card must answer, at a glance:

1. **What happened?**
2. **Why might this work as a short-form clip?**
3. **How sure is the system?**
4. **What source interval / extracted clip should I review?**

Window IDs and normalization lineage can remain available as secondary/debug detail rather than dominate the creator view.

### 6. Taxonomy needs at least one missing multi-game family

`DISCOVERY` is missing even though exploration/sandbox/survival games are a first-class validation archetype.

Add `DISCOVERY` as a generic category. Do not proliferate game-specific categories for every title.

Personality itself does not require a separate category in C1; FUNNY/REACTION/FRIEND_MOMENT/WTF_UNEXPECTED/TENSION_PAYOFF/OTHER can describe the moment while creator-worthiness is captured independently.

### 7. 90-second windows must not become a universal product assumption

v21 showed that 90-second windows improved detection for two fast FPS events. That is useful engineering evidence, not proof that 90 seconds is optimal for every game or creator story.

Decision for C1:

- do not globally optimize the multi-game product around the FPS v21 result;
- preserve window duration as configuration;
- validate at least one fast-action session and slower sandbox/social sessions before selecting a creator-beta default;
- dynamic/profile-aware windowing can be considered later if a single default proves inadequate.

## C1 implementation slice

Implement the smallest backward-compatible creator contract:

1. Add generic `DISCOVERY` category.
2. Extend Scout candidate output with two explicit creator-facing strings:
   - `moment_summary`: concise description of what actually happened;
   - `creator_reason`: why the moment may be worth reviewing for short-form.
3. Keep existing `score` semantics as editorial/short-form potential (0-10) and `confidence` as detection/timing certainty (0-1). Do not conflate them.
4. Make new provider fields backward-compatible at the canonical parser boundary so historical artifacts remain readable.
5. Revise the window Scout prompt from "gameplay anchors first" to balanced creator discovery across gameplay + personality/social/reaction/story families, with explicit `silence != dead air` and `no speech != boring` rules.
6. Preserve setup -> event -> payoff and avoid fragmenting a single creator story.
7. Update deterministic ranking labels to call the value creator/short-form score explicitly; no learned ranking yet.
8. Redesign report cards into a Creator Candidate Pack view while retaining technical details in a secondary section.
9. Add offline/fake-provider tests for multi-game candidate semantics, report output, schema compatibility, and ranking determinism.

## C1 validation before new paid inference

Provider-free first:

- schema/parser compatibility tests;
- fake Scout fixtures for action, sandbox discovery, funny/co-op, reaction, fail, and boring cases;
- reconcile/boundary tests for story clips;
- report snapshot/content assertions;
- full local test suite.

Only after these pass should a real multi-game media validation run be proposed. Any paid provider run still requires a fresh explicit authorization and cap.

## C1 exit condition

C1 implementation is ready for real media testing when the owner can receive a report whose top candidates read like editorial choices rather than detector logs, and every item contains a playable extracted source clip plus a clear "what happened" and "why review this" explanation.

This does not require AI rough editing yet. C2 begins immediately after C1 proves usable on real multi-game footage.
