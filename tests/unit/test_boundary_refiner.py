from __future__ import annotations

from pathlib import Path

import pytest

from game_highlight_finder.domain.models import Candidate, model_json
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.pipeline.boundary_refinement import (
    BoundaryRefinementMediaArtifact,
    BoundaryRefinementMediaResult,
    plan_boundary_refinement,
)
from game_highlight_finder.pipeline.boundary_refiner import (
    FakeBoundaryRefiner,
    build_boundary_refinement_request,
    run_fake_boundary_refinement,
)
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.hashing import hash_file


def _candidate(*, reason: str = "Scout anchor") -> Candidate:
    return Candidate(
        candidate_id="cand_0123456789abcdef",
        category="SKILL",
        event_start_ms=500,
        event_end_ms=1_200,
        score=7.0,
        confidence=0.9,
        reason=reason,
    )


def _media(tmp_path: Path, candidate: Candidate) -> BoundaryRefinementMediaResult:
    item_dir = tmp_path / "session" / "scout" / "boundary_refinement" / candidate.candidate_id
    item_dir.mkdir(parents=True)
    context_path = item_dir / "context.mp4"
    slowed_path = item_dir / "slowed.mp4"
    artifact_path = item_dir / "artifact.json"
    context_path.write_bytes(b"context-bytes")
    slowed_path.write_bytes(b"slowed-bytes")
    plan = plan_boundary_refinement(
        candidate,
        2_000,
        pre_context_ms=250,
        post_context_ms=300,
        slowdown_factor=2,
    )
    artifact = BoundaryRefinementMediaArtifact(
        plan=plan,
        parent_proxy_sha256="a" * 64,
        context_proxy_path="scout/boundary_refinement/cand_0123456789abcdef/context.mp4",
        context_proxy_sha256=hash_file(context_path),
        slowed_proxy_path="scout/boundary_refinement/cand_0123456789abcdef/slowed.mp4",
        slowed_proxy_sha256=hash_file(slowed_path),
        encoder="libx264",
        hardware_accelerated=False,
        audio_present=True,
        context_duration_ms=1_250,
        slowed_proxy_duration_ms=2_500,
    )
    atomic_write_json(artifact_path, model_json(artifact))
    return BoundaryRefinementMediaResult(
        cache_hit=False,
        artifact_path=artifact_path,
        context_path=context_path,
        slowed_proxy_path=slowed_path,
        artifact=artifact,
    )


def _response() -> dict[str, object]:
    return {
        "status": "REFINED",
        "event_start_ms": 400,
        "event_end_ms": 2_100,
        "confidence": 0.9,
        "reason": "same synthetic event with tighter boundaries",
    }


def test_fake_boundary_refiner_round_trip_maps_back_and_caches(tmp_path: Path) -> None:
    candidate = _candidate()
    media = _media(tmp_path, candidate)
    fake = FakeBoundaryRefiner(_response())

    first = run_fake_boundary_refinement(media, candidate, fake)

    assert first.cache_hit is False
    assert len(fake.calls) == 1
    assert (first.candidate.event_start_ms, first.candidate.event_end_ms) == (450, 1_300)
    assert first.candidate.metadata["boundary_refinement"] == "boundary-refiner-v1"
    assert first.request_path.is_file()
    assert first.response_path.is_file()
    assert read_json(first.response_path)["backend"] == "fake"

    unused_fake = FakeBoundaryRefiner(
        {
            "status": "UNCERTAIN",
            "event_start_ms": 500,
            "event_end_ms": 1_500,
            "confidence": 0.1,
            "reason": "must not be called on a valid cache hit",
        }
    )
    second = run_fake_boundary_refinement(media, candidate, unused_fake)
    assert second.cache_hit is True
    assert unused_fake.calls == []
    assert second.candidate == first.candidate


def test_request_fingerprint_changes_when_candidate_semantics_change(tmp_path: Path) -> None:
    candidate = _candidate(reason="first reason")
    media = _media(tmp_path, candidate)
    first = build_boundary_refinement_request(media, candidate)
    changed = candidate.model_copy(update={"reason": "different reason"})
    second = build_boundary_refinement_request(media, changed)

    assert first.candidate_sha256 != second.candidate_sha256
    assert first.request_fingerprint != second.request_fingerprint


def test_fake_boundary_refiner_rejects_out_of_bounds_response(tmp_path: Path) -> None:
    candidate = _candidate()
    media = _media(tmp_path, candidate)
    fake = FakeBoundaryRefiner(
        {
            "status": "REFINED",
            "event_start_ms": 100,
            "event_end_ms": media.artifact.plan.proxy_duration_ms + 1,
            "confidence": 0.9,
            "reason": "invalid duration",
        }
    )

    with pytest.raises(ValidationError, match="exceeds"):
        run_fake_boundary_refinement(media, candidate, fake)

    assert len(fake.calls) == 1
    assert not (media.artifact_path.parent / "response.fake.json").exists()


def test_request_rejects_tampered_slowed_proxy_before_fake_call(tmp_path: Path) -> None:
    candidate = _candidate()
    media = _media(tmp_path, candidate)
    media.slowed_proxy_path.write_bytes(b"tampered")
    fake = FakeBoundaryRefiner(_response())

    with pytest.raises(ValidationError, match="hash"):
        run_fake_boundary_refinement(media, candidate, fake)

    assert fake.calls == []
