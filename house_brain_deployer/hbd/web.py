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
import secrets
import threading
import time
import urllib.parse
from collections.abc import Callable
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
        self._health: tuple[str, list[str], str] = ("", [], "")
        # "Allow more today" (0.3.2): offered only while a request is held by the daily limit.
        self._lift_nonce = ""
        self._lift_owner = ""
        self._lift_by: str | None = None
        self._lifted_note = ""
        # Owner "Undo" (0.3.3)
        self._undo_nonce = ""
        self._undo_owner = ""
        self._undo_offers: list[dict] = []
        self._undo_req: str | None = None
        # 0.3.5 (P4): True while an install transaction is open (set by the service)
        self.busy_probe: Callable[[], bool] | None = None

    def busy(self) -> bool:
        try:
            return bool(self.busy_probe and self.busy_probe())
        except Exception:  # noqa: BLE001 - an unreadable journal shows the warning (safer)
            return True

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

    def set_health(self, last_check: str, reasons: list[str], last_result: str) -> None:
        """Why nothing is waiting (0.3.1): shown whenever there is no pending approval."""
        with self._lock:
            self._health = (last_check, list(reasons)[:20], last_result)

    def health(self) -> tuple[str, list[str], str]:
        with self._lock:
            return self._health

    def offer_lift(self, owner_user_id: str) -> None:
        with self._lock:
            if not self._lift_nonce or self._lift_owner != owner_user_id:
                self._lift_nonce = secrets.token_hex(12)
                self._lift_owner = owner_user_id

    def withdraw_lift(self, note: str = "") -> None:
        with self._lock:
            self._lift_nonce = ""
            self._lifted_note = note

    def lift_state(self) -> tuple[str, str]:
        with self._lock:
            return self._lift_nonce, self._lifted_note

    def accept_lift(self, nonce: str, user_id: str) -> int:
        """Owner tapped "Allow more today". Same rules as a decision: owner id + single-use code."""
        with self._lock:
            if not self._lift_nonce or not nonce or nonce != self._lift_nonce:
                return 409
            if not self._lift_owner or user_id != self._lift_owner:
                return 403
            self._lift_nonce = ""
            self._lift_by = user_id
            return 200

    def set_undo_offers(self, owner_user_id: str, offers: list[dict]) -> None:
        with self._lock:
            ids = [o["request_id"] for o in offers]
            if ids != [o["request_id"] for o in self._undo_offers] or owner_user_id != self._undo_owner \
                    or not self._undo_nonce:
                self._undo_nonce = secrets.token_hex(12) if offers else ""
            self._undo_owner = owner_user_id
            self._undo_offers = list(offers)

    def undo_state(self) -> tuple[str, list[dict]]:
        with self._lock:
            return self._undo_nonce, list(self._undo_offers)

    def accept_undo(self, nonce: str, user_id: str, request_id: str) -> int:
        """Owner tapped Undo: same rules as a decision (owner id, single-use code, offered id only)."""
        with self._lock:
            if not self._undo_nonce or not nonce or nonce != self._undo_nonce:
                return 409
            if not self._undo_owner or user_id != self._undo_owner:
                return 403
            if request_id not in [o["request_id"] for o in self._undo_offers]:
                return 409
            self._undo_nonce = ""
            self._undo_offers = []
            self._undo_req = request_id
            return 200

    def take_undo(self) -> str | None:
        with self._lock:
            rid, self._undo_req = self._undo_req, None
            return rid

    def take_lift(self) -> str | None:
        """Main loop: consume an accepted lift (returns the owner's user id once)."""
        with self._lock:
            by, self._lift_by = self._lift_by, None
            return by

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
<title>House Brain Deployer</title><style>
body{{font-family:-apple-system,system-ui,sans-serif;margin:0;padding:16px;background:#111;color:#eee}}
h1{{font-size:20px}} h2{{font-size:17px;margin-top:22px}} li{{margin:8px 0}} .w{{color:#ffb74d}} pre{{white-space:pre-wrap;background:#1d1d1d;padding:12px;border-radius:8px;font-size:14px}}
button{{width:100%;padding:18px;margin:8px 0;font-size:20px;border:0;border-radius:12px}}
.a{{background:#2e7d32;color:#fff}} .r{{background:#8e2424;color:#fff}} .m{{color:#aaa;font-size:14px}}
</style></head><body>{body}</body></html>"""


def expires_in(left: int) -> str:
    """0.3.4: holds can last hours, so show "5 h 59 min" rather than "359 min 12 s"."""
    if left >= 3600:
        return f"{left // 3600} h {left % 3600 // 60} min"
    return f"{left // 60} min {left % 60} s"


BUSY_BANNER = ('<p class="w"><b>Install in progress.</b> Updating, restarting or reconfiguring the House Brain '
               'Deployer now will roll this install back. Wait until it has finished.</p>')


def render(pending: Pending | None, last: str, action: str, note: str = "",
           health: tuple[str, list[str], str] = ("", [], ""), lift: tuple[str, str] = ("", ""),
           undo_offer: tuple[str, list[dict]] = ("", []), busy: bool = False) -> str:
    banner = BUSY_BANNER if busy else ""
    if pending is None or pending.decision is not None:
        body = banner + "<h1>House Brain Deployer</h1><p>Nothing is waiting for your approval.</p>"
        if note:
            body += f"<p><b>{html.escape(note)}</b></p>"
        checked, reasons, result = health
        if reasons:
            body += '<h2 class="w">Why nothing is happening</h2><ul>'
            body += "".join(f"<li>{html.escape(r)}</li>" for r in reasons) + "</ul>"
            lift_nonce, _ = lift
            if lift_nonce:
                more = action.rsplit("/", 1)[0] + "/allow-more"
                body += (f'<form method="post" action="{html.escape(more)}">'
                         f'<input type="hidden" name="nonce" value="{html.escape(lift_nonce)}">'
                         '<button class="a" name="choice" value="allow">Allow more today</button></form>'
                         '<p class="m">Lifts the daily limit until midnight. Every change still needs your '
                         'deploy and restart approvals.</p>')
        elif checked:
            body += "<p>All clear: no new request on GitHub, nothing held, no errors.</p>"
        if checked:
            body += f'<p class="m">Last check of GitHub: {html.escape(checked)}</p>'
        else:
            body += '<p class="m">The first check of GitHub has not finished yet.</p>'
        if lift[1]:
            body += f'<p class="m">{html.escape(lift[1])}</p>'
        undo_nonce, undo_items = undo_offer
        if undo_nonce and undo_items:
            target = action.rsplit("/", 1)[0] + "/undo"
            body += ('<h2>Recent installs</h2><p class="m">Undo puts back the files that install replaced '
                     '(your tap approves it; the restart still asks you). The undone version is kept as a .bak.</p>')
            for item in undo_items:
                when = time.strftime("%b %d %I:%M %p", time.localtime(float(item.get("at") or 0)))
                body += (f'<form method="post" action="{html.escape(target)}">'
                         f'<input type="hidden" name="nonce" value="{html.escape(undo_nonce)}">'
                         f'<input type="hidden" name="request_id" value="{html.escape(item["request_id"])}">'
                         f'<p>{html.escape(item.get("title") or item["request_id"])}<br>'
                         f'<span class="m">{html.escape(item["request_id"])} · {html.escape(when)}</span></p>'
                         '<button class="r" name="choice" value="undo">Undo this install</button></form>')
        if result:
            body += f'<p class="m">Last result: {html.escape(result)}</p>'
        if last:
            body += f'<p class="m">Last: {html.escape(last)}</p>'
        return PAGE.format(body=body)
    left = max(0, int(pending.deadline - time.monotonic()))
    body = (
        banner +
        f"<h1>{html.escape(pending.title)}</h1>"
        f"<pre>{html.escape(pending.message)}</pre>"
        f'<p class="m">Expires in {expires_in(left)}. No answer = Reject.</p>'
        f'<form method="post" action="{html.escape(action)}">'
        f'<input type="hidden" name="nonce" value="{html.escape(pending.nonce)}">'
        '<button class="a" name="choice" value="approve">Approve</button>'
        '<button class="r" name="choice" value="reject">Reject</button></form>'
    )
    return PAGE.format(body=body)


def make_handler(board: ApprovalBoard, allowed_peer: str, connector=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "hbd"
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

        def _action(self) -> str:
            base = self.headers.get("X-Ingress-Path", "")
            if not RE_INGRESS_PATH.fullmatch(base):
                base = ""
            return f"{base}/decide"

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
            page = render(pending, last, self._action(), health=board.health(), lift=board.lift_state(),
                          undo_offer=board.undo_state(), busy=board.busy())
            base = self._action().rsplit("/", 1)[0]
            self._send(200, page.replace("</body>", self._github_line(base) + "</body>", 1))

        def do_POST(self):  # noqa: N802
            if not self._peer_ok():
                return self._send(403, "forbidden")
            route = urllib.parse.urlsplit(self.path).path
            if self._github_post(route):
                return None
            if route not in ("/decide", "/allow-more", "/undo"):
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
            if route == "/undo":
                rid = (form.get("request_id") or [""])[0]
                status = board.accept_undo(nonce, user, rid) if choice == "undo" else 400
                pending, last = board.snapshot()
                notes = {200: f"Undo of {rid} accepted. The Deployer prepares it within about 5 minutes, then asks "
                              "you to approve the restart.",
                         403: "Only the owner account can undo an install.", 409: "This Undo button has expired; "
                         "reload the page.", 400: "Unknown choice."}
                return self._send(status, render(pending, last, self._action(), notes.get(status, ""),
                                                 health=board.health(), lift=board.lift_state(),
                                                 undo_offer=board.undo_state(), busy=board.busy()))
            if route == "/allow-more":
                status = board.accept_lift(nonce, user) if choice == "allow" else 400
                pending, last = board.snapshot()
                notes = {200: "Done: the daily limit is lifted until midnight. The waiting request will be "
                              "asked within about 5 minutes.",
                         403: "Only the owner account can lift the limit.", 409: "This button has expired; reload.",
                         400: "Unknown choice."}
                return self._send(status, render(pending, last, self._action(), notes.get(status, ""),
                                                 health=board.health(), lift=board.lift_state(),
                                                 undo_offer=board.undo_state(), busy=board.busy()))
            status = board.decide(nonce, user, choice)
            pending, last = board.snapshot()
            notes = {200: f"Recorded: {choice}. You can close this page.",
                     403: "Only the owner account can decide.", 409: "This request is no longer waiting.",
                     410: "This request expired (counted as Reject).", 400: "Unknown choice."}
            self._send(200 if status == 200 else status,
                       render(None if status == 200 else pending, last, self._action(), notes.get(status, ""),
                              health=board.health(), lift=board.lift_state(), undo_offer=board.undo_state(),
                              busy=board.busy()))

        def do_PUT(self):  # noqa: N802
            self._send(405, "method not allowed")

        do_DELETE = do_PATCH = do_PUT

    return Handler


class IngressServer:
    def __init__(self, board: ApprovalBoard, host: str = "0.0.0.0", port: int = 8099,  # noqa: S104 - ingress
                 allowed_peer: str = INGRESS_PEER, connector=None) -> None:
        self.httpd = ThreadingHTTPServer((host, port), make_handler(board, allowed_peer, connector))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
