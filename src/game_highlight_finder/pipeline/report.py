"""Offline, self-contained M7 HTML report renderer."""

# ruff: noqa: E501

from __future__ import annotations

import base64
import hashlib
import html
import json
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from game_highlight_finder.config import AppConfig
from game_highlight_finder.cost.service import CostService
from game_highlight_finder.domain.models import Candidate, Manifest, Match, SessionMap, SourceAsset
from game_highlight_finder.domain.time import format_duration
from game_highlight_finder.errors import ValidationError
from game_highlight_finder.pipeline.extraction import ExtractionManifest
from game_highlight_finder.pipeline.ranking import RankingArtifact, rank_session_map
from game_highlight_finder.providers import ProviderRegistry
from game_highlight_finder.storage.atomic import atomic_write_bytes, atomic_write_json, read_json
from game_highlight_finder.storage.hashing import hash_file
from game_highlight_finder.storage.sessions import SessionPaths

REPORT_VERSION = "m7-report-v1"
MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024


class ReportResult:
    def __init__(
        self, path: Path, *, cache_hit: bool, cache_key: str, warnings: tuple[str, ...] = ()
    ) -> None:
        self.path = path
        self.cache_hit = cache_hit
        self.cache_key = cache_key
        self.warnings = warnings


def review_duration_ms(candidates: Iterable[Candidate]) -> int:
    intervals = sorted(
        (candidate.clip_start_ms, candidate.clip_end_ms)
        for candidate in candidates
        if candidate.clip_start_ms is not None
        and candidate.clip_end_ms is not None
        and candidate.clip_end_ms > candidate.clip_start_ms
    )
    total = 0
    current_start: int | None = None
    current_end: int | None = None
    for start, end in intervals:
        assert start is not None and end is not None
        if current_start is None:
            current_start, current_end = start, end
        elif current_end is not None and start <= current_end:
            current_end = max(current_end or end, end)
        else:
            total += (current_end or 0) - current_start
            current_start, current_end = start, end
    if current_start is not None:
        total += (current_end or 0) - current_start
    return total


def _human_duration(duration_ms: int) -> str:
    total_seconds = max(0, duration_ms) // 1000
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _relative_url(path: Path, *, report_path: Path) -> str:
    relative = path.relative_to(report_path.parent.parent).as_posix()
    return quote("../" + relative, safe="/:.-_~")


def _cost_summary(config: AppConfig, session_id: str) -> dict[str, Any]:
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
    grouping: dict[str, int] = defaultdict(int)
    for call in calls:
        grouping[f"{call.provider}/{call.model}/{call.stage}"] += call.exposure_micro_thb
    hold = service.ledger.safety_hold()
    payload = {
        "settled_micro_thb": settled,
        "reserved_micro_thb": reserved,
        "in_flight_micro_thb": in_flight,
        "ambiguous_micro_thb": ambiguous,
        "call_count": len(calls),
        "grouping": dict(sorted(grouping.items())),
        "safety_hold_active": bool(hold),
        "safety_hold_reason": hold.reason if hold else None,
        "calls": [
            {
                "call_id": call.call_id,
                "status": call.status.value,
                "provider": call.provider,
                "model": call.model,
                "stage": call.stage,
                "exposure_micro_thb": call.exposure_micro_thb,
            }
            for call in calls
        ],
    }
    return payload


def _validate_extraction(
    paths: SessionPaths,
    session_map: SessionMap,
    config: AppConfig,
) -> tuple[ExtractionManifest | None, dict[str, Any], list[str]]:
    warnings: list[str] = []
    if not session_map.candidates:
        return None, {}, warnings
    if not paths.extraction_manifest.is_file():
        raise ValidationError(
            "Candidate extraction manifest is missing; report generation refused.",
            hint="Run: highlight resume " + session_map.session_id,
        )
    try:
        extraction = ExtractionManifest.model_validate(read_json(paths.extraction_manifest))
    except Exception as exc:
        raise ValidationError(
            "Candidate extraction manifest is invalid; report generation refused.",
            hint="Run: highlight resume " + session_map.session_id,
        ) from exc
    records: dict[str, Any] = {}
    for record in extraction.records:
        if record.status != "COMPLETED":
            continue
        output = (paths.root / record.output_path).resolve()
        try:
            output.relative_to(paths.root.resolve())
        except ValueError as exc:
            raise ValidationError(
                f"Candidate artifact path escapes the session: {record.candidate_id}",
                hint="Run: highlight resume " + session_map.session_id,
            ) from exc
        if not output.is_file() or record.output_sha256 != hash_file(output):
            raise ValidationError(
                f"Candidate artifact is missing or hash-invalid: {record.candidate_id}",
                hint="Run: highlight resume " + session_map.session_id,
            )
        thumbnail_ok = False
        if config.report.embed_thumbnails and record.thumbnail_path:
            thumbnail = (paths.root / record.thumbnail_path).resolve()
            try:
                thumbnail.relative_to(paths.root.resolve())
            except ValueError as exc:
                raise ValidationError(
                    f"Thumbnail path escapes the session: {record.candidate_id}",
                    hint="Run: highlight resume " + session_map.session_id,
                ) from exc
            thumbnail_ok = (
                thumbnail.is_file()
                and record.thumbnail_sha256 is not None
                and record.thumbnail_sha256 == hash_file(thumbnail)
                and thumbnail.stat().st_size <= MAX_THUMBNAIL_BYTES
            )
            if not thumbnail_ok:
                warnings.append(f"Thumbnail unavailable or invalid for {record.candidate_id}.")
        records[record.candidate_id] = {
            "record": record,
            "output": output,
            "thumbnail_ok": thumbnail_ok,
        }
    missing = [
        candidate.candidate_id
        for candidate in session_map.candidates
        if candidate.candidate_id not in records
    ]
    if missing:
        raise ValidationError(
            "One or more candidate extraction artifacts are incomplete; report generation refused.",
            hint="Run: highlight resume " + session_map.session_id,
        )
    return extraction, records, warnings


def _cache_key(
    session_map: SessionMap,
    ranking: RankingArtifact,
    extraction: ExtractionManifest | None,
    records: dict[str, Any],
    manifest: Manifest,
    cost: dict[str, Any],
    config: AppConfig,
) -> str:
    extraction_payload: dict[str, Any] | None = None
    if extraction is not None:
        extraction_payload = extraction.model_dump(mode="json")
        # Manifest timestamps describe writes, not report semantics. Excluding
        # them preserves warm-run reuse when every verified clip is unchanged.
        extraction_payload.pop("created_at", None)
        extraction_payload.pop("updated_at", None)
    payload = {
        "report_version": REPORT_VERSION,
        "session_map": hashlib.sha256(
            json.dumps(
                session_map.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest(),
        "ranking": ranking.model_dump(mode="json"),
        "extraction": extraction_payload,
        "thumbnails": {
            key: value["record"].thumbnail_sha256 for key, value in sorted(records.items())
        },
        # Exclude the report stage itself to avoid a self-referential cold/warm
        # miss when the manifest transitions PENDING -> COMPLETED after publish.
        "stages": {
            key: value.status.value
            for key, value in sorted(manifest.stages.items())
            if key != "report"
        },
        "cost": cost,
        "report_config": config.report.model_dump(mode="json"),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _report_bytes_identity(value: bytes) -> tuple[str, int]:
    return hashlib.sha256(value).hexdigest(), len(value)


def _verified_report_cache_hit(paths: SessionPaths, *, cache_key: str) -> bool:
    """Accept a report cache only when metadata and actual HTML bytes agree."""

    if not paths.report_path.is_file() or not paths.report_meta_path.is_file():
        return False
    try:
        meta = read_json(paths.report_meta_path)
        if (
            not isinstance(meta, dict)
            or meta.get("cache_key") != cache_key
            or meta.get("report_version") != REPORT_VERSION
        ):
            return False
        expected_sha = meta.get("report_sha256")
        expected_size = meta.get("report_size_bytes")
        if (
            not isinstance(expected_sha, str)
            or len(expected_sha) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha)
            or not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size < 0
        ):
            return False
        actual_sha, actual_size = _report_bytes_identity(paths.report_path.read_bytes())
        return actual_sha == expected_sha and actual_size == expected_size
    except (OSError, TypeError, ValueError, ValidationError):
        return False


def _thumbnail_html(info: dict[str, Any], *, report_path: Path) -> str:
    if not info.get("thumbnail_ok"):
        return (
            '<div class="thumb placeholder" aria-label="thumbnail unavailable">No thumbnail</div>'
        )
    thumbnail = info["record"].thumbnail_path
    data = (report_path.parent.parent / thumbnail).read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    return f'<img class="thumb" alt="candidate thumbnail" src="data:image/jpeg;base64,{encoded}">'


def _match_label(candidate: Candidate, matches: dict[str, Match]) -> str:
    if candidate.match_id is None:
        return "UNASSIGNED"
    match = matches.get(candidate.match_id)
    if match is None:
        return "UNASSIGNED"
    return (
        match.label or f"Match {match.ordinal + 1 if match.ordinal is not None else match.match_id}"
    )


def _candidate_card(
    candidate: Candidate,
    rank: int,
    info: dict[str, Any] | None,
    matches: dict[str, Match],
    *,
    report_path: Path,
) -> str:
    match_label = _match_label(candidate, matches)
    event = (
        f"{format_duration(candidate.event_start_ms)} - {format_duration(candidate.event_end_ms)}"
    )
    clip = "N/A"
    clip_duration = "N/A"
    link = '<span class="invalid">Clip unavailable</span>'
    if candidate.clip_start_ms is not None and candidate.clip_end_ms is not None:
        clip = (
            f"{format_duration(candidate.clip_start_ms)} - {format_duration(candidate.clip_end_ms)}"
        )
        clip_duration = _human_duration(candidate.clip_end_ms - candidate.clip_start_ms)
    if info is not None:
        href = _relative_url(info["output"], report_path=report_path)
        link = f'<a class="clip" href="{_esc(href)}">Open Clip</a>'
    evidence = " · ".join(item.summary for item in candidate.evidence[:3]) or "No compact evidence"
    actions = (
        " · ".join(candidate.normalization_actions) if candidate.normalization_actions else "None"
    )
    lineage = ", ".join(candidate.source_window_ids) if candidate.source_window_ids else "None"
    thumb = (
        _thumbnail_html(info, report_path=report_path)
        if info is not None
        else '<div class="thumb placeholder">No thumbnail</div>'
    )
    return f"""
    <article class="card candidate-card">
      {thumb}
      <div class="card-body"><div class="badge">#{rank}</div>
      <h3>{_esc(candidate.category)} <small>{_esc(candidate.candidate_id)}</small></h3>
      <p class="meta"><b>{_esc(candidate.kind)}</b> · {_esc(match_label)} · score {_esc(f"{candidate.score:.2f}")} · confidence {_esc(f"{candidate.confidence:.2f}")}</p>
      <p><b>Event:</b> {_esc(event)} · <b>Clip:</b> {_esc(clip)} ({_esc(clip_duration)})</p>
      <p><b>Reason:</b> {_esc(candidate.reason)}</p>
      <p class="evidence"><b>Evidence:</b> {_esc(evidence)}</p>
      <p class="meta"><b>Actions:</b> {_esc(actions)} · <b>Windows:</b> {_esc(lineage)}</p>
      {link}</div>
    </article>"""


def render_report(
    paths: SessionPaths,
    source: SourceAsset,
    session_map: SessionMap,
    ranking: RankingArtifact,
    manifest: Manifest,
    config: AppConfig,
    *,
    force: bool = False,
) -> ReportResult:
    """Validate local artifacts, then atomically create/reuse ``reports/index.html``."""

    if not source.path.is_file():
        raise ValidationError(
            "Original source is missing; report generation refused.",
            hint="Run: highlight resume " + session_map.session_id,
        )
    try:
        stat = source.path.stat()
        if stat.st_size != source.size_bytes or stat.st_mtime_ns != source.mtime_ns:
            raise ValidationError("Original source identity changed; report generation refused.")
        if hash_file(source.path, source=True) != source.sha256:
            raise ValidationError("Original source hash changed; report generation refused.")
    except OSError as exc:
        raise ValidationError(
            "Original source cannot be verified; report generation refused.", hint=str(exc)
        ) from exc
    expected_ranking = rank_session_map(session_map, best_of_limit=config.report.best_of_limit)
    if (
        ranking.session_id != session_map.session_id
        or ranking.source_id != session_map.source_id
        or ranking.cache_key != expected_ranking.cache_key
        or ranking.ordered_candidate_ids != expected_ranking.ordered_candidate_ids
    ):
        raise ValidationError(
            "Ranking artifact is stale or does not match the canonical session map.",
            hint="Run: highlight resume " + session_map.session_id,
        )
    extraction, records, extraction_warnings = _validate_extraction(paths, session_map, config)
    cost = _cost_summary(config, session_map.session_id)
    cache_key = _cache_key(session_map, ranking, extraction, records, manifest, cost, config)
    if not force and _verified_report_cache_hit(paths, cache_key=cache_key):
        return ReportResult(
            paths.report_path,
            cache_hit=True,
            cache_key=cache_key,
            warnings=tuple(extraction_warnings),
        )

    matches = {match.match_id: match for match in session_map.matches}
    rank_by_id = {entry.candidate_id: entry.rank for entry in ranking.entries}
    by_match: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in sorted(
        session_map.candidates, key=lambda item: (item.event_start_ms, item.candidate_id)
    ):
        by_match[candidate.match_id or "__unassigned__"].append(candidate)
    review_ms = review_duration_ms(session_map.candidates)
    ratio = (review_ms / session_map.duration_ms * 100) if session_map.duration_ms else 0.0
    warnings = list(source.warnings) + list(session_map.warnings) + extraction_warnings
    if cost["safety_hold_active"]:
        warnings.append(f"Cost safety hold: {cost['safety_hold_reason']}")
    for stage in manifest.stages.values():
        if stage.status.value not in {"COMPLETED", "PENDING"}:
            warnings.append(f"{stage.stage}: {stage.status.value} {stage.reason or ''}".strip())
    provider = session_map.scout_metadata.get("provider", session_map.scout_backend)
    model = session_map.scout_metadata.get("model", "-")
    settled = cost["settled_micro_thb"] / 1_000_000
    exposure = (
        cost["reserved_micro_thb"] + cost["in_flight_micro_thb"] + cost["ambiguous_micro_thb"]
    ) / 1_000_000
    best = (
        [
            session_map.candidates_by_id[candidate_id]
            for candidate_id in ranking.best_of_candidate_ids
        ]
        if hasattr(session_map, "candidates_by_id")
        else [
            next(
                candidate
                for candidate in session_map.candidates
                if candidate.candidate_id == candidate_id
            )
            for candidate_id in ranking.best_of_candidate_ids
        ]
    )
    match_sections: list[str] = []
    ordered_groups = sorted(
        by_match.items(),
        key=lambda item: (0, matches[item[0]].start_ms) if item[0] in matches else (1, 0),
    )
    for key, candidates in ordered_groups:
        label = _match_label(candidates[0], matches) if key != "__unassigned__" else "UNASSIGNED"
        cards = "".join(
            _candidate_card(
                candidate,
                rank_by_id[candidate.candidate_id],
                records.get(candidate.candidate_id),
                matches,
                report_path=paths.report_path,
            )
            for candidate in candidates
        )
        match_sections.append(f'<section class="group"><h2>{_esc(label)}</h2>{cards}</section>')
    best_cards = "".join(
        _candidate_card(
            candidate,
            rank_by_id[candidate.candidate_id],
            records.get(candidate.candidate_id),
            matches,
            report_path=paths.report_path,
        )
        for candidate in best
    )
    if not best_cards:
        best_cards = '<p class="empty">No candidates found.</p>'
    warning_html = "".join(f"<li>{_esc(item)}</li>" for item in warnings) or "<li>None</li>"
    stage_rows = "".join(
        f"<tr><td>{_esc(name)}</td><td>{_esc('COMPLETED' if name == 'report' else stage.status.value)}</td></tr>"
        for name, stage in manifest.stages.items()
    )
    html_doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Game Highlight Report — {_esc(session_map.session_id)}</title>
<style>
:root {{ color-scheme: dark; font-family: system-ui,-apple-system,Segoe UI,sans-serif; background:#10131a; color:#e9edf5; }}
body {{ margin:0 auto; max-width:1300px; padding:24px; }} h1,h2,h3 {{ margin:0 0 8px; }}
.summary,.cost,.stages,.group {{ margin:18px 0; }} .stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; }}
.stat,.card {{ background:#191e28; border:1px solid #2e3747; border-radius:12px; padding:14px; }} .stat b {{ display:block; font-size:1.25rem; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(330px,1fr)); gap:12px; }} .candidate-card {{ display:flex; gap:12px; }} .card-body {{ flex:1; }}
.thumb {{ width:150px; height:84px; object-fit:cover; border-radius:8px; background:#2a3140; flex:0 0 auto; }} .placeholder {{ display:grid; place-items:center; color:#9ca8bc; font-size:.8rem; }}
.badge {{ color:#ffcf66; font-weight:700; }} small,.meta {{ color:#9ca8bc; font-size:.86rem; }} .clip {{ color:#8bc7ff; }} .invalid {{ color:#ff8989; }}
.empty {{ padding:20px; border:1px dashed #536078; border-radius:8px; }} table {{ border-collapse:collapse; width:100%; }} td {{ border-bottom:1px solid #2e3747; padding:6px; }}
details {{ background:#171c25; padding:10px; border-radius:8px; }}
</style></head><body>
<header><h1>Game Highlight Report</h1><p class="meta">Generated locally · no external assets · report {_esc(REPORT_VERSION)}</p></header>
<section class="summary"><h2>Session summary</h2><div class="stats">
<div class="stat"><span>Session</span><b>{_esc(session_map.session_id)}</b></div><div class="stat"><span>Source</span><b>{_esc(source.path.name)}</b></div>
<div class="stat"><span>Duration</span><b>{_esc(format_duration(source.duration_ms))}</b></div><div class="stat"><span>Profile</span><b>{_esc(session_map.game_profile)}</b></div>
<div class="stat"><span>Candidates</span><b>{len(session_map.candidates)}</b></div><div class="stat"><span>Matches</span><b>{len(session_map.matches)}</b></div>
<div class="stat"><span>Best Of</span><b>{len(ranking.best_of_candidate_ids)}</b></div><div class="stat"><span>Review footage</span><b>{_esc(_human_duration(review_ms))} / {_esc(_human_duration(source.duration_ms))} ({ratio:.1f}%)</b></div>
<div class="stat"><span>Scout</span><b>{_esc(provider)} / {_esc(model)}</b></div><div class="stat"><span>Warnings</span><b>{len(warnings)}</b></div>
</div></section>
<section class="cost"><h2>Session cost</h2><p>Local ledger/list-rate equivalent (not a provider credit-card charge): settled ฿{settled:.6f}; active exposure ฿{exposure:.6f}; {cost["call_count"]} calls.</p>
<p>{_esc(" · ".join(f"{key}: ฿{value / 1_000_000:.6f}" for key, value in cost["grouping"].items()) or "Fake Scout / local: ฿0.000000")}</p></section>
<section class="best"><h2>Best Of</h2><div class="cards">{best_cards}</div></section>
<section><h2>Candidate library</h2>{"".join(match_sections) or '<p class="empty">No candidates found.</p>'}</section>
<section class="stages"><h2>Stages</h2><table><tbody>{stage_rows}</tbody></table></section>
<details><summary>Warnings and diagnostics</summary><ul>{warning_html}</ul></details>
<script>document.querySelectorAll('a.clip').forEach((a) => a.setAttribute('download', ''));</script>
</body></html>"""
    report_bytes = html_doc.encode("utf-8")
    report_sha256, report_size_bytes = _report_bytes_identity(report_bytes)
    atomic_write_bytes(paths.report_path, report_bytes)
    atomic_write_json(
        paths.report_meta_path,
        {
            "cache_key": cache_key,
            "report_version": REPORT_VERSION,
            "report_sha256": report_sha256,
            "report_size_bytes": report_size_bytes,
        },
    )
    return ReportResult(
        paths.report_path, cache_hit=False, cache_key=cache_key, warnings=tuple(warnings)
    )


def load_report_inputs(
    paths: SessionPaths,
) -> tuple[SourceAsset, SessionMap, RankingArtifact, Manifest]:
    from game_highlight_finder.storage.sessions import load_manifest, source_from_artifact

    try:
        source = source_from_artifact(paths.source)
        session_map = SessionMap.model_validate(read_json(paths.session_map))
        ranking = RankingArtifact.model_validate(read_json(paths.ranking_path))
        manifest = load_manifest(paths.manifest)
    except Exception as exc:
        raise ValidationError(
            "Required local report artifacts are missing or invalid.",
            hint="Run: highlight resume " + paths.root.name,
        ) from exc
    return source, session_map, ranking, manifest


__all__ = [
    "REPORT_VERSION",
    "ReportResult",
    "load_report_inputs",
    "render_report",
    "review_duration_ms",
]
