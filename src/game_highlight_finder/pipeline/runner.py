"""M2 vertical pipeline runner: ingest -> proxy -> local signals."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from game_highlight_finder.config import AppConfig
from game_highlight_finder.errors import ConfigError
from game_highlight_finder.pipeline.ingest import IngestResult, ingest_source
from game_highlight_finder.pipeline.local_signals import LocalSignalsResult, generate_local_signals
from game_highlight_finder.pipeline.proxy import ProxyResult, generate_proxy

StopAfter = Literal["ingest", "proxy", "local_signals"]


class AnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ingest: IngestResult
    proxy: ProxyResult | None = None
    local_signals: LocalSignalsResult | None = None
    stop_after: StopAfter


def normalize_stop_after(value: str) -> StopAfter:
    normalized = value.strip().lower().replace("-", "_")
    if normalized not in {"ingest", "proxy", "local_signals"}:
        raise ConfigError("Unknown stop-after stage.", hint="Use ingest, proxy, or local-signals.")
    return normalized  # type: ignore[return-value]


def analyze_source(
    video: Path,
    config: AppConfig,
    *,
    stop_after: str = "local-signals",
) -> AnalysisResult:
    boundary = normalize_stop_after(stop_after)
    ingest = ingest_source(video, config)
    if boundary == "ingest":
        return AnalysisResult(ingest=ingest, stop_after=boundary)
    proxy = generate_proxy(ingest.source, config)
    if boundary == "proxy":
        return AnalysisResult(ingest=ingest, proxy=proxy, stop_after=boundary)
    signals = generate_local_signals(ingest.source, proxy, config)
    return AnalysisResult(ingest=ingest, proxy=proxy, local_signals=signals, stop_after=boundary)
