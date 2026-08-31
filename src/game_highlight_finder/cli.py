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

from game_highlight_finder.benchmark.aggregate import aggregate_comparison, aggregate_manifest
from game_highlight_finder.benchmark.annotation_server import AnnotationServer
from game_highlight_finder.benchmark.boundary_feasibility import (
    run_boundary_refinement_feasibility,
)
from game_highlight_finder.benchmark.boundary_feasibility_bundle import (
    pack_boundary_refinement_feasibility_bundle,
)
from game_highlight_finder.benchmark.calibration import (
    CALIBRATION_EXPERIMENT_REVISION,
    build_calibration_plan,
    write_calibration_artifacts,
)
from game_highlight_finder.benchmark.cross_case_suppression import run_cross_case_suppression
from game_highlight_finder.benchmark.evaluator import (
    annotation_sha256,
    evaluate_session,
    load_annotations,
    validate_annotations_file,
)
from game_highlight_finder.benchmark.identity import benchmark_identity_compatible
from game_highlight_finder.benchmark.models import BenchmarkDataset, BenchmarkSplit
from game_highlight_finder.benchmark.review_proxy import (
    ReviewProxyBatchResult,
    make_review_profile,
    make_review_proxies,
)
from game_highlight_finder.benchmark.review_queue_server import ReviewQueueServer
from game_highlight_finder.benchmark.scout_readiness import (
    run_scout_calibration_readiness,
)
from game_highlight_finder.benchmark.suppression_feasibility import (
    run_candidate_suppression_feasibility,
)
from game_highlight_finder.benchmark.template import create_annotation_template
from game_highlight_finder.config import (
    AppConfig,
    ConfigResult,
    config_hash,
    config_payload,
    load_config,
)
from game_highlight_finder.cost import CostService, Money
from game_highlight_finder.doctor import run_doctor
from game_highlight_finder.domain.models import ProxyMetadata, SessionMap, SourceAsset
from game_highlight_finder.domain.time import format_duration
from game_highlight_finder.errors import AppError, ConfigError, ErrorCategory
from game_highlight_finder.media.ffmpeg import ProgressUpdate
from game_highlight_finder.pipeline.boundary_refiner_gemini_batch import (
    preflight_gemini_boundary_refinement_session_batch,
    run_gemini_boundary_refinement_batch_with_transport_factory,
)
from game_highlight_finder.pipeline.gemini_scout import (
    generate_gemini_scout,
    preflight_gemini_scout,
)
from game_highlight_finder.pipeline.proxy import ProxyResult
from game_highlight_finder.pipeline.ranking import load_or_create_ranking
from game_highlight_finder.pipeline.report import load_report_inputs, render_report
from game_highlight_finder.pipeline.runner import (
    AnalysisResult,
    V1AnalysisResult,
    analyze_m6_source,
    analyze_source,
    analyze_v1_source,
)
from game_highlight_finder.pipeline.windowed_scout import (
    ExecutionActivity,
    aggregate_window_preflight,
)
from game_highlight_finder.providers import ProviderRegistry
from game_highlight_finder.providers.gemini import GenAITransport
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
benchmark_app = typer.Typer(
    help="Build and evaluate private, provider-neutral M8 benchmark artifacts.",
    no_args_is_help=True,
)
app.add_typer(config_app, name="config")
app.add_typer(cost_app, name="cost")
app.add_typer(benchmark_app, name="benchmark")


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


def _format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(amount) < 1024 or unit == "TiB":
            return f"{amount:.2f} {unit}"
        amount /= 1024
    return f"{amount:.2f} TiB"


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
    config = _load_persisted_session_config(options, session_id)
    paths = session_paths(config.storage.data_dir, session_id)
    if not paths.root.is_dir():
        raise ConfigError(f"Session does not exist: {session_id}")
    service = CostService.from_config(config, registry=ProviderRegistry())
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


@benchmark_app.command("template")
def benchmark_template(
    ctx: typer.Context,
    video: Annotated[Path, typer.Argument(help="Local gameplay recording to annotate.")],
    game_profile: Annotated[
        str, typer.Option("--game-profile", help="Stable lowercase game profile identifier.")
    ] = "unknown",
    case_id: Annotated[
        str | None, typer.Option("--case-id", help="Stable private benchmark case ID.")
    ] = None,
    output: Annotated[
        Path | None, typer.Option("--output", help="Private annotation JSON output path.")
    ] = None,
) -> None:
    """Inspect a local source and write an empty annotation template."""
    _execute(
        ctx, lambda options: _benchmark_template(options, video, game_profile, case_id, output)
    )


def _benchmark_template(
    options: RuntimeOptions,
    video: Path,
    game_profile: str,
    case_id: str | None,
    output: Path | None,
) -> None:
    result = create_annotation_template(
        video,
        _load(options).config,
        game_profile=game_profile,
        case_id=case_id,
        output=output,
    )
    typer.echo("[PASS] annotation template created locally (provider calls: ZERO)")
    typer.echo(f"annotation: {result.output_path}")
    typer.echo(f"case ID: {result.annotations.case_id}")
    typer.echo(f"source SHA-256: {result.annotations.source_sha256}")
    typer.echo(f"duration_ms: {result.annotations.source_duration_ms}")


@benchmark_app.command("annotate")
def benchmark_annotate(
    ctx: typer.Context,
    annotations: Annotated[
        Path, typer.Argument(help="Private annotation JSON document to edit locally.")
    ],
    no_open: Annotated[
        bool, typer.Option("--no-open", help="Do not open the local loopback URL automatically.")
    ] = False,
    port: Annotated[
        int,
        typer.Option(
            "--port",
            min=0,
            max=65_535,
            help="Loopback port; 0 selects an available port automatically.",
        ),
    ] = 0,
) -> None:
    """Open one private annotation document in a local-only browser helper."""
    _execute(ctx, lambda options: _benchmark_annotate(options, annotations, no_open, port))


def _benchmark_annotate(
    options: RuntimeOptions,
    annotations_path: Path,
    no_open: bool,
    port: int,
) -> None:
    server = AnnotationServer(annotations_path, _load(options).config, port=port)
    typer.echo(f"Local annotation URL: {server.url}")
    typer.echo("Source is read-only; annotation writes require an explicit Save.")
    typer.echo("provider calls: ZERO")
    if not no_open:
        import webbrowser

        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        typer.echo("Annotation server stopped.")
    finally:
        server.shutdown()


@benchmark_app.command("review-queue")
def benchmark_review_queue(
    ctx: typer.Context,
    queue: Annotated[Path, typer.Argument(help="Private development review_queue JSON.")],
    case: Annotated[
        list[str] | None,
        typer.Option("--case", help="Review only this case; repeat for multiple cases."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Private adjudication sidecar JSON output path."),
    ] = None,
    no_open: Annotated[
        bool,
        typer.Option("--no-open", help="Do not open the local loopback URL automatically."),
    ] = False,
    port: Annotated[
        int,
        typer.Option(
            "--port",
            min=0,
            max=65_535,
            help="Loopback port; 0 selects an available port automatically.",
        ),
    ] = 0,
) -> None:
    """Visually adjudicate a private calibration review queue without provider calls."""
    _execute(
        ctx,
        lambda _options: _benchmark_review_queue(queue, case, output, no_open, port),
    )


def _benchmark_review_queue(
    queue_path: Path,
    cases: list[str] | None,
    output: Path | None,
    no_open: bool,
    port: int,
) -> None:
    server = ReviewQueueServer(
        queue_path,
        cases=tuple(cases or ()),
        output_path=output,
        port=port,
    )
    typer.echo(f"Local review-queue URL: {server.url}")
    typer.echo(f"Cases: {', '.join(server.selected_cases)}")
    typer.echo(f"Adjudication sidecar: {server.output_path}")
    typer.echo("provider calls: ZERO")
    if not no_open:
        import webbrowser

        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        typer.echo("Review-queue server stopped.")
    finally:
        server.shutdown()


@benchmark_app.command("cross-case-suppression")
def benchmark_cross_case_suppression(
    ctx: typer.Context,
    queue: Annotated[Path, typer.Argument(help="Private development review_queue JSON.")],
    adjudication: Annotated[
        Path, typer.Option("--adjudication", help="Complete visual adjudication sidecar JSON.")
    ],
    audio_scale: Annotated[
        Path, typer.Option("--audio-scale", help="Provider-free cross-source audio-scale JSON.")
    ],
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Private cross-case suppression diagnostic JSON."),
    ] = None,
) -> None:
    """Evaluate normalized audio prominence against explicit cross-case visual labels."""
    _execute(
        ctx,
        lambda _options: _benchmark_cross_case_suppression(
            queue, adjudication, audio_scale, output
        ),
    )


def _benchmark_cross_case_suppression(
    queue: Path,
    adjudication: Path,
    audio_scale: Path,
    output: Path | None,
) -> None:
    result, target = run_cross_case_suppression(
        queue, adjudication, audio_scale, output_path=output
    )
    typer.echo("[PASS] cross-case suppression diagnostic ready (provider/API calls: ZERO)")
    typer.echo(f"Cases: {', '.join(result.selected_cases)}")
    typer.echo(
        f"Reviewed: {result.reviewed_count} | positives={result.positive_count} "
        f"boring={result.boring_count}"
    )
    typer.echo(
        "Normalized positive floor: "
        f"{result.protected_positive_min_audio_peak_over_loudness_db:.6f} dB"
    )
    typer.echo(
        f"Rejected boring intervals: {result.rejected_boring_count}/{result.boring_count_total}"
    )
    if result.surviving_boring_review_ids:
        typer.echo(
            "Boring intervals surviving the floor: "
            + ", ".join(result.surviving_boring_review_ids)
        )
    typer.echo(f"Verdict: {result.verdict}")
    typer.echo("Production threshold locked: NO")
    typer.echo(f"Private artifact: {target}")


@benchmark_app.command("make-review-proxies")
def benchmark_make_review_proxies(
    ctx: typer.Context,
    dataset: Annotated[Path, typer.Argument(help="Private BenchmarkDataset JSON manifest.")],
    small: Annotated[
        bool, typer.Option("--small", help="Use the compact 540p review profile.")
    ] = False,
    max_height: Annotated[
        int | None,
        typer.Option("--max-height", min=144, max=2160, help="Override the profile max height."),
    ] = None,
    max_fps: Annotated[
        float | None,
        typer.Option("--max-fps", min=1.0, max=120.0, help="Override the profile FPS cap."),
    ] = None,
    video_bitrate_kbps: Annotated[
        int | None,
        typer.Option(
            "--video-bitrate-kbps",
            min=100,
            max=20_000,
            help="Override the review video bitrate.",
        ),
    ] = None,
    audio_bitrate_kbps: Annotated[
        int | None,
        typer.Option(
            "--audio-bitrate-kbps",
            min=16,
            max=512,
            help="Override the review AAC bitrate.",
        ),
    ] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option("--output-dir", help="Private review-proxy output directory."),
    ] = None,
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Regenerate stale or existing proxies explicitly.")
    ] = False,
    allow_cpu_fallback: Annotated[
        bool,
        typer.Option(
            "--allow-cpu-fallback",
            help="Explicitly allow libx264 only when h264_nvenc is unavailable.",
        ),
    ] = False,
    case_id: Annotated[
        str | None,
        typer.Option("--case-id", help="Generate one dataset case for a bounded local smoke."),
    ] = None,
) -> None:
    """Generate private, full-timeline H.264/AAC review copies from a dataset."""
    _execute(
        ctx,
        lambda options: _benchmark_make_review_proxies(
            options,
            dataset,
            small,
            max_height,
            max_fps,
            video_bitrate_kbps,
            audio_bitrate_kbps,
            output_dir,
            overwrite,
            allow_cpu_fallback,
            case_id,
        ),
    )


def _benchmark_make_review_proxies(
    options: RuntimeOptions,
    dataset: Path,
    small: bool,
    max_height: int | None,
    max_fps: float | None,
    video_bitrate_kbps: int | None,
    audio_bitrate_kbps: int | None,
    output_dir: Path | None,
    overwrite: bool,
    allow_cpu_fallback: bool,
    case_id: str | None,
) -> None:
    profile = make_review_profile(
        small=small,
        max_height=max_height,
        max_fps=max_fps,
        video_bitrate_kbps=video_bitrate_kbps,
        audio_bitrate_kbps=audio_bitrate_kbps,
    )

    def show_progress(current_case_id: str, update: ProgressUpdate) -> None:
        percent = update.percent
        speed = update.speed
        if percent is None:
            return
        suffix = f" Speed: {speed}" if speed else ""
        typer.echo(f"{current_case_id} Progress: {percent:.0f}%{suffix}")

    result = make_review_proxies(
        dataset,
        _load(options).config,
        profile=profile,
        output_dir=output_dir,
        overwrite=overwrite,
        allow_cpu_fallback=allow_cpu_fallback,
        case_id=case_id,
        progress_callback=show_progress,
    )
    _print_review_proxy_summary(result)


def _print_review_proxy_summary(result: ReviewProxyBatchResult) -> None:
    typer.echo("[PASS] private review proxies ready (provider/API calls: ZERO)")
    typer.echo(f"Dataset cases discovered: {len(result.cases)}")
    typer.echo(f"Review proxies generated: {result.generated_count}")
    typer.echo(f"Cache hits: {result.cache_hit_count}")
    typer.echo(f"GPU encoder: {result.encoder}")
    typer.echo(f"Default profile: {result.profile.summary()}")
    for item in result.cases:
        typer.echo(
            f"{item.case_id}: {item.status} | source {_format_bytes(item.source_size_bytes)} "
            f"-> proxy {_format_bytes(item.proxy_size_bytes)} "
            f"({item.reduction_percent:.1f}% reduction) | "
            f"duration delta {item.duration_delta_ms} ms | encoder {item.encoder} | "
            f"proxy {item.proxy_path}"
        )
    typer.echo(f"Original total size: {_format_bytes(result.original_total_size)}")
    typer.echo(f"Review proxy total size: {_format_bytes(result.proxy_total_size)}")
    typer.echo(f"Total reduction: {result.total_reduction_percent:.1f}%")
    typer.echo(f"Largest proxy: {_format_bytes(result.largest_proxy_bytes)}")
    typer.echo(f"Smallest proxy: {_format_bytes(result.smallest_proxy_bytes)}")
    typer.echo(f"Maximum duration delta: {result.maximum_duration_delta_ms} ms")
    typer.echo(f"Review proxy manifest: {result.manifest_path}")


@benchmark_app.command("validate")
def benchmark_validate(
    ctx: typer.Context,
    annotations: Annotated[Path, typer.Argument(help="Annotation JSON document to validate.")],
) -> None:
    """Validate a private annotation document without provider calls."""
    _execute(ctx, lambda _options: _benchmark_validate(annotations))


@benchmark_app.command("plan-calibration")
def benchmark_plan_calibration(
    ctx: typer.Context,
    dataset: Annotated[Path, typer.Argument(help="Locked M8 real-gameplay dataset manifest.")],
    lock: Annotated[
        Path | None,
        typer.Option("--lock", help="Owner-confirmed ground-truth lock JSON."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Private calibration plan JSON output path."),
    ] = None,
    comparison_output: Annotated[
        Path | None,
        typer.Option("--comparison-output", help="Private future comparison manifest path."),
    ] = None,
    fx_usd_thb: Annotated[
        str | None,
        typer.Option(
            "--fx-usd-thb", help="Optional local USD/THB planning rate; no refresh is made."
        ),
    ] = None,
    revision: Annotated[
        str,
        typer.Option(
            "--revision",
            help="Fresh deterministic experiment revision label; planning remains provider-free.",
        ),
    ] = CALIBRATION_EXPERIMENT_REVISION,
) -> None:
    """Plan the two locked Gemini calibration arms without provider calls or uploads."""
    _execute(
        ctx,
        lambda options: _benchmark_plan_calibration(
            options, dataset, lock, output, comparison_output, fx_usd_thb, revision
        ),
    )


def _benchmark_plan_calibration(
    options: RuntimeOptions,
    dataset_path: Path,
    lock_path: Path | None,
    output: Path | None,
    comparison_output: Path | None,
    fx_usd_thb: str | None,
    revision: str,
) -> None:
    config = _load(options).config
    plan = build_calibration_plan(
        dataset_path,
        config,
        lock_path=lock_path,
        fx_usd_thb=fx_usd_thb,
        experiment_revision=revision,
    )
    data_dir = config.storage.data_dir.expanduser().resolve()
    plan_path = (
        output or data_dir / "benchmarks" / "private" / "m8b2_calibration_plan.json"
    ).resolve()
    comparison_path = (
        comparison_output
        or data_dir / "benchmarks" / "private" / "m8b2_calibration_comparison.json"
    ).resolve()
    write_calibration_artifacts(plan, plan_path, comparison_path)
    typer.echo(
        "[PASS] M8B2A calibration plan ready (provider/API calls: ZERO; media uploads: ZERO)"
    )
    typer.echo(f"Benchmark: {plan.benchmark_id}")
    typer.echo(f"Calibration cases: {', '.join(plan.calibration_case_ids)}")
    typer.echo(f"Validation sealed: {len(plan.validation_case_ids_sealed)} case(s)")
    typer.echo(
        f"Model A: {plan.arms[0].model} | windows={plan.arms[0].planned_scout_windows} "
        f"| requests={plan.arms[0].planned_provider_requests}"
    )
    typer.echo(
        f"Model B: {plan.arms[1].model} | windows={plan.arms[1].planned_scout_windows} "
        f"| requests={plan.arms[1].planned_provider_requests}"
    )
    for arm in plan.arms:
        thb = (
            str(arm.estimated_paid_equivalent_cost_thb)
            if arm.estimated_paid_equivalent_cost_thb is not None
            else "unavailable (no FX snapshot supplied)"
        )
        typer.echo(
            f"{arm.model}: media_ms={arm.planned_media_duration_ms} "
            f"input_exposure={arm.usage_estimate.input_video_tokens + arm.usage_estimate.input_audio_tokens} "
            f"paid_equivalent_usd={arm.estimated_paid_equivalent_cost_usd} paid_equivalent_thb={thb}"
        )
    typer.echo("Free-tier intent: YES | paid fallback authorized: NO")
    typer.echo(
        "RAW upload planned: NO | audio retained: YES | review proxies as provider inputs: NO"
    )
    typer.echo(f"Private calibration plan: {plan_path}")
    typer.echo(f"Private comparison manifest: {comparison_path}")


def _benchmark_validate(annotations_path: Path) -> None:
    summary = validate_annotations_file(annotations_path)
    typer.echo(f"source identity: {summary.source_identity}")
    typer.echo(f"annotation SHA-256: {summary.annotation_sha256}")
    typer.echo(f"benchmark ID: {summary.benchmark_id}")
    typer.echo(f"case ID: {summary.case_id}")
    typer.echo(
        f"annotation counts: matches={summary.match_count} highlights={summary.highlight_count}"
    )
    typer.echo(f"MUST_CATCH: {summary.must_catch_count}")
    typer.echo(f"WORTH_REVIEW: {summary.worth_review_count}")
    typer.echo(f"OPTIONAL: {summary.optional_count}")
    typer.echo(f"boring intervals: {summary.boring_interval_count}")
    typer.echo(
        "modality: "
        + ", ".join(f"{key}={value}" for key, value in sorted(summary.modality_breakdown.items()))
    )
    typer.echo(
        f"total annotated highlight duration_ms: {summary.total_annotated_highlight_duration_ms}"
    )
    typer.echo("provider calls: ZERO")


@benchmark_app.command("scout-readiness")
def benchmark_scout_readiness(
    ctx: typer.Context,
    session_id: Annotated[str, typer.Argument(help="Provider-clean calibration session identifier.")],
    dataset: Annotated[Path, typer.Option("--dataset", help="Private benchmark dataset manifest.")],
    annotations: Annotated[
        Path, typer.Option("--annotations", help="Declared calibration annotation JSON.")
    ],
    case_id: Annotated[
        str | None,
        typer.Option("--case-id", help="Calibration case ID when the dataset has more than one."),
    ] = None,
    output: Annotated[
        Path | None, typer.Option("--output", help="Private Scout readiness JSON output path.")
    ] = None,
) -> None:
    """Freeze a provider-free authorization/readiness artifact for one calibration Scout run."""
    _execute(
        ctx,
        lambda options: _benchmark_scout_readiness(
            options, session_id, dataset, annotations, case_id, output
        ),
    )


def _benchmark_scout_readiness(
    options: RuntimeOptions,
    session_id: str,
    dataset: Path,
    annotations: Path,
    case_id: str | None,
    output: Path | None,
) -> None:
    config = _load(options).config
    config = config.model_copy(
        update={"scout": config.scout.model_copy(update={"backend": "gemini"})}
    )
    artifact, target = run_scout_calibration_readiness(
        session_id,
        dataset,
        annotations,
        config,
        case_id=case_id,
        output_path=output,
    )
    typer.echo("[PASS] Scout calibration readiness frozen (provider/API calls: ZERO)")
    typer.echo(f"Case: {artifact.case_id} | split: {artifact.split}")
    typer.echo(f"Session: {artifact.session_id}")
    typer.echo(
        f"Scout: {artifact.provider}/{artifact.model} | prompt={artifact.window_prompt_version}"
    )
    typer.echo(
        f"Windows/requests: {len(artifact.windows)}/{artifact.planned_provider_requests}"
    )
    typer.echo(
        "Aggregate maximum reserved: "
        f"{_format_micro_thb(artifact.aggregate_maximum_reserved_micro_thb)} "
        f"({artifact.aggregate_maximum_reserved_micro_thb} micro-THB)"
    )
    typer.echo(
        "Monthly available: "
        f"{_format_micro_thb(artifact.monthly_available_micro_thb)} "
        f"({artifact.monthly_available_micro_thb} micro-THB)"
    )
    typer.echo(f"Post-reservation headroom: {artifact.post_reservation_headroom_micro_thb} micro-THB")
    typer.echo(f"Budget gate: {'BLOCKED' if artifact.budget_blocked else 'PASS'} ({artifact.budget_reason})")
    typer.echo("Paid-response cache assumption: ZERO")
    typer.echo("Provider calls: ZERO | remote uploads: ZERO | ledger reservations: ZERO")
    typer.echo("Semantic quality available: NO")
    typer.echo("Fresh attempt/exposure authorization required before execution: YES")
    typer.echo(f"Readiness artifact: {target}")
    if not artifact.ready_for_authorized_execution:
        raise ConfigError("Scout calibration readiness is blocked.", hint=artifact.budget_reason)


@benchmark_app.command("boundary-feasibility")
def benchmark_boundary_feasibility(
    ctx: typer.Context,
    session_id: Annotated[str, typer.Argument(help="Completed calibration session identifier.")],
    dataset: Annotated[Path, typer.Option("--dataset", help="Private benchmark dataset manifest.")],
    annotations: Annotated[
        Path, typer.Option("--annotations", help="Declared calibration annotation JSON.")
    ],
    output: Annotated[
        Path | None, typer.Option("--output", help="Private feasibility JSON output path.")
    ] = None,
) -> None:
    """Measure boundary-refinement headroom on calibration data without provider calls."""
    _execute(
        ctx,
        lambda options: _benchmark_boundary_feasibility(
            options, session_id, dataset, annotations, output
        ),
    )


def _benchmark_boundary_feasibility(
    options: RuntimeOptions,
    session_id: str,
    dataset: Path,
    annotations: Path,
    output: Path | None,
) -> None:
    config = _load(options).config
    result, target = run_boundary_refinement_feasibility(
        session_id,
        dataset,
        annotations,
        config,
        output_path=output,
    )
    typer.echo("[PASS] boundary-refinement calibration feasibility (provider/API calls: ZERO)")
    typer.echo(f"Case: {result.case_id} | session: {result.session_id}")
    typer.echo(
        "Scout provenance: "
        f"backend={result.scout_backend} | model={result.scout_model or 'unknown'} | "
        f"prompt={result.scout_prompt_version or 'unknown'} | "
        f"source={result.scout_provenance_source or 'unknown'}"
    )
    if not result.semantic_quality_applicable:
        typer.echo("[WARN] Semantic Scout quality interpretation: NOT APPLICABLE")
    if not result.precision_tuning_safe:
        typer.echo(
            "[WARN] Global strict precision tuning: NOT APPLICABLE until annotations are exhaustive "
            "and semantic Scout quality is applicable"
        )
    if result.false_positive_suppression_safe:
        typer.echo(
            "[PASS] Candidate-level false-positive suppression: SAFE for adjudicated current "
            "predictions"
        )
        if result.score_confidence_threshold_suppression_headroom:
            typer.echo(
                "[PASS] Existing score/confidence threshold headroom: YES; rejectable negatives: "
                + ", ".join(result.threshold_rejectable_confirmed_negative_candidate_ids)
            )
        else:
            typer.echo(
                "[INFO] Existing score/confidence threshold headroom: NONE without dropping a "
                "reviewed positive"
            )
    elif result.human_review_required_candidate_ids:
        typer.echo(
            "[WARN] Candidate-level false-positive suppression: BLOCKED pending human review"
        )
    if result.quality_interpretation_warning is not None:
        typer.echo(f"[WARN] {result.quality_interpretation_warning}")
    typer.echo(
        f"Strict: {result.strict_match_count}/{result.ground_truth_count} truth matched; "
        f"precision={result.strict_precision} recall={result.strict_recall}; "
        f"annotation_coverage={result.annotation_coverage}"
    )
    if result.strict_unmatched_candidate_ids:
        typer.echo(
            "Strict-unmatched candidates: " + ", ".join(result.strict_unmatched_candidate_ids)
        )
    if result.confirmed_negative_candidate_ids:
        typer.echo(
            "Confirmed negative candidates: "
            + ", ".join(result.confirmed_negative_candidate_ids)
        )
    if result.human_review_required_candidate_ids:
        typer.echo(
            "Human-review-required candidates: "
            + ", ".join(result.human_review_required_candidate_ids)
        )
    typer.echo(
        f"Anchor overlap: {result.anchor_overlap_annotation_count}/{result.ground_truth_count}; "
        f"boundary headroom: {result.boundary_headroom_count}; "
        f"detection gaps: {result.detection_gap_count}"
    )
    typer.echo(
        f"MUST_CATCH detection gaps: {result.must_catch_detection_gap_count}; "
        f"MUST_CATCH boundary headroom: {result.must_catch_boundary_headroom_count}"
    )
    typer.echo(f"Diagnostic verdict: {result.diagnostic_verdict}")
    typer.echo(
        "Ground-truth-derived candidate IDs are calibration diagnostics only; "
        "never production selection."
    )
    typer.echo(f"Feasibility artifact: {target}")


@benchmark_app.command("suppression-feasibility")
def benchmark_suppression_feasibility(
    ctx: typer.Context,
    session_id: Annotated[str, typer.Argument(help="Reviewed calibration session identifier.")],
    feasibility: Annotated[
        Path,
        typer.Option(
            "--feasibility",
            help="Reviewed boundary-feasibility JSON with completed candidate adjudication.",
        ),
    ],
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Private candidate-suppression feasibility JSON output path."),
    ] = None,
) -> None:
    """Measure provider-free local-signal headroom for reviewed false positives."""
    _execute(
        ctx,
        lambda options: _benchmark_suppression_feasibility(
            options, session_id, feasibility, output
        ),
    )


def _benchmark_suppression_feasibility(
    options: RuntimeOptions,
    session_id: str,
    feasibility: Path,
    output: Path | None,
) -> None:
    config = _load(options).config
    result, target = run_candidate_suppression_feasibility(
        session_id,
        feasibility,
        config,
        output_path=output,
    )
    typer.echo("[PASS] candidate-suppression calibration feasibility (provider/API calls: ZERO)")
    typer.echo(f"Case: {result.case_id} | session: {result.session_id}")
    typer.echo(
        "Scout provenance: "
        f"backend={result.scout_backend} | model={result.scout_model or 'unknown'} | "
        f"prompt={result.scout_prompt_version or 'unknown'}"
    )
    typer.echo(
        "Reviewed candidates: "
        f"positives={result.protected_positive_count} "
        f"confirmed_negatives={result.confirmed_negative_count}"
    )
    typer.echo(
        "Existing score/confidence threshold headroom: "
        + ("YES" if result.score_confidence_threshold_suppression_headroom else "NONE")
    )
    if result.protected_positive_min_audio_peak_db is not None:
        typer.echo(
            "Audio peak dB lower-bound diagnostic: "
            f"keep >= {result.protected_positive_min_audio_peak_db:.6f} dB; "
            f"rejects {len(result.audio_peak_db_threshold_rejectable_negative_candidate_ids)}/"
            f"{result.confirmed_negative_count} confirmed negatives"
        )
    else:
        typer.echo("Audio peak dB lower-bound diagnostic: NOT APPLICABLE")
    if result.protected_positive_min_audio_peak_over_loudness_db is not None:
        typer.echo(
            "Audio peak-over-loudness lower-bound diagnostic: "
            f"keep >= {result.protected_positive_min_audio_peak_over_loudness_db:.6f} dB; "
            f"rejects "
            f"{len(result.audio_peak_over_loudness_threshold_rejectable_negative_candidate_ids)}/"
            f"{result.confirmed_negative_count} confirmed negatives"
        )
    else:
        typer.echo("Audio peak-over-loudness lower-bound diagnostic: NOT APPLICABLE")
    if result.protected_positive_min_audio_mean_db is not None:
        typer.echo(
            "Audio mean dB lower-bound diagnostic: "
            f"keep >= {result.protected_positive_min_audio_mean_db:.6f} dB; "
            f"rejects {len(result.audio_mean_db_threshold_rejectable_negative_candidate_ids)}/"
            f"{result.confirmed_negative_count} confirmed negatives"
        )
    else:
        typer.echo("Audio mean dB lower-bound diagnostic: NOT APPLICABLE")
    typer.echo(f"Diagnostic verdict: {result.diagnostic_verdict}")
    typer.echo(f"[WARN] {result.warning}")
    typer.echo(f"Suppression feasibility artifact: {target}")


@benchmark_app.command("pack-boundary-feasibility")
def benchmark_pack_boundary_feasibility(
    ctx: typer.Context,
    session_id: Annotated[str, typer.Argument(help="Completed calibration session identifier.")],
    dataset: Annotated[Path, typer.Option("--dataset", help="Private benchmark dataset manifest.")],
    annotations: Annotated[
        Path, typer.Option("--annotations", help="Declared calibration annotation JSON.")
    ],
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir",
            help="New directory for the portable JSON-only feasibility bundle.",
        ),
    ],
) -> None:
    """Pack calibration feasibility evidence for safe cross-machine transfer."""
    _execute(
        ctx,
        lambda options: _benchmark_pack_boundary_feasibility(
            options, session_id, dataset, annotations, output_dir
        ),
    )


def _benchmark_pack_boundary_feasibility(
    options: RuntimeOptions,
    session_id: str,
    dataset: Path,
    annotations: Path,
    output_dir: Path,
) -> None:
    config = _load(options).config
    result = pack_boundary_refinement_feasibility_bundle(
        session_id,
        dataset,
        annotations,
        config,
        output_dir=output_dir,
    )
    annotation_path = result.root / "annotations" / f"{result.manifest.case_id}.json"
    typer.echo(
        "[PASS] portable boundary-feasibility bundle (provider/API calls: ZERO; media files: ZERO)"
    )
    typer.echo(f"Case: {result.manifest.case_id} | session: {result.manifest.session_id}")
    typer.echo(f"Diagnostic verdict: {result.manifest.diagnostic_verdict}")
    typer.echo(
        "Scout provenance: "
        f"{result.manifest.scout_provenance_source} | "
        f"model={result.manifest.scout_model or '-'} | "
        f"prompt={result.manifest.scout_prompt_version or '-'}"
    )
    typer.echo("Validation/holdout included: NO")
    typer.echo("Source path sanitized: YES")
    typer.echo(f"Bundle: {result.root}")
    typer.echo(
        "Portable rerun: highlight --data-dir "
        f'"{result.root / "data"}" benchmark boundary-feasibility '
        f'"{result.manifest.session_id}" --dataset "{result.root / "dataset.json"}" '
        f'--annotations "{annotation_path}"'
    )


@benchmark_app.command("evaluate")
def benchmark_evaluate(
    ctx: typer.Context,
    session_id: Annotated[str, typer.Argument(help="Completed local session identifier.")],
    annotations: Annotated[
        Path, typer.Option("--annotations", help="Private annotation JSON document.")
    ],
    split: Annotated[
        str,
        typer.Option("--split", help="calibration or validation; validation is never tuned here."),
    ] = "calibration",
    output: Annotated[
        Path | None, typer.Option("--output", help="Private evaluation JSON output path.")
    ] = None,
    dataset_manifest: Annotated[
        Path | None,
        typer.Option(
            "--dataset",
            help="Optional dataset manifest whose declared full evaluation policy is authoritative.",
        ),
    ] = None,
) -> None:
    """Evaluate an already completed session entirely from local artifacts."""
    _execute(
        ctx,
        lambda options: _benchmark_evaluate(
            options, session_id, annotations, split, output, dataset_manifest
        ),
    )


def _benchmark_evaluate(
    options: RuntimeOptions,
    session_id: str,
    annotations_path: Path,
    split: str,
    output: Path | None,
    dataset_manifest: Path | None,
) -> None:
    try:
        split_value = BenchmarkSplit(split.strip().lower())
    except ValueError as exc:
        raise ConfigError("Benchmark split must be calibration or validation.") from exc
    config = _load_persisted_session_config(options, session_id)
    loaded_annotations = load_annotations(annotations_path)
    declared_policy = None
    if dataset_manifest is not None:
        dataset_path = dataset_manifest.expanduser().resolve()
        try:
            raw_dataset = read_json(dataset_path)
            if not isinstance(raw_dataset, dict):
                raise ValueError("dataset manifest must be an object")
            dataset = BenchmarkDataset.model_validate(raw_dataset)
        except Exception as exc:
            raise ConfigError(
                "Benchmark dataset manifest is invalid; evaluation cannot guess its policy.",
                hint=str(dataset_manifest),
            ) from exc
        matching_cases = [
            case for case in dataset.cases if case.case_id == loaded_annotations.case_id
        ]
        if not matching_cases:
            raise ConfigError(
                f"Annotation case {loaded_annotations.case_id} is not declared by the dataset."
            )
        case = matching_cases[0]
        expected_annotation_hash = annotation_sha256(annotations_path)
        if not benchmark_identity_compatible(
            dataset_path,
            dataset,
            case,
            evaluation_benchmark_id=loaded_annotations.benchmark_id,
            evaluation_case_id=loaded_annotations.case_id,
            evaluation_source_sha256=loaded_annotations.source_sha256,
            evaluation_annotation_sha256=expected_annotation_hash,
            expected_annotation_sha256=expected_annotation_hash,
        ):
            raise ConfigError(
                "Annotation benchmark identity does not match the declared case and lock."
            )
        if loaded_annotations.game_profile != case.game_profile:
            raise ConfigError("Annotation game profile does not match the dataset manifest.")
        if split_value is not case.split:
            raise ConfigError(
                "Requested split does not match the dataset case; use the declared case split."
            )
        declared_policy = dataset.evaluation_policy
    evaluation = evaluate_session(
        session_id,
        annotations_path,
        config,
        policy=declared_policy,
        split=split_value,
    )
    if dataset_manifest is not None:
        assert declared_policy is not None
        if evaluation.source_sha256 != case.expected_source_sha256:
            raise ConfigError("Completed session source hash does not match the dataset case.")
        if not benchmark_identity_compatible(
            dataset_path,
            dataset,
            case,
            evaluation_benchmark_id=evaluation.benchmark_id,
            evaluation_case_id=evaluation.case_id,
            evaluation_source_sha256=evaluation.source_sha256,
            evaluation_annotation_sha256=evaluation.annotation_sha256,
            expected_annotation_sha256=annotation_sha256(annotations_path),
        ):
            raise ConfigError("Completed evaluation benchmark identity does not match the case.")
        if evaluation.evaluation_policy_fingerprint != declared_policy.fingerprint():
            raise ConfigError("Evaluation policy fingerprint does not match the dataset case.")
    target = (
        output.expanduser().resolve()
        if output is not None
        else config.storage.data_dir.resolve()
        / "benchmarks"
        / "results"
        / f"{loaded_annotations.case_id}.json"
    )
    from game_highlight_finder.benchmark.evaluator import persist_evaluation

    persist_evaluation(evaluation, target)
    typer.echo(f"[PASS] benchmark evaluation written: {target}")
    typer.echo(f"precision: {evaluation.primary_metrics.precision}")
    typer.echo(f"recall: {evaluation.primary_metrics.recall}")
    typer.echo(f"F1: {evaluation.primary_metrics.f1}")
    typer.echo("provider calls: ZERO")


@benchmark_app.command("aggregate")
def benchmark_aggregate(
    ctx: typer.Context,
    dataset_manifest: Annotated[
        Path, typer.Argument(help="Private benchmark dataset or comparison manifest JSON.")
    ],
    output: Annotated[
        Path | None, typer.Option("--output", help="Aggregate JSON output path.")
    ] = None,
    report_output: Annotated[
        Path | None, typer.Option("--report-output", help="Human-readable Markdown output path.")
    ] = None,
) -> None:
    """Aggregate a dataset or compare result sets without provider calls."""
    _execute(ctx, lambda _options: _benchmark_aggregate(dataset_manifest, output, report_output))


def _benchmark_aggregate(
    dataset_manifest: Path,
    output: Path | None,
    report_output: Path | None,
) -> None:
    result = aggregate_manifest(
        dataset_manifest,
        output_path=output,
        markdown_path=report_output,
    )
    typer.echo(f"[PASS] aggregate JSON: {result.json_path}")
    typer.echo(f"[PASS] aggregate Markdown: {result.markdown_path}")
    typer.echo(f"groups: {len(result.aggregate.groups)}")
    typer.echo("provider calls: ZERO")


@benchmark_app.command("compare")
def benchmark_compare(
    ctx: typer.Context,
    comparison_manifest: Annotated[
        Path, typer.Argument(help="Private benchmark comparison manifest JSON.")
    ],
    output: Annotated[
        Path | None, typer.Option("--output", help="Comparison aggregate JSON output path.")
    ] = None,
    report_output: Annotated[
        Path | None, typer.Option("--report-output", help="Comparison Markdown output path.")
    ] = None,
) -> None:
    """Compare multiple local experiment result sets over identical cases."""
    _execute(
        ctx,
        lambda _options: _benchmark_compare(comparison_manifest, output, report_output),
    )


def _benchmark_compare(
    comparison_manifest: Path,
    output: Path | None,
    report_output: Path | None,
) -> None:
    result = aggregate_comparison(
        comparison_manifest,
        output_path=output,
        markdown_path=report_output,
    )
    typer.echo(f"[PASS] comparison JSON: {result.json_path}")
    typer.echo(f"[PASS] comparison Markdown: {result.markdown_path}")
    typer.echo(f"groups: {len(result.aggregate.groups)}")
    typer.echo("provider calls: ZERO")


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
    if dry_run and m6:
        if config.scout.backend != "gemini":
            raise ConfigError("--dry-run --m6 is only available with the Gemini Scout backend.")
        m6_local = analyze_m6_source(video, config, stop_after="windows")
        if m6_local.windows is None:
            raise ConfigError("M6 dry-run did not produce the expected local Scout windows.")
        paths = session_paths(config.storage.data_dir, m6_local.ingest.session_id)
        signal_summaries: dict[str, dict[str, Any]] = {}
        for window in m6_local.windows.windows:
            summary_path = paths.scout_windows_dir / window.window_id / "signals.json"
            if not summary_path.is_file():
                raise ConfigError(
                    "M6 dry-run is missing a local window signal summary.",
                    hint=str(summary_path),
                )
            summary = read_json(summary_path)
            if not isinstance(summary, dict):
                raise ConfigError(
                    "M6 dry-run window signal summary is not a JSON object.",
                    hint=str(summary_path),
                )
            signal_summaries[window.window_id] = summary
        m6_preflight = aggregate_window_preflight(
            m6_local.ingest.source,
            m6_local.windows.windows,
            config,
            cached_window_ids=set(),
            local_signal_summaries=signal_summaries,
        )
        typer.echo("[PASS] M6 Gemini window preflight: provider/API calls ZERO")
        typer.echo(f"Session: {m6_local.ingest.session_id}")
        typer.echo(f"Model: {config.scout.model}")
        typer.echo(f"Prompt version: {config.scout.window_prompt_version}")
        typer.echo(f"Windows: {m6_preflight.total_windows}")
        typer.echo("Paid-response cache assumption: ZERO (conservative dry-run)")
        for window_id, estimate in m6_preflight.window_estimates_micro_thb.items():
            typer.echo(f"  {window_id}: maximum reserved {_format_micro_thb(estimate)}")
        typer.echo(f"Aggregate maximum reserved: {_format_micro_thb(m6_preflight.estimated_micro_thb)}")
        available_micro_thb = m6_preflight.available_micro_thb or 0
        headroom_micro_thb = available_micro_thb - m6_preflight.estimated_micro_thb
        typer.echo(f"Aggregate maximum reserved micro-THB: {m6_preflight.estimated_micro_thb}")
        typer.echo(f"Monthly available budget: {_format_micro_thb(available_micro_thb)}")
        typer.echo(f"Monthly available budget micro-THB: {available_micro_thb}")
        typer.echo(f"Post-reservation headroom micro-THB: {headroom_micro_thb}")
        typer.echo(f"Budget gate: {'BLOCKED' if m6_preflight.blocked else 'PASS'} ({m6_preflight.reason})")
        typer.echo("Provider transport constructed: NO")
        typer.echo("Remote upload: NO")
        typer.echo("Ledger reservation: NO")
        if m6_preflight.blocked:
            raise ConfigError(
                "M6 Gemini window preflight is blocked.",
                hint=m6_preflight.reason,
            )
        return
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
        if m6_result.scout is not None:
            _echo_execution_activity(m6_result.scout.activity)
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
        typer.echo(
            "Thinking level: "
            f"{preflight.thinking_level or 'omitted (model default ' + preflight.effective_thinking_mode + ')'}"
        )
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
    if m6.scout is not None:
        _echo_execution_activity(m6.scout.activity)


def _echo_execution_activity(activity: ExecutionActivity) -> None:
    """Print only provider activity observed during this local invocation."""

    if activity.scout_backend == "fake":
        typer.echo("Real Gemini API calls: ZERO")
    elif activity.scout_backend == "gemini" and activity.provider_generation_calls == 0:
        typer.echo("Gemini generation calls this run: 0")
    elif activity.scout_backend == "gemini":
        typer.echo(f"Gemini generation calls this run: {activity.provider_generation_calls}")
    else:
        typer.echo("Provider generation activity: unknown")


@app.command("refine-boundaries")
def refine_boundaries(
    ctx: typer.Context,
    session_id: Annotated[str, typer.Argument(help="Completed session identifier.")],
    candidate_ids: Annotated[
        list[str],
        typer.Argument(help="One or more explicit candidate IDs to refine in caller order."),
    ],
    execute: Annotated[
        bool,
        typer.Option(
            "--execute",
            help="Execute Gemini refinement after aggregate preflight; default is preflight only.",
        ),
    ] = False,
    allow_remote_upload: Annotated[
        bool,
        typer.Option(
            "--allow-remote-upload",
            help="Freshly authorize candidate-local slowed.mp4 uploads for this invocation.",
        ),
    ] = False,
    minimum_confidence: Annotated[
        float,
        typer.Option(
            "--minimum-confidence",
            min=0.0,
            max=1.0,
            help="Minimum provider confidence required to replace Scout event boundaries.",
        ),
    ] = 0.5,
) -> None:
    """Preflight or explicitly execute Gemini boundary refinement for selected candidates."""

    _execute(
        ctx,
        lambda options: _refine_boundaries(
            options,
            session_id,
            candidate_ids,
            execute=execute,
            allow_remote_upload=allow_remote_upload,
            minimum_confidence=minimum_confidence,
        ),
    )


def _refine_boundaries(
    options: RuntimeOptions,
    session_id: str,
    candidate_ids: list[str],
    *,
    execute: bool,
    allow_remote_upload: bool,
    minimum_confidence: float,
) -> None:
    if execute and not allow_remote_upload:
        raise ConfigError(
            "Gemini boundary refinement execution requires --allow-remote-upload.",
            hint="Preflight is the default and performs no provider call or upload.",
        )

    config, source, proxy, session_map = _load_boundary_refinement_inputs(options, session_id)
    if allow_remote_upload:
        config = config.model_copy(
            update={"scout": config.scout.model_copy(update={"allow_remote_upload": True})}
        )

    if not execute:
        preflight = preflight_gemini_boundary_refinement_session_batch(
            source,
            proxy,
            session_map,
            config,
            candidate_ids=candidate_ids,
        )
        typer.echo("[PASS] Gemini boundary refinement preflight (provider/API calls: ZERO)")
        typer.echo(f"Session: {session_id}")
        typer.echo(f"Candidates: {len(preflight.selected_candidate_ids)}")
        for item in preflight.items:
            if getattr(item, "cache_hit", False):
                typer.echo(f"  {item.candidate_id}: SETTLED cache hit; new reservation ZERO")
            else:
                assert item.preflight is not None
                typer.echo(
                    f"  {item.candidate_id}: maximum reserved "
                    f"{_format_micro_thb(item.preflight.quote.reserved_cost_micro_thb)}"
                )
        typer.echo(
            "Aggregate maximum reserved: "
            f"{_format_micro_thb(preflight.total_reserved_cost_micro_thb)}"
        )
        typer.echo(f"Monthly available budget: {_format_micro_thb(preflight.available_micro_thb)}")
        typer.echo("RAW source upload: NO")
        typer.echo("Provider media if executed: candidate-local slowed.mp4 only")
        return

    result = run_gemini_boundary_refinement_batch_with_transport_factory(
        source,
        proxy,
        session_map,
        config,
        candidate_ids=candidate_ids,
        transport_factory=lambda: GenAITransport(api_key_env=config.scout.api_key_env),
        minimum_confidence=minimum_confidence,
    )
    typer.echo("[PASS] Gemini boundary refinement batch completed")
    typer.echo(f"Session: {session_id}")
    typer.echo(f"Candidates: {len(result.artifact.selected_candidate_ids)}")
    typer.echo(
        "Aggregate preflight maximum reserved: "
        f"{_format_micro_thb(result.preflight.total_reserved_cost_micro_thb)}"
    )
    typer.echo(f"Generated provider responses: {result.generated_responses}")
    typer.echo(f"Provider response cache hits: {result.response_cache_hits}")
    typer.echo(f"Local media cache hits: {result.media_cache_hits}")
    typer.echo(f"Refined session map: {result.refined_session_map_path}")
    typer.echo(f"Batch artifact: {result.artifact_path}")
    typer.echo("Original session_map.json: unchanged")


def _load_boundary_refinement_inputs(
    options: RuntimeOptions,
    session_id: str,
) -> tuple[AppConfig, SourceAsset, ProxyResult, SessionMap]:
    config = _load_persisted_session_config(options, session_id)
    paths = session_paths(config.storage.data_dir, session_id)
    if not paths.root.is_dir():
        raise ConfigError(f"Session does not exist: {session_id}")
    if not paths.source.is_file() or not paths.session_map.is_file():
        raise ConfigError(
            "Boundary refinement requires committed source and session_map artifacts."
        )

    source = source_from_artifact(paths.source)
    if not source.path.is_file():
        raise ConfigError("Original source is missing; boundary refinement cannot continue.")
    try:
        session_map = SessionMap.model_validate(read_json(paths.session_map))
    except Exception as exc:
        raise ConfigError(
            "Stored session map is invalid; boundary refinement cannot continue.", hint=str(exc)
        ) from exc

    proxy_path = paths.proxy_dir / "analysis_proxy.mp4"
    metadata_path = paths.proxy_dir / "metadata.json"
    if not proxy_path.is_file() or not metadata_path.is_file():
        raise ConfigError("Boundary refinement requires the committed analysis proxy and metadata.")
    try:
        metadata = ProxyMetadata.model_validate(read_json(metadata_path))
    except Exception as exc:
        raise ConfigError(
            "Stored proxy metadata is invalid; boundary refinement cannot continue.", hint=str(exc)
        ) from exc
    analysis_audio = paths.audio_dir / "analysis_audio.m4a"
    proxy = ProxyResult(
        session_id=session_id,
        cache_hit=True,
        cache_reason="persisted-artifact",
        proxy_path=proxy_path,
        audio_path=analysis_audio if metadata.audio_present and analysis_audio.is_file() else None,
        metadata_path=metadata_path,
        metadata=metadata,
        session_dir=paths.root,
    )
    return config, source, proxy, session_map


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
    config = _load_persisted_session_config(options, session_id)
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
