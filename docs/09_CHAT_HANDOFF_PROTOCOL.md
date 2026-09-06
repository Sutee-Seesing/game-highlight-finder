# Chat Handoff Protocol

Purpose: make VDO development resilient to ChatGPT conversation-length limits and tool-session interruptions.

## Resume contract

When the user opens a new conversation in the `vdo` project and says `ต่อ VDO` (or equivalent), use this order:

1. Read `.agent/handoff.json`.
2. Read `docs/CURRENT_STATE.md`.
3. Read `docs/NEXT_ACTION.md`.
4. Run `python scripts/handoff_check.py` for a compact read-only sanity check of required checkpoint files, review-pack presence, and live Git state.
5. Inspect live git status/HEAD for every repo/worktree listed in the handoff if the sanity check reports anything unexpected.
6. If the handoff lists active durable task IDs, inspect those tasks before restarting anything.
7. Inspect only the specific historical plan/result files referenced by the checkpoint when additional rationale is needed.
8. Continue the recorded next action. Do not ask the user to paste the previous chat unless the durable checkpoint is missing/corrupt or the live repository materially contradicts it.

Live repository/task/provider-ledger evidence wins over stale prose.

## Checkpoint timing

Refresh the checkpoint:

- after each meaningful milestone or experiment;
- after a provider call settles/fails/turns ambiguous;
- whenever a durable background task is launched or completed;
- before a deliberate conversation rollover;
- before a long sequence likely to generate a large transcript;
- immediately after discovering a new blocker that changes the next action.

## Required checkpoint content

`docs/CURRENT_STATE.md` should stay concise and contain:

- current goal/milestone;
- verified completed work;
- current blocker;
- provider/cost state relevant to the next action;
- branch/HEAD and important dirty-work warnings;
- private artifact locations needed to resume;
- what must not be repeated.

`docs/NEXT_ACTION.md` should contain one primary objective with ordered steps, stop conditions, and completion criteria.

`.agent/handoff.json` should mirror the minimum machine-readable form of the same state and include durable task IDs when any are active.

`docs/DECISIONS.md` stores decisions that should remain valid beyond a single task.

## Transcript discipline

The chat is a controller/decision surface, not a log store.

For long operations:

- run one durable background process/task;
- persist `task_id`, output/log/result path, and intended acceptance criteria;
- poll sparingly;
- write large logs to disk;
- summarize only meaningful changes in chat;
- on timeout/reconnect, inspect existing task/artifacts before retrying.

Never repeat a paid provider request solely because a chat/tool response timed out.

## Git discipline

Before checkpoint maintenance, inspect status. Never blanket-clean active worktrees. If checkpoint files are committed, stage only the reviewed checkpoint/protocol files and verify no unrelated local work enters the commit.

If the current branch is significantly behind its upstream while local work is dirty, preserve the checkpoint locally first and reconcile branch history deliberately rather than pulling/rebasing as an incidental handoff step.

## Privacy and benchmark discipline

Private media, annotations, predictions, ledgers, source paths, and review packs that are intentionally ignored/untracked must remain local. The handoff may point to their local paths but must not copy private media into tracked documentation.

## Recovery when checkpoint and chat disagree

1. Prefer live filesystem/git/task/ledger evidence.
2. Mark stale checkpoint claims as superseded.
3. Update all checkpoint files in the same milestone.
4. Record important changed assumptions in `docs/DECISIONS.md`.
5. Continue from the corrected state without reconstructing the entire old conversation.
