"""Supervisor and Home Assistant Core access through one pinned allowlist.

This App holds a Supervisor **manager** token (owner decision D1, 2026-09-29). A manager
token can read every App's options (including other Apps' secrets) and can change, start,
stop or uninstall any App. The token itself is therefore never "read-only". The only
boundary is this module:

* every HTTP call and WebSocket command passes ``_guard``/``_ws_guard`` before any I/O;
* App routes are pinned at construction to the two configured target slugs (the Scout and
  the Observer). There is no generic ``/addons`` listing, no uninstall, stop, rebuild,
  stdin, security, store-repository, host, OS, network or Docker route;
* other Apps' info replies are projected to a few named fields at once
  (``_project``); the raw reply, and with it any other option value, is never returned,
  logged or stored;
* App updates (0.1.0 update job) add read-only discovery routes (installed list, per-App
  info, store info, changelog, Core info, resolution info) whose replies are projected the
  same way, and exactly one *mutating* target at a time: ``with ha.update_target(slug)``
  opens update / partial backup / partial restore for that one slug only; outside that
  block every mutating update route is forbidden. ``self`` can never be a target.

The ``ha`` CLI is never used (#223 R4 ``apps``/``addons`` escape).
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from . import net

STATUS_ENTITY = "sensor.house_brain_maintenance_status"
APPROVAL_EVENT = "mobile_app_notification_action"
RE_SLUG = re.compile(r"^[a-z0-9][a-z0-9_]{0,99}$")
RE_BACKUP_SLUG = re.compile(r"^[a-f0-9]{8,64}$")
RE_APP_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:-]{0,63}$")
SAFE_STATES = frozenset({"startup", "started", "stopped", "unknown", "error"})
PRIVILEGE_FIELDS = ("hassio_role", "full_access", "host_network", "host_pid", "docker_api", "auth_api",
                    "homeassistant_api", "apparmor")
_WS_ALLOW = frozenset({"subscribe_events", "unsubscribe_events", "config/auth/list"})

_STATIC_ALLOW: tuple[tuple[str, str], ...] = (
    ("GET", r"/addons/self/info"),
    ("GET", r"/addons"),
    ("GET", r"/addons/[a-z0-9][a-z0-9_]{0,99}/info"),
    ("GET", r"/addons/[a-z0-9][a-z0-9_]{0,99}/changelog"),
    ("GET", r"/store/addons/[a-z0-9][a-z0-9_]{0,99}"),
    ("GET", r"/core/info"),
    ("GET", r"/resolution/info"),
    ("GET", r"/core/api/"),
    ("POST", r"/core/api/services/notify/mobile_app_[a-z0-9_]{1,80}"),
    ("POST", r"/core/api/states/" + re.escape(STATUS_ENTITY)),
)


class ForbiddenCall(RuntimeError):
    pass


class HAError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(net.redact(f"{code}: {detail}"[:400]))
        self.code = code


@dataclass(frozen=True)
class AppView:
    """The only fields of another App that this App ever keeps."""

    slug: str
    state: str
    version: str
    option_keys: tuple[str, ...]
    broker_url: str | None
    key_fingerprint: str | None  # SHA-256 of the Scout inventory key; the key itself is dropped


@dataclass(frozen=True)
class InstalledApp:
    slug: str
    name: str
    version: str | None
    version_latest: str | None
    update_available: bool
    state: str
    stage: str
    repository: str


@dataclass(frozen=True)
class AppDetail:
    """Update-relevant facts of one App (installed or store view). No options, no free text."""

    slug: str
    version: str | None
    version_latest: str | None
    state: str
    stage: str
    homeassistant: str | None      # minimum Core version this (store) version declares
    available: bool | None
    privileges: tuple[tuple[str, str], ...]
    services: tuple[str, ...]      # e.g. ("mqtt:provide",)
    installed: bool


def _detail(slug: str, data: Any, *, installed: bool) -> AppDetail:
    if not isinstance(data, dict):
        raise HAError("APP_INFO_SHAPE", slug)
    def ver(key: str) -> str | None:
        v = data.get(key)
        return v if isinstance(v, str) and RE_APP_VERSION.fullmatch(v) else None
    privileges = tuple((k, str(data.get(k))[:20]) for k in PRIVILEGE_FIELDS if k in data)
    services = tuple(sorted(str(x)[:40] for x in (data.get("services") or []) if isinstance(x, str)))
    state = data.get("state")
    return AppDetail(slug=slug, version=ver("version"), version_latest=ver("version_latest"),
                     state=state if state in SAFE_STATES else "unknown", stage=str(data.get("stage") or "")[:16],
                     homeassistant=ver("homeassistant"),
                     available=data.get("available") if isinstance(data.get("available"), bool) else None,
                     privileges=privileges, services=services, installed=installed)


def fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _project(slug: str, data: Any, key_option: str | None) -> AppView:
    if not isinstance(data, dict):
        raise HAError("APP_INFO_SHAPE", slug)
    state = data.get("state")
    version = data.get("version")
    options = data.get("options")
    if not isinstance(state, str) or not isinstance(version, str) or not isinstance(options, dict):
        raise HAError("APP_INFO_SHAPE", slug)
    url = options.get("broker_url")
    key = options.get(key_option) if key_option else None
    return AppView(
        slug=slug,
        state=state[:32],
        version=version[:32],
        option_keys=tuple(sorted(str(k)[:64] for k in options)),
        broker_url=url if isinstance(url, str) and len(url) <= 300 else None,
        key_fingerprint=fingerprint(key) if isinstance(key, str) and key else None,
    )


class HomeAssistant:
    def __init__(self, base: str, token: str, ws_url: str, scout_slug: str, observer_slug: str) -> None:
        for slug in (scout_slug, observer_slug):
            if not RE_SLUG.fullmatch(slug) or slug == "self":
                raise ValueError("invalid target slug")
        if scout_slug == observer_slug:
            raise ValueError("scout and observer slugs must differ")
        self.base = base.rstrip("/")
        self.ws_url = ws_url
        self.scout = scout_slug
        self.observer = observer_slug
        self._token = token
        net.register_secret(token)
        s, o = re.escape(scout_slug), re.escape(observer_slug)
        routes = _STATIC_ALLOW + (
            ("GET", rf"/addons/{s}/info"),
            ("GET", rf"/addons/{o}/info"),
            ("POST", rf"/addons/{s}/options"),
            ("POST", rf"/addons/{s}/start"),
            ("GET", rf"/addons/{s}/logs/latest"),
        )
        self._allow = tuple((m, re.compile(p)) for m, p in routes)
        self._target: str | None = None
        self._self_slug: str | None = None

    # -- guard --------------------------------------------------------------
    def _guard(self, method: str, path: str) -> None:
        for allowed_method, pattern in self._allow:
            if method == allowed_method and pattern.fullmatch(path):
                if path.startswith("/addons/self/") or not path.startswith("/addons/"):
                    return
                slug = path.split("/")[2]
                if slug != "self" and slug != self._self_slug:
                    return
        target = self._target
        if target is not None:
            t = re.escape(target)
            for allowed_method, pattern in (("POST", rf"/addons/{t}/update"),
                                            ("POST", r"/backups/new/partial"),
                                            ("GET", r"/backups/[a-f0-9]{8,64}/info"),
                                            ("POST", r"/backups/[a-f0-9]{8,64}/restore/partial")):
                if method == allowed_method and re.fullmatch(pattern, path):
                    return
        raise ForbiddenCall(f"{method} {path}")

    @contextmanager
    def update_target(self, slug: str):
        """Open the mutating update routes for exactly one App for the duration of a job."""
        if not RE_SLUG.fullmatch(slug) or slug == "self" or slug == self.self_slug():
            raise ForbiddenCall(f"update target {slug}")
        if self._target is not None:
            raise ForbiddenCall("nested update target")
        self._target = slug
        try:
            yield
        finally:
            self._target = None

    @staticmethod
    def _ws_guard(msg: dict) -> None:
        kind = msg.get("type")
        if kind not in _WS_ALLOW:
            raise ForbiddenCall(f"ws {kind}")
        if kind == "subscribe_events" and msg.get("event_type") != APPROVAL_EVENT:
            raise ForbiddenCall("ws subscribe to non-approval event")

    # -- HTTP ---------------------------------------------------------------
    def _call(self, method: str, path: str, body: Any = None, timeout: float = 30.0,
              raw: bool = False) -> Any:
        self._guard(method, path)
        headers = {"Authorization": f"Bearer {self._token}"}
        if raw:
            headers["Accept"] = "text/plain"
        _, data = net.request(method, self.base + path, headers, body=body, timeout=timeout, raw=raw)
        return data

    def _supervisor(self, method: str, path: str, body: Any = None, timeout: float = 30.0) -> Any:
        data = self._call(method, path, body, timeout)
        if not isinstance(data, dict) or data.get("result") != "ok":
            # Never echo the body: an info reply may carry other Apps' option values.
            raise HAError("SUPERVISOR_RESULT", path)
        return data.get("data") or {}

    def self_slug(self) -> str:
        if self._self_slug:
            return self._self_slug
        data = self._supervisor("GET", "/addons/self/info")
        slug = data.get("slug") if isinstance(data, dict) else None
        if not isinstance(slug, str) or not RE_SLUG.fullmatch(slug):
            raise HAError("SELF_SLUG")
        self._self_slug = slug
        return slug

    def scout_view(self) -> AppView:
        return _project(self.scout, self._supervisor("GET", f"/addons/{self.scout}/info"),
                        "broker_inventory_write_key")

    def observer_view(self) -> AppView:
        return _project(self.observer, self._supervisor("GET", f"/addons/{self.observer}/info"), None)

    # -- update discovery (read-only, projected) -----------------------------------
    def installed_apps(self) -> list[InstalledApp]:
        data = self._supervisor("GET", "/addons")
        rows = data.get("addons") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise HAError("APPS_SHAPE")
        out = []
        for row in rows[:256]:
            if not isinstance(row, dict):
                raise HAError("APPS_SHAPE")
            slug, version, latest = row.get("slug"), row.get("version"), row.get("version_latest")
            if not isinstance(slug, str) or not RE_SLUG.fullmatch(slug):
                raise HAError("APPS_SHAPE")
            out.append(InstalledApp(
                slug=slug,
                name=str(row.get("name") or slug)[:60],
                version=version if isinstance(version, str) and RE_APP_VERSION.fullmatch(version) else None,
                version_latest=latest if isinstance(latest, str) and RE_APP_VERSION.fullmatch(latest) else None,
                update_available=row.get("update_available") is True,
                state=row.get("state") if row.get("state") in SAFE_STATES else "unknown",
                stage=str(row.get("stage") or "")[:16],
                repository=str(row.get("repository") or "")[:40],
            ))
        return out

    def app_detail(self, slug: str) -> AppDetail:
        """Installed App facts; options are dropped at once (never kept)."""
        data = self._supervisor("GET", f"/addons/{slug}/info")
        return _detail(slug, data, installed=True)

    def store_detail(self, slug: str) -> AppDetail:
        data = self._supervisor("GET", f"/store/addons/{slug}")
        return _detail(slug, data, installed=False)

    def changelog(self, slug: str) -> str:
        try:
            data = self._call("GET", f"/addons/{slug}/changelog", timeout=30, raw=True)
        except net.NetError:
            return ""
        return bytes(data or b"")[:65536].decode("utf-8", "replace")

    def core_version(self) -> str:
        data = self._supervisor("GET", "/core/info")
        version = data.get("version") if isinstance(data, dict) else None
        if not isinstance(version, str) or not RE_APP_VERSION.fullmatch(version):
            raise HAError("CORE_VERSION")
        return version

    def core_alive(self) -> bool:
        try:
            data = self._call("GET", "/core/api/", timeout=15)
        except (net.NetError, HAError):
            return False
        return isinstance(data, dict) and isinstance(data.get("message"), str)

    def health_flags(self) -> tuple[frozenset[str], frozenset[str]]:
        """(unhealthy reasons, unsupported reasons) as bounded identifier sets."""
        data = self._supervisor("GET", "/resolution/info")
        def ids(key: str) -> frozenset[str]:
            vals = data.get(key) if isinstance(data, dict) else None
            return frozenset(str(v)[:60] for v in (vals or []) if isinstance(v, str))
        return ids("unhealthy"), ids("unsupported")

    # -- update mutation (only inside update_target) ---------------------------------
    def update_app(self, slug: str) -> None:
        self._supervisor("POST", f"/addons/{slug}/update", body={"backup": False}, timeout=3600)

    def backup_app(self, slug: str, name: str) -> str:
        data = self._supervisor("POST", "/backups/new/partial",
                                body={"name": name[:100], "addons": [slug], "folders": [],
                                      "homeassistant": False},
                                timeout=3600)
        bslug = data.get("slug") if isinstance(data, dict) else None
        if not isinstance(bslug, str) or not RE_BACKUP_SLUG.fullmatch(bslug):
            raise HAError("BACKUP_SLUG")
        return bslug

    def backup_app_version(self, bslug: str, slug: str) -> str | None:
        data = self._supervisor("GET", f"/backups/{bslug}/info")
        for row in (data.get("addons") or []) if isinstance(data, dict) else []:
            if isinstance(row, dict) and row.get("slug") == slug and isinstance(row.get("version"), str):
                return row["version"][:64]
        return None

    def restore_app(self, bslug: str, slug: str) -> None:
        self._supervisor("POST", f"/backups/{bslug}/restore/partial",
                         body={"addons": [slug], "folders": [], "homeassistant": False}, timeout=3600)

    def set_scout_options(self, broker_url: str, inventory_key: str) -> None:
        net.register_secret(inventory_key)
        self._supervisor("POST", f"/addons/{self.scout}/options",
                         body={"options": {"broker_url": broker_url,
                                           "broker_inventory_write_key": inventory_key}})

    def start_scout(self) -> None:
        self._supervisor("POST", f"/addons/{self.scout}/start", body={}, timeout=120)

    def scout_latest_logs(self) -> str:
        """Log of the Scout's most recent container run only (Supervisor ``/logs/latest``)."""
        data = self._call("GET", f"/addons/{self.scout}/logs/latest", timeout=60, raw=True)
        return net.redact(bytes(data or b"")[:262144].decode("utf-8", "replace"))

    def notify(self, service: str, payload: dict) -> None:
        self._call("POST", f"/core/api/services/notify/{service}", body=payload, timeout=30)

    def publish_status(self, state: str, attributes: dict) -> None:
        try:
            self._call("POST", f"/core/api/states/{STATUS_ENTITY}",
                       body={"state": state[:250], "attributes": attributes}, timeout=15)
        except (net.NetError, HAError, ForbiddenCall):
            pass  # visibility only

    # -- WebSocket ------------------------------------------------------------
    def ws(self) -> CoreSocket:
        return CoreSocket(self.ws_url, self._token, self._ws_guard)


class CoreSocket:
    """Synchronous Core WebSocket session restricted to the allowlist (Deployer lineage)."""

    def __init__(self, url: str, token: str, guard) -> None:
        from websockets.sync.client import connect

        self._guard = guard
        self._conn = connect(url, open_timeout=20, close_timeout=5, max_size=4 * 1024 * 1024)
        self._next = 1
        if self._recv(20).get("type") != "auth_required":
            raise HAError("WS_HANDSHAKE")
        self._conn.send(json.dumps({"type": "auth", "access_token": token}))
        if self._recv(20).get("type") != "auth_ok":
            raise HAError("WS_AUTH")

    def _recv(self, timeout: float) -> dict:
        data = json.loads(self._conn.recv(timeout=timeout))
        if not isinstance(data, dict):
            raise HAError("WS_SHAPE")
        return data

    def send(self, msg: dict) -> int:
        self._guard(msg)
        msg_id = self._next
        self._next += 1
        self._conn.send(json.dumps({"id": msg_id, **msg}))
        return msg_id

    def command(self, msg: dict, timeout: float = 30.0) -> Any:
        msg_id = self.send(msg)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HAError("WS_TIMEOUT", msg.get("type", ""))
            data = self._recv(remaining)
            if data.get("id") == msg_id and data.get("type") == "result":
                if not data.get("success"):
                    raise HAError("WS_COMMAND", str(data.get("error"))[:200])
                return data.get("result")

    def next_event(self, timeout: float) -> dict | None:
        try:
            data = self._recv(timeout)
        except TimeoutError:
            return None
        if data.get("type") == "event":
            return data.get("event")
        return {}

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001, S110 - closing a dead socket is best-effort
            pass
