# Game Highlight Finder — Start Here

Status: planning complete; implementation intentionally not started  
Plan date: 2026-08-11  
Working project path: `C:\Data\Works\Personal_Projects\Active_Products\game-highlight-finder`

## Repository reference

The requested upstream reference is [github.com/Sutee-Seesing/game-highlight-finder](https://github.com/Sutee-Seesing/game-highlight-finder). It was checked on 2026-08-11 with `git ls-remote` through an approved network path; the command succeeded but returned no refs or commits. Treat the repository as an empty remote at this point. The planning files below are therefore the initial project baseline, not a review of existing implementation. Once a branch/commit is published, re-audit it before scaffolding M1 and preserve any intentional repository conventions.

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
| Python | CPython 3.14.6 only; pip 26.1.2 | Do not build on the system interpreter. Use a project-local Python 3.12 virtual environment for library compatibility. |
| FFmpeg / ffprobe | Not found on `PATH` | Blocking prerequisite for video work. Install a pinned build and validate required codecs/filters. |
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

## Recommended first implementation

Implement one thin, fully tested vertical foundation before any AI integration:

> Project skeleton + `doctor` + config loading/validation + session creation + ffprobe ingest + atomic stage manifest.

The first usable command set should be:

```text
highlight doctor
highlight analyze <video> --stop-after ingest
highlight status <session-id>
highlight config check
```

This slice proves Windows paths, dependency discovery, immutable source handling, stable session IDs, JSON schemas, hashing, atomic writes, and restart behavior. Those are dependencies of every later stage. Do not start Gemini integration until this slice passes tests against at least one short fixture video.

## Documentation map

- [01_PRODUCT_REQUIREMENTS.md](01_PRODUCT_REQUIREMENTS.md): scope, behavior, constraints, and success measures.
- [02_ARCHITECTURE.md](02_ARCHITECTURE.md): components, technology choices, directory structure, and security boundaries.
- [03_PIPELINE.md](03_PIPELINE.md): stages, state machine, cache invalidation, resume, and failure semantics.
- [04_DATA_MODELS.md](04_DATA_MODELS.md): canonical models, identifiers, timestamps, and validation rules.
- [05_COST_STRATEGY.md](05_COST_STRATEGY.md): estimation, ledger, reservations, exchange rate, and hard-budget algorithm.
- [06_IMPLEMENTATION_PLAN.md](06_IMPLEMENTATION_PLAN.md): milestones, tests, validation experiments, risks, and decisions.

## Approval gate

No major implementation should begin until the owner approves this plan, especially these decisions:

- Python 3.12 as the supported runtime.
- Installation method and license/source for FFmpeg.
- Whether cloud-uploaded proxy data is acceptable under the chosen Gemini account/tier and data-use terms.
- Initial Scout model alias and validated price entry at implementation time.
- Default extraction mode: accurate high-quality re-encode versus faster keyframe-aligned stream copy.
