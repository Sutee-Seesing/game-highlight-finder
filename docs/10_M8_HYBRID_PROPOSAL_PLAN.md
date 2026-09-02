# M8 Plan Amendment — Hybrid Local Proposer + Gemini Judge

Date: 2026-09-01

Status: **ACTIVE EXPERIMENT / V1 DEFAULTS NOT LOCKED**

This document is an additive plan amendment. It does **not** delete or rewrite the historical plan and evidence in:

- `docs/06_IMPLEMENTATION_PLAN.md`
- `docs/07_M8_BENCHMARK_PROTOCOL.md`
- `docs/08_M8_V13_STATUS.md`
- `docs/09_M8_V19_PROVIDER_BOUNDARY.md`

Those files remain the history of why M8 reached this point. This amendment records the decision to stop treating prompt-only Scout tuning as the only remediation path and to evaluate a hybrid architecture.

## 1. Why this amendment exists

The long-session pipeline, cost lifecycle, extraction, ranking, report, resume, and provider safety mechanisms are already mature. The unresolved V1 blocker is semantic highlight quality: v13 failed the quality gate, and provider-free follow-up work showed that neither one absolute audio threshold nor a simple motion threshold cleanly separates worthwhile gameplay from boring traversal across sources.

Experimental `gemini-scout-window-v19` is regression-green but has no live quality evidence yet. Rather than entering an open-ended v20/v21 prompt-tuning loop, the project will test a different division of responsibility:

1. **Local proposer:** cheaply reduce the search space with high-recall, non-semantic signals.
2. **Game-profile detectors:** add stronger game-specific evidence when available, such as HUD/kill-feed/round-state cues.
3. **Gemini judge:** inspect only bounded proposal clips and decide whether a real, self-contained highlight exists, with semantic event bounds and editorial score.
4. **Source extraction:** cut the final clip from the original source using the judged event bounds plus bounded context.

The local stage is explicitly a **proposal generator**, not a highlight classifier. Audio, motion, OCR, and HUD cues must never be persisted as semantic ground truth.

## 2. Core architecture

```text
RAW gameplay (source of truth)
        |
        v
analysis proxy + local signals
        |
        v
HIGH-RECALL LOCAL PROPOSER
  - audio prominence
  - visual motion / scene activity
  - optional game-profile HUD/OCR cues
  - optional structured game events where legitimately available
        |
        v
anchor timestamps
        |
        v
event-centered overlapping proposal windows
  - bounded pre/post context
  - merge nearby/overlapping proposals
  - optional round-boundary expansion hints
  - never fixed-chunk semantics
        |
        v
Gemini semantic judge on proposal clips only
  - highlight / boring / uncertain
  - event start/end inside proposal
  - category, score, confidence, visible evidence
        |
        v
reconcile / dedupe / rank
        |
        v
accurate cut from ORIGINAL source
  - event bounds + bounded setup/reaction context
        |
        v
review report -> user chooses final clips
```

## 3. Critical boundary rule: never cut by arbitrary fixed chunk

The hybrid design must not mean “split a 40-minute Valorant match into four 10-minute pieces and keep one.” That would create exactly the failure mode where a round or fight is cut in half at an arbitrary boundary.

Instead:

- Local signals produce **anchor timestamps**, not final cuts.
- Each anchor becomes an **event-centered proposal window** with context on both sides.
- Nearby proposals may overlap and are merged before semantic judging.
- A proposal boundary is only a transport/context boundary for the judge; it is **not** the final clip boundary.
- Gemini returns event-relative start/end after watching the proposal.
- Final extraction always returns to the original source and adds bounded setup/reaction context.

Example:

```text
18:27 ----------- 18:42 anchor ----------- 18:57
                 18:51 anchor -------------------- 19:06

merge => 18:27 ----------------------------------- 19:06

Gemini judges actual event => 18:43.2 - 18:55.8
final source clip          => e.g. 18:38 - 19:01
```

The system may therefore span the full useful part of Valorant Round 10 even if an earlier analysis window happened to end during that round.

## 4. Proposal policy

### 4.1 Generic wide-net signals

Initial generic proposer inputs:

- source-normalized audio prominence from the existing runtime local-signal domain;
- low-cost visual motion/scene evidence sampled from the analysis proxy;
- deterministic ranking and NMS/merge of anchors;
- bounded proposal count and total proposal duration.

These signals are navigation evidence only. They must carry provenance and `semantic_labels_inferred=false`.

### 4.2 Game-profile evidence

Add a versioned profile seam rather than hard-coding one game globally. A Valorant profile may later contribute:

- kill-feed / skull / elimination-state ROI changes;
- round score changes;
- `BUY PHASE`, victory/defeat, spike plant/defuse, or other stable HUD cues;
- OCR only over tightly defined ROIs rather than full-frame OCR;
- cue clustering so rapid consecutive kills become one story rather than many tiny clips.

A game profile is a **proposal enhancer**. If it fails because the HUD changes, the generic proposer must still work.

### 4.3 Optional structured-event adapters

Some games expose legitimate game-state integrations, demo files, logs, Overwolf events, or similar structured data. Where available, add these as optional high-confidence proposal sources. Do not make V1 depend on telemetry that Valorant or another game does not reliably expose.

## 5. Round / match boundary hints

Round detection is useful but should not be a hard dependency.

Boundary hints may be obtained from stable visible state changes such as round timer resets, score changes, buy-phase banners, objective-state transitions, victory/defeat overlays, or other game-profile cues.

Rules:

- A boundary hint may **expand** a proposal to include useful round setup/outcome when within the configured hard duration cap.
- It must not truncate a visible engagement just to fit a detected round boundary.
- One highlight is not required to equal one round.
- Reactions or aftermath immediately after a round may remain attached when semantically useful.
- If round detection is uncertain, retain the event-centered overlapping proposal instead of guessing.

## 6. Recall guardrails

The local proposer is allowed to be noisy; the Gemini judge exists to reject noise. Missing a great moment before Gemini sees it is more damaging than forwarding an extra boring proposal.

Guardrails:

- tune proposer selection for **high calibration recall**, not precision;
- combine independent proposal sources rather than relying on one threshold;
- preserve overlap/context around anchors;
- merge clusters instead of shrinking them prematurely;
- optionally reserve a small **coverage/sentinel budget** for scene changes or low-confidence regions so quiet but visually important moments are not systematically invisible;
- if a game profile is unavailable, fall back to generic audio + motion rather than failing closed on semantic capture.

The sentinel budget is a recall backstop, not random large-scale Gemini scanning. Its size must remain bounded and measurable.

## 7. Current prototype evidence — diagnostic only

Provider-free exploration on the two existing calibration cases tested audio, 4 fps proxy YDIF motion, and fused rankings. The implementation has now been executed on both real calibration sessions, not only simulated scores:

- `m8-real-cal-01`: 8 anchors -> 7 proposal clips, **161.0 s / 600.886 s = 26.7938%** proposal coverage, and the proposals overlap **3/3** currently annotated calibration highlights.
- `m8-real-cal-02`: 8 anchors -> 6 proposal clips, **141.0 s / 600.870 s = 23.4660%** proposal coverage, and the proposals overlap **2/2** currently annotated calibration highlights.
- Combined proposer recall on the currently annotated calibration highlights is therefore **5/5**, while unique proposed timeline coverage is about one quarter of each source. Both runs persist `semantic_labels_inferred=false` and `provider_calls=0`; no Gemini call or upload was made.

H2 boundary hardening now adds a 60 s default maximum proposal duration with a bounded 10 s overlap when a nearby anchor cluster would exceed that cap. Each anchor's own event-centered pre/post context is preserved in at least one proposal. Parameterized regression places an event across internal analysis boundaries throughout a ten-minute source, and the complete event remains visible inside at least one proposal. Long anchor clusters are also forced through the duration cap and verified to split with overlap rather than an arbitrary hard cut. Targeted hybrid verification is **16/16 PASS**; full Ruff passes `src` + `tests`; mypy passes **74 source files**; and the full provider-free regression passes **351/351 in 101.98 s**.

This is **not** M8 acceptance evidence. It is calibration-only evidence that a local proposal layer can materially reduce search area without destroying recall on the currently labelled calibration moments. Exact top-K values and future game-profile cues remain experimental; the revealed v13 validation holdout is still forbidden for tuning.

## 8. GitHub architecture adaptations

The design is intentionally inspired by patterns seen in current open-source gameplay clipping projects, without copying their game assumptions blindly.

### Auto-Clipper

Reference: `https://github.com/bendawg2010/Auto-clipper`

Useful adaptations:

- explicit CV / YOLO / voice / hybrid detector modes;
- per-game detection profiles;
- parallel detector sources feeding one scoring/fusion stage;
- fast pixel/ROI analysis before expensive inference.

Adapt here as a generic proposer interface plus versioned game profiles. Do not require YOLO for V1.

### Game Highlight Detector (`rfypych/video-highlight-detection`)

Reference: `https://github.com/rfypych/video-highlight-detection`

Useful adaptations:

- combine audio, OCR, and visual state instead of betting on one signal;
- configurable per-resolution HUD regions;
- merge clips that occur close together;
- pre/post clip padding around detected events.

Adapt the ROI/configuration pattern, but use OCR only as supporting proposal evidence.

### Gameplay Highlight Suite

Reference: `https://github.com/Volpestyle/gameplay-highlight-suite`

Useful adaptations:

- keep the raw recording as source-of-truth;
- use a lightweight proxy for analysis/review;
- store events separately from final highlight clips;
- allow event padding, merge/split, and later export from the raw source;
- optional structured game-event ingestion where supported.

This strongly matches the existing project architecture and reinforces that proposal boundaries should never become final destructive cuts.

### NiceShot_AI

Reference: `https://github.com/karimm-ai/NiceShot_AI`

Useful adaptations:

- event confirmation using OCR/state checks;
- distinguish special UI states such as killcam/spectating from real events;
- cluster consecutive kills into higher-level events;
- export both horizontal and vertical/TikTok-oriented output later.

Adapt event confirmation/clustering concepts only after the generic proposer seam is stable.

### Neko's AI Clipper

Reference: `https://github.com/NekoSuneProjects/nekos-ai-clipper`

Useful adaptations:

- many game-specific HUD/OCR profiles rather than one universal detector;
- kill-streak clustering;
- reaction detection as another signal source;
- local GPU/CPU media path and vertical output support.

The relevant lesson is architectural: game-specific detectors should plug into a generic fusion layer, not fork the whole pipeline.

## 9. Implementation slices

### H1 — Generic hybrid proposer

- [x] Prototype source-normalized audio ranking.
- [x] Prototype low-cost proxy motion extraction.
- [x] Deterministic proposal data model / plan / cache implementation.
- [x] Unit/integration prototype tests green.
- [x] Finish targeted lint/type/test gates: pytest 16/16, targeted mypy PASS, targeted Ruff PASS.
- [x] Persist proposal-stage provenance and source-relative timestamps.
- [x] Ensure zero provider calls and zero semantic label inference.
- [x] Execute both real calibration sessions provider-free: currently annotated proposer recall 5/5 with 26.79% and 23.47% unique timeline coverage.

Exit: deterministic proposals are reproducible and source-relative; fixed chunk boundaries are not used as semantic cuts.

### H2 — Proposal boundary/merge hardening

- [x] Event-centered configurable pre/post proposal context.
- [x] Overlap/nearby merge and bounded NMS.
- [x] Hard maximum proposal duration with safe split strategy that preserves overlap.
- [ ] Optional boundary-hint interface; defer until a game profile proves a stable round/HUD cue worth consuming.
- [x] Tests for an event/round crossing internal analysis boundaries across the source timeline.

Exit: synthetic and real calibration tests prove an event crossing an internal boundary remains fully visible to at least one semantic-judge proposal.

### H3 — Valorant profile v1

- [ ] Define resolution-normalized HUD ROIs.
- [ ] Evaluate kill-feed/round-state cues provider-free.
- [ ] Add cue clustering for rapid multi-kill sequences.
- [ ] Detect/discount menu, buy-only, spectating, and other non-event UI states where reliable.
- [ ] Keep generic fallback fully functional when profile evidence is absent.

Exit: profile adds proposal recall/precision benefit on calibration without becoming a hard dependency.

### H4 — Gemini judge contract

- [ ] New semantic-judge prompt/schema accepts only bounded proposal media.
- [ ] Return `KEEP | REJECT | UNCERTAIN`, semantic category, score/confidence, visible evidence, and proposal-relative event bounds.
- [ ] Judge may return multiple distinct events from one merged proposal when necessary.
- [ ] Deduplicate across overlapping proposals locally.
- [ ] Reuse current M4/M5 cost ledger, cache, upload privacy, and cleanup lifecycle.

Exit: fake/injected transport tests green; provider-free aggregate preflight shows exact proposal count and exposure before any live call.

### H5 — Calibration decision

- [ ] Measure proposer recall independently from Gemini judge quality.
- [ ] Run one bounded, newly authorized live calibration only after provider-free gates pass.
- [ ] Compare end-to-end recall, false-positive burden, review duration, cost/source-hour, and source immutability.
- [ ] Do not tune against the revealed v13 validation holdout.

Decision:

- If hybrid materially improves usable shortlist quality, make it the V1 candidate architecture.
- If local proposer recall is the blocker, improve proposal sources/game profiles rather than prompt-tuning Gemini.
- If Gemini judge remains the blocker even on short candidate clips, evaluate a different multimodal judge/model before adding more local heuristics.

### H6 — Fresh locked sanity / V1 closeout

- [ ] Prepare fresh holdout before predictions.
- [ ] Lock annotations/evaluation policy.
- [ ] Run final bounded acceptance.
- [ ] If usable for the personal workflow, lock V1 and move remaining ideas to V1.1/V2 instead of extending M8 indefinitely.

## 10. What is deliberately deferred

- training a custom YOLO model before simpler ROI/CV evidence is proven insufficient;
- mandatory OCR/Tesseract dependency for generic mode;
- universal round segmentation for every game;
- automatic publishing/social integration;
- M9 Reviewer as a separate paid post-extraction stage;
- additional prompt-only Scout versions unless a measured experiment justifies them.

## 11. Definition of success for this amendment

This architecture is successful if, on fresh locked evidence, it can preserve the important moments while making Gemini inspect a materially smaller bounded portion of the source, producing a shortlist the user can realistically review and choose from. The goal is **usable personal highlight discovery**, not perfect academic event segmentation.
