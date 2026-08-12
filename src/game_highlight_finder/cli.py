"""Typer command-line interface for the local-first pipeline and M4 cost gate."""

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
    ConfigResult,
    config_hash,
    config_payload,
    load_config,
)
from game_highlight_finder.cost import CostService, Money
from game_highlight_finder.doctor import run_doctor
from game_highlight_finder.errors import AppError, ConfigError, ErrorCategory
from game_highlight_finder.pipeline.runner import analyze_source
from game_highlight_finder.providers import ProviderRegistry
from game_highlight_finder.status import get_session_status

app = typer.Typer(
    name="highlight",
    help="Local-first gameplay recording analysis (M4: cost gate + provider contract).",
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


@app.command()
def analyze(
    ctx: typer.Context,
    video: Annotated[Path, typer.Argument(help="Local source gameplay recording.")],
    stop_after: Annotated[
        str,
        typer.Option(
            "--stop-after",
            help="Stop after ingest, proxy, local-signals, or scout (default: scout).",
        ),
    ] = "scout",
) -> None:
    """Run local stages and deterministic Fake Scout without modifying the source."""
    _execute(ctx, lambda options: _analyze(options, video, stop_after))


def _analyze(options: RuntimeOptions, video: Path, stop_after: str) -> None:
    result = analyze_source(video, _load(options).config, stop_after=stop_after)
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
        typer.echo("Scout backend: fake (offline; no AI/API call)")
        typer.echo(f"candidates: {len(result.scout.session_map.candidates)}")
        typer.echo(f"session map: {result.scout.session_map_path}")
    typer.echo(f"session ID: {result.ingest.session_id}")
    typer.echo(f"source: {result.ingest.source.path}")
    typer.echo(f"duration_ms: {result.ingest.source.duration_ms}")
    typer.echo(f"sha256: {result.ingest.source.sha256}")
    typer.echo(f"session directory: {result.ingest.session_dir}")


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
