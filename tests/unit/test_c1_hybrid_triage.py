from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from game_highlight_finder import __version__
from game_highlight_finder.config import ExtractionConfig
from game_highlight_finder.domain.canonical import deterministic_candidate_id
from game_highlight_finder.domain.models import (
    AudioActivityInterval,
    Candidate,
    CandidateClaim,
    ClaimStatus,
    EditorialRole,
    Evidence,
    LocalSignalsArtifact,
    ResolutionState,
    SessionMap,
    StoryState,
    TimeInterval,
)
from game_highlight_finder.domain.proposals import (
    ManualProposalMarker,
    ManualProposalMarkerSet,
    Proposal,
    ProposalArtifact,
    ProposalRoute,
    ProposalSignalType,
    TranscriptFixture,
    TranscriptUtterance,
)
from game_highlight_finder.pipeline.context_expansion import (
    expand_if_needed,
    plan_proposal_context,
)
from game_highlight_finder.pipeline.creator_evaluation import (
    CreatorCandidateReview,
    CreatorDecision,
    CreatorEvaluationCorpus,
    ObviousCreatorMiss,
    create_creator_review_template,
    creator_evaluation_from_template,
    load_creator_evaluation,
    load_creator_review_template,
    persist_creator_evaluation,
    persist_creator_review_template,
    summarize_creator_evaluation,
)
from game_highlight_finder.pipeline.hybrid_triage import (
    HybridFixtureBundle,
    load_hybrid_triage,
    persist_hybrid_triage,
    run_hybrid_fixture_bundle,
    run_provider_free_hybrid_triage,
)
from game_highlight_finder.pipeline.proposals import (
    combine_and_cluster_proposals,
    proposals_from_local_signals,
    proposals_from_manual_markers,
    proposals_from_transcript,
    route_proposals,
    selected_proposal_artifact,
    summarize_proposals,
)
from game_highlight_finder.pipeline.ranking import (
    is_creator_review_eligible,
    rank_session_map,
)
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
from game_highlight_finder.storage.sessions import session_paths

SOURCE_ID = "src_" + "a" * 16
SESSION_ID = "c1_hybrid_fixture"
NOW = datetime(2026, 9, 9, tzinfo=UTC)


def _candidate(
    *,
    candidate_id: str,
    editorial_role: EditorialRole,
    story_state: StoryState,
    resolution_state: ResolutionState,
    claims: list[CandidateClaim] | None = None,
) -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        category="CLUTCH",
        event_start_ms=10_000,
        event_end_ms=15_000,
        score=9.5,
        confidence=0.99,
        reason="Fixture candidate for hybrid semantic gating.",
        editorial_role=editorial_role,
        story_state=story_state,
        resolution_state=resolution_state,
        claims=claims or [],
    )


def _map(candidate: Candidate) -> SessionMap:
    return SessionMap(
        created_at=NOW,
        producer_version=__version__,
        canonicalization_version="c1-hybrid-test",
        session_id=SESSION_ID,
        source_id=SOURCE_ID,
        duration_ms=60_000,
        candidates=[candidate],
    )


def test_resolved_claim_requires_explicit_evidence() -> None:
    with pytest.raises(PydanticValidationError, match="explicit evidence"):
        CandidateClaim(claim_type="ROUND_WON", status=ClaimStatus.VERIFIED)

    claim = CandidateClaim(
        claim_type="ROUND_WON",
        status=ClaimStatus.VERIFIED,
        evidence=[
            Evidence(
                type="visual",
                start_ms=20_000,
                end_ms=21_000,
                summary="Round-end state is visibly shown.",
                source="resolution_verifier",
            )
        ],
    )
    assert claim.status is ClaimStatus.VERIFIED


def test_unresolved_spike_style_claim_cannot_enter_standalone_shortlist() -> None:
    unresolved = _candidate(
        candidate_id="cand_0000000000000001",
        editorial_role=EditorialRole.STANDALONE_STORY,
        story_state=StoryState.INCOMPLETE,
        resolution_state=ResolutionState.UNVERIFIED,
        claims=[CandidateClaim(claim_type="ROUND_WON", status=ClaimStatus.UNVERIFIED)],
    )

    assert is_creator_review_eligible(unresolved) is False
    assert rank_session_map(_map(unresolved)).candidate_count == 0


def test_historical_spike_plant_replay_blocks_premature_round_win() -> None:
    session_id = "2026-08-15_unknown_7db9940058f7"
    proposal = Proposal(
        proposal_id="prop_00000000000000a1",
        start_ms=228_000,
        end_ms=238_000,
        signal_type=ProposalSignalType.GAME_EVENT,
        event_hypothesis="OBJECTIVE_PLANTED",
        confidence=0.95,
        sources=["historical_a_replay"],
    )
    proposals = ProposalArtifact(
        created_at=NOW,
        producer_version=__version__,
        session_id=session_id,
        source_id=SOURCE_ID,
        source_duration_ms=600_886,
        proposals=[proposal],
    )
    judgment = SemanticJudgment(
        proposal_id=proposal.proposal_id,
        event_start_ms=228_000,
        event_end_ms=238_000,
        category="CLUTCH",
        editorial_role=EditorialRole.STANDALONE_STORY,
        creator_score=8.5,
        confidence=0.95,
        moment_summary="The objective is planted during a tense late-round sequence.",
        creator_reason=(
            "Potential clutch story if the round actually resolves in the player's favor."
        ),
        claim_hypotheses=["ROUND_WON"],
        needs_more_context=False,
        reason="Historical semantic replay intentionally proposes the premature win hypothesis.",
    )
    candidate_id = deterministic_candidate_id(
        session_id=session_id,
        match_id=None,
        start_ms=228_000,
        end_ms=238_000,
        category="CLUTCH",
    )
    verification = CandidateVerification(
        candidate_id=candidate_id,
        story_state=StoryState.INCOMPLETE,
        resolution_state=ResolutionState.UNVERIFIED,
        claims=[CandidateClaim(claim_type="ROUND_WON", status=ClaimStatus.UNVERIFIED)],
        needs_more_context=False,
        reason=(
            "Spike plant is visible, but enemies remain and no terminal round result is present."
        ),
    )

    run = run_provider_free_hybrid_triage(
        proposals=proposals,
        semantic_judge=FakeSemanticJudge({proposal.proposal_id: judgment}),
        resolution_verifier=FakeResolutionVerifier({candidate_id: verification}),
        extraction_config=ExtractionConfig(),
        created_at=NOW,
    )

    assert len(run.session_map.candidates) == 1
    candidate = run.session_map.candidates[0]
    assert candidate.resolution_state is ResolutionState.UNVERIFIED
    assert candidate.story_state is StoryState.INCOMPLETE
    assert candidate.claims[0].status is ClaimStatus.UNVERIFIED
    assert run.review_map.candidates == []


def test_unresolved_real_event_may_still_be_a_montage_beat() -> None:
    montage = _candidate(
        candidate_id="cand_0000000000000002",
        editorial_role=EditorialRole.MONTAGE_BEAT,
        story_state=StoryState.INCOMPLETE,
        resolution_state=ResolutionState.UNVERIFIED,
        claims=[CandidateClaim(claim_type="ROUND_WON", status=ClaimStatus.UNVERIFIED)],
    )

    ranking = rank_session_map(_map(montage))
    assert is_creator_review_eligible(montage) is True
    assert ranking.candidate_count == 1
    assert ranking.entries[0].editorial_role is EditorialRole.MONTAGE_BEAT


def test_verified_or_non_terminal_complete_story_can_enter_standalone_shortlist() -> None:
    verified_claim = CandidateClaim(
        claim_type="ROUND_WON",
        status=ClaimStatus.VERIFIED,
        evidence=[
            Evidence(
                type="visual",
                start_ms=18_000,
                end_ms=19_000,
                summary="Terminal round-end evidence is visible.",
                source="resolution_verifier",
            )
        ],
    )
    verified = _candidate(
        candidate_id="cand_0000000000000003",
        editorial_role=EditorialRole.STANDALONE_STORY,
        story_state=StoryState.COMPLETE,
        resolution_state=ResolutionState.VERIFIED,
        claims=[verified_claim],
    )
    social = _candidate(
        candidate_id="cand_0000000000000004",
        editorial_role=EditorialRole.STANDALONE_STORY,
        story_state=StoryState.COMPLETE,
        resolution_state=ResolutionState.NOT_APPLICABLE,
    )

    assert is_creator_review_eligible(verified) is True
    assert is_creator_review_eligible(social) is True


def test_independent_verifier_unlocks_standalone_only_after_terminal_evidence() -> None:
    provisional = _candidate(
        candidate_id="cand_0000000000000005",
        editorial_role=EditorialRole.STANDALONE_STORY,
        story_state=StoryState.UNKNOWN,
        resolution_state=ResolutionState.UNVERIFIED,
        claims=[CandidateClaim(claim_type="ROUND_WON", status=ClaimStatus.UNVERIFIED)],
    )
    assert is_creator_review_eligible(provisional) is False

    verification = CandidateVerification(
        candidate_id=provisional.candidate_id,
        story_state=StoryState.COMPLETE,
        resolution_state=ResolutionState.VERIFIED,
        claims=[
            CandidateClaim(
                claim_type="ROUND_WON",
                status=ClaimStatus.VERIFIED,
                evidence=[
                    Evidence(
                        type="visual",
                        start_ms=18_000,
                        end_ms=19_000,
                        summary="Round-end state is visibly shown after the fight resolves.",
                        source="resolution_verifier",
                    )
                ],
            )
        ],
        reason="Terminal evidence verifies the provisional round-win claim.",
    )
    verified = apply_candidate_verification(provisional, verification)

    assert verified.resolution_state is ResolutionState.VERIFIED
    assert verified.story_state is StoryState.COMPLETE
    assert is_creator_review_eligible(verified) is True
    assert "c1-resolution-verifier-v1" in verified.normalization_actions


def test_semantic_judge_can_propose_standalone_but_cannot_self_verify_it() -> None:
    proposal = Proposal(
        proposal_id="prop_0000000000000003",
        start_ms=10_000,
        end_ms=12_000,
        signal_type=ProposalSignalType.GAME_EVENT,
        event_hypothesis="OBJECTIVE_PLANTED",
        confidence=0.9,
        sources=["fixture_game_event"],
    )
    judgment = SemanticJudgment(
        proposal_id=proposal.proposal_id,
        event_start_ms=9_000,
        event_end_ms=20_000,
        category="CLUTCH",
        editorial_role=EditorialRole.STANDALONE_STORY,
        creator_score=9.0,
        confidence=0.95,
        moment_summary="The objective is planted during a tense late-round sequence.",
        creator_reason="The sequence may become a complete clutch story if the round resolves.",
        claim_hypotheses=["ROUND_WON"],
        needs_more_context=True,
        reason="Potential story, but terminal round state is not yet established.",
    )
    judge = FakeSemanticJudge({proposal.proposal_id: judgment})
    observed = judge.judge(proposal)
    candidate = candidate_from_semantic_judgment(
        candidate_id="cand_0000000000000006",
        proposal=proposal,
        judgment=observed,
    )

    assert judge.calls == 1
    assert candidate.editorial_role is EditorialRole.STANDALONE_STORY
    assert candidate.story_state is None
    assert candidate.resolution_state is None
    assert [claim.status for claim in candidate.claims] == [ClaimStatus.UNVERIFIED]
    assert is_creator_review_eligible(candidate) is False
    assert rank_session_map(_map(candidate)).candidate_count == 0


def test_context_expands_only_on_request_and_stops_at_budget() -> None:
    proposal = Proposal(
        proposal_id="prop_0000000000000001",
        start_ms=50_000,
        end_ms=52_000,
        signal_type=ProposalSignalType.GAME_EVENT,
        event_hypothesis="OBJECTIVE_PLANTED",
        confidence=0.95,
        sources=["fixture_game_event"],
    )
    initial = plan_proposal_context(
        proposal,
        180_000,
        pre_context_ms=10_000,
        post_context_ms=10_000,
        expansion_step_ms=20_000,
        max_context_ms=70_000,
    )
    unchanged = expand_if_needed(initial, needs_more_context=False)
    expanded_once = expand_if_needed(initial, needs_more_context=True, direction="after")
    expanded_twice = expand_if_needed(
        expanded_once,
        needs_more_context=True,
        direction="after",
    )
    exhausted = expand_if_needed(
        expanded_twice,
        needs_more_context=True,
        direction="after",
    )

    assert unchanged == initial
    assert initial.context_start_ms == 40_000
    assert initial.context_end_ms == 62_000
    assert expanded_once.context_end_ms == 82_000
    assert expanded_twice.context_end_ms == 102_000
    assert exhausted.context_duration_ms == 70_000
    assert exhausted.exhausted is True
    assert exhausted.anchor_start_ms >= exhausted.context_start_ms
    assert exhausted.anchor_end_ms <= exhausted.context_end_ms


def test_context_planner_clamps_to_source_edges() -> None:
    proposal = Proposal(
        proposal_id="prop_0000000000000002",
        start_ms=2_000,
        end_ms=3_000,
        signal_type=ProposalSignalType.MANUAL_MARKER,
        confidence=1.0,
        sources=["manual"],
    )
    plan = plan_proposal_context(
        proposal,
        20_000,
        pre_context_ms=10_000,
        post_context_ms=5_000,
        max_context_ms=20_000,
    )

    assert plan.context_start_ms == 0
    assert plan.context_end_ms == 8_000
    assert plan.exhausted is False


def test_local_proposals_are_deterministic_factual_anchors_without_creator_scores() -> None:
    signals = LocalSignalsArtifact(
        created_at=NOW,
        producer_version=__version__,
        source_duration_ms=60_000,
        audio_present=True,
        audio_activity=[
            AudioActivityInterval(
                start_ms=1_000,
                end_ms=2_000,
                mean_db=-18.0,
                active=True,
            ),
            AudioActivityInterval(
                start_ms=5_000,
                end_ms=40_000,
                mean_db=-20.0,
                active=True,
            ),
            AudioActivityInterval(
                start_ms=42_000,
                end_ms=43_000,
                mean_db=-60.0,
                active=False,
            ),
        ],
        scene_activity=[TimeInterval(start_ms=10_000, end_ms=12_000)],
    )

    first = proposals_from_local_signals(
        session_id=SESSION_ID,
        source_id=SOURCE_ID,
        signals=signals,
        created_at=NOW,
    )
    second = proposals_from_local_signals(
        session_id=SESSION_ID,
        source_id=SOURCE_ID,
        signals=signals,
        created_at=NOW,
    )

    assert first == second
    assert [item.signal_type for item in first.proposals] == [
        ProposalSignalType.AUDIO_ACTIVITY,
        ProposalSignalType.SCENE_ACTIVITY,
    ]
    assert any("broad audio-activity" in warning for warning in first.warnings)
    for proposal in first.proposals:
        payload = proposal.model_dump(mode="json")
        assert "score" not in payload
        assert "creator_score" not in payload
        assert "editorial_role" not in payload
        assert proposal.event_hypothesis is None
        assert proposal.confidence == 1.0


def test_manual_marker_proposals_are_source_bound_factual_anchors() -> None:
    marker_set = ManualProposalMarkerSet(
        source_sha256="b" * 64,
        source_duration_ms=60_000,
        markers=[
            ManualProposalMarker(
                start_ms=9_800,
                end_ms=10_400,
                label="owner noticed an objective state change",
                event_hypothesis="OBJECTIVE_PLANTED",
            )
        ],
    )
    artifact = proposals_from_manual_markers(
        session_id=SESSION_ID,
        source_id=SOURCE_ID,
        source_sha256="b" * 64,
        source_duration_ms=60_000,
        marker_set=marker_set,
        created_at=NOW,
    )

    assert len(artifact.proposals) == 1
    proposal = artifact.proposals[0]
    assert proposal.signal_type is ProposalSignalType.MANUAL_MARKER
    assert proposal.event_hypothesis == "OBJECTIVE_PLANTED"
    assert proposal.metadata["label"] == "owner noticed an objective state change"
    payload = proposal.model_dump(mode="json")
    assert "creator_score" not in payload
    assert "editorial_role" not in payload

    with pytest.raises(ValueError, match="SHA-256"):
        proposals_from_manual_markers(
            session_id=SESSION_ID,
            source_id=SOURCE_ID,
            source_sha256="c" * 64,
            source_duration_ms=60_000,
            marker_set=marker_set,
            created_at=NOW,
        )


def test_proposal_clustering_reduces_duplicate_neighborhoods_without_merging_conflicts() -> None:
    local = ProposalArtifact(
        created_at=NOW,
        producer_version=__version__,
        session_id=SESSION_ID,
        source_id=SOURCE_ID,
        source_duration_ms=60_000,
        proposals=[
            Proposal(
                proposal_id="prop_0000000000000101",
                start_ms=10_000,
                end_ms=10_800,
                signal_type=ProposalSignalType.AUDIO_ACTIVITY,
                confidence=0.7,
                sources=["local_audio_activity"],
            ),
            Proposal(
                proposal_id="prop_0000000000000102",
                start_ms=30_000,
                end_ms=31_000,
                signal_type=ProposalSignalType.GAME_EVENT,
                event_hypothesis="ROUND_WON",
                confidence=0.9,
                sources=["fixture_game_event"],
            ),
        ],
    )
    manual = ProposalArtifact(
        created_at=NOW,
        producer_version=__version__,
        session_id=SESSION_ID,
        source_id=SOURCE_ID,
        source_duration_ms=60_000,
        proposals=[
            Proposal(
                proposal_id="prop_0000000000000103",
                start_ms=10_500,
                end_ms=11_200,
                signal_type=ProposalSignalType.MANUAL_MARKER,
                event_hypothesis="OBJECTIVE_PLANTED",
                confidence=1.0,
                sources=["manual_marker"],
                metadata={"label": "plant"},
            ),
            Proposal(
                proposal_id="prop_0000000000000104",
                start_ms=30_500,
                end_ms=31_300,
                signal_type=ProposalSignalType.MANUAL_MARKER,
                event_hypothesis="ROUND_LOST",
                confidence=1.0,
                sources=["manual_marker"],
            ),
        ],
    )

    clustered = combine_and_cluster_proposals([local, manual])

    assert len(clustered.proposals) == 3
    first = clustered.proposals[0]
    assert first.start_ms == 10_000
    assert first.end_ms == 11_200
    assert first.signal_type is ProposalSignalType.MANUAL_MARKER
    assert first.event_hypothesis == "OBJECTIVE_PLANTED"
    assert first.sources == ["local_audio_activity", "manual_marker"]
    assert first.metadata["cluster_size"] == "2"
    assert "AUDIO_ACTIVITY" in first.metadata["signal_types"]
    assert "MANUAL_MARKER" in first.metadata["signal_types"]
    assert any("Clustered 4 factual anchors into 3" in warning for warning in clustered.warnings)
    assert [item.event_hypothesis for item in clustered.proposals[1:]] == [
        "ROUND_WON",
        "ROUND_LOST",
    ]


def test_transcript_proposals_are_source_bound_speech_evidence_not_creator_truth() -> None:
    transcript = TranscriptFixture(
        source_sha256="d" * 64,
        source_duration_ms=60_000,
        utterances=[
            TranscriptUtterance(
                start_ms=12_000,
                end_ms=13_200,
                text="wait, what just happened?",
                speaker="owner",
                language="en",
                confidence=0.91,
            )
        ],
        notes=["fixture transcript only"],
    )
    artifact = proposals_from_transcript(
        session_id=SESSION_ID,
        source_id=SOURCE_ID,
        source_sha256="d" * 64,
        source_duration_ms=60_000,
        transcript=transcript,
        created_at=NOW,
    )

    assert len(artifact.proposals) == 1
    proposal = artifact.proposals[0]
    assert proposal.signal_type is ProposalSignalType.ASR_UTTERANCE
    assert proposal.event_hypothesis is None
    assert proposal.metadata["text"] == "wait, what just happened?"
    assert proposal.metadata["speaker"] == "owner"
    assert proposal.metadata["language"] == "en"
    payload = proposal.model_dump(mode="json")
    assert "creator_score" not in payload
    assert "editorial_role" not in payload

    with pytest.raises(ValueError, match="transcript source SHA-256"):
        proposals_from_transcript(
            session_id=SESSION_ID,
            source_id=SOURCE_ID,
            source_sha256="e" * 64,
            source_duration_ms=60_000,
            transcript=transcript,
            created_at=NOW,
        )


def test_proposal_summary_measures_density_and_cluster_reduction() -> None:
    artifact = ProposalArtifact(
        created_at=NOW,
        producer_version=__version__,
        session_id=SESSION_ID,
        source_id=SOURCE_ID,
        source_duration_ms=1_800_000,
        proposals=[
            Proposal(
                proposal_id="prop_0000000000000201",
                start_ms=10_000,
                end_ms=11_000,
                signal_type=ProposalSignalType.MANUAL_MARKER,
                event_hypothesis="OBJECTIVE_PLANTED",
                confidence=1.0,
                sources=["manual_marker", "local_audio_activity"],
                metadata={"cluster_size": "3"},
            ),
            Proposal(
                proposal_id="prop_0000000000000202",
                start_ms=100_000,
                end_ms=101_000,
                signal_type=ProposalSignalType.ASR_UTTERANCE,
                confidence=0.9,
                sources=["transcript_fixture"],
            ),
        ],
    )

    summary = summarize_proposals(artifact)

    assert summary.factual_anchor_count_before_clustering == 4
    assert summary.proposal_neighborhood_count == 2
    assert summary.clustered_reduction_count == 2
    assert summary.multi_source_neighborhood_count == 1
    assert summary.explicit_hypothesis_count == 1
    assert summary.proposals_per_source_hour == 4.0
    assert summary.by_signal_type == {"MANUAL_MARKER": 1, "ASR_UTTERANCE": 1}


def test_proposal_routing_preserves_all_evidence_but_bounds_weak_semantic_inspection() -> None:
    artifact = ProposalArtifact(
        created_at=NOW,
        producer_version=__version__,
        session_id=SESSION_ID,
        source_id=SOURCE_ID,
        source_duration_ms=180_000,
        proposals=[
            Proposal(
                proposal_id="prop_0000000000000301",
                start_ms=5_000,
                end_ms=6_000,
                signal_type=ProposalSignalType.AUDIO_ACTIVITY,
                confidence=1.0,
                sources=["local_audio_activity"],
            ),
            Proposal(
                proposal_id="prop_0000000000000302",
                start_ms=10_000,
                end_ms=11_000,
                signal_type=ProposalSignalType.ASR_UTTERANCE,
                confidence=0.8,
                sources=["transcript_fixture"],
            ),
            Proposal(
                proposal_id="prop_0000000000000303",
                start_ms=70_000,
                end_ms=71_000,
                signal_type=ProposalSignalType.MANUAL_MARKER,
                confidence=1.0,
                sources=["manual_marker"],
            ),
            Proposal(
                proposal_id="prop_0000000000000304",
                start_ms=80_000,
                end_ms=81_000,
                signal_type=ProposalSignalType.SCENE_ACTIVITY,
                confidence=1.0,
                sources=["local_scene_activity"],
            ),
            Proposal(
                proposal_id="prop_0000000000000305",
                start_ms=130_000,
                end_ms=131_000,
                signal_type=ProposalSignalType.ASR_UTTERANCE,
                confidence=0.9,
                sources=["transcript_fixture", "local_audio_activity"],
            ),
            Proposal(
                proposal_id="prop_0000000000000306",
                start_ms=140_000,
                end_ms=141_000,
                signal_type=ProposalSignalType.AUDIO_ACTIVITY,
                confidence=1.0,
                sources=["local_audio_activity"],
            ),
        ],
    )

    routing = route_proposals(
        artifact,
        weak_sample_interval_ms=60_000,
        created_at=NOW,
    )
    routed = selected_proposal_artifact(artifact, routing)
    routes = {decision.proposal_id: decision.route for decision in routing.decisions}

    assert len(artifact.proposals) == 6
    assert routes["prop_0000000000000301"] is ProposalRoute.DEFERRED_WEAK
    assert routes["prop_0000000000000302"] is ProposalRoute.SAMPLED_WEAK
    assert routes["prop_0000000000000303"] is ProposalRoute.MUST_INSPECT
    assert routes["prop_0000000000000304"] is ProposalRoute.DEFERRED_WEAK
    assert routes["prop_0000000000000305"] is ProposalRoute.SUPPORTED
    assert routes["prop_0000000000000306"] is ProposalRoute.DEFERRED_WEAK
    assert routing.route_counts == {
        "MUST_INSPECT": 1,
        "SUPPORTED": 1,
        "SAMPLED_WEAK": 1,
        "DEFERRED_WEAK": 3,
    }
    assert routing.selected_per_source_hour == 60.0
    assert [proposal.proposal_id for proposal in routed.proposals] == [
        "prop_0000000000000302",
        "prop_0000000000000303",
        "prop_0000000000000305",
    ]
    assert len(artifact.proposals) == 6


def test_story_assembly_requires_verified_complete_story_and_preserves_reaction_tail() -> None:
    verified_claim = CandidateClaim(
        claim_type="ROUND_WON",
        status=ClaimStatus.VERIFIED,
        evidence=[
            Evidence(
                type="visual",
                start_ms=20_000,
                end_ms=21_000,
                summary="Terminal round-end evidence is visible.",
                source="resolution_verifier",
            )
        ],
    )
    candidate = _candidate(
        candidate_id="cand_0000000000000010",
        editorial_role=EditorialRole.STANDALONE_STORY,
        story_state=StoryState.COMPLETE,
        resolution_state=ResolutionState.VERIFIED,
        claims=[verified_claim],
    )
    assembly = StoryAssembly(
        candidate_id=candidate.candidate_id,
        setup_start_ms=8_000,
        event_start_ms=10_000,
        event_end_ms=15_000,
        payoff_end_ms=21_000,
        reaction_end_ms=24_000,
        reason="Verified round resolution followed by a creator reaction tail.",
    )

    story = apply_story_assembly(candidate, assembly, source_duration_ms=60_000)

    assert story.kind == "STORY"
    assert story.setup_start_ms == 8_000
    assert story.payoff_end_ms == 21_000
    assert story.reaction_end_ms == 24_000
    assert "c1-story-assembler-v1" in story.normalization_actions

    unresolved = candidate.model_copy(
        update={
            "story_state": StoryState.INCOMPLETE,
            "resolution_state": ResolutionState.UNVERIFIED,
            "claims": [CandidateClaim(claim_type="ROUND_WON", status=ClaimStatus.UNVERIFIED)],
        }
    )
    with pytest.raises(ValueError, match="COMPLETE story state"):
        apply_story_assembly(unresolved, assembly, source_duration_ms=60_000)


def test_provider_free_hybrid_orchestration_expands_verifies_assembles_and_persists(
    tmp_path: Path,
) -> None:
    session_id = "2026-09-09_unknown_aaaaaaaaaaaa"
    proposal = Proposal(
        proposal_id="prop_0000000000000010",
        start_ms=50_000,
        end_ms=52_000,
        signal_type=ProposalSignalType.GAME_EVENT,
        event_hypothesis="OBJECTIVE_PLANTED",
        confidence=0.95,
        sources=["fixture_game_event"],
    )
    proposal_artifact = ProposalArtifact(
        created_at=NOW,
        producer_version=__version__,
        session_id=session_id,
        source_id=SOURCE_ID,
        source_duration_ms=180_000,
        proposals=[proposal],
    )
    first_judgment = SemanticJudgment(
        proposal_id=proposal.proposal_id,
        event_start_ms=49_000,
        event_end_ms=60_000,
        category="CLUTCH",
        editorial_role=EditorialRole.STANDALONE_STORY,
        creator_score=8.5,
        confidence=0.9,
        moment_summary="Objective plant begins a possible late-round story.",
        creator_reason="Potentially useful only if the round actually resolves.",
        claim_hypotheses=["ROUND_WON"],
        needs_more_context=True,
        reason="Round-end state is not visible yet.",
    )
    final_judgment = first_judgment.model_copy(
        update={
            "event_start_ms": 48_000,
            "event_end_ms": 65_000,
            "needs_more_context": False,
            "reason": (
                "The event is localized; terminal outcome still needs "
                "independent verification."
            ),
        }
    )
    candidate_id = deterministic_candidate_id(
        session_id=session_id,
        match_id=None,
        start_ms=48_000,
        end_ms=65_000,
        category="CLUTCH",
    )
    first_verification = CandidateVerification(
        candidate_id=candidate_id,
        story_state=StoryState.INCOMPLETE,
        resolution_state=ResolutionState.UNVERIFIED,
        claims=[CandidateClaim(claim_type="ROUND_WON", status=ClaimStatus.UNVERIFIED)],
        needs_more_context=True,
        reason="Plant is visible, but the terminal round outcome is not yet established.",
    )
    final_verification = CandidateVerification(
        candidate_id=candidate_id,
        story_state=StoryState.COMPLETE,
        resolution_state=ResolutionState.VERIFIED,
        claims=[
            CandidateClaim(
                claim_type="ROUND_WON",
                status=ClaimStatus.VERIFIED,
                evidence=[
                    Evidence(
                        type="visual",
                        start_ms=120_000,
                        end_ms=121_000,
                        summary="Round-end victory state is visible after the remaining fight.",
                        source="fixture_resolution_verifier",
                    )
                ],
            )
        ],
        reason="Terminal round-end evidence verifies the provisional outcome.",
    )
    assembly = StoryAssembly(
        candidate_id=candidate_id,
        setup_start_ms=46_000,
        event_start_ms=48_000,
        event_end_ms=65_000,
        payoff_end_ms=121_000,
        reaction_end_ms=128_000,
        reason="Keep setup through verified resolution and reaction tail.",
    )
    judge = FakeSemanticJudge(
        {proposal.proposal_id: [first_judgment, final_judgment]}
    )
    verifier = FakeResolutionVerifier(
        {candidate_id: [first_verification, final_verification]}
    )

    run = run_provider_free_hybrid_triage(
        proposals=proposal_artifact,
        semantic_judge=judge,
        resolution_verifier=verifier,
        extraction_config=ExtractionConfig(),
        story_assemblies={candidate_id: assembly},
        created_at=NOW,
    )

    assert run.semantic_judge_calls == 2
    assert run.resolution_verifier_calls == 2
    assert len(run.context_history) == 3
    assert len(run.session_map.candidates) == 1
    assert len(run.review_map.candidates) == 1
    reviewed = run.review_map.candidates[0]
    assert reviewed.kind == "STORY"
    assert reviewed.story_state is StoryState.COMPLETE
    assert reviewed.resolution_state is ResolutionState.VERIFIED
    assert reviewed.reaction_end_ms == 128_000
    assert reviewed.clip_start_ms == 41_000
    assert reviewed.clip_end_ms == 133_000
    assert is_creator_review_eligible(reviewed) is True

    paths = session_paths(tmp_path, session_id)
    persisted = persist_hybrid_triage(paths, run)
    assert Path(persisted.run_path).is_file()
    assert Path(persisted.session_map_path).is_file()
    assert Path(persisted.review_map_path).is_file()
    assert load_hybrid_triage(paths) == run


def test_creator_evaluation_corpus_tracks_product_usefulness_separately_from_gt(
    tmp_path: Path,
) -> None:
    session_id = "2026-09-09_unknown_bbbbbbbbbbbb"
    verified_claim = CandidateClaim(
        claim_type="ROUND_WON",
        status=ClaimStatus.VERIFIED,
        evidence=[
            Evidence(
                type="visual",
                start_ms=20_000,
                end_ms=21_000,
                summary="Round-end victory state is visible.",
                source="fixture_resolution_verifier",
            )
        ],
    )
    standalone = _candidate(
        candidate_id="cand_0000000000000011",
        editorial_role=EditorialRole.STANDALONE_STORY,
        story_state=StoryState.COMPLETE,
        resolution_state=ResolutionState.VERIFIED,
        claims=[verified_claim],
    )
    montage = _candidate(
        candidate_id="cand_0000000000000012",
        editorial_role=EditorialRole.MONTAGE_BEAT,
        story_state=StoryState.INCOMPLETE,
        resolution_state=ResolutionState.UNVERIFIED,
        claims=[CandidateClaim(claim_type="ROUND_WON", status=ClaimStatus.UNVERIFIED)],
    )
    review_map = SessionMap(
        created_at=NOW,
        producer_version=__version__,
        canonicalization_version="c1-hybrid-test",
        session_id=session_id,
        source_id=SOURCE_ID,
        duration_ms=60_000,
        candidates=[standalone, montage],
    )
    corpus = CreatorEvaluationCorpus(
        created_at=NOW,
        session_id=session_id,
        source_id=SOURCE_ID,
        source_duration_ms=60_000,
        candidate_reviews=[
            CreatorCandidateReview(
                candidate_id=standalone.candidate_id,
                decision=CreatorDecision.KEEP,
                presented_editorial_role=EditorialRole.STANDALONE_STORY,
                owner_editorial_role=EditorialRole.STANDALONE_STORY,
                desired_start_ms=8_000,
                desired_end_ms=24_000,
                review_time_ms=5_000,
            ),
            CreatorCandidateReview(
                candidate_id=montage.candidate_id,
                decision=CreatorDecision.MAYBE,
                presented_editorial_role=EditorialRole.MONTAGE_BEAT,
                owner_editorial_role=EditorialRole.MONTAGE_BEAT,
                review_time_ms=3_000,
            ),
        ],
        obvious_misses=[
            ObviousCreatorMiss(
                start_ms=40_000,
                end_ms=44_000,
                description="An obvious funny reaction was not surfaced.",
                expected_editorial_role=EditorialRole.STANDALONE_STORY,
                category_hint="REACTION",
            )
        ],
    )

    summary = summarize_creator_evaluation(corpus)
    assert summary.reviewed_count == 2
    assert summary.keep_count == 1
    assert summary.maybe_count == 1
    assert summary.keep_or_maybe_rate == 1.0
    assert summary.standalone_keep_rate == 1.0
    assert summary.montage_useful_rate == 1.0
    assert summary.obvious_miss_count == 1
    assert summary.recorded_owner_review_time_ms == 8_000

    paths = session_paths(tmp_path, session_id)
    persist_creator_evaluation(paths, corpus, review_map)
    assert paths.creator_evaluation_path.is_file()
    assert paths.creator_evaluation_summary_path.is_file()
    assert load_creator_evaluation(paths) == corpus


def test_creator_review_template_requires_completed_owner_decisions(tmp_path: Path) -> None:
    session_id = "2026-09-09_unknown_cccccccccccc"
    montage = _candidate(
        candidate_id="cand_0000000000000013",
        editorial_role=EditorialRole.MONTAGE_BEAT,
        story_state=StoryState.INCOMPLETE,
        resolution_state=ResolutionState.UNVERIFIED,
    ).model_copy(update={"clip_start_ms": 8_000, "clip_end_ms": 18_000})
    review_map = SessionMap(
        created_at=NOW,
        producer_version=__version__,
        canonicalization_version="c1-hybrid-test",
        session_id=session_id,
        source_id=SOURCE_ID,
        duration_ms=60_000,
        candidates=[montage],
    )
    template = create_creator_review_template(review_map, created_at=NOW)

    assert template.candidates[0].decision is None
    with pytest.raises(ValueError, match="unfinished decisions"):
        creator_evaluation_from_template(template)

    completed_row = template.candidates[0].model_copy(
        update={
            "decision": CreatorDecision.KEEP,
            "owner_editorial_role": EditorialRole.MONTAGE_BEAT,
            "review_time_ms": 1_500,
        }
    )
    completed = template.model_copy(update={"candidates": [completed_row]})
    corpus = creator_evaluation_from_template(completed)

    assert corpus.candidate_reviews[0].decision is CreatorDecision.KEEP
    assert corpus.candidate_reviews[0].review_time_ms == 1_500
    paths = session_paths(tmp_path, session_id)
    persist_creator_review_template(paths, completed)
    assert load_creator_review_template(paths) == completed


def test_hybrid_fixture_bundle_runs_full_provider_free_contract() -> None:
    session_id = "2026-09-09_unknown_dddddddddddd"
    proposal = Proposal(
        proposal_id="prop_0000000000000014",
        start_ms=20_000,
        end_ms=22_000,
        signal_type=ProposalSignalType.GAME_EVENT,
        event_hypothesis="CLEAN_SHOT",
        confidence=0.9,
        sources=["fixture_game_event"],
    )
    proposal_artifact = ProposalArtifact(
        created_at=NOW,
        producer_version=__version__,
        session_id=session_id,
        source_id=SOURCE_ID,
        source_duration_ms=60_000,
        proposals=[proposal],
    )
    judgment = SemanticJudgment(
        proposal_id=proposal.proposal_id,
        event_start_ms=20_000,
        event_end_ms=22_000,
        category="SKILL",
        editorial_role=EditorialRole.MONTAGE_BEAT,
        creator_score=7.0,
        confidence=0.9,
        moment_summary="A clean mechanical gameplay beat.",
        creator_reason="Useful as montage material rather than a complete story.",
        claim_hypotheses=[],
        needs_more_context=False,
        reason="The visible action is self-contained as a factual montage beat.",
    )
    candidate_id = deterministic_candidate_id(
        session_id=session_id,
        match_id=None,
        start_ms=20_000,
        end_ms=22_000,
        category="SKILL",
    )
    verification = CandidateVerification(
        candidate_id=candidate_id,
        story_state=StoryState.INCOMPLETE,
        resolution_state=ResolutionState.NOT_APPLICABLE,
        needs_more_context=False,
        reason="This montage beat makes no terminal story claim.",
    )
    fixture = HybridFixtureBundle(
        semantic_judgments={proposal.proposal_id: [judgment]},
        verifications={candidate_id: [verification]},
    )

    run = run_hybrid_fixture_bundle(
        proposals=proposal_artifact,
        fixture=fixture,
        extraction_config=ExtractionConfig(),
        created_at=NOW,
    )

    assert run.semantic_judge_calls == 1
    assert run.resolution_verifier_calls == 1
    assert len(run.review_map.candidates) == 1
    assert run.review_map.candidates[0].editorial_role is EditorialRole.MONTAGE_BEAT
