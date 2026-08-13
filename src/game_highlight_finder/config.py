"""Strict configuration loading with explicit precedence."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError

from game_highlight_finder.errors import ConfigError
from game_highlight_finder.redaction import redact_data
from game_highlight_finder.timezones import configured_timezone


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StorageConfig(StrictModel):
    data_dir: Path = Path("data")


class ToolsConfig(StrictModel):
    ffmpeg_path: Path | None = None
    ffprobe_path: Path | None = None
    probe_timeout_seconds: int = Field(default=120, ge=1, le=3600)
    ffmpeg_timeout_seconds: int = Field(default=7200, ge=1, le=86_400)
    termination_grace_seconds: float = Field(default=5.0, gt=0, le=60)


class LoggingConfig(StrictModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class ProxyConfig(StrictModel):
    max_height: int = Field(default=480, ge=144, le=2160)
    max_width: int = Field(default=854, ge=256, le=3840)
    video_bitrate_kbps: int = Field(default=600, ge=100, le=20_000)
    audio_bitrate_kbps: int = Field(default=64, ge=16, le=512)
    fps: float = Field(default=30.0, gt=0, le=120)
    video_codec: Literal["libx264"] = "libx264"
    audio_codec: Literal["aac"] = "aac"
    preset: Literal["ultrafast", "superfast", "veryfast", "faster", "fast", "medium"] = "veryfast"


class AudioConfig(StrictModel):
    sample_rate_hz: int = Field(default=16_000, ge=8_000, le=96_000)
    channels: Literal[1] = 1
    bitrate_kbps: int = Field(default=64, ge=16, le=512)
    codec: Literal["aac"] = "aac"


class ExtractionConfig(StrictModel):
    """M6 candidate extraction policy.

    All durations are integer seconds at the configuration boundary and are
    converted to integer milliseconds by the extraction planner.  Keeping the
    policy explicit makes the default accurate re-encode auditable while still
    allowing a clearly-labelled stream-copy alternative.
    """

    mode: Literal["accurate", "copy"] = "accurate"
    pre_roll_seconds: int = Field(default=5, ge=0, le=60)
    post_roll_seconds: int = Field(default=5, ge=0, le=60)
    minimum_duration_seconds: int = Field(default=1, ge=1, le=900)
    maximum_duration_seconds: int = Field(default=900, ge=1, le=900)
    video_codec: Literal["libx264"] = "libx264"
    crf: int = Field(default=18, ge=0, le=51)
    preset: Literal["ultrafast", "superfast", "veryfast", "faster", "fast", "medium"] = "medium"
    audio_codec: Literal["aac"] = "aac"
    thumbnail: bool = True
    thumbnail_width: int = Field(default=320, ge=32, le=1920)
    thumbnail_height: int = Field(default=180, ge=32, le=1080)
    accurate_tolerance_ms: int = Field(default=500, ge=0, le=10_000)

    @model_validator(mode="after")
    def duration_bounds_ordered(self) -> ExtractionConfig:
        if self.maximum_duration_seconds < self.minimum_duration_seconds:
            raise ValueError("maximum extraction duration must be at least minimum duration")
        return self


class MediaConfig(StrictModel):
    proxy: ProxyConfig = ProxyConfig()
    audio: AudioConfig = AudioConfig()
    extraction: ExtractionConfig = ExtractionConfig()


class DiskConfig(StrictModel):
    safety_factor: float = Field(default=1.5, ge=1.0, le=10.0)
    minimum_free_bytes: int = Field(default=0, ge=0)


class SilenceConfig(StrictModel):
    noise_db: float = Field(default=-35.0, ge=-100, le=0)
    min_duration_seconds: float = Field(default=0.25, gt=0, le=30)


class LoudnessConfig(StrictModel):
    interval_ms: int = Field(default=500, ge=100, le=10_000)


class SignalsConfig(StrictModel):
    silence: SilenceConfig = SilenceConfig()
    loudness: LoudnessConfig = LoudnessConfig()


class ScoutConfig(StrictModel):
    """Offline Fake Scout defaults plus explicitly opt-in Gemini settings.

    No credential is represented here.  The Google SDK reads ``GEMINI_API_KEY``
    (or the configured environment variable) at the provider boundary, so the
    resolved configuration and session artifacts remain safe to persist.
    """

    backend: Literal["fake", "gemini"] = "fake"
    fixture_path: Path | None = None
    response_max_bytes: int = Field(default=1_048_576, ge=4_096, le=16 * 1024 * 1024)
    schema_version: Literal[1] = 1
    canonicalization_version: str = Field(default="m3-canonical-v1", min_length=1, max_length=64)
    model: str = Field(default="gemini-3.5-flash-lite", min_length=1, max_length=256)
    billing_mode: Literal["standard"] = "standard"
    allow_remote_upload: bool = False
    api_key_env: str = Field(default="GEMINI_API_KEY", min_length=3, max_length=64)
    media_resolution: Literal["low"] = "low"
    max_duration_seconds: int = Field(default=900, ge=1, le=10_800)
    max_output_tokens: int = Field(default=2_048, ge=1, le=65_536)
    thinking_level: Literal["minimal", "low", "medium", "high"] = "minimal"
    # Gemini exposes a qualitative thinking level rather than a numeric hard
    # thinking-token limit through Interactions.  This is therefore a local,
    # conservative reservation allowance used only by the M4 cost gate.
    reserved_thinking_tokens: int = Field(default=1_024, ge=0, le=10_000_000)
    prompt_version: str = Field(default="gemini-scout-v1", min_length=1, max_length=64)
    readiness_timeout_seconds: float = Field(default=120.0, gt=0, le=3_600)
    readiness_poll_initial_seconds: float = Field(default=1.0, gt=0, le=60)
    readiness_poll_max_seconds: float = Field(default=8.0, gt=0, le=120)
    cleanup_retry_limit: int = Field(default=3, ge=0, le=10)
    # M6 bounded overlapping-window policy.  These are intentionally separate
    # from the M5 one-request duration ceiling so old configs remain valid.
    window_duration_seconds: int = Field(default=900, ge=1, le=3_600)
    window_overlap_seconds: int = Field(default=30, ge=0, le=3_599)
    max_windows: int = Field(default=1_024, ge=1, le=10_000)
    window_prompt_version: str = Field(
        default="gemini-scout-window-v1", min_length=1, max_length=64
    )

    @field_validator("api_key_env")
    @classmethod
    def validate_api_key_env(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or not normalized.replace("_", "").isalnum() or not normalized.isupper():
            raise ValueError("api_key_env must be an uppercase environment variable name")
        return normalized

    @model_validator(mode="after")
    def validate_polling(self) -> ScoutConfig:
        if self.readiness_poll_max_seconds < self.readiness_poll_initial_seconds:
            raise ValueError("readiness poll max must be at least the initial delay")
        if self.window_overlap_seconds >= self.window_duration_seconds:
            raise ValueError("window overlap must be shorter than window duration")
        return self


class ReportConfig(StrictModel):
    """Small, deterministic local presentation policy for M7."""

    best_of_limit: int = Field(default=3, ge=1, le=3)
    embed_thumbnails: bool = True
    open_after_generate: bool = False


class CostConfig(StrictModel):
    """Provider-neutral hard-budget settings; no provider is enabled by default."""

    monthly_budget_thb: Decimal = Decimal("100.00")
    budget_timezone: str = "Asia/Bangkok"
    hard_limit: bool = True
    estimate_safety_factor: Decimal = Decimal("1.20")
    pricing_max_age_days: int = Field(default=30, ge=0, le=3650)
    fx_max_age_days: int = Field(default=30, ge=0, le=3650)
    ledger_path: Path | None = None
    pricing_catalog_path: Path | None = None
    fx_snapshot_path: Path | None = None

    @field_validator("monthly_budget_thb", "estimate_safety_factor", mode="before")
    @classmethod
    def strict_decimal(cls, value: object) -> Decimal:
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise ValueError("cost decimal values must be numeric")
        try:
            converted = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("cost decimal values must be valid decimals") from exc
        if not converted.is_finite():
            raise ValueError("cost decimal values must be finite")
        return converted

    @model_validator(mode="after")
    def validate_budget_policy(self) -> CostConfig:
        if self.monthly_budget_thb < 0:
            raise ValueError("monthly budget cannot be negative")
        if self.estimate_safety_factor < 1:
            raise ValueError("estimate safety factor must be at least 1")
        try:
            configured_timezone(self.budget_timezone)
        except Exception as exc:
            raise ValueError("budget timezone must be an installed IANA timezone") from exc
        return self


class AppConfig(StrictModel):
    schema_version: Literal[1] = 1
    storage: StorageConfig = StorageConfig()
    tools: ToolsConfig = ToolsConfig()
    logging: LoggingConfig = LoggingConfig()
    media: MediaConfig = MediaConfig()
    disk: DiskConfig = DiskConfig()
    signals: SignalsConfig = SignalsConfig()
    scout: ScoutConfig = ScoutConfig()
    report: ReportConfig = ReportConfig()
    cost: CostConfig = CostConfig()


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
    "GHF_MONTHLY_BUDGET_THB": ("cost", "monthly_budget_thb"),
    "GHF_BUDGET_TIMEZONE": ("cost", "budget_timezone"),
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
    scout = config.scout.model_copy(
        update={"fixture_path": _resolve_optional_path(config.scout.fixture_path, working_dir)}
    )
    cost = config.cost.model_copy(
        update={
            "ledger_path": _resolve_optional_path(config.cost.ledger_path, working_dir),
            "pricing_catalog_path": _resolve_optional_path(
                config.cost.pricing_catalog_path, working_dir
            ),
            "fx_snapshot_path": _resolve_optional_path(config.cost.fx_snapshot_path, working_dir),
        }
    )
    config = config.model_copy(
        update={
            "storage": config.storage.model_copy(update={"data_dir": data_dir}),
            "tools": tools,
            "scout": scout,
            "cost": cost,
        }
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
