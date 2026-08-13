# Game Highlight Finder

Game Highlight Finder is a local-first CLI for turning long gameplay recordings into a reviewable highlight library. The repository currently implements **Milestone 5: Gemini Scout integration** on top of the accepted M1–M4 foundation.

M3 keeps the M2 source/proxy/signal foundation and adds a versioned `Session -> Match -> Candidate` domain map. A deterministic Fake Scout produces bounded offline response fixtures, preserves raw Scout bytes separately from canonical data, and validates hostile output before assigning local deterministic IDs. Canonical timestamps are integer milliseconds with half-open intervals `[start_ms, end_ms)`.

M3 remains offline and deterministic. M4 adds provider-neutral contracts and a fail-closed cost boundary. M5 adds an explicitly opt-in Gemini adapter while keeping Fake Scout as the default; the accepted live smoke used one deterministic synthetic proxy request, and the automated suite continues to use a transport fake with zero provider calls.

## Windows setup

Requirements:

- Windows 11 (Windows is the supported M2 platform)
- [uv](https://docs.astral.sh/uv/) for Python and dependency management
- Python 3.12 managed by uv
- FFmpeg and ffprobe

The validated setup used Scoop:

```powershell
scoop install uv ffmpeg
uv python install 3.12
uv sync --all-groups
```

The Scoop `ffmpeg` package is hash-verified by Scoop and provides both executables. Normal application execution never installs FFmpeg automatically.

If FFmpeg is installed elsewhere, configure explicit executable paths in `config.yaml`, environment variables, or global CLI options. Run `uv run highlight doctor` to verify the resolved paths and versions.

## Configuration

Copy `config.example.yaml` to `config.yaml` if defaults are not suitable. `config.yaml`, `.env`, the local virtual environment, and runtime `data/` are ignored by Git.

Precedence is:

```text
safe defaults < config file < approved GHF_* environment overrides < CLI overrides
```

Approved environment variables are listed in `.env.example`. The application deliberately does not auto-load `.env`; use your shell or secret manager to load it.

Unknown YAML keys fail validation.

The optional `scout.fixture_path` points to a developer/test JSON response. It is
size-bounded, included in the Scout cache identity, and still passes through the
same hostile-output validator as the built-in fixture.

For Gemini, set `scout.backend: gemini`, provide a user-supplied FX snapshot, and
set `scout.allow_remote_upload: true` only after confirming the account/tier data
use terms. The adapter reads the API key from the environment variable named by
`scout.api_key_env` (default `GEMINI_API_KEY`); the key itself is never stored in
configuration or session artifacts. Only the committed session
`proxy/analysis_proxy.mp4` may cross the provider boundary. `--dry-run` performs
local preflight and cost quoting without uploading anything.

## Commands

```powershell
uv run highlight --help
uv run highlight doctor
uv run highlight config check
uv run highlight analyze "D:\Recordings\game.mp4"
uv run highlight analyze "D:\Recordings\game.mp4" --scout-backend gemini --dry-run
uv run highlight analyze "D:\Recordings\game.mp4" --scout-backend gemini --allow-remote-upload
uv run highlight analyze "D:\Recordings\game.mp4" --stop-after ingest
uv run highlight analyze "D:\Recordings\game.mp4" --stop-after proxy
uv run highlight analyze "D:\Recordings\game.mp4" --stop-after local-signals
uv run highlight analyze "D:\Recordings\game.mp4" --stop-after scout
uv run highlight status <session-id>
uv run highlight cost status
uv run highlight cost report
uv run highlight cost calls
```

Global overrides must appear before the subcommand:

```powershell
uv run highlight --config C:\path\config.yaml doctor
uv run highlight --data-dir D:\HighlightData analyze D:\Recordings\game.mp4 --stop-after ingest
uv run highlight --ffprobe-path C:\tools\ffprobe.exe doctor
```

Expected session artifacts:

```text
data/sessions/<session-id>/
  source.json
  config.resolved.json
  environment.json
  manifest.json
  proxy/
    analysis_proxy.mp4
    metadata.json
  audio/
    analysis_audio.m4a       # omitted when the source has no audio
  signals/
    activity.json
  scout/
    raw/fake_response.json
    raw/gemini_response.json
    raw/gemini_request_meta.json
    raw/gemini_remote_file.json
    canonical/scout_result.json
    cost.json                 # derived from the authoritative SQLite ledger
  session_map.json
  logs/
```

The proxy is an analysis derivative (maximum 854x480 by default, aspect-ratio preserving, H.264/AAC mono), not a publishing clip. `metadata.json` stores the integer-millisecond source/proxy timestamp transform. FFmpeg writes to a temporary run directory, re-probes output, hashes it, and only then commits it. A conservative disk-space check runs before encoding.

M3 is entirely local: Fake Scout makes no cloud uploads, paid requests, network calls, or AI calls. A source with no audio still completes proxy and local-signal stages with a warning and empty audio signals. The canonical map keeps every valid candidate above quality/safety validation; there is no product quota such as “top 5”.

Completed stages are cache-verified and resumable. Changing proxy settings invalidates proxy and dependent local signals while keeping ingest cached; changing logging does not invalidate any semantic stage.

## Development and tests

```powershell
uv sync --all-groups
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

Integration tests generate tiny videos with the locally resolved FFmpeg. No large binary fixtures are committed.

## M3 domain and Fake Scout

- Fake Scout is deterministic development/test infrastructure, not a highlight detector.
- Raw fixture response bytes are retained under `scout/raw/`; validated canonical output and `session_map.json` are separate artifacts.
- Provider IDs are advisory. Canonical `match_...` and `cand_...` IDs are locally derived from semantic inputs and are stable across identical runs.
- Generic and bounded game-profile categories are validated; arbitrary unknown strings are rejected.
- Match hierarchy supports zero candidates, one candidate, or many candidates, including an unknown/unsegmented session.
- M3 canonicalization stores every valid candidate without selecting a best-of list; `best_of_candidate_ids` remains empty for later presentation stages.

## M4 cost gate and provider contract

- The default hard budget is **฿100.00 per month** in the configured `Asia/Bangkok` timezone.
- Quotes use explicit local pricing and FX snapshots, integer micro-THB accounting, and conservative upward rounding.
- Reservations are transactional in `data/cost/ledger.sqlite3`; `RESERVED`, `IN_FLIGHT`, `SETTLED`, `RELEASED`, and `AMBIGUOUS` states are persisted for audit and recovery.
- Unknown providers/models/modes, missing or stale prices/FX, unsupported usage dimensions, malformed/oversized usage counts, missing output rates for non-zero output, ledger failures, and budget overages fail closed.
- An ambiguous post-send outcome remains budget exposure until explicit settlement or evidence-backed release; it is never silently retried.
- A persisted actual-cost overage opens a global cost safety hold; new reservations remain blocked until an explicit owner acknowledgement/reconciliation is recorded.
- M4's generic cost boundary never fetches pricing or FX. M5 adds one dated, exact Gemini pricing entry without enabling automatic refresh.

## M5 Gemini Scout

The M5 adapter targets the exact stable model `gemini-3.5-flash-lite` using the
Google Developer API Standard tier. The dated pricing snapshot uses USD 0.30 per
million input tokens for text/image/video/audio and USD 2.50 per million output
tokens including thinking, verified against Google's official pages on 2026-08-13
(Asia/Bangkok): [pricing](https://ai.google.dev/gemini-api/docs/pricing) and
[model](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite).
The low-resolution estimate follows Google's video guidance of about 66 vision
tokens/second plus 32 audio tokens/second. The Interactions adapter places
`resolution: "low"` on the video content item (not an unsupported generation-level
`media_resolution` field), and fails closed rather than falling back. It parses
current `total_input_tokens`, `total_output_tokens`, `total_thought_tokens`, and
`input_tokens_by_modality` usage first, with strict conflict checks for legacy
aliases. `thinking_level: minimal` is sent to Gemini; `reserved_thinking_tokens`
is only a conservative local cost allowance, not a provider-enforced ceiling.

M5 is one bounded request (maximum 900 seconds by default). It uses the Files API
with `store=false` Interactions, structured JSON output, usage capture, persisted
request/cache fingerprints, cost lifecycle `RESERVED -> IN_FLIGHT ->
SETTLED/AMBIGUOUS`, and explicit remote-file deletion with retry-only cleanup on
resume. Raw provider response, redacted request metadata, remote deletion state,
canonical output, and a derived `cost.json` are stored under the session. A paid
result is reused on a verified cache hit; ambiguous outcomes are never retried
automatically. Long-session windowing, candidate detection, Gemini live acceptance,
and all later milestones remain unimplemented.

## Known M3 limitations

- Local signals are intentionally lightweight: silence intervals, bounded RMS/loudness activity buckets, and an integrated loudness summary. There is no transcription or computer-vision detector yet.
- The game profile is `unknown`; game-specific identification starts in a later milestone.
- Moving a source after ingest is reported as missing; a relink command is future work.
- Cache identity uses a fast path/size/mtime check and a stored authoritative SHA-256. A changed source creates a new session.
- Lock recovery handles dead processes on the same host conservatively; unreadable or remote-host locks require manual inspection.
- Gemini integration is implemented but live acceptance is opt-in and was not run by the offline test suite. Long-session reconciliation, candidate extraction, reports, and reviewer/ranking workflows are future work.
- JSON schema migrations and force-stage controls are future work.

See [docs/00_START_HERE.md](docs/00_START_HERE.md) for the full product plan.
