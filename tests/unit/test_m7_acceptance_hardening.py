from __future__ import annotations

import pytest

from game_highlight_finder.cli import _echo_execution_activity
from game_highlight_finder.pipeline.windowed_scout import ExecutionActivity


def test_fake_activity_is_the_only_unconditional_zero_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _echo_execution_activity(ExecutionActivity(scout_backend="fake"))
    assert capsys.readouterr().out.strip() == "Real Gemini API calls: ZERO"


def test_cached_gemini_activity_reports_zero_new_generations(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _echo_execution_activity(
        ExecutionActivity(scout_backend="gemini", provider_generation_calls=0, cache_hits=2)
    )
    output = capsys.readouterr().out
    assert "Gemini generation calls this run: 0" in output
    assert "Real Gemini API calls: ZERO" not in output


def test_gemini_generation_activity_never_claims_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _echo_execution_activity(
        ExecutionActivity(
            scout_backend="gemini",
            provider_generation_calls=2,
            provider_uploads=2,
            paid_reservations_created=2,
        )
    )
    output = capsys.readouterr().out
    assert "Gemini generation calls this run: 2" in output
    assert "Real Gemini API calls: ZERO" not in output
