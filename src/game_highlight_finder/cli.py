"""Typer command-line interface for the local-first pipeline and M4 cost gate."""

# ruff: noqa: E501

from __future__ import annotations

import json
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer

from game_highlight_finder.config import (
    AppConfig,
    ConfigResult,
    config_hash,
    config_payload,
    load_config,
)
from game_highlight_finder.cost import CostService, Money
from game_highlight_finder.doctor import run_doctor
from game_highlight_finder.domain.time import format_duration
from game_highlight_finder.errors import AppError, ConfigError, ErrorCategory
from game_highlight_finder.pipeline.gemini_scout import (
    generate_gemini_scout,
    preflight_gemini_scout,
)
from game_highlight_finder.pipeline.ranking import load_or_create_ranking
from game_highlight_finder.pipeline.report import load_report_inputs, render_report
from game_highlight_finder.pipeline.runner import (
    AnalysisResult,
    V1AnalysisResult,
    analyze_m6_source,
    analyze_source,
    analyze_v1_source,
)
from game_highlight_finder.providers import ProviderRegistry
from game_highlight_finder.status import get_session_status
from game_highlight_finder.storage.atomic import read_json
from game_highlight_finder.storage.sessions import session_paths, source_from_artifact

app = typer.Typer(
    name="highlight",
    help="Local-first gameplay recording analysis with offline ranking and HTML reports.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
config_app = typer.Typer(help="Inspect and validate configuration.", no_args_is_help=True)
cost_app = typer.Typer(help="Inspect the local hard-budget cost ledger.", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(cost_app, name="cost")


@dataclass(frozen=True)
class RuntimeOptions:
    config_path: Path | None
    data_dir: Path | None
    ffmpeg_path: Path | None
    ffprobe_path: Path | None
    debug: bool


@app.callback()
def main(
    ctx: typer.Context,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to a YAML configuration file.")
    ] = None,
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", help="Override the session data directory.")
    ] = None,
    ffmpeg_path: Annotated[
        Path | None, typer.Option("--ffmpeg-path", help="Explicit ffmpeg executable path.")
    ] = None,
    ffprobe_path: Annotated[
        Path | None, typer.Option("--ffprobe-path", help="Explicit ffprobe executable path.")
    ] = None,
    debug: Annotated[
        bool, typer.Option("--debug", help="Show tracebacks for unexpected errors.")
    ] = False,
) -> None:
    _configure_console()
    ctx.obj = RuntimeOptions(config_path, data_dir, ffmpeg_path, ffprobe_path, debug)


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Check Python, FFmpeg, configuration, and local storage readiness."""
    options = _options(ctx)
    config_error: str | None = None
    try:
        result = _load(options)
        config = result.config
    except ConfigError as exc:
        config_error = f"{exc.message}{': ' + exc.hint if exc.hint else ''}"
        fallback = load_config(environ={}, cli_overrides=_cli_overrides(options))
        config = fallback.config
    report = run_doctor(config, config_error=config_error)
    for check in report.checks:
        suffix = f" [{check.path}]" if check.path else ""
        typer.echo(f"[{check.level}] {check.name}: {check.message}{suffix}")
    if report.has_failures:
        raise typer.Exit(1)


@config_app.command("check")
def config_check(ctx: typer.Context) -> None:
    """Load strict configuration and print the resolved redacted result."""
    _execute(ctx, _config_check)


@cost_app.command("status")
def cost_status(ctx: typer.Context) -> None:
    """Show the current configured monthly budget exposure."""
    _execute(ctx, lambda options: _cost_status(options, None))


@cost_app.command("report")
def cost_report(
    ctx: typer.Context,
    month: Annotated[
        str | None,
        typer.Option(
            "--month", help="Budget period in YYYY-MM; defaults to the configured local month."
        ),
    ] = None,
) -> None:
    """Show a compact monthly cost report grouped by provider/model/stage."""
    _execute(ctx, lambda options: _cost_status(options, month))


@cost_app.command("calls")
def cost_calls(ctx: typer.Context) -> None:
    """List current-month cost calls and lifecycle states."""
    _execute(ctx, _cost_calls)


@cost_app.command("session")
def cost_session(
    ctx: typer.Context,
    session_id: Annotated[str, typer.Argument(help="Stable session identifier.")],
) -> None:
    """Show only the calls and exposure belonging to one session."""
    _execute(ctx, lambda options: _cost_session(options, session_id))


def _config_check(options: RuntimeOptions) -> None:
    result = _load(options)
    typer.echo("[PASS] configuration is valid")
    typer.echo(f"source: {result.source_file or 'safe defaults'}")
    typer.echo(f"config hash: {config_hash(result.config)}")
    typer.echo(json.dumps(config_payload(result.config), indent=2, ensure_ascii=False))


def _cost_service(options: RuntimeOptions) -> CostService:
    config = _load(options).config
    # M4 ships no production provider/pricing entries. Status/report commands only
    # inspect the ledger; paid quote operations require an explicitly supplied catalog.
    return CostService.from_config(config, registry=ProviderRegistry())


def _format_micro_thb(value: int) -> str:
    return Money(micro_thb=value).display()


def _cost_status(options: RuntimeOptions, month: str | None) -> None:
    service = _cost_service(options)
    summary = service.summary()
    if month is not None:
        if len(month) != 7 or month[4] != "-" or not month.replace("-", "").isdigit():
            raise ConfigError("Cost report month must use YYYY-MM format.")
        summary = service.ledger.summary(month)
    typer.echo(f"Budget period: {summary.budget_period} ({service.config.cost.budget_timezone})")
    typer.echo(f"Monthly hard cap: {_format_micro_thb(summary.budget_micro_thb)}")
    typer.echo(f"Settled: {_format_micro_thb(summary.settled_micro_thb)}")
    typer.echo(f"Reserved: {_format_micro_thb(summary.reserved_micro_thb)}")
    typer.echo(f"In-flight: {_format_micro_thb(summary.in_flight_micro_thb)}")
    typer.echo(f"Ambiguous: {_format_micro_thb(summary.ambiguous_micro_thb)}")
    typer.echo(f"Available: {_format_micro_thb(summary.available_micro_thb)}")
    typer.echo("Currency: THB (integer micro-THB ledger)")
    typer.echo(f"Unreconciled calls: {summary.unreconciled_calls}")
    if summary.safety_hold_active:
        typer.echo(f"SAFETY HOLD: {summary.safety_hold_reason}")
    else:
        typer.echo("Safety hold: none")
    if month is not None:
        grouped: dict[tuple[str, str, str], int] = {}
        for call in service.ledger.list_calls(budget_period=month):
            key = (call.provider, call.model, call.stage)
            grouped[key] = grouped.get(key, 0) + call.exposure_micro_thb
        if grouped:
            typer.echo("By provider/model/stage:")
            for (provider, model, stage), amount in sorted(grouped.items()):
                typer.echo(f"  {provider}/{model} [{stage}]: {_format_micro_thb(amount)}")


def _cost_calls(options: RuntimeOptions) -> None:
    service = _cost_service(options)
    calls = service.calls()
    if not calls:
        typer.echo("No cost calls for the current budget period.")
        return
    for call in calls:
        amount = (
            call.settled_cost_micro_thb
            if call.status.value == "SETTLED"
            else call.reserved_cost_micro_thb
        )
        typer.echo(
            f"{call.call_id} {call.status} {call.provider}/{call.model}/{call.billing_mode} "
            f"{_format_micro_thb(amount or 0)} stage={call.stage}"
        )


def _cost_session(options: RuntimeOptions, session_id: str) -> None:
    paths = session_paths(_load(options).config.storage.data_dir, session_id)
    if not paths.root.is_dir():
        raise ConfigError(f"Session does not exist: {session_id}")
    service = _cost_service(options)
    calls = [call for call in service.ledger.list_calls() if call.session_id == session_id]
    settled = sum(
        call.settled_cost_micro_thb or 0 for call in calls if call.status.value == "SETTLED"
    )
    reserved = sum(
        call.reserved_cost_micro_thb for call in calls if call.status.value == "RESERVED"
    )
    in_flight = sum(
        call.reserved_cost_micro_thb for call in calls if call.status.value == "IN_FLIGHT"
    )
    ambiguous = sum(
        call.reserved_cost_micro_thb for call in calls if call.status.value == "AMBIGUOUS"
    )
    typer.echo(f"Session: {session_id}")
    typer.echo(f"Settled: {_format_micro_thb(settled)}")
    typer.echo(f"Reserved: {_format_micro_thb(reserved)}")
    typer.echo(f"In-flight: {_format_micro_thb(in_flight)}")
    typer.echo(f"Ambiguous: {_format_micro_thb(ambiguous)}")
    typer.echo(f"Call count: {len(calls)}")
    grouped: dict[tuple[str, str, str], int] = {}
    for call in calls:
        key = (call.provider, call.model, call.stage)
        grouped[key] = grouped.get(key, 0) + call.exposure_micro_thb
    for (provider, model, stage), amount in sorted(grouped.items()):
        typer.echo(f"  {provider}/{model} [{stage}]: {_format_micro_thb(amount)}")
    hold = service.ledger.safety_hold()
    typer.echo(f"Safety hold: {hold.reason if hold else 'none'}")


@app.command()
def analyze(
    ctx: typer.Context,
    video: Annotated[Path, typer.Argument(help="Local source gameplay recording.")],
    stop_after: Annotated[
        str,
        typer.Option(
            "--stop-after",
            help="Stop after ingest, proxy, local-signals, windows, scout, reconcile, extract, rank, or report.",
        ),
    ] = "report",
    scout_backend: Annotated[
        str | None,
        typer.Option(
            "--scout-backend",
            help="Override the Scout backend for this run (fake or gemini).",
        ),
    ] = None,
    allow_remote_upload: Annotated[
        bool,
        typer.Option(
            "--allow-remote-upload",
            help="Explicitly authorize uploading the analysis proxy to Gemini.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Gemini preflight only: quote locally and make no upload or provider call.",
        ),
    ] = False,
    m6: Annotated[
        bool,
        typer.Option("--m6", help="Backward-compatible alias for the windowed V1 flow."),
    ] = False,
    force_stage: Annotated[
        str | None,
        typer.Option("--force-stage", help="Force one stage and its downstream local work."),
    ] = None,
) -> None:
    """Run local stages and the configured Scout without modifying the source."""
    _execute(
        ctx,
        lambda options: _analyze(
            options,
            video,
            stop_after,
            scout_backend=scout_backend,
            allow_remote_upload=allow_remote_upload,
            dry_run=dry_run,
            m6=m6,
            force_stage=force_stage,
        ),
    )


def _analyze(
    options: RuntimeOptions,
    video: Path,
    stop_after: str,
    *,
    scout_backend: str | None = None,
    allow_remote_upload: bool = False,
    dry_run: bool = False,
    m6: bool = False,
    force_stage: str | None = None,
) -> None:
    config = _load(options).config
    if scout_backend is not None:
        normalized_backend = scout_backend.strip().lower()
        if normalized_backend not in {"fake", "gemini"}:
            raise ConfigError("Scout backend must be fake or gemini.")
        config = config.model_copy(
            update={"scout": config.scout.model_copy(update={"backend": normalized_backend})}
        )
    if allow_remote_upload:
        config = config.model_copy(
            update={"scout": config.scout.model_copy(update={"allow_remote_upload": True})}
        )
    if m6 and stop_after.strip().lower().replace("-", "_") not in {"report", "rank"}:
        if config.scout.backend == "gemini" and not config.scout.allow_remote_upload:
            raise ConfigError("M6 Gemini requires --allow-remote-upload.")
        m6_result = analyze_m6_source(video, config, stop_after=stop_after)
        typer.echo("[PASS] M6 windowed analysis completed")
        if m6_result.windows is not None:
            typer.echo(
                f"windows: {len(m6_result.windows.windows)} "
                f"(cache hits: {m6_result.windows.cache_hits})"
            )
        if m6_result.scout is not None:
            cache_hits = sum(1 for item in m6_result.scout.results if item.cache_hit)
            typer.echo(
                f"window Scout responses: {len(m6_result.scout.results)} (cache hits: {cache_hits})"
            )
            typer.echo(
                "aggregate cost preflight micro-THB: "
                f"{m6_result.scout.aggregate_preflight.estimated_micro_thb}"
            )
        if m6_result.session_map is not None:
            typer.echo(f"reconciled candidates: {len(m6_result.session_map.candidates)}")
            session_map_path = (
                config.storage.data_dir
                / "sessions"
                / m6_result.ingest.session_id
                / "session_map.json"
            )
            typer.echo(f"session map: {session_map_path}")
        if m6_result.extraction is not None:
            typer.echo(
                f"extractions: {m6_result.extraction.completed} completed, "
                f"{m6_result.extraction.incomplete} incomplete"
            )
        if config.scout.backend == "fake":
            typer.echo("Real Gemini API calls: ZERO")
        typer.echo(f"session ID: {m6_result.ingest.session_id}")
        typer.echo(f"session directory: {m6_result.ingest.session_dir}")
        return
    if not dry_run and (
        m6
        or stop_after.strip().lower().replace("-", "_")
        in {"report", "rank", "reconcile", "extract", "windows"}
    ):
        v1 = analyze_v1_source(video, config, stop_after=stop_after, force_stage=force_stage)
        _print_v1_result(v1)
        return
    if dry_run:
        if config.scout.backend != "gemini":
            raise ConfigError("--dry-run is only available with the Gemini Scout backend.")
        local = analyze_source(video, config, stop_after="local-signals")
        assert local.proxy is not None and local.local_signals is not None
        preflight = preflight_gemini_scout(
            local.ingest.source, local.proxy, local.local_signals, config
        )
        typer.echo("[PASS] Gemini preflight: no provider call or upload performed")
        typer.echo("Provider: Gemini")
        typer.echo(f"Model: {preflight.model}")
        typer.echo("Input: analysis proxy only")
        typer.echo(f"Media resolution: {preflight.media_resolution}")
        typer.echo(f"Thinking level: {preflight.thinking_level}")
        typer.echo(f"Reserved thinking allowance: {preflight.reserved_thinking_tokens} tokens")
        typer.echo(
            f"Maximum reserved cost: {_format_micro_thb(preflight.quote.reserved_cost_micro_thb)}"
        )
        typer.echo(f"Monthly available budget: {_format_micro_thb(preflight.available_micro_thb)}")
        return

    normalized_stop = stop_after.strip().lower().replace("-", "_")
    if config.scout.backend == "gemini" and normalized_stop == "scout":
        local = analyze_source(video, config, stop_after="local-signals")
        assert local.proxy is not None and local.local_signals is not None
        typer.echo("Scout backend: Gemini")
        typer.echo("Uploading analysis proxy only")
        typer.echo("Original source will NOT be uploaded")
        typer.echo(f"Model: {config.scout.model}")
        typer.echo(f"Media resolution: {config.scout.media_resolution}")
        typer.echo(f"Thinking level: {config.scout.thinking_level}")
        typer.echo(f"Reserved thinking allowance: {config.scout.reserved_thinking_tokens} tokens")
        preflight = preflight_gemini_scout(
            local.ingest.source, local.proxy, local.local_signals, config
        )
        typer.echo(
            f"Maximum reserved cost: {_format_micro_thb(preflight.quote.reserved_cost_micro_thb)}"
        )
        typer.echo(f"Monthly available budget: {_format_micro_thb(preflight.available_micro_thb)}")
        result: AnalysisResult = AnalysisResult(
            ingest=local.ingest,
            proxy=local.proxy,
            local_signals=local.local_signals,
            scout=generate_gemini_scout(
                local.ingest.source, local.proxy, local.local_signals, config
            ),
            stop_after="scout",
        )
    else:
        if stop_after.strip().lower().replace("-", "_") == "report":
            v1 = analyze_v1_source(video, config, stop_after="report", force_stage=force_stage)
            _print_v1_result(v1)
            return
        result = analyze_source(video, config, stop_after=stop_after)
    ingest_outcome = "CACHE HIT" if result.ingest.cache_hit else "COMPLETED"
    typer.echo(f"[PASS] ingest: {ingest_outcome}")
    if result.proxy is not None:
        proxy_outcome = "CACHE HIT" if result.proxy.cache_hit else "COMPLETED"
        typer.echo(f"[PASS] proxy: {proxy_outcome}")
    if result.local_signals is not None:
        signal_outcome = "CACHE HIT" if result.local_signals.cache_hit else "COMPLETED"
        typer.echo(f"[PASS] local_signals: {signal_outcome}")
        if result.local_signals.signals.warnings:
            for warning in result.local_signals.signals.warnings:
                typer.echo(f"[WARN] local_signals: {warning}")
    if result.scout is not None:
        scout_outcome = "CACHE HIT" if result.scout.cache_hit else "COMPLETED"
        typer.echo(f"[PASS] scout: {scout_outcome}")
        if result.scout.backend == "fake":
            typer.echo("Scout backend: fake (offline; no AI/API call)")
        else:
            typer.echo("Scout backend: Gemini (analysis proxy only)")
        typer.echo(f"candidates: {len(result.scout.session_map.candidates)}")
        typer.echo(f"session map: {result.scout.session_map_path}")
    typer.echo(f"session ID: {result.ingest.session_id}")
    typer.echo(f"source: {result.ingest.source.path}")
    typer.echo(f"duration_ms: {result.ingest.source.duration_ms}")
    typer.echo(f"sha256: {result.ingest.source.sha256}")
    typer.echo(f"session directory: {result.ingest.session_dir}")


def _print_v1_result(result: V1AnalysisResult) -> None:
    m6 = result.m6
    typer.echo("[PASS] V1 local analysis completed")
    typer.echo(f"session ID: {m6.ingest.session_id}")
    typer.echo(f"session directory: {m6.ingest.session_dir}")
    if result.ranking is not None:
        typer.echo(f"ranking: {m6.ingest.session_dir / 'reports' / 'ranking.json'}")
        typer.echo(f"best-of candidates: {len(result.ranking.best_of_candidate_ids)}")
    if result.report is not None:
        typer.echo(f"report: {result.report.path}")
        typer.echo(f"report cache: {'HIT' if result.report.cache_hit else 'MISS'}")
    typer.echo("Real Gemini API calls: ZERO" if m6.ingest.source.path.is_file() else "")


@app.command()
def resume(
    ctx: typer.Context,
    session_id: Annotated[str, typer.Argument(help="Stable session identifier to resume.")],
    allow_remote_upload: Annotated[
        bool,
        typer.Option(
            "--allow-remote-upload",
            help="Freshly authorize missing Gemini work for this invocation.",
        ),
    ] = False,
    force_stage: Annotated[
        str | None, typer.Option("--force-stage", help="Force one stage and downstream local work.")
    ] = None,
) -> None:
    """Resume a persisted session and finish local ranking/report stages."""
    _execute(ctx, lambda options: _resume(options, session_id, allow_remote_upload, force_stage))


def _resume(
    options: RuntimeOptions, session_id: str, allow_remote_upload: bool, force_stage: str | None
) -> None:
    config = _load_persisted_session_config(options, session_id)
    paths = session_paths(config.storage.data_dir, session_id)
    if not paths.root.is_dir():
        raise ConfigError(f"Session does not exist: {session_id}")
    source = source_from_artifact(paths.source)
    if not source.path.is_file():
        raise ConfigError("Original source is missing; resume cannot continue.")
    if allow_remote_upload:
        config = config.model_copy(
            update={"scout": config.scout.model_copy(update={"allow_remote_upload": True})}
        )
    result = analyze_v1_source(source.path, config, stop_after="report", force_stage=force_stage)
    _print_v1_result(result)


def _load_persisted_session_config(options: RuntimeOptions, session_id: str) -> AppConfig:
    """Load the redacted resolved config while preserving operational overrides."""

    current = _load(options).config
    paths = session_paths(current.storage.data_dir, session_id)
    if not paths.config.is_file():
        raise ConfigError(
            "Persisted session configuration is missing; resume cannot guess settings."
        )
    try:
        document = read_json(paths.config)
        persisted = document.get("config") if isinstance(document, dict) else None
        if not isinstance(persisted, dict):
            raise ValueError("config payload is not an object")
        merged = current.model_dump(mode="python")

        def merge(target: dict[str, Any], update: dict[str, Any]) -> None:
            for key, value in update.items():
                if value == "<redacted>":
                    continue
                if isinstance(value, dict) and isinstance(target.get(key), dict):
                    merge(target[key], value)
                else:
                    target[key] = value

        merge(merged, persisted)
        merged["storage"] = current.storage.model_dump(mode="python")
        # A persisted Gemini opt-in is not authorization for this invocation.
        merged.setdefault("scout", {})["allow_remote_upload"] = False
        return AppConfig.model_validate(merged)
    except Exception as exc:
        raise ConfigError(
            "Persisted session configuration is invalid; resume cannot guess settings.",
            hint=str(exc),
        ) from exc


@app.command()
def report(
    ctx: typer.Context,
    session_id: Annotated[str, typer.Argument(help="Stable session identifier.")],
    open_report: Annotated[
        bool, typer.Option("--open", help="Open the local report in the default browser.")
    ] = False,
) -> None:
    """Generate or reuse a local report without running Scout."""
    _execute(ctx, lambda options: _report(options, session_id, open_report))


def _report(options: RuntimeOptions, session_id: str, open_report: bool) -> None:
    config = _load(options).config
    paths = session_paths(config.storage.data_dir, session_id)
    source, session_map, ranking, manifest = load_report_inputs(paths)
    ranking, _ = load_or_create_ranking(paths, session_map, config)
    result = render_report(paths, source, session_map, ranking, manifest, config)
    typer.echo(str(result.path.resolve()))
    if open_report:
        import webbrowser

        webbrowser.open(result.path.resolve().as_uri())


@app.command()
def candidates(
    ctx: typer.Context,
    session_id: Annotated[str, typer.Argument(help="Stable session identifier.")],
    limit: Annotated[int | None, typer.Option("--limit", min=1)] = None,
    category: Annotated[str | None, typer.Option("--category")] = None,
    match: Annotated[str | None, typer.Option("--match")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List ranked candidate metadata without provider calls."""
    _execute(ctx, lambda options: _candidates(options, session_id, limit, category, match, as_json))


def _candidates(
    options: RuntimeOptions,
    session_id: str,
    limit: int | None,
    category: str | None,
    match_filter: str | None,
    as_json: bool,
) -> None:
    config = _load(options).config
    paths = session_paths(config.storage.data_dir, session_id)
    _source, session_map, ranking, _manifest = load_report_inputs(paths)
    matches = {item.match_id: item for item in session_map.matches}
    entries = {entry.candidate_id: entry for entry in ranking.entries}
    rows: list[dict[str, Any]] = []
    for candidate in session_map.candidates:
        label = (
            "UNASSIGNED"
            if candidate.match_id is None
            else (matches[candidate.match_id].label or candidate.match_id)
        )
        if category and candidate.category != category.strip().upper():
            continue
        if match_filter and label != match_filter:
            continue
        rows.append(
            {
                "rank": entries[candidate.candidate_id].rank,
                "category": candidate.category,
                "match": label,
                "score": candidate.score,
                "confidence": candidate.confidence,
                "event_time": format_duration(candidate.event_start_ms),
                "clip_duration": (candidate.clip_end_ms - candidate.clip_start_ms)
                if candidate.clip_start_ms is not None and candidate.clip_end_ms is not None
                else None,
                "candidate_id": candidate.candidate_id,
                "clip_path": str(
                    (paths.root / f"candidates/{candidate.candidate_id}.mp4").resolve()
                ),
            }
        )
    rows.sort(key=lambda item: item["rank"])
    if limit is not None:
        rows = rows[:limit]
    if as_json:
        typer.echo(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        if not rows:
            typer.echo("No candidates found.")
        for row in rows:
            typer.echo(
                f"#{row['rank']} {row['category']} {row['match']} score={row['score']:.2f} confidence={row['confidence']:.2f} event={row['event_time']} id={row['candidate_id']} clip={row['clip_path']}"
            )


@app.command()
def status(
    ctx: typer.Context,
    session_id: Annotated[str, typer.Argument(help="Stable session identifier.")],
) -> None:
    """Show all implemented stage states, cache details, artifacts, and warnings."""
    _execute(ctx, lambda options: _status(options, session_id))


def _status(options: RuntimeOptions, session_id: str) -> None:
    result = get_session_status(session_id, _load(options).config)
    typer.echo(f"session ID: {result.session_id}")
    typer.echo(f"source: {result.source.path}")
    typer.echo(f"duration: {result.duration_text} ({result.source.duration_ms} ms)")
    typer.echo(f"source hash: {result.source.sha256[:12]}")
    for stage_name, stage_status in result.stages.items():
        detail = result.stage_details.get(stage_name, "")
        suffix = f"  {detail}" if detail else ""
        typer.echo(f"{stage_name:14} {stage_status}{suffix}")
    typer.echo(f"cache state: {result.cache_state} ({result.cache_detail})")
    if result.source.warnings:
        typer.echo("warnings:")
        for warning in result.source.warnings:
            typer.echo(f"  - {warning}")
    else:
        typer.echo("warnings: none")
    typer.echo("artifacts:")
    for artifact in result.artifact_paths:
        typer.echo(f"  - {artifact}")
    if result.last_attempt:
        completed = (
            result.last_attempt.completed_at.isoformat()
            if result.last_attempt.completed_at
            else "-"
        )
        typer.echo(
            "last attempt: "
            f"{result.last_attempt.status} started={result.last_attempt.started_at.isoformat()} "
            f"completed={completed}"
        )
    else:
        typer.echo("last attempt: none")


def _execute[T](ctx: typer.Context, action: Callable[[RuntimeOptions], T]) -> T:
    options = _options(ctx)
    try:
        return action(options)
    except AppError as exc:
        _render_error(exc)
        if options.debug:
            traceback.print_exc()
        raise typer.Exit(exc.exit_code) from None
    except Exception as exc:
        if options.debug:
            traceback.print_exc()
        _render_error(
            AppError(
                ErrorCategory.INTERNAL,
                "Unexpected internal error.",
                hint=f"{type(exc).__name__}: {exc}" if options.debug else "Run again with --debug.",
                exit_code=3,
            )
        )
        raise typer.Exit(3) from None


def _render_error(error: AppError) -> None:
    typer.echo(f"[FAIL] {error.category}: {error.message}", err=True)
    if error.hint:
        typer.echo(f"hint: {error.hint}", err=True)


def _options(ctx: typer.Context) -> RuntimeOptions:
    root = ctx.find_root()
    if not isinstance(root.obj, RuntimeOptions):
        raise RuntimeError("CLI runtime options are unavailable")
    return root.obj


def _load(options: RuntimeOptions) -> ConfigResult:
    return load_config(
        options.config_path,
        cli_overrides=_cli_overrides(options),
    )


def _cli_overrides(options: RuntimeOptions) -> dict[str, Any]:
    return {
        "storage.data_dir": options.data_dir,
        "tools.ffmpeg_path": options.ffmpeg_path,
        "tools.ffprobe_path": options.ffprobe_path,
        "logging.level": "DEBUG" if options.debug else None,
    }


def _configure_console() -> None:
    """Use UTF-8 on real Windows streams while leaving test capture streams alone."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")


if __name__ == "__main__":
    app()
