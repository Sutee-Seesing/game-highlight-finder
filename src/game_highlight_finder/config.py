"""Strict configuration loading with explicit precedence."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from game_highlight_finder.errors import ConfigError
from game_highlight_finder.redaction import redact_data


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StorageConfig(StrictModel):
    data_dir: Path = Path("data")


class ToolsConfig(StrictModel):
    ffmpeg_path: Path | None = None
    ffprobe_path: Path | None = None
    probe_timeout_seconds: int = Field(default=120, ge=1, le=3600)


class LoggingConfig(StrictModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class AppConfig(StrictModel):
    schema_version: Literal[1] = 1
    storage: StorageConfig = StorageConfig()
    tools: ToolsConfig = ToolsConfig()
    logging: LoggingConfig = LoggingConfig()


class ConfigResult(StrictModel):
    config: AppConfig
    source_file: Path | None
    applied_environment: tuple[str, ...] = ()
    applied_cli: tuple[str, ...] = ()


ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "GHF_DATA_DIR": ("storage", "data_dir"),
    "GHF_FFMPEG_PATH": ("tools", "ffmpeg_path"),
    "GHF_FFPROBE_PATH": ("tools", "ffprobe_path"),
    "GHF_LOG_LEVEL": ("logging", "level"),
}


def _deep_merge(base: dict[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    for key, value in update.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _set_nested(target: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = target
    for part in path[:-1]:
        node = node.setdefault(part, {})
    node[path[-1]] = value


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read configuration file: {path}", hint=str(exc)) from exc
    try:
        loaded = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in configuration file: {path}", hint=str(exc)) from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError("Configuration root must be a mapping/object.")
    return loaded


def load_config(
    config_path: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
    cwd: Path | None = None,
) -> ConfigResult:
    """Load defaults < YAML < approved environment < CLI overrides."""

    env = os.environ if environ is None else environ
    working_dir = (cwd or Path.cwd()).resolve()
    source_file: Path | None = None
    if config_path is not None:
        source_file = config_path.expanduser().resolve()
        if not source_file.is_file():
            raise ConfigError(f"Configuration file does not exist: {source_file}")
    else:
        candidate = working_dir / "config.yaml"
        if candidate.is_file():
            source_file = candidate.resolve()

    merged = AppConfig().model_dump(mode="python")
    if source_file is not None:
        _deep_merge(merged, _read_yaml(source_file))

    applied_environment: list[str] = []
    for variable, path in ENV_OVERRIDES.items():
        value = env.get(variable)
        if value is not None and value != "":
            _set_nested(merged, path, value)
            applied_environment.append(variable)

    applied_cli: list[str] = []
    for dotted_key, value in (cli_overrides or {}).items():
        if value is None:
            continue
        parts = tuple(dotted_key.split("."))
        _set_nested(merged, parts, value)
        applied_cli.append(dotted_key)

    try:
        config = AppConfig.model_validate(merged)
    except PydanticValidationError as exc:
        raise ConfigError("Configuration validation failed.", hint=str(exc)) from exc

    data_dir = config.storage.data_dir.expanduser()
    if not data_dir.is_absolute():
        anchor = source_file.parent if source_file is not None else working_dir
        data_dir = (anchor / data_dir).resolve()
    tools = config.tools.model_copy(
        update={
            "ffmpeg_path": _resolve_optional_path(config.tools.ffmpeg_path, working_dir),
            "ffprobe_path": _resolve_optional_path(config.tools.ffprobe_path, working_dir),
        }
    )
    config = config.model_copy(
        update={"storage": config.storage.model_copy(update={"data_dir": data_dir}), "tools": tools}
    )
    return ConfigResult(
        config=config,
        source_file=source_file,
        applied_environment=tuple(applied_environment),
        applied_cli=tuple(applied_cli),
    )


def _resolve_optional_path(value: Path | None, anchor: Path) -> Path | None:
    if value is None:
        return None
    expanded = value.expanduser()
    return expanded.resolve() if expanded.is_absolute() else (anchor / expanded).resolve()


def config_payload(config: AppConfig, *, redacted: bool = True) -> dict[str, Any]:
    payload = config.model_dump(mode="json")
    return redact_data(payload) if redacted else payload


def config_hash(config: AppConfig) -> str:
    canonical = json.dumps(
        config_payload(config, redacted=True), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
