# VDO Folder Cleanup Checkpoint

Updated: 2026-09-07 Asia/Bangkok

## Scope

This cleanup is deliberately limited to **VDO / game-highlight-finder** material under `C:\Data`. Lotto and unrelated projects are out of scope and were not intentionally modified.

C1 execution was paused during cleanup. The owner later resumed normal VDO work under a restricted file-scope rule while the Supervisor Cleanup/Scope fix is pending. This document records what moved and what must remain stable; it is not permission to perform further broad reorganization.

## Canonical VDO locations kept in place

These paths are still active and were intentionally **not moved** because current configs, workspaces, ignored artifacts, or historical evidence depend on them:

- `C:\Data\Works\Personal_Projects\Active_Products\game-highlight-finder` — canonical source repo.
- `C:\Data\Works\Personal_Projects\Active_Products\game-highlight-finder-media` — companion media/review worktree.
- `C:\Data\Works\Personal_Projects\Active_Products\game-highlight-finder-private` — historical calibration/private media; about 13 GiB and referenced by ignored/historical artifacts.
- `C:\Data\Works\Personal_Projects\Active_Products\ghf-env-backup-sync` — small Git-backed environment backup/sync area; retained rather than guessing it is stale.

Raw recording sources under `E:\Obs` were not touched.

## External VDO test clutter archived

The following old GHF temp-test outputs were moved out of `C:\Data\Temp` into a dated VDO archive:

Archive root:

`C:\Data\Works\Personal_Projects\Archive\VDO\2026-09-07-pre-cleanup\temp-test-runs\`

Moved:

- `C:\Data\Temp\ghf-v4-tests` -> `...\temp-test-runs\ghf-v4-tests`
- `C:\Data\Temp\ghf-v5-full` -> `...\temp-test-runs\ghf-v5-full`
- `C:\Data\Temp\ghf-v5-post` -> `...\temp-test-runs\ghf-v5-post`
- `C:\Data\Temp\ghf-v5-preflight` -> `...\temp-test-runs\ghf-v5-preflight`

A fresh check showed no remaining top-level `ghf-*` entries under `C:\Data\Temp`.

## Canonical repo root cleanup

Four ignored, reproducible local cache/test directories were removed from the repo top level **without deleting them**. They were moved under the existing ignored `.t` scratch tree:

`C:\Data\Works\Personal_Projects\Active_Products\game-highlight-finder\.t\archive\legacy-local-cache-2026-09-07\`

Moved:

- `.mypy_cache` — about 21.63 MiB
- `.ruff_cache` — about 0.01 MiB
- `.test-tmp` — about 145.87 MiB
- `.uv-cache` — about 108.75 MiB

They were all confirmed ignored by `.gitignore` before moving. `.venv`, `.t`, `data`, `secrets`, source, tests and tracked project files were preserved.

The main repo root is now limited to the actual project structure plus preserved tracked files rather than old tool caches.

## Old sibling pytest basetemp directories

Eight historical empty pytest basetemp directories still exist directly under `Active_Products`:

- `.pytest-final-basetemp`
- `.pytest-full-basetemp`
- `.pytest-full-basetemp2`
- `.pytest-m4-basetemp`
- `.pytest-m4-basetemp2`
- `.pytest-m4-basetemp3`
- `.pytest-m4-basetemp4`
- `pytest-m7-final-0814`

A fresh scan confirmed **0 files in every one** and timestamps from 2026-08-13/14. They are VDO-era test leftovers, but the current Work Laptop mutation boundary only permits writes inside the host-selected `game-highlight-finder` Active Project, so moving/deleting these sibling directories was intentionally not forced through a bypass. A future host-level cleanup with `C:\Data` or `Active_Products` selected as the writable workspace may remove them safely.

## C1 state preserved

Nothing in the C1 evidence chain was deliberately moved or deleted:

- V-C1-A local analysis-window state remains preserved.
- V-C1-B session `2026-08-24_unknown_0db51591365f` remains under `.t/c1-real-media-validation/data-b/...`; ingest + proxy + local-signals are complete.
- V-C1-B provider-free preparation is complete: 17/17 analysis-window proxies exist and the exact aggregate reserve estimate is ฿15.156116.
- V-C1-C provider-free preparation is complete after a clean retry following the owner's interrupted first proxy attempt: ingest/proxy/local-signals completed, 26/26 analysis-window proxies exist, and the exact aggregate reserve estimate is ฿23.114436.
- V-C1-A was recomputed from the current preserved local state and remains 3 windows with aggregate reserve estimate ฿2.514798.
- A/B/C local preparation is complete at 46 logical windows total / aggregate estimated reserve ฿40.785350 if all sources were authorized.
- creator-review templates/config/preflight artifacts under `.t/c1-real-media-validation/` remain preserved.
- GT/history remains untouched.
- provider calls = 0, uploads = 0, new provider spend = ฿0; execution is stopped at the provider boundary pending fresh explicit authorization and hard THB cap.

## Resume / scope rule

The owner has explicitly resumed VDO execution, but **broad filesystem reorganization remains blocked** until the Supervisor Cleanup/Scope fix is ready. Do not mass-rename, move a repo/worktree, move a large folder, run recursive cleanup, or create a new workspace structure. Keep canonical paths fixed.

For continuing C1:

1. re-read `.agent/handoff.json`, `docs/CURRENT_STATE.md`, `docs/NEXT_ACTION.md`, and this file;
2. preserve the current repo/media/private paths and ignored C1 artifacts in place;
3. keep `allow_remote_upload=false` throughout provider-free preparation;
4. preserve the completed A/B/C provider-free preflight evidence in place;
5. remain stopped at the provider boundary until a fresh explicit hard THB cap is authorized;
6. when live validation is authorized, run one source at a time and review each source before advancing to the next;
7. keep any later GPU/media optimization as a separate performance pass so it does not change the active correctness baseline.
