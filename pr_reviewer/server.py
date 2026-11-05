"""
Step 1: the webhook listener (Python standard library only, no web framework).

POST /webhook   GitHub "pull_request" events, verified with X-Hub-Signature-256 (HMAC-SHA256
                of the raw body with WEBHOOK_SECRET). Valid PR events are answered at once with
                202 and reviewed on a worker thread, one at a time, so two merges never race.
GET  /health    liveness check.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, Optional, Tuple

from .git_platform import PREvent, parse_github_event

MAX_BODY = 5 * 1024 * 1024


def verify_signature(secret: Optional[str], body: bytes, header: Optional[str]) -> bool:
    if not secret:
        return True  # only when started with --no-verify (local testing)
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


class Dispatcher:
    """Single worker thread + de-duplication of GitHub delivery IDs."""

    def __init__(self, handle: Callable[[PREvent], None]) -> None:
        self.handle = handle
        self.q: "queue.Queue[PREvent]" = queue.Queue()
        self.seen: set = set()
        self.lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, delivery_id: Optional[str], event: PREvent) -> bool:
        with self.lock:
            if delivery_id and delivery_id in self.seen:
                return False
            if delivery_id:
                self.seen.add(delivery_id)
        self.q.put(event)
        return True

    def _run(self) -> None:
        while True:
            event = self.q.get()
            try:
                self.handle(event)
            except Exception as exc:  # a bug in one review must not kill the listener
                print(f"!! review of {event.repo}#{event.number} crashed: {exc!r}", flush=True)


def handle_webhook(headers: Dict[str, str], body: bytes, secret: Optional[str],
                   submit: Callable[[Optional[str], PREvent], bool]) -> Tuple[int, dict]:
    """Pure request handler (easy to test): returns (HTTP status, JSON body)."""
    h = {k.lower(): v for k, v in headers.items()}
    if not verify_signature(secret, body, h.get("x-hub-signature-256")):
        return 401, {"error": "invalid or missing signature"}
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return 400, {"error": "body is not valid JSON"}
    event, reason = parse_github_event(h.get("x-github-event", ""), payload)
    if event is None:
        return 200, {"ignored": reason}
    accepted = submit(h.get("x-github-delivery"), event)
    if not accepted:
        return 200, {"ignored": "duplicate delivery"}
    return 202, {"accepted": f"{event.repo}#{event.number}", "branch": event.branch}


def make_server(host: str, port: int, secret: Optional[str], dispatcher: Dispatcher) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, data: dict) -> None:
            raw = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):  # noqa: N802
            if self.path.rstrip("/") in ("", "/health"):
                self._send(200, {"status": "ok"})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if self.path.rstrip("/") != "/webhook":
                return self._send(404, {"error": "POST to /webhook"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._send(413, {"error": "payload too large"})
            body = self.rfile.read(length)
            status, data = handle_webhook(dict(self.headers.items()), body, secret, dispatcher.submit)
            print(f"<- {self.headers.get('X-GitHub-Event', '?')}: {status} {data}", flush=True)
            self._send(status, data)

        def log_message(self, fmt, *args):  # keep the console readable
            pass

    return ThreadingHTTPServer((host, port), Handler)
