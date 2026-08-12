"""Validated source, media-derivative, and stage-manifest models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PersistedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Rational(PersistedModel):
    numerator: int
    denominator: int = Field(gt=0)

    @property
    def value(self) -> float:
        return self.numerator / self.denominator

    @property
    def text(self) -> str:
        return f"{self.numerator}/{self.denominator}"


class VideoStream(PersistedModel):
    index: int = Field(ge=0)
    codec_name: str = Field(min_length=1, max_length=100)
    width: int = Field(gt=0, le=100_000)
    height: int = Field(gt=0, le=100_000)
    pixel_format: str | None = Field(default=None, max_length=100)
    average_frame_rate: Rational | None = None
    real_frame_rate: Rational | None = None
    time_base: Rational | None = None
    duration_ms: int | None = Field(default=None, gt=0)
    start_time_ms: int | None = Field(default=None, ge=0)


class AudioStream(PersistedModel):
    index: int = Field(ge=0)
    codec_name: str = Field(min_length=1, max_length=100)
    channels: int | None = Field(default=None, gt=0, le=128)
    sample_rate_hz: int | None = Field(default=None, gt=0, le=1_000_000)
    language: str | None = Field(default=None, max_length=50)
    time_base: Rational | None = None
    duration_ms: int | None = Field(default=None, gt=0)
    start_time_ms: int | None = Field(default=None, ge=0)


Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class SourceAsset(PersistedModel):
    schema_version: Literal[1] = 1
    created_at: datetime
    producer_version: str
    source_id: str = Field(pattern=r"^src_[0-9a-f]{16}$")
    path: Path
    sha256: Sha256
    size_bytes: int = Field(gt=0)
    mtime_ns: int = Field(gt=0)
    duration_ms: int = Field(gt=0)
    container: str = Field(min_length=1, max_length=500)
    video_stream: VideoStream
    audio_streams: list[AudioStream] = Field(default_factory=list, max_length=128)
    selected_video_stream: int = Field(ge=0)
    selected_audio_stream: int | None = Field(default=None, ge=0)
    timestamp_origin_ms: int | None = Field(default=None, ge=0)
    probe_version: str = Field(min_length=1, max_length=1000)
    warnings: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def selected_streams_exist(self) -> SourceAsset:
        if self.selected_video_stream != self.video_stream.index:
            raise ValueError("selected video stream does not match video_stream.index")
        audio_indexes = {stream.index for stream in self.audio_streams}
        if (
            self.selected_audio_stream is not None
            and self.selected_audio_stream not in audio_indexes
        ):
            raise ValueError("selected audio stream does not exist")
        if not self.path.is_absolute():
            raise ValueError("source path must be absolute")
        return self


class TimestampMapping(PersistedModel):
    """The lossless integer-millisecond transform between source and proxy time."""

    schema_version: Literal[1] = 1
    mapping_version: str = Field(default="source-offset-v1", min_length=1, max_length=100)
    source_start_ms: int
    proxy_start_ms: int = Field(ge=0)
    source_duration_ms: int = Field(gt=0)
    proxy_duration_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def durations_are_sane(self) -> TimestampMapping:
        if self.proxy_start_ms + self.proxy_duration_ms <= self.proxy_start_ms:
            raise ValueError("proxy duration must be positive")
        if self.source_duration_ms <= 0:
            raise ValueError("source duration must be positive")
        return self

    def source_to_proxy_ms(self, source_ms: int) -> int:
        return source_ms - self.source_start_ms + self.proxy_start_ms

    def proxy_to_source_ms(self, proxy_ms: int) -> int:
        return proxy_ms - self.proxy_start_ms + self.source_start_ms


class ProxyMetadata(PersistedModel):
    """Validated metadata for the analysis-only proxy and its source transform."""

    schema_version: Literal[1] = 1
    created_at: datetime
    producer_version: str
    proxy_path: str
    duration_ms: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    video_codec: str = Field(min_length=1, max_length=100)
    audio_present: bool
    audio_codec: str | None = Field(default=None, max_length=100)
    audio_sample_rate_hz: int | None = Field(default=None, gt=0)
    audio_channels: int | None = Field(default=None, gt=0)
    timestamp_mapping: TimestampMapping
    warnings: list[str] = Field(default_factory=list, max_length=100)
    tool_identities: dict[str, str] = Field(default_factory=dict, max_length=20)


class TimeInterval(PersistedModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> TimeInterval:
        if self.end_ms <= self.start_ms:
            raise ValueError("interval end must be greater than start")
        return self


class AudioActivityInterval(TimeInterval):
    mean_db: float | None = Field(default=None, ge=-200, le=20)
    peak_db: float | None = Field(default=None, ge=-200, le=20)
    active: bool = True


class LocalSignalsArtifact(PersistedModel):
    """Versioned, bounded, source-relative local activity signals."""

    schema_version: Literal[1] = 1
    created_at: datetime
    producer_version: str
    source_duration_ms: int = Field(gt=0)
    audio_present: bool
    silence_intervals: list[TimeInterval] = Field(default_factory=list, max_length=20_000)
    audio_activity: list[AudioActivityInterval] = Field(default_factory=list, max_length=20_000)
    scene_activity: list[TimeInterval] = Field(default_factory=list, max_length=20_000)
    overall_loudness_lufs: float | None = Field(default=None, ge=-200, le=20)
    warnings: list[str] = Field(default_factory=list, max_length=100)
    tool_identities: dict[str, str] = Field(default_factory=dict, max_length=20)


class StageStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    BLOCKED_BUDGET = "BLOCKED_BUDGET"
    STALE = "STALE"


class ErrorRecord(PersistedModel):
    category: str
    message: str
    hint: str | None = None
    retryable: bool = False


class ArtifactIdentity(PersistedModel):
    path: str
    sha256: Sha256
    size_bytes: int = Field(ge=0)


class AttemptRecord(PersistedModel):
    run_id: str
    status: StageStatus
    started_at: datetime
    completed_at: datetime | None = None
    error: ErrorRecord | None = None


class StageRecord(PersistedModel):
    # Keep this a string so manifests written by future milestones remain readable.
    stage: str = Field(default="ingest", min_length=1, max_length=100)
    status: StageStatus = StageStatus.PENDING
    cache_key: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    attempts: list[AttemptRecord] = Field(default_factory=list)
    input_artifacts: list[ArtifactIdentity] = Field(default_factory=list)
    output_artifacts: list[ArtifactIdentity] = Field(default_factory=list)
    item_states: dict[str, str] = Field(default_factory=dict, max_length=10_000)
    error: ErrorRecord | None = None
    reason: str | None = None


class Manifest(PersistedModel):
    schema_version: Literal[1] = 1
    created_at: datetime
    updated_at: datetime
    producer_version: str
    session_id: str
    # M1 manifests contain only ``ingest``. M2 adds stages additively when loaded.
    stages: dict[str, StageRecord]


class SessionRecord(PersistedModel):
    schema_version: Literal[1] = 1
    created_at: datetime
    producer_version: str
    session_id: str
    source_id: str
    game_profile: Literal["unknown"] = "unknown"
    title: str
    resolved_config_hash: Sha256
    stage_manifest_path: str


class SourceLocator(PersistedModel):
    schema_version: Literal[1] = 1
    path: Path
    path_key: Sha256
    session_id: str
    source_sha256: Sha256
    size_bytes: int = Field(gt=0)
    mtime_ns: int = Field(gt=0)
    updated_at: datetime


def model_json(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=False)
