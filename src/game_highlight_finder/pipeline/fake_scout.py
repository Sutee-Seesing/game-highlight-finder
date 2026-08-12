"""Deterministic, offline Scout fixture provider for M3."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from game_highlight_finder.errors import ValidationError


@dataclass(frozen=True)
class FakeScoutOutput:
    raw_bytes: bytes
    fixture_sha256: str | None
    description: str


class FakeScout:
    """A provider-shaped deterministic fixture source; it never calls a network."""

    def __init__(self, fixture_path: Path | None = None, *, max_bytes: int = 1_048_576) -> None:
        self.fixture_path = fixture_path
        self.max_bytes = max_bytes

    def fixture_sha256(self) -> str | None:
        """Return the fixture identity without invoking or parsing Scout output."""

        if self.fixture_path is None:
            return None
        return self._read_fixture().fixture_sha256

    def generate(self, *, source_duration_ms: int, source_sha256: str) -> FakeScoutOutput:
        if source_duration_ms <= 0:
            raise ValidationError("Fake Scout source duration must be positive")
        if self.fixture_path is not None:
            return self._read_fixture()
        return FakeScoutOutput(
            raw_bytes=build_builtin_response(
                source_duration_ms=source_duration_ms, source_sha256=source_sha256
            ),
            fixture_sha256=None,
            description="builtin-deterministic",
        )

    def _read_fixture(self) -> FakeScoutOutput:
        if self.fixture_path is None:  # pragma: no cover - guarded by callers
            raise ValidationError("Fake Scout fixture path is not configured")
        try:
            size = self.fixture_path.stat().st_size
            if size > self.max_bytes:
                raise ValidationError(
                    f"Fake Scout fixture exceeds the {self.max_bytes} byte safety limit."
                )
            raw = self.fixture_path.read_bytes()
        except ValidationError:
            raise
        except OSError as exc:
            raise ValidationError(
                f"Cannot read Fake Scout fixture: {self.fixture_path}", hint=str(exc)
            ) from exc
        if len(raw) > self.max_bytes:
            raise ValidationError("Fake Scout fixture exceeded the configured byte limit.")
        import hashlib

        return FakeScoutOutput(
            raw_bytes=raw,
            fixture_sha256=hashlib.sha256(raw).hexdigest(),
            description=f"fixture:{self.fixture_path.name}",
        )


def build_builtin_response(*, source_duration_ms: int, source_sha256: str) -> bytes:
    """Return a stable response with a zero-candidate match and multiple candidates."""

    if source_duration_ms == 1:
        return json.dumps(
            {
                "schema_version": 1,
                "source_duration_ms": 1,
                "time_basis": "source_relative",
                "matches": [
                    {
                        "start_ms": 0,
                        "end_ms": 1,
                        "confidence": 0.5,
                        "label": "unsegmented session",
                        "provider_id": "model-match-only",
                        "ordinal": 0,
                        "evidence": [],
                        "candidates": [],
                    }
                ],
                "warnings": ["source duration is too short for a candidate fixture"],
                "metadata": {"backend": "fake", "fixture": "builtin-deterministic"},
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")

    first_match_end = max(1, source_duration_ms // 3)
    second_start = first_match_end
    second_length = max(1, source_duration_ms - second_start)
    second_candidate: dict[str, object] | None
    if second_length == 1:
        first_candidate_start = second_start
        first_candidate_end = source_duration_ms
        second_candidate = None
    else:
        first_candidate_start = second_start + max(0, second_length // 4)
        first_candidate_end = min(
            source_duration_ms, first_candidate_start + max(1, second_length // 3)
        )
        second_candidate_start = min(
            source_duration_ms - 1, first_candidate_start + max(1, second_length // 5)
        )
        second_candidate_end = min(
            source_duration_ms, second_candidate_start + max(1, second_length // 3)
        )
        if second_candidate_end <= second_candidate_start:
            second_candidate = None
        else:
            second_candidate = {
                "start_ms": second_candidate_start,
                "end_ms": second_candidate_end,
                "category": "REACTION",
                "score": 7.2,
                "confidence": 0.74,
                "reason": "A nearby reaction overlaps the main event window.",
                "provider_id": "model-candidate-reaction",
                "evidence": [
                    {
                        "type": "reaction speech hint",
                        "start_ms": second_candidate_start,
                        "end_ms": second_candidate_end,
                        "strength": 0.74,
                        "summary": "A compact reaction hint follows the event.",
                        "source": "fake_scout",
                    }
                ],
            }

    first_candidate: dict[str, object] = {
        "start_ms": first_candidate_start,
        "end_ms": max(first_candidate_start + 1, first_candidate_end),
        "category": "CLUTCH" if source_sha256[0] in "01234567" else "FUNNY",
        "score": 8.4,
        "confidence": 0.86,
        "reason": "A deterministic fake event with compact supporting evidence.",
        "provider_id": "model-candidate-primary",
        "evidence": [
            {
                "type": "local signal active",
                "start_ms": first_candidate_start,
                "end_ms": first_candidate_end,
                "strength": 0.82,
                "summary": "Local activity is present around the simulated event.",
                "source": "fake_scout",
            }
        ],
    }
    candidates: list[dict[str, object]] = [first_candidate]
    if second_candidate is not None:
        candidates.append(second_candidate)
    payload = {
        "schema_version": 1,
        "source_duration_ms": source_duration_ms,
        "time_basis": "source_relative",
        "matches": [
            {
                "start_ms": 0,
                "end_ms": first_match_end,
                "confidence": 0.62,
                "label": "unsegmented opening",
                "provider_id": "model-match-opening",
                "ordinal": 0,
                "evidence": [
                    {
                        "type": "match boundary hint",
                        "start_ms": 0,
                        "end_ms": first_match_end,
                        "strength": 0.62,
                        "summary": "The fake provider marks an initial match with no candidates.",
                        "source": "fake_scout",
                    }
                ],
                "candidates": [],
            },
            {
                "start_ms": second_start,
                "end_ms": source_duration_ms,
                "confidence": 0.78,
                "label": "deterministic fake match",
                "provider_id": "model-match-main",
                "ordinal": 1,
                "evidence": [],
                "candidates": candidates,
            },
        ],
        "warnings": [],
        "metadata": {"backend": "fake", "fixture": "builtin-deterministic"},
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")


FakeScoutProvider = FakeScout
generate_fake_response = build_builtin_response
