"""Provider-free orchestration core for C1 hybrid creator triage."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from game_highlight_finder import __version__
from game_highlight_finder.config import ExtractionConfig
from game_highlight_finder.domain.canonical import deterministic_candidate_id
from game_highlight_finder.domain.models import Candidate, SessionMap
from game_highlight_finder.domain.proposals import Proposal, ProposalArtifact
from game_highlight_finder.domain.reconcile import derive_clip_boundaries
from game_highlight_finder.pipeline.context_expansion import (
    ContextExpansionPlan,
    ExpansionDirection,
    expand_if_needed,
    plan_proposal_context,
)
from game_highlight_finder.pipeline.ranking import is_creator_review_eligible
from game_highlight_finder.pipeline.semantic_judge import (
    FakeSemanticJudge,
    SemanticJudgment,
    candidate_from_semantic_judgment,
)
from game_highlight_finder.pipeline.story_assembly import StoryAssembly, apply_story_assembly
from game_highlight_finder.pipeline.verification import (
    CandidateVerification,
    FakeResolutionVerifier,
    apply_candidate_verification,
)
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.sessions import SessionPaths

HYBRID_TRIAGE_VERSION = "c1-hybrid-triage-v1"
DEFAULT_MAX_CONTEXT_ITERATIONS = 8


class SemanticJudgePort(Protocol):
    def judge(
        self,
        proposal: Proposal,
        context: ContextExpansionPlan | None = None,
    ) -> SemanticJudgment: ...


class ResolutionVerifierPort(Protocol):
    def verify(
        self,
        candidate: Candidate,
        context: ContextExpansionPlan | None = None,
    ) -> CandidateVerification: ...


class HybridTriageRun(BaseModel):
    """Auditable provider-neutral result of proposal -> verify -> story -> review-map."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    created_at: datetime
    producer_version: str
    version: str = HYBRID_TRIAGE_VERSION
    session_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(pattern=r"^src_[0-9a-f]{16}$")
    source_duration_ms: int = Field(gt=0)
    proposals: ProposalArtifact
    context_history: list[ContextExpansionPlan] = Field(default_factory=list, max_length=20_000)
    judgments: list[SemanticJudgment] = Field(default_factory=list, max_length=20_000)
    verifications: list[CandidateVerification] = Field(default_factory=list, max_length=20_000)
    story_assemblies: list[StoryAssembly] = Field(default_factory=list, max_length=10_000)
    session_map: SessionMap
    review_map: SessionMap
    warnings: list[str] = Field(default_factory=list, max_length=100)
    semantic_judge_calls: int = Field(ge=0)
    resolution_verifier_calls: int = Field(ge=0)


class HybridTriagePersistence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_path: str
    session_map_path: str
    review_map_path: str


class HybridFixtureBundle(BaseModel):
    """Local-only semantic/verifier fixtures used to exercise orchestration end to end."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    semantic_judgments: dict[str, list[SemanticJudgment]] = Field(default_factory=dict)
    verifications: dict[str, list[CandidateVerification]] = Field(default_factory=dict)
    story_assemblies: dict[str, StoryAssembly] = Field(default_factory=dict)



def run_provider_free_hybrid_triage(
    *,
    proposals: ProposalArtifact,
    semantic_judge: SemanticJudgePort,
    resolution_verifier: ResolutionVerifierPort,
    extraction_config: ExtractionConfig,
    story_assemblies: Mapping[str, StoryAssembly] | None = None,
    context_direction: ExpansionDirection = "after",
    max_context_iterations: int = DEFAULT_MAX_CONTEXT_ITERATIONS,
    created_at: datetime | None = None,
) -> HybridTriageRun:
    """Run the semantic orchestration with no provider implementation assumptions.

    The supplied ports may be local fakes, local models, or future provider adapters. This
    function itself performs no network access and never authorizes paid inference.
    """

    if max_context_iterations <= 0:
        raise ValueError("max_context_iterations must be positive")
    if context_direction not in {"before", "after", "both"}:
        raise ValueError("context_direction must be before, after, or both")

    run_created_at = created_at or datetime.now(UTC)
    assemblies = dict(story_assemblies or {})
    candidates: list[Candidate] = []
    context_history: list[ContextExpansionPlan] = []
    judgments: list[SemanticJudgment] = []
    verifications: list[CandidateVerification] = []
    applied_assemblies: list[StoryAssembly] = []
    warnings = list(proposals.warnings)
    semantic_calls_before = int(getattr(semantic_judge, "calls", 0))
    verifier_calls_before = int(getattr(resolution_verifier, "calls", 0))

    for proposal in proposals.proposals:
        context = plan_proposal_context(proposal, proposals.source_duration_ms)
        context_history.append(context)

        judgment: SemanticJudgment | None = None
        for _ in range(max_context_iterations):
            judgment = semantic_judge.judge(proposal, context)
            judgments.append(judgment)
            if not judgment.needs_more_context:
                break
            expanded = expand_if_needed(
                context,
                needs_more_context=True,
                direction=context_direction,
            )
            if expanded == context:
                break
            context = expanded
            context_history.append(context)
        if judgment is None:
            raise RuntimeError("semantic judge produced no judgment")

        candidate_id = deterministic_candidate_id(
            session_id=proposals.session_id,
            match_id=None,
            start_ms=judgment.event_start_ms,
            end_ms=judgment.event_end_ms,
            category=judgment.category,
        )
        candidate = candidate_from_semantic_judgment(
            candidate_id=candidate_id,
            proposal=proposal,
            judgment=judgment,
        )

        verification: CandidateVerification | None = None
        for _ in range(max_context_iterations):
            verification = resolution_verifier.verify(candidate, context)
            _validate_verification_evidence(verification, context)
            verifications.append(verification)
            if not verification.needs_more_context:
                break
            expanded = expand_if_needed(
                context,
                needs_more_context=True,
                direction=context_direction,
            )
            if expanded == context:
                break
            context = expanded
            context_history.append(context)
        if verification is None:
            raise RuntimeError("resolution verifier produced no verification")

        candidate = apply_candidate_verification(candidate, verification)
        assembly = assemblies.get(candidate.candidate_id)
        if assembly is not None:
            candidate = apply_story_assembly(
                candidate,
                assembly,
                source_duration_ms=proposals.source_duration_ms,
            )
            applied_assemblies.append(assembly)
        elif (
            candidate.editorial_role is not None
            and candidate.editorial_role.value == "STANDALONE_STORY"
        ):
            if is_creator_review_eligible(candidate):
                warnings.append(
                    f"Verified standalone {candidate.candidate_id} has no explicit story assembly; "
                    "using verified candidate boundaries until H6 boundary evidence is supplied."
                )

        if judgment.needs_more_context and context.exhausted:
            warnings.append(
                f"Semantic context exhausted for {proposal.proposal_id}; retained verifier state."
            )
        if verification.needs_more_context and context.exhausted:
            warnings.append(
                "Verification context exhausted for "
                f"{candidate.candidate_id}; candidate remains gated."
            )
        candidates.append(candidate)

    session_map = SessionMap(
        created_at=run_created_at,
        producer_version=__version__,
        canonicalization_version=HYBRID_TRIAGE_VERSION,
        session_id=proposals.session_id,
        source_id=proposals.source_id,
        duration_ms=proposals.source_duration_ms,
        candidates=candidates,
        statistics={
            "proposal_count": len(proposals.proposals),
            "candidate_count": len(candidates),
            "creator_review_eligible_count": sum(
                1 for candidate in candidates if is_creator_review_eligible(candidate)
            ),
        },
        warnings=list(dict.fromkeys(warnings))[:100],
        scout_backend="hybrid-provider-neutral",
        scout_metadata={
            "hybrid_triage_version": HYBRID_TRIAGE_VERSION,
            "semantic_center": "proposal_verify_story_rank",
        },
    )
    eligible = [candidate for candidate in candidates if is_creator_review_eligible(candidate)]
    review_map = session_map.model_copy(
        update={
            "candidates": eligible,
            "statistics": {
                **session_map.statistics,
                "candidate_count": len(eligible),
                "creator_review_eligible_count": len(eligible),
            },
        }
    )
    review_map = derive_clip_boundaries(
        review_map,
        proposals.source_duration_ms,
        extraction_config,
    )

    return HybridTriageRun(
        created_at=run_created_at,
        producer_version=__version__,
        session_id=proposals.session_id,
        source_id=proposals.source_id,
        source_duration_ms=proposals.source_duration_ms,
        proposals=proposals,
        context_history=context_history,
        judgments=judgments,
        verifications=verifications,
        story_assemblies=applied_assemblies,
        session_map=session_map,
        review_map=review_map,
        warnings=list(dict.fromkeys(warnings))[:100],
        semantic_judge_calls=int(getattr(semantic_judge, "calls", 0)) - semantic_calls_before,
        resolution_verifier_calls=(
            int(getattr(resolution_verifier, "calls", 0)) - verifier_calls_before
        ),
    )



def run_hybrid_fixture_bundle(
    *,
    proposals: ProposalArtifact,
    fixture: HybridFixtureBundle,
    extraction_config: ExtractionConfig,
    context_direction: ExpansionDirection = "after",
    max_context_iterations: int = DEFAULT_MAX_CONTEXT_ITERATIONS,
    created_at: datetime | None = None,
) -> HybridTriageRun:
    """Exercise the hybrid orchestration from local fixtures with zero provider I/O."""

    for proposal in proposals.proposals:
        sequence = fixture.semantic_judgments.get(proposal.proposal_id)
        if not sequence:
            raise ValueError(f"fixture is missing semantic judgment for {proposal.proposal_id}")
    semantic = FakeSemanticJudge(fixture.semantic_judgments)
    verifier = FakeResolutionVerifier(fixture.verifications)
    return run_provider_free_hybrid_triage(
        proposals=proposals,
        semantic_judge=semantic,
        resolution_verifier=verifier,
        extraction_config=extraction_config,
        story_assemblies=fixture.story_assemblies,
        context_direction=context_direction,
        max_context_iterations=max_context_iterations,
        created_at=created_at,
    )



def persist_hybrid_triage(paths: SessionPaths, run: HybridTriageRun) -> HybridTriagePersistence:
    """Persist one auditable hybrid run and its full/review maps atomically."""

    if paths.root.name != run.session_id:
        raise ValueError("hybrid run session does not match target session path")
    paths.hybrid_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(paths.hybrid_run_path, run.model_dump(mode="json"))
    atomic_write_json(paths.hybrid_session_map_path, run.session_map.model_dump(mode="json"))
    atomic_write_json(paths.hybrid_review_map_path, run.review_map.model_dump(mode="json"))
    return HybridTriagePersistence(
        run_path=str(paths.hybrid_run_path),
        session_map_path=str(paths.hybrid_session_map_path),
        review_map_path=str(paths.hybrid_review_map_path),
    )



def load_hybrid_triage(paths: SessionPaths) -> HybridTriageRun:
    return HybridTriageRun.model_validate(read_json(paths.hybrid_run_path))



def _validate_verification_evidence(
    verification: CandidateVerification,
    context: ContextExpansionPlan,
) -> None:
    for claim in verification.claims:
        for evidence in claim.evidence:
            if evidence.start_ms is not None and evidence.start_ms < context.context_start_ms:
                raise ValueError("verification evidence begins before supplied context")
            if evidence.end_ms is not None and evidence.end_ms > context.context_end_ms:
                raise ValueError("verification evidence ends after supplied context")


__all__ = [
    "DEFAULT_MAX_CONTEXT_ITERATIONS",
    "HYBRID_TRIAGE_VERSION",
    "HybridFixtureBundle",
    "HybridTriagePersistence",
    "HybridTriageRun",
    "ResolutionVerifierPort",
    "SemanticJudgePort",
    "load_hybrid_triage",
    "persist_hybrid_triage",
    "run_hybrid_fixture_bundle",
    "run_provider_free_hybrid_triage",
]
