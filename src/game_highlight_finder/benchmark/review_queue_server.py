"""Local-only visual adjudication helper for private development review queues.

This helper is intentionally provider-free.  It serves only review clips declared by one
local queue JSON, accepts explicit human labels, and persists a sidecar JSON next to the
queue.  It never promotes labels into BenchmarkAnnotations automatically.
"""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import mimetypes
import threading
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal, cast
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from game_highlight_finder.errors import AppError, StorageError, ValidationError
from game_highlight_finder.storage.atomic import atomic_write_json, read_json

MAX_REQUEST_BYTES = 2 * 1024 * 1024
VIDEO_CHUNK_BYTES = 64 * 1024
REVIEW_ADJUDICATION_VERSION = 1
ReviewDecision = Literal["POSITIVE", "BORING", "UNCERTAIN"]


class ReviewQueueInterval(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    review_id: str = Field(min_length=1, max_length=128)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    peak_db: float | None = None
    review_clip: str = Field(min_length=1, max_length=4096)
    notes: str | None = None


class ReviewQueueCase(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    case: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=4096)
    duration_ms: int = Field(gt=0)
    intervals: tuple[ReviewQueueInterval, ...]


class ReviewQueueDocument(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_version: Literal[1] = 1
    set_id: str = Field(min_length=1, max_length=128)
    not_ground_truth: bool
    excluded_from_m8_acceptance: bool
    provider_calls: Literal[0]
    cases: tuple[ReviewQueueCase, ...]


class ReviewDecisionItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    review_id: str = Field(min_length=1, max_length=128)
    decision: ReviewDecision
    notes: str | None = Field(default=None, max_length=4000)


class ReviewAdjudicationDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    set_id: str = Field(min_length=1, max_length=128)
    queue_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    updated_at: datetime
    selected_cases: tuple[str, ...]
    decisions: tuple[ReviewDecisionItem, ...]
    provider_calls: Literal[0] = 0


class _ReviewQueueHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: ReviewQueueServer) -> None:
        super().__init__(address, _ReviewQueueRequestHandler)
        self.app = app


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _parse_range(value: str | None, size: int) -> tuple[int, int]:
    if size <= 0:
        raise ValueError("clip is empty")
    if value is None:
        return 0, size - 1
    if not value.startswith("bytes=") or "," in value:
        raise ValueError("only one byte range is supported")
    start_text, end_text = value[6:].split("-", 1)
    if not start_text:
        length = int(end_text)
        if length <= 0:
            raise ValueError("range suffix must be positive")
        return max(0, size - length), size - 1
    start = int(start_text)
    if start < 0 or start >= size:
        raise ValueError("range start is outside the clip")
    end = size - 1 if not end_text else min(int(end_text), size - 1)
    if end < start:
        raise ValueError("range end precedes start")
    return start, end


class ReviewQueueServer:
    """Serve one private review queue and save explicit local visual decisions."""

    def __init__(
        self,
        queue_path: Path,
        *,
        cases: tuple[str, ...] = (),
        output_path: Path | None = None,
        port: int = 0,
    ) -> None:
        self.queue_path = queue_path.expanduser().resolve()
        if not self.queue_path.is_file():
            raise ValidationError("Review queue JSON does not exist.", hint=str(self.queue_path))
        try:
            self.queue = ReviewQueueDocument.model_validate(read_json(self.queue_path))
        except (PydanticValidationError, OSError, TypeError, ValueError) as exc:
            raise ValidationError("Review queue JSON is invalid.", hint=str(self.queue_path)) from exc
        if not self.queue.not_ground_truth or not self.queue.excluded_from_m8_acceptance:
            raise ValidationError(
                "Review queue must remain explicitly non-ground-truth and excluded from M8 acceptance."
            )
        available = {case.case for case in self.queue.cases}
        requested = tuple(dict.fromkeys(cases))
        missing = [case for case in requested if case not in available]
        if missing:
            raise ValidationError("Requested review queue case is missing.", hint=", ".join(missing))
        self.selected_cases = requested or tuple(case.case for case in self.queue.cases)
        self._selected_case_set = set(self.selected_cases)
        self._root = self.queue_path.parent
        self._intervals: dict[str, tuple[ReviewQueueCase, ReviewQueueInterval, Path]] = {}
        for case in self.queue.cases:
            if case.case not in self._selected_case_set:
                continue
            for interval in case.intervals:
                if interval.end_ms <= interval.start_ms or interval.end_ms > case.duration_ms:
                    raise ValidationError(
                        "Review interval is outside its case duration.", hint=interval.review_id
                    )
                if interval.review_id in self._intervals:
                    raise ValidationError("Duplicate review_id in review queue.", hint=interval.review_id)
                clip = (self._root / interval.review_clip).resolve()
                try:
                    clip.relative_to(self._root)
                except ValueError as exc:
                    raise ValidationError(
                        "Review clip escapes the queue directory.", hint=interval.review_id
                    ) from exc
                if not clip.is_file():
                    raise ValidationError("Review clip is missing.", hint=str(clip))
                self._intervals[interval.review_id] = (case, interval, clip)
        if not self._intervals:
            raise ValidationError("Selected review queue has no intervals.")
        self.output_path = (
            output_path.expanduser().resolve()
            if output_path is not None
            else self.queue_path.with_name(f"{self.queue_path.stem}.adjudication.json")
        )
        self.queue_sha256 = hashlib.sha256(self.queue_path.read_bytes()).hexdigest()
        self._lock = threading.RLock()
        self.httpd = _ReviewQueueHTTPServer(("127.0.0.1", port), self)

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def port(self) -> int:
        return int(self.httpd.server_port)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def serve_forever(self) -> None:
        self.httpd.serve_forever(poll_interval=0.2)

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def _load_existing(self) -> dict[str, ReviewDecisionItem]:
        if not self.output_path.is_file():
            return {}
        try:
            document = ReviewAdjudicationDocument.model_validate(read_json(self.output_path))
        except (PydanticValidationError, OSError, TypeError, ValueError) as exc:
            raise ValidationError(
                "Review adjudication sidecar is invalid.", hint=str(self.output_path)
            ) from exc
        if document.set_id != self.queue.set_id or document.queue_sha256 != self.queue_sha256:
            return {}
        return {
            item.review_id: item
            for item in document.decisions
            if item.review_id in self._intervals
        }

    def payload(self) -> dict[str, object]:
        with self._lock:
            existing = self._load_existing()
            cases: list[dict[str, object]] = []
            for case in self.queue.cases:
                if case.case not in self._selected_case_set:
                    continue
                intervals: list[dict[str, object]] = []
                for interval in case.intervals:
                    if interval.review_id not in self._intervals:
                        continue
                    saved = existing.get(interval.review_id)
                    intervals.append(
                        {
                            "review_id": interval.review_id,
                            "start_ms": interval.start_ms,
                            "end_ms": interval.end_ms,
                            "peak_db": interval.peak_db,
                            "notes": interval.notes,
                            "decision": saved.decision if saved is not None else None,
                            "decision_notes": saved.notes if saved is not None else None,
                        }
                    )
                cases.append(
                    {
                        "case": case.case,
                        "session_id": case.session_id,
                        "source": case.source,
                        "duration_ms": case.duration_ms,
                        "intervals": intervals,
                    }
                )
            return {
                "set_id": self.queue.set_id,
                "queue_sha256": self.queue_sha256,
                "selected_cases": list(self.selected_cases),
                "cases": cases,
                "reviewed_count": len(existing),
                "interval_count": len(self._intervals),
                "provider_calls": 0,
                "warning": (
                    "Development calibration review only. Decisions are explicit visual labels and "
                    "are not promoted to BenchmarkAnnotations automatically."
                ),
            }

    def save(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict) or not isinstance(payload.get("decisions"), list):
            raise ValidationError("Review save requires a decisions array.")
        decisions: list[ReviewDecisionItem] = []
        seen: set[str] = set()
        for raw in payload["decisions"]:
            try:
                item = ReviewDecisionItem.model_validate(raw)
            except (PydanticValidationError, TypeError, ValueError) as exc:
                raise ValidationError("Review decision is invalid.", hint=str(exc)) from exc
            if item.review_id not in self._intervals:
                raise ValidationError("Review decision references an unknown review_id.", hint=item.review_id)
            if item.review_id in seen:
                raise ValidationError("Review save contains duplicate review_id.", hint=item.review_id)
            seen.add(item.review_id)
            decisions.append(item)
        with self._lock:
            document = ReviewAdjudicationDocument(
                set_id=self.queue.set_id,
                queue_sha256=self.queue_sha256,
                updated_at=datetime.now(UTC),
                selected_cases=self.selected_cases,
                decisions=tuple(decisions),
            )
            try:
                atomic_write_json(self.output_path, document.model_dump(mode="json"))
            except OSError as exc:
                raise StorageError("Could not save review adjudication sidecar.", hint=str(self.output_path)) from exc
            return {
                "status": "SAVED",
                "output_path": str(self.output_path),
                "reviewed_count": len(decisions),
                "interval_count": len(self._intervals),
                "complete": len(decisions) == len(self._intervals),
                "provider_calls": 0,
            }

    def clip_path(self, review_id: str) -> Path:
        try:
            return self._intervals[review_id][2]
        except KeyError as exc:
            raise ValidationError("Unknown review clip.", hint=review_id) from exc


class _ReviewQueueRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> ReviewQueueServer:
        return cast(_ReviewQueueHTTPServer, self.server).app

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
            parsed = urlsplit(self.path)
            if method in {"GET", "HEAD"} and parsed.path == "/":
                self._send_bytes(HTTPStatus.OK, "text/html; charset=utf-8", REVIEW_QUEUE_HTML.encode())
                return
            if method in {"GET", "HEAD"} and parsed.path == "/api/queue":
                self._send_json(HTTPStatus.OK, self.app.payload())
                return
            if method in {"GET", "HEAD"} and parsed.path == "/api/clip":
                review_id = parse_qs(parsed.query).get("id", [""])[0]
                self._send_clip(review_id, head_only=method == "HEAD")
                return
            if method == "POST" and parsed.path == "/api/save":
                self._send_json(HTTPStatus.OK, self.app.save(self._read_json()))
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
                raise ValidationError("State-changing requests must originate from this loopback page.")

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

    def _send_clip(self, review_id: str, *, head_only: bool) -> None:
        clip = self.app.clip_path(review_id)
        size = clip.stat().st_size
        try:
            start, end = _parse_range(self.headers.get("Range"), size)
        except (ValueError, TypeError):
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        length = end - start + 1
        status = HTTPStatus.PARTIAL_CONTENT if self.headers.get("Range") else HTTPStatus.OK
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(clip.name)[0] or "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if head_only:
            return
        with clip.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining:
                chunk = handle.read(min(VIDEO_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


REVIEW_QUEUE_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Local calibration review queue</title>
<style>
:root{color-scheme:dark;font-family:system-ui,sans-serif}body{margin:0;background:#111827;color:#e5e7eb}main{max-width:1100px;margin:auto;padding:16px}header,.card{background:#1f2937;border:1px solid #374151;border-radius:10px;padding:14px;margin-bottom:14px}.meta{color:#cbd5e1}.warn{color:#fde68a}.ok{color:#86efac}video{width:100%;max-height:520px;background:#000;border-radius:8px}button,textarea{font:inherit;color:inherit;border:1px solid #4b5563;border-radius:6px;padding:8px;background:#111827}button{cursor:pointer;margin:4px}.positive{background:#166534}.boring{background:#7f1d1d}.uncertain{background:#854d0e}.selected{outline:3px solid #93c5fd}textarea{width:calc(100% - 18px);min-height:48px}.badge{padding:2px 8px;border:1px solid #60a5fa;border-radius:99px}.toolbar{display:flex;flex-wrap:wrap;gap:4px;align-items:center}</style></head>
<body><main><header><h1>Calibration review queue <span class="badge">LOCAL · PROVIDER CALLS ZERO</span></h1><p class="warn" id="warning"></p><p id="summary"></p><button id="save">Save adjudication</button><span id="result" class="meta"></span></header><div id="cards"></div></main>
<script>(()=>{"use strict";const decisions=new Map();let payload=null;const esc=v=>String(v??"").replace(/[&<>"']/g,ch=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[ch]));const fmt=ms=>`${(ms/1000).toFixed(3)}s`;async function api(path,opts={}){const r=await fetch(path,{...opts,headers:{"Content-Type":"application/json",...(opts.headers||{})}});const b=await r.json().catch(()=>({}));if(!r.ok)throw new Error(b.error||"request failed");return b}function render(){const cards=[];for(const c of payload.cases){cards.push(`<h2>${esc(c.case)}</h2>`);for(const i of c.intervals){const saved=decisions.get(i.review_id)||{decision:i.decision,notes:i.decision_notes||""};if(saved.decision)decisions.set(i.review_id,saved);cards.push(`<div class="card" data-id="${esc(i.review_id)}"><h3>${esc(i.review_id)} · ${fmt(i.start_ms)} → ${fmt(i.end_ms)}</h3><p class="meta">peak ${i.peak_db??"n/a"} dB</p><video controls preload="metadata" src="/api/clip?id=${encodeURIComponent(i.review_id)}"></video><div class="toolbar"><button class="positive ${saved.decision==="POSITIVE"?"selected":""}" data-d="POSITIVE">POSITIVE</button><button class="boring ${saved.decision==="BORING"?"selected":""}" data-d="BORING">BORING</button><button class="uncertain ${saved.decision==="UNCERTAIN"?"selected":""}" data-d="UNCERTAIN">UNCERTAIN</button></div><textarea placeholder="optional visual notes">${esc(saved.notes||"")}</textarea></div>`);}}document.getElementById("cards").innerHTML=cards.join("");document.getElementById("summary").textContent=`${decisions.size}/${payload.interval_count} reviewed · cases ${payload.selected_cases.join(", ")}`;document.querySelectorAll(".card button[data-d]").forEach(btn=>btn.onclick=()=>{const card=btn.closest(".card");const id=card.dataset.id;const notes=card.querySelector("textarea").value.trim()||null;decisions.set(id,{decision:btn.dataset.d,notes});render()});document.querySelectorAll(".card textarea").forEach(area=>area.onchange=()=>{const card=area.closest(".card");const id=card.dataset.id;const current=decisions.get(id);if(current)decisions.set(id,{...current,notes:area.value.trim()||null})})}document.getElementById("save").onclick=async()=>{try{const out=await api("/api/save",{method:"POST",body:JSON.stringify({decisions:[...decisions.entries()].map(([review_id,v])=>({review_id,...v}))})});document.getElementById("result").textContent=`${out.status}: ${out.reviewed_count}/${out.interval_count}${out.complete?" COMPLETE":""}`;}catch(e){document.getElementById("result").textContent=e.message}};api("/api/queue").then(p=>{payload=p;document.getElementById("warning").textContent=p.warning;for(const c of p.cases)for(const i of c.intervals)if(i.decision)decisions.set(i.review_id,{decision:i.decision,notes:i.decision_notes});render()}).catch(e=>document.getElementById("result").textContent=e.message)})();</script></body></html>"""


__all__ = [
    "REVIEW_QUEUE_HTML",
    "ReviewAdjudicationDocument",
    "ReviewDecisionItem",
    "ReviewQueueDocument",
    "ReviewQueueServer",
]
