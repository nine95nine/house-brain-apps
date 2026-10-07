"""GitHub sign-in without hand-made tokens (Credential Autopilot R1, owner decision 2026-10-06).

A private GitHub App's private key never expires. This module keeps that key only in the App's own
``/data/github_app/`` folder (0600; never in the App options, because every manager-role App can read
every App's options: finding F2), signs a short JWT with it and exchanges the JWT for a one-hour
installation token limited to ONE repository and the minimal permissions. Tokens are reused until two
minutes before they expire, so a running App mints about 24 a day; a hard daily cap fails closed.

Owner setup never needs the Configuration tab or a copy-paste: the App's own page posts a GitHub App
*manifest* (the permissions are fixed here), GitHub returns a one-time ``code`` to the page, and the App
exchanges it for the key itself (``POST /app-manifests/{code}/conversions``). Fallback: upload the
``.pem`` file GitHub downloads.

R2 (owner 2026-10-07, "take it off my plate"): once the GitHub App has signed every call for 24 hours, the
App retires its own old hand-made token through GitHub's unauthenticated revoke endpoint (``POST
/credentials/revoke``), so the owner never deletes it by hand. Only a fingerprint of the token is kept.

This file is byte-identical in ``hbm/`` and ``hbd/`` (pinned by a test); it knows no App-specific name.
Nothing here ever logs, returns or stores a token or the key outside the key file.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from datetime import UTC, datetime

from . import net

# The Pi has no clock battery: until NTP has set the time the clock can read 1970 or an old date, and
# GitHub refuses a JWT whose times are wrong. Never sign before the clock is past this build's floor.
CLOCK_FLOOR = 1790812800            # 2026-10-01T00:00:00Z
JWT_BACKDATE = 60                   # GitHub: iat 60 s in the past allows for clock drift
JWT_LIFETIME = 540                  # GitHub allows at most 600 s
REFRESH_MARGIN = 120                # mint again 2 minutes before expiry: with 5-minute polls ~24 a day (a 5-minute
                                    # margin measured 27 a day); a token that dies mid-call gets one 401 retry
MAX_MINTS_PER_DAY = 30              # expected <= 24 (one per hour); above this fail closed
FAILURE_BACKOFF = 300               # after a failed sign-in, repeat its reason for 5 minutes without calling GitHub
NO_BACKOFF = frozenset({"GITHUB_APP_NOT_CONNECTED", "GITHUB_APP_CLOCK_NOT_SET"})   # local checks: free to retry
MIN_KEY_BITS = 2048
MAX_PEM_BYTES = 8192
DIR_NAME = "github_app"

# Installation tokens: classic 40-character ``ghs_...`` and, since GitHub's staged rollout from
# 2026-04-27, the stateless ``ghs_<appid>_<JWT>`` form (~520 characters, dots and dashes).
# GitHub's own recommended pattern is ghs_[A-Za-z0-9.\-_]{36,}; the upper bound is ours.
RE_INSTALL_TOKEN = re.compile(r"ghs_[A-Za-z0-9._-]{36,2048}")
RE_CODE = re.compile(r"[A-Za-z0-9_-]{10,128}")
RE_CLIENT_ID = re.compile(r"Iv[A-Za-z0-9._-]{4,60}")
RE_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,60}")
RE_STATE = re.compile(r"[A-Za-z0-9_-]{20,64}")
STATE_TTL = 3600                    # GitHub's manifest code is valid for one hour
RETIRE_AFTER = 24 * 3600            # R2: the GitHub App must have signed every call for this long first
RETIRE_RETRY = 24 * 3600            # a refused or failed revoke is tried again once a day, never more
RE_PAT = re.compile(r"(github_pat_[A-Za-z0-9_]{20,255}|ghp_[A-Za-z0-9]{36})")

# Reason codes (registered in contracts/house_brain_reason_code_registry.v1.json).
NOT_CONNECTED = "GITHUB_APP_NOT_CONNECTED"
NOT_INSTALLED = "GITHUB_APP_NOT_INSTALLED"
CLOCK_NOT_SET = "GITHUB_APP_CLOCK_NOT_SET"
CLOCK_SKEW = "GITHUB_APP_CLOCK_SKEW"
KEY_REJECTED = "GITHUB_APP_KEY_REJECTED"
NO_ACCESS = "GITHUB_APP_NO_ACCESS"
PERMISSIONS = "GITHUB_APP_PERMISSIONS_MISMATCH"
MINT_BUDGET = "GITHUB_APP_MINT_BUDGET"
MINT_FAILED = "GITHUB_APP_MINT_FAILED"
BAD_KEY = "GITHUB_APP_BAD_KEY"
CONNECT_FAILED = "GITHUB_APP_CONNECT_FAILED"
# The same codes as literals, for the estate reason-code registry (tools/reason_code_registry.py scans
# this assignment); a test keeps it equal to the constants above.
GITHUB_APP_REASON_CODES = ("GITHUB_APP_NOT_CONNECTED", "GITHUB_APP_NOT_INSTALLED", "GITHUB_APP_CLOCK_NOT_SET",
                           "GITHUB_APP_CLOCK_SKEW", "GITHUB_APP_KEY_REJECTED", "GITHUB_APP_NO_ACCESS",
                           "GITHUB_APP_PERMISSIONS_MISMATCH", "GITHUB_APP_MINT_BUDGET", "GITHUB_APP_MINT_FAILED",
                           "GITHUB_APP_BAD_KEY", "GITHUB_APP_CONNECT_FAILED")


# Plain English and the fix, per code; ``{page}`` is "<App> page -> GitHub connection".
REASON_TEXT = {
    NOT_CONNECTED: ("GitHub is not connected: there is no GitHub App key and no token.",
                    "Open the {page} and tap Connect to GitHub."),
    NOT_INSTALLED: ("The GitHub App exists but is not installed on the repository.",
                    "Open the {page} and follow 'install it on the repository'."),
    CLOCK_NOT_SET: ("The clock is not set yet (the Pi has no clock battery); signing in waits for network time.",
                    "Nothing; it clears by itself once the time is synced."),
    CLOCK_SKEW: ("GitHub says this device's clock is wrong, so it refused the sign-in.",
                 "Settings -> System -> General: check the time zone; restart the host if it persists."),
    KEY_REJECTED: ("GitHub refused the GitHub App key: it was deleted on GitHub, or the App was.",
                   "Open the {page}: Disconnect, then Connect to GitHub again."),
    NO_ACCESS: ("The GitHub App may not use this repository (it was removed from the installation).",
                "github.com/settings/installations -> the App -> Configure: add the repository."),
    PERMISSIONS: ("The GitHub App does not have the permissions this App needs.",
                  "Open the {page}: Disconnect, delete the App on GitHub, then Connect to GitHub again."),
    MINT_BUDGET: ("More GitHub App sign-ins today than allowed (30); stopped to stay safe.",
                  "Nothing; it resumes after midnight UTC. If it repeats, send this to the AI."),
    MINT_FAILED: ("GitHub could not issue a sign-in pass (GitHub or the internet may be down).",
                  "Nothing; it retries automatically."),
    BAD_KEY: ("The stored GitHub App key cannot be read.",
              "Open the {page}: Disconnect, then upload the key file or Connect to GitHub again."),
    CONNECT_FAILED: ("Connecting to GitHub did not finish.", "Open the {page} and try again."),
}


def explain(code: str, app: str) -> tuple[str, str] | None:
    item = REASON_TEXT.get(code)
    if item is None:
        return None
    return item[0], item[1].format(page=f"{app} page -> GitHub connection")


class GitHubAppError(net.NetError):
    """A GitHub App sign-in fault. ``code`` is a registered reason code; the text holds no secret."""

    def __init__(self, code: str, status: int = 0, message: str = "") -> None:
        super().__init__(status, f"{code}: {message}" if message else code)
        self.code = code


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _crypto():
    # Imported lazily so the rest of the App runs (PAT mode) even if the library failed to load.
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    return hashes, serialization, padding, rsa


def load_key(pem: bytes):
    """Parse an unencrypted RSA private key (GitHub ships PKCS#1; PKCS#8 also accepted)."""
    if not isinstance(pem, (bytes, bytearray)) or not 0 < len(pem) <= MAX_PEM_BYTES:
        raise GitHubAppError(BAD_KEY, message="not a private key file")
    if b"PRIVATE KEY-----" not in pem or b"ENCRYPTED" in pem:
        raise GitHubAppError(BAD_KEY, message="not an unencrypted private key file")
    _, serialization, _, rsa = _crypto()
    try:
        key = serialization.load_pem_private_key(bytes(pem), password=None)
    except (ValueError, TypeError):
        raise GitHubAppError(BAD_KEY, message="the file could not be read as a private key") from None
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < MIN_KEY_BITS:
        raise GitHubAppError(BAD_KEY, message="not an RSA key of at least 2048 bits")
    return key


def fingerprint(key) -> str:
    """The form GitHub shows next to each private key in the App settings ("SHA256:...")."""
    _, serialization, _, _ = _crypto()
    der = key.public_key().public_bytes(serialization.Encoding.DER,
                                        serialization.PublicFormat.SubjectPublicKeyInfo)
    return "SHA256:%s" % base64.b64encode(hashlib.sha256(der).digest()).decode("ascii")  # noqa: UP031 - not a reason code


def make_jwt(key, issuer: str, now: float) -> str:
    """RS256 JWT for the App itself: {iat: now-60, exp: now+540, iss: client id or App id}."""
    hashes, _, padding, _ = _crypto()
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = {"iat": int(now) - JWT_BACKDATE, "exp": int(now) + JWT_LIFETIME, "iss": str(issuer)}
    payload = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64url(signature)}"


def parse_time(value) -> float | None:
    """GitHub times: ``2026-10-06T21:00:00Z`` (token expires_at) or ``2026-12-27 10:00:00 -0700`` /
    ``... UTC`` (the PAT expiry header). Returns epoch seconds or None."""
    if not isinstance(value, str) or not 10 <= len(value) <= 40:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S UTC", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()
    return None


def _write_private(path: str, data: bytes) -> None:
    """Atomic 0600 write: a crash leaves the old file or the new one, never half of either."""
    tmp = f"{path}.tmp-{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


class KeyStore:
    """``<data>/github_app/key.pem`` (secret, 0600) and ``app.json`` (not secret: ids, fingerprint)."""

    def __init__(self, data_dir: str) -> None:
        self.dir = os.path.join(data_dir, DIR_NAME)
        self.key_path = os.path.join(self.dir, "key.pem")
        self.meta_path = os.path.join(self.dir, "app.json")
        self._lock = threading.Lock()

    def _ensure_dir(self) -> None:
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        os.chmod(self.dir, 0o700)

    def present(self) -> bool:
        return os.path.isfile(self.key_path) and os.path.isfile(self.meta_path)

    def meta(self) -> dict:
        try:
            with open(self.meta_path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            return {}
        return doc if isinstance(doc, dict) else {}

    def save_meta(self, doc: dict) -> None:
        with self._lock:
            self._ensure_dir()
            _write_private(self.meta_path, json.dumps(doc, sort_keys=True).encode("utf-8"))

    def update_meta(self, **fields) -> None:
        doc = self.meta()
        doc.update(fields)
        self.save_meta(doc)

    def save(self, pem: bytes, *, app_id: int, client_id: str, slug: str, owner: str, how: str,
             now: float) -> dict:
        """Store a verified key. Returns the non-secret meta (never the key)."""
        key = load_key(pem)
        if not isinstance(app_id, int) or isinstance(app_id, bool) or not 0 < app_id < 10**12:
            raise GitHubAppError(BAD_KEY, message="bad App id")
        if client_id and not RE_CLIENT_ID.fullmatch(client_id):
            raise GitHubAppError(BAD_KEY, message="bad client id")
        if slug and not RE_SLUG.fullmatch(slug):
            raise GitHubAppError(BAD_KEY, message="bad App name")
        meta = {"app_id": app_id, "client_id": client_id or "", "slug": slug or "", "owner": owner or "",
                "fingerprint": fingerprint(key), "connected_at": int(now), "how": how,
                "installation_id": None}
        with self._lock:
            self._ensure_dir()
            _write_private(self.key_path, bytes(pem))
            _write_private(self.meta_path, json.dumps(meta, sort_keys=True).encode("utf-8"))
        return meta

    def load(self):
        """(key, meta) or None when not connected. A damaged key file raises BAD_KEY."""
        if not self.present():
            return None
        with open(self.key_path, "rb") as fh:
            pem = fh.read(MAX_PEM_BYTES + 1)
        return load_key(pem), self.meta()

    # R2: the old-token retire state. Not secret (a fingerprint and dates); kept apart from the key so a
    # Disconnect never forgets that a token was already retired.
    def retire_state(self) -> dict:
        try:
            with open(os.path.join(self.dir, "retire.json"), encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            return {}
        return doc if isinstance(doc, dict) else {}

    def update_retire_state(self, **fields) -> dict:
        with self._lock:
            doc = {}
            try:
                with open(os.path.join(self.dir, "retire.json"), encoding="utf-8") as fh:
                    loaded = json.load(fh)
                    doc = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                pass
            doc.update(fields)
            self._ensure_dir()
            _write_private(os.path.join(self.dir, "retire.json"), json.dumps(doc, sort_keys=True).encode("utf-8"))
            return doc

    def forget(self) -> None:
        with self._lock:
            for path in (self.key_path, self.meta_path):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass


class TokenProvider:
    """Mints and caches one-hour installation tokens for one repository."""

    def __init__(self, store: KeyStore, repo: str, permissions: dict[str, str], api: str,
                 user_agent: str, clock=time.time, request=None) -> None:
        self.store = store
        self.repo = repo
        self.repo_name = repo.split("/", 1)[1]
        self.permissions = dict(permissions)
        self.api = api.rstrip("/")
        self.user_agent = user_agent
        self.clock = clock
        self._request = request or net.request
        self._lock = threading.Lock()
        self._token = ""
        self._expires = 0.0
        self.last_error = ""
        self.last_mint = 0.0
        self.granted: dict[str, str] = {}
        # A failing App (revoked key, uninstalled ...) must not be retried on every call: that would use up the
        # daily cap and replace the real reason with GITHUB_APP_MINT_BUDGET.
        self._failed: GitHubAppError | None = None
        self._retry_at = 0.0

    # -- public ------------------------------------------------------------------------
    def connected(self) -> bool:
        return self.store.present()

    def token(self) -> str:
        with self._lock:
            now = self.clock()
            if self._token and now < self._expires - REFRESH_MARGIN:
                return self._token
            self._token, self._expires = "", 0.0
            if self._failed is not None and now < self._retry_at:
                raise self._failed
            try:
                token, expires = self._mint(now)
            except GitHubAppError as err:
                self.last_error = err.code
                if err.code not in NO_BACKOFF:
                    self._failed, self._retry_at = err, now + FAILURE_BACKOFF
                raise
            self._token, self._expires = token, expires
            self._failed, self.last_error = None, ""
            return token

    def invalidate(self) -> None:
        """GitHub refused a cached token (revoked): the next call mints a new one."""
        with self._lock:
            self._token, self._expires = "", 0.0
            self._failed, self._retry_at = None, 0.0   # a new key or a revoked token: try again now

    def status(self) -> dict:
        """Everything the owner page and the daily check may show. Never a token or the key."""
        meta = self.store.meta() if self.store.present() else {}
        return {"connected": bool(meta), "slug": meta.get("slug") or "", "app_id": meta.get("app_id"),
                "fingerprint": meta.get("fingerprint") or "", "installed": bool(meta.get("installation_id")),
                "connected_at": meta.get("connected_at"), "last_mint": int(self.last_mint) or None,
                "token_expires": int(self._expires) or None, "last_error": self.last_error,
                "granted": dict(self.granted), "mints_today": self._mints_today(self.clock())}

    # -- minting -----------------------------------------------------------------------
    def _headers(self, bearer: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {bearer}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": self.user_agent}

    def _mints_today(self, now: float) -> int:
        meta = self.store.meta()
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        return int(meta.get("mints", 0)) if meta.get("mint_day") == day else 0

    def _count_mint(self, now: float) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        self.store.update_meta(mint_day=day, mints=self._mints_today(now) + 1)

    def _mint(self, now: float) -> tuple[str, float]:
        if now < CLOCK_FLOOR:
            raise GitHubAppError(CLOCK_NOT_SET, message="the clock is not set yet (waiting for network time)")
        loaded = self.store.load()
        if loaded is None:
            raise GitHubAppError(NOT_CONNECTED, message="no GitHub App key on this App yet")
        key, meta = loaded
        issuer = meta.get("client_id") or str(meta.get("app_id") or "")
        if not issuer:
            raise GitHubAppError(BAD_KEY, message="App id missing")
        if self._mints_today(now) >= MAX_MINTS_PER_DAY:
            raise GitHubAppError(MINT_BUDGET, message=f"more than {MAX_MINTS_PER_DAY} sign-ins today")
        jwt = make_jwt(key, issuer, now)
        net.register_secret(jwt)
        installation = meta.get("installation_id")
        if not installation:
            installation = self._installation_id(jwt)
            self.store.update_meta(installation_id=installation)
        self._count_mint(now)
        body = {"repositories": [self.repo_name], "permissions": self.permissions}
        try:
            _, data = self._request("POST", f"{self.api}/app/installations/{int(installation)}/access_tokens",
                                    self._headers(jwt), body=body, timeout=30)
        except net.NetError as err:
            if err.status == 404:      # the owner uninstalled the App: find the installation again
                self.store.update_meta(installation_id=None)
                raise GitHubAppError(NOT_INSTALLED, 404, "the GitHub App is not installed on the repository") \
                    from None
            raise self._classify(err) from None
        token = (data or {}).get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not RE_INSTALL_TOKEN.fullmatch(token):
            raise GitHubAppError(MINT_FAILED, message="GitHub answered without a usable token")
        net.register_secret(token)
        expires = parse_time((data or {}).get("expires_at"))
        if expires is None or not now < expires <= now + 3700:
            raise GitHubAppError(MINT_FAILED, message="GitHub answered with an unusable expiry time")
        granted = (data or {}).get("permissions")
        self.granted = {k: v for k, v in granted.items() if isinstance(k, str) and isinstance(v, str)} \
            if isinstance(granted, dict) else {}
        for name, level in self.permissions.items():
            if self.granted.get(name) != level:
                raise GitHubAppError(PERMISSIONS, message=f"GitHub did not grant {name}: {level}")
        self.last_mint = now
        return token, expires

    def _installation_id(self, jwt: str) -> int:
        try:
            _, data = self._request("GET", f"{self.api}/repos/{self.repo}/installation",
                                    self._headers(jwt), timeout=30)
        except net.NetError as err:
            if err.status == 404:
                raise GitHubAppError(NOT_INSTALLED, 404, "the GitHub App is not installed on the repository") \
                    from None
            raise self._classify(err) from None
        ident = (data or {}).get("id") if isinstance(data, dict) else None
        if not isinstance(ident, int) or isinstance(ident, bool) or ident <= 0:
            raise GitHubAppError(MINT_FAILED, message="GitHub answered without an installation id")
        return ident

    @staticmethod
    def _classify(err: net.NetError) -> GitHubAppError:
        text = str(err).lower()
        if err.status == 401:
            if "'exp'" in text or "'iat'" in text or "expiration time" in text or "issued at" in text:
                return GitHubAppError(CLOCK_SKEW, 401, "GitHub says this device's clock is wrong")
            return GitHubAppError(KEY_REJECTED, 401, "GitHub refused the App key (revoked, or the App was deleted)")
        if err.status == 403:
            return GitHubAppError(NO_ACCESS, 403, "the App may not use this repository or permission")
        if err.status == 422:
            return GitHubAppError(PERMISSIONS, 422, "the App does not have the permissions this App asks for")
        return GitHubAppError(MINT_FAILED, err.status, "GitHub could not be reached or answered with an error")


# -- owner setup: GitHub App manifest -----------------------------------------------------------
def manifest(name: str, repo: str, redirect_url: str, setup_url: str, permissions: dict[str, str],
             description: str) -> dict:
    """The fixed GitHub App definition. Webhook off, private, no OAuth, no events."""
    perms = dict(permissions)
    perms.setdefault("metadata", "read")
    return {
        "name": name[:34],
        "url": f"https://github.com/{repo}",
        "description": description[:200],
        "hook_attributes": {"url": f"https://github.com/{repo}", "active": False},
        "redirect_url": redirect_url,
        "setup_url": setup_url,
        "setup_on_update": False,
        "public": False,
        "request_oauth_on_install": False,
        "default_permissions": perms,
        "default_events": [],
    }


def manifest_action(owner: str, state: str) -> str:
    """Where the page's form posts (a personal account; the owner taps Create there)."""
    return "https://github.com/settings/apps/new?" + urllib.parse.urlencode({"state": state})


class ConnectStates:
    """Single-use, owner-bound, one-hour ``state`` values for the manifest round trip."""

    def __init__(self, clock=time.time) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, tuple[str, float]] = {}
        self.clock = clock

    def issue(self, user_id: str) -> str:
        state = secrets.token_urlsafe(24)
        with self._lock:
            now = self.clock()
            self._items = {k: v for k, v in self._items.items() if v[1] > now}
            if len(self._items) >= 8:                 # bounded: oldest goes first
                oldest = min(self._items, key=lambda k: self._items[k][1])
                del self._items[oldest]
            self._items[state] = (user_id, now + STATE_TTL)
        return state

    def take_any(self, state: str) -> str:
        """Use the value up (any attempt) and return the HA user it was issued to ("" = not valid). R2: the
        hand-off address has no HA session, so the caller checks that this user is the owner."""
        if not isinstance(state, str) or not RE_STATE.fullmatch(state):
            return ""
        with self._lock:
            item = self._items.pop(state, None)
        return item[0] if item and item[0] and self.clock() <= item[1] else ""


def convert(api: str, code: str, user_agent: str, expect_owner: str, permissions: dict[str, str],
            request=None) -> dict:
    """Exchange the one-time manifest code for the App id, client id and private key (unauthenticated)."""
    if not isinstance(code, str) or not RE_CODE.fullmatch(code):
        raise GitHubAppError(CONNECT_FAILED, message="the code from GitHub is malformed")
    req = request or net.request
    try:
        _, data = req("POST", f"{api.rstrip('/')}/app-manifests/{code}/conversions",
                      {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                       "User-Agent": user_agent}, body={}, timeout=30)
    except net.NetError as err:
        raise GitHubAppError(CONNECT_FAILED, err.status,
                             "GitHub refused the code (used already or older than one hour)") from None
    if not isinstance(data, dict):
        raise GitHubAppError(CONNECT_FAILED, message="GitHub answered without the App details")
    pem = data.get("pem")
    if isinstance(pem, str):
        net.register_secret(pem)
    owner = (data.get("owner") or {}).get("login") if isinstance(data.get("owner"), dict) else None
    if not isinstance(owner, str) or owner.lower() != expect_owner.lower():
        raise GitHubAppError(CONNECT_FAILED, message="the new App does not belong to the repository owner")
    raw_granted = data.get("permissions")
    granted: dict = raw_granted if isinstance(raw_granted, dict) else {}
    wanted = dict(permissions)
    wanted.setdefault("metadata", "read")
    if dict(granted) != wanted:
        raise GitHubAppError(PERMISSIONS, message="the new App's permissions differ from the fixed list")
    if not isinstance(pem, str) or not isinstance(data.get("id"), int):
        raise GitHubAppError(CONNECT_FAILED, message="GitHub answered without the key")
    owner_id = data["owner"].get("id")
    return {"pem": pem.encode("ascii", "replace"), "app_id": data["id"], "client_id": str(data.get("client_id") or ""),
            "slug": str(data.get("slug") or ""), "owner": owner,
            "owner_id": owner_id if isinstance(owner_id, int) and not isinstance(owner_id, bool) else None}


def lookup_app(api: str, slug: str, bearer: str, user_agent: str, request=None) -> dict:
    """``GET /apps/{slug}`` (needs a token for a private App): the App id for the .pem upload fallback."""
    if not RE_SLUG.fullmatch(slug or ""):
        raise GitHubAppError(CONNECT_FAILED, message="bad App name")
    req = request or net.request
    try:
        _, data = req("GET", f"{api.rstrip('/')}/apps/{slug}",
                      {"Authorization": f"Bearer {bearer}", "Accept": "application/vnd.github+json",
                       "X-GitHub-Api-Version": "2022-11-28", "User-Agent": user_agent}, timeout=30)
    except net.NetError as err:
        raise GitHubAppError(CONNECT_FAILED, err.status, "GitHub could not find that App") from None
    if not isinstance(data, dict) or not isinstance(data.get("id"), int):
        raise GitHubAppError(CONNECT_FAILED, message="GitHub could not find that App")
    owner = (data.get("owner") or {}).get("login") if isinstance(data.get("owner"), dict) else ""
    return {"app_id": data["id"], "client_id": str(data.get("client_id") or ""), "slug": slug, "owner": owner or ""}


def repo_id(api: str, repo: str, bearer: str, user_agent: str, request=None) -> int | None:
    """R2: the repository's numeric id (``GET /repos/{repo}``), so the install page opens with only this repository
    selected. None when it cannot be read; the caller then never sends a pre-selecting link (GitHub would
    pre-select ALL repositories without ``repository_ids``)."""
    if not bearer:
        return None
    req = request or net.request
    try:
        _, data = req("GET", f"{api.rstrip('/')}/repos/{repo}",
                      {"Authorization": f"Bearer {bearer}", "Accept": "application/vnd.github+json",
                       "X-GitHub-Api-Version": "2022-11-28", "User-Agent": user_agent}, timeout=30)
    except net.NetError:
        return None
    rid = data.get("id") if isinstance(data, dict) else None
    full = data.get("full_name") if isinstance(data, dict) else None
    if not isinstance(rid, int) or isinstance(rid, bool) or not 0 < rid < 10**12:
        return None
    if not isinstance(full, str) or full.lower() != repo.lower():
        return None
    return rid


def install_url(slug: str, owner_id: int | None, rid: int | None) -> str | None:
    """R2: the install page with the account AND only this repository pre-selected; None without both ids."""
    if not RE_SLUG.fullmatch(slug or ""):
        return None
    for value in (owner_id, rid):
        if not isinstance(value, int) or isinstance(value, bool) or not 0 < value < 10**12:
            return None
    query = urllib.parse.urlencode([("suggested_target_id", str(owner_id)), ("repository_ids[]", str(rid))])
    return f"https://github.com/apps/{slug}/installations/new/permissions?{query}"


def token_fingerprint(token: str) -> str:
    """Not secret: enough to recognise one token again, never enough to use it."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def revoke(api: str, token: str, user_agent: str, request=None) -> int:
    """R2: ask GitHub to revoke one hand-made token (``POST /credentials/revoke``). GitHub refuses this call when
    it carries an Authorization header (403), so it carries none. Returns the HTTP status (0 = no answer)."""
    if not isinstance(token, str) or not RE_PAT.fullmatch(token):
        return -1                                          # not a hand-made token: never sent anywhere
    req = request or net.request
    try:
        status, _ = req("POST", f"{api.rstrip('/')}/credentials/revoke",
                        {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                         "User-Agent": user_agent}, body={"credentials": [token]}, timeout=30)
    except net.NetError as err:
        return err.status
    return status


def days_left(expires_epoch: float | None, now: float) -> int | None:
    if expires_epoch is None:
        return None
    return int((expires_epoch - now) // 86400)


# -- which credential signs a request ------------------------------------------------------------
AUTO, APP_ONLY, PAT_ONLY = "auto", "github_app", "pat"
MODES = (AUTO, APP_ONLY, PAT_ONLY)
EXPIRY_HEADER = "github-authentication-token-expiration"


class Auth:
    """Chooses the credential for each GitHub call.

    ``auto`` (default): the GitHub App when one is connected, else the hand-made token (PAT); when the
    App is connected but cannot sign in, the PAT is used for that call and the reason is kept for the
    owner page and the daily credential check (transition fallback; accepted PAT behaviour preserved).
    ``github_app``: never the PAT. ``pat``: never the App (the old behaviour exactly).

    R2: ``maybe_retire`` revokes the PAT once the GitHub App has signed every call for ``RETIRE_AFTER``; a
    retired PAT is never used again (``pat_set`` reads false), whatever happens to the GitHub App later.
    """

    def __init__(self, mode: str, pat: str, provider: TokenProvider | None, clock=time.time, *,
                 retire: bool = False, api: str = "", user_agent: str = "", request=None) -> None:
        if mode not in MODES:
            raise ValueError("github_auth")
        self.mode = mode
        self.pat = pat or ""
        self.provider = provider
        self.clock = clock
        self.retire = bool(retire)
        self.api = api
        self.user_agent = user_agent
        self.request = request
        store = provider.store if provider is not None else None
        state = store.retire_state() if store is not None else {}
        self._retire_state = state
        if self.pat and state.get("retired_fp") == token_fingerprint(self.pat):
            self.pat = ""                      # retired earlier: never used again
            self.pat_retired = True
        else:
            self.pat_retired = False
        self.last_kind = ""
        self.app_error = ""            # last GitHub App fault while the PAT stood in (auto mode)
        self.pat_expires: float | None = None
        self.pat_expiry_seen = False   # a PAT answer carried the header (absent = no expiry date)
        self.last_ok = 0.0
        self.last_status = 0

    def bearer(self) -> tuple[str, str]:
        if self.mode != PAT_ONLY and self.provider is not None and self.provider.connected():
            try:
                token = self.provider.token()
                self.app_error = ""
                self.last_kind = APP_ONLY
                return token, APP_ONLY
            except GitHubAppError as err:
                self._streak_broken()
                if self.mode == APP_ONLY or not self.pat:
                    raise
                self.app_error = err.code
        if self.mode != APP_ONLY and self.pat:
            self.last_kind = PAT_ONLY
            return self.pat, PAT_ONLY
        raise GitHubAppError(NOT_CONNECTED, message="connect the GitHub App on this App's page")

    def observe(self, kind: str, status: int, headers: dict[str, str] | None) -> None:
        """Record the outcome of one call (status 0 = network fault)."""
        self.last_status = status
        if 200 <= status < 300:
            self.last_ok = self.clock()
        if kind == APP_ONLY and 200 <= status < 300:
            self._streak_ok()
        elif kind == PAT_ONLY or status in (0, 401) or status >= 500:
            self._streak_broken()     # a PAT-signed call, a refused App token or no answer: start again
        if kind == PAT_ONLY and headers is not None and status and status != 401:
            raw = headers.get(EXPIRY_HEADER)
            self.pat_expiry_seen = True
            self.pat_expires = parse_time(raw) if raw else None

    def unauthorized(self, kind: str) -> bool:
        """GitHub said 401. True when one retry with a fresh App token is worth it."""
        if kind == APP_ONLY and self.provider is not None:
            self.provider.invalidate()
            return True
        return False

    # -- R2: retire the old hand-made token ------------------------------------------------------
    def _store(self):
        return self.provider.store if self.provider is not None else None

    def _streak_ok(self) -> None:
        if not self._retire_state.get("app_ok_since") and self._store() is not None:
            self._retire_state = self._store().update_retire_state(app_ok_since=int(self.clock()))

    def _streak_broken(self) -> None:
        if self._retire_state.get("app_ok_since") and self._store() is not None:
            self._retire_state = self._store().update_retire_state(app_ok_since=None)

    def retire_due(self) -> bool:
        """All conditions for retiring the PAT hold now (no network)."""
        state, now = self._retire_state, self.clock()
        since = state.get("app_ok_since")
        return bool(
            self.retire and self.pat and not self.pat_retired and self.mode != PAT_ONLY
            and self.provider is not None and self.provider.connected()
            and bool(self.provider.status().get("installed")) and not self.app_error
            and isinstance(since, (int, float)) and now - since >= RETIRE_AFTER
            and now >= float(state.get("retry_after") or 0))

    def maybe_retire(self) -> str:
        """Called once per poll. Returns "" (nothing to do), "RETIRED", or "RETRY_<status>"."""
        if not self.retire_due():
            return ""
        status = revoke(self.api, self.pat, self.user_agent, request=self.request)
        now = int(self.clock())
        store = self._store()
        if status in (200, 202):
            self._retire_state = store.update_retire_state(
                retired_fp=token_fingerprint(self.pat), retired_at=now, last_status=status, retry_after=None)
            self.pat = ""
            self.pat_retired = True
            self.pat_expires = None
            return "RETIRED"
        self._retire_state = store.update_retire_state(last_status=status, retry_after=now + RETIRE_RETRY)
        return f"RETRY_{status}"

    def summary(self) -> dict:
        """For the status entity, the owner page and the daily check. Dates and codes only."""
        now = self.clock()
        app = self.provider.status() if self.provider is not None else {"connected": False}
        pat_exp = self.pat_expires if self.pat else None
        return {
            "mode": self.mode, "using": self.last_kind or "", "pat_set": bool(self.pat),
            "pat_expires": time.strftime("%Y-%m-%d", time.gmtime(pat_exp)) if pat_exp else None,
            "pat_days_left": days_left(pat_exp, now),
            "app_connected": bool(app.get("connected")), "app_slug": app.get("slug") or "",
            "app_fingerprint": app.get("fingerprint") or "", "app_installed": bool(app.get("installed")),
            "app_error": app.get("last_error") or self.app_error or "",
            "last_ok": int(self.last_ok) or None, "last_status": self.last_status,
            "pat_retired": (time.strftime("%Y-%m-%d", time.gmtime(float(self._retire_state["retired_at"])))
                            if self.pat_retired and self._retire_state.get("retired_at") else
                            ("yes" if self.pat_retired else None)),
            "retire_after": (time.strftime("%Y-%m-%d %H:%M", time.gmtime(
                float(self._retire_state["app_ok_since"]) + RETIRE_AFTER))
                if self.retire and self.pat and self._retire_state.get("app_ok_since") else None),
        }
