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

0.3.0: the page also shows the **Recovery Report** card (latest outages, likely cause, checklist).
Its only action, "Got it", follows the same rules (ingress peer, owner user id, single-use code,
POST to ``/recovery/ack``) and only marks the reports read.
"""
from __future__ import annotations

import html
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import ghpage

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


STATUS_TEXT = {"RECOVERED": "✅ back", "PENDING": "⏳ waiting", "ATTENTION": "⚠️ needs you",
               "NOT_OBSERVED": "– not seen"}


def _when(ts: float) -> str:
    lt = time.localtime(ts)
    return f"{time.strftime('%a %b %d', lt)} {lt.tm_hour % 12 or 12}:{lt.tm_min:02d} {'am' if lt.tm_hour < 12 else 'pm'}"


MESH_IDS = {"MESH_ZHA": "zha", "MESH_ZWAVE": "zwave_js", "MESH_MATTER": "matter"}


def _mesh_note(inc: dict, check_id: str) -> str:
    """0.5.2: "after 84 s" or "3 still unavailable (1 before the restart)" for a mesh check."""
    r = ((inc.get("mesh") or {}).get("res") or {}).get(MESH_IDS.get(check_id, ""))
    if not r:
        return ""
    if r.get("seconds") is not None:
        return f" (after {r['seconds']} s)"
    return f" ({r.get('unavailable')} still unavailable, {r.get('baseline')} before the restart)"


def render_ledger(view: dict | None) -> str:
    """0.5.2: restart ledger (30/90-day counts by class, mean time between unplanned failures, feed text)."""
    if not view:
        return ""
    e = html.escape

    def counts(d: dict) -> str:
        return ", ".join(f"{k} {v}" for k, v in d.items()) or "none"
    mtbf = (f"{view['mtbf_unplanned_days']} days" if view.get("mtbf_unplanned_days") is not None
            else f"no unplanned failure in {view.get('observed_days')} days")
    return (f'<p class="m"><b>Restarts</b> 30 d: {e(counts(view.get("restarts_30d") or {}))}; '
            f'90 d: {e(counts(view.get("restarts_90d") or {}))}. Mean time between unplanned failures: '
            f'{e(mtbf)}.</p><details><summary>Stability ledger text (#152 / #163)</summary><pre>'
            f'{e(view.get("feed") or "")}</pre></details>')


def render_recovery(incidents: list[dict], nonce: str, action: str, labels: dict[str, str],
                    ledger: dict | None = None) -> str:
    """The Recovery Report card: latest incident in full, then one line per earlier incident."""
    if not incidents:
        return '<h2>Recovery Report</h2><p class="m">No outages recorded yet.</p>' + render_ledger(ledger)
    e = html.escape
    inc = incidents[0]
    mins = max(1, int(round((inc["end"] - inc["start"]) / 60)))
    tag = inc.get("tag")
    tag_text = (f"<br>{e(tag)}" + (f" (Deployer request {e(inc['request_id'])})" if inc.get("request_id") else "")
                if tag else "")
    out = [f"<h2>Recovery Report</h2><p><b>{e(inc['cause'])}</b> ({e(inc['confidence'])} confidence)<br>"
           f"{e(_when(inc['start']))} → {e(_when(inc['end']))} ({mins} min){tag_text}</p>"]
    if inc.get("evidence"):
        out.append("<ul>" + "".join(f"<li>{e(x)}</li>" for x in inc["evidence"]) + "</ul>")
    checks = inc.get("checks") or {}
    if checks:
        out.append("<p><b>Checklist</b></p><ul>" + "".join(
            f"<li>{e(labels.get(k, k))}: {e(STATUS_TEXT.get(v, v))}{e(_mesh_note(inc, k))}</li>"
            for k, v in checks.items()) + "</ul>")
    if inc.get("steps"):
        out.append("<p><b>What to do</b></p><ol>" + "".join(f"<li>{e(s)}</li>" for s in inc["steps"]) + "</ol>")
    if inc.get("log_lines"):
        out.append("<details><summary>Log lines from before</summary><pre>"
                   + e("\n".join(inc["log_lines"])) + "</pre></details>")
    if not all(i.get("acked") for i in incidents):
        out.append(f'<form method="post" action="{e(action)}"><input type="hidden" name="nonce" value="{e(nonce)}">'
                   '<button class="a" name="choice" value="ack">Got it</button></form>')
    if len(incidents) > 1:
        out.append('<p class="m">Earlier:</p><ul class="m">' + "".join(
            f"<li>{e(_when(i['start']))}: {e(i['cause'])} "
            f"({max(1, int(round((i['end'] - i['start']) / 60)))} min)"
            + (f" {e(i['tag'])}" if i.get("tag") else "") + "</li>" for i in incidents[1:]) + "</ul>")
    out.append(render_ledger(ledger))
    return "".join(out)


def render(pending: Pending | None, last: str, action: str, note: str = "", extra: str = "") -> str:
    if pending is None or pending.decision is not None:
        body = "<h1>House Brain Maintenance</h1><p>Nothing is waiting for your approval.</p>"
        if note:
            body += f"<p><b>{html.escape(note)}</b></p>"
        if last:
            body += f'<p class="m">Last: {html.escape(last)}</p>'
        return PAGE.format(body=body + extra)
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
    return PAGE.format(body=body + extra)


def make_handler(board: ApprovalBoard, allowed_peer: str, recovery=None, connector=None):
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
            # R2: no page here posts to github.com any more (Connect opens the hand-off address in a new window).
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
            self.end_headers()
            self.wfile.write(data)

        def _peer_ok(self) -> bool:
            return self.client_address[0] == allowed_peer

        def _base(self) -> str:
            base = self.headers.get("X-Ingress-Path", "")
            return base if RE_INGRESS_PATH.fullmatch(base) else ""

        def _action(self) -> str:
            return f"{self._base()}/decide"

        def _extra(self) -> str:
            if recovery is None:
                return ""
            from .recovery import CHECK_LABELS
            incidents, nonce = recovery.snapshot()
            ledger = recovery.ledger_view() if hasattr(recovery, "ledger_view") else None
            return render_recovery(incidents, nonce, f"{self._base()}/recovery/ack", CHECK_LABELS, ledger)

        def _github_line(self, base: str) -> str:
            if connector is None:
                return ""
            return (f'<p class="m"><a href="{html.escape(base)}/github">GitHub connection</a>: '
                    f"{html.escape(connector.status_line())}</p>")

        def _github_page(self, status: int, note: str = "") -> None:
            body = connector.render(self.headers.get("X-Remote-User-Id", ""), self.headers.get("X-Ingress-Path", ""),
                                    self.headers.get("X-Forwarded-Host", ""),
                                    self.headers.get("X-Forwarded-Proto", ""), note)
            self._send(status, PAGE.format(body=body))

        def _github_get(self, route: str) -> bool:
            """GET /github (Credential Autopilot). R2: GitHub returns to the hand-off address, not here."""
            if connector is None or route != "/github":
                return False
            self._github_page(200)
            return True

        def _github_post(self, route: str) -> bool:
            if connector is None or route not in ("/github/upload", "/github/forget"):
                return False
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                length = -1
            limit = ghpage.MAX_UPLOAD if route == "/github/upload" else MAX_BODY
            if not 0 < length <= limit:
                self._send(413, "bad request size")
                return True
            raw = self.rfile.read(length)
            user = self.headers.get("X-Remote-User-Id", "")
            if route == "/github/upload":
                status, note = connector.upload(self.headers.get("Content-Type") or "", raw, user,
                                                lookup_bearer=connector.auth.pat)
            else:
                if not (self.headers.get("Content-Type") or "").startswith("application/x-www-form-urlencoded"):
                    self._send(415, "unsupported")
                    return True
                status, note = connector.forget(urllib.parse.parse_qs(raw.decode("utf-8", "replace")), user)
            self._github_page(status, note)
            return True

        def do_GET(self):  # noqa: N802
            if not self._peer_ok():
                return self._send(403, "forbidden")
            route = urllib.parse.urlsplit(self.path).path
            if self._github_get(route):
                return None
            if route not in ("/", ""):
                return self._send(404, "not found")
            pending, last = board.snapshot()
            self._send(200, render(pending, last, self._action(), extra=self._extra() + self._github_line(self._base())))

        def do_POST(self):  # noqa: N802
            if not self._peer_ok():
                return self._send(403, "forbidden")
            route = urllib.parse.urlsplit(self.path).path
            if self._github_post(route):
                return None
            if route not in ("/decide", "/recovery/ack") or (route == "/recovery/ack" and recovery is None):
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
            if route == "/recovery/ack":
                status = recovery.request_ack(nonce, user) if choice == "ack" else 400
                pending, last = board.snapshot()
                ack_notes = {200: "Marked as read.", 403: "Only the owner account can do this.",
                             409: "This button has expired; reload the page.", 400: "Unknown choice."}
                return self._send(status, render(pending, last, self._action(), ack_notes.get(status, ""),
                                                 extra=self._extra()))
            status = board.decide(nonce, user, choice)
            pending, last = board.snapshot()
            notes = {200: f"Recorded: {choice}. You can close this page.",
                     403: "Only the owner account can decide.", 409: "This request is no longer waiting.",
                     410: "This request expired (counted as Reject).", 400: "Unknown choice."}
            self._send(200 if status == 200 else status,
                       render(None if status == 200 else pending, last, self._action(), notes.get(status, ""),
                              extra=self._extra()))

        def do_PUT(self):  # noqa: N802
            self._send(405, "method not allowed")

        do_DELETE = do_PATCH = do_PUT

    return Handler


class IngressServer:
    def __init__(self, board: ApprovalBoard, host: str = "0.0.0.0", port: int = 8099,  # noqa: S104 - ingress
                 allowed_peer: str = INGRESS_PEER, recovery=None, connector=None) -> None:
        self.httpd = ThreadingHTTPServer((host, port), make_handler(board, allowed_peer, recovery, connector))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
