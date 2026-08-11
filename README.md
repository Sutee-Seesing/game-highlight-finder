# Game Highlight Finder

Game Highlight Finder is a local-first CLI for turning long gameplay recordings into a reviewable highlight library. The repository currently implements **Milestone 1: Foundation and Ingest** only.

M1 validates a source recording, reads metadata with ffprobe, computes a streaming SHA-256, records validated source/session artifacts, and resumes from a verified cache. It does not copy or modify the source video.

Not implemented yet: proxy generation, local signals/transcription, AI integration, cost accounting, highlight detection, candidate extraction, reports, publishing, or a GUI.

## Windows setup

Requirements:

- Windows 11 (Windows is the supported M1 platform)
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

Approved M1 environment variables are listed in `.env.example`. The application deliberately does not auto-load `.env` in M1; use your shell or secret manager to load it.

Unknown YAML keys fail validation.

## Commands

```powershell
uv run highlight --help
uv run highlight doctor
uv run highlight config check
uv run highlight analyze "D:\Recordings\game.mp4" --stop-after ingest
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
  logs/
```

The original recording remains at its original path and is never copied into the session directory.

## Development and tests

```powershell
uv sync --all-groups
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

Integration tests generate tiny videos with the locally resolved FFmpeg. No large binary fixtures are committed.

## Known M1 limitations

- Only the `ingest` stage exists; `--stop-after` accepts only `ingest`.
- The game profile is `unknown`; game-specific identification starts in a later milestone.
- Moving a source after ingest is reported as missing; a relink command is future work.
- Cache identity uses a fast path/size/mtime check and a stored authoritative SHA-256. A changed source creates a new session.
- Lock recovery handles dead processes on the same host conservatively; unreadable or remote-host locks require manual inspection.
- JSON schema migrations and force-stage controls are future work.

See [docs/00_START_HERE.md](docs/00_START_HERE.md) for the full product plan.
