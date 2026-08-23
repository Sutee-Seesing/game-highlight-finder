from __future__ import annotations

from pathlib import Path

import pytest

from game_highlight_finder.config import config_hash, config_payload, load_config
from game_highlight_finder.errors import ConfigError
from game_highlight_finder.redaction import REDACTED, redact_data, redact_text


def test_config_defaults_are_resolved(tmp_path: Path) -> None:
    result = load_config(environ={}, cwd=tmp_path)

    assert result.config.schema_version == 1
    assert result.config.storage.data_dir == (tmp_path / "data").resolve()
    assert result.config.tools.probe_timeout_seconds == 120
    assert result.config.scout.window_prompt_version == "gemini-scout-window-v17"
    assert result.source_file is None


def test_invalid_config_value_fails(tmp_path: Path) -> None:
    config = tmp_path / "bad.yaml"
    config.write_text("tools:\n  probe_timeout_seconds: 0\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="validation failed"):
        load_config(config, environ={})


def test_unknown_config_key_fails(tmp_path: Path) -> None:
    config = tmp_path / "bad.yaml"
    config.write_text("surprise: true\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="validation failed"):
        load_config(config, environ={})


def test_environment_overrides_file_and_cli_overrides_environment(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("storage:\n  data_dir: from-file\n", encoding="utf-8")
    environment_dir = tmp_path / "from-environment"
    cli_dir = tmp_path / "from-cli"

    result = load_config(
        config,
        environ={"GHF_DATA_DIR": str(environment_dir), "IGNORED_UNKNOWN": "value"},
        cli_overrides={"storage.data_dir": cli_dir},
    )

    assert result.config.storage.data_dir == cli_dir.resolve()
    assert result.applied_environment == ("GHF_DATA_DIR",)
    assert result.applied_cli == ("storage.data_dir",)


def test_relative_data_dir_anchors_to_config_file(tmp_path: Path) -> None:
    config_dir = tmp_path / "nested"
    config_dir.mkdir()
    config = config_dir / "config.yaml"
    config.write_text("storage:\n  data_dir: library\n", encoding="utf-8")

    result = load_config(config, environ={}, cwd=tmp_path)

    assert result.config.storage.data_dir == (config_dir / "library").resolve()


def test_config_hash_is_deterministic(tmp_path: Path) -> None:
    first = load_config(environ={}, cwd=tmp_path).config
    second = load_config(environ={}, cwd=tmp_path).config

    assert config_hash(first) == config_hash(second)
    assert len(config_hash(first)) == 64
    assert config_payload(first)["storage"]["data_dir"] == str((tmp_path / "data").resolve())


def test_secret_redaction_is_recursive_and_text_safe() -> None:
    payload = {
        "api_key": "secret-value",
        "nested": {"PASSWORD": "hunter2", "safe": "visible"},
        "items": [{"access_token": "token-value"}],
    }

    redacted = redact_data(payload)

    assert redacted["api_key"] == REDACTED
    assert redacted["nested"]["PASSWORD"] == REDACTED
    assert redacted["nested"]["safe"] == "visible"
    assert redacted["items"][0]["access_token"] == REDACTED
    assert redact_text("GEMINI_API_KEY=abc123 next") == f"GEMINI_API_KEY={REDACTED} next"
