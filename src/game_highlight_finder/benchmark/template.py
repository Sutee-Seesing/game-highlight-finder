"""Offline annotation-template generation."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from game_highlight_finder.benchmark.models import BenchmarkAnnotations
from game_highlight_finder.config import AppConfig
from game_highlight_finder.errors import ConfigError
from game_highlight_finder.pipeline.ingest import ingest_source
from game_highlight_finder.storage.atomic import atomic_write_json


class AnnotationTemplateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    annotations: BenchmarkAnnotations
    output_path: Path


def create_annotation_template(
    video: Path,
    config: AppConfig,
    *,
    game_profile: str = "unknown",
    case_id: str | None = None,
    output: Path | None = None,
) -> AnnotationTemplateResult:
    """Inspect a local source and write an empty valid annotation document.

    ``ingest_source`` performs only local ffprobe/hash work and persists ordinary
    session metadata; it does not invoke Scout, upload media, or read credentials.
    The input video is never opened for writing.
    """

    normalized_profile = game_profile.strip().lower()
    if not normalized_profile or not all(
        character.isalnum() or character in "_-" for character in normalized_profile
    ):
        raise ConfigError("Game profile must contain only lowercase letters, digits, '_' or '-'.")
    source = ingest_source(video, config).source
    stable_case_id = case_id or f"case-{source.sha256[:16]}"
    annotations = BenchmarkAnnotations(
        benchmark_id="m8-private",
        case_id=stable_case_id,
        source_sha256=source.sha256,
        source_duration_ms=source.duration_ms,
        game_profile=normalized_profile,
        annotated_by="local",
        source_path=source.path,
    )
    target = (
        output.expanduser().resolve()
        if output is not None
        else config.storage.data_dir.resolve()
        / "benchmarks"
        / "annotations"
        / f"{stable_case_id}.json"
    )
    atomic_write_json(target, annotations.model_dump(mode="json"))
    return AnnotationTemplateResult(annotations=annotations, output_path=target)


__all__ = ["AnnotationTemplateResult", "create_annotation_template"]
