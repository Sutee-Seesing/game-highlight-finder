# VDO Current State

Updated: 2026-09-07 Asia/Bangkok

## Product North Star

Game Highlight Finder is a **multi-game creator assistant** whose purpose is to help the owner build an audience from zero with short-form gaming content (TikTok first) and eventually turn that audience into livestream viewers.

It is not an FPS kill detector. Competitive-FPS kill cases are engineering diagnostics because they provide clear temporal truth, but they do not define product quality.

Primary creator loop:

`play/record -> creator candidate pack -> choose moments -> AI rough-edit drafts -> human final edit/approval -> publish -> learn from real audience performance -> repeat`

Canonical creator strategy:

`docs/10_CREATOR_GROWTH_AND_AI_EDITING_PLAN.md`

## What the product should output first

The first usable product milestone is **Creator Candidate Pack Beta**:

- analyze a long 1–4+ hour VOD;
- surface a small ranked set of creator-worthy moments;
- support multiple games/gameplay styles;
- treat funny/fail/reaction/friend/social/WTF/tension-payoff/discovery/personality/skill/clutch as first-class moment families;
- preserve setup -> event -> payoff context;
- extract candidate clips from the original source;
- let the owner review candidates instead of scrubbing the full VOD;
- keep detection confidence separate from creator/short-form worthiness.

The candidate clips are **raw material for editing**, not necessarily finished TikToks.

## AI editing direction

AI editing is now explicitly on the roadmap, but as a non-destructive **AI rough editor** after candidate discovery becomes usable.

Initial AI-edit scope should operate on owner-selected candidates and produce draft variants with:

- true-dead-air/pacing tightening, with the hard rule `silence != dead air` and `no speech != boring`;
- hook/opening suggestion;
- safe 9:16 reframing;
- draft captions/subtitles;
- loudness cleanup;
- restrained punch-ins/zooms;
- caption/on-screen-hook suggestions.

Always preserve the untouched candidate and keep human final approval. Fully automatic publishing remains later.

## C1 implementation progress

Provider-free implementation is now underway:

- added generic `DISCOVERY` candidate category;
- added backward-compatible `moment_summary` (what happened) and `creator_reason` (why review it) candidate semantics;
- legacy Scout responses fall back to `reason` for those fields so old artifacts remain usable;
- added creator-balanced `gemini-scout-window-v20-creator` prompt and made it the new default prompt version while keeping window duration configurable;
- the creator prompt explicitly treats visual-only gameplay as first-class and states `silence != dead air` / `no speech != boring`;
- social/personality moments can qualify without a gameplay payoff when they have a self-contained audience payoff;
- ranking now exposes an explicit `creator_score` while preserving short-form score and detection confidence;
- the report now renders a `Creator Candidate Pack` with `What happened`, `Why review this`, creator score, detection confidence, Open Clip, and technical lineage behind secondary details;
- `config.example.yaml` now uses the creator prompt default instead of the old v18 prompt;
- focused offline tests cover creator prompt semantics, `DISCOVERY`, creator fields, ranking, report rendering, quiet discovery, social payoff, fail/tension story, boring-zero-candidate behavior and legacy fallback;
- a provider-free end-to-end integration smoke test now exercises window payload -> canonicalization -> reconcile -> clip boundaries -> ranking -> extraction manifest -> report.

Focused C1 integration result: **4 passed in 0.56s**.

A local five-family Creator Candidate Pack demo was generated under `.t/c1-creator-pack-demo/` with synthetic playable MP4 fixtures/thumbnails for `DISCOVERY`, `FUNNY`, `FAIL`, `REACTION`, and `SKILL`. It uses the actual report/ranking pipeline and made **0 provider calls**.

The fresh complete local suite including the newest integration test/config-example state passed **278 tests in 222.38s (0:03:42)** via durable task `9d34102e-75e6-4b17-a751-02b343a33ed6`. The provider-free C1 implementation/test gate is green.

Real-media validation is specified in `docs/13_C1_REAL_MEDIA_VALIDATION_PLAN.md`. Provider-free V-C1-A fast-action preflight is prepared from cal-01 using the creator prompt: 3 x 300s/30s-overlap windows (last window shorter), aggregate estimated reserve **฿2.514798**, provider calls **0**, uploads **0**, and no live authorization. The three corresponding local `analysis_window.mp4` files now exist under the ignored cal-01 session `data/sessions/2026-08-15_unknown_7db9940058f7/scout/windows/` for window ids `scout_window_87ac84ad01cdabbf`, `scout_window_ef57ca109a4e9708`, and `scout_window_919f91ad02042c1c`. Creator review and obvious-miss templates are also prepared under local-only `.t/c1-real-media-validation/`. Source scouting for sandbox/exploration (V-C1-B) and social/co-op/personality-heavy (V-C1-C) has started from owner OBS media, but neither source is selected yet.

## Benchmark status / what we learned

GT v2 calibration truth remains preserved for regression work. Do not overwrite old GT or use revealed validation holdout for tuning.

Historical v19 300s-window calibration showed weak creator/event detection and missed both cal-01 MUST_CATCH kills.

v20 targeted 120s diagnostic:

- 2 paid calls, actual settled cost `฿0.453404`;
- recovered the `348–350s` kill;
- still missed the `447–448s` kill under the locked temporal ruler;
- false positives remained inside annotated boring intervals.

v21 targeted 90s diagnostic is complete:

- exactly 2 paid calls, no automatic retry;
- actual settled cost `฿0.423648`;
- first known kill surfaced at `346–353s` (GT `348–350s`);
- second known kill was semantically surfaced inside a broad `445–465s` multi-kill candidate (GT `447–448s`), showing detection improved but boundaries remained too broad for the locked IoU ruler;
- remote/provider lifecycle completed with no remaining reserved/in-flight/ambiguous exposure.

Interpretation: shorter context materially helps fast-event visibility, but this is enough evidence to stop treating window-length micro-optimization as the product goal. The next milestone is multi-game creator usefulness.

## Durable decisions

- Production quality is multi-game and creator-oriented.
- A kill is one highlight type, not the definition of highlight.
- Benchmarks are regression tools, not an endless shipping gate.
- `short_form_score` is an editorial prior, not a virality prediction.
- AI rough editing follows candidate discovery; human approval remains first-class.
- Real publish performance should later improve creator-specific ranking/editing without contaminating event-detection ground truth.

See `docs/DECISIONS.md` for durable rationale.

## Safety / workflow

- No new provider call is currently authorized.
- No automatic provider generation retries.
- Analysis proxy upload only when explicitly authorized; never raw source upload.
- Preserve existing benchmark artifacts and GT history.
- Do not clean/reset/stash/drop unrelated dirty work.

## Next action

See `docs/NEXT_ACTION.md`.
