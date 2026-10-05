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
* problem detection (0.2.0) adds read-only routes (host disk, backup list) and three read-only
  Core WebSocket commands (Repairs list, integration entries, system log), all projected to
  bounded fields. A one-tap fix opens exactly one mutating route for one target inside
  ``with ha.fix_target(kind, ref)``: restart/start of one App, apply of one Supervisor
  suggestion, or reload of one integration entry. Never ``self``, never an update route.
* network discovery check (0.2.3) adds one read-only Core WebSocket subscription
  (``ssdp/subscribe_discovery``: what Home Assistant currently hears announced on the network).
  It is read for a few seconds, projected to device types and identities only (addresses,
  locations and headers are dropped at once), and the socket is closed.

* checks added in 0.5.0 use four more read-only Core WebSocket commands: ``backup/info`` (projected to
  each backup's date, storage locations and whether it includes Home Assistant), and ``get_states`` with the
  entity and device registries (projected to entity id, state, last change, device class, unit, platform,
  device id, device name and disabled flags; attributes and everything else are dropped at once).

* 0.5.1 adds one more read-only Core WebSocket command, ``config_entries/flow/progress``, projected
  to the entry ids of pending re-login ("reauth") flows only, and reads backup sizes from ``/backups``.

* 0.5.2 (Recovery Report follow-ups, all read-only): one more pinned state read,
  ``sensor.house_brain_deployer_status`` (projected to its state, a validated request id, ``dry_run`` and the
  request id / outcome of its ``last_result``; nothing else), a few named attributes of the Connection
  Forensics sensor (``last_start``, ``stop_to_ready_seconds`` as integers), and ``mesh_counts``: the already
  allowed ``get_states`` and entity registry reads, reduced at once to per-mesh counts (ZHA, Z-Wave JS,
  Matter: entities and how many are unavailable). No new route kind and no write.

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
RE_UUID_HEX = re.compile(r"^[a-f0-9]{32}$")
RE_ENTRY_ID = re.compile(r"^[A-Za-z0-9]{20,40}$")
RE_UDN = re.compile(r"^uuid:[A-Za-z0-9._:-]{1,100}$")
RE_ST = re.compile(r"^[A-Za-z0-9._:/-]{1,120}$")
SSDP_READ_SECONDS = 4.0
SSDP_MAX_ROWS = 400
RE_SWITCH = re.compile(r"^switch\.[a-z0-9_]{1,80}$")
FIX_KINDS = frozenset({"restart_app", "start_app", "apply_suggestion", "reload_entry", "power_cycle"})
SAFE_STATES = frozenset({"startup", "started", "stopped", "unknown", "error"})
PRIVILEGE_FIELDS = ("hassio_role", "full_access", "host_network", "host_pid", "docker_api", "auth_api",
                    "homeassistant_api", "apparmor")
_WS_ALLOW = frozenset({"subscribe_events", "unsubscribe_events", "config/auth/list",
                       "repairs/list_issues", "config_entries/get", "system_log/list",
                       "ssdp/subscribe_discovery",
                       # 0.5.0 read-only checks: backup locations, low batteries / offline devices.
                       "backup/info", "get_states", "config/entity_registry/list",
                       "config/device_registry/list",
                       # 0.5.1: pending "log in again" flows (read-only list; never starts or answers one).
                       "config_entries/flow/progress"})
RE_ENTITY_ID = re.compile(r"^[a-z0-9_]{1,40}\.[a-z0-9_]{1,200}$")
RE_DEVICE_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")
RE_AGENT_ID = re.compile(r"^[a-z0-9_]{1,40}\.[A-Za-z0-9_.-]{1,80}$")
DEVICE_SNAPSHOT_MAX_BYTES = 32 * 1024 * 1024

# Recovery Report (0.3.0): exact entities it may read, nothing else (read-only).
FORENSICS_ENTITY = "sensor.house_brain_connection_forensics_last_restart"
DEPLOYER_STATUS_ENTITY = "sensor.house_brain_deployer_status"      # 0.5.2: PLANNED vs UNPLANNED_CLEAN join
RECOVERY_READ_ENTITIES = frozenset({
    FORENSICS_ENTITY, DEPLOYER_STATUS_ENTITY,
    "sensor.house_brain_network_outage_class", "sensor.house_brain_network_outage_summary",
    "sensor.house_brain_network_wan_status", "sensor.ups_status_data", "sensor.ups_battery_charge",
    "binary_sensor.any_smoke_detected", "binary_sensor.any_co_detected", "climate.ecobee_thermostat",
    "sensor.sense_51446_l1_voltage", "sensor.enphase_solar_power_now",
})
RECOVERY_NOTIFICATION_ID = "hbm_recovery_report"
# 0.5.2: Deployer request ids (``hbd/manifest.py`` RE_REQUEST_ID, optionally the Undo suffix) and the
# ``last_result`` text it publishes ("<request id>: <OUTCOME>").
RE_DEPLOYER_RID = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}(?:#undo)?$")
RE_DEPLOYER_LAST = re.compile(r"^([a-z0-9][a-z0-9-]{2,63}(?:#undo)?): ([A-Z][A-Z_]{1,39})$")
MESH_PLATFORMS = ("zha", "zwave_js", "matter")


def _whole(v: Any) -> int | None:
    """An integer attribute (epoch seconds, a duration), or None. Booleans and fractions are refused."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and v.isdigit() and len(v) <= 12:
        return int(v)
    return None


def project_attrs(entity_id: str, attrs: Any) -> dict:
    """The few named attributes the Recovery Report may keep (0.5.2); every other attribute is dropped."""
    attrs = attrs if isinstance(attrs, dict) else {}
    if entity_id == FORENSICS_ENTITY:
        return {"last_start": _whole(attrs.get("last_start")),
                "stop_to_ready_seconds": _whole(attrs.get("stop_to_ready_seconds"))}
    if entity_id == DEPLOYER_STATUS_ENTITY:
        rid, last = attrs.get("request_id"), attrs.get("last_result")
        m = RE_DEPLOYER_LAST.fullmatch(last) if isinstance(last, str) else None
        return {"request_id": rid if isinstance(rid, str) and RE_DEPLOYER_RID.fullmatch(rid) else None,
                "dry_run": attrs.get("dry_run") is True,
                "last_rid": m.group(1) if m else None, "last_outcome": m.group(2) if m else None}
    return {}

_STATIC_ALLOW: tuple[tuple[str, str], ...] = (
    ("GET", r"/addons/self/info"),
    ("GET", r"/addons"),
    ("GET", r"/addons/[a-z0-9][a-z0-9_]{0,99}/info"),
    ("GET", r"/addons/[a-z0-9][a-z0-9_]{0,99}/changelog"),
    ("GET", r"/store/addons/[a-z0-9][a-z0-9_]{0,99}"),
    ("GET", r"/core/info"),
    ("GET", r"/resolution/info"),
    ("GET", r"/host/info"),
    ("GET", r"/backups"),
    ("GET", r"/core/api/"),
    ("POST", r"/core/api/services/notify/mobile_app_[a-z0-9_]{1,80}"),
    ("POST", r"/core/api/states/" + re.escape(STATUS_ENTITY)),
    # Recovery Report (0.3.0), read-only except its own persistent notification:
    ("GET", r"/core/logs"),
    ("GET", r"/core/logs/boots/-1"),
    ("GET", r"/core/api/states/(?:" + "|".join(re.escape(e) for e in sorted(RECOVERY_READ_ENTITIES)) + r")"),
    ("POST", r"/core/api/services/persistent_notification/create"),
    ("POST", r"/core/api/services/persistent_notification/dismiss"),
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


def _ssdp_row(item: dict) -> dict:
    """Project one discovery record: device type, identity, model/maker names. No addresses."""
    upnp = item.get("upnp") if isinstance(item.get("upnp"), dict) else {}
    def text(v: Any, n: int) -> str:
        return re.sub(r"[^A-Za-z0-9 ()._+-]", "", v)[:n] if isinstance(v, str) else ""
    st, udn = item.get("ssdp_st"), item.get("ssdp_udn")
    return {"st": st if isinstance(st, str) and RE_ST.fullmatch(st) else "",
            "udn": udn if isinstance(udn, str) and RE_UDN.fullmatch(udn) else "",
            "name": text(upnp.get("friendlyName"), 60), "model": text(upnp.get("modelName"), 40),
            "maker": text(upnp.get("manufacturer"), 40)}


class HomeAssistant:
    def __init__(self, base: str, token: str, ws_url: str, scout_slug: str, observer_slug: str,
                 power_cycle_entity: str = "") -> None:
        if power_cycle_entity and not RE_SWITCH.fullmatch(power_cycle_entity):
            raise ValueError("invalid power-cycle switch")
        # The one switch an owner-approved power cycle may toggle (0.4.0, owner decision 2026-10-02).
        self.power_cycle_entity = power_cycle_entity
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
        ) + ((("GET", r"/core/api/states/" + re.escape(power_cycle_entity)),) if power_cycle_entity else ())
        self._allow = tuple((m, re.compile(p)) for m, p in routes)
        self._target: str | None = None
        self._fix: tuple[str, str] | None = None
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
        fix = self._fix
        if fix is not None and method == "POST":
            kind, ref = fix
            allowed = {
                "restart_app": {f"/addons/{ref}/restart"},
                "start_app": {f"/addons/{ref}/start"},
                "apply_suggestion": {f"/resolution/suggestion/{ref}"},
                "reload_entry": {"/core/api/services/homeassistant/reload_config_entry"},
                "power_cycle": {"/core/api/services/switch/turn_off", "/core/api/services/switch/turn_on"},
            }[kind]
            if path in allowed:
                return
        raise ForbiddenCall(f"{method} {path}")

    @contextmanager
    def fix_target(self, kind: str, ref: str):
        """Open exactly one owner-approved fix route for one target."""
        if kind not in FIX_KINDS:
            raise ForbiddenCall(f"fix kind {kind}")
        if kind in ("restart_app", "start_app"):
            if not RE_SLUG.fullmatch(ref) or ref in ("self", self.self_slug(), self.scout):
                raise ForbiddenCall(f"fix target {ref}")
            if self.app_boot(ref) != "auto":
                # Checked again at fix time: never restart/start a manual-start App.
                raise ForbiddenCall(f"fix target {ref} is not started at boot")
        elif kind == "apply_suggestion" and not RE_UUID_HEX.fullmatch(ref):
            raise ForbiddenCall("fix suggestion id")
        elif kind == "reload_entry" and not RE_ENTRY_ID.fullmatch(ref):
            raise ForbiddenCall("fix entry id")
        elif kind == "power_cycle" and (not self.power_cycle_entity or ref != self.power_cycle_entity):
            raise ForbiddenCall("power-cycle target is not the configured switch")
        if self._fix is not None or self._target is not None:
            raise ForbiddenCall("nested target")
        self._fix = (kind, ref)
        try:
            yield
        finally:
            self._fix = None

    @contextmanager
    def update_target(self, slug: str):
        """Open the mutating update routes for exactly one App for the duration of a job."""
        if not RE_SLUG.fullmatch(slug) or slug == "self" or slug == self.self_slug():
            raise ForbiddenCall(f"update target {slug}")
        if self._target is not None or self._fix is not None:
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
              raw: bool = False, query: str = "") -> Any:
        self._guard(method, path)
        if query and not re.fullmatch(r"\?lines=[0-9]{1,4}", query):
            raise ForbiddenCall(f"query {query[:40]}")
        headers = {"Authorization": f"Bearer {self._token}"}
        if raw:
            headers["Accept"] = "text/plain"
        _, data = net.request(method, self.base + path + query, headers, body=body, timeout=timeout, raw=raw)
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

    # -- problem detection (read-only, projected) ----------------------------------------
    def resolution(self) -> dict:
        """Supervisor issues/suggestions/unhealthy/unsupported as bounded identifier tuples."""
        data = self._supervisor("GET", "/resolution/info")
        data = data if isinstance(data, dict) else {}
        def ident(v: Any) -> str:
            return re.sub(r"[^A-Za-z0-9_.:-]", "", str(v or ""))[:60]
        def rows(key: str, auto: bool) -> list[dict]:
            out = []
            for row in (data.get(key) or [])[:50]:
                if isinstance(row, dict) and isinstance(row.get("uuid"), str) and RE_UUID_HEX.fullmatch(row["uuid"]):
                    item = {"uuid": row["uuid"], "type": ident(row.get("type")),
                            "context": ident(row.get("context")), "reference": ident(row.get("reference"))}
                    if auto:
                        item["auto"] = row.get("auto") is True
                    out.append(item)
            return out
        return {"unhealthy": sorted({ident(v) for v in (data.get("unhealthy") or [])[:30] if isinstance(v, str)}),
                "unsupported": sorted({ident(v) for v in (data.get("unsupported") or [])[:30] if isinstance(v, str)}),
                "issues": rows("issues", False), "suggestions": rows("suggestions", True)}

    def disk(self) -> tuple[float, float] | None:
        """(total GB, free GB) of the data disk, or None when not reported."""
        data = self._supervisor("GET", "/host/info")
        total, free = (data.get("disk_total"), data.get("disk_free")) if isinstance(data, dict) else (None, None)
        if isinstance(total, (int, float)) and isinstance(free, (int, float)) and total > 0 and 0 <= free <= total:
            return float(total), float(free)
        return None

    def backup_list(self) -> list[tuple[str, str, float | None]]:
        """(type, ISO date, size GB or None) of every backup; names and contents are not kept.

        A backup that includes Home Assistant itself counts as ``full``: Home Assistant's own
        automatic backups are stored by Supervisor as type ``partial`` with everything selected.
        """
        data = self._supervisor("GET", "/backups")
        out = []
        for row in ((data.get("backups") or []) if isinstance(data, dict) else [])[:500]:
            if isinstance(row, dict) and isinstance(row.get("date"), str):
                content = row.get("content") if isinstance(row.get("content"), dict) else {}
                kind = "full" if row.get("type") == "full" or content.get("homeassistant") is True else "partial"
                size_b, size_mb = row.get("size_bytes"), row.get("size")
                gb = (size_b / 1024 ** 3 if isinstance(size_b, int) and not isinstance(size_b, bool) and size_b > 0
                      else size_mb / 1024 if isinstance(size_mb, (int, float)) and not isinstance(size_mb, bool)
                      and size_mb > 0 else None)
                out.append((kind, row["date"][:40], round(gb, 3) if gb else None))
        return out

    def app_boot(self, slug: str) -> str:
        """``auto`` only when the App starts at boot and is not ``manual_only``; anything else is manual."""
        data = self._supervisor("GET", f"/addons/{slug}/info")
        if not isinstance(data, dict):
            return "unknown"
        if data.get("boot_config") == "manual_only":
            return "manual"
        boot = data.get("boot")
        return boot if boot in ("auto", "manual") else "unknown"

    def app_state(self, slug: str) -> str:
        data = self._supervisor("GET", f"/addons/{slug}/info")
        state = data.get("state") if isinstance(data, dict) else None
        return state if state in SAFE_STATES else "unknown"

    def core_problems(self) -> dict:
        """Repairs issues, integration entries and system-log errors over one Core socket."""
        sock = self.ws()
        try:
            repairs = sock.command({"type": "repairs/list_issues"})
            entries = sock.command({"type": "config_entries/get"})
            log = sock.command({"type": "system_log/list"})
            try:
                flows = sock.command({"type": "config_entries/flow/progress"})
            except HAError:
                flows = []                            # older Core / not allowed: no re-login information
        finally:
            sock.close()
        def s(v: Any, n: int) -> str:
            return str(v)[:n] if isinstance(v, (str, int, float)) else ""
        out_repairs = []
        for row in ((repairs or {}).get("issues") or [])[:200] if isinstance(repairs, dict) else []:
            if isinstance(row, dict):
                out_repairs.append({k: s(row.get(k), 120) for k in (
                    "domain", "issue_id", "severity", "translation_key", "learn_more_url",
                    "breaks_in_ha_version", "dismissed_version")} |
                    {"is_fixable": row.get("is_fixable") is True, "ignored": row.get("ignored") is True})
        out_entries = []
        for row in (entries or [])[:500] if isinstance(entries, list) else []:
            if isinstance(row, dict) and isinstance(row.get("entry_id"), str) and RE_ENTRY_ID.fullmatch(row["entry_id"]):
                out_entries.append({"entry_id": row["entry_id"], "domain": s(row.get("domain"), 60),
                                    "title": s(row.get("title"), 80), "state": s(row.get("state"), 30),
                                    "reason": s(row.get("reason"), 200), "disabled": bool(row.get("disabled_by"))})
        out_log = []
        for row in (log or [])[:100] if isinstance(log, list) else []:
            if isinstance(row, dict) and row.get("level") in ("ERROR", "CRITICAL"):
                msg = row.get("message")
                first = msg[0] if isinstance(msg, list) and msg else msg
                src = row.get("source")
                source = f"{s(src[0], 120)}:{s(src[1], 8)}" if isinstance(src, list) and len(src) == 2 else ""
                exc = row.get("exception")
                out_log.append({"name": s(row.get("name"), 120), "level": row["level"], "message": s(first, 400),
                                "source": source, "count": row.get("count") if isinstance(row.get("count"), int) else 1,
                                "exception": "\n".join(str(exc).strip().splitlines()[-3:])[:600] if exc else ""})
        reauth = sorted({str(f["context"]["entry_id"]) for f in (flows if isinstance(flows, list) else [])[:200]
                         if isinstance(f, dict) and isinstance(f.get("context"), dict)
                         and f["context"].get("source") == "reauth"
                         and isinstance(f["context"].get("entry_id"), str)
                         and RE_ENTRY_ID.fullmatch(f["context"]["entry_id"])})
        return {"repairs": out_repairs, "entries": out_entries, "log": out_log, "reauth": reauth}

    def ssdp_heard(self, seconds: float = SSDP_READ_SECONDS) -> list[dict]:
        """What Home Assistant hears announced on the network right now (read-only).

        Subscribing replays Home Assistant's whole discovery cache at once. Each row keeps only
        the announced device type (ST), the device identity (UDN) and its model/maker names;
        IP addresses, locations and raw headers are dropped before anything is kept.
        """
        sock = self.ws()
        rows: list[dict] = []
        try:
            sock.command({"type": "ssdp/subscribe_discovery"}, timeout=20)
            deadline = time.monotonic() + seconds
            while len(rows) < SSDP_MAX_ROWS:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                event = sock.next_event(remaining)
                if event is None:
                    break
                for item in (event.get("add") or []) if isinstance(event, dict) else []:
                    if isinstance(item, dict):
                        rows.append(_ssdp_row(item))
        finally:
            sock.close()
        return rows[:SSDP_MAX_ROWS]

    # -- owner-approved fixes (only inside fix_target) ----------------------------------
    def restart_app(self, slug: str) -> None:
        self._supervisor("POST", f"/addons/{slug}/restart", body={}, timeout=300)

    def start_app(self, slug: str) -> None:
        self._supervisor("POST", f"/addons/{slug}/start", body={}, timeout=300)

    def apply_suggestion(self, uuid: str) -> None:
        self._supervisor("POST", f"/resolution/suggestion/{uuid}", body={}, timeout=600)

    def reload_entry(self, entry_id: str) -> None:
        self._call("POST", "/core/api/services/homeassistant/reload_config_entry",
                   body={"entry_id": entry_id}, timeout=120)

    def switch_power(self, entity_id: str, on: bool) -> None:
        """Turn the configured power-cycle switch on/off; only inside ``fix_target("power_cycle", it)``."""
        if self._fix != ("power_cycle", entity_id) or entity_id != self.power_cycle_entity:
            raise ForbiddenCall(f"switch {entity_id}")
        self._call("POST", f"/core/api/services/switch/turn_{'on' if on else 'off'}",
                    body={"entity_id": entity_id}, timeout=30)

    def switch_state(self, entity_id: str) -> str:
        if entity_id != self.power_cycle_entity or not entity_id:
            raise ForbiddenCall(f"state {entity_id}")
        data = self._call("GET", f"/core/api/states/{entity_id}", timeout=15)
        state = data.get("state") if isinstance(data, dict) else None
        return state if state in ("on", "off", "unavailable", "unknown") else "unknown"

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

    # -- Recovery Report (0.3.0): read-only + its own persistent notification ----------
    def entity_state(self, entity_id: str) -> dict | None:
        """``{"state", "last_changed"}`` of one pinned entity, or None when it does not exist."""
        if entity_id not in RECOVERY_READ_ENTITIES:
            raise ForbiddenCall(f"entity {entity_id[:80]}")
        try:
            data = self._call("GET", f"/core/api/states/{entity_id}", timeout=15)
        except net.NetError as err:
            if err.status == 404:
                return None
            raise
        if not isinstance(data, dict):
            return None
        state = data.get("state")
        return {"state": str(state)[:255] if state is not None else None,
                "last_changed": str(data.get("last_changed") or "")[:40],
                "attrs": project_attrs(entity_id, data.get("attributes"))}

    def host_boot_info(self) -> tuple[str | None, str | None]:
        """(host boot timestamp, operating system string) from ``/host/info``."""
        data = self._supervisor("GET", "/host/info")
        boot = data.get("boot_timestamp") if isinstance(data, dict) else None
        osv = data.get("operating_system") if isinstance(data, dict) else None
        return (str(boot)[:20] if isinstance(boot, (int, str)) and str(boot).isdigit() else None,
                str(osv)[:60] if isinstance(osv, str) else None)

    def core_log_tail(self, *, previous_boot: bool, lines: int = 1500) -> str:
        """Plain-text Core log tail of this boot or of the previous boot (bounded, 20 s)."""
        path = "/core/logs/boots/-1" if previous_boot else "/core/logs"
        data = self._call("GET", path, timeout=20, raw=True, query=f"?lines={min(max(lines, 2), 2000)}")
        return bytes(data or b"")[:524288].decode("utf-8", "replace")

    def persistent_note(self, notification_id: str, title: str, message: str) -> None:
        if notification_id != RECOVERY_NOTIFICATION_ID:
            raise ForbiddenCall("notification id")
        self._call("POST", "/core/api/services/persistent_notification/create",
                   body={"notification_id": notification_id, "title": title[:120], "message": message[:900]},
                   timeout=15)

    def persistent_dismiss(self, notification_id: str) -> None:
        if notification_id != RECOVERY_NOTIFICATION_ID:
            raise ForbiddenCall("notification id")
        self._call("POST", "/core/api/services/persistent_notification/dismiss",
                   body={"notification_id": notification_id}, timeout=15)

    def publish_status(self, state: str, attributes: dict) -> None:
        try:
            self._call("POST", f"/core/api/states/{STATUS_ENTITY}",
                       body={"state": state[:250], "attributes": attributes}, timeout=15)
        except (net.NetError, HAError, ForbiddenCall):
            pass  # visibility only

    # -- 0.5.0 read-only checks ------------------------------------------------
    def backup_locations(self) -> list[dict]:
        """Every backup as ``{"date", "agents": [location ids], "ha": bool}`` (names and contents dropped)."""
        sock = self.ws()
        try:
            info = sock.command({"type": "backup/info"})
        finally:
            sock.close()
        out = []
        for row in ((info or {}).get("backups") or [])[:500] if isinstance(info, dict) else []:
            if not isinstance(row, dict) or not isinstance(row.get("date"), str):
                continue
            agents = row.get("agents")
            ids = list(agents) if isinstance(agents, dict) else row.get("agent_ids")
            ids = [a for a in (ids if isinstance(ids, list) else []) if isinstance(a, str) and RE_AGENT_ID.fullmatch(a)]
            ha_included = row.get("homeassistant_included")
            out.append({"date": row["date"][:40], "agents": sorted(set(ids))[:20],
                        "ha": ha_included is not False})
        return out

    def device_health(self) -> dict:
        """Projected states + entity/device registries for the low-battery / offline sweep (read-only)."""
        sock = self.ws(max_size=DEVICE_SNAPSHOT_MAX_BYTES)
        try:
            states = sock.command({"type": "get_states"}, timeout=60)
            entities = sock.command({"type": "config/entity_registry/list"}, timeout=60)
            devices = sock.command({"type": "config/device_registry/list"}, timeout=60)
        finally:
            sock.close()
        def text(v: Any, n: int) -> str | None:
            return str(v)[:n] if isinstance(v, (str, int, float)) and not isinstance(v, bool) else None
        out_states = []
        for row in (states if isinstance(states, list) else [])[:20000]:
            if not isinstance(row, dict) or not isinstance(row.get("entity_id"), str) \
                    or not RE_ENTITY_ID.fullmatch(row["entity_id"]):
                continue
            attrs = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
            out_states.append({"entity_id": row["entity_id"], "state": text(row.get("state"), 40),
                               "last_changed": text(row.get("last_changed"), 40),
                               "device_class": text(attrs.get("device_class"), 40),
                               "unit": text(attrs.get("unit_of_measurement"), 10)})
        registry = {}
        for row in (entities if isinstance(entities, list) else [])[:20000]:
            if isinstance(row, dict) and isinstance(row.get("entity_id"), str) and RE_ENTITY_ID.fullmatch(row["entity_id"]):
                dev = row.get("device_id")
                registry[row["entity_id"]] = {
                    "platform": text(row.get("platform"), 60),
                    "device_id": dev if isinstance(dev, str) and RE_DEVICE_ID.fullmatch(dev) else None,
                    "disabled": bool(row.get("disabled_by"))}
        out_devices = {}
        for row in (devices if isinstance(devices, list) else [])[:10000]:
            if isinstance(row, dict) and isinstance(row.get("id"), str) and RE_DEVICE_ID.fullmatch(row["id"]):
                out_devices[row["id"]] = {"name": text(row.get("name_by_user") or row.get("name"), 80),
                                          "disabled": bool(row.get("disabled_by"))}
        return {"states": out_states, "registry": registry, "devices": out_devices}

    def mesh_counts(self) -> dict[str, dict[str, int]]:
        """0.5.2: per mesh platform present (ZHA, Z-Wave JS, Matter), how many enabled entities it has and
        how many are unavailable or not loaded yet. Only these counts leave this method (read-only)."""
        sock = self.ws(max_size=DEVICE_SNAPSHOT_MAX_BYTES)
        try:
            states = sock.command({"type": "get_states"}, timeout=60)
            entities = sock.command({"type": "config/entity_registry/list"}, timeout=60)
        finally:
            sock.close()
        live: dict[str, Any] = {}
        for row in (states if isinstance(states, list) else [])[:20000]:
            if isinstance(row, dict) and isinstance(row.get("entity_id"), str):
                live[row["entity_id"]] = row.get("state")
        out: dict[str, dict[str, int]] = {}
        for row in (entities if isinstance(entities, list) else [])[:20000]:
            if not isinstance(row, dict) or row.get("platform") not in MESH_PLATFORMS or row.get("disabled_by") \
                    or not isinstance(row.get("entity_id"), str) or not RE_ENTITY_ID.fullmatch(row["entity_id"]):
                continue
            c = out.setdefault(str(row["platform"]), {"total": 0, "unavailable": 0})
            c["total"] += 1
            if live.get(row["entity_id"]) in (None, "unavailable"):
                c["unavailable"] += 1
        return out

    # -- WebSocket ------------------------------------------------------------
    def ws(self, max_size: int = 4 * 1024 * 1024) -> CoreSocket:
        return CoreSocket(self.ws_url, self._token, self._ws_guard, max_size=max_size)


class CoreSocket:
    """Synchronous Core WebSocket session restricted to the allowlist (Deployer lineage)."""

    def __init__(self, url: str, token: str, guard, max_size: int = 4 * 1024 * 1024) -> None:
        from websockets.sync.client import connect

        self._guard = guard
        self._conn = connect(url, open_timeout=20, close_timeout=5, max_size=max_size)
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
