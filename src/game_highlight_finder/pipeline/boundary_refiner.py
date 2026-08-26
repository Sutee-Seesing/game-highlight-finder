"""Provider-free boundary-refiner request contract and deterministic fake execution."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from game_highlight_finder.domain.models import Candidate, Sha256, model_json
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.pipeline.boundary_refinement import (
    BOUNDARY_REFINEMENT_VERSION,
    BoundaryRefinementMediaResult,
    BoundaryRefinementPlan,
    BoundaryRefinementResponse,
    apply_boundary_refinement,
    boundary_refinement_schema,
    build_boundary_refinement_prompt,
)
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.hashing import hash_file


class BoundaryRefinementRequestArtifact(BaseModel):
    """Canonical local request prepared for a future provider adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    version: str = BOUNDARY_REFINEMENT_VERSION
    candidate_id: str = Field(pattern=r"^cand_[0-9a-f]{16}$")
    candidate_sha256: Sha256
    media_path: str = Field(min_length=1, max_length=1000)
    media_sha256: Sha256
    prompt: str = Field(min_length=1, max_length=20_000)
    response_schema: dict[str, Any]
    plan: BoundaryRefinementPlan

    @property
    def request_fingerprint(self) -> str:
        return _canonical_sha256(self.model_dump(mode="json"))


class BoundaryRefinementFakeResponseArtifact(BaseModel):
    """Persisted fake-only response; never valid as evidence of a paid provider call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    backend: Literal["fake"] = "fake"
    request_fingerprint: Sha256
    response: BoundaryRefinementResponse


class BoundaryRefinementFakeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    cache_hit: bool
    request_path: Path
    response_path: Path
    request: BoundaryRefinementRequestArtifact
    response: BoundaryRefinementResponse
    candidate: Candidate


class FakeBoundaryRefiner:
    """Deterministic local fixture that makes no network or provider call."""

    def __init__(self, response: Mapping[str, Any] | str | bytes) -> None:
        self.response = response
        self.calls: list[str] = []

    def generate(
        self, request: BoundaryRefinementRequestArtifact
    ) -> Mapping[str, Any] | str | bytes:
        self.calls.append(request.request_fingerprint)
        return self.response


def build_boundary_refinement_request(
    media: BoundaryRefinementMediaResult,
    candidate: Candidate,
) -> BoundaryRefinementRequestArtifact:
    """Build a canonical request only from validated committed local artifacts."""

    plan = media.artifact.plan
    if plan.candidate_id != candidate.candidate_id:
        raise ValidationError("boundary refiner media belongs to a different candidate")
    if (
        plan.anchor_start_ms != candidate.event_start_ms
        or plan.anchor_end_ms != candidate.event_end_ms
    ):
        raise ValidationError(
            "boundary refiner candidate no longer matches the prepared Scout anchor"
        )
    if not media.slowed_proxy_path.is_file():
        raise ValidationError("boundary refiner slowed proxy is missing")
    if hash_file(media.slowed_proxy_path) != media.artifact.slowed_proxy_sha256:
        raise ValidationError("boundary refiner slowed proxy hash does not match provenance")

    return BoundaryRefinementRequestArtifact(
        candidate_id=candidate.candidate_id,
        candidate_sha256=_canonical_sha256(candidate.model_dump(mode="json")),
        media_path=media.artifact.slowed_proxy_path,
        media_sha256=media.artifact.slowed_proxy_sha256,
        prompt=build_boundary_refinement_prompt(plan, candidate),
        response_schema=boundary_refinement_schema(plan),
        plan=plan,
    )


def parse_boundary_refinement_response(
    raw: Mapping[str, Any] | str | bytes,
    plan: BoundaryRefinementPlan,
) -> BoundaryRefinementResponse:
    """Parse strict JSON-compatible fake output and enforce plan-local time bounds."""

    payload: object
    if isinstance(raw, bytes):
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("boundary refiner response is not valid UTF-8 JSON") from exc
    elif isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError("boundary refiner response is not valid JSON") from exc
    elif isinstance(raw, Mapping):
        payload = dict(raw)
    else:
        raise ValidationError("boundary refiner response must be a JSON object")

    if not isinstance(payload, dict):
        raise ValidationError("boundary refiner response must be a JSON object")
    try:
        response = BoundaryRefinementResponse.model_validate(payload)
    except Exception as exc:
        raise ValidationError(
            "boundary refiner response violates the strict response contract"
        ) from exc
    if response.event_end_ms > plan.proxy_duration_ms:
        raise ValidationError("boundary refiner response exceeds the slowed proxy duration")
    return response


def run_fake_boundary_refinement(
    media: BoundaryRefinementMediaResult,
    candidate: Candidate,
    fake: FakeBoundaryRefiner | None = None,
    *,
    minimum_confidence: float = 0.5,
    force: bool = False,
) -> BoundaryRefinementFakeResult:
    """Exercise request -> parse -> map -> apply locally, with fake-only cache provenance."""

    request = build_boundary_refinement_request(media, candidate)
    item_dir = media.artifact_path.parent
    request_path = item_dir / "request.fake.json"
    response_path = item_dir / "response.fake.json"

    if not force:
        cached = _load_cached_fake_response(request_path, response_path, request)
        if cached is not None:
            refined = apply_boundary_refinement(
                candidate,
                request.plan,
                cached.response,
                minimum_confidence=minimum_confidence,
            )
            return BoundaryRefinementFakeResult(
                cache_hit=True,
                request_path=request_path,
                response_path=response_path,
                request=request,
                response=cached.response,
                candidate=refined,
            )

    if fake is None:
        raise ValidationError(
            "boundary refiner fake response is required when no valid cache exists"
        )
    atomic_write_json(request_path, model_json(request))
    response = parse_boundary_refinement_response(fake.generate(request), request.plan)
    refined = apply_boundary_refinement(
        candidate,
        request.plan,
        response,
        minimum_confidence=minimum_confidence,
    )
    persisted = BoundaryRefinementFakeResponseArtifact(
        request_fingerprint=request.request_fingerprint,
        response=response,
    )
    atomic_write_json(response_path, model_json(persisted))
    return BoundaryRefinementFakeResult(
        cache_hit=False,
        request_path=request_path,
        response_path=response_path,
        request=request,
        response=response,
        candidate=refined,
    )


def _load_cached_fake_response(
    request_path: Path,
    response_path: Path,
    request: BoundaryRefinementRequestArtifact,
) -> BoundaryRefinementFakeResponseArtifact | None:
    if not request_path.is_file() or not response_path.is_file():
        return None
    try:
        stored_request = BoundaryRefinementRequestArtifact.model_validate(read_json(request_path))
        stored_response = BoundaryRefinementFakeResponseArtifact.model_validate(
            read_json(response_path)
        )
    except Exception:
        return None
    if stored_request.request_fingerprint != request.request_fingerprint:
        return None
    if stored_response.request_fingerprint != request.request_fingerprint:
        return None
    try:
        parse_boundary_refinement_response(
            stored_response.response.model_dump(mode="json"),
            request.plan,
        )
    except ValidationError:
        return None
    return stored_response


def canonical_payload_sha256(payload: object) -> str:
    """Return the deterministic JSON hash used by boundary-refinement artifacts."""

    return _canonical_sha256(payload)


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
