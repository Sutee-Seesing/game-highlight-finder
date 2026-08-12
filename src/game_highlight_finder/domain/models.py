"""Validated source, media-derivative, and stage-manifest models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


# ---------------------------------------------------------------------------
# M3 canonical domain and Scout contracts
# ---------------------------------------------------------------------------


class CandidateCategory(StrEnum):
    """Controlled generic and first-party game-specific category taxonomy."""

    FUNNY = "FUNNY"
    FAIL = "FAIL"
    CLUTCH = "CLUTCH"
    REACTION = "REACTION"
    SMART_PLAY = "SMART_PLAY"
    FRIEND_MOMENT = "FRIEND_MOMENT"
    WTF_UNEXPECTED = "WTF_UNEXPECTED"
    TENSION_PAYOFF = "TENSION_PAYOFF"
    SKILL = "SKILL"
    OTHER = "OTHER"
    CAMOUFLAGE = "CAMOUFLAGE"
    BOSS_KILL = "BOSS_KILL"
    LOW_HEALTH_ESCAPE = "LOW_HEALTH_ESCAPE"


Category = CandidateCategory


GENERIC_CATEGORIES = frozenset(
    category.value
    for category in CandidateCategory
    if category
    not in {
        CandidateCategory.CAMOUFLAGE,
        CandidateCategory.BOSS_KILL,
        CandidateCategory.LOW_HEALTH_ESCAPE,
    }
)
GAME_SPECIFIC_CATEGORIES = frozenset(
    {
        CandidateCategory.CAMOUFLAGE.value,
        CandidateCategory.BOSS_KILL.value,
        CandidateCategory.LOW_HEALTH_ESCAPE.value,
    }
)


def is_known_category(value: str) -> bool:
    """Return whether a category is controlled by the generic/profile taxonomy.

    Future profiles can add a bounded ``GAME_<PROFILE>_<CATEGORY>`` value through
    their own registry without widening the accepted set to arbitrary strings.
    """

    if value in GENERIC_CATEGORIES or value in GAME_SPECIFIC_CATEGORIES:
        return True
    return (
        value.startswith("GAME_")
        and 6 <= len(value) <= 48
        and all(part.isidentifier() and part.isupper() for part in value.split("_"))
    )


class Evidence(PersistedModel):
    """Compact user-facing evidence; never a chain-of-thought trace."""

    type: str = Field(min_length=1, max_length=64)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, gt=0)
    strength: float | None = Field(default=None, ge=0, le=1)
    summary: str = Field(min_length=1, max_length=240)
    source: str = Field(default="scout", min_length=1, max_length=64)

    @field_validator("start_ms", "end_ms", mode="before")
    @classmethod
    def strict_optional_integer(cls, value: object) -> object:
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError("evidence timestamps must be integer milliseconds")
        return value

    @field_validator("strength", mode="before")
    @classmethod
    def strict_finite_strength(cls, value: object) -> object:
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("evidence strength must be a finite number")
        import math

        if not math.isfinite(float(value)):
            raise ValueError("evidence strength must be finite")
        return value

    @model_validator(mode="after")
    def evidence_interval_is_ordered(self) -> Evidence:
        if self.start_ms is not None and self.end_ms is not None and self.end_ms <= self.start_ms:
            raise ValueError("evidence end must be greater than start")
        return self


class Session(PersistedModel):
    """Canonical session identity used by the M3 session map."""

    schema_version: Literal[1] = 1
    session_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(pattern=r"^src_[0-9a-f]{16}$")
    duration_ms: int = Field(gt=0)
    title: str = Field(default="", max_length=240)
    game_profile: str = Field(default="unknown", pattern=r"^[a-z0-9_\-]{1,64}$")
    recorded_at: datetime | None = None
    warnings: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("duration_ms", mode="before")
    @classmethod
    def strict_duration(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("duration_ms must be an integer")
        return value


class Match(PersistedModel):
    """A canonical match/round interval; zero candidates is valid."""

    match_id: str = Field(pattern=r"^match_[0-9a-f]{16}$")
    ordinal: int | None = Field(default=None, ge=0)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    label: str | None = Field(default=None, max_length=160)
    confidence: float = Field(ge=0, le=1)
    evidence: list[Evidence] = Field(default_factory=list, max_length=32)
    source_window_ids: list[str] = Field(default_factory=list, max_length=32)
    candidate_ids: list[str] = Field(default_factory=list, max_length=10_000)
    warnings: list[str] = Field(default_factory=list, max_length=32)
    metadata: dict[str, str] = Field(default_factory=dict, max_length=32)

    @field_validator("ordinal", "start_ms", "end_ms", mode="before")
    @classmethod
    def strict_match_integer(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("match time and ordinal fields must be integers")
        return value

    @field_validator("confidence", mode="before")
    @classmethod
    def strict_match_confidence(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("match confidence must be a finite number")
        import math

        if not math.isfinite(float(value)):
            raise ValueError("match confidence must be finite")
        return value

    @model_validator(mode="after")
    def match_interval_is_ordered(self) -> Match:
        if self.end_ms <= self.start_ms:
            raise ValueError("match end must be greater than start")
        if len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ValueError("match candidate IDs must be unique")
        return self


class Candidate(PersistedModel):
    """Canonical candidate moment with distinct score and confidence semantics."""

    candidate_id: str = Field(pattern=r"^cand_[0-9a-f]{16}$")
    match_id: str | None = Field(default=None, max_length=128)
    kind: Literal["MOMENT", "STORY"] = "MOMENT"
    category: str = Field(min_length=1, max_length=48)
    event_start_ms: int = Field(ge=0)
    event_end_ms: int = Field(gt=0)
    setup_start_ms: int | None = Field(default=None, ge=0)
    payoff_end_ms: int | None = Field(default=None, gt=0)
    score: float = Field(ge=0, le=10)
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=500)
    evidence: list[Evidence] = Field(default_factory=list, max_length=16)
    source_window_ids: list[str] = Field(default_factory=list, max_length=32)
    related_candidate_ids: list[str] = Field(default_factory=list, max_length=32)
    clip_start_ms: int | None = Field(default=None, ge=0)
    clip_end_ms: int | None = Field(default=None, gt=0)
    normalization_actions: list[str] = Field(default_factory=list, max_length=32)
    metadata: dict[str, str] = Field(default_factory=dict, max_length=32)

    @field_validator(
        "event_start_ms",
        "event_end_ms",
        "setup_start_ms",
        "payoff_end_ms",
        "clip_start_ms",
        "clip_end_ms",
        mode="before",
    )
    @classmethod
    def strict_candidate_integer(cls, value: object) -> object:
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError("candidate timestamps must be integer milliseconds")
        return value

    @field_validator("score", "confidence", mode="before")
    @classmethod
    def strict_candidate_number(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("candidate score/confidence must be finite numbers")
        import math

        if not math.isfinite(float(value)):
            raise ValueError("candidate score/confidence must be finite")
        return value

    @field_validator("category")
    @classmethod
    def controlled_category(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not is_known_category(normalized):
            raise ValueError(f"unknown candidate category: {value!r}")
        return normalized

    @model_validator(mode="after")
    def candidate_intervals_are_ordered(self) -> Candidate:
        if self.event_end_ms <= self.event_start_ms:
            raise ValueError("candidate event end must be greater than start")
        if self.setup_start_ms is not None and self.setup_start_ms > self.event_start_ms:
            raise ValueError("candidate setup must not start after the event")
        if self.payoff_end_ms is not None and self.payoff_end_ms < self.event_end_ms:
            raise ValueError("candidate payoff must include the event")
        if (
            self.clip_start_ms is not None
            and self.clip_end_ms is not None
            and self.clip_end_ms <= self.clip_start_ms
        ):
            raise ValueError("candidate clip end must be greater than start")
        return self


class ScoutEvidence(PersistedModel):
    """Strict, bounded untrusted Scout evidence fragment."""

    type: str = Field(min_length=1, max_length=64)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, gt=0)
    strength: float | None = Field(default=None, ge=0, le=1)
    summary: str = Field(min_length=1, max_length=240)
    source: str = Field(default="fake_scout", min_length=1, max_length=64)

    @field_validator("start_ms", "end_ms", mode="before")
    @classmethod
    def strict_scout_integer(cls, value: object) -> object:
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError("Scout evidence timestamps must be integer milliseconds")
        return value

    @field_validator("strength", mode="before")
    @classmethod
    def strict_scout_strength(cls, value: object) -> object:
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Scout evidence strength must be numeric")
        import math

        if not math.isfinite(float(value)):
            raise ValueError("Scout evidence strength must be finite")
        return value

    @model_validator(mode="after")
    def scout_evidence_ordered(self) -> ScoutEvidence:
        if self.start_ms is not None and self.end_ms is not None and self.end_ms <= self.start_ms:
            raise ValueError("Scout evidence end must be greater than start")
        return self


class ScoutCandidateFragment(PersistedModel):
    """Provider-shaped candidate fragment; IDs are advisory and never authoritative."""

    start_ms: int
    end_ms: int
    category: str = Field(min_length=1, max_length=64)
    score: float
    confidence: float
    reason: str = Field(min_length=1, max_length=500)
    evidence: list[ScoutEvidence] = Field(default_factory=list, max_length=16)
    match_id: str | None = Field(default=None, max_length=128)
    match_index: int | None = Field(default=None, ge=0)
    provider_id: str | None = Field(default=None, max_length=128)
    candidate_id: str | None = Field(default=None, max_length=128)
    setup_start_ms: int | None = Field(default=None, ge=0)
    payoff_end_ms: int | None = Field(default=None, gt=0)

    @field_validator(
        "start_ms", "end_ms", "match_index", "setup_start_ms", "payoff_end_ms", mode="before"
    )
    @classmethod
    def strict_fragment_integer(cls, value: object) -> object:
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError("Scout candidate timestamps/index must be integers")
        return value

    @field_validator("score", "confidence", mode="before")
    @classmethod
    def strict_fragment_number(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Scout candidate score/confidence must be numeric")
        import math

        if not math.isfinite(float(value)):
            raise ValueError("Scout candidate score/confidence must be finite")
        return value


class ScoutMatchFragment(PersistedModel):
    start_ms: int
    end_ms: int
    confidence: float = 0.5
    label: str | None = Field(default=None, max_length=160)
    evidence: list[ScoutEvidence] = Field(default_factory=list, max_length=32)
    candidates: list[ScoutCandidateFragment] = Field(default_factory=list, max_length=2_000)
    provider_id: str | None = Field(default=None, max_length=128)
    match_id: str | None = Field(default=None, max_length=128)
    ordinal: int | None = Field(default=None, ge=0)

    @field_validator("start_ms", "end_ms", "ordinal", mode="before")
    @classmethod
    def strict_fragment_match_integer(cls, value: object) -> object:
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError("Scout match timestamps/index must be integers")
        return value

    @field_validator("confidence", mode="before")
    @classmethod
    def strict_fragment_match_confidence(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Scout match confidence must be numeric")
        import math

        if not math.isfinite(float(value)):
            raise ValueError("Scout match confidence must be finite")
        return value


class ScoutResponse(PersistedModel):
    """Versioned fake/provider response contract at the trust boundary."""

    schema_version: Literal[1] = 1
    source_duration_ms: int = Field(gt=0)
    time_basis: Literal["source_relative", "window_relative"] = "source_relative"
    window_start_ms: int = Field(default=0, ge=0)
    window_end_ms: int | None = Field(default=None, gt=0)
    matches: list[ScoutMatchFragment] = Field(default_factory=list, max_length=256)
    candidates: list[ScoutCandidateFragment] = Field(default_factory=list, max_length=10_000)
    warnings: list[str] = Field(default_factory=list, max_length=100)
    metadata: dict[str, str] = Field(default_factory=dict, max_length=32)

    @field_validator("source_duration_ms", "window_start_ms", "window_end_ms", mode="before")
    @classmethod
    def strict_response_integer(cls, value: object) -> object:
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError("Scout response time fields must be integers")
        return value


ScoutCandidate = ScoutCandidateFragment
ScoutMatch = ScoutMatchFragment


class SessionMap(PersistedModel):
    """Versioned canonical Session -> Match -> Candidate map."""

    schema_version: Literal[1] = 1
    created_at: datetime
    producer_version: str
    canonicalization_version: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(pattern=r"^src_[0-9a-f]{16}$")
    duration_ms: int = Field(gt=0)
    game_profile: str = Field(default="unknown", pattern=r"^[a-z0-9_\-]{1,64}$")
    matches: list[Match] = Field(default_factory=list, max_length=256)
    candidates: list[Candidate] = Field(default_factory=list, max_length=10_000)
    best_of_candidate_ids: list[str] = Field(default_factory=list, max_length=100)
    statistics: dict[str, int | float | str] = Field(default_factory=dict, max_length=32)
    warnings: list[str] = Field(default_factory=list, max_length=100)
    scout_backend: str = Field(default="fake", min_length=1, max_length=64)
    scout_metadata: dict[str, str] = Field(default_factory=dict, max_length=32)

    @field_validator("duration_ms", mode="before")
    @classmethod
    def strict_map_duration(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("session map duration must be integer milliseconds")
        return value

    @property
    def source_duration_ms(self) -> int:
        return self.duration_ms

    @model_validator(mode="after")
    def references_are_consistent(self) -> SessionMap:
        match_ids = {match.match_id for match in self.matches}
        candidate_ids = {candidate.candidate_id for candidate in self.candidates}
        if len(match_ids) != len(self.matches):
            raise ValueError("session map match IDs must be unique")
        if len(candidate_ids) != len(self.candidates):
            raise ValueError("session map candidate IDs must be unique")
        known_candidate_ids = set(candidate_ids)
        previous_end = -1
        for match in sorted(self.matches, key=lambda item: (item.start_ms, item.end_ms)):
            if match.start_ms < 0 or match.end_ms > self.duration_ms:
                raise ValueError("match interval exceeds session duration")
            if match.start_ms < previous_end:
                raise ValueError("canonical match intervals must not overlap")
            previous_end = match.end_ms
            if not set(match.candidate_ids).issubset(known_candidate_ids):
                raise ValueError("match references an unknown candidate")
        for candidate in self.candidates:
            if candidate.match_id is not None and candidate.match_id not in match_ids:
                raise ValueError("candidate references an unknown match")
            if candidate.event_end_ms > self.duration_ms:
                raise ValueError("candidate event exceeds session duration")
            if candidate.setup_start_ms is not None and candidate.setup_start_ms > self.duration_ms:
                raise ValueError("candidate setup exceeds session duration")
            if candidate.payoff_end_ms is not None and candidate.payoff_end_ms > self.duration_ms:
                raise ValueError("candidate payoff exceeds session duration")
            if candidate.clip_start_ms is not None and candidate.clip_start_ms > self.duration_ms:
                raise ValueError("candidate clip start exceeds session duration")
            if candidate.clip_end_ms is not None and candidate.clip_end_ms > self.duration_ms:
                raise ValueError("candidate clip exceeds session duration")
            if candidate.match_id is not None:
                match = next(item for item in self.matches if item.match_id == candidate.match_id)
                if candidate.candidate_id not in match.candidate_ids:
                    raise ValueError("candidate is not listed by its match")
        if not set(self.best_of_candidate_ids).issubset(candidate_ids):
            raise ValueError("best-of list references an unknown candidate")
        if len(set(self.best_of_candidate_ids)) != len(self.best_of_candidate_ids):
            raise ValueError("best-of candidate IDs must be unique")
        return self


class ProviderRun(PersistedModel):
    """Provider-neutral metadata for a Scout attempt; no secrets are persisted."""

    provider_run_id: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    stage: Literal["scout"] = "scout"
    session_id: str = Field(min_length=1, max_length=128)
    schema_version: int = Field(ge=1, le=100)
    prompt_version: str = Field(min_length=1, max_length=64)
    request_fingerprint: Sha256
    started_at: datetime
    completed_at: datetime | None = None
    response_artifact: str = Field(min_length=1, max_length=300)
    status: StageStatus
