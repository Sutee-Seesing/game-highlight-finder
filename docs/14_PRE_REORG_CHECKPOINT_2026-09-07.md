# VDO Pre-Reorganization Checkpoint — 2026-09-07

This checkpoint intentionally pauses feature/validation work before another agent reorganizes and cleans the project folders.

## Pause state

- **Do not continue C1 execution until the folder reorganization is finished and paths are re-validated.**
- No paid/provider work is authorized.
- Provider generation calls since the last C1 checkpoint: **0**.
- Provider uploads since the last C1 checkpoint: **0**.
- New provider spend: **฿0**.
- Raw source upload remains forbidden.
- All queued WorkLab delegate tasks were cancelled before this checkpoint.
- No heavy FFmpeg/media task is currently running.

## Git state at pause

- Branch: `checkpoint/vdo-c1-provider-free-20260907`
- Previous durable checkpoint commit before this pause: `0f36e88` (`checkpoint: preserve C1 provider-free progress`).
- This document and the synchronized state/handoff docs should be committed as the pre-reorganization checkpoint before folder moves begin.

## C1 state

### Provider-free implementation gate

- Full local suite is green: **278 passed in 222.38s**.
- Creator Candidate Pack Beta implementation is intact.
- `gemini-scout-window-v20-creator` remains the Creator Scout default.
- Creator labels remain separate from locked GT v2 truth.
- Parallel WorkLab policy is now durable in `docs/13_C1_REAL_MEDIA_VALIDATION_PLAN.md` and `docs/NEXT_ACTION.md`.

Default VDO concurrency after the reorganization:

`1 heavy media worker + 2-3 parallel reasoning/audit agents`

Do not run multiple full-video FFmpeg jobs against the same T-small/OBS storage merely to create parallelism.

### V-C1-A — fast action / competitive

- Existing provider-free preflight remains ready.
- 3 x creator-prompt windows.
- Historical aggregate estimated reserve: **฿2.514798**.
- Provider calls: **0**.
- Uploads: **0**.
- Live authorization: **false**.
- Local analysis-window artifacts are intentionally ignored/local and must be preserved or recreated after any path migration.

### V-C1-B — sandbox / survival / exploration

Source is now selected and locally processed:

- Source: `E:\Obs\2026-08-24 23-42-23.mkv`
- Duration: **4,526.017s (~75m26s)**
- Session ID: `2026-08-24_unknown_0db51591365f`
- Source SHA-256: `0db51591365fce491ba0363e05296742062009a8638091e0104a584c0f2da172`
- Local session root before reorganization: `.t/c1-real-media-validation/data-b/sessions/2026-08-24_unknown_0db51591365f/`
- Ingest: **COMPLETED**
- Proxy: **COMPLETED**
- Local signals: **COMPLETED**
- Durable task: `b801aa31-4d42-42da-901f-354273205cec`
- Runtime: about **22m29s** for ingest + proxy + local-signals.
- Provider calls/uploads/spend: **0 / 0 / ฿0**.

A later attempt to enter the M6 `windows` CLI path was **blocked intentionally** by the configuration safety guard because Gemini M6 requires `--allow-remote-upload`:

- Task: `48bc7aee-c2c5-4f4e-8aac-1209c1794caf`
- Result: exit 2, `[FAIL] configuration: M6 Gemini requires --allow-remote-upload.`
- We did **not** bypass this guard and did **not** enable remote upload.
- Therefore B window materialization / exact cost preflight is still pending.

### V-C1-C — co-op / social / personality-heavy

Source is selected but its full local pipeline has **not** been started:

- Source: `E:\Obs\2026-08-26 23-17-37.mkv`
- Duration: **6,853.967s (~114m14s)**
- Selection evidence: strong real Discord activity in early/mid/late samples, including a mid-session interval with active Discord while local mic is nearly silent.
- Provider calls/uploads/spend: **0 / 0 / ฿0**.

Do not start C until the folder reorganization is complete and the new canonical scratch/data paths are decided.

## Local-only artifacts that matter during cleanup

The following are intentionally ignored by Git and are therefore **not protected by the repository checkpoint**. The reorganization agent must either preserve them, deliberately migrate them, or document that they will be regenerated:

- `.t/c1-real-media-validation/`
  - `SOURCE_SELECTION_BC.json`
  - `C1_A_PREFLIGHT.json`
  - `CREATOR_REVIEW_TEMPLATE.csv`
  - `CREATOR_MISS_TEMPLATE.csv`
  - `c1-a-fast-action-preflight.yaml`
  - `c1-b-quiet-exploration-preflight.yaml`
  - `c1-c-social-voice-preflight.yaml`
  - `c1_source_preflight.py`
  - `data-b/` including the completed V-C1-B ingest/proxy/signals session
- `.t/c1-creator-pack-demo/`
- historical ignored `data/` / analysis-window artifacts used by V-C1-A, where present

Raw OBS sources are external to the repo and must not be moved or deleted by project cleanup unless the owner explicitly asks:

- `E:\Obs\2026-08-24 23-42-23.mkv`
- `E:\Obs\2026-08-26 23-17-37.mkv`

## Folder-reorganization guardrails

The cleanup/reorganization agent may reorganize project folders, but should preserve behavior and history:

1. Do not delete or rewrite GT v2 history.
2. Do not delete raw OBS recordings.
3. Do not make provider calls or uploads during cleanup.
4. Do not change Scout/ranking semantics merely as part of a folder cleanup.
5. Keep local scratch/media clearly separated from tracked source/docs.
6. If moving ignored local artifacts, record old -> new paths explicitly.
7. Update config paths, docs, tests, and `.agent/handoff.json` together if canonical directories change.
8. After the move, run path/config checks before resuming B/C media work.
9. Do not resume paid inference until a fresh explicit hard THB authorization is given.

## Resume point after reorganization

After the new folder structure is stable:

1. Re-read this checkpoint plus `.agent/handoff.json`, `docs/CURRENT_STATE.md`, and `docs/NEXT_ACTION.md`.
2. Verify Git state and all new canonical paths.
3. Verify the V-C1-B completed local artifacts survived or regenerate only the missing local stages.
4. Resolve a provider-free way to materialize B Scout windows without weakening the `allow_remote_upload=false` safety boundary; do not bypass the guard casually.
5. Produce B exact window count + cost preflight.
6. Then run V-C1-C ingest/proxy/local-signals as the single heavy media worker while parallel read-only audit lanes handle non-contentious work.
7. Stop again at the explicit provider-authorization boundary.
