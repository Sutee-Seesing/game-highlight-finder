"""Read-only VDO chat-handoff sanity check.

Run from any directory:
    python scripts/handoff_check.py

This script never edits the repository. It verifies the durable handoff files and
prints compact live Git/artifact state for a new conversation to consume before
continuing work.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HANDOFF_PATH = ROOT / ".agent" / "handoff.json"
REQUIRED_FILES = [
    ROOT / "docs" / "CURRENT_STATE.md",
    ROOT / "docs" / "NEXT_ACTION.md",
    ROOT / "docs" / "DECISIONS.md",
    ROOT / "docs" / "09_CHAT_HANDOFF_PROTOCOL.md",
]


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        return f"ERROR({completed.returncode}): {message}"
    return completed.stdout.strip()


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    if not HANDOFF_PATH.is_file():
        fail(f"missing {HANDOFF_PATH}")

    try:
        handoff = json.loads(HANDOFF_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"invalid handoff JSON: {exc}")

    if handoff.get("schema_version") != 1:
        fail(f"unsupported schema_version={handoff.get('schema_version')!r}")

    missing = [str(path) for path in REQUIRED_FILES if not path.is_file()]
    if missing:
        fail("missing checkpoint file(s): " + ", ".join(missing))

    canonical = Path(handoff["canonical_repo"]["path"])
    if not canonical.is_dir():
        fail(f"canonical repo missing: {canonical}")

    review_pack = handoff.get("review_pack", {})
    review_root = Path(review_pack.get("root", ""))
    queue_name = review_pack.get("queue")
    if review_root and not review_root.is_dir():
        fail(f"review pack root missing: {review_root}")
    if queue_name and not (review_root / queue_name).is_file():
        fail(f"review queue missing: {review_root / queue_name}")

    candidates = review_pack.get("candidates", [])
    print(f"HANDOFF_OK schema=1 updated_at={handoff.get('updated_at')}")
    print(f"GOAL: {handoff.get('current_goal')}")
    print(f"NEXT: {handoff.get('next_action')}")
    print(f"CANDIDATES: {len(candidates)} provider_calls_recorded={review_pack.get('provider_calls_recorded')}")
    print(f"CANONICAL_HEAD: {git(canonical, 'rev-parse', 'HEAD')}")
    print("CANONICAL_STATUS:")
    print(git(canonical, "status", "--short", "--branch"))

    for companion in handoff.get("companion_repos", []):
        path = Path(companion["path"])
        print(f"COMPANION: {companion.get('role')} :: {path}")
        if path.is_dir():
            print(f"COMPANION_HEAD: {git(path, 'rev-parse', 'HEAD')}")
            print("COMPANION_STATUS:")
            print(git(path, "status", "--short", "--branch"))
        else:
            print("COMPANION_STATUS: MISSING")


if __name__ == "__main__":
    main()
