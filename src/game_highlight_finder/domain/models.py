"""Validated M1 source, session, and manifest models."""

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
    stage: Literal["ingest"] = "ingest"
    status: StageStatus = StageStatus.PENDING
    cache_key: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    attempts: list[AttemptRecord] = Field(default_factory=list)
    input_artifacts: list[ArtifactIdentity] = Field(default_factory=list)
    output_artifacts: list[ArtifactIdentity] = Field(default_factory=list)
    error: ErrorRecord | None = None
    reason: str | None = None


class Manifest(PersistedModel):
    schema_version: Literal[1] = 1
    created_at: datetime
    updated_at: datetime
    producer_version: str
    session_id: str
    stages: dict[Literal["ingest"], StageRecord]


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
