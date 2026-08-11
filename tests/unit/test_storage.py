from __future__ import annotations

import json
from pathlib import Path

from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.hashing import hash_file


def test_streaming_source_hash(tmp_path: Path) -> None:
    source = tmp_path / "large-ish.bin"
    source.write_bytes((b"0123456789abcdef" * 100_000) + b"end")

    assert hash_file(source) == "e4692dafea2996818e4a0777992c46edda2c1796d521aad08b1bd9aa39fca19d"


def test_atomic_json_write_replaces_complete_document(tmp_path: Path) -> None:
    artifact = tmp_path / "nested" / "artifact.json"
    atomic_write_json(artifact, {"version": 1, "name": "เกม"})
    atomic_write_json(artifact, {"version": 2, "items": [1, 2, 3]})

    assert read_json(artifact) == {"version": 2, "items": [1, 2, 3]}
    assert json.loads(artifact.read_text(encoding="utf-8"))["version"] == 2
    assert list(artifact.parent.glob("*.tmp")) == []
