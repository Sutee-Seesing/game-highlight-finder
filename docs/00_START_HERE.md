# Game Highlight Finder — Start Here

Status: Milestone 6 long-session reconciliation and extraction complete; live windowed Gemini acceptance not run
Plan date: 2026-08-11  
Working project path: `C:\Data\Works\Personal_Projects\Active_Products\game-highlight-finder`

## Repository reference

The requested upstream reference is [github.com/Sutee-Seesing/game-highlight-finder](https://github.com/Sutee-Seesing/game-highlight-finder). It was initially empty when checked on 2026-08-11. This planning baseline has now been committed and pushed to `main` as `88a2795`; the working folder is the local checkout of that repository. Future implementation should preserve this documentation baseline and update it when decisions change.

## What we are building

Game Highlight Finder is a local-first CLI that turns a long gameplay recording into a permanent, match-aware library of reviewable highlight clips. The original recording stays local and unchanged. Local tools create metadata, signals, and a lightweight proxy; a low-cost multimodal model scouts that proxy; FFmpeg then extracts high-quality candidate clips from the original.

The product optimizes for reliability, cost control, resumability, and reducing human review time. It does not automatically publish or make the final editorial decision.

## Environment audit

| Item | Finding | Consequence |
|---|---|---|
| OS | Windows 11, 64-bit (`10.0.26200`) | Windows paths and process handling must be first-class and tested. |
| CPU | AMD Ryzen 7 3700X, 8 cores / 16 logical processors | Adequate for proxying and CPU fallback transcription; sustained encodes may be slow. |
| RAM | 31.9 GB total; about 10.5 GB available during audit | Adequate for the proposed pipeline; stages should stream files rather than load videos into memory. |
| GPU | NVIDIA GeForce RTX 4070, 12,282 MiB VRAM; driver `610.74` | Strong candidate for NVENC proxy/export and optional local Whisper acceleration. |
| Storage | `C:` NTFS, 1,906.7 GB total, 1,128 GB free | Healthy now, but session storage needs preflight estimates and retention visibility. |
| Python | Global CPython 3.14.6 remains installed; uv now manages CPython 3.12.13 in project-local `.venv` | M1 runs on its supported interpreter without relying on global Python. |
| FFmpeg / ffprobe | FFmpeg/ffprobe 9.0 installed through Scoop's hash-verified `ffmpeg` package | M1 doctor and integration tests resolve both tools; explicit configured paths are also supported. |
| Git | 2.55.0.windows.2 | Ready. The workspace root is not itself a repository. |
| Useful installed Python packages | Pydantic 2.13.4, Click 8.4.2, PyYAML 6.0.3, pytest 8.4.2 | These system packages should not be relied on; dependencies will be pinned in the project environment. |
| AI/transcription packages | `google-genai`, `faster-whisper`, and `torch` were not detected | Expected at this stage; install only in the project environment and keep transcription optional. |

Notes:

- Hardware discovery was read-only. Windows CIM access was denied, so CPU data came from the registry, memory/storage from .NET, and GPU data from `nvidia-smi`.
- No existing Game Highlight Finder directory was found, so this documentation creates a new planning-only project directory.
- No secrets were inspected. Existing environment files elsewhere in the workspace are unrelated and must not be reused.

## Architecture decision summary

1. Use a modular Python 3.12 CLI application—not services, Docker, or a GUI.
2. Keep originals outside the session library and never modify them.
3. Store session artifacts as versioned JSON for inspectability and easy recovery.
4. Use a small SQLite database only for the global cost ledger and transactional budget reservations.
5. Represent time internally as integer milliseconds; display timecodes only at boundaries.
6. Split long VODs into overlapping Scout windows, validate each response, then reconcile matches and candidates deterministically.
7. Make every stage content-addressed by source, configuration, code/schema, and dependency hashes.
8. Treat provider output as hostile input: schema validation, semantic checks, duration clamping, deduplication, and explicit warnings.
9. Make the optional Reviewer consume only extracted candidates, never the full recording.
10. Fail closed before paid calls when price data is missing/stale or the hard THB budget would be exceeded.

## Implemented M1 foundation

The first implementation slice is complete:

> Project skeleton + `doctor` + config loading/validation + session creation + ffprobe ingest + atomic stage manifest.

The first usable command set should be:

```text
highlight doctor
highlight analyze <video> --stop-after ingest
highlight status <session-id>
highlight config check
```

This slice proves Windows/Unicode paths, dependency discovery, immutable source handling, stable session IDs, validated metadata, hashing, atomic writes, locking, cache hits, interrupted-run recovery, and status reporting.

## Implemented M2 local foundation

The current CLI extends the accepted M1 ingest with local-only proxy and signal stages:

```text
highlight analyze <video> --stop-after ingest
highlight analyze <video> --stop-after proxy
highlight analyze <video> --stop-after local-signals
highlight status <session-id>
```

M2 generates an aspect-ratio-preserving analysis proxy, optional mono analysis audio, a versioned source/proxy timestamp mapping, and bounded silence/RMS/loudness metadata. Outputs are re-probed, hashed, atomically committed, and cached by stage-specific configuration plus external-tool identity. Existing M1 manifests are upgraded additively when an M2 command opens them. No cloud upload, AI provider, transcription, candidate detection, or cost-ledger code is part of M2.

## Implemented M3 canonical domain and Fake Scout

The current CLI can continue through an offline deterministic Scout:

```text
highlight analyze <video> --stop-after scout
highlight status <session-id>
```

M3 adds bounded Pydantic Scout contracts, canonical `Session -> Match -> Candidate` models, controlled categories, distinct score/confidence semantics, compact evidence, deterministic local IDs, source-relative integer-millisecond normalization, and immutable raw-versus-canonical Scout artifacts. The Fake Scout demonstrates zero-candidate matches and multiple/overlapping candidates without network access, API keys, paid requests, or provider SDKs. Existing M1/M2 manifests are upgraded additively; changing Scout fixtures/configuration invalidates only the Scout stage.

## Documentation map

- [01_PRODUCT_REQUIREMENTS.md](01_PRODUCT_REQUIREMENTS.md): scope, behavior, constraints, and success measures.
- [02_ARCHITECTURE.md](02_ARCHITECTURE.md): components, technology choices, directory structure, and security boundaries.
- [03_PIPELINE.md](03_PIPELINE.md): stages, state machine, cache invalidation, resume, and failure semantics.
- [04_DATA_MODELS.md](04_DATA_MODELS.md): canonical models, identifiers, timestamps, and validation rules.
- [05_COST_STRATEGY.md](05_COST_STRATEGY.md): estimation, ledger, reservations, exchange rate, and hard-budget algorithm.
- [06_IMPLEMENTATION_PLAN.md](06_IMPLEMENTATION_PLAN.md): milestones, tests, validation experiments, risks, and decisions.

## Implemented M4 cost boundary

M4 adds provider-neutral capabilities/registry contracts, exact versioned pricing
and FX snapshot models, Decimal-based conservative quotes, and a durable SQLite
ledger at `data/cost/ledger.sqlite3`. The default hard cap is ฿100.00 per month
using the configured `Asia/Bangkok` budget timezone. Reservations use integer
micro-THB values and `BEGIN IMMEDIATE`; ambiguous calls remain counted until
explicit reconciliation. Untrusted usage counts are bounded, missing output
rates fail closed, and a settled overage opens a persisted global safety hold
until explicit acknowledgement. M4 performs no provider, AI, or network calls.
M5 adds the exact production Gemini pricing snapshot only inside the explicitly
selected Gemini pipeline.

## M5 Gemini Scout (accepted)

M5 adds one bounded, explicitly opt-in Gemini request after the local ingest,
proxy, and signal stages. The exact model is `gemini-3.5-flash-lite` on Google's
Standard tier. The adapter uploads only the committed session analysis proxy,
uses low media resolution on the video content item and structured JSON output
through `store=false` Interactions, captures current total/modality usage plus
separate visible/thinking counts, reserves before upload, and
persists the complete cost lifecycle. Remote file metadata excludes the URI;
deletion is retried without regenerating a paid request. Completed paid responses
are cacheable by a semantic provider fingerprint, while ambiguous outcomes remain
unresolved until explicit ledger reconciliation. M5 intentionally does not add
long-session windows, candidate extraction, or any M6 work. Live acceptance was
completed on 2026-08-13 with one deterministic synthetic 8-second proxy request
to `gemini-3.5-flash-lite` using low video resolution, `thinking_level=minimal`,
and `store=false`. Remote cleanup finished as `deletion_status=deleted`; the
list-rate-equivalent reservation/settlement were ฿0.331074/฿0.021643. The
identical cache rerun performed zero generation calls and zero new reservations.

## Current publication and acceptance state

M1–M5 implementation and the authorized M5 live acceptance are published. The
default backend remains Fake Scout and remote upload remains opt-in. Before any
future live run, confirm:

- Whether cloud-uploaded proxy data is acceptable under the chosen Gemini account/tier and data-use terms.
- A valid user-managed `GEMINI_API_KEY` and an explicit FX snapshot.
- A short, synthetic/non-private proxy and the configured M5 duration/budget limits.

## M6 long-session reconciliation and extraction

M6 is implemented as an explicit offline `highlight analyze --m6` flow. It plans
deterministic source-relative windows bounded to 900 seconds with 30 seconds of
overlap by default, derives every window proxy only from the committed analysis
proxy, intersects bounded local-signal hints, and persists strict per-window
lineage plus raw/canonical responses. Fake Window Scout is the implementation
and acceptance harness; M6 live windowed Gemini acceptance was not run.

Window timestamps are returned relative to each window and converted exactly
once to the canonical source timeline. Reconciliation conservatively stitches
compatible match fragments, records conflicts, deduplicates same-category
candidate fragments across overlap lineage, derives bounded clip context, and
persists diagnostics. Accurate source re-encode is the extraction default;
stream-copy is opt-in and marked keyframe-approximate. Outputs and thumbnails
are re-probed, hashed, atomically committed, and tracked per candidate in a
restart-safe extraction manifest. Source identity is rechecked before cutting.

All M6 implementation tests and smoke validation used local FFmpeg/ffprobe,
synthetic media, and Fake Scout. Real Gemini API calls: ZERO. M7 is not started.
