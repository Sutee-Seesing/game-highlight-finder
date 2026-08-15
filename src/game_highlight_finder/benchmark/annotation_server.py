"""Tiny local-only human annotation server for the private M8 benchmark.

The server deliberately has no provider, network client, or model dependency.  It
opens exactly one validated annotation document and its immutable source video.  All
annotation edits stay in browser memory until the owner presses Save; every persisted
payload is validated again with the strict M8A models before an atomic write.
"""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import mimetypes
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from pydantic import ValidationError as PydanticValidationError

from game_highlight_finder.benchmark.evaluator import annotation_sha256
from game_highlight_finder.benchmark.models import (
    BenchmarkAnnotations,
    Importance,
    Modality,
)
from game_highlight_finder.config import AppConfig
from game_highlight_finder.errors import (
    AppError,
    SourceError,
    StorageError,
    ValidationError,
)
from game_highlight_finder.media.ffprobe import parse_source_asset, run_ffprobe
from game_highlight_finder.media.tools import executable_version, require_executable
from game_highlight_finder.storage.atomic import (
    atomic_write_bytes,
    atomic_write_json,
    read_json,
)
from game_highlight_finder.storage.hashing import hash_file

MAX_REQUEST_BYTES = 2 * 1024 * 1024
SOURCE_DURATION_TOLERANCE_MS = 1_000
VIDEO_CHUNK_BYTES = 64 * 1024
REVIEW_STATE_VERSION = 1


@dataclass(frozen=True)
class SourceIdentity:
    """The source identity checked before serving or persisting annotations."""

    path: Path
    sha256: str
    duration_ms: int
    size_bytes: int
    mtime_ns: int


def _json_bytes(value: object) -> bytes:
    """Serialize exactly as ``atomic_write_json`` does, for preview hashes."""

    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _load_annotation(path: Path) -> BenchmarkAnnotations:
    try:
        value = read_json(path)
        return BenchmarkAnnotations.model_validate(value)
    except (PydanticValidationError, OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValidationError(
            "Annotation JSON is invalid or unreadable.",
            hint=str(path),
        ) from exc


def _verify_source(annotation: BenchmarkAnnotations, config: AppConfig) -> SourceIdentity:
    """Hash and probe the exact source named by an annotation document."""

    if annotation.source_path is None:
        raise SourceError(
            "Annotation source_path is required for local annotation.",
            hint="Use a private template created by `benchmark template`.",
        )
    source = annotation.source_path.expanduser().resolve()
    try:
        stat = source.stat()
    except OSError as exc:
        raise SourceError(
            "Annotated source video is missing or unreadable.", hint=str(source)
        ) from exc
    if not source.is_file():
        raise SourceError("Annotated source path is not a file.", hint=str(source))
    observed_sha = hash_file(source, source=True)
    if observed_sha != annotation.source_sha256:
        raise SourceError(
            "Source identity FAIL: annotation SHA-256 does not match the local source.",
            hint=f"Expected {annotation.source_sha256}; observed {observed_sha}.",
        )

    ffprobe = require_executable("ffprobe", config.tools.ffprobe_path)
    raw_probe = run_ffprobe(
        ffprobe,
        source,
        timeout_seconds=config.tools.probe_timeout_seconds,
    )
    asset = parse_source_asset(
        raw_probe,
        source_path=source,
        source_sha256=observed_sha,
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        probe_version=executable_version(ffprobe),
    )
    if abs(asset.duration_ms - annotation.source_duration_ms) > SOURCE_DURATION_TOLERANCE_MS:
        raise SourceError(
            "Source duration FAIL: annotation duration is incompatible with the local source.",
            hint=(f"Expected {annotation.source_duration_ms} ms; observed {asset.duration_ms} ms."),
        )
    return SourceIdentity(
        path=source,
        sha256=observed_sha,
        duration_ms=asset.duration_ms,
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )


def _source_signature(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise SourceError("Annotated source video disappeared.", hint=str(path)) from exc
    if not path.is_file():
        raise SourceError("Annotated source path is no longer a file.", hint=str(path))
    return stat.st_size, stat.st_mtime_ns


def _summary(annotation: BenchmarkAnnotations, annotation_hash: str) -> dict[str, object]:
    importance_counts = {item.value: 0 for item in Importance}
    modality_counts = {item.value: 0 for item in Modality}
    for highlight in annotation.highlights:
        importance_counts[highlight.importance.value] += 1
        modality_counts[highlight.modality.value] += 1
    return {
        "source_identity": "PASS",
        "source_duration_ms": annotation.source_duration_ms,
        "highlights_count": len(annotation.highlights),
        "matches_count": len(annotation.matches),
        "boring_interval_count": len(annotation.boring_intervals),
        "MUST_CATCH": importance_counts[Importance.MUST_CATCH.value],
        "WORTH_REVIEW": importance_counts[Importance.WORTH_REVIEW.value],
        "OPTIONAL": importance_counts[Importance.OPTIONAL.value],
        "modality": modality_counts,
        "annotation_sha256": annotation_hash,
    }


def _review_state_path(annotation_path: Path) -> Path:
    return annotation_path.with_name(f"{annotation_path.stem}.review-state.json")


def _default_review_state(
    annotation: BenchmarkAnnotations, annotation_hash: str | None = None
) -> dict[str, object]:
    return {
        "schema_version": REVIEW_STATE_VERSION,
        "case_id": annotation.case_id,
        "reviewed": False,
        "updated_at": None,
        "annotation_sha256": annotation_hash or annotation_sha256_from_model(annotation),
    }


def _load_review_state(
    annotation_path: Path, annotation: BenchmarkAnnotations
) -> dict[str, object]:
    path = _review_state_path(annotation_path)
    if not path.is_file():
        return _default_review_state(annotation, annotation_sha256(annotation_path))
    try:
        value = read_json(path)
    except Exception as exc:
        raise ValidationError("Review-state sidecar is unreadable.", hint=str(path)) from exc
    if not isinstance(value, dict):
        raise ValidationError("Review-state sidecar must be a JSON object.", hint=str(path))
    if value.get("schema_version") != REVIEW_STATE_VERSION:
        raise ValidationError("Unsupported review-state sidecar version.", hint=str(path))
    if value.get("case_id") != annotation.case_id or not isinstance(value.get("reviewed"), bool):
        raise ValidationError(
            "Review-state sidecar does not match the annotation case.", hint=str(path)
        )
    current_hash = annotation_sha256(annotation_path)
    if value.get("annotation_sha256") != current_hash:
        return _default_review_state(annotation, current_hash)
    return value


def annotation_sha256_from_model(annotation: BenchmarkAnnotations) -> str:
    """Hash a model using the exact bytes that an atomic save would persist."""

    return hashlib.sha256(_json_bytes(annotation.model_dump(mode="json"))).hexdigest()


def _has_meaningful_human_data(annotation: BenchmarkAnnotations) -> bool:
    return bool(
        annotation.matches
        or annotation.highlights
        or annotation.boring_intervals
        or annotation.notes.strip()
    )


def _next_backup_path(annotation_path: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    candidate = annotation_path.with_name(f"{annotation_path.stem}.backup-{stamp}.json")
    index = 1
    while candidate.exists():
        candidate = annotation_path.with_name(f"{annotation_path.stem}.backup-{stamp}-{index}.json")
        index += 1
    return candidate


def _identity_mismatch(
    baseline: BenchmarkAnnotations, candidate: BenchmarkAnnotations
) -> str | None:
    for field in (
        "schema_version",
        "annotation_version",
        "benchmark_id",
        "case_id",
        "source_sha256",
        "source_duration_ms",
        "game_profile",
        "annotated_by",
    ):
        if getattr(baseline, field) != getattr(candidate, field):
            return field
    if baseline.source_path is None or candidate.source_path is None:
        return "source_path"
    if baseline.source_path.expanduser().resolve() != candidate.source_path.expanduser().resolve():
        return "source_path"
    return None


class _AnnotationHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: AnnotationServer) -> None:
        super().__init__(address, _AnnotationRequestHandler)
        self.app = app


class AnnotationServer:
    """Single-document loopback server with a deliberately tiny attack surface."""

    def __init__(self, annotation_path: Path, config: AppConfig, *, port: int = 0) -> None:
        self.annotation_path = annotation_path.expanduser().resolve()
        if not self.annotation_path.is_file():
            raise ValidationError(
                "Annotation JSON file does not exist.", hint=str(self.annotation_path)
            )
        self.config = config
        self._lock = threading.RLock()
        self.annotation = _load_annotation(self.annotation_path)
        self.source = _verify_source(self.annotation, config)
        self.httpd = _AnnotationHTTPServer(("127.0.0.1", port), self)

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def port(self) -> int:
        return int(self.httpd.server_port)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    @property
    def review_state_path(self) -> Path:
        return _review_state_path(self.annotation_path)

    def serve_forever(self) -> None:
        self.httpd.serve_forever(poll_interval=0.2)

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def assert_source_unchanged(self) -> None:
        """Reject changed source bytes before serving a new range or saving."""

        signature = _source_signature(self.source.path)
        if signature != (self.source.size_bytes, self.source.mtime_ns):
            with self._lock:
                self.source = _verify_source(self.annotation, self.config)
            if signature != (self.source.size_bytes, self.source.mtime_ns):
                raise SourceError("Source changed while the annotation server was open.")

    def annotation_payload(self) -> dict[str, object]:
        with self._lock:
            self.assert_source_unchanged()
            annotation = self.annotation
            review = _load_review_state(self.annotation_path, annotation)
            return {
                "annotation": annotation.model_dump(mode="json"),
                "summary": _summary(annotation, annotation_sha256(self.annotation_path)),
                "review_state": review,
                "source": {
                    "filename": self.source.path.name,
                    "duration_ms": self.source.duration_ms,
                    "mime_type": _video_mime_type(self.source.path),
                },
            }

    def validate_payload(self, payload: object) -> dict[str, object]:
        with self._lock:
            candidate = _validate_payload_model(payload)
            mismatch = _identity_mismatch(self.annotation, candidate)
            if mismatch:
                raise ValidationError(
                    "Annotation identity fields cannot change in the helper.",
                    hint=mismatch,
                )
            source = _verify_source(candidate, self.config)
            if source.path != self.source.path:
                raise SourceError("Candidate source path does not match the opened source.")
            return {
                "status": "VALID_JSON",
                "human_readiness": _human_readiness(
                    candidate, _load_review_state(self.annotation_path, self.annotation)
                ),
                "summary": _summary(
                    candidate,
                    hashlib.sha256(_json_bytes(candidate.model_dump(mode="json"))).hexdigest(),
                ),
            }

    def save_payload(self, payload: object) -> dict[str, object]:
        with self._lock:
            candidate = _validate_payload_model(payload)
            mismatch = _identity_mismatch(self.annotation, candidate)
            if mismatch:
                raise ValidationError(
                    "Annotation identity fields cannot change in the helper.",
                    hint=mismatch,
                )
            _verify_source(candidate, self.config)
            try:
                current_bytes = self.annotation_path.read_bytes()
            except OSError as exc:
                raise StorageError(
                    "Annotation JSON cannot be read before save.", hint=str(self.annotation_path)
                ) from exc
            current_dump = self.annotation.model_dump(mode="json")
            candidate_dump = candidate.model_dump(mode="json")
            if current_dump == candidate_dump:
                return {
                    "status": "UNCHANGED",
                    "annotation": candidate_dump,
                    "summary": _summary(candidate, annotation_sha256(self.annotation_path)),
                    "backup": None,
                }
            backup: Path | None = None
            if _has_meaningful_human_data(self.annotation):
                backup = _next_backup_path(self.annotation_path)
                atomic_write_bytes(backup, current_bytes)
            atomic_write_json(self.annotation_path, candidate_dump)
            self.annotation = candidate
            self.source = _verify_source(candidate, self.config)
            if self.review_state_path.is_file():
                state = _default_review_state(candidate, annotation_sha256(self.annotation_path))
                atomic_write_json(self.review_state_path, state)
            return {
                "status": "SAVED",
                "annotation": candidate_dump,
                "summary": _summary(candidate, annotation_sha256(self.annotation_path)),
                "backup": str(backup) if backup is not None else None,
            }

    def review_state(self) -> dict[str, object]:
        with self._lock:
            return _load_review_state(self.annotation_path, self.annotation)

    def save_review_state(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict) or not isinstance(payload.get("reviewed"), bool):
            raise ValidationError("Review state requires a boolean reviewed field.")
        with self._lock:
            self.assert_source_unchanged()
            state = {
                "schema_version": REVIEW_STATE_VERSION,
                "case_id": self.annotation.case_id,
                "reviewed": payload["reviewed"],
                "updated_at": datetime.now(UTC).isoformat(),
                "annotation_sha256": annotation_sha256(self.annotation_path),
            }
            atomic_write_json(self.review_state_path, state)
            return state

    def video_path(self) -> Path:
        self.assert_source_unchanged()
        return self.source.path


def _validate_payload_model(payload: object) -> BenchmarkAnnotations:
    try:
        return BenchmarkAnnotations.model_validate(payload)
    except (PydanticValidationError, TypeError, ValueError) as exc:
        raise ValidationError("Proposed annotation payload is invalid.", hint=str(exc)) from exc


def _human_readiness(annotation: BenchmarkAnnotations, review: dict[str, object]) -> str:
    if bool(review.get("reviewed")):
        return "HUMAN_REVIEWED"
    if annotation.matches or annotation.highlights or annotation.boring_intervals:
        return "PARTIALLY_ANNOTATED"
    return "EMPTY"


def _video_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".mp4": "video/mp4",
        ".m4v": "video/mp4",
        ".webm": "video/webm",
        ".mkv": "video/x-matroska",
        ".mov": "video/quicktime",
        ".ts": "video/mp2t",
    }.get(suffix, mimetypes.guess_type(path.name)[0] or "application/octet-stream")


def _parse_range(value: str | None, size: int) -> tuple[int, int] | None:
    if value is None:
        return 0, size - 1
    if not value.startswith("bytes=") or "," in value:
        raise ValueError("only one bytes range is supported")
    start_text, end_text = value[6:].split("-", 1)
    if not start_text:
        length = int(end_text)
        if length <= 0:
            raise ValueError("range suffix must be positive")
        return max(0, size - length), size - 1
    start = int(start_text)
    if start < 0 or start >= size:
        raise ValueError("range start is outside the file")
    end = size - 1 if not end_text else min(int(end_text), size - 1)
    if end < start:
        raise ValueError("range end precedes start")
    return start, end


class _AnnotationRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> AnnotationServer:
        server = cast(_AnnotationHTTPServer, self.server)
        return server.app

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            self._check_loopback_request(method)
            route = urlsplit(self.path).path
            if method in {"GET", "HEAD"} and route == "/":
                self._send_bytes(
                    HTTPStatus.OK, "text/html; charset=utf-8", ANNOTATION_HTML.encode()
                )
                return
            if method in {"GET", "HEAD"} and route == "/api/annotation":
                self._send_json(HTTPStatus.OK, self.app.annotation_payload())
                return
            if method in {"GET", "HEAD"} and route == "/api/review-state":
                self._send_json(HTTPStatus.OK, self.app.review_state())
                return
            if method in {"GET", "HEAD"} and route == "/api/video":
                self._send_video(head_only=method == "HEAD")
                return
            if method == "POST" and route == "/api/validate":
                self._send_json(HTTPStatus.OK, self.app.validate_payload(self._read_json()))
                return
            if method == "POST" and route == "/api/save":
                self._send_json(HTTPStatus.OK, self.app.save_payload(self._read_json()))
                return
            if method == "POST" and route == "/api/review-state":
                self._send_json(HTTPStatus.OK, self.app.save_review_state(self._read_json()))
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except AppError as exc:
            status = HTTPStatus.BAD_REQUEST
            if exc.category.value in {"storage", "dependency", "internal"}:
                status = HTTPStatus.INTERNAL_SERVER_ERROR
            self._send_json(status, {"error": exc.message, "hint": exc.hint})
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid request", "hint": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            return

    def _check_loopback_request(self, method: str) -> None:
        host = self.headers.get("Host", "").split(":", 1)[0].strip("[]").lower()
        if host and host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValidationError("Only loopback Host headers are accepted.")
        if method == "POST":
            origin = self.headers.get("Origin")
            if origin and origin.rstrip("/") != self.app.url.rstrip("/"):
                raise ValidationError(
                    "State-changing requests must originate from this loopback page."
                )

    def _read_json(self) -> object:
        length_text = self.headers.get("Content-Length")
        if length_text is None:
            raise ValidationError("JSON request requires Content-Length.")
        length = int(length_text)
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValidationError("JSON request is too large.")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValidationError("JSON request body was truncated.")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("Request body is not valid UTF-8 JSON.") from exc

    def _send_json(self, status: HTTPStatus, value: object) -> None:
        self._send_bytes(status, "application/json; charset=utf-8", _json_bytes(value))

    def _send_bytes(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_video(self, *, head_only: bool) -> None:
        source = self.app.video_path()
        size = source.stat().st_size
        try:
            bounds = _parse_range(self.headers.get("Range"), size)
        except (ValueError, TypeError):
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        assert bounds is not None
        start, end = bounds
        length = end - start + 1
        status = HTTPStatus.PARTIAL_CONTENT if self.headers.get("Range") else HTTPStatus.OK
        self.send_response(status)
        self.send_header("Content-Type", _video_mime_type(source))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if head_only:
            return
        with source.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining:
                chunk = handle.read(min(VIDEO_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


ANNOTATION_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Local M8 human annotation</title>
<style>
:root { color-scheme: dark; font-family: system-ui, sans-serif; }
body { margin: 0; background: #111827; color: #e5e7eb; }
main { max-width: 1180px; margin: 0 auto; padding: 1rem; }
header, section { background: #1f2937; border: 1px solid #374151; border-radius: .6rem; padding: 1rem; margin-bottom: 1rem; }
h1, h2 { margin-top: 0; }
.meta, .toolbar, .row, .time-row { display: flex; flex-wrap: wrap; align-items: center; gap: .5rem; }
.meta { justify-content: space-between; color: #cbd5e1; }
video { width: 100%; max-height: 68vh; background: #000; border-radius: .4rem; }
button, select, input, textarea { font: inherit; color: inherit; background: #111827; border: 1px solid #4b5563; border-radius: .35rem; padding: .45rem .6rem; }
button { cursor: pointer; background: #2563eb; border-color: #3b82f6; }
button.secondary { background: #374151; border-color: #6b7280; }
button.danger { background: #991b1b; border-color: #ef4444; }
button:disabled { opacity: .45; cursor: not-allowed; }
input, select { min-width: 12rem; }
textarea { width: min(100%, 52rem); min-height: 4rem; }
label { color: #cbd5e1; }
.clock { font-size: 1.15rem; font-variant-numeric: tabular-nums; }
.time-value { min-width: 7.5rem; display: inline-block; font-variant-numeric: tabular-nums; color: #93c5fd; }
.item { border-top: 1px solid #374151; padding: .7rem 0; }
.item:first-child { border-top: 0; }
.item small { color: #cbd5e1; }
.status { padding: .6rem; border-radius: .35rem; background: #0f172a; }
.status.ok { color: #86efac; }
.status.warn { color: #fde68a; }
.error { color: #fca5a5; }
.hint { color: #cbd5e1; font-size: .9rem; }
.badge { border: 1px solid #60a5fa; border-radius: 99px; padding: .2rem .55rem; color: #bfdbfe; }
</style>
</head>
<body>
<main>
<header>
  <div class="meta"><h1 id="case-title">M8 human annotation</h1><span class="badge">LOCAL-ONLY · NO MODEL ASSISTANCE</span></div>
  <div id="source-info" class="hint">Loading private source…</div>
  <div id="readiness" class="status warn">Loading…</div>
</header>
<section>
  <video id="video" controls preload="metadata"><source src="/api/video"></video>
  <div id="video-error" class="error" hidden>Browser playback failed for this container. The source is read-only; no automatic re-encode is performed.</div>
  <div class="clock"><span id="current">00:00.000</span> / <span id="duration">00:00.000</span></div>
  <div class="toolbar">
    <button class="secondary" id="back5">-5 sec</button><button class="secondary" id="back1">-1 sec</button>
    <button id="play">Play</button><button class="secondary" id="forward1">+1 sec</button><button class="secondary" id="forward5">+5 sec</button>
  </div>
</section>
<section>
  <h2>Highlight</h2>
  <div class="time-row"><span>Setup</span><span id="highlight-setup" class="time-value">unset</span><button class="secondary" data-capture="highlight-setup">Set Setup Start</button></div>
  <div class="time-row"><span>Event start</span><span id="highlight-start" class="time-value">unset</span><button class="secondary" data-capture="highlight-start">Set Event Start</button></div>
  <div class="time-row"><span>Event end</span><span id="highlight-end" class="time-value">unset</span><button class="secondary" data-capture="highlight-end">Set Event End</button></div>
  <div class="time-row"><span>Payoff</span><span id="highlight-payoff" class="time-value">unset</span><button class="secondary" data-capture="highlight-payoff">Set Payoff End</button></div>
  <div class="row"><label for="importance">Importance</label><select id="importance"><option>MUST_CATCH</option><option>WORTH_REVIEW</option><option>OPTIONAL</option></select><label for="modality">Modality</label><select id="modality"><option>VISUAL</option><option>AUDIO</option><option>VISUAL_AND_AUDIO</option><option>UNKNOWN</option></select></div>
  <div class="row"><label for="category">Category (optional)</label><input id="category" list="category-list" placeholder="e.g. CLUTCH"><datalist id="category-list"><option>FUNNY</option><option>FAIL</option><option>CLUTCH</option><option>REACTION</option><option>SMART_PLAY</option><option>FRIEND_MOMENT</option><option>WTF_UNEXPECTED</option><option>TENSION_PAYOFF</option><option>SKILL</option><option>OTHER</option></datalist></div>
  <div><label for="highlight-notes">Notes (optional)</label><br><textarea id="highlight-notes"></textarea></div>
  <div class="row"><button id="add-highlight">Add Highlight</button><button class="secondary" id="cancel-highlight" hidden>Cancel edit</button></div>
</section>
<section>
  <h2>Boring interval</h2>
  <div class="time-row"><span>Start</span><span id="boring-start" class="time-value">unset</span><button class="secondary" data-capture="boring-start">Set Boring Start</button></div>
  <div class="time-row"><span>End</span><span id="boring-end" class="time-value">unset</span><button class="secondary" data-capture="boring-end">Set Boring End</button></div>
  <div><label for="boring-notes">Notes (optional)</label><br><textarea id="boring-notes"></textarea></div>
  <div class="row"><button id="add-boring">Add Boring Interval</button><button class="secondary" id="cancel-boring" hidden>Cancel edit</button></div>
</section>
<section>
  <h2>Match / round (optional)</h2>
  <div class="time-row"><span>Start</span><span id="match-start" class="time-value">unset</span><button class="secondary" data-capture="match-start">Set Match Start</button></div>
  <div class="time-row"><span>End</span><span id="match-end" class="time-value">unset</span><button class="secondary" data-capture="match-end">Set Match End</button></div>
  <div class="row"><label for="match-label">Label</label><input id="match-label" placeholder="optional round label"><label for="match-ordinal">Ordinal</label><input id="match-ordinal" type="number" min="0" step="1"></div>
  <div><label for="match-notes">Notes (optional)</label><br><textarea id="match-notes"></textarea></div>
  <div class="row"><button id="add-match">Add Match</button><button class="secondary" id="cancel-match" hidden>Cancel edit</button></div>
</section>
<section>
  <h2>Existing annotations</h2>
  <div id="items"></div>
</section>
<section>
  <div class="row"><button id="validate">Validate</button><button id="save">Save</button><button class="secondary" id="review">I have reviewed this entire case</button></div>
  <div id="result" class="status">No validation run yet.</div>
  <p class="hint">VALID JSON is not the same as READY FOR PROVIDER BENCHMARK. Human review must be explicitly saved; this helper never suggests highlights or uses model output.</p>
</section>
</main>
<script>
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const video = $("video");
  let state = null;
  let reviewState = { reviewed: false };
  let dirty = false;
  let editing = null;
  const draft = { highlight: { setup: null, start: null, end: null, payoff: null }, boring: { start: null, end: null }, match: { start: null, end: null } };
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[ch]));
  const fmt = (ms) => { ms = Math.max(0, Math.round(Number(ms) || 0)); const h = Math.floor(ms / 3600000); ms %= 3600000; const m = Math.floor(ms / 60000); ms %= 60000; const s = Math.floor(ms / 1000); const x = ms % 1000; return (h ? String(h).padStart(2,"0") + ":" : "") + String(m).padStart(2,"0") + ":" + String(s).padStart(2,"0") + "." + String(x).padStart(3,"0"); };
  const now = () => Math.max(0, Math.round((video.currentTime || 0) * 1000));
  const markDirty = () => { dirty = true; render(); };
  const setText = (id, value) => { $(id).textContent = value == null ? "unset" : fmt(value); };
  const nextId = (prefix, items) => { let n = 1; while (items.some((item) => item.annotation_id === `${prefix}-${String(n).padStart(4,"0")}`)) n += 1; return `${prefix}-${String(n).padStart(4,"0")}`; };
  async function api(path, options = {}) { const response = await fetch(path, { ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } }); const body = await response.json().catch(() => ({})); if (!response.ok) throw new Error(body.error || "Local request failed"); return body; }
  function setResult(message, kind = "") { const target = $("result"); target.textContent = message; target.className = `status ${kind}`; }
  function readiness() { if (reviewState.reviewed) return "HUMAN_REVIEWED"; if (state && (state.highlights.length || state.boring_intervals.length || state.matches.length)) return "PARTIALLY_ANNOTATED"; return "EMPTY"; }
  function render() {
    if (!state) return;
    $("case-title").textContent = `Case ${state.case_id}`;
    $("source-info").textContent = `${state.game_profile} · ${state.source_path ? state.source_path.split(/[\\/]/).pop() : "private source"} · ${fmt(state.source_duration_ms)}`;
    $("readiness").textContent = `Human annotation: ${readiness()}${dirty ? " · unsaved changes" : ""}`;
    $("readiness").className = `status ${dirty || !reviewState.reviewed ? "warn" : "ok"}`;
    setText("highlight-setup", draft.highlight.setup); setText("highlight-start", draft.highlight.start); setText("highlight-end", draft.highlight.end); setText("highlight-payoff", draft.highlight.payoff);
    setText("boring-start", draft.boring.start); setText("boring-end", draft.boring.end); setText("match-start", draft.match.start); setText("match-end", draft.match.end);
    $("cancel-highlight").hidden = !(editing && editing.kind === "highlight"); $("cancel-boring").hidden = !(editing && editing.kind === "boring"); $("cancel-match").hidden = !(editing && editing.kind === "match");
    const groups = [["Highlights", state.highlights, "highlight"], ["Boring intervals", state.boring_intervals, "boring"], ["Matches", state.matches, "match"]];
    $("items").innerHTML = groups.map(([title, items, kind]) => `<h3>${title}</h3>` + (items.length ? items.map((item, index) => itemHtml(item, index, kind)).join("") : `<p class="hint">None yet.</p>`)).join("");
  }
  function itemHtml(item, index, kind) {
    const start = kind === "highlight" ? item.event_start_ms : item.start_ms; const end = kind === "highlight" ? item.event_end_ms : item.end_ms; const label = kind === "highlight" ? `${item.importance} · ${item.modality}${item.category ? ` · ${item.category}` : ""}` : (item.label || ""); const notes = item.notes ? `<br><small>${esc(item.notes)}</small>` : "";
    return `<div class="item"><strong>${esc(label || kind)}</strong> <span>${fmt(start)} → ${fmt(end)} (${fmt(end - start)})</span>${notes}<div class="row"><button class="secondary" data-action="jump" data-kind="${kind}" data-index="${index}">Jump</button><button class="secondary" data-action="edit" data-kind="${kind}" data-index="${index}">Edit</button><button class="danger" data-action="delete" data-kind="${kind}" data-index="${index}">Delete</button></div></div>`;
  }
  function capture(target) { const [kind, field] = target.split("-"); if (kind === "highlight") draft.highlight[field] = now(); else draft[kind][field] = now(); markDirty(); }
  function reset(kind) { if (kind === "highlight") { draft.highlight = { setup: null, start: null, end: null, payoff: null }; $("importance").value = "MUST_CATCH"; $("modality").value = "VISUAL"; $("category").value = ""; $("highlight-notes").value = ""; } else if (kind === "boring") { draft.boring = { start: null, end: null }; $("boring-notes").value = ""; } else { draft.match = { start: null, end: null }; $("match-label").value = ""; $("match-ordinal").value = ""; $("match-notes").value = ""; } editing = null; render(); }
  function edit(kind, index) { const list = kind === "highlight" ? state.highlights : kind === "boring" ? state.boring_intervals : state.matches; const item = list[index]; editing = { kind, index, id: item.annotation_id }; if (kind === "highlight") { draft.highlight = { setup: item.setup_start_ms, start: item.event_start_ms, end: item.event_end_ms, payoff: item.payoff_end_ms }; $("importance").value = item.importance; $("modality").value = item.modality; $("category").value = item.category || ""; $("highlight-notes").value = item.notes || ""; } else if (kind === "boring") { draft.boring = { start: item.start_ms, end: item.end_ms }; $("boring-notes").value = item.notes || ""; } else { draft.match = { start: item.start_ms, end: item.end_ms }; $("match-label").value = item.label || ""; $("match-ordinal").value = item.ordinal ?? ""; $("match-notes").value = item.notes || ""; } render(); }
  function remove(kind, index) { const list = kind === "highlight" ? state.highlights : kind === "boring" ? state.boring_intervals : state.matches; if (!confirm("Delete this annotation from the working document?")) return; const removed = list.splice(index, 1)[0]; if (kind === "match") state.highlights.forEach((item) => { if (item.match_annotation_id === removed.annotation_id) item.match_annotation_id = null; }); markDirty(); }
  function addHighlight() { if (draft.highlight.start == null || draft.highlight.end == null || draft.highlight.end <= draft.highlight.start) return alert("Set an event start and a later event end."); const existing = editing && editing.kind === "highlight" ? state.highlights[editing.index] : null; const item = { annotation_id: editing?.id || nextId("hl", state.highlights), match_annotation_id: existing?.match_annotation_id || null, event_start_ms: draft.highlight.start, event_end_ms: draft.highlight.end, setup_start_ms: draft.highlight.setup, payoff_end_ms: draft.highlight.payoff, category: $("category").value.trim() || null, importance: $("importance").value, modality: $("modality").value, notes: $("highlight-notes").value.trim() || null }; if (existing) state.highlights[editing.index] = item; else state.highlights.push(item); markDirty(); reset("highlight"); }
  function addBoring() { if (draft.boring.start == null || draft.boring.end == null || draft.boring.end <= draft.boring.start) return alert("Set a boring start and a later boring end."); const item = { annotation_id: editing?.id || nextId("boring", state.boring_intervals), start_ms: draft.boring.start, end_ms: draft.boring.end, notes: $("boring-notes").value.trim() || null }; if (editing && editing.kind === "boring") state.boring_intervals[editing.index] = item; else state.boring_intervals.push(item); markDirty(); reset("boring"); }
  function addMatch() { if (draft.match.start == null || draft.match.end == null || draft.match.end <= draft.match.start) return alert("Set a match start and a later match end."); const ordinal = $("match-ordinal").value === "" ? null : Number($("match-ordinal").value); const item = { annotation_id: editing?.id || nextId("match", state.matches), ordinal, start_ms: draft.match.start, end_ms: draft.match.end, label: $("match-label").value.trim() || null, confidence: null, notes: $("match-notes").value.trim() || null }; if (editing && editing.kind === "match") state.matches[editing.index] = item; else state.matches.push(item); markDirty(); reset("match"); }
  async function validate() { try { const result = await api("/api/validate", { method: "POST", body: JSON.stringify(state) }); const s = result.summary; const m = s.modality; setResult(`VALID JSON · source PASS · duration ${fmt(s.source_duration_ms)} · highlights ${s.highlights_count} (MUST ${s.MUST_CATCH}, WORTH ${s.WORTH_REVIEW}, OPTIONAL ${s.OPTIONAL}) · modality V:${m.VISUAL} A:${m.AUDIO} VA:${m.VISUAL_AND_AUDIO} U:${m.UNKNOWN} · boring ${s.boring_interval_count} · matches ${s.matches_count} · ${result.human_readiness}`, "ok"); } catch (error) { setResult(error.message, "error"); } }
  async function save() { try { const result = await api("/api/save", { method: "POST", body: JSON.stringify(state) }); state = result.annotation; dirty = false; setResult(`${result.status} · annotation SHA ${result.summary.annotation_sha256}`, "ok"); render(); } catch (error) { setResult(error.message, "error"); } }
  async function setReviewed() { if (dirty) return setResult("Save changes before marking the case reviewed.", "warn"); try { reviewState = await api("/api/review-state", { method: "POST", body: JSON.stringify({ reviewed: !reviewState.reviewed }) }); setResult(reviewState.reviewed ? "HUMAN_REVIEWED saved" : "Review mark cleared", "ok"); render(); } catch (error) { setResult(error.message, "error"); } }
  $("video").addEventListener("timeupdate", () => { $("current").textContent = fmt(now()); }); $("video").addEventListener("loadedmetadata", () => { $("duration").textContent = fmt(Math.round(video.duration * 1000)); }); $("video").addEventListener("error", () => { $("video-error").hidden = false; });
  $("play").onclick = () => { if (video.paused) { video.play(); $("play").textContent = "Pause"; } else { video.pause(); $("play").textContent = "Play"; } }; [["back5",-5000],["back1",-1000],["forward1",1000],["forward5",5000]].forEach(([id, delta]) => { $(id).onclick = () => { video.currentTime = Math.max(0, video.currentTime + delta / 1000); }; });
  document.querySelectorAll("[data-capture]").forEach((button) => button.addEventListener("click", () => capture(button.dataset.capture))); $("add-highlight").onclick = addHighlight; $("add-boring").onclick = addBoring; $("add-match").onclick = addMatch; $("cancel-highlight").onclick = () => reset("highlight"); $("cancel-boring").onclick = () => reset("boring"); $("cancel-match").onclick = () => reset("match"); $("validate").onclick = validate; $("save").onclick = save; $("review").onclick = setReviewed;
  $("items").addEventListener("click", (event) => { const button = event.target.closest("button[data-action]"); if (!button) return; const kind = button.dataset.kind; const index = Number(button.dataset.index); const list = kind === "highlight" ? state.highlights : kind === "boring" ? state.boring_intervals : state.matches; const item = list[index]; if (button.dataset.action === "jump") video.currentTime = (kind === "highlight" ? item.event_start_ms : item.start_ms) / 1000; if (button.dataset.action === "edit") edit(kind, index); if (button.dataset.action === "delete") remove(kind, index); });
  document.addEventListener("keydown", (event) => { if (["INPUT","TEXTAREA","SELECT"].includes(document.activeElement?.tagName)) return; if (event.code === "Space") { event.preventDefault(); $("play").click(); } if (event.key === "ArrowLeft") { event.preventDefault(); video.currentTime = Math.max(0, video.currentTime - (event.shiftKey ? 5 : 1)); } if (event.key === "ArrowRight") { event.preventDefault(); video.currentTime += event.shiftKey ? 5 : 1; } });
  window.addEventListener("beforeunload", (event) => { if (dirty) { event.preventDefault(); event.returnValue = ""; } });
  api("/api/annotation").then((payload) => { state = payload.annotation; reviewState = payload.review_state; $("duration").textContent = fmt(state.source_duration_ms); render(); }).catch((error) => setResult(error.message, "error"));
})();
</script>
</body>
</html>
"""


__all__ = [
    "ANNOTATION_HTML",
    "AnnotationServer",
    "SourceIdentity",
    "annotation_sha256_from_model",
]
