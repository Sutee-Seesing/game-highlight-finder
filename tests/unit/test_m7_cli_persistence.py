from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import yaml
from typer.testing import CliRunner

from game_highlight_finder import __version__
from game_highlight_finder.cli import app
from game_highlight_finder.config import AppConfig, config_payload
from game_highlight_finder.cost.calculator import budget_period_for
from game_highlight_finder.cost.ledger import CostLedger
from game_highlight_finder.cost.models import CostQuote, PricingEntry
from game_highlight_finder.domain.models import (
    Candidate,
    Rational,
    SessionMap,
    SourceAsset,
    VideoStream,
)
from game_highlight_finder.pipeline.extraction import ExtractionManifest, ExtractionRecord
from game_highlight_finder.pipeline.manifest import new_manifest
from game_highlight_finder.pipeline.ranking import rank_session_map
from game_highlight_finder.providers.base import ProviderUsageEstimate
from game_highlight_finder.storage.atomic import atomic_write_json
from game_highlight_finder.storage.hashing import hash_file
from game_highlight_finder.storage.sessions import session_paths

SESSION_ID = "2026-08-14_unknown_aaaaaaaaaaaa"


def _quote(*, call_id: str, amount: int, now: datetime) -> CostQuote:
    pricing = PricingEntry(
        provider="fake",
        model="fake-model",
        billing_mode="standard",
        input_rates_by_modality={"text": Decimal("1")},
        output_rate=Decimal("1"),
        effective_from=now - timedelta(minutes=1),
        verified_at=now,
        source="offline-test",
        catalog_version="test-v1",
    )
    usage = ProviderUsageEstimate(input_text_tokens=1)
    return CostQuote(
        provider="fake",
        model="fake-model",
        billing_mode="standard",
        budget_period=budget_period_for(now, "Asia/Bangkok"),
        usage_estimate=usage,
        pricing_snapshot=pricing,
        fx_snapshot={
            "base_currency": "USD",
            "quote_currency": "THB",
            "rate": "36",
            "captured_at": now.isoformat(),
            "source": "offline-test",
        },
        base_cost_micro_thb=amount,
        reserved_cost_micro_thb=amount,
    )


def _write_session(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    data_dir = tmp_path / "session-data"
    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"source-artifact")
    stat = source_path.stat()
    created_at = datetime(2026, 8, 14, tzinfo=UTC)
    source = SourceAsset(
        created_at=created_at,
        producer_version=__version__,
        source_id="src_aaaaaaaaaaaaaaaa",
        path=source_path.resolve(),
        sha256=hash_file(source_path),
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        duration_ms=30_000,
        container="mp4",
        video_stream=VideoStream(
            index=0,
            codec_name="h264",
            width=320,
            height=240,
            average_frame_rate=Rational(numerator=30, denominator=1),
        ),
        selected_video_stream=0,
        probe_version="test",
    )
    candidates = [
        Candidate(
            candidate_id=f"cand_{index:016x}",
            category="CLUTCH",
            event_start_ms=index * 5_000,
            event_end_ms=index * 5_000 + 1_000,
            score=9 - index,
            confidence=0.9,
            reason=f"candidate {index}",
            clip_start_ms=index * 5_000,
            clip_end_ms=index * 5_000 + 2_000,
        )
        for index in range(1, 4)
    ]
    session_map = SessionMap(
        created_at=created_at,
        producer_version=__version__,
        canonicalization_version="test-v1",
        session_id=SESSION_ID,
        source_id=source.source_id,
        duration_ms=source.duration_ms,
        candidates=candidates,
        scout_backend="gemini",
        scout_metadata={"provider": "gemini", "model": "fake-model"},
    )
    config = AppConfig.model_validate(
        {
            "storage": {"data_dir": str(data_dir)},
            "report": {"best_of_limit": 2},
            "scout": {"backend": "gemini", "allow_remote_upload": True},
            "cost": {"ledger_path": str(tmp_path / "custom" / "session-ledger.sqlite3")},
        }
    )
    paths = session_paths(data_dir, SESSION_ID)
    paths.root.mkdir(parents=True)
    atomic_write_json(paths.source, source.model_dump(mode="json"))
    atomic_write_json(paths.session_map, session_map.model_dump(mode="json"))
    atomic_write_json(
        paths.manifest,
        new_manifest(SESSION_ID, now=created_at).model_dump(mode="json"),
    )
    records: list[ExtractionRecord] = []
    for candidate in candidates:
        output = paths.candidates_dir / f"{candidate.candidate_id}.mp4"
        thumbnail = paths.thumbnails_dir / f"{candidate.candidate_id}.jpg"
        output.parent.mkdir(parents=True, exist_ok=True)
        thumbnail.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(candidate.candidate_id.encode())
        thumbnail.write_bytes(b"thumbnail")
        records.append(
            ExtractionRecord(
                candidate_id=candidate.candidate_id,
                source_id=source.source_id,
                source_sha256=source.sha256,
                requested_start_ms=candidate.clip_start_ms or 0,
                requested_end_ms=candidate.clip_end_ms or 1,
                mode="accurate",
                accuracy_class="frame-accurate",
                output_path=output.relative_to(paths.root).as_posix(),
                output_sha256=hash_file(output),
                output_size_bytes=output.stat().st_size,
                thumbnail_path=thumbnail.relative_to(paths.root).as_posix(),
                thumbnail_sha256=hash_file(thumbnail),
                ffmpeg_identity="test",
                config_fingerprint="a" * 64,
                status="COMPLETED",
            )
        )
    atomic_write_json(
        paths.extraction_manifest,
        ExtractionManifest(
            created_at=created_at,
            updated_at=created_at,
            producer_version=__version__,
            session_id=SESSION_ID,
            source_id=source.source_id,
            source_sha256=source.sha256,
            records=tuple(records),
            status="COMPLETED",
        ).model_dump(mode="json"),
    )
    atomic_write_json(
        paths.config,
        {
            "schema_version": 1,
            "config": config_payload(config, redacted=True),
        },
    )
    atomic_write_json(
        paths.ranking_path,
        rank_session_map(session_map, best_of_limit=3).model_dump(mode="json"),
    )

    now = datetime.now(UTC)
    custom_ledger = CostLedger(
        tmp_path / "custom" / "session-ledger.sqlite3", budget_micro_thb=10_000_000
    )
    custom_ledger.reserve(
        call_id="custom-call",
        request_fingerprint="c" * 64,
        quote=_quote(call_id="custom-call", amount=123_456, now=now),
        stage="scout",
        session_id=SESSION_ID,
        now=now,
    )
    global_ledger = CostLedger(data_dir / "cost" / "ledger.sqlite3", budget_micro_thb=10_000_000)
    global_ledger.reserve(
        call_id="misleading-global-call",
        request_fingerprint="g" * 64,
        quote=_quote(call_id="misleading-global-call", amount=999_999, now=now),
        stage="scout",
        session_id=SESSION_ID,
        now=now,
    )

    current_config = tmp_path / "current.yaml"
    current_config.write_text(
        yaml.safe_dump(
            {
                "storage": {"data_dir": str(data_dir)},
                "report": {"best_of_limit": 3},
                "cost": {"ledger_path": str(data_dir / "cost" / "ledger.sqlite3")},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return current_config, data_dir, paths.report_path, paths.ranking_path


def test_report_and_cost_session_use_persisted_session_config(tmp_path: Path) -> None:
    current_config, data_dir, report_path, ranking_path = _write_session(tmp_path)
    runner = CliRunner()

    cost_result = runner.invoke(
        app,
        ["--config", str(current_config), "cost", "session", SESSION_ID],
    )
    assert cost_result.exit_code == 0, cost_result.output
    assert "฿0.12" in cost_result.output
    assert "฿1.00" not in cost_result.output

    report_result = runner.invoke(
        app,
        ["--config", str(current_config), "report", SESSION_ID],
    )
    assert report_result.exit_code == 0, report_result.output
    html = report_path.read_text(encoding="utf-8")
    assert "Best Of</span><b>2</b>" in html
    assert "0.123456" in html
    assert "0.999999" not in html

    session_map = SessionMap.model_validate(
        json.loads(
            (data_dir / "sessions" / SESSION_ID / "session_map.json").read_text(encoding="utf-8")
        )
    )
    ranking = rank_session_map(session_map, best_of_limit=2)
    assert len(ranking.best_of_candidate_ids) == 2
    assert ranking_path.is_file()

    candidates_result = runner.invoke(
        app,
        ["--config", str(current_config), "candidates", SESSION_ID],
    )
    assert candidates_result.exit_code == 0, candidates_result.output
    json_result = runner.invoke(
        app,
        ["--config", str(current_config), "candidates", SESSION_ID, "--json"],
    )
    assert json_result.exit_code == 0, json_result.output
    assert '"candidate_id"' in json_result.output
