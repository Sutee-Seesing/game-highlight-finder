from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from game_highlight_finder import __version__
from game_highlight_finder.config import AppConfig
from game_highlight_finder.domain.models import (
    Candidate,
    Evidence,
    Match,
    Rational,
    SessionMap,
    SourceAsset,
    VideoStream,
)
from game_highlight_finder.pipeline.extraction import ExtractionManifest, ExtractionRecord
from game_highlight_finder.pipeline.manifest import new_manifest
from game_highlight_finder.pipeline.ranking import rank_session_map
from game_highlight_finder.pipeline.report import render_report
from game_highlight_finder.storage.atomic import atomic_write_json, read_json
from game_highlight_finder.storage.hashing import hash_file
from game_highlight_finder.storage.sessions import session_paths


def test_multi_candidate_report_is_offline_escaped_and_cached(tmp_path: Path) -> None:
    source_path = tmp_path / "Thai gameplay recording.mp4"
    source_path.write_bytes(b"source")
    source_stat = source_path.stat()
    source = SourceAsset(
        created_at=datetime(2026, 8, 13, tzinfo=UTC),
        producer_version=__version__,
        source_id="src_aaaaaaaaaaaaaaaa",
        path=source_path.resolve(),
        sha256=hash_file(source_path),
        size_bytes=source_stat.st_size,
        mtime_ns=source_stat.st_mtime_ns,
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
    first = Candidate(
        candidate_id="cand_0000000000000001",
        match_id="match_0000000000000001",
        category="FUNNY",
        event_start_ms=2_000,
        event_end_ms=4_000,
        score=9,
        confidence=0.9,
        reason="</script><script>alert(1)</script> เหตุการณ์",
        evidence=[Evidence(type="fixture", summary='quote "safe"')],
        clip_start_ms=0,
        clip_end_ms=6_000,
    )
    second = Candidate(
        candidate_id="cand_0000000000000002",
        category="REACTION",
        event_start_ms=12_000,
        event_end_ms=14_000,
        score=8,
        confidence=0.8,
        reason="Unicode reaction",
        clip_start_ms=10_000,
        clip_end_ms=16_000,
    )
    session_map = SessionMap(
        created_at=source.created_at,
        producer_version=__version__,
        canonicalization_version="test-v1",
        session_id="2026-08-13_unknown_aaaaaaaaaaaa",
        source_id=source.source_id,
        duration_ms=source.duration_ms,
        matches=[
            Match(
                match_id="match_0000000000000001",
                ordinal=0,
                start_ms=0,
                end_ms=8_000,
                confidence=0.8,
                label='Round "one"',
                candidate_ids=[first.candidate_id],
            )
        ],
        candidates=[first, second],
        scout_metadata={"provider": "fake", "model": "offline"},
    )
    config = AppConfig.model_validate({"storage": {"data_dir": str(tmp_path / "data")}})
    paths = session_paths(config.storage.data_dir, session_map.session_id)
    paths.root.mkdir(parents=True)
    paths.source.write_text(source.model_dump_json(), encoding="utf-8")
    paths.session_map.write_text(session_map.model_dump_json(), encoding="utf-8")
    output_records: list[ExtractionRecord] = []
    for candidate in session_map.candidates:
        output = paths.root / f"candidates/{candidate.candidate_id}.mp4"
        thumbnail = paths.root / f"thumbnails/{candidate.candidate_id}.jpg"
        output.parent.mkdir(parents=True, exist_ok=True)
        thumbnail.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"clip-" + candidate.candidate_id.encode())
        thumbnail.write_bytes(b"fake-jpeg")
        output_records.append(
            ExtractionRecord(
                candidate_id=candidate.candidate_id,
                source_id=source.source_id,
                source_sha256=source.sha256,
                requested_start_ms=candidate.clip_start_ms or 0,
                requested_end_ms=candidate.clip_end_ms or 1,
                mode="accurate",
                accuracy_class="frame-accurate",
                output_path=str(output.relative_to(paths.root)).replace("\\", "/"),
                output_sha256=hash_file(output),
                output_size_bytes=output.stat().st_size,
                thumbnail_path=str(thumbnail.relative_to(paths.root)).replace("\\", "/"),
                thumbnail_sha256=hash_file(thumbnail),
                ffmpeg_identity="test",
                config_fingerprint="a" * 64,
                status="COMPLETED",
            )
        )
    extraction = ExtractionManifest(
        created_at=source.created_at,
        updated_at=source.created_at,
        producer_version=__version__,
        session_id=session_map.session_id,
        source_id=source.source_id,
        source_sha256=source.sha256,
        records=tuple(output_records),
        status="COMPLETED",
    )
    paths.extraction_manifest.write_text(extraction.model_dump_json(), encoding="utf-8")
    paths.manifest.write_text(
        new_manifest(session_map.session_id, now=source.created_at).model_dump_json(),
        encoding="utf-8",
    )
    from game_highlight_finder.storage.sessions import load_manifest

    manifest = load_manifest(paths.manifest)
    ranking = rank_session_map(session_map)
    first_result = render_report(paths, source, session_map, ranking, manifest, config)
    html = paths.report_path.read_text(encoding="utf-8")
    first_hash = hash_file(paths.report_path)
    second_result = render_report(paths, source, session_map, ranking, manifest, config)

    assert first_result.cache_hit is False
    assert second_result.cache_hit is True
    metadata = read_json(paths.report_meta_path)
    assert metadata["cache_key"] == first_result.cache_key
    assert metadata["report_version"] == "m7-report-v1"
    assert metadata["report_sha256"] == first_hash
    assert metadata["report_size_bytes"] == paths.report_path.stat().st_size
    assert hash_file(paths.report_path) == first_hash
    assert "No external" not in html
    assert "http://" not in html and "https://" not in html and "cdn" not in html.lower()
    assert "&lt;/script&gt;" in html
    assert "No candidates found" not in html
    assert "UNASSIGNED" in html
    assert "Open Clip" in html

    # Every integrity failure is a stale cache, not a cache hit, and is repaired.
    paths.report_path.write_text("truncated", encoding="utf-8")
    assert render_report(paths, source, session_map, ranking, manifest, config).cache_hit is False
    assert hash_file(paths.report_path) == first_hash

    paths.report_path.write_text("manual alteration", encoding="utf-8")
    atomic_write_json(paths.report_meta_path, metadata)
    assert render_report(paths, source, session_map, ranking, manifest, config).cache_hit is False
    assert hash_file(paths.report_path) == first_hash

    paths.report_meta_path.write_text("{not-json", encoding="utf-8")
    assert render_report(paths, source, session_map, ranking, manifest, config).cache_hit is False
    assert hash_file(paths.report_path) == first_hash

    metadata = read_json(paths.report_meta_path)
    metadata["report_sha256"] = "0" * 64
    atomic_write_json(paths.report_meta_path, metadata)
    assert render_report(paths, source, session_map, ranking, manifest, config).cache_hit is False
    assert hash_file(paths.report_path) == first_hash

    metadata = read_json(paths.report_meta_path)
    metadata["report_size_bytes"] = 1
    atomic_write_json(paths.report_meta_path, metadata)
    assert render_report(paths, source, session_map, ranking, manifest, config).cache_hit is False
    assert hash_file(paths.report_path) == first_hash

    # A matching semantic cache key is still invalid when the HTML bytes differ.
    metadata = read_json(paths.report_meta_path)
    paths.report_path.write_bytes(b"different bytes")
    atomic_write_json(paths.report_meta_path, metadata)
    assert render_report(paths, source, session_map, ranking, manifest, config).cache_hit is False
    assert hash_file(paths.report_path) == first_hash
