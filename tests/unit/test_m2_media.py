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
    build_concat_mixed_audio_command,
    build_mixed_audio_intermediate_command,
    build_proxy_command,
    build_signal_command,
    compute_proxy_dimensions,
    parse_progress_text,
    run_ffmpeg,
)
from game_highlight_finder.pipeline.local_signals import (
    effective_loudness_interval_ms,
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
    assert "-nostdin" in command
    assert "h264_nvenc" in command
    assert "p4" in command
    audio = build_audio_command(Path("C:/tools/ffmpeg.exe"), source, output, config)
    assert "-map" in audio and "0:a:0" in audio
    assert "-nostdin" in audio
    signal = build_signal_command(Path("C:/tools/ffmpeg.exe"), output, config)
    assert "-nostdin" in signal


def test_cuda_proxy_accelerates_video_without_changing_audio_mapping() -> None:
    base = AppConfig()
    cuda_proxy = base.media.proxy.model_copy(update={"video_acceleration": "cuda"})
    config = base.model_copy(update={"media": base.media.model_copy(update={"proxy": cuda_proxy})})
    source = Path("C:/recordings/à game.mkv")
    mixed = Path("C:/library/tmp/mixed.nut")
    command = build_proxy_command(
        Path("ffmpeg"), source, Path("C:/library/tmp/proxy.mp4"), config,
        has_audio=True, mixed_audio_path=mixed,
    )
    assert command[command.index("-hwaccel") + 1] == "cuda"
    assert command[command.index("-hwaccel_output_format") + 1] == "cuda"
    assert command.index("-hwaccel") < command.index("-i")
    assert "scale_cuda=" in command[command.index("-vf") + 1]
    assert ":format=nv12" in command[command.index("-vf") + 1]
    assert "-pix_fmt" not in command
    assert [command[i + 1] for i, arg in enumerate(command[:-1]) if arg == "-i"] == [
        str(source), str(mixed)
    ]
    assert command[command.index("-af") + 1] == "alimiter=limit=0.90:level=0"
    assert "1:a:0" in command
    assert "-hwaccel" not in build_proxy_command(
        Path("ffmpeg"), source, Path("C:/library/tmp/cpu.mp4"), base, has_audio=False,
    )
    with pytest.raises(ValueError, match="requires h264_nvenc"):
        ProxyConfig(video_codec="libx264", preset="veryfast", video_acceleration="cuda")


def test_analysis_audio_builders_mix_explicit_multitrack_sources_without_dropping_tracks() -> None:
    config = AppConfig()
    source = Path("C:/recordings/multitrack.mkv")
    output = Path("C:/library/proxy/analysis.mp4")
    indexes = (1, 2, 4)

    proxy = build_proxy_command(
        Path("ffmpeg"),
        source,
        output,
        config,
        has_audio=True,
        audio_stream_indexes=indexes,
    )
    audio = build_audio_command(
        Path("ffmpeg"),
        source,
        output,
        config,
        audio_stream_indexes=indexes,
    )

    for command in (proxy, audio):
        assert "-filter_complex" in command
        graph = command[command.index("-filter_complex") + 1]
        assert "[0:1][0:2][0:4]" in graph
        assert "amix=inputs=3:normalize=0:dropout_transition=0" in graph
        assert "alimiter=limit=0.95[analysis_audio]" in graph
        assert "[analysis_audio]" in command
        assert "0:a:0" not in command

    single = build_audio_command(
        Path("ffmpeg"),
        source,
        output,
        config,
        audio_stream_indexes=(3,),
    )
    assert "-filter_complex" not in single
    assert "0:3" in single


def test_long_multitrack_uses_lossless_nut_then_limits_each_derivative() -> None:
    config = AppConfig()
    source = Path("C:/recordings/multitrack.mkv")
    intermediate = Path("C:/library/tmp/mixed.nut")
    proxy_path = Path("C:/library/tmp/proxy.mp4")
    audio_path = Path("C:/library/tmp/analysis.m4a")
    mixed = build_mixed_audio_intermediate_command(
        Path("ffmpeg"), source, intermediate, config, audio_stream_indexes=(1, 2, 4)
    )
    graph = mixed[mixed.index("-filter_complex") + 1]
    assert "[0:1][0:2][0:4]amix=inputs=3:normalize=0:dropout_transition=0" in graph
    assert "alimiter" not in graph
    assert mixed[mixed.index("-c:a") + 1] == "pcm_f32le"
    assert mixed[mixed.index("-f") + 1] == "nut"
    assert str(intermediate) == mixed[-1]

    proxy = build_proxy_command(
        Path("ffmpeg"), source, proxy_path, config,
        has_audio=True, mixed_audio_path=intermediate,
    )
    assert [proxy[i + 1] for i, arg in enumerate(proxy[:-1]) if arg == "-i"] == [
        str(source), str(intermediate)
    ]
    assert "-filter_complex" not in proxy
    assert "1:a:0" in proxy
    assert proxy[proxy.index("-af") + 1] == "alimiter=limit=0.90:level=0"

    analysis = build_audio_command(
        Path("ffmpeg"), intermediate, audio_path, config, mixed_audio_input=True
    )
    assert "-filter_complex" not in analysis
    assert "0:a:0" in analysis
    assert analysis[analysis.index("-af") + 1] == "alimiter=limit=0.90:level=0"
    with pytest.raises(ValueError, match="multiple audio"):
        build_mixed_audio_intermediate_command(
            Path("ffmpeg"), source, intermediate, config, audio_stream_indexes=(1,)
        )


def test_bounded_lossless_mix_and_concat_copy_preserve_audio_contract() -> None:
    config = AppConfig()
    source = Path("C:/recordings/long.mkv")
    chunk = Path("C:/library/tmp/analysis_audio.part_0003.nut")
    command = build_mixed_audio_intermediate_command(
        Path("ffmpeg"), source, chunk, config,
        audio_stream_indexes=(1, 2, 3, 4),
        start_ms=2_700_000,
        duration_ms=900_000,
    )
    assert command[command.index("-ss") + 1] == "2700.000"
    assert command[command.index("-t") + 1] == "900.000"
    assert "-nostdin" in command
    assert "alimiter" not in command[command.index("-filter_complex") + 1]
    assert command[-1] == str(chunk)
    with pytest.raises(ValueError, match="requires a duration"):
        build_mixed_audio_intermediate_command(
            Path("ffmpeg"), source, chunk, config,
            audio_stream_indexes=(1, 2), start_ms=1000,
        )

    concat = build_concat_mixed_audio_command(
        Path("ffmpeg"), chunk.parent / "parts.ffconcat", chunk.parent / "joined.nut"
    )
    assert concat[concat.index("-f") + 1] == "concat"
    assert concat[concat.index("-c:a") + 1] == "copy"
    assert concat[concat.index("-map") + 1] == "0:a:0"
    assert "-filter_complex" not in concat and "-af" not in concat


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


def test_long_vod_loudness_interval_adapts_without_losing_tail() -> None:
    duration_ms = 4 * 60 * 60 * 1000
    interval_ms = effective_loudness_interval_ms(
        duration_ms=duration_ms,
        configured_interval_ms=500,
    )
    assert interval_ms == 720

    command = build_signal_command(
        Path("ffmpeg"),
        Path("analysis_audio.m4a"),
        AppConfig(),
        interval_ms=interval_ms,
    )
    filters = command[command.index("-af") + 1]
    assert "asetnsamples=n=11520:pad=1" in filters

    text = """
    [Parsed_ametadata] frame:0 pts_time:0.0
    [Parsed_ametadata] lavfi.astats.Overall.RMS_level=-12.0
    [Parsed_ametadata] frame:1 pts_time:14399.5
    [Parsed_ametadata] lavfi.astats.Overall.RMS_level=-8.0
    """
    activity, _ = parse_loudness_activity(
        text,
        duration_ms=duration_ms,
        interval_ms=interval_ms,
        active_threshold_db=-35,
    )
    assert activity[0].start_ms == 0
    assert activity[-1].start_ms == 14_399_280
    assert activity[-1].end_ms == duration_ms


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
    _, _, mixed_temporary, mixed_required = estimate_required_bytes(
        100_000_000, 60_000, AppConfig(), lossless_mix_intermediate=True
    )
    assert mixed_temporary >= temporary + (60 * AppConfig().media.audio.sample_rate_hz * 4)
    assert mixed_required > required


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
