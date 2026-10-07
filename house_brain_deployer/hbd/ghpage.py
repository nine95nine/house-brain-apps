"""The "GitHub connection" page of the App (Credential Autopilot R1, owner decision 2026-10-06).

Served only through Home Assistant ingress (the App's web server already refuses every other peer).
Every change needs the owner's Home Assistant account (``X-Remote-User-Id``, inserted by the Supervisor)
and a single-use value from the page, like the approval buttons:

* **Connect to GitHub**: a form that posts a fixed GitHub App *manifest* to github.com. The owner taps
  Create there; GitHub sends the browser back to this page with a one-time ``code`` (valid one hour),
  and the App exchanges it for the App id and private key itself. The owner never sees the key.
* **Upload key file** (fallback, e.g. when GitHub cannot return to this page on the phone): the ``.pem``
  file that GitHub's "Generate a private key" downloads.
* **Disconnect**: deletes the key from this App (the GitHub App itself is deleted on GitHub).

The page shows the App name, the key fingerprint (the same "SHA256:..." GitHub shows) and the status.
It never shows, logs or echoes a key, a token or the manifest code. Byte-identical in hbm/ and hbd/.

R2 (owner 2026-10-07, "iPhone, 3 taps per App"): the page sits in Home Assistant's frame, which GitHub refuses
to load in, and the iPhone app opens GitHub in a browser without the HA login. So **Connect** opens the App's
own *hand-off address* (same host the owner already uses for Home Assistant, the App's hand-off port), which
exists only while a Connect is pending (at most one hour) and answers only three paths with a valid single-use
``state``: ``/github/start`` (posts the manifest to GitHub, top level), ``/github/callback`` (exchanges the code,
then forwards to the install page with only this repository pre-selected) and ``/github/installed``.
"""
from __future__ import annotations

import email.parser
import email.policy
import html
import json
import re
import secrets
import threading
import time
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import ghauth

MAX_UPLOAD = 16 * 1024
RE_HOST = re.compile(r"[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?")
RE_INGRESS_BASE = re.compile(r"/api/hassio_ingress/[A-Za-z0-9_\-]{1,200}")
RE_APP_ID = re.compile(r"[0-9]{1,12}")
PAT_SETTINGS_LINK = "https://github.com/settings/personal-access-tokens"
APPS_LINK = "https://github.com/settings/apps"


OFFER_TTL = 1800                    # an offered Connect link (page opened, not yet tapped) lives 30 minutes
HANDOFF_TTL = ghauth.STATE_TTL       # after GitHub's return, the install page may come back within one hour
MAX_PENDING = 4
RE_HOSTNAME = re.compile(r"[A-Za-z0-9.-]{1,253}")


class Connector:
    def __init__(self, *, app_title: str, bot_name: str, bot_description: str, repo: str,
                 permissions: dict[str, str], api: str, user_agent: str, store: ghauth.KeyStore,
                 auth: ghauth.Auth, owner_id: Callable[[], str], clock=time.time, request=None,
                 handoff_port: int = 0, handoff_bind: str = "0.0.0.0") -> None:  # noqa: S104 - see HandoffServer
        self.app_title = app_title
        self.bot_name = bot_name
        self.bot_description = bot_description
        self.repo = repo
        self.repo_owner = repo.split("/", 1)[0]
        self.permissions = dict(permissions)
        self.api = api
        self.user_agent = user_agent
        self.store = store
        self.auth = auth
        self.owner_id = owner_id
        self.clock = clock
        self.request = request
        self.states = ghauth.ConnectStates(clock)
        self._lock = threading.Lock()
        self._nonce = ""
        self._nonce_owner = ""
        self.handoff_port = int(handoff_port or 0)
        self.handoff_bind = handoff_bind
        self._handoff: HandoffServer | None = None
        self._pending: dict[str, tuple[str, float]] = {}    # state -> (hand-off origin, expiry)
        self._installed_until = 0.0                        # /github/installed answers only after a connect

    # -- who may act ------------------------------------------------------------------------
    def _is_owner(self, user_id: str) -> bool:
        try:
            owner = self.owner_id() or ""
        except Exception:  # noqa: BLE001 - unknown owner: nobody may change the connection
            return False
        return bool(user_id) and secrets.compare_digest(user_id, owner)

    def _page_nonce(self, user_id: str) -> str:
        with self._lock:
            if not self._nonce or self._nonce_owner != user_id:
                self._nonce, self._nonce_owner = secrets.token_hex(12), user_id
            return self._nonce

    def _take_nonce(self, nonce: str, user_id: str) -> bool:
        with self._lock:
            ok = bool(self._nonce) and secrets.compare_digest(nonce or "", self._nonce) \
                and self._nonce_owner == user_id
            if ok:
                self._nonce = ""
            return ok

    # -- page -------------------------------------------------------------------------------
    @staticmethod
    def base(ingress_path: str) -> str:
        return ingress_path if RE_INGRESS_BASE.fullmatch(ingress_path or "") else ""

    @staticmethod
    def browser_origin(host: str, proto: str) -> str:
        """The address the owner's browser used (set by Home Assistant's ingress proxy)."""
        host = (host or "").split(",")[0].strip()
        proto = (proto or "").split(",")[0].strip().lower()
        if proto not in ("http", "https") or not RE_HOST.fullmatch(host):
            return ""
        return f"{proto}://{host}"

    def status_line(self) -> str:
        """One plain line for the App's main page."""
        s = self.auth.summary()
        if s["app_connected"] and s["using"] == ghauth.APP_ONLY and not s["app_error"]:
            return "GitHub: signed in with the GitHub App (no token to renew)."
        if s["app_connected"] and s["app_error"]:
            fallback = " The old token is standing in." if s["using"] == ghauth.PAT_ONLY else ""
            return f"GitHub: the GitHub App cannot sign in ({s['app_error']}).{fallback} Open GitHub connection."
        if s["app_connected"]:
            return "GitHub: GitHub App connected; the first sign-in happens with the next check."
        if s["pat_set"]:
            left = s["pat_days_left"]
            when = f" It expires in {left} days." if left is not None else ""
            return f"GitHub: using a hand-made token.{when} Open GitHub connection to switch to the GitHub App."
        return "GitHub: not connected. Open GitHub connection."

    def render(self, user_id: str, ingress_path: str, host: str, proto: str, note: str = "") -> str:
        base = self.base(ingress_path)
        s = self.auth.summary()
        out = [f"<h1>{html.escape(self.app_title)}: GitHub connection</h1>"]
        if note:
            out.append(f"<p><b>{html.escape(note)}</b></p>")
        out.append(f"<p>{html.escape(self.status_line())}</p>")
        meta = self.store.meta() if self.store.present() else {}
        if meta:
            out.append('<ul>'
                       f"<li>GitHub App: <b>{html.escape(str(meta.get('slug') or meta.get('app_id')))}</b></li>"
                       f"<li>Key fingerprint: <code>{html.escape(str(meta.get('fingerprint') or ''))}</code> "
                       '<span class="m">(GitHub shows the same text next to the key)</span></li>'
                       f"<li>Installed on {html.escape(self.repo)}: "
                       f"{'yes' if s['app_installed'] else 'not yet'}</li>"
                       f"<li>Last good GitHub answer: {self._when(s['last_ok'])}</li></ul>")
        owner = self._is_owner(user_id)
        if not owner:
            out.append('<p class="m">Only the owner account can change the GitHub connection.</p>')
            return "".join(out)
        nonce = self._page_nonce(user_id)
        direct = ghauth.install_url(str(meta.get("slug") or ""), meta.get("owner_id"), meta.get("repo_id")) \
            if meta else None
        if meta and not s["app_installed"] and direct:
            out.append("<h2>Next: install it on the repository</h2>"
                       f'<p><a class="a" href="{html.escape(direct)}" target="_blank" rel="noopener">Install on '
                       f"{html.escape(self.repo.split('/', 1)[1])}</a> (only this repository is selected; tap "
                       "<b>Install</b> on GitHub). This page notices it within one check.</p>")
        elif meta and not s["app_installed"] and meta.get("slug"):
            slug = urllib.parse.quote(str(meta["slug"]), safe="-")
            out.append("<h2>Next: install it on the repository</h2>"
                       f'<p><a href="https://github.com/apps/{slug}/installations/new" target="_blank" '
                       'rel="noopener">Open the install page on GitHub</a>, choose <b>Only select repositories</b>, '
                       f"pick <b>{html.escape(self.repo.split('/', 1)[1])}</b> and tap <b>Install</b>. This page "
                       "notices it within one check.</p>")
        if s.get("pat_retired"):
            out.append("<h2>Old token retired</h2>"
                       f"<p>This App revoked its old hand-made token on GitHub ({html.escape(str(s['pat_retired']))}); "
                       "GitHub e-mailed you about it. Nothing to do. Clearing the old value in Configuration is "
                       "optional.</p>")
        elif meta and s["using"] == ghauth.APP_ONLY and not s["app_error"] and s["pat_set"]:
            when = s.get("retire_after")
            plan = (f"This App retires it by itself after 24 hours of GitHub App sign-ins (about {html.escape(when)} "
                    "UTC); GitHub will e-mail you when it does." if when and self.auth.retire else
                    f'Delete it on GitHub: <a href="{PAT_SETTINGS_LINK}" target="_blank" rel="noopener">'
                    "Fine-grained tokens</a> → the token → <b>Delete</b>.")
            out.append("<h2>The old token is no longer needed</h2>"
                       f"<p>{plan} It is used by this App only; this App ignores it while the GitHub App works.</p>")
        if not meta:
            link = self.connect_link(user_id, host)
            if link:
                out.append("<h2>Connect to GitHub</h2>"
                           "<p>Creates a private GitHub App that can only read this repository's files and "
                           "comment on its issues. Its key never expires, and this App keeps it to itself.</p>"
                           f'<p><a class="a" href="{html.escape(link)}" target="_blank" rel="noopener">'
                           "Connect to GitHub</a></p>"
                           '<p class="m">Then on GitHub: tap <b>Create GitHub App</b>, then <b>Install</b> (this '
                           "repository is already selected). Works at home or over Tailscale; the link is valid for "
                           "one hour. If it does not open, use Upload key file below.</p>")
            else:
                out.append('<p class="w">The Connect button needs this App\'s hand-off port (Network section of the '
                           "App) and the page address. Use Upload key file below.</p>")
        out.append("<h2>Upload key file</h2>"
                   f'<p class="m">On GitHub: <a href="{APPS_LINK}" target="_blank" rel="noopener">Developer settings › '
                   "GitHub Apps</a> → the App → <b>Generate a private key</b>. Then pick the downloaded "
                   "<code>.pem</code> file here. The App ID is the number at the top of that GitHub page "
                   "(only needed when this App cannot look it up).</p>"
                   f'<form method="post" enctype="multipart/form-data" action="{html.escape(base)}/github/upload">'
                   f'<input type="hidden" name="nonce" value="{html.escape(nonce)}">'
                   '<p><input type="file" name="key" accept=".pem"></p>'
                   '<p><input type="text" name="app_id" inputmode="numeric" placeholder="App ID (optional)"></p>'
                   '<button class="a">Upload key file</button></form>')
        if meta:
            out.append("<h2>Disconnect</h2>"
                       '<p class="m">Deletes the key from this App only. To stop the GitHub App everywhere, also '
                       f'delete it on GitHub (<a href="{APPS_LINK}" target="_blank" rel="noopener">GitHub Apps</a> → '
                       "the App → Advanced → Delete).</p>"
                       f'<form method="post" action="{html.escape(base)}/github/forget">'
                       f'<input type="hidden" name="nonce" value="{html.escape(nonce)}">'
                       '<button class="r" name="choice" value="forget">Disconnect</button></form>')
        return "".join(out)

    # -- R2: the hand-off address ------------------------------------------------------------------
    @staticmethod
    def handoff_host(host: str) -> str:
        """The bare host name the owner's browser uses for Home Assistant (no port, no brackets)."""
        host = (host or "").split(",")[0].strip()
        if not RE_HOST.fullmatch(host):
            return ""
        name = host.rsplit(":", 1)[0] if ":" in host else host
        return name if RE_HOSTNAME.fullmatch(name) else ""

    def connect_link(self, user_id: str, host: str) -> str:
        """Issue a ``state`` and open the hand-off address for it. "" when it cannot be offered."""
        name = self.handoff_host(host)
        if not (self.handoff_port and name and self._is_owner(user_id)):
            return ""
        origin = f"http://{name}:{self.handoff_port}"
        state = self.states.issue(user_id)
        with self._lock:
            now = self.clock()
            self._pending = {k: v for k, v in self._pending.items() if v[1] > now}
            while len(self._pending) >= MAX_PENDING:
                del self._pending[min(self._pending, key=lambda k: self._pending[k][1])]
            self._pending[state] = (origin, now + OFFER_TTL)
        if not self._ensure_handoff():
            return ""
        return f"{origin}/github/start?" + urllib.parse.urlencode({"state": state})

    def _ensure_handoff(self) -> bool:
        with self._lock:
            if self._handoff is not None:
                return True
            try:
                self._handoff = HandoffServer(self, self.handoff_port, self.handoff_bind)
            except OSError:
                return False
        self._handoff.start()
        return True

    def handoff_open(self) -> bool:
        """True while a Connect is pending or the install page may still come back."""
        now = self.clock()
        with self._lock:
            return any(v[1] > now for v in self._pending.values()) or now < self._installed_until

    def close_handoff_if_idle(self) -> None:
        if self._handoff is not None and not self.handoff_open():
            server, self._handoff = self._handoff, None
            server.stop()

    def handoff_start(self, query: str) -> tuple[int, str, str]:
        """GET /github/start: the page that posts the manifest to GitHub (top level, no frame)."""
        state = (urllib.parse.parse_qs(query or "", max_num_fields=4).get("state") or [""])[0]
        with self._lock:
            item = self._pending.get(state) if ghauth.RE_STATE.fullmatch(state or "") else None
        if not item or item[1] <= self.clock():
            return 404, "", ""
        origin = item[0]
        with self._lock:                                   # tapped: GitHub's return may take up to the code's hour
            if state in self._pending:
                self._pending[state] = (origin, self.clock() + HANDOFF_TTL)
        doc = ghauth.manifest(self.bot_name, self.repo, f"{origin}/github/callback", f"{origin}/github/installed",
                              self.permissions, self.bot_description)
        nonce = secrets.token_hex(12)
        body = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                f"<title>{html.escape(self.app_title)}: Connect to GitHub</title>"
                f'<form id="f" method="post" action="{html.escape(ghauth.manifest_action(self.repo_owner, state))}">'
                f'<input type="hidden" name="manifest" value="{html.escape(json.dumps(doc))}">'
                f"<p>Opening GitHub for {html.escape(self.app_title)}…</p>"
                "<button>Continue to GitHub</button></form>"
                f'<script nonce="{nonce}">document.getElementById("f").submit()</script>')
        return 200, body, nonce

    def handoff_callback(self, query: str) -> tuple[int, str, str]:
        """GET /github/callback: GitHub's return. Returns (status, page text, forward URL or "")."""
        params = urllib.parse.parse_qs(query or "", max_num_fields=8)
        state = (params.get("state") or [""])[0]
        code = (params.get("code") or [""])[0]
        with self._lock:
            item = self._pending.pop(state, None) if ghauth.RE_STATE.fullmatch(state or "") else None
        user_id = self.states.take_any(state)          # single use: any attempt uses it up
        if not item or item[1] <= self.clock() or not user_id or not self._is_owner(user_id):
            return 404, "", ""
        if self.store.present():
            return 409, "A GitHub App is already connected. Disconnect first to connect another.", ""
        try:
            got = ghauth.convert(self.api, code, self.user_agent, self.repo_owner, self.permissions,
                                 request=self.request)
            meta = self.store.save(got["pem"], app_id=got["app_id"], client_id=got["client_id"], slug=got["slug"],
                                   owner=got["owner"], how="manifest", now=self.clock())
        except ghauth.GitHubAppError as err:
            return 400, f"Not connected: {err.code}. Go back to Home Assistant and try again, or use Upload key file.", ""
        self._forget_cached_token()
        rid = self._repo_id()
        self.store.update_meta(owner_id=got.get("owner_id"), repo_id=rid)
        with self._lock:
            self._installed_until = self.clock() + HANDOFF_TTL
        forward = ghauth.install_url(meta["slug"], got.get("owner_id"), rid)
        if forward:
            return 303, "Connected. Opening the install page…", forward
        return 200, (f"Connected: {meta['slug'] or meta['app_id']}. Now install it: go back to Home Assistant, "
                     "GitHub connection, and follow 'install it on the repository'."), ""

    def _repo_id(self) -> int | None:
        """The repository id, read with the credential the App has now (the old token while the new App is not
        installed yet). None = no pre-selecting link."""
        try:
            bearer, _ = self.auth.bearer()
        except ghauth.GitHubAppError:
            return None
        return ghauth.repo_id(self.api, self.repo, bearer, self.user_agent, request=self.request)

    def handoff_installed(self) -> tuple[int, str]:
        if self.clock() >= self._installed_until:
            return 404, ""
        return 200, (f"Done: {self.app_title} is connected to GitHub and installed. Go back to Home Assistant; the "
                     "GitHub connection page shows it within one check.")

    def _when(self, epoch) -> str:
        if not epoch:
            return "none yet"
        return time.strftime("%b %d %I:%M %p", time.localtime(float(epoch)))

    # -- actions ----------------------------------------------------------------------------
    def _forget_cached_token(self) -> None:
        if self.auth.provider is not None:
            self.auth.provider.invalidate()

    def upload(self, content_type: str, body: bytes, user_id: str, lookup_bearer: str = "") -> tuple[int, str]:
        fields = parse_multipart(content_type, body)
        if fields is None:
            return 400, "The upload could not be read. Pick the .pem file again."
        if not self._is_owner(user_id):
            return 403, "Only the owner account can change the GitHub connection."
        if not self._take_nonce(fields.get("nonce", b"").decode("ascii", "replace"), user_id):
            return 409, "This form has expired; reload the page."
        if self.store.present():
            return 409, "A GitHub App is already connected. Disconnect first to replace its key."
        pem = fields.get("key", b"")
        app_id_text = fields.get("app_id", b"").decode("ascii", "replace").strip()
        try:
            ghauth.load_key(pem)
            if app_id_text:
                if not RE_APP_ID.fullmatch(app_id_text):
                    return 400, "The App ID is the number at the top of the GitHub App page (digits only)."
                found: dict[str, Any] = {"app_id": int(app_id_text), "client_id": "", "slug": "",
                                         "owner": self.repo_owner}
            elif lookup_bearer:
                slug = re.sub(r"[^a-z0-9]+", "-", self.bot_name.lower()).strip("-")
                found = ghauth.lookup_app(self.api, slug, lookup_bearer, self.user_agent, request=self.request)
            else:
                return 400, "Type the App ID (the number at the top of the GitHub App page) and upload again."
            meta = self.store.save(pem, app_id=int(found["app_id"]), client_id=str(found["client_id"]),
                                   slug=str(found["slug"]), owner=str(found["owner"]), how="upload", now=self.clock())
        except ghauth.GitHubAppError as err:
            return 400, f"Not connected: {err.code}. Check the file and the App ID."
        self._forget_cached_token()
        return 200, f"Key saved (fingerprint {meta['fingerprint']}). The next check signs in with it."

    def forget(self, form: dict, user_id: str) -> tuple[int, str]:
        if not self._is_owner(user_id):
            return 403, "Only the owner account can change the GitHub connection."
        if (form.get("choice") or [""])[0] != "forget" or not self._take_nonce((form.get("nonce") or [""])[0],
                                                                              user_id):
            return 409, "This form has expired; reload the page."
        self.store.forget()
        self._forget_cached_token()
        return 200, "Disconnected: the key was deleted from this App. Delete the GitHub App on GitHub too."


class HandoffServer:
    """The hand-off address (R2). Open only while ``connector.handoff_open()``; three GET paths; nothing else.

    It binds on all interfaces of the App's container because Home Assistant maps it to the host port shown in
    the App's Network section; the owner's browser reaches it at the same host it uses for Home Assistant.
    """

    def __init__(self, connector: Connector, port: int, bind: str) -> None:
        handler = _handoff_handler(connector)
        self.httpd = ThreadingHTTPServer((bind, port), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.connector = connector
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="github-handoff", daemon=True)
        self.watch = threading.Thread(target=self._watch, name="github-handoff-close", daemon=True)

    def start(self) -> None:
        self.thread.start()
        self.watch.start()

    def _watch(self) -> None:
        while not self._stop.wait(5):
            if not self.connector.handoff_open():
                self.connector.close_handoff_if_idle()
                return

    def stop(self) -> None:
        self._stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()


HANDOFF_PAGE = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                "<title>GitHub connection</title><p>{text}</p>")


def _handoff_handler(connector: Connector):
    class Handler(BaseHTTPRequestHandler):
        server_version = "hb-handoff"
        sys_version = ""

        def log_message(self, *args):  # noqa: D401 - never log (the query carries the one-time code)
            pass

        def _reply(self, status: int, text: str = "", location: str = "", nonce: str = "", raw: bool = False) -> None:
            body = (text if raw else HANDOFF_PAGE.format(text=html.escape(text))).encode("utf-8") \
                if status != 404 else b"not found"
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8" if status != 404 else "text/plain")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            script = f"'nonce-{nonce}'" if nonce else "'none'"
            self.send_header("Content-Security-Policy", f"default-src 'none'; script-src {script}; "
                                                        "form-action https://github.com; frame-ancestors 'none'")
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            path, _, query = self.path.partition("?")
            if not connector.handoff_open() or len(self.path) > 2048:
                return self._reply(404)
            if path == "/github/start":
                status, page, nonce = connector.handoff_start(query)
                return self._reply(status, page, nonce=nonce, raw=True)
            if path == "/github/callback":
                status, text, forward = connector.handoff_callback(query)
                return self._reply(status, text, location=forward)
            if path == "/github/installed":
                status, text = connector.handoff_installed()
                return self._reply(status, text)
            return self._reply(404)

        def do_POST(self):  # noqa: N802
            self._reply(404)

        do_PUT = do_DELETE = do_HEAD = do_PATCH = do_OPTIONS = do_POST

    return Handler


def parse_multipart(content_type: str, body: bytes) -> dict[str, bytes] | None:
    """Tiny multipart/form-data reader for the upload form (fields: nonce, key, app_id). None = refused."""
    if not (content_type or "").lower().startswith("multipart/form-data") or not 0 < len(body) <= MAX_UPLOAD:
        return None
    try:
        msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            b"Content-Type: " + content_type.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
    except Exception:  # noqa: BLE001 - malformed upload
        return None
    if not msg.is_multipart():
        return None
    out: dict[str, bytes] = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name not in ("nonce", "key", "app_id") or name in out:
            continue
        payload = part.get_payload(decode=True)
        out[name] = payload if isinstance(payload, bytes) else b""
    return out


def no_secret_in(text: str) -> bool:
    """Defence in depth for tests and the page: nothing that looks like a key or a token."""
    return "PRIVATE KEY" not in text and not re.search(r"ghs_[A-Za-z0-9._-]{20,}|github_pat_[A-Za-z0-9_]{20,}", text)


__all__ = ["Connector", "HandoffServer", "parse_multipart", "no_secret_in"]
