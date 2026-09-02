from __future__ import annotations

from pathlib import Path

import pytest

from game_highlight_finder.errors import ValidationError
from game_highlight_finder.pipeline.hybrid_judge import (
    FakeHybridJudge,
    build_hybrid_judge_prompt,
    build_hybrid_judge_request,
    hybrid_judge_schema,
    parse_hybrid_judge_response,
    response_to_candidates,
    run_fake_hybrid_judge_batch,
)
from game_highlight_finder.pipeline.hybrid_proposals import (
    HybridAnchor,
    HybridProposal,
    HybridProposalPlan,
    HybridProposalPolicy,
    HybridProposalPreparation,
    PreparedHybridProposal,
)
from game_highlight_finder.storage.hashing import hash_file

SESSION_ID = "2026-09-02_unknown_aaaaaaaaaaaa"
SOURCE_ID = "src_" + "b" * 16


def _proposal(
    proposal_id: str,
    *,
    start_ms: int,
    end_ms: int,
    anchor_ms: int,
) -> HybridProposal:
    return HybridProposal(
        proposal_id=proposal_id,
        start_ms=start_ms,
        end_ms=end_ms,
        anchors=[
            HybridAnchor(
                anchor_ms=anchor_ms,
                audio_percentile=0.95,
                motion_percentile=0.85,
                fused_score=1.15,
                selected_by="FUSED",
            )
        ],
    )


def _preparation(tmp_path: Path, proposals: list[HybridProposal]) -> HybridProposalPreparation:
    prepared: list[PreparedHybridProposal] = []
    for proposal in proposals:
        item_dir = tmp_path / proposal.proposal_id
        item_dir.mkdir(parents=True)
        media = item_dir / "analysis_proposal.mp4"
        media.write_bytes(f"media-{proposal.proposal_id}".encode())
        prepared.append(
            PreparedHybridProposal(
                proposal=proposal,
                proxy_path=media,
                proxy_sha256=hash_file(media),
                cache_hit=False,
            )
        )
    intervals = sorted((item.start_ms, item.end_ms) for item in proposals)
    total = 0
    if intervals:
        current_start, current_end = intervals[0]
        for start, end in intervals[1:]:
            if start <= current_end:
                current_end = max(current_end, end)
            else:
                total += current_end - current_start
                current_start, current_end = start, end
        total += current_end - current_start
    plan = HybridProposalPlan(
        session_id=SESSION_ID,
        source_id=SOURCE_ID,
        source_duration_ms=100_000,
        parent_proxy_sha256="c" * 64,
        local_signals_sha256="d" * 64,
        policy=HybridProposalPolicy(),
        anchors=[anchor for proposal in proposals for anchor in proposal.anchors],
        proposals=proposals,
        total_proposed_duration_ms=total,
        proposal_ratio=total / 100_000,
        plan_hash="e" * 64,
    )
    return HybridProposalPreparation(
        plan=plan,
        plan_path=tmp_path / "plan.json",
        motion_path=tmp_path / "motion.v1.json",
        motion_cache_hit=False,
        prepared=tuple(prepared),
        cache_hits=0,
        generated=len(prepared),
    )


def _keep_response(start_ms: int, end_ms: int) -> dict[str, object]:
    return {
        "decision": "KEEP",
        "summary": "visible multi-kill sequence worth reviewing",
        "events": [
            {
                "event_start_ms": start_ms,
                "event_end_ms": end_ms,
                "category": "skill",
                "score": 8.5,
                "confidence": 0.9,
                "reason": "player visibly wins a rapid fight",
                "visible_evidence": ["two visible eliminations in one engagement"],
            }
        ],
    }


def test_hybrid_judge_prompt_and_schema_keep_navigation_nonsemantic() -> None:
    proposal = _proposal(
        "proposal_1111111111111111",
        start_ms=10_000,
        end_ms=30_000,
        anchor_ms=18_000,
    )

    prompt = build_hybrid_judge_prompt(proposal)
    schema = hybrid_judge_schema()

    assert "navigation hints only" in prompt
    assert "do not cite loudness alone" in prompt
    assert "score on a 0-10 scale" in prompt
    assert "confidence on a 0-1 scale" in prompt
    assert "proposal-relative" in prompt
    assert "KEEP" in prompt and "REJECT" in prompt and "UNCERTAIN" in prompt
    assert schema["additionalProperties"] is False
    assert schema["properties"]["decision"]["enum"] == ["KEEP", "REJECT", "UNCERTAIN"]
    assert schema["properties"]["events"]["maxItems"] == 4


def test_response_contract_rejects_inconsistent_decisions_and_out_of_bounds() -> None:
    with pytest.raises(ValidationError, match="strict contract"):
        parse_hybrid_judge_response(
            {"decision": "KEEP", "summary": "missing event", "events": []},
            proposal_duration_ms=20_000,
        )
    with pytest.raises(ValidationError, match="strict contract"):
        parse_hybrid_judge_response(
            {
                "decision": "REJECT",
                "summary": "must not emit event",
                "events": _keep_response(1_000, 2_000)["events"],
            },
            proposal_duration_ms=20_000,
        )
    with pytest.raises(ValidationError, match="exceeds"):
        parse_hybrid_judge_response(_keep_response(10_000, 20_001), proposal_duration_ms=20_000)


def test_keep_maps_proposal_relative_event_to_source_candidate(tmp_path: Path) -> None:
    proposal = _proposal(
        "proposal_2222222222222222",
        start_ms=50_000,
        end_ms=75_000,
        anchor_ms=60_000,
    )
    preparation = _preparation(tmp_path, [proposal])
    request = build_hybrid_judge_request(preparation, preparation.prepared[0])
    response = parse_hybrid_judge_response(
        _keep_response(5_000, 12_000),
        proposal_duration_ms=request.proposal_duration_ms,
    )

    candidates = response_to_candidates(request, response)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert (candidate.event_start_ms, candidate.event_end_ms) == (55_000, 62_000)
    assert candidate.category == "SKILL"
    assert candidate.source_window_ids == [proposal.proposal_id]
    assert candidate.metadata["hybrid_judge"] == "hybrid-judge-v1"
    assert candidate.evidence[0].source == "hybrid_judge"


def test_uncertain_is_preserved_but_not_promoted_to_candidate(tmp_path: Path) -> None:
    proposal = _proposal(
        "proposal_3333333333333333",
        start_ms=0,
        end_ms=20_000,
        anchor_ms=8_000,
    )
    preparation = _preparation(tmp_path, [proposal])
    request = build_hybrid_judge_request(preparation, preparation.prepared[0])
    response = parse_hybrid_judge_response(
        {
            "decision": "UNCERTAIN",
            "summary": "real fight but payoff is unclear",
            "events": _keep_response(5_000, 9_000)["events"],
        },
        proposal_duration_ms=request.proposal_duration_ms,
    )

    assert response.decision == "UNCERTAIN"
    assert len(response.events) == 1
    assert response_to_candidates(request, response) == ()


def test_request_rejects_tampered_proposal_media(tmp_path: Path) -> None:
    proposal = _proposal(
        "proposal_4444444444444444",
        start_ms=10_000,
        end_ms=30_000,
        anchor_ms=20_000,
    )
    preparation = _preparation(tmp_path, [proposal])
    preparation.prepared[0].proxy_path.write_bytes(b"tampered")

    with pytest.raises(ValidationError, match="hash"):
        build_hybrid_judge_request(preparation, preparation.prepared[0])


def test_fake_batch_caches_and_dedupes_overlapping_proposal_judgments(tmp_path: Path) -> None:
    first = _proposal(
        "proposal_5555555555555555",
        start_ms=0,
        end_ms=30_000,
        anchor_ms=15_000,
    )
    second = _proposal(
        "proposal_6666666666666666",
        start_ms=10_000,
        end_ms=40_000,
        anchor_ms=20_000,
    )
    preparation = _preparation(tmp_path, [first, second])
    fake = FakeHybridJudge(
        {
            first.proposal_id: _keep_response(12_000, 18_000),
            second.proposal_id: _keep_response(2_500, 8_500),
        }
    )

    result = run_fake_hybrid_judge_batch(preparation, fake)

    assert result.provider_calls == 0
    assert fake.calls == [first.proposal_id, second.proposal_id]
    assert len(result.proposal_results) == 2
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert (candidate.event_start_ms, candidate.event_end_ms) == (12_000, 18_500)
    assert candidate.source_window_ids == [first.proposal_id, second.proposal_id]

    unused = FakeHybridJudge({})
    cached = run_fake_hybrid_judge_batch(preparation, unused)
    assert unused.calls == []
    assert all(item.cache_hit for item in cached.proposal_results)
    assert cached.candidates == result.candidates


def test_reject_proposal_stays_in_audit_result_without_candidate(tmp_path: Path) -> None:
    proposal = _proposal(
        "proposal_7777777777777777",
        start_ms=10_000,
        end_ms=30_000,
        anchor_ms=20_000,
    )
    preparation = _preparation(tmp_path, [proposal])
    fake = FakeHybridJudge(
        {
            proposal.proposal_id: {
                "decision": "REJECT",
                "summary": "only traversal and weapon movement",
                "events": [],
            }
        }
    )

    result = run_fake_hybrid_judge_batch(preparation, fake)

    assert result.provider_calls == 0
    assert result.candidates == ()
    assert result.proposal_results[0].response.decision == "REJECT"
    assert result.proposal_results[0].response_path.is_file()
