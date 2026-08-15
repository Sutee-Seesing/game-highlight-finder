from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from game_highlight_finder.benchmark.models import (
    BenchmarkAnnotations,
    BenchmarkCase,
    BenchmarkDataset,
    BenchmarkSplit,
)
from game_highlight_finder.benchmark.review_proxy import (
    DEFAULT_REVIEW_PROFILE,
    EncoderCapability,
    MediaProbe,
    build_review_proxy_command,
    make_review_profile,
    make_review_proxies,
    probe_encoder_capability,
    resolve_review_case,
)
from game_highlight_finder.cli import app
from game_highlight_finder.config import AppConfig, StorageConfig, ToolsConfig
from game_highlight_finder.errors import DependencyError, SourceError, StorageError, ValidationError
from game_highlight_finder.media.ffmpeg import FFmpegResult
from game_highlight_finder.pipeline.ingest import ingest_source
from game_highlight_finder.storage.atomic import atomic_write_json
from game_highlight_finder.storage.hashing import hash_file


@dataclass(frozen=True)
class ReviewFixture:
    dataset_path: Path
    source_path: Path
    config: AppConfig
    source_probe: MediaProbe


@pytest.fixture
def review_fixture(
    tmp_path: Path,
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
) -> ReviewFixture:
    config = AppConfig(
        storage=StorageConfig(data_dir=tmp_path / "library"),
        tools=ToolsConfig(ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path),
    )
    source = ingest_source(tiny_video, config).source
    case = BenchmarkCase(
        case_id="synthetic-review-case",
        source_path=source.path,
        expected_source_sha256=source.sha256,
        annotation_path=tmp_path / "annotations" / "synthetic-review-case.json",
        game_profile="unknown",
        split=BenchmarkSplit.CALIBRATION,
    )
    dataset = BenchmarkDataset(
        benchmark_id="synthetic-review",
        name="synthetic review dataset",
        cases=(case,),
    )
    annotation = BenchmarkAnnotations(
        benchmark_id="m8-private",
        case_id=case.case_id,
        source_sha256=source.sha256,
        source_duration_ms=source.duration_ms,
        game_profile="unknown",
        source_path=source.path,
    )
    atomic_write_json(case.annotation_path, annotation.model_dump(mode="json"))
    dataset_path = tmp_path / "dataset.json"
    atomic_write_json(dataset_path, dataset.model_dump(mode="json"))
    source_probe = MediaProbe(
        duration_ms=source.duration_ms,
        width=source.video_stream.width,
        height=source.video_stream.height,
        video_codec=source.video_stream.codec_name,
        fps=(
            source.video_stream.average_frame_rate.value
            if source.video_stream.average_frame_rate is not None
            else None
        ),
        audio_present=source.selected_audio_stream is not None,
        audio_codec=source.audio_streams[0].codec_name if source.audio_streams else None,
        audio_channels=source.audio_streams[0].channels if source.audio_streams else None,
        bitrate_kbps=None,
        format_name="mov,mp4,m4a,3gp,3g2,mj2",
    )
    return ReviewFixture(dataset_path, source.path, config, source_probe)


def test_review_proxy_cli_help_is_available() -> None:
    result = CliRunner().invoke(app, ["benchmark", "make-review-proxies", "--help"])
    assert result.exit_code == 0, result.output
    assert "--small" in result.output
    assert "--allow-cpu-fallback" in result.output
    assert "--overwrite" in result.output


def test_default_profile_and_command_preserve_full_timeline() -> None:
    command = build_review_proxy_command(
        Path("ffmpeg"),
        Path("source.mkv"),
        Path("proxy.mp4"),
        DEFAULT_REVIEW_PROFILE,
        has_audio=True,
        audio_channels=2,
    )
    assert command[0] == "ffmpeg"
    assert "h264_nvenc" in command
    assert "-ss" not in command
    assert "-t" not in command
    assert "-fpsmax" in command
    assert "force_original_aspect_ratio=decrease" in command[command.index("-vf") + 1]
    assert "force_divisible_by=2" in command[command.index("-vf") + 1]
    assert "+faststart" in command
    assert "0:a:0" in command
    assert "-ac" in command and command[command.index("-ac") + 1] == "2"


def test_small_profile_never_upscales_or_uses_production_defaults() -> None:
    profile = make_review_profile(small=True)
    assert profile.max_height == 540
    assert profile.video_bitrate_kbps == 750
    assert profile.audio_bitrate_kbps == 64
    assert profile.name == "small"


def test_nvenc_capability_probe_detects_encoder(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> Any:
        return __import__("subprocess").CompletedProcess(
            args=["ffmpeg"],
            returncode=0,
            stdout=" V....D h264_nvenc NVIDIA NVENC H.264 encoder\n",
            stderr="",
        )

    monkeypatch.setattr("game_highlight_finder.benchmark.review_proxy.subprocess.run", fake_run)
    capability = probe_encoder_capability(Path("ffmpeg"))
    assert capability.available is True
    assert capability.encoder == "h264_nvenc"


def test_dataset_case_resolution_verifies_source_and_annotation(
    review_fixture: ReviewFixture,
) -> None:
    dataset = json.loads(review_fixture.dataset_path.read_text(encoding="utf-8"))
    case = BenchmarkCase.model_validate(dataset["cases"][0])
    resolved = resolve_review_case(
        review_fixture.dataset_path,
        case,
        review_fixture.config,
        ffprobe_path=review_fixture.config.tools.ffprobe_path or Path("ffprobe"),
    )
    assert resolved.source_fingerprint.sha256 == hash_file(review_fixture.source_path, source=True)
    assert resolved.source_probe.duration_ms == review_fixture.source_probe.duration_ms


def test_dataset_source_missing_and_sha_mismatch_are_rejected(
    review_fixture: ReviewFixture,
) -> None:
    dataset = json.loads(review_fixture.dataset_path.read_text(encoding="utf-8"))
    case_data = cast(dict[str, Any], dataset["cases"][0])
    missing = BenchmarkCase.model_validate(
        {**case_data, "source_path": str(review_fixture.source_path) + ".missing"}
    )
    with pytest.raises(SourceError):
        resolve_review_case(
            review_fixture.dataset_path,
            missing,
            review_fixture.config,
            ffprobe_path=review_fixture.config.tools.ffprobe_path or Path("ffprobe"),
        )
    mismatch = BenchmarkCase.model_validate({**case_data, "expected_source_sha256": "0" * 64})
    with pytest.raises(SourceError):
        resolve_review_case(
            review_fixture.dataset_path,
            mismatch,
            review_fixture.config,
            ffprobe_path=review_fixture.config.tools.ffprobe_path or Path("ffprobe"),
        )


def test_nvenc_is_required_by_default(
    monkeypatch: pytest.MonkeyPatch, review_fixture: ReviewFixture
) -> None:
    monkeypatch.setattr(
        "game_highlight_finder.benchmark.review_proxy.require_executable",
        lambda name, _configured=None: Path(name),
    )
    monkeypatch.setattr(
        "game_highlight_finder.benchmark.review_proxy.executable_version",
        lambda _path: "ffmpeg-test",
    )
    monkeypatch.setattr(
        "game_highlight_finder.benchmark.review_proxy.probe_encoder_capability",
        lambda path: EncoderCapability("h264_nvenc", False, path, "no nvenc"),
    )
    with pytest.raises(DependencyError):
        make_review_proxies(review_fixture.dataset_path, review_fixture.config)


def test_generation_cache_and_config_invalidation(
    monkeypatch: pytest.MonkeyPatch,
    review_fixture: ReviewFixture,
    tmp_path: Path,
) -> None:
    ffmpeg = review_fixture.config.tools.ffmpeg_path or Path("ffmpeg")
    ffprobe = review_fixture.config.tools.ffprobe_path or Path("ffprobe")
    monkeypatch.setattr(
        "game_highlight_finder.benchmark.review_proxy.require_executable",
        lambda name, _configured=None: ffmpeg if name == "ffmpeg" else ffprobe,
    )
    monkeypatch.setattr(
        "game_highlight_finder.benchmark.review_proxy.executable_version",
        lambda _path: "ffmpeg-test",
    )
    monkeypatch.setattr(
        "game_highlight_finder.benchmark.review_proxy.probe_encoder_capability",
        lambda path: EncoderCapability("h264_nvenc", True, path, "h264_nvenc"),
    )
    monkeypatch.setattr(
        "game_highlight_finder.benchmark.review_proxy.probe_media",
        lambda _ffprobe, _path, _config: review_fixture.source_probe,
    )
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> FFmpegResult:
        calls.append(command)
        Path(command[-1]).write_bytes(review_fixture.source_path.read_bytes())
        return FFmpegResult(returncode=0, stderr="", progress=())

    monkeypatch.setattr("game_highlight_finder.benchmark.review_proxy.run_ffmpeg", fake_run)
    output_dir = tmp_path / "review-proxies"
    first = make_review_proxies(
        review_fixture.dataset_path, review_fixture.config, output_dir=output_dir
    )
    assert first.generated_count == 1
    assert first.cache_hit_count == 0
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert manifest["cases"]["synthetic-review-case"]["encoder"] == "h264_nvenc"
    second = make_review_proxies(
        review_fixture.dataset_path, review_fixture.config, output_dir=output_dir
    )
    assert second.cache_hit_count == 1
    assert len(calls) == 1
    with pytest.raises(StorageError):
        make_review_proxies(
            review_fixture.dataset_path,
            review_fixture.config,
            profile=make_review_profile(max_height=540),
            output_dir=output_dir,
        )


def test_explicit_cpu_fallback_generates_synthetic_media(
    monkeypatch: pytest.MonkeyPatch,
    review_fixture: ReviewFixture,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "game_highlight_finder.benchmark.review_proxy.probe_encoder_capability",
        lambda path: EncoderCapability("h264_nvenc", False, path, "mocked unavailable"),
    )
    result = make_review_proxies(
        review_fixture.dataset_path,
        review_fixture.config,
        output_dir=tmp_path / "cpu-review-proxies",
        allow_cpu_fallback=True,
    )
    assert result.encoder == "libx264"
    assert result.cases[0].audio_retained is True
    assert result.maximum_duration_delta_ms <= 250


def test_duration_drift_and_source_mutation_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    review_fixture: ReviewFixture,
    tmp_path: Path,
) -> None:
    ffmpeg = review_fixture.config.tools.ffmpeg_path or Path("ffmpeg")
    ffprobe = review_fixture.config.tools.ffprobe_path or Path("ffprobe")
    monkeypatch.setattr(
        "game_highlight_finder.benchmark.review_proxy.require_executable",
        lambda name, _configured=None: ffmpeg if name == "ffmpeg" else ffprobe,
    )
    monkeypatch.setattr(
        "game_highlight_finder.benchmark.review_proxy.executable_version",
        lambda _path: "ffmpeg-test",
    )
    monkeypatch.setattr(
        "game_highlight_finder.benchmark.review_proxy.probe_encoder_capability",
        lambda path: EncoderCapability("h264_nvenc", True, path, "h264_nvenc"),
    )

    def drift_probe(_ffprobe: Path, path: Path, _config: AppConfig) -> MediaProbe:
        if path == review_fixture.source_path:
            return review_fixture.source_probe
        return replace(
            review_fixture.source_probe,
            duration_ms=review_fixture.source_probe.duration_ms + 251,
        )

    monkeypatch.setattr(
        "game_highlight_finder.benchmark.review_proxy.probe_media",
        drift_probe,
    )
    monkeypatch.setattr(
        "game_highlight_finder.benchmark.review_proxy.run_ffmpeg",
        lambda command, **_kwargs: (
            Path(command[-1]).write_bytes(review_fixture.source_path.read_bytes()),
            FFmpegResult(returncode=0, stderr="", progress=()),
        )[1],
    )
    with pytest.raises(ValidationError):
        make_review_proxies(
            review_fixture.dataset_path,
            review_fixture.config,
            output_dir=tmp_path / "drift-review-proxies",
        )

    monkeypatch.setattr(
        "game_highlight_finder.benchmark.review_proxy.probe_media",
        lambda _ffprobe, _path, _config: review_fixture.source_probe,
    )

    def mutate_source(command: list[str], **_kwargs: object) -> FFmpegResult:
        Path(command[-1]).write_bytes(review_fixture.source_path.read_bytes())
        review_fixture.source_path.write_bytes(review_fixture.source_path.read_bytes() + b"changed")
        return FFmpegResult(returncode=0, stderr="", progress=())

    monkeypatch.setattr("game_highlight_finder.benchmark.review_proxy.run_ffmpeg", mutate_source)
    with pytest.raises(SourceError):
        make_review_proxies(
            review_fixture.dataset_path,
            review_fixture.config,
            output_dir=tmp_path / "mutation-review-proxies",
        )


def test_output_directory_inside_source_directory_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    review_fixture: ReviewFixture,
) -> None:
    monkeypatch.setattr(
        "game_highlight_finder.benchmark.review_proxy.probe_encoder_capability",
        lambda path: EncoderCapability("h264_nvenc", True, path, "h264_nvenc"),
    )
    with pytest.raises(ValidationError):
        make_review_proxies(
            review_fixture.dataset_path,
            review_fixture.config,
            output_dir=review_fixture.source_path.parent,
        )


def test_no_provider_or_external_client_imports() -> None:
    source = Path("src/game_highlight_finder/benchmark/review_proxy.py").read_text(encoding="utf-8")
    assert "google.genai" not in source
    assert "requests" not in source
    assert "urllib.request" not in source
