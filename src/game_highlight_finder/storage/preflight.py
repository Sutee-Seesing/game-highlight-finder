"""Conservative storage checks before local media generation."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from game_highlight_finder.config import AppConfig
from game_highlight_finder.errors import StorageError


@dataclass(frozen=True)
class DiskSpaceEstimate:
    proxy_bytes: int
    audio_bytes: int
    temporary_bytes: int
    required_bytes: int
    available_bytes: int

    @property
    def sufficient(self) -> bool:
        return self.available_bytes >= self.required_bytes


def estimate_required_bytes(
    source_size_bytes: int,
    duration_ms: int,
    config: AppConfig,
) -> tuple[int, int, int, int]:
    """Estimate outputs plus a duplicate temporary workspace, conservatively."""

    duration_seconds = max(1.0, duration_ms / 1000.0)
    proxy_by_bitrate = (
        duration_seconds
        * (config.media.proxy.video_bitrate_kbps + config.media.proxy.audio_bitrate_kbps)
        * 1000
        / 8
    )
    audio_by_bitrate = duration_seconds * config.media.audio.bitrate_kbps * 1000 / 8
    # Source-size floors protect against unusual codecs whose decoded complexity is high.
    proxy_bytes = max(int(proxy_by_bitrate), int(source_size_bytes * 0.05), 1 << 20)
    audio_bytes = max(int(audio_by_bitrate), int(duration_seconds * 1024), 64 * 1024)
    temporary_bytes = proxy_bytes + audio_bytes
    required = int((proxy_bytes + audio_bytes + temporary_bytes) * config.disk.safety_factor)
    required = max(required, config.disk.minimum_free_bytes)
    return proxy_bytes, audio_bytes, temporary_bytes, required


def check_disk_space(
    output_root: Path,
    *,
    source_size_bytes: int,
    duration_ms: int,
    config: AppConfig,
) -> DiskSpaceEstimate:
    proxy_bytes, audio_bytes, temporary_bytes, required = estimate_required_bytes(
        source_size_bytes, duration_ms, config
    )
    try:
        available = shutil.disk_usage(output_root).free
    except OSError as exc:
        raise StorageError(
            "Could not determine available disk space before media generation.", hint=str(exc)
        ) from exc
    estimate = DiskSpaceEstimate(
        proxy_bytes=proxy_bytes,
        audio_bytes=audio_bytes,
        temporary_bytes=temporary_bytes,
        required_bytes=required,
        available_bytes=available,
    )
    if not estimate.sufficient:
        raise StorageError(
            "Insufficient disk space for M2 media artifacts.",
            hint=(
                f"Estimated temporary/output requirement: {_gib(required):.2f} GiB; "
                f"Available: {_gib(available):.2f} GiB."
            ),
        )
    return estimate


def _gib(value: int) -> float:
    return value / (1024**3)
