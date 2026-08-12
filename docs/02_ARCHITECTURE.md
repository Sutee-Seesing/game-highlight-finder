# Architecture

## 1. Architectural style

Use a single-process, modular Python CLI with file-based session artifacts and one SQLite cost ledger. Each pipeline stage has a narrow input/output contract. External systems—FFmpeg, ffprobe, optional transcription, and AI providers—sit behind adapters.

This avoids service orchestration while keeping domain logic independent from subprocesses and SDKs.

```text
CLI
  -> Application services / pipeline runner
      -> Domain models + validation + ranking
      -> Session artifact store (JSON/files)
      -> Stage manifest/cache
      -> Cost gate (SQLite ledger)
      -> Ports
          -> ffprobe / FFmpeg adapter
          -> local signal/transcription adapter
          -> Gemini provider adapter
          -> HTML report renderer
```

## 2. Technology choices

### Runtime and packages

- Python 3.12, 64-bit, in `.venv`. Python 3.14 is installed but is not the V1 target because ML/media packages often lag new CPython releases.
- `typer` for discoverable CLI commands and help; `pydantic` v2 for runtime validation and schema generation; `PyYAML` for configuration; `google-genai` isolated inside the Gemini adapter; `pytest` for tests.
- Standard-library `sqlite3` for the cost ledger. Do not introduce an ORM in V1.
- FFmpeg/ffprobe as pinned external prerequisites. Invoke with argument arrays and capture stdout/stderr; never concatenate user paths into a shell command.
- Optional `faster-whisper` extra, installed only after a Python/CUDA compatibility spike.

Pin direct dependencies and commit a lock file once tooling is selected. Record `ffmpeg -version`, relevant encoders, and package versions in each session's environment snapshot.

### JSON plus SQLite rationale

Session data belongs in versioned JSON because it should be readable, diffable, copyable with the media, and recoverable without a database. The global budget is different: two simultaneous processes must not both see the same remaining allowance and overspend it. SQLite provides an atomic transaction for reservations and reliable monthly queries with no server. Its scope stays restricted to cost events.

## 3. Proposed repository structure

```text
game-highlight-finder/
  README.md
  pyproject.toml
  config.example.yaml
  .env.example
  .gitignore
  docs/
    00_START_HERE.md
    01_PRODUCT_REQUIREMENTS.md
    02_ARCHITECTURE.md
    03_PIPELINE.md
    04_DATA_MODELS.md
    05_COST_STRATEGY.md
    06_IMPLEMENTATION_PLAN.md
  src/game_highlight_finder/
    __init__.py
    cli.py
    config.py
    errors.py
    logging.py
    domain/
      models.py
      time.py
      validation.py
      matching.py
      stories.py
      ranking.py
    pipeline/
      runner.py
      manifest.py
      cache.py
      stages/
        ingest.py
        proxy.py
        local_signals.py
        scout.py
        reconcile.py
        extract.py
        reviewer.py
        report.py
    media/
      ffprobe.py
      ffmpeg.py
      commands.py
    providers/
      base.py
      registry.py
      gemini.py
      fake.py
    cost/
      estimator.py
      pricing.py
      ledger.py
      budget.py
    storage/
      atomic.py
      sessions.py
      schemas.py
    reports/
      renderer.py
      templates/report.html.j2
    game_profiles/
      base.py
      generic.py
      meccha_chameleon.py
      roblox.py
      fps_generic.py
  tests/
    unit/
    integration/
    fixtures/
    golden/
  data/                 # ignored by Git; configurable outside repository
    sessions/
    cost/cost.sqlite3
```

Game-specific profiles initially provide hints and thresholds, not hard-coded perfect detectors. The generic profile must always work.

## 4. Runtime data layout

```text
data/sessions/<session_id>/
  source.json
  config.resolved.json
  environment.json
  manifest.json
  preprocessing.json
  session_map.json
  scout_results.json
  scout/
    raw/fake_response.json
    canonical/scout_result.json
  session_map.json
  reviewer_results.json
  cost.json
  proxy/analysis_proxy.mp4
  audio/analysis_audio.m4a
  signals/activity.json
  transcript/transcript.json
  provider/scout/<window_id>/request.json
  provider/scout/<window_id>/response.raw.json
  provider/scout/<window_id>/response.validated.json
  candidates/<candidate_id>.mp4
  thumbnails/<candidate_id>.jpg
  highlights/best_01.mp4       # optional link/copy; never sole copy
  reports/report.html
  logs/run-<timestamp>.jsonl
  tmp/                         # incomplete writes only
```

`session_id` should be stable and human-readable: UTC/local recording date if available, normalized game slug, and a short source-hash suffix. Candidate filenames may include match/category for convenience, but the stable candidate ID is authoritative.

The source path is referenced, not copied. A source availability check is required before extraction or rerun. Moving a source can be repaired with a future `relink` command after hash verification.

## 5. Media strategy

- Read metadata with `ffprobe -show_format -show_streams -of json`.
- Default proxy target to validate: 854x480 maximum, aspect ratio preserved, constant frame cadence appropriate for upload, low bitrate H.264, mono AAC speech-quality audio, timestamps starting at zero.
- Gemini token cost is driven mainly by duration and API media resolution/sampling—not merely the proxy file bitrate. Configure the provider's low media-resolution mode explicitly and use the proxy mainly for upload size, privacy minimization, and stable timestamps.
- Keep a machine-readable timestamp transform. If ingest start time is not zero, normalize the proxy but retain the mapping back to source milliseconds.
- For clips, default to accurate high-quality re-encode (`libx264` CRF-based fallback; NVENC only after quality/availability validation). Offer a fast stream-copy mode with a warning that cuts are keyframe-dependent.
- Generate thumbnails locally after extraction.

## 6. Provider abstraction

The domain layer must not import a provider SDK. Define an interface along these lines:

```text
ProviderAdapter
  capabilities() -> ProviderCapabilities
  upload(asset, purpose) -> RemoteAsset
  get_asset(remote_id) -> RemoteAsset
  delete_asset(remote_id) -> DeletionResult
  estimate_input(request) -> UsageEstimate
  scout(request, response_schema) -> ProviderResponse[ScoutWindowResult]
  review(request, response_schema) -> ProviderResponse[ReviewBatchResult]
```

The application owns prompts, canonical schemas, retries, cost approval, and normalization. The adapter owns authentication, SDK translation, remote lifecycle, provider error mapping, and usage extraction.

Model IDs are configuration values resolved through a model catalog, not conditionals in pipeline code. The catalog records provider, model ID, capabilities, pricing version/effective date, context constraints, supported media resolution, and deprecation state. Friendly aliases such as `scout-cheap` may resolve to a concrete ID at run start; the resolved ID is then frozen in session artifacts.

For Gemini V1:

- Use the Files API for long proxy uploads; remote objects are temporary and must not be treated as cache artifacts.
- Save the remote ID and expiry, reuse it only while valid, and attempt deletion when the stage completes or is abandoned.
- Use provider-supported structured output, then still apply local semantic validation.
- One video per Scout request.
- Use low media resolution and bounded overlapping windows.

## 7. Configuration and secrets

Resolution order: built-in safe defaults < `config.yaml` < explicitly allowed environment overrides < CLI flags. Persist the fully resolved, redacted configuration and its hash per session.

Suggested groups:

```yaml
storage: {data_dir: "data"}
media:
  proxy: {max_height: 480, video_bitrate_kbps: 600, audio_bitrate_kbps: 64}
  extraction: {mode: "accurate", pre_roll_seconds: 5, post_roll_seconds: 5}
scout:
  provider: "gemini"
  model: "scout-cheap"
  window_minutes: 45
  overlap_seconds: 30
  media_resolution: "low"
  candidate_min_score: 6.5
  max_candidates_per_match: null
reviewer: {enabled: false, provider: "gemini", model: "reviewer-quality"}
cost:
  monthly_budget_thb: 100
  hard_limit: true
  usd_to_thb: 36.0
  estimate_safety_factor: 1.20
  pricing_max_age_days: 30
transcription: {enabled: false, backend: "faster-whisper", model: "small"}
```

Exact media/model defaults require real fixture validation. API keys live in `.env`, which is ignored; `.env.example` contains names only. Logs, request snapshots, errors, and reports must redact values matching known secret keys.

## 8. Security and privacy boundaries

- The original never crosses a provider boundary.
- A dry-run must list which derivative files would be uploaded, their size/duration, provider, retention expectation, and projected cost.
- Reject source paths that resolve to the session output directory to prevent recursive processing.
- Constrain output paths beneath the selected session root and validate resolved paths.
- Validate MIME/container and probe results; extensions are not trusted.
- Cap model response size, collection lengths, reason length, and candidate durations.
- Store no authentication material in artifacts or SQLite.
- Document the selected provider tier's data-use policy before first real upload; free and paid tiers may differ.

## 9. Current external facts to revalidate at implementation

As checked on 2026-08-11, Google's official documentation says Gemini video processing is about 300 tokens/second at default media resolution or 100 tokens/second at low resolution, 1M-context models handle up to about one hour default or three hours low resolution, and default visual sampling is 1 FPS. The Files API documentation says uploaded files are automatically deleted after 48 hours. These are volatile provider facts and belong in a dated catalog/compatibility test, not hard-coded assumptions:

- [Gemini video understanding](https://ai.google.dev/gemini-api/docs/video-understanding)
- [Gemini Files API](https://ai.google.dev/gemini-api/docs/files)
- [Gemini structured outputs](https://ai.google.dev/gemini-api/docs/structured-output)
