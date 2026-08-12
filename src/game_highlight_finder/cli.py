"""Typer command-line interface for the local M3 pipeline."""

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
from game_highlight_finder.doctor import run_doctor
from game_highlight_finder.errors import AppError, ConfigError, ErrorCategory
from game_highlight_finder.pipeline.runner import analyze_source
from game_highlight_finder.status import get_session_status

app = typer.Typer(
    name="highlight",
    help="Local-first gameplay recording analysis (M3: canonical domain + Fake Scout).",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
config_app = typer.Typer(help="Inspect and validate configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")


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


def _config_check(options: RuntimeOptions) -> None:
    result = _load(options)
    typer.echo("[PASS] configuration is valid")
    typer.echo(f"source: {result.source_file or 'safe defaults'}")
    typer.echo(f"config hash: {config_hash(result.config)}")
    typer.echo(json.dumps(config_payload(result.config), indent=2, ensure_ascii=False))


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
