from __future__ import annotations

import hashlib
import http.client
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from game_highlight_finder.benchmark.annotation_server import AnnotationServer
from game_highlight_finder.benchmark.models import BenchmarkAnnotations
from game_highlight_finder.cli import app
from game_highlight_finder.config import AppConfig, StorageConfig, ToolsConfig
from game_highlight_finder.errors import SourceError
from game_highlight_finder.pipeline.ingest import ingest_source
from game_highlight_finder.storage.atomic import atomic_write_json
from game_highlight_finder.storage.hashing import hash_file


@dataclass(frozen=True)
class RunningAnnotation:
    server: AnnotationServer
    annotation_path: Path
    source_path: Path
    source_sha256: str


@pytest.fixture
def running_annotation(
    tmp_path: Path,
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
) -> Iterator[RunningAnnotation]:
    config = AppConfig(
        storage=StorageConfig(data_dir=tmp_path / "library"),
        tools=ToolsConfig(ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path),
    )
    source = ingest_source(tiny_video, config).source
    annotation_path = tmp_path / "library" / "benchmarks" / "annotations" / "case.json"
    annotations = BenchmarkAnnotations(
        benchmark_id="m8-real-v1",
        case_id="synthetic-case",
        source_sha256=source.sha256,
        source_duration_ms=source.duration_ms,
        game_profile="unknown",
        source_path=source.path,
    )
    atomic_write_json(annotation_path, annotations.model_dump(mode="json"))
    server = AnnotationServer(annotation_path, config, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield RunningAnnotation(server, annotation_path, tiny_video, source.sha256)
    server.shutdown()
    thread.join(timeout=2)


def _request(
    running: RunningAnnotation,
    method: str,
    path: str,
    payload: object | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection(
        running.server.host,
        running.server.port,
        timeout=10,
    )
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = {
        "Host": f"{running.server.host}:{running.server.port}",
        "Content-Type": "application/json",
    }
    if headers:
        request_headers.update(headers)
    connection.request(method, path, body=body, headers=request_headers)
    response = connection.getresponse()
    result = (response.status, dict(response.getheaders()), response.read())
    connection.close()
    return result


def _annotation_payload(running: RunningAnnotation) -> dict[str, Any]:
    status, _headers, body = _request(running, "GET", "/api/annotation")
    assert status == 200
    return cast(dict[str, Any], json.loads(body)["annotation"])


def _highlight_payload(running: RunningAnnotation, *, notes: str = "") -> dict[str, Any]:
    payload = _annotation_payload(running)
    payload["highlights"] = [
        {
            "annotation_id": "hl-0001",
            "match_annotation_id": None,
            "event_start_ms": 200,
            "event_end_ms": 900,
            "setup_start_ms": 100,
            "payoff_end_ms": 1_000,
            "category": "CLUTCH",
            "importance": "MUST_CATCH",
            "modality": "VISUAL_AND_AUDIO",
            "notes": notes or None,
        }
    ]
    return payload


def test_annotate_cli_help_is_available() -> None:
    result = CliRunner().invoke(app, ["benchmark", "annotate", "--help"])
    assert result.exit_code == 0, result.output
    assert "--no-open" in result.output
    assert "--port" in result.output


def test_loopback_html_load_and_no_arbitrary_file_endpoint(
    running_annotation: RunningAnnotation,
) -> None:
    status, headers, body = _request(running_annotation, "GET", "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"/api/video" in body
    assert b"https://" not in body
    assert b"Suggest Highlights" not in body

    status, _headers, body = _request(running_annotation, "GET", "/not-a-file")
    assert status == 404
    assert b"Not found" in body


def test_annotation_load_and_source_identity_verification(
    running_annotation: RunningAnnotation,
) -> None:
    status, _headers, body = _request(running_annotation, "GET", "/api/annotation")
    assert status == 200
    loaded = json.loads(body)
    assert loaded["summary"]["source_identity"] == "PASS"
    assert loaded["annotation"]["case_id"] == "synthetic-case"
    assert loaded["review_state"]["reviewed"] is False


def test_byte_range_streaming_and_source_immutability(
    running_annotation: RunningAnnotation,
) -> None:
    before = hash_file(running_annotation.source_path, source=True)
    status, headers, body = _request(
        running_annotation,
        "GET",
        "/api/video",
        headers={"Range": "bytes=0-15"},
    )
    assert status == 206
    assert headers["Accept-Ranges"] == "bytes"
    assert headers["Content-Range"].startswith("bytes 0-15/")
    assert len(body) == 16
    assert hash_file(running_annotation.source_path, source=True) == before

    status, headers, body = _request(
        running_annotation,
        "GET",
        "/api/video",
        headers={"Range": "bytes=999999999-1000000000"},
    )
    assert status == 416
    assert headers["Content-Range"].startswith("bytes */")
    assert body == b""


def test_valid_save_boring_match_and_server_side_summary(
    running_annotation: RunningAnnotation,
) -> None:
    payload = _highlight_payload(running_annotation)
    payload["boring_intervals"] = [
        {"annotation_id": "boring-0001", "start_ms": 1_100, "end_ms": 1_500, "notes": "quiet"}
    ]
    payload["matches"] = [
        {
            "annotation_id": "match-0001",
            "ordinal": 1,
            "start_ms": 0,
            "end_ms": 1_800,
            "label": "round",
            "confidence": None,
            "notes": None,
        }
    ]
    payload["highlights"][0]["match_annotation_id"] = "match-0001"
    status, _headers, body = _request(running_annotation, "POST", "/api/validate", payload)
    assert status == 200
    assert json.loads(body)["status"] == "VALID_JSON"

    status, _headers, body = _request(running_annotation, "POST", "/api/save", payload)
    assert status == 200
    saved = json.loads(body)
    assert saved["status"] == "SAVED"
    assert saved["summary"]["MUST_CATCH"] == 1
    persisted = json.loads(running_annotation.annotation_path.read_text(encoding="utf-8"))
    assert persisted["highlights"][0]["annotation_id"] == "hl-0001"
    assert persisted["boring_intervals"][0]["annotation_id"] == "boring-0001"
    assert persisted["matches"][0]["annotation_id"] == "match-0001"


def test_invalid_intervals_and_identity_changes_are_rejected(
    running_annotation: RunningAnnotation,
) -> None:
    invalid = _highlight_payload(running_annotation)
    invalid["highlights"][0]["event_end_ms"] = invalid["highlights"][0]["event_start_ms"]
    status, _headers, _body = _request(running_annotation, "POST", "/api/validate", invalid)
    assert status == 400

    changed = _highlight_payload(running_annotation)
    changed["source_sha256"] = "0" * 64
    status, _headers, body = _request(running_annotation, "POST", "/api/save", changed)
    assert status == 400
    assert b"identity" in body.lower() or b"source" in body.lower()


def test_importance_modality_and_origin_validation(
    running_annotation: RunningAnnotation,
) -> None:
    invalid = _highlight_payload(running_annotation)
    invalid["highlights"][0]["importance"] = "AI_SUGGESTED"
    status, _headers, _body = _request(running_annotation, "POST", "/api/save", invalid)
    assert status == 400

    invalid = _highlight_payload(running_annotation)
    invalid["highlights"][0]["modality"] = "MODEL_CONFIDENCE"
    status, _headers, _body = _request(running_annotation, "POST", "/api/save", invalid)
    assert status == 400

    valid = _highlight_payload(running_annotation)
    status, _headers, _body = _request(
        running_annotation,
        "POST",
        "/api/validate",
        valid,
        headers={"Origin": "http://evil.invalid"},
    )
    assert status == 400


def test_stable_edit_delete_and_backup_behavior(
    running_annotation: RunningAnnotation,
) -> None:
    first = _highlight_payload(running_annotation, notes="first")
    status, _headers, body = _request(running_annotation, "POST", "/api/save", first)
    assert status == 200
    assert json.loads(body)["backup"] is None
    assert not list(running_annotation.annotation_path.parent.glob("*.backup-*.json"))

    edited = _highlight_payload(running_annotation, notes="edited")
    status, _headers, body = _request(running_annotation, "POST", "/api/save", edited)
    assert status == 200
    backup = json.loads(body)["backup"]
    assert backup is not None and Path(backup).is_file()
    assert json.loads(Path(backup).read_text(encoding="utf-8"))["highlights"][0]["notes"] == "first"
    persisted = json.loads(running_annotation.annotation_path.read_text(encoding="utf-8"))
    assert persisted["highlights"][0]["annotation_id"] == "hl-0001"

    backup_count = len(list(running_annotation.annotation_path.parent.glob("*.backup-*.json")))
    status, _headers, body = _request(running_annotation, "POST", "/api/save", edited)
    assert status == 200
    assert json.loads(body)["status"] == "UNCHANGED"
    assert len(list(running_annotation.annotation_path.parent.glob("*.backup-*.json"))) == (
        backup_count
    )

    deleted = _annotation_payload(running_annotation)
    deleted["highlights"] = []
    status, _headers, _body = _request(running_annotation, "POST", "/api/save", deleted)
    assert status == 200
    persisted = json.loads(running_annotation.annotation_path.read_text(encoding="utf-8"))
    assert persisted["highlights"] == []


def test_review_state_sidecar_and_stale_review_reset(
    running_annotation: RunningAnnotation,
) -> None:
    status, _headers, body = _request(
        running_annotation,
        "POST",
        "/api/review-state",
        {"reviewed": True},
    )
    assert status == 200
    assert json.loads(body)["reviewed"] is True
    assert running_annotation.server.review_state_path.is_file()

    payload = _highlight_payload(running_annotation)
    status, _headers, _body = _request(running_annotation, "POST", "/api/save", payload)
    assert status == 200
    assert json.loads(running_annotation.server.review_state_path.read_text())["reviewed"] is False


def test_constructor_rejects_changed_source(
    tmp_path: Path,
    tiny_video: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
) -> None:
    config = AppConfig(
        storage=StorageConfig(data_dir=tmp_path / "library"),
        tools=ToolsConfig(ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path),
    )
    source = ingest_source(tiny_video, config).source
    annotation_path = tmp_path / "case.json"
    annotations = BenchmarkAnnotations(
        benchmark_id="m8-real-v1",
        case_id="changed-source",
        source_sha256=source.sha256,
        source_duration_ms=source.duration_ms,
        source_path=source.path,
    )
    atomic_write_json(annotation_path, annotations.model_dump(mode="json"))
    tiny_video.write_bytes(tiny_video.read_bytes() + b"changed")
    with pytest.raises(SourceError):
        AnnotationServer(annotation_path, config, port=0)


def test_model_hash_is_deterministic(running_annotation: RunningAnnotation) -> None:
    payload = _annotation_payload(running_annotation)
    expected = hashlib.sha256(
        (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    ).hexdigest()
    status, _headers, body = _request(running_annotation, "POST", "/api/validate", payload)
    assert status == 200
    assert json.loads(body)["summary"]["annotation_sha256"] == expected
