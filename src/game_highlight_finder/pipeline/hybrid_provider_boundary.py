"""Provider-neutral hybrid semantic/verifier contracts and provider-free cost preflight."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from game_highlight_finder.config import AppConfig
from game_highlight_finder.cost.models import CostQuote
from game_highlight_finder.cost.service import CostRequest, CostService
from game_highlight_finder.domain.models import (
    CandidateClaim,
    ClaimStatus,
    EditorialRole,
    Evidence,
    ResolutionState,
    StoryState,
)
from game_highlight_finder.domain.proposals import Proposal, ProposalArtifact
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.pipeline.gemini_scout import (
    effective_gemini_media_resolution,
    effective_gemini_thinking,
    estimate_gemini_usage,
)
from game_highlight_finder.pipeline.hybrid_context_media import (
    HybridContextMedia,
    validate_hybrid_context_proxy,
)
from game_highlight_finder.pipeline.semantic_judge import (
    SEMANTIC_JUDGE_VERSION,
    SemanticJudgment,
)
from game_highlight_finder.pipeline.verification import (
    VERIFICATION_VERSION,
    CandidateVerification,
)
from game_highlight_finder.storage.atomic import atomic_write_json
from game_highlight_finder.storage.hashing import hash_file
from game_highlight_finder.storage.sessions import SessionPaths

HYBRID_PROVIDER_PREFLIGHT_VERSION = "c1-hybrid-provider-preflight-v1"
HYBRID_SEMANTIC_PROMPT_VERSION = "hybrid-semantic-judge-v1"
HYBRID_VERIFIER_PROMPT_VERSION = "hybrid-resolution-verifier-v1"
HybridProviderStage = Literal["SEMANTIC_JUDGE", "RESOLUTION_VERIFIER"]

def _assert_projection_fields(
    model: type[BaseModel],
    schema: dict[str, Any],
    *,
    name: str,
) -> None:
    """Fail closed when a compact provider projection drifts from its canonical model."""

    properties = schema.get("properties")
    if not isinstance(properties, dict) or set(properties) != set(model.model_fields):
        raise RuntimeError(f"{name} provider schema drifted from canonical model fields")


def _evidence_provider_schema() -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "type": {"type": "string"},
            "start_ms": {"type": "integer"},
            "end_ms": {"type": "integer"},
            "strength": {"type": "number"},
            "summary": {"type": "string"},
            "source": {"type": "string"},
        },
        "required": ["type", "summary", "source"],
        "additionalProperties": False,
    }
    _assert_projection_fields(Evidence, schema, name="Evidence")
    return schema


def _claim_provider_schema() -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "claim_type": {"type": "string"},
            "status": {
                "type": "string",
                "enum": [status.value for status in ClaimStatus],
            },
            "evidence": {"type": "array", "items": _evidence_provider_schema()},
            "reason": {"type": "string"},
        },
        "required": ["claim_type", "status", "evidence"],
        "additionalProperties": False,
    }
    _assert_projection_fields(CandidateClaim, schema, name="CandidateClaim")
    return schema


def semantic_response_schema() -> dict[str, Any]:
    """Return the compact Gemini-compatible projection of ``SemanticJudgment``."""

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "version": {"type": "string", "enum": [SEMANTIC_JUDGE_VERSION]},
            "proposal_id": {"type": "string"},
            "event_start_ms": {"type": "integer"},
            "event_end_ms": {"type": "integer"},
            "category": {"type": "string"},
            "editorial_role": {
                "type": "string",
                "enum": [role.value for role in EditorialRole],
            },
            "creator_score": {"type": "number"},
            "confidence": {"type": "number"},
            "moment_summary": {"type": "string"},
            "creator_reason": {"type": "string"},
            "claim_hypotheses": {"type": "array", "items": {"type": "string"}},
            "needs_more_context": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "required": list(SemanticJudgment.model_fields),
        "additionalProperties": False,
    }
    _assert_projection_fields(SemanticJudgment, schema, name="SemanticJudgment")
    return schema


def verifier_response_schema() -> dict[str, Any]:
    """Return the compact Gemini-compatible projection of ``CandidateVerification``."""

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "version": {"type": "string", "enum": [VERIFICATION_VERSION]},
            "candidate_id": {"type": "string"},
            "story_state": {
                "type": "string",
                "enum": [state.value for state in StoryState],
            },
            "resolution_state": {
                "type": "string",
                "enum": [state.value for state in ResolutionState],
            },
            "claims": {"type": "array", "items": _claim_provider_schema()},
            "needs_more_context": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "required": list(CandidateVerification.model_fields),
        "additionalProperties": False,
    }
    _assert_projection_fields(CandidateVerification, schema, name="CandidateVerification")
    return schema


SEMANTIC_RESPONSE_SCHEMA = semantic_response_schema()
VERIFIER_RESPONSE_SCHEMA = verifier_response_schema()


class HybridProviderRequestPlan(BaseModel):
    """Auditable future provider request that has not crossed the provider boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    stage: HybridProviderStage
    call_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    proposal_id: str = Field(pattern=r"^prop_[0-9a-f]{16}$")
    context_id: str = Field(pattern=r"^hctx_[0-9a-f]{16}$")
    context_proxy_path: str = Field(min_length=1, max_length=500)
    context_proxy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_proxy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_duration_ms: int = Field(gt=0)
    provider: str = "gemini"
    model: str = Field(min_length=1, max_length=256)
    billing_mode: str = Field(min_length=1, max_length=64)
    prompt_version: str = Field(min_length=1, max_length=64)
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    configured_media_resolution: str = Field(min_length=1, max_length=32)
    effective_media_resolution: str = Field(min_length=1, max_length=64)
    configured_thinking_level: str = Field(min_length=1, max_length=32)
    effective_thinking_mode: str = Field(min_length=1, max_length=64)
    max_output_tokens: int = Field(gt=0)
    reserved_thinking_tokens: int = Field(ge=0)
    max_generation_attempts: int = Field(default=1, ge=1, le=1)
    automatic_generation_retries: int = Field(default=0, ge=0, le=0)
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    cache_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    quote: CostQuote


class HybridInitialProviderPreflight(BaseModel):
    """Exact quote for the currently materialized semantic first pass only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    version: str = HYBRID_PROVIDER_PREFLIGHT_VERSION
    created_at: datetime
    session_id: str = Field(min_length=1, max_length=128)
    stage: Literal["SEMANTIC_JUDGE"] = "SEMANTIC_JUDGE"
    requests: tuple[HybridProviderRequestPlan, ...]
    logical_call_count: int = Field(ge=0)
    total_media_input_ms: int = Field(ge=0)
    aggregate_estimated_reserve_micro_thb: int = Field(ge=0)
    available_budget_micro_thb: int = Field(ge=0)
    budget_sufficient: bool
    verifier_preflight_state: Literal["BLOCKED_UNTIL_SEMANTIC_OUTPUT"] = (
        "BLOCKED_UNTIL_SEMANTIC_OUTPUT"
    )
    dynamic_expansion_preflight_state: Literal["SEPARATE_REPREFLIGHT_REQUIRED"] = (
        "SEPARATE_REPREFLIGHT_REQUIRED"
    )
    expansion_calls_included: int = Field(default=0, ge=0, le=0)
    live_authorized: bool = False
    raw_source_upload_allowed: bool = False
    provider_calls: int = Field(default=0, ge=0, le=0)
    uploads: int = Field(default=0, ge=0, le=0)
    reservations_created: int = Field(default=0, ge=0, le=0)
    max_generation_attempts_per_logical_call: int = Field(default=1, ge=1, le=1)
    automatic_generation_retries: int = Field(default=0, ge=0, le=0)
    preflight_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def aggregate_matches_requests(self) -> HybridInitialProviderPreflight:
        if self.logical_call_count != len(self.requests):
            raise ValueError("hybrid preflight logical call count does not match requests")
        if self.total_media_input_ms != sum(item.context_duration_ms for item in self.requests):
            raise ValueError("hybrid preflight media duration does not match requests")
        if self.aggregate_estimated_reserve_micro_thb != sum(
            item.quote.reserved_cost_micro_thb for item in self.requests
        ):
            raise ValueError("hybrid preflight aggregate quote does not match requests")
        return self

    @property
    def total_media_input_minutes(self) -> float:
        return self.total_media_input_ms / 60_000

    @property
    def aggregate_estimated_reserve_thb(self) -> float:
        return self.aggregate_estimated_reserve_micro_thb / 1_000_000


class HybridProviderPreflightResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    preflight: HybridInitialProviderPreflight
    preflight_path: Path


def semantic_judge_prompt(proposal: Proposal, context: HybridContextMedia) -> str:
    """Build deterministic creator/story judgment instructions around one factual anchor."""

    proposal_payload = json.dumps(
        {
            "proposal_id": proposal.proposal_id,
            "start_ms": proposal.start_ms,
            "end_ms": proposal.end_ms,
            "signal_type": proposal.signal_type.value,
            "event_hypothesis": proposal.event_hypothesis,
            "confidence": proposal.confidence,
            "sources": proposal.sources,
            "metadata": proposal.metadata,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    context_payload = json.dumps(
        {
            "context_id": context.context_id,
            "source_start_ms": context.plan.context_start_ms,
            "source_end_ms": context.plan.context_end_ms,
            "context_media_time_zero_ms": 0,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return f"""Hybrid creator semantic judge {HYBRID_SEMANTIC_PROMPT_VERSION}.
The factual proposal below is only an anchor for inspection. It is not proof of a highlight,
story, payoff, outcome, or creator value. Judge only the supplied context media.

Separate event/category from editorial role:
- STANDALONE_STORY only when the supplied media appears to contain enough setup plus an actual
  observed payoff/resolution/reaction to make sense as one creator clip; an independent verifier
  must still verify terminal outcome claims.
- MONTAGE_BEAT for a useful mechanical/action/social ingredient whose standalone story is
  incomplete.
- CONTEXT_ONLY for setup/reaction that belongs with another beat.
- NONE when this neighborhood should not consume creator review time.

All claim_hypotheses are provisional. Never mark or imply a claim is verified. Do not infer a
win, clutch, escape, boss defeat, joke payoff, round result, or other resolution before it is
visibly/audibly observed in the supplied media. Planting an objective while opponents remain is
not proof that the round was won or clutched. If setup or terminal payoff is not present, set
needs_more_context=true rather than inventing the missing result.

The supplied MP4 starts at context-media time 0, but response timestamps MUST be source-relative.
Convert every observed context-media timestamp by adding source_start_ms from CONTEXT_MAPPING.
Do not emit context-relative timestamps. Keep reason concise; do not expose hidden chain-of-thought.

FACTUAL_PROPOSAL={proposal_payload}
CONTEXT_MAPPING={context_payload}
"""


def resolution_verifier_prompt(
    *,
    candidate_id: str,
    claim_hypotheses: list[str],
    context_start_ms: int,
    context_end_ms: int,
) -> str:
    """Build the independent factual verifier contract for a future second pass."""

    payload = json.dumps(
        {"candidate_id": candidate_id, "claim_hypotheses": claim_hypotheses},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    context_payload = json.dumps(
        {
            "source_start_ms": context_start_ms,
            "source_end_ms": context_end_ms,
            "context_media_time_zero_ms": 0,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return f"""Hybrid factual resolution verifier {HYBRID_VERIFIER_PROMPT_VERSION}.
Verify only explicit claims against the supplied context media. Do not score creator value.
A VERIFIED or CONTRADICTED claim requires timestamped evidence from the supplied media. If the
terminal result is not observed, leave the claim UNVERIFIED and request more context when useful.
Planting an objective is not evidence of ROUND_WON or CLUTCH_WIN. Never convert a semantic
hypothesis into fact merely because the semantic judge proposed it. The supplied MP4 starts at
context-media time 0; convert evidence timestamps to source-relative time by adding
source_start_ms from CONTEXT_MAPPING. Keep reason concise and do not expose hidden chain-of-thought.

CANDIDATE_CLAIMS={payload}
CONTEXT_MAPPING={context_payload}
"""


def load_committed_hybrid_contexts(paths: SessionPaths) -> tuple[HybridContextMedia, ...]:
    """Load and validate all committed routed context media for one session."""

    if not paths.hybrid_contexts_dir.is_dir():
        raise ValidationError("hybrid provider preflight requires committed routed contexts")
    contexts: list[HybridContextMedia] = []
    for item_dir in sorted(path for path in paths.hybrid_contexts_dir.iterdir() if path.is_dir()):
        media_path = item_dir / "analysis_context.mp4"
        metadata_path = item_dir / "context.json"
        if not media_path.is_file() and not metadata_path.is_file():
            continue
        contexts.append(validate_hybrid_context_proxy(media_path, paths.hybrid_contexts_dir))
    if not contexts:
        raise ValidationError("hybrid provider preflight has no committed routed contexts")
    return tuple(contexts)


def prepare_initial_semantic_preflight(
    *,
    config: AppConfig,
    session_id: str,
    contexts: tuple[HybridContextMedia, ...],
    routed_proposals: ProposalArtifact,
    paths: SessionPaths,
    cost_service: CostService,
    now: datetime | None = None,
) -> HybridProviderPreflightResult:
    """Quote exact current semantic first-pass contexts without reservation or provider I/O."""

    if routed_proposals.session_id != session_id:
        raise ValidationError("hybrid provider preflight proposals belong to another session")
    by_proposal = {proposal.proposal_id: proposal for proposal in routed_proposals.proposals}
    if len(by_proposal) != len(routed_proposals.proposals):
        raise ValidationError("hybrid provider preflight proposal IDs must be unique")
    if not contexts:
        raise ValidationError("hybrid provider preflight requires one committed context per route")
    context_ids = [context.context_id for context in contexts]
    if len(set(context_ids)) != len(context_ids):
        raise ValidationError("hybrid provider preflight context IDs must be unique")
    context_proposal_ids = [context.proposal_id for context in contexts]
    if len(set(context_proposal_ids)) != len(context_proposal_ids):
        raise ValidationError("hybrid provider preflight requires one initial context per proposal")
    if set(context_proposal_ids) != set(by_proposal):
        raise ValidationError(
            "hybrid provider preflight contexts do not exactly cover routed proposals"
        )
    for context in contexts:
        if context.session_id != session_id:
            raise ValidationError("hybrid provider preflight context belongs to another session")
        if context.source_id != routed_proposals.source_id:
            raise ValidationError("hybrid provider preflight context belongs to another source")

    timestamp = now or datetime.now(UTC)
    thinking = effective_gemini_thinking(config)
    media = effective_gemini_media_resolution(config)
    response_schema_hash = _hash_json(SEMANTIC_RESPONSE_SCHEMA)
    analysis_proxy_path = paths.proxy_dir / "analysis_proxy.mp4"
    if not analysis_proxy_path.is_file():
        raise ValidationError("hybrid provider preflight requires the committed analysis proxy")
    parent_proxy_sha256 = hash_file(analysis_proxy_path)
    request_plans: list[HybridProviderRequestPlan] = []

    for context in sorted(contexts, key=lambda item: (item.plan.context_start_ms, item.context_id)):
        proposal = by_proposal.get(context.proposal_id)
        if proposal is None:
            raise ValidationError("hybrid context has no matching routed factual proposal")
        context_path = paths.root / context.proxy_path
        validated = validate_hybrid_context_proxy(
            context_path,
            paths.hybrid_contexts_dir,
            expected_parent_proxy_sha256=parent_proxy_sha256,
        )
        if validated != context:
            raise ValidationError("hybrid context metadata changed during provider preflight")
        prompt = semantic_judge_prompt(proposal, context)
        usage = estimate_gemini_usage(
            duration_ms=context.plan.context_duration_ms,
            prompt=prompt,
            response_schema=SEMANTIC_RESPONSE_SCHEMA,
            audio_present=context.audio_present,
            max_output_tokens=config.scout.max_output_tokens,
            reserved_thinking_tokens=thinking.reserved_thinking_tokens,
            model=config.scout.model,
            media_resolution=config.scout.media_resolution,
        )
        prompt_hash = _sha256_text(prompt)
        payload = {
            "contract": HYBRID_SEMANTIC_PROMPT_VERSION,
            "proposal_id": proposal.proposal_id,
            "context_id": context.context_id,
            "context_proxy_sha256": context.proxy_sha256,
            "prompt_sha256": prompt_hash,
            "response_schema_sha256": response_schema_hash,
            "wire_media_resolution": media.wire_level,
            "thinking_level": thinking.wire_level,
            "max_output_tokens": config.scout.max_output_tokens,
        }
        provisional_request = CostRequest(
            call_id="hybrid-preflight",
            provider="gemini",
            model=config.scout.model,
            billing_mode=config.scout.billing_mode,
            stage="hybrid_semantic_judge",
            session_id=session_id,
            usage_estimate=usage,
            request_payload=payload,
        )
        request_fingerprint = provisional_request.request_fingerprint
        call_id = f"hsem_{request_fingerprint[:20]}"
        request = provisional_request.model_copy(update={"call_id": call_id})
        quote = cost_service.quote(request, now=timestamp)
        request_plans.append(
            HybridProviderRequestPlan(
                stage="SEMANTIC_JUDGE",
                call_id=call_id,
                session_id=session_id,
                proposal_id=proposal.proposal_id,
                context_id=context.context_id,
                context_proxy_path=context.proxy_path,
                context_proxy_sha256=context.proxy_sha256,
                parent_proxy_sha256=context.parent_proxy_sha256,
                context_duration_ms=context.plan.context_duration_ms,
                model=config.scout.model,
                billing_mode=config.scout.billing_mode,
                prompt_version=HYBRID_SEMANTIC_PROMPT_VERSION,
                prompt_sha256=prompt_hash,
                response_schema_sha256=response_schema_hash,
                configured_media_resolution=config.scout.media_resolution,
                effective_media_resolution=media.effective_mode,
                configured_thinking_level=config.scout.thinking_level,
                effective_thinking_mode=thinking.effective_mode,
                max_output_tokens=config.scout.max_output_tokens,
                reserved_thinking_tokens=thinking.reserved_thinking_tokens,
                request_fingerprint=request.request_fingerprint,
                cache_key=_hash_json(
                    {
                        "version": HYBRID_PROVIDER_PREFLIGHT_VERSION,
                        "request_fingerprint": request.request_fingerprint,
                        "context_materialization_key": context.materialization_key,
                    }
                ),
                quote=quote,
            )
        )

    aggregate = sum(item.quote.reserved_cost_micro_thb for item in request_plans)
    budget = cost_service.summary(now=timestamp)
    fingerprint = _hash_json(
        {
            "version": HYBRID_PROVIDER_PREFLIGHT_VERSION,
            "session_id": session_id,
            "requests": [
                {
                    "call_id": item.call_id,
                    "request_fingerprint": item.request_fingerprint,
                    "cache_key": item.cache_key,
                    "quoted_micro_thb": item.quote.reserved_cost_micro_thb,
                }
                for item in request_plans
            ],
            "verifier": "BLOCKED_UNTIL_SEMANTIC_OUTPUT",
            "expansion": "SEPARATE_REPREFLIGHT_REQUIRED",
        }
    )
    preflight = HybridInitialProviderPreflight(
        created_at=timestamp,
        session_id=session_id,
        requests=tuple(request_plans),
        logical_call_count=len(request_plans),
        total_media_input_ms=sum(item.context_duration_ms for item in request_plans),
        aggregate_estimated_reserve_micro_thb=aggregate,
        available_budget_micro_thb=budget.available_micro_thb,
        budget_sufficient=aggregate <= budget.available_micro_thb and not budget.safety_hold_active,
        preflight_fingerprint=fingerprint,
    )
    paths.hybrid_provider_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(paths.hybrid_semantic_preflight_path, preflight.model_dump(mode="json"))
    return HybridProviderPreflightResult(
        preflight=preflight,
        preflight_path=paths.hybrid_semantic_preflight_path,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hash_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "HYBRID_PROVIDER_PREFLIGHT_VERSION",
    "HYBRID_SEMANTIC_PROMPT_VERSION",
    "HYBRID_VERIFIER_PROMPT_VERSION",
    "SEMANTIC_RESPONSE_SCHEMA",
    "VERIFIER_RESPONSE_SCHEMA",
    "HybridInitialProviderPreflight",
    "HybridProviderPreflightResult",
    "HybridProviderRequestPlan",
    "load_committed_hybrid_contexts",
    "prepare_initial_semantic_preflight",
    "resolution_verifier_prompt",
    "semantic_judge_prompt",
]
