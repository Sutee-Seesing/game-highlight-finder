# Game Highlight Finder

Game Highlight Finder is a local-first CLI for turning long gameplay recordings into a reviewable highlight library. The repository currently implements **Milestone 3: Canonical Domain + Fake Scout**.

M3 keeps the M2 source/proxy/signal foundation and adds a versioned `Session -> Match -> Candidate` domain map. A deterministic Fake Scout produces bounded offline response fixtures, preserves raw Scout bytes separately from canonical data, and validates hostile output before assigning local deterministic IDs. Canonical timestamps are integer milliseconds with half-open intervals `[start_ms, end_ms)`.

M3 does not call a real AI provider, use network access, require an API key, extract clips, or publish anything. Gemini/provider contracts and cost controls remain future M4/M5 work.

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

## Commands

```powershell
uv run highlight --help
uv run highlight doctor
uv run highlight config check
uv run highlight analyze "D:\Recordings\game.mp4"
uv run highlight analyze "D:\Recordings\game.mp4" --stop-after ingest
uv run highlight analyze "D:\Recordings\game.mp4" --stop-after proxy
uv run highlight analyze "D:\Recordings\game.mp4" --stop-after local-signals
uv run highlight analyze "D:\Recordings\game.mp4" --stop-after scout
uv run highlight status <session-id>
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
    canonical/scout_result.json
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

## Known M3 limitations

- Local signals are intentionally lightweight: silence intervals, bounded RMS/loudness activity buckets, and an integrated loudness summary. There is no transcription or computer-vision detector yet.
- The game profile is `unknown`; game-specific identification starts in a later milestone.
- Moving a source after ingest is reported as missing; a relink command is future work.
- Cache identity uses a fast path/size/mtime check and a stored authoritative SHA-256. A changed source creates a new session.
- Lock recovery handles dead processes on the same host conservatively; unreadable or remote-host locks require manual inspection.
- Real provider calls, cost accounting, long-session reconciliation, candidate extraction, reports, and reviewer/ranking workflows are future work.
- JSON schema migrations and force-stage controls are future work.

See [docs/00_START_HERE.md](docs/00_START_HERE.md) for the full product plan.
