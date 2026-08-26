from __future__ import annotations

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from game_highlight_finder.cli import app
from game_highlight_finder.config import AppConfig

SESSION_ID = "2026-08-26_unknown_aaaaaaaaaaaa"
CANDIDATE_ID = "cand_1111111111111111"
runner = CliRunner()


def _preflight_stub() -> SimpleNamespace:
    quote = SimpleNamespace(reserved_cost_micro_thb=123_456)
    item_preflight = SimpleNamespace(quote=quote)
    item = SimpleNamespace(candidate_id=CANDIDATE_ID, preflight=item_preflight)
    return SimpleNamespace(
        selected_candidate_ids=(CANDIDATE_ID,),
        items=(item,),
        total_reserved_cost_micro_thb=123_456,
        available_micro_thb=9_000_000,
    )


def test_refine_boundaries_defaults_to_preflight_without_real_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_created = False

    def unexpected_transport(*_args: object, **_kwargs: object) -> object:
        nonlocal transport_created
        transport_created = True
        raise AssertionError("preflight must not construct GenAITransport")

    monkeypatch.setattr(
        "game_highlight_finder.cli._load_boundary_refinement_inputs",
        lambda *_args, **_kwargs: (AppConfig(), object(), object(), object()),
    )
    monkeypatch.setattr(
        "game_highlight_finder.cli.preflight_gemini_boundary_refinement_session_batch",
        lambda *_args, **_kwargs: _preflight_stub(),
    )
    monkeypatch.setattr("game_highlight_finder.cli.GenAITransport", unexpected_transport)

    result = runner.invoke(app, ["refine-boundaries", SESSION_ID, CANDIDATE_ID])

    assert result.exit_code == 0, result.output
    assert "provider/API calls: ZERO" in result.output
    assert "Aggregate maximum reserved: ฿0.12" in result.output
    assert "RAW source upload: NO" in result.output
    assert transport_created is False


def test_refine_boundaries_execute_requires_fresh_upload_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = False

    def unexpected_load(*_args: object, **_kwargs: object) -> object:
        nonlocal loaded
        loaded = True
        raise AssertionError("authorization must fail before session/provider work")

    monkeypatch.setattr(
        "game_highlight_finder.cli._load_boundary_refinement_inputs",
        unexpected_load,
    )

    result = runner.invoke(
        app,
        ["refine-boundaries", SESSION_ID, CANDIDATE_ID, "--execute"],
    )

    assert result.exit_code == 2
    assert "requires --allow-remote-upload" in result.output
    assert "Preflight is the default" in result.output
    assert loaded is False


def test_refine_boundaries_execute_wires_fresh_opt_in_and_lazy_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel_transport = object()
    transport_created = 0
    batch_called = 0

    def fake_transport(*, api_key_env: str) -> object:
        nonlocal transport_created
        transport_created += 1
        assert api_key_env == "GEMINI_API_KEY"
        return sentinel_transport

    def fake_batch(
        _source: object,
        _proxy: object,
        _session_map: object,
        config: AppConfig,
        *,
        candidate_ids: list[str],
        transport_factory: object,
        minimum_confidence: float,
    ) -> SimpleNamespace:
        nonlocal batch_called
        batch_called += 1
        assert config.scout.allow_remote_upload is True
        assert candidate_ids == [CANDIDATE_ID]
        assert minimum_confidence == 0.75
        assert callable(transport_factory)
        assert transport_factory() is sentinel_transport
        preflight = SimpleNamespace(total_reserved_cost_micro_thb=200_000)
        artifact = SimpleNamespace(selected_candidate_ids=(CANDIDATE_ID,))
        return SimpleNamespace(
            artifact=artifact,
            preflight=preflight,
            generated_responses=1,
            response_cache_hits=0,
            media_cache_hits=1,
            refined_session_map_path="refined.json",
            artifact_path="batch.json",
        )

    monkeypatch.setattr(
        "game_highlight_finder.cli._load_boundary_refinement_inputs",
        lambda *_args, **_kwargs: (AppConfig(), object(), object(), object()),
    )
    monkeypatch.setattr("game_highlight_finder.cli.GenAITransport", fake_transport)
    monkeypatch.setattr(
        "game_highlight_finder.cli.run_gemini_boundary_refinement_batch_with_transport_factory",
        fake_batch,
    )

    result = runner.invoke(
        app,
        [
            "refine-boundaries",
            SESSION_ID,
            CANDIDATE_ID,
            "--execute",
            "--allow-remote-upload",
            "--minimum-confidence",
            "0.75",
        ],
    )

    assert result.exit_code == 0, result.output
    assert batch_called == 1
    assert transport_created == 1
    assert "Gemini boundary refinement batch completed" in result.output
    assert "Original session_map.json: unchanged" in result.output
