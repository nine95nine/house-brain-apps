"""Supervisor and Home Assistant Core access through a single allowlisted gate.

The App token (``SUPERVISOR_TOKEN``) can technically reach more than the
Deployer needs. Every HTTP call and every WebSocket command therefore passes
through ``_guard``/``_ws_guard``: anything not on the explicit allowlist raises
``ForbiddenCall`` before any I/O. Only stable v1 Supervisor paths are used;
the ``ha`` CLI is never invoked (see the #223 R4 ``apps``/``addons`` escape).
"""
from __future__ import annotations

import calendar
import hashlib
import hmac
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
    # 0.3.30 (owner pop-up 2026-10-19 "Yes, one tap"): the Companion setup only, after the owner's approval. Every
    # body is pinned by _body_guard below: one integration (llama_cpp), one URL, one model, no control API.
    ("POST", re.compile(r"^/core/api/config/config_entries/flow$")),
    # 0.3.32: a config flow id is a 26-character ULID (Core ConfigEntriesFlowManager.async_init, ulid_now()); a
    # subentry flow id stays 32 hex (base FlowManager, random_uuid_hex()).
    ("POST", re.compile(r"^/core/api/config/config_entries/flow/[0-9A-Z]{26}$")),
    ("POST", re.compile(r"^/core/api/config/config_entries/subentries/flow$")),
    ("POST", re.compile(r"^/core/api/config/config_entries/subentries/flow/[0-9a-f]{32}$")),
    ("DELETE", re.compile(r"^/core/api/config/config_entries/entry/[0-9A-Z]{26}$")),  # undo of this run's entry only
    # 0.3.34 (#215, owner pop-up 2026-10-24): one read-only history window for an owner-approved lookup. GET only;
    # the ids and window are checked again in history_period() (at most 16 ids, at most 7 days).
    ("GET", re.compile(r"^/core/api/history/period/" + r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
                       + r"\?filter_entity_id=[a-z0-9_.,]{1,2400}&end_time=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
                       + r"&minimal_response&no_attributes$")),
)
RE_HISTORY_ENTITY = re.compile(r"^[a-z_]{2,32}\.[a-z0-9_]{1,96}$")
MAX_HISTORY_IDS = 16
MAX_HISTORY_WINDOW_S = 7 * 86400
# 0.3.38: Recorder long-term statistics (read-only). One window per owner-approved lookup; the window is bounded per
# period so a reply stays small (hour: 31 days, day: 366 days, month: 3 years).
STATISTICS_PERIODS = {"hour": 31 * 86400, "day": 366 * 86400, "month": 1096 * 86400}
STATISTICS_TYPES = frozenset({"mean", "min", "max", "change"})
RE_WS_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_WS_ALLOW = frozenset({"subscribe_events", "unsubscribe_events", "system_log/list", "config/auth/list",
                       "http/config", "repairs/list_issues",
                       "zwave_js/get_config_parameters",  # 0.3.29: read-only, one device per owner-approved lookup
                       "recorder/statistics_during_period",  # 0.3.38: read-only, one window per owner-approved lookup
                       # 0.3.30: Companion setup (reads, plus one pinned pipeline create)
                       "config_entries/subentries/list", "config/entity_registry/list",
                       "assist_pipeline/pipeline/list", "assist_pipeline/pipeline/create"})
RE_DEVICE_ID = re.compile(r"^[0-9a-f]{32}$")
RE_ENTRY_ID = re.compile(r"^[0-9A-Z]{26}$")
RE_CONVERSATION_ENTITY = re.compile(r"^conversation\.[a-z0-9_]{1,96}$")
APPROVAL_EVENT = "mobile_app_notification_action"

# 0.3.30: the only values the Companion setup can ever send. Literals, never from a request, except the
# Worker URL: the public store refuses a concrete Cloudflare host, so the set-up request carries it and only
# the URL whose SHA-256 is pinned here is ever accepted.
COMPANION_URL_SHA256 = "d67b9516dad8c2833917503af83d0ae50e1125ef1b61fc9d4a82d6b416137a72"
COMPANION_MODEL = "house-brain-companion"
COMPANION_PIPELINE = "House Brain Companion"
COMPANION_DOMAIN = "llama_cpp"
PIPELINE_KEYS = frozenset({"conversation_engine", "conversation_language", "language", "name", "stt_engine",
                           "stt_language", "tts_engine", "tts_language", "tts_voice", "wake_word_entity",
                           "wake_word_id", "prefer_local_intents"})
RE_COMPANION_KEY = re.compile(r"^[0-9a-f]{48}$")


def companion_url_ok(url: Any) -> bool:
    return (isinstance(url, str) and url.startswith("https://") and len(url) <= 200
            and hmac.compare_digest(hashlib.sha256(url.encode("utf-8")).hexdigest(), COMPANION_URL_SHA256))


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


_FLOW = re.compile(r"^/core/api/config/config_entries/flow/[0-9A-Z]{26}$")
_SUB_FLOW = re.compile(r"^/core/api/config/config_entries/subentries/flow/[0-9a-f]{32}$")


def _body_guard(method: str, path: str, body: Any) -> None:
    """0.3.30: the config-flow paths accept only the Companion's fixed bodies (checked before any I/O)."""
    if path == "/core/api/config/config_entries/flow":
        if body != {"handler": COMPANION_DOMAIN, "show_advanced_options": False}:
            raise ForbiddenCall("config flow for another integration")
    elif _FLOW.fullmatch(path):
        if not isinstance(body, dict) or not body or not set(body) <= {"base_url", "api_key", "chat_model"}:
            raise ForbiddenCall("config flow step shape")
        if "base_url" in body and (not companion_url_ok(body["base_url"]) or set(body) != {"base_url", "api_key"}
                                   or not RE_COMPANION_KEY.fullmatch(str(body.get("api_key")))):
            raise ForbiddenCall("config flow server step")
        if "chat_model" in body and body != {"chat_model": COMPANION_MODEL}:
            raise ForbiddenCall("config flow model step")
    elif path == "/core/api/config/config_entries/subentries/flow":
        handler = body.get("handler") if isinstance(body, dict) else None
        if (not isinstance(body, dict) or set(body) != {"handler", "subentry_id"} or not isinstance(handler, list)
                or len(handler) != 2 or not RE_ENTRY_ID.fullmatch(str(handler[0])) or handler[1] != "conversation"
                or not RE_ENTRY_ID.fullmatch(str(body["subentry_id"]))):
            raise ForbiddenCall("subentry flow shape")
    elif _SUB_FLOW.fullmatch(path):
        # The re-save that removes control: recommended settings, the Companion model, and NO llm_hass_api key.
        if body != {"recommended": True, "chat_model": COMPANION_MODEL}:
            raise ForbiddenCall("subentry step must be exactly recommended + model (no control API)")
    elif method == "DELETE" and body is not None:
        raise ForbiddenCall("delete takes no body")


def _ws_guard(msg: dict) -> None:
    kind = msg.get("type")
    if kind not in _WS_ALLOW:
        raise ForbiddenCall(f"ws {kind}")
    if kind == "subscribe_events" and msg.get("event_type") != APPROVAL_EVENT:
        raise ForbiddenCall("ws subscribe to non-approval event")
    if kind in ("http/config", "repairs/list_issues") and set(msg) != {"type"}:
        raise ForbiddenCall("ws read takes no parameters")
    if kind == "zwave_js/get_config_parameters" and (
            set(msg) != {"type", "device_id"} or not isinstance(msg.get("device_id"), str)
            or not RE_DEVICE_ID.fullmatch(msg["device_id"])):
        raise ForbiddenCall("ws zwave config read takes exactly one device id")
    if kind == "recorder/statistics_during_period":
        _statistics_guard(msg)
    if kind in ("config/entity_registry/list", "assist_pipeline/pipeline/list") and set(msg) != {"type"}:
        raise ForbiddenCall("ws read takes no parameters")
    if kind == "config_entries/subentries/list" and (
            set(msg) != {"type", "entry_id"} or not RE_ENTRY_ID.fullmatch(str(msg.get("entry_id")))):
        raise ForbiddenCall("ws subentry list takes exactly one entry id")
    if kind == "assist_pipeline/pipeline/create":
        fields = {k: v for k, v in msg.items() if k != "type"}
        if (set(fields) != PIPELINE_KEYS or fields["name"] != COMPANION_PIPELINE
                or not RE_CONVERSATION_ENTITY.fullmatch(str(fields["conversation_engine"]))
                or fields["prefer_local_intents"] is not False
                or fields["wake_word_entity"] is not None or fields["wake_word_id"] is not None):
            raise ForbiddenCall("ws pipeline create must be the Companion pipeline")


def _statistics_guard(msg: dict) -> None:
    """0.3.38: exactly the read the statistics lookup builds: ≤ 16 entity-shaped ids, a known period, known types,
    a UTC window within the period's bound. No units conversion, no other field."""
    if set(msg) != {"type", "start_time", "end_time", "statistic_ids", "period", "types"}:
        raise ForbiddenCall("ws statistics fields")
    ids, types, period = msg.get("statistic_ids"), msg.get("types"), msg.get("period")
    if (not isinstance(ids, list) or not ids or len(ids) > MAX_HISTORY_IDS or len(set(map(str, ids))) != len(ids)
            or not all(isinstance(i, str) and RE_HISTORY_ENTITY.fullmatch(i) for i in ids)):
        raise ForbiddenCall("ws statistics ids")
    if period not in STATISTICS_PERIODS:
        raise ForbiddenCall("ws statistics period")
    if (not isinstance(types, list) or not types or len(set(map(str, types))) != len(types)
            or not all(isinstance(t, str) and t in STATISTICS_TYPES for t in types)):
        raise ForbiddenCall("ws statistics types")
    start, end = msg.get("start_time"), msg.get("end_time")
    if not (isinstance(start, str) and isinstance(end, str) and RE_WS_TIME.fullmatch(start) and RE_WS_TIME.fullmatch(end)):
        raise ForbiddenCall("ws statistics window")
    span = calendar.timegm(time.strptime(end, "%Y-%m-%dT%H:%M:%SZ")) - calendar.timegm(
        time.strptime(start, "%Y-%m-%dT%H:%M:%SZ"))
    if not 0 < span <= STATISTICS_PERIODS[period]:
        raise ForbiddenCall("ws statistics window")


class HomeAssistant:
    def __init__(self, base: str, token: str, ws_url: str) -> None:
        self.base = base.rstrip("/")
        self.ws_url = ws_url
        self._token = token
        net.register_secret(token)

    # -- HTTP -------------------------------------------------------------
    def _call(self, method: str, path: str, body: Any = None, timeout: float = 30.0) -> Any:
        _guard(method, path)
        _body_guard(method, path, body)
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

    def backup_legacy_app(self, name: str) -> None:
        """#985 backup custody: a partial backup holding ONLY the stopped legacy local Deployer App.

        Same allowlisted Core service as ``backup_partial``; no Home Assistant config, no folders.
        """
        self._call("POST", "/core/api/services/hassio/backup_partial",
                   body={"addons": ["local_house_brain_deployer"], "homeassistant": False, "name": name},
                   timeout=1800)

    def history_period(self, entity_ids, start_ts: int, end_ts: int):
        """0.3.34: Core ``GET /api/history/period`` for one owner-approved lookup. Read-only, bounded, no attributes."""
        ids = tuple(entity_ids)
        if (not ids or len(ids) > MAX_HISTORY_IDS or len(set(ids)) != len(ids)
                or not all(isinstance(e, str) and RE_HISTORY_ENTITY.fullmatch(e) for e in ids)):
            raise ForbiddenCall("history ids")
        if not (isinstance(start_ts, int) and isinstance(end_ts, int) and 0 < end_ts - start_ts <= MAX_HISTORY_WINDOW_S):
            raise ForbiddenCall("history window")
        stamp = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))  # noqa: E731
        path = (f"/core/api/history/period/{stamp(start_ts)}?filter_entity_id={','.join(ids)}"
                f"&end_time={stamp(end_ts)}&minimal_response&no_attributes")
        return self._call("GET", path, timeout=60)

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

    def http_configuration(self) -> dict:
        sock = self.ws()
        try:
            return sock.command({"type": "http/config"})
        finally:
            sock.close()

    def statistics_during_period(self, entity_ids, start_ts: int, end_ts: int, period: str, types) -> dict:
        """0.3.38: Recorder ``recorder/statistics_during_period`` for one owner-approved lookup. Read-only (it answers
        from the long-term statistics already recorded); the caller projects the result."""
        stamp = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))  # noqa: E731
        msg = {"type": "recorder/statistics_during_period", "start_time": stamp(start_ts), "end_time": stamp(end_ts),
               "statistic_ids": list(entity_ids), "period": period, "types": list(types)}
        _statistics_guard(msg)
        sock = self.ws()
        try:
            result = sock.command(msg)
        finally:
            sock.close()
        if not isinstance(result, dict):
            raise HAError("STATISTICS_SHAPE")
        return result

    def zwave_config_parameters(self, device_id: str) -> dict:
        """0.3.29: ``zwave_js/get_config_parameters`` for one device. Read-only: Z-Wave JS answers from the
        values it already holds (no radio traffic, no write). The caller projects the result."""
        sock = self.ws()
        try:
            result = sock.command({"type": "zwave_js/get_config_parameters", "device_id": device_id})
        finally:
            sock.close()
        if not isinstance(result, dict):
            raise HAError("ZWAVE_CONFIG_SHAPE")
        return result

    # -- 0.3.30 Companion setup (called only after the owner's approval; bodies pinned by _body_guard) ----
    def config_flow(self, flow_id: str | None, data: dict) -> dict:
        path = "/core/api/config/config_entries/flow" + (f"/{flow_id}" if flow_id else "")
        result = self._call("POST", path, body=data, timeout=60)
        if not isinstance(result, dict):
            raise HAError("CONFIG_FLOW_SHAPE")
        return result

    def subentry_flow(self, flow_id: str | None, data: dict) -> dict:
        path = "/core/api/config/config_entries/subentries/flow" + (f"/{flow_id}" if flow_id else "")
        result = self._call("POST", path, body=data, timeout=60)
        if not isinstance(result, dict):
            raise HAError("SUBENTRY_FLOW_SHAPE")
        return result

    def delete_entry(self, entry_id: str) -> None:
        self._call("DELETE", f"/core/api/config/config_entries/entry/{entry_id}", timeout=60)

    def ws_once(self, msg: dict, timeout: float = 30.0) -> Any:
        """One allowlisted WebSocket command on a short-lived socket."""
        sock = self.ws()
        try:
            return sock.command(msg, timeout=timeout)
        finally:
            sock.close()

    def http_repair_ids(self) -> set[str]:
        sock = self.ws()
        try:
            result = sock.command({"type": "repairs/list_issues"})
        finally:
            sock.close()
        if not isinstance(result, dict) or not isinstance(result.get("issues"), list):
            raise HAError("HTTP_REPAIR_SHAPE")
        # Only closed HTTP repair ids leave this adapter; other Repairs may contain private data.
        allowed = {"yaml_still_present_after_migration", "deprecated_yaml", "deprecated_yaml_import_error"}
        return {i["issue_id"] for i in result["issues"] if isinstance(i, dict)
                and i.get("domain") == "http" and i.get("issue_id") in allowed}


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

