"""Supervisor and Home Assistant Core access through a single allowlisted gate.

The App token (``SUPERVISOR_TOKEN``) can technically reach more than the
Deployer needs. Every HTTP call and every WebSocket command therefore passes
through ``_guard``/``_ws_guard``: anything not on the explicit allowlist raises
``ForbiddenCall`` before any I/O. Only stable v1 Supervisor paths are used;
the ``ha`` CLI is never invoked (see the #223 R4 ``apps``/``addons`` escape).
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from . import net

STATUS_ENTITY = "sensor.house_brain_deployer_status"
# 0.3.7 (R2, store move): which Deployer writes the status. The Supervisor names an App's container after its slug
# (``local-house-brain-deployer`` or ``<hash>-house-brain-deployer``); "" when unknown (then never standby).
RE_INSTANCE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
INSTANCE = os.environ.get("HOSTNAME", "") if RE_INSTANCE.fullmatch(os.environ.get("HOSTNAME", "")) else ""

_HTTP_ALLOW: tuple[tuple[str, re.Pattern], ...] = (
    ("GET", re.compile(r"^/info$")),
    ("GET", re.compile(r"^/core/info$")),
    ("GET", re.compile(r"^/backups/info$")),
    ("POST", re.compile(r"^/core/check$")),
    ("POST", re.compile(r"^/core/restart$")),
    ("GET", re.compile(r"^/core/api/config$")),
    ("GET", re.compile(r"^/core/api/states/[a-z_]{2,32}\.[a-z0-9_]{1,96}$")),
    ("POST", re.compile(r"^/core/api/config/core/check_config$")),
    ("POST", re.compile(r"^/core/api/template$")),  # read-only render (owner-approved lookups)
    ("POST", re.compile(r"^/core/api/services/notify/mobile_app_[a-z0-9_]{1,80}$")),
    ("POST", re.compile(r"^/core/api/services/hassio/backup_partial$")),
    ("POST", re.compile(r"^/core/api/states/" + re.escape(STATUS_ENTITY) + r"$")),
)
_WS_ALLOW = frozenset({"subscribe_events", "unsubscribe_events", "system_log/list", "config/auth/list"})
APPROVAL_EVENT = "mobile_app_notification_action"


# 0.3.5 (P4): an App update or options save during an open install hard-stops the App and rolls it back.
INSTALL_WARNING = ("Install in progress - updating, restarting or reconfiguring the House Brain Deployer now "
                   "will roll it back. Wait until the status is no longer DEPLOYING.")

class ForbiddenCall(RuntimeError):
    pass


class HAError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(net.redact(f"{code}: {detail}"[:400]))
        self.code = code


def _guard(method: str, path: str) -> None:
    for allowed_method, pattern in _HTTP_ALLOW:
        if method == allowed_method and pattern.fullmatch(path):
            return
    raise ForbiddenCall(f"{method} {path}")


def _ws_guard(msg: dict) -> None:
    kind = msg.get("type")
    if kind not in _WS_ALLOW:
        raise ForbiddenCall(f"ws {kind}")
    if kind == "subscribe_events" and msg.get("event_type") != APPROVAL_EVENT:
        raise ForbiddenCall("ws subscribe to non-approval event")


class HomeAssistant:
    def __init__(self, base: str, token: str, ws_url: str) -> None:
        self.base = base.rstrip("/")
        self.ws_url = ws_url
        self._token = token
        net.register_secret(token)

    # -- HTTP -------------------------------------------------------------
    def _call(self, method: str, path: str, body: Any = None, timeout: float = 30.0) -> Any:
        _guard(method, path)
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            _, data = net.request(method, self.base + path, headers, body=body, timeout=timeout)
        except net.NetError as err:
            err.service = "home_assistant"
            raise
        return data

    def _supervisor(self, method: str, path: str, body: Any = None, timeout: float = 30.0) -> Any:
        data = self._call(method, path, body, timeout)
        if not isinstance(data, dict) or data.get("result") != "ok":
            raise HAError("SUPERVISOR_RESULT", json.dumps(data)[:200] if data else "empty")
        return data.get("data") or {}

    def supervisor_info(self) -> dict:
        return self._supervisor("GET", "/info")

    def core_info(self) -> dict:
        return self._supervisor("GET", "/core/info")

    def backups(self) -> list[dict]:
        data = self._supervisor("GET", "/backups/info")
        items = data.get("backups")
        return items if isinstance(items, list) else []

    def supervisor_check(self) -> tuple[bool, str]:
        """Supervisor full-process config check. Errors only (it has no warnings channel)."""
        try:
            self._supervisor("POST", "/core/check", body={}, timeout=600)
            return True, ""
        except net.NetError as err:
            return False, str(err)
        except HAError as err:
            return False, str(err)

    def core_check(self) -> tuple[str, str | None, str | None]:
        data = self._call("POST", "/core/api/config/core/check_config", body={}, timeout=600)
        if not isinstance(data, dict) or data.get("result") not in ("valid", "invalid"):
            raise HAError("CORE_CHECK_SHAPE")
        return data["result"], data.get("errors"), data.get("warnings")

    def restart(self) -> None:
        self._supervisor("POST", "/core/restart", body={"safe_mode": False}, timeout=120)

    def core_state(self) -> str | None:
        try:
            data = self._call("GET", "/core/api/config", timeout=15)
        except net.NetError:
            return None
        return data.get("state") if isinstance(data, dict) else None

    def get_state(self, entity_id: str) -> dict | None:
        try:
            data = self._call("GET", f"/core/api/states/{entity_id}", timeout=15)
        except net.NetError as err:
            if err.status == 404:
                return None
            raise
        return data if isinstance(data, dict) else None

    def backup_partial(self, name: str) -> None:
        self._call("POST", "/core/api/services/hassio/backup_partial",
                   body={"homeassistant": True, "homeassistant_exclude_database": True, "name": name},
                   timeout=1800)

    def render_template(self, template: str) -> str:
        """Core ``POST /api/template``: renders read-only; templates cannot change state."""
        _guard("POST", "/core/api/template")
        headers = {"Authorization": f"Bearer {self._token}"}
        _, data = net.request("POST", self.base + "/core/api/template", headers,
                              body={"template": template}, timeout=30, raw=True)
        return data.decode("utf-8", "replace")

    def notify(self, service: str, payload: dict) -> None:
        try:
            self._call("POST", f"/core/api/services/notify/{service}", body=payload, timeout=30)
        except net.NetError as err:
            err.what = "notify"
            raise

    def publish_status(self, state: str, attributes: dict) -> None:
        try:
            attrs = {**attributes, "instance": INSTANCE} if INSTANCE else attributes
            self._call("POST", f"/core/api/states/{STATUS_ENTITY}",
                       body={"state": state[:250], "attributes": attrs}, timeout=15)
        except (net.NetError, HAError):
            pass  # visibility only; never blocks or alters a transaction

    def set_marker(self, marker: str, request_id: str) -> None:
        """Write a per-transaction marker into the (API-only) status entity.

        States created through the REST API are not restored by Home Assistant,
        so the marker disappears whenever Core restarts. Its absence later proves
        Core restarted (and may have loaded new files) during the transaction.
        """
        self._call("POST", f"/core/api/states/{STATUS_ENTITY}",
                   body={"state": "DEPLOYING", "attributes": {"txn_marker": marker, "request_id": request_id,
                                                              "warning": INSTALL_WARNING,
                                                              **({"instance": INSTANCE} if INSTANCE else {})}},
                   timeout=15)

    def marker_present(self, marker: str) -> bool:
        try:
            state = self.get_state(STATUS_ENTITY)
        except Exception:  # noqa: BLE001 - unknown == assume Core restarted (conservative)
            return False
        if not state:
            return False
        return (state.get("attributes") or {}).get("txn_marker") == marker

    def wait_running(self, timeout: float, poll: float, should_stop=None) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if should_stop is not None and should_stop():
                return False          # 0.3.5 (P3): the caller sees the stop flag and leaves the journal as is
            if self.core_state() == "RUNNING":
                return True
            time.sleep(poll)
        return False

    # -- WebSocket --------------------------------------------------------
    def ws(self) -> CoreSocket:
        return CoreSocket(self.ws_url, self._token)


class CoreSocket:
    """Synchronous Core WebSocket session restricted to the allowlist."""

    def __init__(self, url: str, token: str) -> None:
        from websockets.sync.client import connect

        self._conn = connect(url, open_timeout=20, close_timeout=5, max_size=4 * 1024 * 1024)
        self._next = 1
        hello = self._recv(20)
        if hello.get("type") != "auth_required":
            raise HAError("WS_HANDSHAKE")
        self._conn.send(json.dumps({"type": "auth", "access_token": token}))
        if self._recv(20).get("type") != "auth_ok":
            raise HAError("WS_AUTH")

    def _recv(self, timeout: float) -> dict:
        raw = self._conn.recv(timeout=timeout)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise HAError("WS_SHAPE")
        return data

    def send(self, msg: dict) -> int:
        _ws_guard(msg)
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
