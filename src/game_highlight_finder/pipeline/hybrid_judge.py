"""Provider-free semantic judge contract for hybrid gameplay proposals.

The local proposer decides *where to look*. This module defines the next boundary: a
semantic judge decides whether a bounded proposal contains a worthwhile moment and, when
it does, returns proposal-relative event bounds. Proposal anchors remain navigation hints
only and are never promoted to semantic evidence by this layer.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from game_highlight_finder.domain.canonical import deterministic_candidate_id
from game_highlight_finder.domain.models import Candidate, CandidateCategory, Evidence, Sha256
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.pipeline.hybrid_proposals import (
    HybridProposal,
    HybridProposalPreparation,
    PreparedHybridProposal,
)
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.hashing import hash_file

HYBRID_JUDGE_VERSION = "hybrid-judge-v1"
HYBRID_JUDGE_MAX_EVENTS = 4
HybridJudgeDecision = Literal["KEEP", "REJECT", "UNCERTAIN"]


class HybridJudgeEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_start_ms: int = Field(ge=0)
    event_end_ms: int = Field(gt=0)
    category: str = Field(min_length=1, max_length=48)
    score: float = Field(ge=0, le=10)
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=500)
    visible_evidence: list[str] = Field(min_length=1, max_length=8)

    @field_validator("category")
    @classmethod
    def controlled_category(cls, value: str) -> str:
        normalized = value.strip().upper()
        allowed = {item.value for item in CandidateCategory}
        if normalized not in allowed:
            raise ValueError(f"unknown hybrid judge category: {value!r}")
        return normalized

    @field_validator("visible_evidence")
    @classmethod
    def bounded_visible_evidence(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 240 for item in value):
            raise ValueError("visible evidence entries must be 1-240 characters")
        return [item.strip() for item in value]

    @model_validator(mode="after")
    def event_is_ordered(self) -> HybridJudgeEvent:
        if self.event_end_ms <= self.event_start_ms:
            raise ValueError("hybrid judge event interval must be non-empty")
        return self


class HybridJudgeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: HybridJudgeDecision
    summary: str = Field(min_length=1, max_length=500)
    events: list[HybridJudgeEvent] = Field(default_factory=list, max_length=HYBRID_JUDGE_MAX_EVENTS)

    @model_validator(mode="after")
    def decision_matches_events(self) -> HybridJudgeResponse:
        if self.decision == "KEEP" and not self.events:
            raise ValueError("KEEP requires at least one bounded event")
        if self.decision == "REJECT" and self.events:
            raise ValueError("REJECT must not contain semantic events")
        return self


class HybridJudgeRequestArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    version: Literal["hybrid-judge-v1"] = "hybrid-judge-v1"
    session_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(pattern=r"^src_[0-9a-f]{16}$")
    proposal_id: str = Field(pattern=r"^proposal_[0-9a-f]{16}$")
    proposal_start_ms: int = Field(ge=0)
    proposal_end_ms: int = Field(gt=0)
    proposal_sha256: Sha256
    media_path: str = Field(min_length=1, max_length=1000)
    media_sha256: Sha256
    prompt: str = Field(min_length=1, max_length=20_000)
    response_schema: dict[str, Any]

    @model_validator(mode="after")
    def proposal_is_ordered(self) -> HybridJudgeRequestArtifact:
        if self.proposal_end_ms <= self.proposal_start_ms:
            raise ValueError("hybrid judge proposal interval must be non-empty")
        return self

    @property
    def proposal_duration_ms(self) -> int:
        return self.proposal_end_ms - self.proposal_start_ms

    @property
    def request_fingerprint(self) -> str:
        return canonical_payload_sha256(self.model_dump(mode="json"))


class HybridJudgeFakeResponseArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    backend: Literal["fake"] = "fake"
    request_fingerprint: Sha256
    response: HybridJudgeResponse


class HybridJudgeProposalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    cache_hit: bool
    request_path: Path
    response_path: Path
    request: HybridJudgeRequestArtifact
    response: HybridJudgeResponse
    candidates: tuple[Candidate, ...] = ()


class HybridJudgeBatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    session_id: str = Field(min_length=1, max_length=128)
    provider_calls: Literal[0] = 0
    proposal_results: tuple[HybridJudgeProposalResult, ...]
    candidates: tuple[Candidate, ...]


class FakeHybridJudge:
    """Deterministic proposal-ID keyed semantic fixture with zero provider I/O."""

    def __init__(self, responses: Mapping[str, Mapping[str, Any] | str | bytes]) -> None:
        self.responses = dict(responses)
        self.calls: list[str] = []

    def generate(self, request: HybridJudgeRequestArtifact) -> Mapping[str, Any] | str | bytes:
        self.calls.append(request.proposal_id)
        if request.proposal_id not in self.responses:
            raise ValidationError(f"No fake hybrid judge response for {request.proposal_id}")
        return self.responses[request.proposal_id]


def hybrid_judge_schema() -> dict[str, Any]:
    """Return the deliberately small Gemini-compatible schema for bounded proposals.

    Numeric bounds are enforced locally by Pydantic rather than encoded in the provider
    JSON schema because Gemini structured-output surfaces have historically been more
    reliable with simple types/enums than with numeric-bound keywords.
    """

    event = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "event_start_ms": {"type": "integer"},
            "event_end_ms": {"type": "integer"},
            "category": {
                "type": "string",
                "enum": [item.value for item in CandidateCategory],
            },
            "score": {"type": "number"},
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
            "visible_evidence": {
                "type": "array",
                "maxItems": 8,
                "items": {"type": "string"},
            },
        },
        "required": [
            "event_start_ms",
            "event_end_ms",
            "category",
            "score",
            "confidence",
            "reason",
            "visible_evidence",
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "decision": {"type": "string", "enum": ["KEEP", "REJECT", "UNCERTAIN"]},
            "summary": {"type": "string"},
            "events": {
                "type": "array",
                "maxItems": HYBRID_JUDGE_MAX_EVENTS,
                "items": event,
            },
        },
        "required": ["decision", "summary", "events"],
    }


def build_hybrid_judge_prompt(proposal: HybridProposal) -> str:
    """Build a deterministic semantic-only instruction for one proposal clip."""

    duration_ms = proposal.end_ms - proposal.start_ms
    anchor_payload = [
        {
            "anchor_ms_in_source": item.anchor_ms,
            "anchor_ms_in_proposal": item.anchor_ms - proposal.start_ms,
            "selected_by": item.selected_by,
        }
        for item in proposal.anchors
    ]
    return "\n".join(
        (
            f"You are Game Highlight Finder semantic judge {HYBRID_JUDGE_VERSION}.",
            (
                "Analyze only the supplied bounded gameplay proposal and return JSON "
                "matching the schema."
            ),
            "Local audio/motion anchors are navigation hints only, never semantic evidence.",
            "Return KEEP only for a visibly self-contained, clip-worthy gameplay moment.",
            (
                "Return REJECT for traversal, menus, idle motion, ordinary uneventful play, "
                "or activity with no visible payoff."
            ),
            (
                "Return UNCERTAIN when a real interaction is visible but clip-worthiness "
                "or event bounds remain genuinely unclear."
            ),
            (
                "For KEEP, return one or more distinct visible events. REJECT must return "
                "an empty events array."
            ),
            (
                "All event timestamps are proposal-relative integer milliseconds from 0 "
                "to the proposal duration."
            ),
            (
                "Every kept event must cite concise visible on-screen evidence; do not cite "
                "loudness alone."
            ),
            "Use score on a 0-10 scale and confidence on a 0-1 scale.",
            "Do not emit hidden reasoning or thought steps.",
            f"Proposal duration (milliseconds): {duration_ms}",
            f"Source interval: {proposal.start_ms}-{proposal.end_ms} ms",
            "Navigation anchors: "
            + json.dumps(anchor_payload, sort_keys=True, separators=(",", ":")),
        )
    )


def build_hybrid_judge_request(
    preparation: HybridProposalPreparation,
    prepared: PreparedHybridProposal,
) -> HybridJudgeRequestArtifact:
    """Bind a judge request to an exact committed proposal and media hash."""

    proposal = prepared.proposal
    bound = next(
        (item for item in preparation.plan.proposals if item.proposal_id == proposal.proposal_id),
        None,
    )
    if bound is None or bound != proposal:
        raise ValidationError("hybrid judge proposal is absent from the bound proposal plan")
    if not prepared.proxy_path.is_file() or prepared.proxy_path.name != "analysis_proposal.mp4":
        raise ValidationError("hybrid judge requires a committed analysis_proposal.mp4")
    actual_sha = hash_file(prepared.proxy_path)
    if actual_sha != prepared.proxy_sha256:
        raise ValidationError("hybrid judge proposal media hash does not match provenance")
    return HybridJudgeRequestArtifact(
        session_id=preparation.plan.session_id,
        source_id=preparation.plan.source_id,
        proposal_id=proposal.proposal_id,
        proposal_start_ms=proposal.start_ms,
        proposal_end_ms=proposal.end_ms,
        proposal_sha256=canonical_payload_sha256(proposal.model_dump(mode="json")),
        media_path=str(prepared.proxy_path),
        media_sha256=actual_sha,
        prompt=build_hybrid_judge_prompt(proposal),
        response_schema=hybrid_judge_schema(),
    )


def parse_hybrid_judge_response(
    raw: Mapping[str, Any] | str | bytes,
    *,
    proposal_duration_ms: int,
) -> HybridJudgeResponse:
    """Parse strict JSON and fail closed when event bounds escape the proposal."""

    payload: object
    if isinstance(raw, bytes):
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("hybrid judge response is not valid UTF-8 JSON") from exc
    elif isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError("hybrid judge response is not valid JSON") from exc
    elif isinstance(raw, Mapping):
        payload = dict(raw)
    else:
        raise ValidationError("hybrid judge response must be a JSON object")
    if not isinstance(payload, dict):
        raise ValidationError("hybrid judge response must be a JSON object")
    try:
        response = HybridJudgeResponse.model_validate(payload)
    except Exception as exc:
        raise ValidationError("hybrid judge response violates the strict contract") from exc
    if proposal_duration_ms <= 0:
        raise ValidationError("hybrid judge proposal duration must be positive")
    if any(item.event_end_ms > proposal_duration_ms for item in response.events):
        raise ValidationError("hybrid judge response exceeds the proposal duration")
    return response


def response_to_candidates(
    request: HybridJudgeRequestArtifact,
    response: HybridJudgeResponse,
) -> tuple[Candidate, ...]:
    """Map only KEEP events back to source-relative canonical candidates."""

    if response.decision != "KEEP":
        return ()
    candidates: list[Candidate] = []
    for event in response.events:
        source_start = request.proposal_start_ms + event.event_start_ms
        source_end = request.proposal_start_ms + event.event_end_ms
        candidate_id = deterministic_candidate_id(
            session_id=request.session_id,
            match_id=None,
            start_ms=source_start,
            end_ms=source_end,
            category=event.category,
            kind="MOMENT",
        )
        evidence = [
            Evidence(
                type="VISIBLE_EVENT",
                start_ms=source_start,
                end_ms=source_end,
                summary=item,
                source="hybrid_judge",
            )
            for item in event.visible_evidence
        ]
        candidates.append(
            Candidate(
                candidate_id=candidate_id,
                category=event.category,
                event_start_ms=source_start,
                event_end_ms=source_end,
                score=event.score,
                confidence=event.confidence,
                reason=event.reason,
                evidence=evidence,
                source_window_ids=[request.proposal_id],
                metadata={
                    "hybrid_judge": HYBRID_JUDGE_VERSION,
                    "judge_decision": response.decision,
                    "proposal_id": request.proposal_id,
                },
            )
        )
    return tuple(candidates)


def run_fake_hybrid_judge_batch(
    preparation: HybridProposalPreparation,
    fake: FakeHybridJudge,
    *,
    force: bool = False,
) -> HybridJudgeBatchResult:
    """Exercise proposal → semantic response → source candidate locally with fake fixtures."""

    results: list[HybridJudgeProposalResult] = []
    all_candidates: list[Candidate] = []
    proposal_ranges = {
        item.proposal.proposal_id: (item.proposal.start_ms, item.proposal.end_ms)
        for item in preparation.prepared
    }
    for prepared in preparation.prepared:
        request = build_hybrid_judge_request(preparation, prepared)
        item_dir = prepared.proxy_path.parent
        request_path = item_dir / "request.judge.fake.json"
        response_path = item_dir / "response.judge.fake.json"
        cached = None if force else _load_cached_fake(request_path, response_path, request)
        if cached is None:
            atomic_write_json(request_path, request.model_dump(mode="json"))
            response = parse_hybrid_judge_response(
                fake.generate(request),
                proposal_duration_ms=request.proposal_duration_ms,
            )
            artifact = HybridJudgeFakeResponseArtifact(
                request_fingerprint=request.request_fingerprint,
                response=response,
            )
            atomic_write_json(response_path, artifact.model_dump(mode="json"))
            cache_hit = False
        else:
            response = cached.response
            cache_hit = True
        candidates = response_to_candidates(request, response)
        all_candidates.extend(candidates)
        results.append(
            HybridJudgeProposalResult(
                cache_hit=cache_hit,
                request_path=request_path,
                response_path=response_path,
                request=request,
                response=response,
                candidates=candidates,
            )
        )
    return HybridJudgeBatchResult(
        session_id=preparation.plan.session_id,
        proposal_results=tuple(results),
        candidates=reconcile_hybrid_candidates(
            preparation.plan.session_id,
            all_candidates,
            proposal_ranges,
        ),
    )


def reconcile_hybrid_candidates(
    session_id: str,
    candidates: Sequence[Candidate],
    proposal_ranges: Mapping[str, tuple[int, int]],
) -> tuple[Candidate, ...]:
    """Deduplicate same-event judgments emitted from overlapping proposal clips."""

    merged: list[Candidate] = []
    for candidate in sorted(
        candidates,
        key=lambda item: (item.event_start_ms, item.event_end_ms, item.category, item.candidate_id),
    ):
        duplicate_index = next(
            (
                index
                for index, existing in enumerate(merged)
                if _hybrid_candidate_compatible(existing, candidate, proposal_ranges)
            ),
            None,
        )
        if duplicate_index is None:
            merged.append(candidate)
            continue
        existing = merged[duplicate_index]
        preferred = (
            existing
            if (existing.confidence, existing.score, existing.candidate_id)
            >= (candidate.confidence, candidate.score, candidate.candidate_id)
            else candidate
        )
        start_ms = min(existing.event_start_ms, candidate.event_start_ms)
        end_ms = max(existing.event_end_ms, candidate.event_end_ms)
        merged_id = deterministic_candidate_id(
            session_id=session_id,
            match_id=None,
            start_ms=start_ms,
            end_ms=end_ms,
            category=existing.category,
            kind="MOMENT",
        )
        merged[duplicate_index] = preferred.model_copy(
            update={
                "candidate_id": merged_id,
                "event_start_ms": start_ms,
                "event_end_ms": end_ms,
                "source_window_ids": list(
                    dict.fromkeys([*existing.source_window_ids, *candidate.source_window_ids])
                )[:32],
                "evidence": _merge_evidence([*existing.evidence, *candidate.evidence]),
                "related_candidate_ids": list(
                    dict.fromkeys(
                        [
                            *existing.related_candidate_ids,
                            *candidate.related_candidate_ids,
                            existing.candidate_id,
                            candidate.candidate_id,
                        ]
                    )
                )[:32],
            }
        )
    return tuple(merged)


def canonical_payload_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_cached_fake(
    request_path: Path,
    response_path: Path,
    request: HybridJudgeRequestArtifact,
) -> HybridJudgeFakeResponseArtifact | None:
    if not request_path.is_file() or not response_path.is_file():
        return None
    try:
        stored_request = HybridJudgeRequestArtifact.model_validate(read_json(request_path))
        stored = HybridJudgeFakeResponseArtifact.model_validate(read_json(response_path))
        if stored_request.request_fingerprint != request.request_fingerprint:
            return None
        if stored.request_fingerprint != request.request_fingerprint:
            return None
        parse_hybrid_judge_response(
            stored.response.model_dump(mode="json"),
            proposal_duration_ms=request.proposal_duration_ms,
        )
    except Exception:
        return None
    return stored


def _hybrid_candidate_compatible(
    left: Candidate,
    right: Candidate,
    proposal_ranges: Mapping[str, tuple[int, int]],
) -> bool:
    if left.category != right.category:
        return False
    lineage_overlap = any(
        _overlap(*proposal_ranges[left_id], *proposal_ranges[right_id]) > 0
        for left_id in left.source_window_ids
        for right_id in right.source_window_ids
        if left_id in proposal_ranges and right_id in proposal_ranges
    )
    if not lineage_overlap:
        return False
    endpoint_jitter = (
        abs(left.event_start_ms - right.event_start_ms) <= 750
        and abs(left.event_end_ms - right.event_end_ms) <= 750
    )
    return (
        _iou(
            left.event_start_ms,
            left.event_end_ms,
            right.event_start_ms,
            right.event_end_ms,
        )
        >= 0.5
        or endpoint_jitter
    )


def _overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start))


def _iou(a_start: int, a_end: int, b_start: int, b_end: int) -> float:
    intersection = _overlap(a_start, a_end, b_start, b_end)
    union = max(a_end, b_end) - min(a_start, b_start)
    return intersection / union if union else 0.0


def _merge_evidence(items: Sequence[Evidence]) -> list[Evidence]:
    result: list[Evidence] = []
    seen: set[tuple[object, ...]] = set()
    for item in items:
        key = (item.type, item.start_ms, item.end_ms, item.summary, item.source)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= 16:
            break
    return result


__all__ = [
    "HYBRID_JUDGE_MAX_EVENTS",
    "HYBRID_JUDGE_VERSION",
    "FakeHybridJudge",
    "HybridJudgeBatchResult",
    "HybridJudgeEvent",
    "HybridJudgeProposalResult",
    "HybridJudgeRequestArtifact",
    "HybridJudgeResponse",
    "build_hybrid_judge_prompt",
    "build_hybrid_judge_request",
    "canonical_payload_sha256",
    "hybrid_judge_schema",
    "parse_hybrid_judge_response",
    "reconcile_hybrid_candidates",
    "response_to_candidates",
    "run_fake_hybrid_judge_batch",
]
