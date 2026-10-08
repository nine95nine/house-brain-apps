"""Supervisor and Home Assistant Core access through one pinned allowlist.

This App holds a Supervisor **manager** token (owner decision D1, 2026-09-29). A manager
token can read every App's options (including other Apps' secrets) and can change, start,
stop or uninstall any App. The token itself is therefore never "read-only". The only
boundary is this module:

* every HTTP call and WebSocket command passes ``_guard``/``_ws_guard`` before any I/O;
* App routes are pinned at construction to the two configured target slugs (the Scout and
  the Observer). There is no generic ``/addons`` listing, no uninstall, stop, rebuild,
  stdin, security, store-repository, host, OS, network or Docker route (0.5.4 exceptions, both read-only: the
  House Brain App log read and the host boot list, below; 0.6.0 exceptions: the read-only OS info and the
  owner-approved Core/OS update inside ``system_target``, below);
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

* 0.5.4 (owner decision 2026-10-06, "App log window"): read-only log reads of **House Brain Apps only**:
  ``/addons/<slug>/logs/boots/<n>`` for ``n`` 0 to -5 with exactly ``?verbose&no_colors&lines=N`` (N <= 20000,
  4 MiB, 60 s), where the slug must look like ``local_house_brain_*`` / ``<8 hex>_house_brain_*`` **and** be
  installed (never this App itself), and the host boot list ``/host/logs/boots`` (boot ids only; the one host
  route, owner choice). Lines are filtered to a UTC window and keywords and scrubbed before they leave.

* 0.5.2 (Recovery Report follow-ups, all read-only): one more pinned state read,
  ``sensor.house_brain_deployer_status`` (projected to its state, a validated request id, ``dry_run`` and the
  request id / outcome of its ``last_result``; nothing else), a few named attributes of the Connection
  Forensics sensor (``last_start``, ``stop_to_ready_seconds`` as integers), and ``mesh_counts``: the already
  allowed ``get_states`` and entity registry reads, reduced at once to per-mesh counts (ZHA, Z-Wave JS,
  Matter: entities and how many are unavailable). No new route kind and no write.

* 0.6.0 (owner decision 2026-10-06, "Core/OS update gate"): two more read-only reads, ``/os/info`` (and the
  already allowed ``/core/info``) projected to version, latest version and ``update_available``, and
  ``/core/api/config`` projected to Core's run state and its safe/recovery-mode flags. Exactly one *mutating*
  system target at a time, opened only after the owner's Approve: ``with ha.system_target(kind, version)`` for
  ``kind`` ``core`` or ``os`` and the one approved version. Inside it, and nowhere else, the guard allows
  ``POST /backups/new/full`` with the body exactly ``{"name": "hbm-pre-<kind>-<version>"}``, ``GET
  /backups/<slug>/info``, and either ``POST /core/update`` with exactly ``{"version": <version>, "backup":
  false}`` or ``POST /os/update`` with exactly ``{"version": <version>}``. Only for ``core``, and only for the
  backup this App just made (pinned in the block), ``POST /backups/<slug>/restore/partial`` with exactly
  ``{"homeassistant": true}`` (Home Assistant only; never Apps, folders or a full restore). Bodies are compared
  whole in ``_guard`` before any I/O. Core and OS are never opened together, never inside an App update or a
  fix. The Supervisor, this App, a host reboot or a shutdown are never routes; the only reboot is the one the
  Supervisor performs as part of the OS update the owner approved.

* 0.7.0 (owner decisions 2026-10-08, "HACS and device-firmware updates"): read-only discovery of Home Assistant
  ``update.*`` entities that the Supervisor does not own (``get_states`` and the entity registry, already allowed,
  projected to version facts; ``GET /core/api/states/update.<x>`` projected the same way), the HACS repository
  list (``hacs/repositories/list``, projected to id, category, domain, name, minimum Home Assistant version and
  versions) and one entity's release notes (``update/release_notes``). Exactly one *mutating* entity target at a
  time, opened only for an owner-approved (or, for a display-only HACS card or theme, an owner-enabled automatic)
  update: ``with ha.entity_target(entity_id, kind, to, frm)``. Inside it the guard allows ``POST
  /core/api/services/update/install`` with the body exactly ``{"entity_id": <it>, "version": <to>}`` (HACS; the
  rollback body with ``<frm>`` too) or exactly ``{"entity_id": <it>}`` (device firmware, which cannot pick a
  version), for HACS the one configuration backup ``POST /backups/new/partial`` with exactly ``{"name":
  "hbm-pre-<entity>-<to>", "homeassistant": true, "homeassistant_exclude_database": true, "addons": [],
  "folders": []}`` and ``GET /backups/<slug>/info``, and only for a HACS *integration* ``POST /core/restart``
  with no body (the restart the owner's Approve covers). Never another entity, never a Supervisor-owned update
  (Apps, Core, OS, Supervisor), never a restore.

The ``ha`` CLI is never used (#223 R4 ``apps``/``addons`` escape).
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
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
                       "config_entries/flow/progress",
                       # 0.7.0: HACS repository list and one update entity's release notes (both read-only).
                       "hacs/repositories/list", "update/release_notes"})
RE_ENTITY_ID = re.compile(r"^[a-z0-9_]{1,40}\.[a-z0-9_]{1,200}$")
RE_DEVICE_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")
RE_AGENT_ID = re.compile(r"^[a-z0-9_]{1,40}\.[A-Za-z0-9_.-]{1,80}$")
DEVICE_SNAPSHOT_MAX_BYTES = 32 * 1024 * 1024
# 0.6.1 (owner 2026-10-06, "Fix at the source"): the periodic reads share ONE long-lived Core socket. Opening a
# fresh socket per read made sensor.connected_clients flip about once a minute (~1,900 history rows a day).
SHARED_WS_BACKOFF_MAX = 60.0

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
# 0.5.3: the Google Drive Backup App (sabeechen/hassio-google-drive-backup) copies backups to Google Drive
# outside Home Assistant's own backup locations and reports it on this one entity (read-only, projected).
DRIVE_BACKUP_ENTITY = "sensor.backup_state"
# 0.5.4: App log window (read-only; House Brain Apps only)
RE_HB_APP_SLUG = re.compile(r"^(?:local|[0-9a-f]{8})_house_brain_[a-z0-9_]{1,60}$")
LOG_BOOTS = (0, -1, -2, -3, -4, -5)
LOG_MAX_LINES = 20000
LOG_MAX_BYTES = 4 * 1024 * 1024
DRIVE_BACKUP_STATES = ("backed_up", "waiting", "error")
# 0.5.2: Deployer request ids (``hbd/manifest.py`` RE_REQUEST_ID, optionally the Undo suffix) and the
# ``last_result`` text it publishes ("<request id>: <OUTCOME>").
RE_DEPLOYER_RID = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}(?:#undo)?$")
RE_DEPLOYER_LAST = re.compile(r"^([a-z0-9][a-z0-9-]{2,63}(?:#undo)?): ([A-Z][A-Z_]{1,39})$")
MESH_PLATFORMS = ("zha", "zwave_js", "matter")
# 0.6.0 Core/OS update gate (owner decision 2026-10-06)
SYSTEM_KINDS = ("core", "os")
CORE_STATES = ("NOT_RUNNING", "STARTING", "RUNNING", "STOPPING", "FINAL_WRITE", "STOPPED")
# 0.7.0 HACS and device-firmware updates (owner decisions 2026-10-08)
RE_UPDATE_ENTITY = re.compile(r"^update\.[a-z0-9_]{1,200}$")
RE_ENTITY_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:~-]{0,63}$")
RE_DOMAIN = re.compile(r"^[a-z0-9_]{1,64}$")
RE_HACS_ID = re.compile(r"^[0-9]{1,20}$")
RE_FULL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}$")
SUPERVISOR_PLATFORM = "hassio"            # Apps, Core, OS and Supervisor: handled by the App/Core/OS engines only
HACS_PLATFORM = "hacs"
HACS_CATEGORIES = ("integration", "plugin", "theme", "python_script", "appdaemon", "netdaemon", "template")
ENTITY_KINDS = ("hacs", "hacs_integration", "firmware")
FEATURE_SPECIFIC_VERSION = 2              # homeassistant.components.update.UpdateEntityFeature.SPECIFIC_VERSION


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


RE_AUTH_DATE = re.compile(r"20[0-9]{2}-[01][0-9]-[0-3][0-9]")
RE_AUTH_CODE = re.compile(r"GITHUB_APP_[A-Z_]{2,40}")


def project_github_auth(raw: Any) -> dict | None:
    """0.6.3 Credential Autopilot: the Deployer's sign-in summary, typed field by field (never a value)."""
    if not isinstance(raw, dict):
        return None

    def flag(key: str) -> bool:
        return raw.get(key) is True

    days = raw.get("pat_days_left")
    using = raw.get("using")
    error = raw.get("app_error")
    expires = raw.get("pat_expires")
    retired = raw.get("pat_retired")
    return {"using": using if using in ("github_app", "pat", "") else "",
            "pat_set": flag("pat_set"), "app_connected": flag("app_connected"), "app_installed": flag("app_installed"),
            "pat_days_left": days if isinstance(days, int) and not isinstance(days, bool) and -9999 < days < 99999
            else None,
            "pat_expires": expires if isinstance(expires, str) and RE_AUTH_DATE.fullmatch(expires) else None,
            "app_error": error if isinstance(error, str) and RE_AUTH_CODE.fullmatch(error) else "",
            # 0.6.6 (R2): the date the Deployer retired its old token itself
            "pat_retired": retired if isinstance(retired, str) and (retired == "yes" or RE_AUTH_DATE.fullmatch(retired))
            else None}


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
                "last_rid": m.group(1) if m else None, "last_outcome": m.group(2) if m else None,
                "github_auth": project_github_auth(attrs.get("github_auth")),
                # 0.6.6: which Deployer wrote it (its container host name) and its version
                "instance": inst if isinstance(inst := attrs.get("instance"), str) and RE_INSTANCE.fullmatch(inst)
                else None,
                "version": ver if isinstance(ver := attrs.get("version"), str) and RE_APP_VERSION.fullmatch(ver)
                else None}
    return {}

RE_INSTANCE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")

_STATIC_ALLOW: tuple[tuple[str, str], ...] = (
    ("GET", r"/addons/self/info"),
    ("GET", r"/addons"),
    ("GET", r"/addons/[a-z0-9][a-z0-9_]{0,99}/info"),
    ("GET", r"/addons/[a-z0-9][a-z0-9_]{0,99}/changelog"),
    ("GET", r"/store/addons/[a-z0-9][a-z0-9_]{0,99}"),
    ("GET", r"/core/info"),
    ("GET", r"/os/info"),                                   # 0.6.0: OS version / update (read-only, projected)
    ("GET", r"/core/api/config"),                           # 0.6.0: Core run state / safe mode (projected)
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
    ("GET", r"/core/api/states/" + re.escape(DRIVE_BACKUP_ENTITY)),
    # 0.5.4 App log window: House Brain App logs by boot, and the host boot list (ids only)
    ("GET", r"/addons/(?:local|[0-9a-f]{8})_house_brain_[a-z0-9_]{1,60}/logs/boots/(?:0|-[1-5])"),
    ("GET", r"/host/logs/boots"),
    ("POST", r"/core/api/services/persistent_notification/create"),
    ("POST", r"/core/api/services/persistent_notification/dismiss"),
    # 0.7.0: one update entity's state, projected to version facts (read-only)
    ("GET", r"/core/api/states/update\.[a-z0-9_]{1,200}"),
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


@dataclass(frozen=True)
class SystemInfo:
    """Core or OS version facts (0.6.0). Nothing else from the reply is kept."""

    kind: str
    version: str | None
    version_latest: str | None
    update_available: bool


def system_call_ok(kind: str, version: str, method: str, path: str, body: Any, backup: str | None) -> bool:
    """True only for the exact system-update calls of one open ``system_target`` (bodies compared whole)."""
    if method == "POST" and path == "/backups/new/full":
        return body == {"name": f"hbm-pre-{kind}-{version}"}
    if method == "GET" and re.fullmatch(r"/backups/[a-f0-9]{8,64}/info", path):
        return body is None
    if kind == "core" and method == "POST" and path == "/core/update":
        return body == {"version": version, "backup": False} and body["backup"] is False
    if kind == "os" and method == "POST" and path == "/os/update":
        return body == {"version": version}
    if kind == "core" and backup and method == "POST" and path == f"/backups/{backup}/restore/partial":
        return body == {"homeassistant": True} and body["homeassistant"] is True
    return False


@dataclass(frozen=True)
class UpdateEntity:
    """0.7.0: one ``update.*`` entity, projected to version facts. Titles and notes are bounded text."""

    entity_id: str
    platform: str
    device_id: str | None
    unique_id: str | None
    title: str
    state: str                     # on | off | unavailable | unknown
    installed: str | None
    latest: str | None
    skipped: str | None
    in_progress: bool
    specific_version: bool
    auto_update: bool


@dataclass(frozen=True)
class HacsRepo:
    """0.7.0: one HACS repository, projected (no descriptions, authors or paths)."""

    id: str
    category: str
    domain: str | None
    full_name: str
    homeassistant: str | None      # minimum Home Assistant version the repository declares
    installed_version: str | None
    available_version: str | None


def _ver(v: Any) -> str | None:
    return v if isinstance(v, str) and RE_ENTITY_VERSION.fullmatch(v) else None


def project_update_state(entity_id: str, row: Any, registry: dict | None = None) -> UpdateEntity | None:
    """One ``update.*`` state (and its registry row) to an ``UpdateEntity``; None when it is not one."""
    if not isinstance(row, dict) or not RE_UPDATE_ENTITY.fullmatch(entity_id):
        return None
    attrs = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
    reg = registry or {}
    state = row.get("state")
    features = attrs.get("supported_features")
    title = attrs.get("title") or attrs.get("friendly_name") or entity_id
    dev, uid, plat = reg.get("device_id"), reg.get("unique_id"), reg.get("platform")
    return UpdateEntity(
        entity_id=entity_id,
        platform=plat if isinstance(plat, str) and RE_DOMAIN.fullmatch(plat) else "",
        device_id=dev if isinstance(dev, str) and RE_DEVICE_ID.fullmatch(dev) else None,
        unique_id=str(uid)[:80] if isinstance(uid, (str, int)) and not isinstance(uid, bool) else None,
        title=re.sub(r"[^A-Za-z0-9 ()._+&/:-]", "", str(title))[:60],
        state=state if state in ("on", "off", "unavailable", "unknown") else "unknown",
        installed=_ver(attrs.get("installed_version")), latest=_ver(attrs.get("latest_version")),
        skipped=_ver(attrs.get("skipped_version")), in_progress=attrs.get("in_progress") not in (None, False),
        specific_version=isinstance(features, int) and not isinstance(features, bool)
        and bool(features & FEATURE_SPECIFIC_VERSION),
        auto_update=attrs.get("auto_update") is True)


def project_hacs_repo(row: Any) -> HacsRepo | None:
    if not isinstance(row, dict):
        return None
    rid, cat, name = row.get("id"), row.get("category"), row.get("full_name")
    rid = str(rid) if isinstance(rid, (str, int)) and not isinstance(rid, bool) else ""
    if not RE_HACS_ID.fullmatch(rid) or cat not in HACS_CATEGORIES or not isinstance(name, str) \
            or not RE_FULL_NAME.fullmatch(name):
        return None
    dom = row.get("domain")
    return HacsRepo(id=rid, category=cat, domain=dom if isinstance(dom, str) and RE_DOMAIN.fullmatch(dom) else None,
                    full_name=name, homeassistant=_ver(row.get("homeassistant")),
                    installed_version=_ver(row.get("installed_version")),
                    available_version=_ver(row.get("available_version")))


def entity_backup_name(entity_id: str, version: str) -> str:
    return f"hbm-pre-{entity_id[len('update.'):]}-{version}"[:100]


def entity_backup_body(entity_id: str, version: str) -> dict:
    """0.7.0: the one backup a HACS update makes: Home Assistant's configuration (custom_components and
    www included), without its database, without Apps and folders."""
    return {"name": entity_backup_name(entity_id, version), "homeassistant": True,
            "homeassistant_exclude_database": True, "addons": [], "folders": []}


def entity_call_ok(target: dict, method: str, path: str, body: Any) -> bool:
    """True only for the exact calls of one open ``entity_target`` (bodies compared whole, types included)."""
    eid, kind, to, frm = target["entity_id"], target["kind"], target["to"], target["frm"]
    if method == "POST" and path == "/core/api/services/update/install":
        if kind == "firmware":
            return body == {"entity_id": eid} and set(body) == {"entity_id"}
        return isinstance(body, dict) and set(body) == {"entity_id", "version"} and body["entity_id"] == eid \
            and isinstance(body["version"], str) and body["version"] in {to, frm} - {None}
    if kind == "firmware":
        return False
    if method == "POST" and path == "/backups/new/partial":
        want = entity_backup_body(eid, to)
        return isinstance(body, dict) and body == want and body["homeassistant"] is True \
            and body["homeassistant_exclude_database"] is True
    if method == "GET" and re.fullmatch(r"/backups/[a-f0-9]{8,64}/info", path):
        return body is None
    if kind == "hacs_integration" and method == "POST" and path == "/core/restart":
        return body is None
    return False


# -- 0.6.6 (Credential Autopilot R2): move the Deployer from the local folder to the store -----------------
MIGRATE_OLD = "local_house_brain_deployer"
RE_MIGRATE_NEW = re.compile(r"[0-9a-f]{8}_house_brain_deployer")
# The Deployer 0.3.7 option names; nothing else is ever copied (the store App has the same schema).
DEPLOYER_OPTION_KEYS = ("github_repo", "github_token", "github_auth", "retire_old_token", "requests_branch",
                        "source_ref_allowlist", "notify_service", "owner_username", "dry_run", "poll_seconds",
                        "max_approval_requests_per_day", "approval_timeout_minutes",
                        "restart_approval_timeout_minutes", "require_phone_unlock", "clear_freeze_for")


def migrate_call_ok(old: str, new: str, remove: bool, method: str, path: str, body: Any) -> bool:
    """True only for the exact calls of one open ``migrate_target`` (bodies compared whole)."""
    if method != "POST":
        return False
    empty = body in (None, {})
    if remove:
        return path == f"/addons/{old}/uninstall" and empty
    if path == f"/addons/{new}/options":
        opts = body.get("options") if isinstance(body, dict) and set(body) == {"options"} else None
        return isinstance(opts, dict) and bool(opts) and set(opts) <= set(DEPLOYER_OPTION_KEYS)
    if path == f"/addons/{old}/options":
        return body in ({"boot": "manual"}, {"boot": "auto"})
    return empty and path in {f"/addons/{old}/stop", f"/addons/{old}/start", f"/addons/{new}/start",
                              f"/addons/{new}/stop"}


def app_backup_body_ok(slug: str, path: str, body: Any) -> bool:
    """0.6.0 (tightened): an App-update backup or restore names that one App only, never Home Assistant."""
    if not isinstance(body, dict) or body.get("addons") != [slug] or body.get("folders") != [] \
            or body.get("homeassistant") is not False:
        return False
    keys = {"addons", "folders", "homeassistant"} | ({"name"} if path == "/backups/new/partial" else set())
    return set(body) == keys


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


def backup_content_kind(row: dict) -> str:
    """Which parts a Supervisor backup holds: ``full``, or ``partial`` + Home Assistant / Apps / folders."""
    if row.get("type") == "full":
        return "full"
    content = row.get("content") if isinstance(row.get("content"), dict) else {}
    addons, folders = content.get("addons"), content.get("folders")
    names = sorted(f for f in folders if isinstance(f, str) and re.fullmatch(r"[a-z_]{1,20}", f))[:8] \
        if isinstance(folders, list) else []
    return (f"partial:ha={content.get('homeassistant') is True}"
            f":apps={isinstance(addons, list) and len(addons) > 0}:folders={'+'.join(names) or '-'}")


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
        self._system: dict | None = None          # 0.6.0: {"kind", "version", "backup"} while open
        self._migrate: dict | None = None         # 0.6.6: {"old", "new", "remove"} while open
        self._entity: dict | None = None          # 0.7.0: {"entity_id", "kind", "to", "frm"} while open
        self._self_slug: str | None = None
        # 0.6.1 shared Core socket (see shared_ws)
        self._shared_lock = threading.Lock()
        self._shared: CoreSocket | None = None
        self._shared_backoff = 0.0
        self._shared_retry_at = 0.0
        self.shared_connects = 0

    # -- guard --------------------------------------------------------------
    def _guard(self, method: str, path: str, body: Any = None) -> None:
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
                    if method == "POST" and path.startswith("/backups/") \
                            and not app_backup_body_ok(target, path, body):
                        break                     # 0.6.0: never Home Assistant, folders or another App here
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
        system = self._system
        if system is not None and system_call_ok(system["kind"], system["version"], method, path, body,
                                                 system["backup"]):
            return
        mig = self._migrate
        if mig is not None and migrate_call_ok(mig["old"], mig["new"], mig["remove"], method, path, body):
            return
        ent = self._entity
        if ent is not None and entity_call_ok(ent, method, path, body):
            return
        raise ForbiddenCall(f"{method} {path}")

    def _busy(self) -> bool:
        return any(t is not None for t in (self._target, self._fix, self._system, self._migrate, self._entity))

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
        if self._busy():
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
        if self._busy():
            raise ForbiddenCall("nested update target")
        self._target = slug
        try:
            yield
        finally:
            self._target = None

    @contextmanager
    def system_target(self, kind: str, version: str, backup: str | None = None):
        """0.6.0: open the Core *or* OS update routes for one owner-approved version (see the module docstring).

        ``backup`` pins the one backup a Core restore may use; ``backup_full`` sets it inside the block.
        """
        if kind not in SYSTEM_KINDS or not isinstance(version, str) or not RE_APP_VERSION.fullmatch(version):
            raise ForbiddenCall(f"system target {str(kind)[:8]}")
        if backup is not None and not RE_BACKUP_SLUG.fullmatch(backup):
            raise ForbiddenCall("system backup slug")
        if self._busy():
            raise ForbiddenCall("nested system target")
        self._system = {"kind": kind, "version": version, "backup": backup}
        try:
            yield
        finally:
            self._system = None

    @contextmanager
    def entity_target(self, entity_id: str, kind: str, to: str, frm: str | None = None):
        """0.7.0: open the install calls for one update entity and one version (``frm``: the HACS rollback)."""
        if not isinstance(entity_id, str) or not RE_UPDATE_ENTITY.fullmatch(entity_id) or kind not in ENTITY_KINDS:
            raise ForbiddenCall("entity target")
        if not isinstance(to, str) or not RE_ENTITY_VERSION.fullmatch(to) \
                or (frm is not None and (kind == "firmware" or not RE_ENTITY_VERSION.fullmatch(frm) or frm == to)):
            raise ForbiddenCall("entity target version")
        if self._busy():
            raise ForbiddenCall("nested entity target")
        self._entity = {"entity_id": entity_id, "kind": kind, "to": to, "frm": frm}
        try:
            yield
        finally:
            self._entity = None

    def store_deployer_slug(self) -> str | None:
        """0.6.6: the store slug the Deployer gets from the same store as this App (``<hash>_house_brain_deployer``),
        or None when this App is not a store App itself."""
        own = self.self_slug()
        prefix = own.split("_", 1)[0]
        slug = f"{prefix}_house_brain_deployer"
        return slug if RE_MIGRATE_NEW.fullmatch(slug) else None

    @contextmanager
    def migrate_target(self, old: str, new: str, remove: bool = False):
        """0.6.6: open the Deployer switch-over calls (or, with ``remove``, only the old App's uninstall)."""
        if old != MIGRATE_OLD or not isinstance(new, str) or not RE_MIGRATE_NEW.fullmatch(new) \
                or new != self.store_deployer_slug():
            raise ForbiddenCall("migrate target")
        if self._busy():
            raise ForbiddenCall("nested migrate target")
        self._migrate = {"old": old, "new": new, "remove": bool(remove)}
        try:
            yield
        finally:
            self._migrate = None

    def deployer_options(self) -> dict:
        """0.6.6, only inside ``migrate_target``: the old Deployer's options, limited to the known names. The token
        is registered as a secret at once (never logged), held in memory only for the copy."""
        mig = self._migrate
        if mig is None or mig["remove"]:
            raise ForbiddenCall("deployer options outside the switch-over")
        data = self._supervisor("GET", f"/addons/{mig['old']}/info")
        raw = data.get("options") if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            raise HAError("DEPLOYER_OPTIONS")
        token = raw.get("github_token")
        if isinstance(token, str):
            net.register_secret(token)
        return {k: raw[k] for k in DEPLOYER_OPTION_KEYS if k in raw}

    def set_deployer_options(self, options: dict) -> None:
        mig = self._migrate or {}
        self._supervisor("POST", f"/addons/{mig.get('new')}/options", body={"options": options}, timeout=60)

    def set_boot(self, slug: str, boot: str) -> None:
        self._supervisor("POST", f"/addons/{slug}/options", body={"boot": boot}, timeout=60)

    def stop_app(self, slug: str) -> None:
        self._supervisor("POST", f"/addons/{slug}/stop", body={}, timeout=300)

    def uninstall_app(self, slug: str) -> None:
        self._supervisor("POST", f"/addons/{slug}/uninstall", body={}, timeout=600)

    @staticmethod
    def _ws_guard(msg: dict) -> None:
        kind = msg.get("type")
        if kind not in _WS_ALLOW:
            raise ForbiddenCall(f"ws {kind}")
        if kind == "subscribe_events" and msg.get("event_type") != APPROVAL_EVENT:
            raise ForbiddenCall("ws subscribe to non-approval event")
        if kind == "update/release_notes" and (set(msg) != {"type", "entity_id"} or not isinstance(
                msg.get("entity_id"), str) or not RE_UPDATE_ENTITY.fullmatch(msg["entity_id"])):
            raise ForbiddenCall("ws release notes")
        if kind == "hacs/repositories/list" and set(msg) != {"type"}:
            raise ForbiddenCall("ws hacs list")

    # -- HTTP ---------------------------------------------------------------
    def _call(self, method: str, path: str, body: Any = None, timeout: float = 30.0,
              raw: bool = False, query: str = "") -> Any:
        self._guard(method, path, body)
        app_log = path.startswith("/addons/") and "/logs/boots/" in path
        allowed_query = r"\?verbose&no_colors&lines=[0-9]{1,5}" if app_log else r"\?lines=[0-9]{1,4}"
        if (query or app_log) and not re.fullmatch(allowed_query, query):
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

    def system_info(self, kind: str) -> SystemInfo:
        """0.6.0: ``/core/info`` or ``/os/info`` projected to version, latest version and update flag."""
        if kind not in SYSTEM_KINDS:
            raise ForbiddenCall(f"system info {str(kind)[:8]}")
        data = self._supervisor("GET", f"/{kind}/info")
        def ver(key: str) -> str | None:
            v = data.get(key) if isinstance(data, dict) else None
            return v if isinstance(v, str) and RE_APP_VERSION.fullmatch(v) else None
        return SystemInfo(kind=kind, version=ver("version"), version_latest=ver("version_latest"),
                          update_available=isinstance(data, dict) and data.get("update_available") is True)

    def core_state(self) -> dict:
        """0.6.0: Core's run state and safe/recovery-mode flags (``/core/api/config``); everything else dropped."""
        try:
            data = self._call("GET", "/core/api/config", timeout=15)
        except (net.NetError, HAError):
            return {"state": "UNREACHABLE", "safe_mode": False, "recovery_mode": False}
        data = data if isinstance(data, dict) else {}
        state = data.get("state")
        return {"state": state if state in CORE_STATES else "UNKNOWN",
                "safe_mode": data.get("safe_mode") is True, "recovery_mode": data.get("recovery_mode") is True}

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

    def backup_list(self) -> list[tuple[str, str, float | None, str]]:
        """(type, ISO date, size GB or None, content kind) of every backup; names and contents are not kept.

        A backup that includes Home Assistant itself counts as ``full``: Home Assistant's own
        automatic backups are stored by Supervisor as type ``partial`` with everything selected.
        The content kind (0.5.3) groups backups that hold the same parts, so sizes are compared like
        with like: a settings-only backup is never measured against one with every App and folder.
        """
        data = self._supervisor("GET", "/backups")
        out = []
        protection = []
        for row in ((data.get("backups") or []) if isinstance(data, dict) else [])[:500]:
            if isinstance(row, dict) and isinstance(row.get("date"), str):
                content = row.get("content") if isinstance(row.get("content"), dict) else {}
                kind = "full" if row.get("type") == "full" or content.get("homeassistant") is True else "partial"
                # 0.6.6 (R2): password protection, from the same answer. Only a flag and "made by this App"
                # are kept; the name itself is dropped.
                name = row.get("name") if isinstance(row.get("name"), str) else ""
                protected = row.get("protected") if isinstance(row.get("protected"), bool) else None
                protection.append((row["date"][:40], protected, kind == "full", name.startswith("hbm-pre-")))
                size_b, size_mb = row.get("size_bytes"), row.get("size")
                gb = (size_b / 1024 ** 3 if isinstance(size_b, int) and not isinstance(size_b, bool) and size_b > 0
                      else size_mb / 1024 if isinstance(size_mb, (int, float)) and not isinstance(size_mb, bool)
                      and size_mb > 0 else None)
                out.append((kind, row["date"][:40], round(gb, 3) if gb else None, backup_content_kind(row)))
        self.backup_protection = protection
        return out

    def host_boots(self) -> list[int]:
        """Boot offsets the host journal still holds (0 = this boot, -1 = the one before ...). Ids are dropped."""
        data = self._supervisor("GET", "/host/logs/boots", timeout=20)
        boots = data.get("boots") if isinstance(data, dict) else None
        out = []
        for key in (boots if isinstance(boots, dict) else {}):
            if isinstance(key, str) and re.fullmatch(r"0|-[1-9][0-9]{0,3}", key):
                out.append(int(key))
        return sorted(out, reverse=True)[:200]

    def app_log_boot(self, slug: str, boot: int, lines: int = LOG_MAX_LINES) -> str:
        """Verbose (UTC-stamped) log of one House Brain App for one boot. Read-only, bounded."""
        if not isinstance(slug, str) or not RE_HB_APP_SLUG.fullmatch(slug) or slug == self.self_slug():
            raise ForbiddenCall(f"log of {str(slug)[:40]}")
        if isinstance(boot, bool) or boot not in LOG_BOOTS:
            raise ForbiddenCall(f"log boot {str(boot)[:8]}")
        if slug not in {a.slug for a in self.installed_apps()}:
            raise HAError("LOG_APP_NOT_INSTALLED", slug)
        n = min(max(int(lines), 2), LOG_MAX_LINES)
        data = self._call("GET", f"/addons/{slug}/logs/boots/{boot}", timeout=60, raw=True,
                          query=f"?verbose&no_colors&lines={n}")
        return bytes(data or b"")[:LOG_MAX_BYTES].decode("utf-8", "replace")

    def drive_backup_state(self) -> dict | None:
        """The Google Drive Backup App's own status, or None when that App is not installed.

        Kept: its state, the date of the newest backup that is in Google Drive and how many are there.
        Backup names, sizes and the backup list are dropped.
        """
        try:
            data = self._call("GET", f"/core/api/states/{DRIVE_BACKUP_ENTITY}", timeout=15)
        except net.NetError as err:
            if err.status == 404:
                return None
            raise
        attrs = data.get("attributes") if isinstance(data, dict) else None
        if not isinstance(attrs, dict) or "backups_in_google_drive" not in attrs:
            return None                 # another integration's entity with that name, or the old snapshot mode
        state = data.get("state")
        count = attrs.get("backups_in_google_drive")
        last = attrs.get("last_uploaded")
        return {"state": state if state in DRIVE_BACKUP_STATES else "unknown",
                "last_uploaded": last[:40] if isinstance(last, str) and last != "Never" else None,
                "in_drive": count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None}

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
        sock = self.shared_ws()
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

    # -- 0.7.0 HACS and device-firmware updates -----------------------------------------------------
    def update_entities(self) -> list[UpdateEntity]:
        """Every ``update.*`` entity with its registry platform (read-only, projected)."""
        sock = self.shared_ws()
        try:
            states = sock.command({"type": "get_states"}, timeout=60)
            entities = sock.command({"type": "config/entity_registry/list"}, timeout=60)
        finally:
            sock.close()
        registry = {}
        for row in (entities if isinstance(entities, list) else [])[:20000]:
            if isinstance(row, dict) and isinstance(row.get("entity_id"), str) \
                    and RE_UPDATE_ENTITY.fullmatch(row["entity_id"]) and not row.get("disabled_by"):
                registry[row["entity_id"]] = row
        out = []
        for row in (states if isinstance(states, list) else [])[:20000]:
            eid = row.get("entity_id") if isinstance(row, dict) else None
            if isinstance(eid, str) and eid in registry:
                ent = project_update_state(eid, row, registry[eid])
                if ent is not None:
                    out.append(ent)
        return sorted(out, key=lambda e: e.entity_id)

    def update_entity(self, entity_id: str, platform: str = "", device_id: str | None = None,
                      unique_id: str | None = None) -> UpdateEntity | None:
        """One update entity now (REST, projected); None when it does not exist (any more)."""
        if not RE_UPDATE_ENTITY.fullmatch(entity_id):
            raise ForbiddenCall("update entity")
        try:
            data = self._call("GET", f"/core/api/states/{entity_id}", timeout=15)
        except net.NetError as err:
            if err.status == 404:
                return None
            raise
        return project_update_state(entity_id, data, {"platform": platform, "device_id": device_id,
                                                      "unique_id": unique_id})

    def hacs_repos(self) -> dict[str, HacsRepo]:
        """HACS repositories by id (projected); empty when HACS is not installed."""
        sock = self.shared_ws()
        try:
            rows = sock.command({"type": "hacs/repositories/list"}, timeout=60)
        except HAError as err:
            if err.code == "WS_COMMAND":
                return {}
            raise
        finally:
            sock.close()
        out = {}
        for row in (rows if isinstance(rows, list) else [])[:5000]:
            repo = project_hacs_repo(row)
            if repo is not None:
                out[repo.id] = repo
        return out

    def release_notes(self, entity_id: str) -> str:
        """One update entity's release notes (bounded text); "" when there are none."""
        sock = self.shared_ws()
        try:
            notes = sock.command({"type": "update/release_notes", "entity_id": entity_id}, timeout=60)
        except HAError:
            return ""
        finally:
            sock.close()
        return notes[:65536] if isinstance(notes, str) else ""

    def entity_health(self, *, domain: str | None = None, device_id: str | None = None) -> dict:
        """``{"entries": {entry_id: state}, "total": n, "unavailable": n}`` for one integration (its config
        entries and the entities of that platform) or one device (its entities). Update entities are not
        counted. Read-only, counts and entry states only."""
        sock = self.shared_ws()
        try:
            states = sock.command({"type": "get_states"}, timeout=60)
            entities = sock.command({"type": "config/entity_registry/list"}, timeout=60)
            entries = sock.command({"type": "config_entries/get"}) if domain else []
        finally:
            sock.close()
        live = {r.get("entity_id"): r.get("state") for r in (states if isinstance(states, list) else [])[:20000]
                if isinstance(r, dict)}
        total = unavailable = 0
        for row in (entities if isinstance(entities, list) else [])[:20000]:
            if not isinstance(row, dict) or row.get("disabled_by") or not isinstance(row.get("entity_id"), str) \
                    or row["entity_id"].startswith("update."):
                continue
            if (domain and row.get("platform") == domain) or (device_id and row.get("device_id") == device_id):
                total += 1
                if live.get(row["entity_id"]) in (None, "unavailable"):
                    unavailable += 1
        out_entries = {}
        for row in (entries if isinstance(entries, list) else [])[:2000]:
            if isinstance(row, dict) and row.get("domain") == domain and not row.get("disabled_by") \
                    and isinstance(row.get("entry_id"), str) and RE_ENTRY_ID.fullmatch(row["entry_id"]):
                st = row.get("state")
                out_entries[row["entry_id"]] = st if isinstance(st, str) and re.fullmatch(r"[a-z_]{1,30}", st) \
                    else "unknown"
        return {"entries": out_entries, "total": total, "unavailable": unavailable}

    def backup_ha_config(self, entity_id: str, version: str) -> str:
        """Inside ``entity_target`` (HACS): the configuration backup; returns its slug."""
        data = self._supervisor("POST", "/backups/new/partial", body=entity_backup_body(entity_id, version),
                                timeout=3600)
        bslug = data.get("slug") if isinstance(data, dict) else None
        if not isinstance(bslug, str) or not RE_BACKUP_SLUG.fullmatch(bslug):
            raise HAError("BACKUP_SLUG")
        return bslug

    def backup_has_ha(self, bslug: str) -> bool:
        data = self._supervisor("GET", f"/backups/{bslug}/info")
        return isinstance(data, dict) and isinstance(data.get("homeassistant"), str)

    def install_update(self, entity_id: str, version: str | None, timeout: float = 600.0) -> None:
        """Inside ``entity_target``: Home Assistant's ``update.install`` for exactly this entity (and version)."""
        body = {"entity_id": entity_id} if version is None else {"entity_id": entity_id, "version": version}
        self._call("POST", "/core/api/services/update/install", body=body, timeout=timeout)

    def restart_core(self) -> None:
        """Inside a HACS-integration ``entity_target`` only: restart Home Assistant Core (Supervisor)."""
        self._supervisor("POST", "/core/restart", timeout=900)

    # -- 0.6.0 Core/OS update (only inside system_target, after the owner's Approve) ------------
    def backup_full(self, name: str) -> str:
        """A full backup; its slug becomes the only one a Core restore may use in this block."""
        data = self._supervisor("POST", "/backups/new/full", body={"name": name}, timeout=7200)
        bslug = data.get("slug") if isinstance(data, dict) else None
        if not isinstance(bslug, str) or not RE_BACKUP_SLUG.fullmatch(bslug):
            raise HAError("BACKUP_SLUG")
        if self._system is not None:
            self._system["backup"] = bslug
        return bslug

    def backup_facts(self, bslug: str) -> dict:
        """``{"type", "homeassistant"}`` of one backup (type and its Core version); everything else dropped."""
        data = self._supervisor("GET", f"/backups/{bslug}/info")
        data = data if isinstance(data, dict) else {}
        ha_version = data.get("homeassistant")
        return {"type": data.get("type") if data.get("type") in ("full", "partial") else "unknown",
                "homeassistant": ha_version if isinstance(ha_version, str) and RE_APP_VERSION.fullmatch(ha_version)
                else None}

    def update_core(self, version: str) -> None:
        self._supervisor("POST", "/core/update", body={"version": version, "backup": False}, timeout=3600)

    def update_os(self, version: str) -> None:
        self._supervisor("POST", "/os/update", body={"version": version}, timeout=3600)

    def restore_core(self, bslug: str) -> None:
        """Home Assistant only (config and its Core version) from ``bslug``; never Apps or folders."""
        self._supervisor("POST", f"/backups/{bslug}/restore/partial", body={"homeassistant": True}, timeout=3600)

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
        sock = self.shared_ws()
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
        sock = self.shared_ws()
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
        sock = self.shared_ws()
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

    # -- 0.6.1: one shared, long-lived Core socket for the periodic reads ---------------------
    def shared_ws(self) -> "SharedLease":
        """Lease the shared socket (one user at a time). ``close()`` on the lease releases it; the
        connection stays open. A transport failure drops it; the next lease reconnects, with backoff
        (1 s doubling to SHARED_WS_BACKOFF_MAX) so a restarting Core is not hammered."""
        self._shared_lock.acquire()
        try:
            if self._shared is None:
                now = time.monotonic()
                if now < self._shared_retry_at:
                    raise HAError("WS_BACKOFF", f"{self._shared_retry_at - now:.0f}s")
                try:
                    self._shared = self.ws(max_size=DEVICE_SNAPSHOT_MAX_BYTES)
                except Exception:
                    self._shared_backoff = min(SHARED_WS_BACKOFF_MAX, max(1.0, self._shared_backoff * 2))
                    self._shared_retry_at = now + self._shared_backoff
                    raise
                self._shared_backoff = 0.0
                self.shared_connects += 1
            return SharedLease(self)
        except BaseException:
            self._shared_lock.release()
            raise

    def _drop_shared(self) -> None:
        sock, self._shared = self._shared, None
        if sock is not None:
            sock.close()

    def close_shared_ws(self) -> None:
        """Close the shared socket (App shutdown)."""
        with self._shared_lock:
            self._drop_shared()


class SharedLease:
    """One user's turn on the shared socket. Same ``command``/``close`` shape as ``CoreSocket``."""

    def __init__(self, owner: HomeAssistant) -> None:
        self._owner = owner
        self._released = False

    def command(self, msg: dict, timeout: float = 30.0) -> Any:
        sock = self._owner._shared
        if sock is None or self._released:
            raise HAError("WS_LEASE")
        try:
            return sock.command(msg, timeout=timeout)
        except HAError as err:
            if err.code != "WS_COMMAND":      # Core answered "error": the connection itself is fine
                self._owner._drop_shared()
            raise
        except Exception:
            self._owner._drop_shared()
            raise

    def close(self) -> None:
        if not self._released:
            self._released = True
            self._owner._shared_lock.release()


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
