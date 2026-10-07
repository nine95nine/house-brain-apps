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
from typing import Any

from . import ghauth

MAX_UPLOAD = 16 * 1024
RE_HOST = re.compile(r"[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?")
RE_INGRESS_BASE = re.compile(r"/api/hassio_ingress/[A-Za-z0-9_\-]{1,200}")
RE_APP_ID = re.compile(r"[0-9]{1,12}")
PAT_SETTINGS_LINK = "https://github.com/settings/personal-access-tokens"
APPS_LINK = "https://github.com/settings/apps"


class Connector:
    def __init__(self, *, app_title: str, bot_name: str, bot_description: str, repo: str,
                 permissions: dict[str, str], api: str, user_agent: str, store: ghauth.KeyStore,
                 auth: ghauth.Auth, owner_id: Callable[[], str], clock=time.time, request=None) -> None:
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
        if meta and not s["app_installed"] and meta.get("slug"):
            slug = urllib.parse.quote(str(meta["slug"]), safe="-")
            out.append("<h2>Next: install it on the repository</h2>"
                       f'<p><a href="https://github.com/apps/{slug}/installations/new" target="_blank" '
                       'rel="noopener">Open the install page on GitHub</a>, choose <b>Only select repositories</b>, '
                       f"pick <b>{html.escape(self.repo.split('/', 1)[1])}</b> and tap <b>Install</b>. This page "
                       "notices it within one check.</p>")
        if meta and s["using"] == ghauth.APP_ONLY and not s["app_error"] and s["pat_set"]:
            out.append("<h2>The old token is no longer needed</h2>"
                       f'<p>Delete it on GitHub: <a href="{PAT_SETTINGS_LINK}" target="_blank" rel="noopener">'
                       "Fine-grained tokens</a> → the token → <b>Delete</b>. Nothing else to do: this App ignores "
                       "it while the GitHub App works, and a deleted token can never leak.</p>")
        if not meta:
            origin = self.browser_origin(host, proto)
            if origin and base:
                state = self.states.issue(user_id)
                back = f"{origin}{base}/github/callback"
                doc = ghauth.manifest(self.bot_name, self.repo, back, back, self.permissions, self.bot_description)
                out.append("<h2>Connect to GitHub</h2>"
                           "<p>Creates a private GitHub App that can only read this repository's files and "
                           "comment on its issues. Its key never expires, and this App keeps it to itself.</p>"
                           f'<form method="post" action="{html.escape(ghauth.manifest_action(self.repo_owner, state))}">'
                           f'<input type="hidden" name="manifest" value="{html.escape(json.dumps(doc))}">'
                           '<button class="a">Connect to GitHub</button></form>'
                           f'<p class="m">GitHub will send you back to {html.escape(origin)}. If it cannot (some '
                           "phones open GitHub outside the Home Assistant app), use Upload key file below.</p>")
            else:
                out.append('<p class="w">The page address is unknown, so the Connect button is not offered. '
                           "Use Upload key file below.</p>")
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

    def _when(self, epoch) -> str:
        if not epoch:
            return "none yet"
        return time.strftime("%b %d %I:%M %p", time.localtime(float(epoch)))

    # -- actions ----------------------------------------------------------------------------
    def _forget_cached_token(self) -> None:
        if self.auth.provider is not None:
            self.auth.provider.invalidate()

    def callback(self, query: str, user_id: str) -> tuple[int, str]:
        """GitHub sent the browser back: exchange the one-time code for the key."""
        params = urllib.parse.parse_qs(query or "", max_num_fields=8)
        code = (params.get("code") or [""])[0]
        state = (params.get("state") or [""])[0]
        taken = self.states.take(state, user_id)       # any attempt uses the value up, even a refused one
        if not taken or not self._is_owner(user_id):
            return 403, "This link is not valid any more (or not for this account). Start again with Connect."
        if self.store.present():
            return 409, "A GitHub App is already connected. Disconnect first to connect another."
        try:
            got = ghauth.convert(self.api, code, self.user_agent, self.repo_owner, self.permissions,
                                 request=self.request)
            meta = self.store.save(got["pem"], app_id=got["app_id"], client_id=got["client_id"], slug=got["slug"],
                                   owner=got["owner"], how="manifest", now=self.clock())
        except ghauth.GitHubAppError as err:
            return 400, f"Not connected: {err.code}. Try again, or use Upload key file."
        self._forget_cached_token()
        return 200, (f"Connected: {meta['slug'] or meta['app_id']}. Next: install it on the repository "
                     "(button below).")

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


__all__ = ["Connector", "parse_multipart", "no_secret_in"]
