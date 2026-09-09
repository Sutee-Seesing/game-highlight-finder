from __future__ import annotations

from game_highlight_finder.domain.models import CandidateClaim, Evidence
from game_highlight_finder.pipeline.hybrid_provider_boundary import (
    SEMANTIC_RESPONSE_SCHEMA,
    VERIFIER_RESPONSE_SCHEMA,
)
from game_highlight_finder.pipeline.semantic_judge import (
    SEMANTIC_JUDGE_VERSION,
    SemanticJudgment,
)
from game_highlight_finder.pipeline.verification import (
    VERIFICATION_VERSION,
    CandidateVerification,
)


def test_semantic_provider_schema_tracks_canonical_contract() -> None:
    properties = SEMANTIC_RESPONSE_SCHEMA["properties"]

    assert set(properties) == set(SemanticJudgment.model_fields)
    assert set(SEMANTIC_RESPONSE_SCHEMA["required"]) == set(SemanticJudgment.model_fields)
    assert properties["version"]["enum"] == [SEMANTIC_JUDGE_VERSION]
    assert SEMANTIC_RESPONSE_SCHEMA["additionalProperties"] is False


def test_verifier_provider_schema_tracks_canonical_nested_contracts() -> None:
    properties = VERIFIER_RESPONSE_SCHEMA["properties"]
    claim_schema = properties["claims"]["items"]
    evidence_schema = claim_schema["properties"]["evidence"]["items"]

    assert set(properties) == set(CandidateVerification.model_fields)
    assert set(VERIFIER_RESPONSE_SCHEMA["required"]) == set(CandidateVerification.model_fields)
    assert properties["version"]["enum"] == [VERIFICATION_VERSION]
    assert set(claim_schema["properties"]) == set(CandidateClaim.model_fields)
    assert set(evidence_schema["properties"]) == set(Evidence.model_fields)
    assert "reason" in claim_schema["properties"]
    assert "strength" in evidence_schema["properties"]
    assert VERIFIER_RESPONSE_SCHEMA["additionalProperties"] is False
