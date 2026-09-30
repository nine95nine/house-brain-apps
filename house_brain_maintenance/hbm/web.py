"""Tap-to-open approval page served through Home Assistant ingress.

Tapping the push opens this page inside the Home Assistant app; the owner then
taps Approve or Reject. Rules (all must hold for a decision to count):

* the TCP peer is the Supervisor ingress proxy (172.30.32.2);
* the Supervisor-inserted ``X-Remote-User-Id`` equals the owner's user id
  (Supervisor strips any client-supplied copy of this header);
* the form carries the current single-use nonce and the request is a POST;
* the pending request has not expired or already been decided.

The page never approves on open: an accidental tap on the push is harmless.
It has no other routes and serves no files.
"""
from __future__ import annotations

import html
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

INGRESS_PEER = "172.30.32.2"
MAX_BODY = 2048
RE_INGRESS_PATH = re.compile(r"/api/hassio_ingress/[A-Za-z0-9_\-]{1,200}")


@dataclass
class Pending:
    stage: str
    nonce: str
    title: str
    message: str
    owner_user_id: str
    deadline: float
    decision: str | None = None
    decided_by: str | None = None


class ApprovalBoard:
    """Thread-safe holder of the single pending approval."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: Pending | None = None
        self._last: str = ""

    def open(self, pending: Pending) -> None:
        with self._lock:
            self._pending = pending

    def close(self) -> None:
        with self._lock:
            if self._pending is not None:
                self._last = f"{self._pending.stage}: {self._pending.decision or 'no answer'}"
            self._pending = None

    def snapshot(self) -> tuple[Pending | None, str]:
        with self._lock:
            return self._pending, self._last

    def decision(self, nonce: str) -> str | None:
        with self._lock:
            if self._pending is not None and self._pending.nonce == nonce:
                return self._pending.decision
            return None

    def decide(self, nonce: str, user_id: str, choice: str) -> int:
        """Return an HTTP status: 200 accepted, else the refusal reason."""
        with self._lock:
            p = self._pending
            if p is None or p.decision is not None:
                return 409
            if not nonce or nonce != p.nonce:
                return 409
            if time.monotonic() > p.deadline:
                return 410
            if user_id != p.owner_user_id:
                return 403
            if choice not in ("approve", "reject"):
                return 400
            p.decision = choice
            p.decided_by = user_id
            return 200


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>House Brain Maintenance</title><style>
body{{font-family:-apple-system,system-ui,sans-serif;margin:0;padding:16px;background:#111;color:#eee}}
h1{{font-size:20px}} pre{{white-space:pre-wrap;background:#1d1d1d;padding:12px;border-radius:8px;font-size:14px}}
button{{width:100%;padding:18px;margin:8px 0;font-size:20px;border:0;border-radius:12px}}
.a{{background:#2e7d32;color:#fff}} .r{{background:#8e2424;color:#fff}} .m{{color:#aaa;font-size:14px}}
</style></head><body>{body}</body></html>"""


def render(pending: Pending | None, last: str, action: str, note: str = "") -> str:
    if pending is None or pending.decision is not None:
        body = "<h1>House Brain Maintenance</h1><p>Nothing is waiting for your approval.</p>"
        if note:
            body += f"<p><b>{html.escape(note)}</b></p>"
        if last:
            body += f'<p class="m">Last: {html.escape(last)}</p>'
        return PAGE.format(body=body)
    left = max(0, int(pending.deadline - time.monotonic()))
    body = (
        f"<h1>{html.escape(pending.title)}</h1>"
        f"<pre>{html.escape(pending.message)}</pre>"
        f'<p class="m">Expires in {left // 60} min {left % 60} s. No answer = Reject.</p>'
        f'<form method="post" action="{html.escape(action)}">'
        f'<input type="hidden" name="nonce" value="{html.escape(pending.nonce)}">'
        '<button class="a" name="choice" value="approve">Approve</button>'
        '<button class="r" name="choice" value="reject">Reject</button></form>'
    )
    return PAGE.format(body=body)


def make_handler(board: ApprovalBoard, allowed_peer: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "hbm"
        sys_version = ""

        def log_message(self, *args):  # noqa: D401 - no request logging (no secrets, no noise)
            return

        def _send(self, status: int, text: str) -> None:
            data = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
            self.end_headers()
            self.wfile.write(data)

        def _peer_ok(self) -> bool:
            return self.client_address[0] == allowed_peer

        def _action(self) -> str:
            base = self.headers.get("X-Ingress-Path", "")
            if not RE_INGRESS_PATH.fullmatch(base):
                base = ""
            return f"{base}/decide"

        def do_GET(self):  # noqa: N802
            if not self._peer_ok():
                return self._send(403, "forbidden")
            if urllib.parse.urlsplit(self.path).path not in ("/", ""):
                return self._send(404, "not found")
            pending, last = board.snapshot()
            self._send(200, render(pending, last, self._action()))

        def do_POST(self):  # noqa: N802
            if not self._peer_ok():
                return self._send(403, "forbidden")
            if urllib.parse.urlsplit(self.path).path != "/decide":
                return self._send(404, "not found")
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                length = -1
            if not 0 < length <= MAX_BODY:
                return self._send(413, "bad request size")
            if not (self.headers.get("Content-Type") or "").startswith("application/x-www-form-urlencoded"):
                return self._send(415, "unsupported")
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            nonce = (form.get("nonce") or [""])[0]
            choice = (form.get("choice") or [""])[0]
            user = self.headers.get("X-Remote-User-Id", "")
            status = board.decide(nonce, user, choice)
            pending, last = board.snapshot()
            notes = {200: f"Recorded: {choice}. You can close this page.",
                     403: "Only the owner account can decide.", 409: "This request is no longer waiting.",
                     410: "This request expired (counted as Reject).", 400: "Unknown choice."}
            self._send(200 if status == 200 else status,
                       render(None if status == 200 else pending, last, self._action(), notes.get(status, "")))

        def do_PUT(self):  # noqa: N802
            self._send(405, "method not allowed")

        do_DELETE = do_PATCH = do_PUT

    return Handler


class IngressServer:
    def __init__(self, board: ApprovalBoard, host: str = "0.0.0.0", port: int = 8099,  # noqa: S104 - ingress
                 allowed_peer: str = INGRESS_PEER) -> None:
        self.httpd = ThreadingHTTPServer((host, port), make_handler(board, allowed_peer))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
