from __future__ import annotations

import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from game_highlight_finder.config import AppConfig, MediaConfig, ProxyConfig, SignalsConfig
from game_highlight_finder.domain.models import (
    AudioActivityInterval,
    LocalSignalsArtifact,
    TimeInterval,
    TimestampMapping,
)
from game_highlight_finder.media.ffmpeg import (
    FFmpegCancelled,
    FFmpegExecutionError,
    FFmpegProgressParser,
    build_audio_command,
    build_proxy_command,
    compute_proxy_dimensions,
    parse_progress_text,
    run_ffmpeg,
)
from game_highlight_finder.pipeline.local_signals import (
    parse_loudness_activity,
    parse_silence_intervals,
)
from game_highlight_finder.storage.preflight import estimate_required_bytes


def test_proxy_dimensions_preserve_aspect_and_even_pixels() -> None:
    assert compute_proxy_dimensions(1920, 1080, max_width=854, max_height=480) == (852, 480)
    assert compute_proxy_dimensions(400, 1200, max_width=854, max_height=480) == (160, 480)
    assert compute_proxy_dimensions(320, 240, max_width=854, max_height=480) == (320, 240)


def test_timestamp_mapping_preserves_nonzero_source_origin() -> None:
    mapping = TimestampMapping(
        source_start_ms=5000,
        proxy_start_ms=0,
        source_duration_ms=2000,
        proxy_duration_ms=2000,
    )
    assert mapping.proxy_to_source_ms(0) == 5000
    assert mapping.proxy_to_source_ms(1250) == 6250
    assert mapping.source_to_proxy_ms(6250) == 1250


def test_ffmpeg_commands_are_argument_arrays_and_preserve_unicode_paths() -> None:
    config = AppConfig()
    source = Path("C:/recordings/à game with spaces.mp4")
    output = Path("C:/library/proxy/analysis proxy.partial.mp4")
    ffmpeg = Path("C:/tools/ffmpeg.exe")
    command = build_proxy_command(ffmpeg, source, output, config, has_audio=True)
    assert command[0] == str(ffmpeg)
    assert str(source) in command
    assert str(output) in command
    assert "shell=True" not in command
    assert "h264_nvenc" in command
    assert "p4" in command
    audio = build_audio_command(Path("C:/tools/ffmpeg.exe"), source, output, config)
    assert "-map" in audio and "0:a:0" in audio


def test_proxy_encoder_defaults_to_nvenc_and_cpu_fallback_is_explicit() -> None:
    default = AppConfig().media.proxy
    assert default.video_codec == "h264_nvenc"
    assert default.preset == "p4"

    cpu = ProxyConfig(video_codec="libx264", preset="veryfast")
    assert cpu.video_codec == "libx264"

    with pytest.raises(ValueError, match="NVENC p1-p7"):
        ProxyConfig(video_codec="h264_nvenc", preset="veryfast")
    with pytest.raises(ValueError, match="x264 speed preset"):
        ProxyConfig(video_codec="libx264", preset="p4")


def test_progress_parser_reports_percent_and_completion() -> None:
    parser = FFmpegProgressParser(duration_ms=10_000)
    assert parser.feed("out_time_us=5000000") is None
    update = parser.feed("progress=continue")
    assert update is not None
    assert update.out_time_ms == 5000
    assert update.percent == 50
    updates = parse_progress_text("out_time_ms=10000000\nprogress=end\n", duration_ms=10_000)
    assert updates[-1].progress == "end"
    assert updates[-1].percent == 100


def test_progress_parser_converts_legacy_out_time_ms_microseconds() -> None:
    updates = parse_progress_text("out_time_ms=5000000\nprogress=continue\n", duration_ms=10_000)

    assert updates[0].out_time_ms == 5000
    assert updates[0].percent == 50


def test_progress_parser_prefers_out_time_us_when_both_fields_are_present() -> None:
    updates = parse_progress_text(
        "out_time_ms=9000000\nout_time_us=5000000\nprogress=continue\n",
        duration_ms=10_000,
    )

    assert updates[0].out_time_ms == 5000
    assert updates[0].percent == 50


def test_progress_parser_supports_textual_timestamp_fallback() -> None:
    updates = parse_progress_text("out_time=00:00:05.250000\nprogress=continue\n")

    assert updates[0].out_time_ms == 5250


def test_progress_end_without_timestamp_reports_completion() -> None:
    updates = parse_progress_text("progress=end\n", duration_ms=10_000)

    assert updates[0].progress == "end"
    assert updates[0].out_time_ms is None
    assert updates[0].percent == 100


def test_progress_parser_ignores_malformed_values_without_crashing() -> None:
    updates = parse_progress_text(
        "out_time_us=not-a-number\nout_time_ms=also-bad\nout_time=bad\nprogress=continue\n"
    )

    assert updates[0].out_time_ms is None
    assert updates[0].percent is None


def test_progress_parser_keeps_only_bounded_machine_fields() -> None:
    parser = FFmpegProgressParser()
    for index in range(10_000):
        assert parser.feed(f"irrelevant_{index}=value") is None
    assert parser.feed("speed=1.2x") is None
    update = parser.feed("progress=end")

    assert update is not None
    assert update.speed == "1.2x"


def test_ffmpeg_runner_reports_abnormal_exit_timeout_and_cancellation() -> None:
    with pytest.raises(FFmpegExecutionError):
        run_ffmpeg([sys.executable, "-c", "import sys; sys.exit(2)"], timeout_seconds=2)
    with pytest.raises(FFmpegExecutionError):
        run_ffmpeg(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            timeout_seconds=0.1,
            termination_grace_seconds=0.1,
        )
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(FFmpegCancelled):
        run_ffmpeg(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            timeout_seconds=2,
            termination_grace_seconds=0.1,
            cancel_event=cancelled,
        )


def test_silence_and_loudness_parsers_clamp_to_source_duration() -> None:
    text = """
    [silencedetect] silence_start: -0.2
    [silencedetect] silence_end: 1.2
    [Parsed_ametadata] frame:0 pts_time:1.0
    [Parsed_ametadata] lavfi.astats.Overall.RMS_level=-12.0
        I:         -18.0 LUFS
    """
    silence = parse_silence_intervals(text, duration_ms=1000)
    assert silence == [TimeInterval(start_ms=0, end_ms=1000)]
    activity, overall = parse_loudness_activity(
        text, duration_ms=1000, interval_ms=500, active_threshold_db=-35
    )
    assert activity
    assert activity[0].active is True
    assert overall == -18


def test_signal_models_reject_malformed_intervals() -> None:
    with pytest.raises(ValueError):
        TimeInterval(start_ms=100, end_ms=100)
    with pytest.raises(ValueError):
        AudioActivityInterval(start_ms=500, end_ms=100, mean_db=-20)
    with pytest.raises(ValueError):
        LocalSignalsArtifact(
            created_at=datetime.now(UTC),
            producer_version="test",
            source_duration_ms=1000,
            audio_present=True,
            silence_intervals=[TimeInterval(start_ms=10, end_ms=10)],
        )


def test_disk_preflight_estimate_is_conservative() -> None:
    proxy, audio, temporary, required = estimate_required_bytes(100_000_000, 60_000, AppConfig())
    assert proxy > 0
    assert audio > 0
    assert temporary == proxy + audio
    assert required > proxy + audio


def test_stage_config_fingerprints_are_relevant_only() -> None:
    base = AppConfig()
    changed_logging = base.model_copy(
        update={"logging": base.logging.model_copy(update={"level": "DEBUG"})}
    )
    changed_proxy = base.model_copy(
        update={
            "media": MediaConfig(proxy=ProxyConfig(video_bitrate_kbps=900), audio=base.media.audio)
        }
    )
    changed_signals = base.model_copy(
        update={
            "signals": SignalsConfig(
                silence=base.signals.silence,
                loudness=base.signals.loudness.model_copy(update={"interval_ms": 1000}),
            )
        }
    )
    from game_highlight_finder.storage.sessions import (
        local_signals_config_fingerprint,
        proxy_config_fingerprint,
    )

    assert proxy_config_fingerprint(base) == proxy_config_fingerprint(changed_logging)
    assert proxy_config_fingerprint(base) != proxy_config_fingerprint(changed_proxy)
    assert local_signals_config_fingerprint(base) == local_signals_config_fingerprint(
        changed_logging
    )
    assert local_signals_config_fingerprint(base) != local_signals_config_fingerprint(
        changed_signals
    )
