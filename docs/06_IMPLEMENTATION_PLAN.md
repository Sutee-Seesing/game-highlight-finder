# Implementation Plan, Tests, and Risks

## 1. Delivery strategy

Build in vertical, demonstrable increments. Every milestone must leave the CLI restart-safe and tests green. Paid integration comes only after local contracts, budget refusal, and fake-provider tests work.

## 2. Milestones and exit criteria

### M0 — Plan and environment audit (complete)

- Requirements, architecture, pipeline, models, budget, risks, and tests documented.
- Environment audited without installing or implementing.
- Owner approval gate recorded.

### M1 — Foundation and ingest (complete)

- Create repository, Python 3.12 environment, packaging, lint/type/test commands, `.gitignore`, `.env.example`, and example config.
- Implement `highlight doctor`, `config check`, `analyze --stop-after ingest`, and `status`.
- Discover/pin FFmpeg and validate ffprobe output.
- Implement source hashing, `SourceAsset`, session ID, atomic JSON, lock, and stage manifest.
- Exit: a fixture and real short clip ingest twice; second run is a verified cache hit; source hash unchanged.

Implemented on 2026-08-11 with uv-managed Python 3.12, Pydantic v2, Typer, PyYAML, pytest, and Scoop-resolved FFmpeg/ffprobe 9.0. Validation covers strict configuration, doctor, safe ffprobe parsing, source hashing, atomic artifacts, manifest transitions, per-session locking, interrupted-run recovery, cache invalidation, status output, Unicode paths, and source immutability. No M2 components were added.

### M2 — Proxy and local signals foundation

- Implement command builders, progress parsing, cancellation, disk preflight, proxy generation, audio extraction, silence/loudness/activity metadata, and timestamp mapping.
- Probe and hash generated media before commit.
- Add optional hooks—not a mandatory ML installation—for transcript and scene detectors.
- Exit: representative CFR/VFR/no-audio fixtures preserve source-time mapping within tolerance.

Implemented on 2026-08-12 with CPU-safe FFmpeg command adapters, machine-readable progress parsing, cancellation/timeout handling, conservative disk preflight, atomic temporary media commits, re-probe validation, versioned timestamp mapping, mono AAC audio extraction, deterministic silence/RMS/loudness parsing, additive M1 manifest migration, stage-specific cache keys, CLI stop boundaries, and 68 automated tests. M2 is local-only and makes no AI/API calls. The default proxy uses H.264/AAC, max 854x480, preserved aspect ratio, and a 2% duration tolerance with a 500 ms floor.

### M3 — Canonical domain and fake Scout

- Implement bounded Scout schemas, semantic validator, interval math, match reconciliation, candidate dedupe, conservative story merge, ranking, and fake provider.
- Drive the pipeline end-to-end with malformed and golden fake responses before network access.
- Exit: all required malformed JSON/timestamp/overlap tests pass and a fake session map is deterministic.

Implemented on 2026-08-12 as the local-only canonical contract slice. The runtime now has bounded Pydantic Match/Candidate/Evidence/SessionMap models, controlled generic and game-profile categories, distinct score/confidence validation, half-open integer-millisecond normalization, deterministic local IDs, immutable raw Fake Scout artifacts, canonical session-map persistence, M3 cache identity, additive manifest migration, CLI `--stop-after scout`, and hostile-response validation. The complete automated suite is **94 passed / 0 failed / 0 skipped**.

### M4 — Cost gate and provider contract (approval-gated; not started)

- Implement price catalog, FX snapshot, SQLite migrations, transactional reservations, ambiguous-call handling, monthly reports, and provider registry/capabilities.
- Exit: concurrent reservation test proves the hard limit cannot be crossed; unknown/stale pricing fails closed.

### M5 — Gemini Scout integration

- Implement Files API lifecycle, low media-resolution requests, structured output, usage capture, bounded retries, remote deletion, window checkpointing, and request fingerprints.
- Verify the concrete current model ID and pricing at implementation time; initially benchmark a GA Flash-Lite-class model.
- Exit: one short opted-in proxy succeeds; cached rerun makes no generation call; all logs/artifacts are redacted.

### M6 — Long-session reconciliation and extraction

- Implement overlapping window planner/stitcher and per-window resume.
- Implement precise candidate extraction from source, pre/post-roll, thumbnails, candidate manifest, and stream-copy alternative.
- Exit: boundary accuracy and dedupe pass on synthetic cross-window cases; interrupted extraction resumes at the missing candidate.

### M7 — Report and usable V1

- Generate self-contained HTML grouped by match with chronology/ranking/category data, cost, warnings, thumbnails, and local file links.
- Implement `analyze`, `resume`, `report`, `candidates`, `cost`, and force-stage UX.
- Exit: cold and warm end-to-end runs on a representative session; warm run performs no unnecessary work.

### M8 — Real gameplay validation and tuning

- Annotate several short/medium MECCHA CHAMELEON and at least one contrasting game session.
- Measure recall proxy, precision, boundary errors, duplicates, review ratio, time, storage, and THB/hour.
- Tune proxy, window, prompt, thresholds, and generic/MECCHA profiles from evidence.
- Exit: publish a written benchmark and lock V1 defaults. A full 1–4 hour validation is required before calling V1 reliable.

### M9 — Optional Reviewer

- Batch only extracted candidates; enforce independent reservations and cache keys.
- Add keep/maybe/reject/merge suggestions without destructive rewrites.
- Exit: measured improvement in shortlist precision justifies incremental cost; otherwise leave disabled.

The original milestone order is adjusted so schemas/fake AI and budget controls precede real API integration. Cache/resume is foundational from M1 rather than added late, preventing paid-stage rework. M4 remains approval-gated and is not part of M3.

## 3. Test strategy

### Unit tests

- Config defaults, precedence, unknown keys, ranges, redaction, and stable relevant-subset hashes.
- Timecode parse/format; integer-millisecond conversions; half-open overlap/IoU.
- Pre/post-roll and duration clamping at start/end/zero-length edges.
- Match fragment reconciliation and cross-window boundary cases.
- Candidate dedupe, nested overlaps, incompatible categories, and deterministic IDs.
- Story merge relationship/gap/duration rules.
- Ranking with/without Reviewer and deterministic tie-breaking.
- Price lookup, multimodal estimates, FX/safety rounding, month boundary in Asia/Bangkok.
- Budget allow/refuse, exact-limit behavior, reservations, releases, and ambiguous outcomes.
- Cache key relevance, stale propagation, source changes, interrupted-stage recovery, and force-stage closure.
- Scout parsing: invalid JSON, schema-valid nonsense, huge lists/strings, NaN, negative/reversed/out-of-window timestamps, unknown enums, and excessive corrections.
- FFmpeg/ffprobe argument-list generation including spaces, Unicode, quotes, and Windows drive paths.

### Integration tests

- Generate tiny deterministic fixture media with FFmpeg: audio/no-audio, multiple audio streams, CFR/VFR, nonzero timestamps, short GOP/long GOP, portrait/landscape, and corrupt/truncated input.
- Run ffprobe ingest, proxy, timestamp mapping, exact extraction, thumbnail, and output re-probe.
- Kill a subprocess mid-stage and verify resume/temporary-file cleanup.
- Run two budget reservation processes against one SQLite ledger.
- Fake provider simulates rate limit, timeout-before-dispatch, ambiguous timeout-after-dispatch, malformed body, usage missing, deletion failure, and remote expiry.
- Golden report snapshot checks escaped reasons/paths to prevent HTML injection.

### Contract and live tests

- Provider contract suite runs against fake adapter on every test run.
- Opt-in live Gemini smoke tests are marked, excluded by default, tightly budget-capped, and use a synthetic non-private short proxy.
- Record SDK/model capability changes without putting API keys or response media in Git.

### Real evaluation

Create a private annotation format for matches and candidate intervals. Evaluate temporal tolerance rather than exact millisecond equality. Include boring sessions to verify legitimate zero-candidate output and high-event sessions to test non-quota behavior.

## 4. Key technical risks and mitigations

| Risk | Impact | Mitigation / validation |
|---|---|---|
| 1 FPS model sampling misses fast gameplay events | High recall loss | Combine audio/activity signals, overlap windows, test low vs higher resolution on annotated clips, and use candidate context. |
| Match UI differs by game/update | Incorrect hierarchy | Generic fallback, confidence/evidence, unassigned group, versioned game profiles. |
| Four-hour context/upload is unreliable | Timeouts, cost, impossible context | Bounded overlapping windows with item checkpoints; never one giant request. |
| Proxy/source timestamps drift, especially VFR | Wrong clips | Integer source timeline, stored transform, synthetic VFR tests, re-probe extracted clips. |
| Precise stream-copy cuts are keyframe-limited | Missing setup/event | Accurate high-quality re-encode default; label copy mode as approximate. |
| Python 3.14 package incompatibility | Install/runtime failures | Standardize on Python 3.12; test optional GPU stack separately. |
| FFmpeg absent or encoder capabilities vary | Pipeline blocked | `doctor`, pinned build guidance, encoder probe, CPU fallback. |
| Provider pricing/model/API changes | Budget errors or breakage | Dated external catalog, max age, capabilities, aliases resolved per run, fail closed. |
| Free-tier data use/privacy differs from paid tier | Personal footage exposure | Explicit consent/config, proxy only, tier documentation, deletion audit, no original upload. |
| Model output is plausible but wrong | Bad/unsafe extraction | Structured output plus semantic validation, correction limits, raw evidence, human final review. |
| Local storage grows without bound | Disk exhaustion | Preflight estimate, size reporting, configurable library location; no automatic deletion in V1. |
| Concurrent processes overspend | Hard-budget violation | SQLite transactional reservations and per-session locks. |
| Reviewer cost adds little value | Wasted budget | Disabled by default; enable only after Scout benchmark and measure incremental lift. |

## 5. Assumptions and unresolved decisions to validate

### Owner decisions before implementation

1. Is uploading a lightweight gameplay proxy (including voice chat) to the selected Gemini tier acceptable? Confirm account/tier data-use terms.
2. Where should the permanent `data/` library live? The current `C:` has ample space, but recordings may be on another drive.
3. Is Python 3.12 installation approved, and should project setup use `uv`, standard `venv`, or another preferred manager? Recommendation: `uv` if acceptable, otherwise `venv` plus a lock tool.
4. Which trusted FFmpeg distribution/install method is preferred? It must expose ffmpeg/ffprobe and NVENC where licensed/supported.
5. Is accurate re-encode acceptable as the default candidate export? Recommendation: yes; keep stream-copy opt-in.
6. Should voice-chat transcription be enabled later, given privacy and possible multilingual Thai/English speech? Recommendation: off initially, then benchmark locally.

### Experiments, not owner guesses

- Proxy bitrate/resolution and whether UI text is readable enough at 480p.
- Low media resolution versus highlight recall for fast motion and small HUD events.
- Optimal Scout window length/overlap and whether signal-driven subdivision improves quality.
- Whether local transcription materially improves funny/reaction detection on this hardware.
- Generic match detection quality for MECCHA CHAMELEON and which UI/audio cues are stable.
- NVENC versus libx264 quality/speed/file size for extracted review clips.
- Real output-token distribution and cost per analyzed hour.
- Candidate score threshold needed to reach 5–20 candidates without quotas.

## 6. Definition of V1 done

V1 is done only after a representative 1–4 hour recording completes through report generation, an interrupted run resumes without repeating paid completed work, the original hash remains unchanged, the cost ledger stays within the hard limit, all candidates are traceable to validated evidence and source intervals, and the owner can find useful clips by reviewing a small fraction of the VOD.

## 7. Exact next action after approval

M3 is implemented and accepted. The next milestone is **M4 — Cost gate + provider contract**. M4 remains approval-gated; do not add cost accounting, provider SDKs, Gemini integration, or later-milestone work as part of M3.
