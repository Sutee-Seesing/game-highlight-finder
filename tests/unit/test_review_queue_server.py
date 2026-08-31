from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from game_highlight_finder.benchmark.review_queue_server import ReviewQueueServer
from game_highlight_finder.cli import app
from game_highlight_finder.errors import ValidationError


@dataclass(frozen=True)
class RunningReviewQueue:
    server: ReviewQueueServer
    queue_path: Path


@pytest.fixture
def running_review_queue(tmp_path: Path) -> Iterator[RunningReviewQueue]:
    clips = tmp_path / "review_clips"
    clips.mkdir()
    (clips / "xonotic-01.mp4").write_bytes(b"0123456789abcdef")
    (clips / "freedoom-01.mp4").write_bytes(b"abcdefghijklmnop")
    queue_path = tmp_path / "review_queue.json"
    queue_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "set_id": "external-fps-dev-v1",
                "purpose": "development_review_queue_only",
                "not_ground_truth": True,
                "excluded_from_m8_acceptance": True,
                "provider_calls": 0,
                "cases": [
                    {
                        "case": "xonotic",
                        "session_id": "session-x",
                        "source": "xonotic.webm",
                        "duration_ms": 20_000,
                        "intervals": [
                            {
                                "review_id": "xonotic-review-01",
                                "start_ms": 4_500,
                                "end_ms": 8_000,
                                "peak_db": -28.4,
                                "review_clip": "review_clips/xonotic-01.mp4",
                            }
                        ],
                    },
                    {
                        "case": "freedoom",
                        "session_id": "session-f",
                        "source": "freedoom.webm",
                        "duration_ms": 20_000,
                        "intervals": [
                            {
                                "review_id": "freedoom-review-01",
                                "start_ms": 5_000,
                                "end_ms": 9_000,
                                "peak_db": -16.4,
                                "review_clip": "review_clips/freedoom-01.mp4",
                            }
                        ],
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    server = ReviewQueueServer(queue_path, cases=("xonotic",), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield RunningReviewQueue(server, queue_path)
    server.shutdown()
    thread.join(timeout=2)


def _request(
    running: RunningReviewQueue,
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


def test_review_queue_cli_help_is_available() -> None:
    result = CliRunner().invoke(app, ["benchmark", "review-queue", "--help"])
    assert result.exit_code == 0, result.output
    assert "--case" in result.output
    assert "--no-open" in result.output
    assert "--output" in result.output


def test_queue_payload_is_selected_case_only_and_provider_free(
    running_review_queue: RunningReviewQueue,
) -> None:
    status, headers, body = _request(running_review_queue, "GET", "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"PROVIDER CALLS ZERO" in body

    status, _headers, body = _request(running_review_queue, "GET", "/api/queue")
    assert status == 200
    payload = cast(dict[str, Any], json.loads(body))
    assert payload["selected_cases"] == ["xonotic"]
    assert payload["interval_count"] == 1
    assert payload["reviewed_count"] == 0
    assert payload["reviewer_kind"] == "HUMAN"
    assert payload["provider_calls"] == 0
    assert payload["cases"][0]["intervals"][0]["review_id"] == "xonotic-review-01"


def test_clip_byte_range_is_bounded_to_declared_review_clip(
    running_review_queue: RunningReviewQueue,
) -> None:
    status, headers, body = _request(
        running_review_queue,
        "GET",
        "/api/clip?id=xonotic-review-01",
        headers={"Range": "bytes=2-5"},
    )
    assert status == 206
    assert headers["Content-Range"] == "bytes 2-5/16"
    assert body == b"2345"

    status, _headers, body = _request(
        running_review_queue,
        "GET",
        "/api/clip?id=freedoom-review-01",
    )
    assert status == 400
    assert b"Unknown review clip" in body


def test_save_persists_explicit_decision_and_reloads_it(
    running_review_queue: RunningReviewQueue,
) -> None:
    decision = {
        "decisions": [
            {
                "review_id": "xonotic-review-01",
                "decision": "BORING",
                "notes": "visual traversal only",
            }
        ]
    }
    status, _headers, body = _request(running_review_queue, "POST", "/api/save", decision)
    assert status == 200
    saved = json.loads(body)
    assert saved["complete"] is True
    assert saved["provider_calls"] == 0
    sidecar = Path(saved["output_path"])
    assert sidecar.is_file()
    persisted = json.loads(sidecar.read_text(encoding="utf-8"))
    assert persisted["decisions"][0]["decision"] == "BORING"
    assert persisted["reviewer_kind"] == "HUMAN"
    assert persisted["provider_calls"] == 0

    status, _headers, body = _request(running_review_queue, "GET", "/api/queue")
    assert status == 200
    payload = json.loads(body)
    assert payload["reviewed_count"] == 1
    assert payload["cases"][0]["intervals"][0]["decision"] == "BORING"


def test_unknown_duplicate_and_invalid_decisions_are_rejected(
    running_review_queue: RunningReviewQueue,
) -> None:
    invalid_payloads = [
        {"decisions": [{"review_id": "missing", "decision": "BORING"}]},
        {
            "decisions": [
                {"review_id": "xonotic-review-01", "decision": "BORING"},
                {"review_id": "xonotic-review-01", "decision": "POSITIVE"},
            ]
        },
        {"decisions": [{"review_id": "xonotic-review-01", "decision": "MODEL_GUESS"}]},
    ]
    for payload in invalid_payloads:
        status, _headers, _body = _request(running_review_queue, "POST", "/api/save", payload)
        assert status == 400


def test_queue_requires_non_ground_truth_safety_flags(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"1234")
    queue = {
        "schema_version": 1,
        "set_id": "unsafe",
        "not_ground_truth": False,
        "excluded_from_m8_acceptance": True,
        "provider_calls": 0,
        "cases": [
            {
                "case": "x",
                "session_id": "s",
                "source": "x.webm",
                "duration_ms": 1000,
                "intervals": [
                    {
                        "review_id": "r1",
                        "start_ms": 0,
                        "end_ms": 500,
                        "review_clip": "clip.mp4",
                    }
                ],
            }
        ],
    }
    path = tmp_path / "queue.json"
    path.write_text(json.dumps(queue), encoding="utf-8")
    with pytest.raises(ValidationError):
        ReviewQueueServer(path)
