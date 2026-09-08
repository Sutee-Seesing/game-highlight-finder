# C1 Hybrid Creator Triage + Story Verification Plan

Updated: 2026-09-09 Asia/Bangkok

## Why the plan changed

External Deep Research plus the first real creator review changed the semantic center of C1.

The product North Star is unchanged:

`long multi-game VOD -> reduce creator review burden -> surface useful raw material -> human chooses -> AI rough-edit later -> human final/publish`

The first live A pass proved that the engineering pipeline can complete, extract and report candidates, but it failed the product test: the owner judged **0/6 candidates usable as standalone creator clips**. The clearest failure treated a Valorant Spike plant as a clutch/win-style payoff while the round was still unresolved.

That failure should not be solved primarily by another prompt revision or by spending more on longer/shorter dense VLM windows. The new C1 architecture separates factual proposal generation, semantic interpretation, resolution verification, story assembly/boundaries and creator ranking.

No provider call is authorized by this plan.

## Product promise for C1

C1 succeeds when, after a 1–4+ hour gameplay session, the owner can review only a few minutes of genuinely promising material while retaining obvious creator moments and avoiding false completed-story claims.

C1 is not required to predict virality or autonomously publish.

## Architecture decision

Retire this as the long-term semantic architecture:

```text
300s window
  -> one multimodal Scout finds event
  -> interprets event/outcome
  -> chooses boundaries
  -> assigns creator score/editorial role
  -> rank
```

Keep 300s windows only as optional transport/coarse-context infrastructure.

Target architecture:

```text
RAW VOD
  -> local analysis proxy
  -> cheap evidence sources
       - generic visual/activity
       - audio
       - ASR/transcript when added
       - optional OCR
       - optional game plugin / telemetry
       - manual markers
  -> high-recall factual proposals
  -> cluster/dedupe/diversify proposals
  -> proposal-centered context retrieval
  -> multimodal semantic judge
  -> dynamic context expansion when unresolved
  -> factual resolution verifier
  -> story assembler + story-boundary verifier
  -> creator ranker
  -> exact clip extraction from RAW
  -> human KEEP / MAYBE / REJECT
  -> C2 rough editing only after KEEP
```

## Hard semantic invariants

1. A proposal is a factual anchor, not a story and not a creator score.
2. Event category and editorial role remain separate axes.
3. Keep the editorial roles:
   - `STANDALONE_STORY`
   - `MONTAGE_BEAT`
   - `CONTEXT_ONLY`
   - `NONE`
4. Add orthogonal story state:
   - `COMPLETE`
   - `INCOMPLETE`
   - `UNKNOWN`
5. Add orthogonal resolution state:
   - `VERIFIED`
   - `UNVERIFIED`
   - `CONTRADICTED`
   - `NOT_APPLICABLE`
6. Terminal semantic claims require explicit evidence. Provider self-confidence is never proof.
7. `STANDALONE_STORY` is eligible for the standalone shortlist only when:
   - story state is `COMPLETE`; and
   - resolution state is `VERIFIED` or `NOT_APPLICABLE`.
8. A real kill/headshot/ability play may still be a useful `MONTAGE_BEAT` without being a standalone story.
9. A Spike plant is allowed as an event. `ROUND_WON` / `CLUTCH_WIN` is forbidden until terminal evidence exists.
10. Boundary refinement happens only after the event/story premise is supported. A boundary refiner is not a factual verifier.
11. Expensive inference should scale with plausible proposal neighborhoods, not nearly all source duration by default.
12. Raw source remains local; only explicitly authorized analysis derivatives may cross provider boundaries.

## Stage contracts

### H1 — Truth model + semantic regression gate

Provider-free first milestone.

Add durable data contracts for:

- `StoryState`;
- `ResolutionState`;
- claim status (`VERIFIED`, `UNVERIFIED`, `CONTRADICTED`);
- timestamped claim evidence;
- creator-review eligibility that cannot promote an unresolved standalone story.

Add a permanent Spike regression fixture:

```text
objective planted
+ opponents still alive
+ no round-end evidence
=> ROUND_WON remains UNVERIFIED
=> cannot enter standalone shortlist
=> may remain MONTAGE_BEAT / CONTEXT_ONLY / unresolved
```

Preserve existing A artifacts as historical diagnostic evidence.

### H2 — Provider-neutral proposal layer

Create a factual proposal contract that can accept evidence from many sources without coupling the rest of the pipeline to one game or one detector.

Minimum proposal fields:

```text
proposal_id
source/session identity
anchor or interval
signal_type
event_hypothesis optional
confidence
provenance/source adapters
supporting metadata
```

Initial provider-free adapters may use existing local signals only as weak evidence. Do not claim the initial loudness-based adapter is a good creator detector. Its purpose is to establish the interface and deterministic artifact flow.

Planned adapters:

- generic local activity;
- ASR/transcript branch;
- optional OCR/visual state change;
- optional game-event/plugin integration;
- manual/creator marker input.

Metrics for this layer:

- MUST_CATCH proposal recall;
- proposals/source-hour;
- duplicate proposal burden;
- cost = local/provider-free where possible.

### H3 — Dynamic semantic context planner

A proposal does not carry a fixed story boundary.

Start with bounded context around the proposal. If the judge says `needs_more_context`, expand before/after in controlled steps until:

- resolution is verified;
- resolution is contradicted;
- story becomes clearly not useful;
- configured context budget is reached.

Do not solve cross-boundary context by permanently increasing fixed overlap.

### H4 — Semantic judge

The semantic judge receives proposal-centered media, not an undifferentiated full VOD by default.

Responsibilities:

1. describe what actually happened;
2. identify possible creator value;
3. propose event category;
4. propose editorial use;
5. emit explicit claims that require verification;
6. request more context when needed.

The judge does not self-certify terminal claims.

### H5 — Resolution verifier

Separate factual verification from creator judgment.

Input:

- candidate hypothesis;
- explicit claim(s);
- retrieved context;
- optional stronger evidence such as telemetry/OCR/game plugin.

Output per claim:

- `VERIFIED`;
- `UNVERIFIED`;
- `CONTRADICTED`;
- timestamped evidence and source.

Examples:

- round won -> round-end evidence;
- boss defeated -> boss death/defeated UI/state transition/loot sequence;
- escaped -> danger/pursuit actually terminates;
- joke payoff -> setup receives understandable culmination/reaction;
- discovery -> reveal/recognition actually occurs.

### H6 — Story assembler + semantic boundaries

Keep existing reconcile/dedupe specifically for overlapping fragments.

Add a separate story-assembly responsibility that can combine related beats:

```text
setup
+ event
+ resolution
+ reaction
= one story candidate
```

Represent semantic boundary roles separately where available:

```text
setup_start
event_start
event_end
resolution/payoff_end
reaction_end
```

Derived edits can then differ:

- standalone story;
- tighter short;
- montage beat;
- supporting context.

Existing fine boundary refinement remains useful after verification.

### H7 — Creator ranking

Do not treat Scout/provider self-score as the final creator rank.

Ranking inputs should eventually include:

- verified editorial role;
- creator preference/history;
- novelty/diversity;
- creator-value dimensions such as humor/skill/surprise/tension/personality/social/discovery;
- factual reliability;
- duplicate/redundancy penalty.

Human labels remain separate from detection truth:

- `KEEP`
- `MAYBE`
- rejection reason

### H8 — Real unseen validation

Only after H1–H7 have a coherent provider-free contract should paid inference resume.

Use unseen real footage across at least:

1. competitive/action;
2. sandbox/survival/exploration;
3. co-op/social/personality-heavy.

Compare the old flat Scout baseline with the hybrid pipeline using creator-facing outcomes.

## Creator-facing metrics

Primary product metric:

`creator time saved while preserving important moments`

Track:

### Retrieval efficiency

- review minutes / source hour;
- candidate count / source hour;
- duplicate review minutes.

### Creator utility

- standalone KEEP rate;
- standalone KEEP + MAYBE rate;
- montage usefulness;
- selected -> published conversion later.

### Semantic reliability

- MUST_CATCH recall;
- standalone-story precision;
- false terminal claim count;
- event-description correctness;
- story-completeness accuracy.

### Editing quality

- boundary correction seconds;
- owner trim/edit distance;
- time from VOD completion -> publishable draft later.

### Cost

- THB / source hour;
- THB / creator-accepted candidate;
- expensive VLM media minutes / source hour.

Any numeric beta thresholds are provisional experiment targets, not industry truths. Tune them only from real owner workflow evidence.

## Immediate implementation order

1. Freeze A/v21 evidence; do not spend on B/C.
2. Add H1 story/resolution/claim models and a mandatory unresolved-Spike regression gate.
3. Change ranking/report semantics so an unverified standalone cannot appear as a verified standalone recommendation.
4. Add provider-neutral proposal models and deterministic provider-free proposal generation from current signals as an interface proof.
5. Add tests proving proposals are factual anchors with no creator/editorial score.
6. Design/persist the creator-evaluation corpus schema for real annotations.
7. Add dynamic context planning.
8. Add semantic judge/verifier contracts with fake provider fixtures first.
9. Reuse existing boundary-refinement infrastructure only after verification.
10. Run focused + full local regression before considering any paid run.
11. Revisit A/B/C source selection and exact cost only after the hybrid contract is coherent.

## What stays frozen for now

- no B/C live inference;
- no new provider authorization inferred from normal chat continuation;
- no C2 rough editor implementation;
- no universal 90s/120s/300s semantic default;
- no broad per-game detector buildout;
- no GPU/media optimization mixed into semantic validation;
- no raw source upload;
- no automatic provider generation retry.

## 30-day learning gate

After an evidence-driven hybrid iteration, continue toward C2 only if the redesigned system shows directionally:

- strong review compression;
- materially better creator usefulness than A's 0/6 standalone baseline;
- useful montage ingredients;
- acceptable MUST_CATCH recall;
- zero contradicted terminal claims in evaluated shortlist;
- cost trajectory compatible with actual creator usage.

If creator usefulness remains poor, pivot further down to semantic VOD search/retrieval + fast manual candidate creation rather than buying more inference for autonomous highlight judgment.

## Provider-free implementation status — 2026-09-09

The first coherent hybrid contract now exists locally without provider inference:

- H1: explicit story/resolution/claim truth states and timestamped evidence gate;
- H2: provider-neutral factual `Proposal` / `ProposalArtifact` plus deterministic local activity adapter as an interface proof;
- H3: bounded proposal-centered context planning with explicit dynamic expansion;
- H4: provider-neutral semantic-judge contract plus sequential FakeSemanticJudge fixtures for re-judgment after context expansion;
- H5: independent CandidateVerification contract plus sequential FakeResolutionVerifier fixtures; verified/contradicted claims require timestamped evidence;
- H6: verified StoryAssembly with setup/event/payoff/reaction semantic boundaries; story assembly is refused before COMPLETE + VERIFIED/NOT_APPLICABLE state;
- orchestration: provider-free `run_provider_free_hybrid_triage` now executes proposal -> context -> judge -> expansion -> verifier -> expansion -> story assembly -> creator-review gate -> clip-boundary derivation;
- persistence: hybrid run history, full semantic map and creator-review map are written under `session/hybrid/`;
- creator evaluation: a separate owner-review corpus records KEEP/MAYBE/rejection reasons, owner role corrections, desired boundary corrections, review time and `MISS_OBVIOUS` without mutating event GT.

This is a contract/orchestration milestone, **not yet evidence that real-VOD creator quality is solved**. The local activity proposal adapter is intentionally weak, semantic/verifier implementations are still fake/provider-neutral fixtures, and story assemblies are still supplied explicitly in tests rather than inferred from real media.

Provider-free follow-through after H6:

- local CLI/session integration now exists through `hybrid proposals`, `hybrid run-fixture`, `hybrid review-template`, and `hybrid review-summary`; the historical flat-Scout path is still preserved separately;
- creator-review worksheets and summaries now round-trip deterministically into the separate creator-evaluation corpus;
- H2.1 adds source-bound `ManualProposalMarkerSet` fixtures plus deterministic proposal clustering: nearby compatible factual anchors can share one semantic neighborhood, while conflicting explicit event hypotheses are never merged;
- manual markers are bound to exact source SHA-256 + duration, carry no creator/editorial score, and can be supplied to both `hybrid proposals` and the provider-free `hybrid run-fixture` path;
- clustering preserves source provenance and records the contributing signal types rather than promoting loudness/activity to semantic truth.

Next learning steps before any paid run:

1. exercise the enriched proposal/owner-review workflow on a small local media fixture and then on one carefully chosen real source without provider inference;
2. add proposal-density/diversity metrics and one additional provider-free evidence adapter only if it materially improves proposal recall (ASR/transcript fixture is the preferred next candidate; avoid broad game-specific detector sprawl);
3. design the real semantic-judge and independent resolution-verifier provider contracts, privacy boundary, cache identity, and exact cost preflight separately from the old full-window Scout contract;
4. add a provider-free replay harness for the historical Spike failure so future semantic/verifier adapters must keep `ROUND_WON` unverified until terminal evidence exists;
5. only then select unseen real media and request a fresh explicit hard THB authorization if paid inference still has enough learning value.

## Current authorization

`NO_NEW_PROVIDER_CALL_AUTHORIZED`
