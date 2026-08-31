from __future__ import annotations

import pytest

from game_highlight_finder.providers.gemini import (
    GeminiConfigurationError,
    _resolve_gemini_api_key,
)


def test_default_gemini_key_takes_precedence_over_numbered_fallbacks() -> None:
    environ = {
        "GEMINI_API_KEY": "primary",
        "GEMINI_API_KEY1": "first-fallback",
    }

    assert _resolve_gemini_api_key("GEMINI_API_KEY", environ) == "primary"


def test_default_gemini_key_uses_lowest_non_empty_numbered_fallback() -> None:
    environ = {
        "GEMINI_API_KEY10": "tenth",
        "GEMINI_API_KEY2": "second",
        "GEMINI_API_KEY1": "",
        "GEMINI_API_KEY03": "ignored-leading-zero",
        "GEMINI_API_KEY0": "ignored-zero",
    }

    assert _resolve_gemini_api_key("GEMINI_API_KEY", environ) == "second"


def test_custom_gemini_key_name_does_not_fallback_to_default_numbered_pool() -> None:
    with pytest.raises(GeminiConfigurationError, match="CUSTOM_GEMINI_KEY"):
        _resolve_gemini_api_key(
            "CUSTOM_GEMINI_KEY",
            {"GEMINI_API_KEY1": "must-not-be-used"},
        )


def test_default_gemini_key_fails_when_no_credential_is_available() -> None:
    with pytest.raises(GeminiConfigurationError, match=r"GEMINI_API_KEY1\.\.N"):
        _resolve_gemini_api_key("GEMINI_API_KEY", {})
