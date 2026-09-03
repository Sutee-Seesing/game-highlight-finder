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

H4 now implements the bounded semantic-judge seam without changing the local proposer contract. It accepts only hash-bound `analysis_proposal.mp4` media, returns `KEEP | REJECT | UNCERTAIN` with proposal-relative event bounds and visible evidence, maps kept bounds back to source time, and deduplicates duplicate judgments from overlapping proposals. Fake and injected Gemini transports share aggregate cost preflight, cache identity, upload validation, cleanup, and ambiguous-call safety. Targeted Hybrid/H4 verification is **29/29 PASS**, Ruff passes, mypy passes **76 source files**, and the full provider-free suite passes **364/364 in 111.83 s**. Real Gemini generations remain **0** for this H4 checkpoint.

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

- [x] New semantic-judge prompt/schema accepts only bounded proposal media.
- [x] Return `KEEP | REJECT | UNCERTAIN`, semantic category, score/confidence, visible evidence, and proposal-relative event bounds.
- [x] Judge may return multiple distinct events from one merged proposal when necessary.
- [x] Deduplicate across overlapping proposals locally.
- [x] Reuse current M4/M5 cost ledger, cache, upload privacy, and cleanup lifecycle.

Exit: fake/injected transport tests green; provider-free aggregate preflight shows exact proposal count and exposure before any live call. **Implemented provider-free on 2026-09-02:** targeted Hybrid/H4 verification is 29/29 PASS, Ruff passes, mypy passes 76 source files, and the full suite is 364/364 PASS in 111.83 s. The injected-transport path validates exact proposal-media hashes, preflights the whole batch before reservation, persists the existing cost lifecycle, treats ambiguous post-dispatch state as non-retriable, and reuses only verified `SETTLED` cache entries. No real provider generation was executed.

### H5 — Calibration decision

- [x] Measure proposer recall independently from Gemini judge quality: current calibration proposer coverage is 5/5 annotated highlights while forwarding about one quarter of each source.
- [x] Complete real proposal-batch provider-free preflight with a fresh explicit FX snapshot before any live call.
- [x] Run one bounded, newly authorized live calibration only after provider-free gates pass.
- [x] Compare end-to-end recall, false-positive burden, review duration, cost/source-hour, and source immutability for cal-01.
- [ ] Do not tune against the revealed v13 validation holdout.

Provider-free H5 preflight on T used the Bank of Thailand 01 Sep 2026 USD/THB reference rate snapshot (`33.2030`) and made **0 provider calls, 0 remote uploads, and 0 ledger reservations**. With `gemini-3.7-flash`, cal-01 contains 7 cached proposal clips with a combined maximum reservation of **THB 2.758416**; cal-02 contains 6 cached proposals with a maximum reservation of **THB 2.374214**; both calibration cases together would cap at **THB 5.132630**. A cost-only comparison with `gemini-3.5-flash-lite` is **THB 1.674566** for cal-01 and **THB 1.439285** for cal-02 (**THB 3.113851** combined). Because the remaining blocker is semantic quality rather than provider cost, the first live experiment should prefer the stronger `gemini-3.7-flash` judge and execute **cal-01 only** before considering cal-02. This is an experiment choice, not a locked V1 model default.

The first authorized live cal-01 attempt began on 2026-09-02 with an explicit cap of **7 proposal generations / THB 2.76 maximum exposure / no automatic generation retry**. It intentionally stopped after **2/7** distinct proposal calls when the second semantic response violated the strict local contract; the remaining five proposals were never sent. Proposal `56-76s` was correctly returned as `REJECT` for buy-phase/spawn-only activity, settled at **THB 0.056155**, and its remote media was deleted. Proposal `115-135s` overlaps calibration highlight `hl-0001` (`120-136s`); Gemini returned a completed response containing an event, but used `confidence=6.8` instead of the required `0-1` scale, so the strict parser refused to promote it. The pre-hardening lifecycle conservatively left that second call `AMBIGUOUS` at a **THB 0.384830** reservation and still deleted its remote media. This is a contract/lifecycle finding, not enough evidence to score hybrid semantic quality.

Provider-free hardening after that stop makes the numeric contract explicit in the prompt (`score 0-10`, `confidence 0-1`), settles a successfully completed provider call before semantic parsing, persists the sanitized raw completed envelope before parsing, and ensures a later local semantic-contract failure remains `SETTLED` rather than being mislabeled as provider ambiguity. The same settled request remains non-retriable without a changed request identity. Verification after the hardening is **14/14 targeted PASS**, Ruff PASS, mypy PASS over **76 source files**, and **365/365 full pytest PASS in 99.85 s**. A fresh provider-free preflight of the hardened request identity makes **0 calls / 0 uploads / 0 reservations** and now quotes `gemini-3.7-flash` at **THB 2.761373 for cal-01 (7 calls)** and **THB 2.376752 for cal-02 (6 calls)**, **THB 5.138125 combined**. The extra explicit scale instruction increased cal-01's maximum quote by only THB 0.002957, but it also means the previous THB 2.76 authorization is no longer sufficient for a complete hardened 7-call rerun. Because the prompt/request identity changed and the original authorization specified no automatic retry, another live call now requires fresh explicit authorization rather than silently consuming the five unused attempts.

The newly authorized hardened cal-01 rerun then completed all **7/7 distinct proposal calls** under **THB 2.77 maximum exposure / no automatic generation retry**. All seven ledger rows are **SETTLED**, actual settled usage is **THB 0.517870** against **THB 2.761373** maximum reservation, and all seven remote media objects report cleanup **deleted**. Gemini returned 4 candidates against the 3 currently annotated cal-01 highlights: **3 TP / 1 FP / 0 FN, precision 0.75, recall 1.00, MUST_CATCH recall 1.00, WORTH_REVIEW recall 1.00**. The local proposer forwarded **161.0 s / 600.886 s = 26.7938%** of the source; the four kept event intervals total **55 s / 600.886 s = 9.1532%** of source duration, so the judge materially reduces the human shortlist after proposal generation. Actual judge cost is approximately **THB 3.1026 per source-hour** for this case. Source immutability was rechecked after the run: the original 6,282,414,778-byte file still hashes to `7db9940058f764d1725f89340e8c1226d80b31671953739b7be9aeb06d2ac726`, exactly matching the persisted source manifest. This is strong positive calibration evidence for the hybrid architecture on cal-01, but it is not an M8 acceptance result.

A separately authorized hardened cal-02 run then started under **Gemini 3.7 Flash / 6 attempts / THB 2.39 / no automatic retry**. It stopped after **5/6** distinct proposal calls when proposal `386-421s` returned an HTTP 401 authentication error after HTTP dispatch. The ledger is **4 SETTLED + 1 AMBIGUOUS**, with **THB 0.242824** actually settled and **THB 0.431596** reserved on the ambiguous request; proposal 6 was never sent. Remote cleanup is **deleted 5/5**, including the failed request. The four successful calls all returned REJECT, but none overlaps either currently annotated cal-02 highlight. Critically, the ambiguous fifth proposal overlaps `hl-0001` (`401-417s`) and the unsent sixth proposal overlaps MUST_CATCH `hl-0002` (`574-590s`). Therefore the partial cal-02 run is **semantic-inconclusive rather than a quality failure**: neither known positive was successfully adjudicated. The source remains immutable at 2,805,344,323 bytes with SHA-256 `8d973547b93d432a4deb5f4880ea08fe6cbb7466a6c08a5de0d2e94f0ace2126`. Do not retry the ambiguous request or send proposal 6 without a fresh explicit authorization. Revealed v13 validation remains forbidden for tuning, and final M8 acceptance still requires a fresh locked holdout.

Provider-free remediation first pinned only the Hybrid Judge to stable Gemini Interactions API **`v1`**, with the API version in request/cost identity and a fail-closed non-v1 guard. That implementation passed **35/35 targeted, Ruff, mypy over 76 source files, and 367/367 full pytest** before a separately authorized positive-subset continuation was attempted at **2 calls / THB 0.82 / no automatic retry**. The live run stopped after **1/2** calls: stable-v1 Interactions returned HTTP **400 `invalid_request`** because `type=video` is not accepted on that stable input surface. The call remains conservatively **AMBIGUOUS at THB 0.431596 reserved / THB 0 settled**, its remote upload was deleted, proposal 2 was never sent, and source immutability remains intact. This is an endpoint-contract failure and yields no cal-02 semantic verdict; the consumed authorization cannot be reused.

Provider-free remediation therefore keeps stable **`v1`** but changes the Hybrid Judge generation surface to **`models.generate_content`**, which supports Files/video input. Both `api_version=v1` and `api_surface=generate_content` are now included in provider-request/cost identity, and the judge rejects any other endpoint surface before reservation/upload/generation. The adapter preserves Files upload/cleanup and converts authoritative generateContent usage metadata, including video/audio/text prompt modality token counts, into the existing ledger contract. Verification is **37/37 targeted PASS, Ruff PASS, mypy PASS over 76 source files, and 369/369 full pytest PASS in 147.38 s** under task `9ec4d11a-c5cb-45b5-900a-fbe3f53047f7`.

The narrowed continuation still targets only `386-421s` and `574-594s`. Under the fresh generateContent request identity, provider-free preflight remains **2 planned calls / THB 0.816844 maximum reservation / 0 provider calls / 0 uploads / 0 ledger reservations / 0 judge cache hits**. The dedicated generateContent helper is syntax-valid, keeps **attempt cap 2 / exposure cap THB 0.82 / no automatic retry / stable v1**, and its new ledger/summary are absent. A fresh authorization then ran this two-call generateContent subset. Proposal `386-421s` completed as **KEEP / FUNNY** with a relative event at `13-25s`, mapping to approximately `399-411s` source time and overlapping WORTH_REVIEW `hl-0001` (`401-417s`). It is **SETTLED at THB 0.108898** and cleanup is deleted. Proposal `574-594s`, which covers MUST_CATCH `hl-0002` (`574-590s`), reached the provider but failed with HTTP **503 `UNAVAILABLE`** because Gemini 3.7 Flash was under temporary high demand. It remains conservatively **AMBIGUOUS at THB 0.385248 reserved**, has no semantic response, and its remote upload was deleted. The original source hash remains unchanged. Cal-02 therefore now has one confirmed positive match, but the MUST_CATCH positive remains unresolved, so H5 cal-02 is still semantic-inconclusive.

A post-incident audit found that `google-genai 2.21.0` defaults to up to **5 SDK HTTP attempts** for retryable 408/429/5xx responses when `retry_options` are omitted. The application/batch layer made no retries, but the exact number of SDK-internal HTTP attempts inside the failed 503 call is unknown; therefore the earlier `no automatic retry` guarantee was incomplete. Provider-free hardening now pins the Hybrid Judge SDK client to **`retry_options.attempts=1`**, fails closed before reservation/upload/generation when that invariant is absent, and adds **`sdk_http_attempts=1`** to provider-request/cost identity. Verification is **39/39 targeted PASS, Ruff PASS, mypy PASS over 76 source files, and 371/371 full pytest PASS in 113.16 s** under task `611d6a96-4bee-4196-bb5e-e4fc42e2af5a`.

The single unresolved MUST_CATCH proposal `574-594s` was then run under a fresh explicit authorization with **attempt cap 1 / exposure cap THB 0.39 / SDK HTTP attempts 1 / no application retry**. The provider call completed normally and SETTLED at **THB 0.056977**, remote cleanup is **deleted**, and source immutability still holds, but Gemini returned **REJECT with zero events**, describing the interval as standard hide-and-seek exploration with no highlight-worthy moment. Because `hl-0002` (`574-590s`) is a locked calibration MUST_CATCH highlight inside that proposal, this is a genuine semantic miss rather than an infrastructure failure.

Final cal-02 semantics now combine the four earlier settled REJECTs outside the annotations, the KEEP/FUNNY event around source `399-411s` matching WORTH_REVIEW `hl-0001`, and the final MUST_CATCH REJECT: **1 prediction / 2 GT / TP=1 / FP=0 / FN=1 / precision=1.00 / recall=0.50 / WORTH_REVIEW recall=1.00 / MUST_CATCH recall=0.00**. The proposer itself covered **2/2** known positives, so cal-02 isolates the remaining quality gap to the Gemini semantic judge. H5 cal-02 is therefore **closed as a quality failure**. H5 does not qualify the current Gemini judge as the V1 semantic gate, M8 remains **NOT ACCEPTED**, and V1 defaults remain unlocked. Do not tune against revealed v13 validation; the next step should be a bounded provider-free plan for an alternate-judge same-calibration comparison before any new paid experiment or fresh locked holdout.

### H5A — Provider-free OpenRouter alternate-judge bake-off

The alternate-judge slice now selects **Z.AI `glm-5v-turbo`** as the quality-first comparator. This supersedes the earlier provisional GLM-4.6V choice because the current Z.AI catalog lists GLM-5V-Turbo as the newer top vision model, with Video/Image/Text/File input, 200K context, and USD list pricing of **$1.20/M input, $0.24/M cached input, and $4.00/M output**. The pricing snapshot is verified on 2026-09-03 from `https://docs.z.ai/guides/overview/pricing`; capability provenance is `https://docs.z.ai/guides/vlm/glm-5v-turbo`.

The comparator remains **provider-free with no paid call authorized**, but its live transport boundary is now implemented through OpenRouter rather than an inferred direct-Z.AI upload path. It reuses the exact locked `hybrid-judge-v1` prompt, JSON contract, parser, candidate mapping, and the same calibration proposal media used by Gemini. The cost/provider identity is `openrouter` with exact model `z-ai/glm-5v-turbo`; routing is pinned to upstream Z.AI with fallbacks disabled and HTTP attempts=1. Strict local semantic validation remains authoritative, and no request can run until an `OPENROUTER_API_KEY` credential is supplied plus a fresh explicit paid-call authorization is granted.

The same-calibration A/B scope is locked to the existing **13 proposals / 302 seconds** only: cal-01 has 7 proposals / 161 s and cal-02 has 6 proposals / 141 s. Revealed v13 validation is not touched. The selected live-capable route is now **OpenRouter -> Z.AI `z-ai/glm-5v-turbo`**, not an inferred direct-Z.AI upload contract. OpenRouter officially accepts local/private MP4 as `data:video/mp4;base64,...`; its current endpoint catalog exposes one GLM-5V-Turbo endpoint, Z.AI, at USD 1.20/M prompt tokens, USD 0.24/M cache-read tokens, and USD 4.00/M completion tokens. The comparator keeps the provider-neutral `hybrid-judge-v1` semantic prompt/schema/parser. Because GLM-5V-Turbo exposes JSON-object mode rather than provider-enforced JSON Schema, the exact schema is appended only as a provider formatting contract while local Pydantic parsing remains authoritative.

The OpenRouter transport is deliberately auditable: one stdlib HTTP POST with **no client retry**, local MP4 base64 encoding, a 16 MiB encoded-request guard, `usage.include=true`, reasoning enabled with reasoning text excluded, and exact routing controls `only=[z-ai]`, `allow_fallbacks=false`, `require_parameters=true`. The router `max_price` ceiling uses OpenRouter's routing contract in **USD per million tokens** (`1.2` prompt / `4.0` completion for GLM-5V-Turbo); the local reservation catalog separately normalizes those list prices for per-token accounting. Request identity records the OpenRouter provider, Z.AI upstream lock, no-fallback policy, HTTP attempts=1, and `openrouter-base64-video-v1`. Successful responses are settled from authoritative OpenRouter usage before local semantic parsing; routing metadata must then prove `attempt=1` and selected provider `Z.AI`, otherwise the billing record stays SETTLED but no semantic response is reusable. Existing proposal clips are 4.6-11.0 MB, with the largest estimated base64 payload about 14.7 MB, inside the local 16 MiB request guard.

The local reservation heuristic remains intentionally conservative and versioned (`zai-video-estimate-v1`): 256 video tokens/s plus bounded prompt/schema text, 1,024 visible output tokens, and 1,024 reserved thinking tokens per request when thinking is enabled. This is a safety quote, **not** a tokenizer claim. Using the existing 2026-09-01 USD/THB FX snapshot, provider-free preflight still quotes cal-01 at **THB 4.461257**, cal-02 at **THB 3.861802**, and the full 13-call comparison at **THB 8.323059 maximum reservation**, with **0 provider calls / 0 remote uploads / 0 ledger reservations / 0 cache hits** and `live_media_transport_verified=true`. A cheaper discriminating first gate is prepared on only the two locked positive cal-02 proposals (`386-421s` WORTH_REVIEW and `574-594s` MUST_CATCH): **2 planned calls / THB 1.385522 maximum reservation / 0 calls / 0 uploads / 0 reservations / 0 cache hits**. If GLM-5V-Turbo still misses MUST_CATCH, stop without paying for the remaining 11 proposals; if it catches both positives, the remaining 11 can be run later to measure precision. Current OpenRouter/Gemini targeted verification is **23/23 PASS**, Ruff PASS over `src + tests`, mypy PASS over **78 source files**, and canonical full pytest is **384/384 PASS in 106.24 s** under durable task `62037e5f-743c-4ba7-9942-c1d67b5a4872`. No paid OpenRouter call is authorized by this provider-free work, and `OPENROUTER_API_KEY` is not currently present in either the process environment or the checked local `.env` declarations. A live experiment therefore requires both credential setup and fresh explicit paid-call authorization.

Round A now supersedes the GLM-5V-Turbo-only first gate above. The locked tournament profiles are
Qwen3.8 Flash -> Alibaba, GLM-5.3-Flash -> Z.AI, MiMo-V2.5 -> Xiaomi direct, Seed-2.0-Mini ->
Seed, Seed-2.0-Lite -> Seed, GLM-5V-Turbo -> Z.AI, and Qwen3.8 Max -> Alibaba. Each profile pins
one upstream provider with no fallback, one HTTP attempt, exact USD-per-million routing price ceilings,
context and pricing-tier guards, and a response-format contract. Qwen/Seed endpoints use JSON Schema;
GLM/MiMo use JSON-object formatting plus strict local validation. Artifacts/cache identity now
include the model slug so evidence cannot collide across challengers.

Round A uses only the two locked cal-02 positives: `386-421s` WORTH_REVIEW that Gemini caught and
`574-594s` MUST_CATCH that Gemini missed. Provider-free preflight therefore plans **14 logical
calls** and reserves a conservative maximum of **THB 4.663985 total**: Qwen3.8 Flash 0.168295,
GLM-5.3-Flash 0.173191, MiMo-V2.5 0.131181, Seed-2.0-Mini 0.126341, Seed-2.0-Lite 0.479051,
GLM-5V-Turbo 1.385522, and Qwen3.8 Max 2.200404. It made 0 provider calls / 0 uploads / 0 ledger
reservations / 0 cache hits. GLM-5.3-Flash uses the undiscounted list price for reservation despite
the temporary promotion. Seed models fail closed before their >=128K higher-price tier. Only
models that preserve the WORTH_REVIEW positive and catch the MUST_CATCH miss should advance to the
13-proposal / 302-second precision/value round. Current provider-free verification is 32/32
targeted PASS, Ruff PASS, mypy 79 source files PASS, and canonical full pytest **393/393 PASS in
114.72s** under task `9f673f76-ef08-4b8b-b6b3-4f5f5c740c4b`. The original prepared local live runner used dedicated fresh ledger/preflight-ledger/summary artifacts, a THB 4.67 hard ceiling, logical attempt cap 14, HTTP attempts=1, no automatic retry, and MUST_CATCH-first early stopping. Its 14-call / THB 4.663985 preflight was the worst-case pre-live boundary, and it compiled/imported provider-free with dedicated live artifacts absent before execution. The actual live outcomes and narrower current recovery boundary are recorded below.

The first authorized live Round A dispatch on 2026-09-03 consumed **7 logical router requests** because every model failed at the MUST_CATCH request and therefore triggered the locked early-stop before WORTH_REVIEW. This was **not a semantic model failure**: all seven requests returned OpenRouter HTTP 404 `No endpoints found that satisfy the max price for this request`, with no completed model response and **THB 0 settled**. Root cause was a transport-unit defect: `provider.max_price` was sent in USD per token even though OpenRouter routing expects USD per million tokens, making every ceiling 1,000,000x too low and filtering all upstream endpoints. The pre-remediation ledger conservatively retains seven AMBIGUOUS rows / **THB 2.025090 unresolved reservation** because the exact error routing metadata was not persisted. Remediation sends the documented USD-per-million ceiling and interprets explicit router `attempt=0` as confirmed pre-upstream rejection, allowing the reservation to be released instead of becoming ambiguous; missing routing evidence remains conservative. The corrected v3 identity prevented collision with the incident call IDs and passed **49/49 targeted**, Ruff, mypy over **79 source files**, and full pytest **395/395** before the replacement live round.

The fresh authorized replacement Round A then made **7 MUST_CATCH calls** and no WORTH_REVIEW calls. MiMo-V2.5 and GLM-5V-Turbo returned valid settled responses but both said `REJECT`, so they are genuine semantic MUST_CATCH misses under the current prompt/video evidence. Seed-2.0-Mini returned a settled response that ended with `finish_reason=length`; Qwen3.8-Flash, GLM-5.3-Flash, Seed-2.0-Lite, and Qwen3.8-Max reached the post-dispatch parser without usable final text and remained conservative AMBIGUOUS. Known settled cost is **THB 0.390358**, unresolved reservation **THB 1.314330**, and known-plus-unresolved exposure **THB 1.704688**, comfortably below the THB 4.67 cap. Seed Mini's raw envelope isolates the second transport-contract defect: the reservation already budgeted 1,024 final-output plus 1,024 thinking tokens, but the wire request constrained the entire completion to `max_tokens=1024`; Seed Mini consumed **1,016 reasoning + 8 visible tokens** and truncated its JSON. MiMo consumed 936 reasoning + 81 visible tokens and GLM-5V consumed 613 reasoning + 78 visible tokens. The four missing-text cases are consistent with completion-budget starvation but cannot be claimed as proven length exhaustion because the v3 transport did not persist those successful HTTP bodies after its text-content validation failed.

The v4 remediation is provider-free and makes the request/ledger contract internally consistent: reasoning-enabled calls send **2,048 combined completion tokens** with `reasoning.max_tokens=1,024`, while the conservative cost quote remains exactly 1,024 final + 1,024 thinking. A successful HTTP response may now carry empty final text through the transport long enough to parse authoritative usage, settle the ledger, and persist its raw envelope; only then does the pipeline reject `finish_reason=length` or missing final text. This closes the observability/lifecycle gap without weakening semantic validation. Request identity is now `hybrid-judge-openrouter-v4` with media contract `openrouter-base64-video-v2`. Final provider-free verification is **51/51 targeted PASS**, Ruff PASS over `src + tests`, mypy PASS over **79 source files**, and canonical full pytest **397/397 PASS in 98.23s** under durable task `c60cc148-6443-4275-a8fb-427f007b1ea5`. The seven-model v4 audit preflight remains **14 planned calls / THB 4.663985 maximum reservation**, but MiMo-V2.5 and GLM-5V-Turbo are deliberately excluded from the next recovery because their v3 calls already completed, settled, and returned definitive `REJECT` on MUST_CATCH. Recovery therefore targets only Qwen3.8-Flash, GLM-5.3-Flash, Seed-2.0-Mini, Seed-2.0-Lite, and Qwen3.8-Max. The locked five-model v4 recovery preflight is **10 planned calls / THB 3.147282 maximum reservation**, with provider calls/uploads/reservations/cache hits all zero, zero collisions with prior replacement call IDs, and zero call/event/control rows.

The authorized v4 recovery completed in **9 calls** with **THB 1.267537 settled + THB 1.349723 unresolved = THB 2.617260 known-plus-unresolved**, safely below the THB 3.15 hard cap. Qwen3.8-Flash and Qwen3.8-Max both caught MUST_CATCH but their WORTH_REVIEW calls returned HTTP 413 and remain AMBIGUOUS; GLM-5.3-Flash said `KEEP` but selected 591.5-594s, missing the locked MUST_CATCH interval by starting after its 590s end; Seed-2.0-Mini caught MUST_CATCH but exhausted the 2,048-token combined ceiling on WORTH_REVIEW. Seed Mini reported **1,906 thinking tokens** despite `reasoning.max_tokens=1,024`, so that upstream reasoning sub-cap is treated as non-authoritative while the combined completion/cost ceiling remains authoritative. **Seed-2.0-Lite alone passes the full Round A semantic gate**, catching MUST_CATCH at 574-580s and WORTH_REVIEW at 401-405s with settled `finish_reason=stop`, router attempt=1, provider=Seed responses. Its two calls settled at **THB 0.215223 total**. It therefore advances alone to Round B, the locked 13-proposal / 302-second precision/value evaluation. A naive fresh 13-call Seed Lite preflight is THB 2.971575, but the two Round A positives already have settled v4 evidence and must be reused rather than rerolled. The actual Round B execution plan is therefore **2 reused settled responses + 11 new proposals / 247 seconds**, with provider-free maximum new reservation **THB 2.492524**. The dedicated runner/secure launcher compiles, enforces 11-call cap / HTTP attempt=1 / no automatic retry, proposes a THB 2.50 hard exposure cap, and has no live artifacts yet. Round B paid execution requires a separate fresh authorization.

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
