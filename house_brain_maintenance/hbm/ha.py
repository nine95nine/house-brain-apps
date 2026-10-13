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

* 0.7.4 (owner decision 2026-10-10, "Approve design, build it": stage a local App source): exactly one more
  mutating route, ``POST /store/reload`` (body empty), and only inside ``with ha.stage_target(slug)`` for one of the
  three built-in local House Brain Apps (``manifest.STAGE_APPS``) while an owner-approved
  ``STAGE_LOCAL_APP_SOURCE`` job runs. It only makes the Supervisor re-read the App folders; the update itself is
  the existing ``update_target`` path. Never nested with any other target, never for this App.

* 0.8.0 (owner decisions K1-K4 2026-10-11, "automatic keys"): exactly one *key target* at a time,
  ``with ha.key_target(role, slug)``, only for the Broker key of one House Brain App (``KEY_CONSUMERS``: the
  Observer, the AI Access App, the Inspector and the Scout) during an automatic key change. Inside it the guard
  allows that one App's ``GET /addons/<slug>/info`` read **with its options** (``key_consumer_read``; every key, token,
  password or secret option value is registered as a secret at once and nothing is kept after the change), ``POST
  /addons/<slug>/options`` with ``{"options": {...}}`` whose option *names* are exactly the ones just read (only
  values change), and, for the Observer, AI Access and Inspector only, ``POST /addons/<slug>/restart`` with an
  empty body (the owner's standing permission K3/K4: restart only right after its key changed, and to roll that
  back; a stopped App is never started). Never this App, never another App, never nested with another target.

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
from .manifest import STAGE_APPS

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
FIX_KINDS = frozenset({"restart_app", "start_app", "apply_suggestion", "reload_entry", "power_cycle",
                       "relabel_stats"})          # 0.9.6: statistics unit label (never a delete)
RE_UNIT_TEXT = re.compile(r"^[^\x00-\x1f<>`|]{0,24}$")
RE_UNIT_CLASS = re.compile(r"^[a-z_]{1,40}$")
STATS_ISSUES = ("units_changed", "state_class_removed", "mean_type_changed")
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
                       "hacs/repositories/list", "update/release_notes",
                       # 0.8.7 Housekeeping (read-only): automation run traces, what an automation refers to, and the
                       # system-health subscription (only the recorder's database size is kept).
                       "trace/list", "search/related", "system_health/info",
                       # 0.9.2 (owner pop-up 2026-10-12 "Measure first"): how many history rows each entity wrote in
                       # one past hour. Counted at once; every state value is dropped unread.
                       "history/history_during_period"})
HISTORY_KEYS = frozenset({"type", "start_time", "end_time", "entity_ids", "minimal_response", "no_attributes",
                          "significant_changes_only", "include_start_time_state"})
HISTORY_CHUNK = 200
RE_ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")
RE_AUTOMATION_ENTITY = re.compile(r"^automation\.[a-z0-9_]{1,200}$")
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
AI_CONNECTORS_ENTITY = "sensor.house_brain_ai_connectors"        # 0.9.11: AI Access 0.1.5-dev connector sign-ins
RECOVERY_READ_ENTITIES = frozenset({
    FORENSICS_ENTITY, DEPLOYER_STATUS_ENTITY,
    "sensor.house_brain_network_outage_class", "sensor.house_brain_network_outage_summary",
    "sensor.house_brain_network_wan_status", "sensor.ups_status_data", "sensor.ups_battery_charge",
    "binary_sensor.any_smoke_detected", "binary_sensor.any_co_detected", "climate.ecobee_thermostat",
    "sensor.sense_51446_l1_voltage", "sensor.enphase_solar_power_now",
})
# 0.9.11: the one other entity it may read (credential heads-up), kept apart from the Recovery Report's own list.
CREDENTIAL_READ_ENTITIES = frozenset({AI_CONNECTORS_ENTITY})
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
# 0.8.0 automatic keys: the Broker key option of each House Brain App (role -> option name); the slug is resolved
# per App (the Observer and Scout from this App's options, the Inspector is local, AI Access comes from the same
# store as this App). Restart is allowed for every consumer except the Scout (it reads its options per run).
KEY_OPTIONS = {"observer_write": "broker_write_key", "ai_read": "broker_read_key",
               "inspector_entity": "entity_write_key", "scout_inventory": "broker_inventory_write_key"}
KEY_NO_RESTART = frozenset({"scout_inventory"})
INSPECTOR_SLUG = "local_house_brain_engineering_inspector"
RE_AI_ACCESS_SLUG = re.compile(r"^(?:local|[0-9a-f]{8})_house_brain_ai_access$")
RE_SECRET_OPTION = re.compile(r"(?:key|token|password|secret|pass)$", re.I)


def _min_core(v: Any) -> str | None:
    """0.8.1 (found by #975): a minimum-Core requirement is kept even when it is malformed, so the review refuses
    the update ("cannot be compared") instead of silently treating it as "no requirement". None = none given."""
    if v is None:
        return None
    if not isinstance(v, str):
        return "invalid"
    text = "".join(c for c in v if 32 < ord(c) < 127)[:40]
    return text or "invalid"


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
    if entity_id == AI_CONNECTORS_ENTITY:
        return {"providers": project_connectors(attrs.get("providers"))}
    return {}


CONNECTOR_PROVIDERS = ("claude", "chatgpt", "grok")
CONNECTOR_STATES = frozenset({"SIGN_IN_FAILED", "EXPIRING_SOON", "OK", "NO_CONNECTOR", "UNKNOWN"})
RE_CONNECTOR_DAY = re.compile(r"20[0-9]{2}-[01][0-9]-[0-3][0-9]")


def project_connectors(raw: Any) -> dict:
    """0.9.11: per known provider only its state, days left and end day; names, links and anything else dropped."""
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    for name in CONNECTOR_PROVIDERS:
        row = raw.get(name)
        if not isinstance(row, dict) or row.get("state") not in CONNECTOR_STATES:
            continue
        days, end = row.get("days_left"), row.get("expires_on")
        out[name] = {"state": row["state"],
                     "days_left": days if isinstance(days, int) and not isinstance(days, bool) and -9999 < days < 99999
                     else None,
                     "expires_on": end if isinstance(end, str) and RE_CONNECTOR_DAY.fullmatch(end) else None}
    return out

RE_INSTANCE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")

_STATIC_ALLOW: tuple[tuple[str, str], ...] = (
    ("GET", r"/addons/self/info"),
    ("GET", r"/addons"),
    ("GET", r"/addons/[a-z0-9][a-z0-9_]{0,99}/info"),
    ("GET", r"/addons/[a-z0-9][a-z0-9_]{0,99}/changelog"),
    ("GET", r"/addons/[a-z0-9][a-z0-9_]{0,99}/stats"),     # 0.8.6: App memory/CPU (read-only, projected)
    ("GET", r"/os/boards/raspberrypi/firmware"),           # 0.8.6: Pi firmware version / update (read-only)
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
    ("GET", r"/core/api/states/(?:" + "|".join(re.escape(e) for e in sorted(RECOVERY_READ_ENTITIES
                                                                           | CREDENTIAL_READ_ENTITIES)) + r")"),
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


@dataclass(frozen=True)
class OsState:
    """0.8.3: Home Assistant OS slot facts from ``/os/info`` (Supervisor >= 2026.07). ``running`` is the version
    of the booted slot (falls back to ``version``); ``pending`` is an installed update that waits for a reboot."""

    running: str | None
    pending: str | None
    boot: str | None
    slots: dict                      # bootname -> version (str or None); A/B only


def pi_firmware_call_ok(method: str, path: str, body: Any) -> bool:
    """0.8.6: the exact calls of one owner-approved Pi firmware update (``pi_firmware_target``)."""
    return method == "POST" and body is None and path in ("/core/check", "/os/boards/raspberrypi/firmware/update",
                                                           "/host/reboot")


def os_activation_call_ok(act: dict, method: str, path: str, body: Any) -> bool:
    """0.8.3: the exact calls of one owner-approved OS activation (``os_activation_target``)."""
    if act["action"] == "reboot":
        if method == "POST" and path == "/core/check":       # config check before the restart
            return body is None
        return method == "POST" and path == "/host/reboot" and body is None
    if act["action"] == "boot_slot":
        return method == "POST" and path == "/os/boot-slot" and body == {"boot_slot": act["slot"]} \
            and body["boot_slot"] in ("A", "B")
    return False


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
                    full_name=name, homeassistant=_min_core(row.get("homeassistant")),
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
    if kind == "hacs_integration" and method == "POST" and path == "/core/check":   # 0.8.4: before the restart
        return body is None
    return False


def key_call_ok(key: dict, method: str, path: str, body: Any) -> bool:
    """True only for the exact calls of one open ``key_target`` (0.8.0)."""
    slug = key["slug"]
    if method == "GET":
        return path == f"/addons/{slug}/info" and body is None
    if method != "POST":
        return False
    if path == f"/addons/{slug}/restart":
        return key["role"] not in KEY_NO_RESTART and body in (None, {})
    if path == f"/addons/{slug}/options":
        opts = body.get("options") if isinstance(body, dict) and set(body) == {"options"} else None
        names = key.get("names")
        return isinstance(opts, dict) and names is not None and frozenset(opts) == names \
            and KEY_OPTIONS[key["role"]] in names
    return False


# -- 0.6.6 (Credential Autopilot R2): move the Deployer from the local folder to the store -----------------
MIGRATE_OLD = "local_house_brain_deployer"
# 0.9.3 (owner pop-up 2026-10-12 "Scout first, then others"): the Inventory Scout moves from the local folder to the
# House Brain store. Only this store's Scout may become the Scout target, and only by an owner-approved switch.
LOCAL_SCOUT = "local_house_brain_maintenance_inventory_scout"
RE_STORE_SCOUT = re.compile(r"[0-9a-f]{8}_house_brain_maintenance_inventory_scout")
SCOUT_OPTION_NAMES = ("broker_inventory_write_key", "broker_url")
RE_MIGRATE_NEW = re.compile(r"[0-9a-f]{8}_house_brain_deployer")
# 0.9.8 (owner pop-ups 2026-10-13 "Observer to the store", "One Approve does it all"): the Engineering Observer moves
# from the local folder to the House Brain store. One owner Approve opens exactly the calls of that switch.
LOCAL_OBSERVER = "local_house_brain_engineering_observer"
RE_STORE_OBSERVER = re.compile(r"[0-9a-f]{8}_house_brain_engineering_observer")
OBSERVER_OPTION_NAMES = ("broker_url", "broker_write_key", "publish_interval_seconds")
# 0.9.9 (owner pop-ups 2026-10-13 "Inspector 0.2.6 adds it", "Only in export mode"): the Engineering Inspector follows.
# It is a manual_only App, so its switch never touches a boot setting.
RE_STORE_INSPECTOR = re.compile(r"[0-9a-f]{8}_house_brain_engineering_inspector")
INSPECTOR_OPTION_NAMES = ("broker_entity_url", "entity_snapshot_mode", "entity_write_key")
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


def observer_move_call_ok(old: str, new: str, method: str, path: str, body: Any,
                          names: tuple = OBSERVER_OPTION_NAMES, boot: bool = True) -> bool:
    """0.9.8: True only for the exact calls of one open ``observer_move_target`` (bodies compared whole). 0.9.9: the
    Inspector switch uses the same rule with its own option names and no boot setting (``boot=False``)."""
    if method == "GET":
        return path in (f"/addons/{new}/info", f"/addons/{new}/logs/latest")
    if method != "POST":
        return False
    if path == f"/addons/{new}/options":
        opts = body.get("options") if isinstance(body, dict) and set(body) == {"options"} else None
        return isinstance(opts, dict) and tuple(sorted(opts)) == names
    if path == f"/addons/{old}/options":
        return boot and body in ({"boot": "manual"}, {"boot": "auto"})
    return body in (None, {}) and path in {f"/addons/{old}/stop", f"/addons/{old}/start", f"/addons/{new}/start",
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
                     homeassistant=_min_core(data.get("homeassistant")),
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
        self.inspector = INSPECTOR_SLUG           # 0.9.9: the store Inspector after an owner-approved switch
        self._token = token
        net.register_secret(token)
        self._scout_move: str | None = None       # 0.9.3: the store Scout, only inside copy_scout_to
        self._build_routes()
        self._target: str | None = None
        self._fix: tuple[str, str] | None = None
        self._system: dict | None = None          # 0.6.0: {"kind", "version", "backup"} while open
        self._os_act: dict | None = None          # 0.8.3: {"action", "version", "slot"} while open
        self._pifw: str | None = None             # 0.8.6: the approved Pi firmware version while open
        self._migrate: dict | None = None         # 0.6.6: {"old", "new", "remove"} while open
        self._entity: dict | None = None          # 0.7.0: {"entity_id", "kind", "to", "frm"} while open
        self._stage: str | None = None            # 0.7.4: the allowlisted local App whose source is being staged
        self._key: dict | None = None             # 0.8.0: {"role", "slug", "names"} during one key change
        self._legacy: dict | None = None          # 0.9.5: {"bslug"} during one legacy Deployer backup/stage step
        self._obsmove: dict | None = None         # 0.9.8: {"old", "new"} during the Observer switch to the store
        self._insmove: dict | None = None         # 0.9.9: {"old", "new"} during the Inspector switch to the store
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
        if self._pifw is not None and pi_firmware_call_ok(method, path, body):
            return                                # 0.8.6: only inside pi_firmware_target
        act = self._os_act
        if act is not None and os_activation_call_ok(act, method, path, body):
            return                                # 0.8.3: only inside os_activation_target
        system = self._system
        if system is not None and system_call_ok(system["kind"], system["version"], method, path, body,
                                                 system["backup"]):
            return
        leg = self._legacy                        # 0.9.5: the legacy Deployer's backup, its info and its download
        if leg is not None and (
                (method == "POST" and path == "/backups/new/partial" and leg["bslug"] is None
                 and app_backup_body_ok(MIGRATE_OLD, path, body))
                or (method == "GET" and leg["bslug"] is not None
                    and path in (f"/backups/{leg['bslug']}/info", f"/backups/{leg['bslug']}/download"))):
            return
        move = self._scout_move                   # 0.9.3: the store Scout's info and its two settings only
        if move is not None and ((method == "GET" and path == f"/addons/{move}/info") or (
                method == "POST" and path == f"/addons/{move}/options" and isinstance(body, dict)
                and set(body) == {"options"} and isinstance(body["options"], dict)
                and tuple(sorted(body["options"])) == SCOUT_OPTION_NAMES)):
            return
        om = self._obsmove                        # 0.9.8: the Observer switch to the store
        if om is not None and observer_move_call_ok(om["old"], om["new"], method, path, body):
            return
        im = self._insmove                        # 0.9.9: the Inspector switch (no boot setting)
        if im is not None and observer_move_call_ok(im["old"], im["new"], method, path, body,
                                                    names=INSPECTOR_OPTION_NAMES, boot=False):
            return
        mig = self._migrate
        if mig is not None and migrate_call_ok(mig["old"], mig["new"], mig["remove"], method, path, body):
            return
        ent = self._entity
        if ent is not None and entity_call_ok(ent, method, path, body):
            return
        if self._stage is not None and method == "POST" and path == "/store/reload" and body in (None, {}):
            return                                # 0.7.4: only inside stage_target (see the module docstring)
        key = self._key
        if key is not None and key_call_ok(key, method, path, body):
            return                                # 0.8.0: only inside key_target (see the module docstring)
        raise ForbiddenCall(f"{method} {path}")

    def _busy(self) -> bool:
        return any(t is not None for t in (self._target, self._fix, self._system, self._os_act, self._pifw,
                                           self._migrate, self._legacy, self._obsmove, self._insmove,
                                           self._entity,
                                           self._stage, self._key))

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
        elif kind == "relabel_stats" and ref != "units_changed":
            raise ForbiddenCall("relabel target")
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
    def os_activation_target(self, action: str, version: str, slot: str | None = None):
        """0.8.3 (owner pop-up 2026-10-11 "Ask me, then reboot"): open the host reboot (with a config check
        first) that finishes one owner-approved OS install, or the boot-slot switch of an owner-approved OS
        rollback. Nothing else; the caller has the owner's Approve for exactly this step."""
        if action not in ("reboot", "boot_slot") or not isinstance(version, str) \
                or not RE_APP_VERSION.fullmatch(version):
            raise ForbiddenCall("os activation target")
        if action == "boot_slot" and slot not in ("A", "B"):
            raise ForbiddenCall("os activation slot")
        if action == "reboot" and slot is not None:
            raise ForbiddenCall("os activation slot")
        if self._busy():
            raise ForbiddenCall("nested os activation target")
        self._os_act = {"action": action, "version": version, "slot": slot}
        try:
            yield
        finally:
            self._os_act = None

    @contextmanager
    def pi_firmware_target(self, version: str):
        """0.8.6: open the config check, the Pi firmware update and the restart it needs, for one approved version."""
        if not isinstance(version, str) or not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._:+-]{0,39}", version):
            raise ForbiddenCall("pi firmware target")
        if self._busy():
            raise ForbiddenCall("nested pi firmware target")
        self._pifw = version
        try:
            yield
        finally:
            self._pifw = None

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

    @contextmanager
    def stage_target(self, slug: str):
        """0.7.4: open ``POST /store/reload`` for one allowlisted local App's source staging job (nothing else)."""
        if not isinstance(slug, str) or slug not in STAGE_APPS or slug == "self" or slug == self.self_slug():
            raise ForbiddenCall("stage target")
        if self._busy():
            raise ForbiddenCall("nested stage target")
        self._stage = slug
        try:
            yield
        finally:
            self._stage = None

    def reload_store(self) -> None:
        """0.7.4, only inside ``stage_target``: the Supervisor re-reads the App folders (new local version)."""
        if self._stage is None:
            raise ForbiddenCall("store reload outside a stage job")
        self._supervisor("POST", "/store/reload", body={}, timeout=300)

    def key_slug(self, role: str) -> str | None:
        """0.8.0: the slug of the App that holds ``role``'s Broker key (None: not resolvable here)."""
        if role == "observer_write":
            return self.observer
        if role == "scout_inventory":
            return self.scout
        if role == "inspector_entity":
            return self.inspector
        if role == "ai_read":
            prefix = self.self_slug().split("_", 1)[0]
            slug = f"{prefix}_house_brain_ai_access"
            return slug if RE_AI_ACCESS_SLUG.fullmatch(slug) else None
        return None

    @contextmanager
    def key_target(self, role: str, slug: str):
        """0.8.0: open one House Brain App's Broker-key read, option write and (not the Scout) restart."""
        if role not in KEY_OPTIONS or not isinstance(slug, str) or slug != self.key_slug(role) \
                or slug in ("self", self.self_slug()):
            raise ForbiddenCall("key target")
        if self._busy():
            raise ForbiddenCall("nested key target")
        self._key = {"role": role, "slug": slug, "names": None}
        try:
            yield
        finally:
            self._key = None

    def key_consumer_read(self) -> tuple[str, dict]:
        """0.8.0, only inside ``key_target``: (state, options) of the key App. Every string option value is
        registered as a secret at once; the option names are pinned for the write that may follow."""
        key = self._key
        if key is None:
            raise ForbiddenCall("key read outside a key change")
        data = self._supervisor("GET", f"/addons/{key['slug']}/info")
        options = data.get("options") if isinstance(data, dict) else None
        if not isinstance(options, dict):
            raise HAError("KEY_APP_OPTIONS")
        for name, value in options.items():
            # Secret-looking options only (keys, tokens, passwords), so ordinary values such as a URL or
            # "FULL_STOP" are not blanked out of later reports.
            if isinstance(value, str) and RE_SECRET_OPTION.search(str(name)):
                net.register_secret(value)
        key["names"] = frozenset(str(k) for k in options)
        state = data.get("state")
        return (state if state in SAFE_STATES else "unknown"), dict(options)

    def key_consumer_write(self, options: dict) -> None:
        key = self._key or {}
        self._supervisor("POST", f"/addons/{key.get('slug')}/options", body={"options": options}, timeout=60)

    def key_consumer_restart(self) -> None:
        key = self._key or {}
        self._supervisor("POST", f"/addons/{key.get('slug')}/restart", body={}, timeout=300)

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

    @contextmanager
    def legacy_target(self, bslug: str | None = None):
        """0.9.5 (#985 test plan M2/M3): with ``bslug`` None, only a partial backup of the legacy Deployer
        (``local_house_brain_deployer``, nothing else); with ``bslug``, only that backup's info and its download."""
        if bslug is not None and (not isinstance(bslug, str) or not RE_BACKUP_SLUG.fullmatch(bslug)):
            raise ForbiddenCall("legacy backup")
        if self._busy():
            raise ForbiddenCall("nested legacy target")
        self._legacy = {"bslug": bslug}
        try:
            yield
        finally:
            self._legacy = None

    def legacy_backup(self, name: str) -> str:
        if self._legacy is None or self._legacy["bslug"] is not None:
            raise ForbiddenCall("legacy backup outside its step")
        return self.backup_app(MIGRATE_OLD, name)

    def legacy_backup_info(self) -> dict:
        """The pinned backup's custody facts only (no options, no file names)."""
        leg = self._legacy
        if leg is None or leg["bslug"] is None:
            raise ForbiddenCall("legacy backup info outside its step")
        data = self._supervisor("GET", f"/backups/{leg['bslug']}/info")
        data = data if isinstance(data, dict) else {}
        apps = [{"slug": str(a.get("slug"))[:80], "version": str(a.get("version"))[:32]}
                for a in (data.get("addons") or [])[:20] if isinstance(a, dict)]
        folders = sorted(str(f)[:40] for f in (data.get("folders") or [])[:20])
        def s(key, n):
            v = data.get(key)
            return v[:n] if isinstance(v, str) else None
        return {"slug": leg["bslug"], "name": s("name", 100), "date": s("date", 40), "type": s("type", 20),
                "size": data.get("size") if isinstance(data.get("size"), (int, float)) else None,
                "protected": data.get("protected") is True, "compressed": data.get("compressed") is True,
                "homeassistant": bool(data.get("homeassistant")), "addons": apps, "folders": folders}

    def legacy_backup_download(self, max_bytes: int) -> bytes:
        leg = self._legacy
        if leg is None or leg["bslug"] is None:
            raise ForbiddenCall("legacy backup download outside its step")
        path = f"/backups/{leg['bslug']}/download"
        self._guard("GET", path)
        _, data = net.request("GET", self.base + path, {"Authorization": f"Bearer {self._token}"}, timeout=600,
                              raw=True, max_bytes=max_bytes)
        if not isinstance(data, bytes):
            raise HAError("BACKUP_DOWNLOAD")
        return data

    # -- 0.9.8 Engineering Observer from the store ----------------------------------------------
    def store_observer_slug(self) -> str | None:
        """This store's Observer (``<hash>_house_brain_engineering_observer``), or None when this App is not a store
        App itself."""
        slug = f"{self.self_slug().split('_', 1)[0]}_house_brain_engineering_observer"
        return slug if RE_STORE_OBSERVER.fullmatch(slug) else None

    @contextmanager
    def observer_move_target(self, old: str, new: str):
        """0.9.8: open the Observer switch-over calls: the store Observer's info, log and three settings, the local
        Observer's stop/start and Start-on-boot, the store Observer's start/stop. Nothing else, never an uninstall."""
        if old != LOCAL_OBSERVER or old != self.observer or not isinstance(new, str) \
                or not RE_STORE_OBSERVER.fullmatch(new) or new != self.store_observer_slug() or new == self.scout:
            raise ForbiddenCall("observer move target")
        if self._busy():
            raise ForbiddenCall("nested observer move target")
        self._obsmove = {"old": old, "new": new}
        try:
            yield
        finally:
            self._obsmove = None

    def copy_observer_settings(self) -> tuple[str, str]:
        """Only inside ``observer_move_target``: copy the local Observer's three settings to the store Observer. The
        key is registered as a secret at once, held in memory only for the copy: (broker URL, key fingerprint)."""
        om = self._obsmove
        if om is None:
            raise ForbiddenCall("observer copy outside the switch-over")
        data = self._supervisor("GET", f"/addons/{om['old']}/info")
        raw = data.get("options") if isinstance(data, dict) else None
        if not isinstance(raw, dict) or tuple(sorted(raw)) != OBSERVER_OPTION_NAMES:
            raise HAError("OBSERVER_OPTIONS")
        url, key, every = raw.get("broker_url"), raw.get("broker_write_key"), raw.get("publish_interval_seconds")
        if not isinstance(url, str) or not url or len(url) > 300 or not isinstance(key, str) or not key \
                or not isinstance(every, int) or isinstance(every, bool) or not 60 <= every <= 300:
            raise HAError("OBSERVER_OPTIONS")
        net.register_secret(key)
        self._supervisor("POST", f"/addons/{om['new']}/options",
                         body={"options": {"broker_url": url, "broker_write_key": key,
                                           "publish_interval_seconds": every}}, timeout=60)
        return url, fingerprint(key)

    def store_observer_view(self) -> AppView:
        """Only inside ``observer_move_target``: the store Observer's state, version, URL and key fingerprint."""
        om = self._obsmove
        if om is None:
            raise ForbiddenCall("observer view outside the switch-over")
        return _project(om["new"], self._supervisor("GET", f"/addons/{om['new']}/info"), "broker_write_key")

    def store_observer_log(self) -> str:
        """Only inside ``observer_move_target``: the store Observer's current-run log (redacted)."""
        om = self._obsmove
        if om is None:
            raise ForbiddenCall("observer log outside the switch-over")
        data = self._call("GET", f"/addons/{om['new']}/logs/latest", timeout=60, raw=True)
        return net.redact(bytes(data or b"")[-262144:].decode("utf-8", "replace"))

    def retarget_observer(self, new: str) -> None:
        """Make this store's Observer the Observer target (owner-approved switch, or its record after a restart)."""
        if not isinstance(new, str) or not RE_STORE_OBSERVER.fullmatch(new) or new != self.store_observer_slug() \
                or new == self.scout:
            raise ForbiddenCall("observer target")
        self.observer = new
        self._build_routes()

    # -- 0.9.9 Engineering Inspector from the store ---------------------------------------------
    def store_inspector_slug(self) -> str | None:
        slug = f"{self.self_slug().split('_', 1)[0]}_house_brain_engineering_inspector"
        return slug if RE_STORE_INSPECTOR.fullmatch(slug) else None

    @contextmanager
    def inspector_move_target(self, old: str, new: str):
        """0.9.9: open the Inspector switch-over calls: the store Inspector's info, log and three settings, and stop/
        start of the local and the store Inspector. No boot setting (both are manual_only), never an uninstall."""
        if old != INSPECTOR_SLUG or old != self.inspector or not isinstance(new, str) \
                or not RE_STORE_INSPECTOR.fullmatch(new) or new != self.store_inspector_slug():
            raise ForbiddenCall("inspector move target")
        if self._busy():
            raise ForbiddenCall("nested inspector move target")
        self._insmove = {"old": old, "new": new}
        try:
            yield
        finally:
            self._insmove = None

    def inspector_mode(self) -> str | None:
        """The local Inspector's ``entity_snapshot_mode`` (read-only; the key and URL are dropped at once)."""
        data = self._supervisor("GET", f"/addons/{self.inspector}/info")
        raw = data.get("options") if isinstance(data, dict) else None
        mode = raw.get("entity_snapshot_mode") if isinstance(raw, dict) else None
        return mode if mode in ("off", "dry_run", "export") else None

    def copy_inspector_settings(self) -> tuple[str, str]:
        """Only inside ``inspector_move_target`` and only in export mode: copy the three settings to the store
        Inspector; the key is registered as a secret at once: (broker URL, key fingerprint)."""
        im = self._insmove
        if im is None:
            raise ForbiddenCall("inspector copy outside the switch-over")
        data = self._supervisor("GET", f"/addons/{im['old']}/info")
        raw = data.get("options") if isinstance(data, dict) else None
        if not isinstance(raw, dict) or tuple(sorted(raw)) != INSPECTOR_OPTION_NAMES:
            raise HAError("INSPECTOR_OPTIONS")
        url, key, mode = raw.get("broker_entity_url"), raw.get("entity_write_key"), raw.get("entity_snapshot_mode")
        if mode != "export" or not isinstance(url, str) or not url or len(url) > 300 \
                or not isinstance(key, str) or not key:
            raise HAError("INSPECTOR_OPTIONS")
        net.register_secret(key)
        self._supervisor("POST", f"/addons/{im['new']}/options",
                         body={"options": {"broker_entity_url": url, "entity_snapshot_mode": "export",
                                           "entity_write_key": key}}, timeout=60)
        return url, fingerprint(key)

    def store_inspector_view(self) -> AppView:
        im = self._insmove
        if im is None:
            raise ForbiddenCall("inspector view outside the switch-over")
        data = self._supervisor("GET", f"/addons/{im['new']}/info")
        if isinstance(data, dict) and isinstance(data.get("options"), dict):   # its URL option has another name
            data = {**data, "options": {**data["options"], "broker_url": data["options"].get("broker_entity_url")}}
        return _project(im["new"], data, "entity_write_key")

    def store_inspector_log(self) -> str:
        im = self._insmove
        if im is None:
            raise ForbiddenCall("inspector log outside the switch-over")
        data = self._call("GET", f"/addons/{im['new']}/logs/latest", timeout=60, raw=True)
        return net.redact(bytes(data or b"")[-262144:].decode("utf-8", "replace"))

    def retarget_inspector(self, new: str) -> None:
        if not isinstance(new, str) or not RE_STORE_INSPECTOR.fullmatch(new) or new != self.store_inspector_slug():
            raise ForbiddenCall("inspector target")
        self.inspector = new

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
        if kind == "trace/list" and msg != {"type": "trace/list", "domain": "automation"}:
            raise ForbiddenCall("ws trace list")
        if kind == "search/related" and (set(msg) != {"type", "item_type", "item_id"} or msg["item_type"] != "automation"
                                         or not isinstance(msg["item_id"], str)
                                         or not RE_AUTOMATION_ENTITY.fullmatch(msg["item_id"])):
            raise ForbiddenCall("ws search related")
        if kind == "system_health/info" and set(msg) != {"type"}:
            raise ForbiddenCall("ws system health")
        if kind == "history/history_during_period":
            ids = msg.get("entity_ids")
            if (set(msg) != HISTORY_KEYS or msg["minimal_response"] is not True or msg["no_attributes"] is not True
                    or msg["significant_changes_only"] is not False or msg["include_start_time_state"] is not False
                    or not all(isinstance(msg[k], str) and RE_ISO_UTC.fullmatch(msg[k])
                               for k in ("start_time", "end_time"))
                    or not isinstance(ids, list) or not 0 < len(ids) <= HISTORY_CHUNK
                    or not all(isinstance(e, str) and RE_ENTITY_ID.fullmatch(e) for e in ids)):
                raise ForbiddenCall("ws history")

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

    def _build_routes(self) -> None:
        s, o = re.escape(self.scout), re.escape(self.observer)
        routes = _STATIC_ALLOW + (
            ("GET", rf"/addons/{s}/info"),
            ("GET", rf"/addons/{o}/info"),
            ("POST", rf"/addons/{s}/options"),
            ("POST", rf"/addons/{s}/start"),
            ("GET", rf"/addons/{s}/logs/latest"),
        ) + ((("GET", r"/core/api/states/" + re.escape(self.power_cycle_entity)),) if self.power_cycle_entity
             else ())
        self._allow = tuple((m, re.compile(p)) for m, p in routes)

    # -- 0.9.3 Inventory Scout from the store ------------------------------------------------
    def store_scout_slug(self) -> str | None:
        """This store's Scout (``<hash>_house_brain_maintenance_inventory_scout``), or None when this App is not a
        store App itself."""
        slug = f"{self.self_slug().split('_', 1)[0]}_house_brain_maintenance_inventory_scout"
        return slug if RE_STORE_SCOUT.fullmatch(slug) else None

    def _check_store_scout(self, new: str) -> None:
        if not isinstance(new, str) or not RE_STORE_SCOUT.fullmatch(new) or new != self.store_scout_slug() \
                or new == self.observer:
            raise ForbiddenCall("scout target")

    def copy_scout_to(self, new: str) -> tuple[str, str]:
        """Copy the current Scout's two settings to this store's Scout. The key is registered as a secret at once,
        held in memory only for the copy and never returned: (broker URL, key fingerprint)."""
        self._check_store_scout(new)
        if self.scout == new or self._busy() or self._scout_move is not None:
            raise ForbiddenCall("scout copy")
        data = self._supervisor("GET", f"/addons/{self.scout}/info")
        raw = data.get("options") if isinstance(data, dict) else None
        if not isinstance(raw, dict) or tuple(sorted(raw)) != SCOUT_OPTION_NAMES:
            raise HAError("SCOUT_OPTIONS")
        url, key = raw.get("broker_url"), raw.get("broker_inventory_write_key")
        if not isinstance(url, str) or not isinstance(key, str) or not key or len(url) > 300:
            raise HAError("SCOUT_OPTIONS")
        net.register_secret(key)
        self._scout_move = new
        try:
            self._supervisor("POST", f"/addons/{new}/options",
                             body={"options": {"broker_url": url, "broker_inventory_write_key": key}})
        finally:
            self._scout_move = None
        return url, fingerprint(key)

    def store_scout_view(self, new: str) -> AppView:
        """The store Scout's state, version, option names, broker URL and key fingerprint (the key is dropped)."""
        self._check_store_scout(new)
        self._scout_move = new
        try:
            return _project(new, self._supervisor("GET", f"/addons/{new}/info"), "broker_inventory_write_key")
        finally:
            self._scout_move = None

    def retarget_scout(self, new: str) -> None:
        """Make this store's Scout the one Scout target (owner-approved switch, or its record after a restart)."""
        self._check_store_scout(new)
        self.scout = new
        self._build_routes()

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

    def os_state(self) -> OsState:
        """0.8.3: ``/os/info`` projected to running/pending versions and the A/B slots (bounded identifiers)."""
        data = self._supervisor("GET", "/os/info")
        data = data if isinstance(data, dict) else {}

        def ver(v) -> str | None:
            return v if isinstance(v, str) and RE_APP_VERSION.fullmatch(v) else None
        boot = data.get("boot") if data.get("boot") in ("A", "B") else None
        slots = {}
        raw = data.get("boot_slots")
        if isinstance(raw, dict):
            for name in ("A", "B"):
                row = raw.get(name)
                if isinstance(row, dict):
                    slots[name] = ver(row.get("version"))
        running = slots.get(boot) if boot and slots.get(boot) else ver(data.get("version"))
        return OsState(running=running, pending=ver(data.get("version_pending")), boot=boot, slots=slots)

    def core_config_check(self) -> tuple[bool, str]:
        """0.8.3/0.8.4/0.8.6: Supervisor ``/core/check`` (the current Core image). Only inside
        ``os_activation_target`` (before the OS restart), a HACS-integration ``entity_target`` (before the Core
        restart) or ``pi_firmware_target`` (before the firmware restart)."""
        try:
            self._supervisor("POST", "/core/check", timeout=300)
        except HAError:
            return False, "the configuration check failed"
        except net.NetError as err:
            return False, f"the configuration check failed (HTTP {err.status})"
        return True, "the configuration check passed"

    def pi_firmware(self) -> dict | None:
        """0.8.6: ``/os/boards/raspberrypi/firmware`` projected; None when this board/OS Agent has no Pi firmware."""
        try:
            data = self._supervisor("GET", "/os/boards/raspberrypi/firmware", timeout=30)
        except HAError:
            return None
        except net.NetError as err:
            if 400 <= err.status < 500:
                return None
            raise

        def ver(v) -> str | None:
            return v if isinstance(v, str) and re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._:+-]{0,39}", v) else \
                (str(v) if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 10 ** 12 else None)
        data = data if isinstance(data, dict) else {}
        return {"current": ver(data.get("current_version")), "latest": ver(data.get("latest_version")),
                "available": data.get("update_available") is True, "blocked": data.get("update_blocked") is True,
                "pending": data.get("update_pending") is True}

    def update_pi_firmware(self) -> None:
        """0.8.6, only inside ``pi_firmware_target``."""
        self._supervisor("POST", "/os/boards/raspberrypi/firmware/update", timeout=900)

    def app_stats(self, slug: str) -> dict:
        """0.8.6: ``{"memory_percent", "cpu_percent"}`` of one App (floats or None); everything else dropped."""
        if not isinstance(slug, str) or not RE_SLUG.fullmatch(slug):
            raise ForbiddenCall("app stats slug")
        data = self._supervisor("GET", f"/addons/{slug}/stats", timeout=15)
        data = data if isinstance(data, dict) else {}

        def pct(v):
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 10000 else None
        return {"memory_percent": pct(data.get("memory_percent")), "cpu_percent": pct(data.get("cpu_percent"))}

    def reboot_host(self) -> None:
        """0.8.3, only inside ``os_activation_target("reboot")``. The connection may drop as the host goes down."""
        self._supervisor("POST", "/host/reboot", timeout=120)

    def set_os_boot_slot(self, slot: str) -> None:
        """0.8.3, only inside ``os_activation_target("boot_slot")``: mark ``slot`` active and reboot into it."""
        self._supervisor("POST", "/os/boot-slot", body={"boot_slot": slot}, timeout=120)

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

    def loaded_components(self) -> set[str]:
        """0.8.4: the integrations Core has loaded (``/core/api/config`` ``components``), bounded domain names only
        (``zha``, ``sensor.zha`` -> ``zha``). Empty when Core does not answer."""
        try:
            data = self._call("GET", "/core/api/config", timeout=15)
        except (net.NetError, HAError):
            return set()
        raw = data.get("components") if isinstance(data, dict) else None
        out: set[str] = set()
        for c in raw if isinstance(raw, list) else []:
            if isinstance(c, str):
                domain = c.rsplit(".", 1)[-1]
                if re.fullmatch(r"[a-z0-9_]{1,64}", domain):
                    out.add(domain)
            if len(out) >= 3000:
                break
        return out

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
        inventory = []                    # 0.8.5: per backup, which Apps it holds (slugs only) and its flags
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
                apps = content.get("addons") if isinstance(content.get("addons"), list) else content.get("apps")
                inventory.append({"date": row["date"][:40], "ha": kind == "full", "ours": name.startswith("hbm-pre-"),
                                  "protected": protected, "size_gb": round(gb, 3) if gb else None,
                                  "apps": tuple(sorted({a for a in (apps if isinstance(apps, list) else [])
                                                        if isinstance(a, str) and RE_SLUG.fullmatch(a)}))[:200]})
        self.backup_protection = protection
        self.backup_inventory = inventory
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
                item = {k: s(row.get(k), 120) for k in (
                    "domain", "issue_id", "severity", "translation_key", "learn_more_url",
                    "breaks_in_ha_version", "dismissed_version")} | \
                    {"is_fixable": row.get("is_fixable") is True, "ignored": row.get("ignored") is True}
                ph = row.get("translation_placeholders")
                if row.get("domain") == "sensor" and item["translation_key"] in STATS_ISSUES and isinstance(ph, dict):
                    # 0.9.6: the statistics issues' units (no values), so the fix can name the exact change
                    sid = ph.get("statistic_id")
                    item["stats"] = {
                        "statistic_id": sid if isinstance(sid, str) and RE_ENTITY_ID.fullmatch(sid) else "",
                        **{k: (ph.get(k) if isinstance(ph.get(k), str) and RE_UNIT_TEXT.fullmatch(ph.get(k))
                               else None) for k in ("state_unit", "metadata_unit")},
                        **{k: (ph.get(k) if isinstance(ph.get(k), str) and RE_UNIT_CLASS.fullmatch(ph.get(k))
                               else None) for k in ("state_unit_class", "metadata_unit_class")}}
                out_repairs.append(item)
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

    def relabel_statistics(self, statistic_id: str, unit_class: str, unit: str) -> None:
        """0.9.6, only inside ``fix_target("relabel_stats", "units_changed")``: set the stored unit label of one
        sensor's long-term statistics to its current unit, without converting any value (Home Assistant's own
        "Update the unit of the historic statistic values" fix). Never deletes statistics."""
        if self._fix != ("relabel_stats", "units_changed") or not RE_ENTITY_ID.fullmatch(statistic_id) \
                or not statistic_id.startswith("sensor.") or not RE_UNIT_CLASS.fullmatch(unit_class or "") \
                or not unit or not RE_UNIT_TEXT.fullmatch(unit):
            raise ForbiddenCall("relabel outside its fix")
        msg = {"type": "recorder/update_statistics_metadata", "statistic_id": statistic_id,
               "unit_class": unit_class, "unit_of_measurement": unit}

        def guard(m: dict) -> None:
            if m != msg:
                raise ForbiddenCall("ws relabel")
        sock = CoreSocket(self.ws_url, self._token, guard)
        try:
            sock.command(msg, timeout=30)
        finally:
            sock.close()

    def statistics_units(self, statistic_ids: list[str]) -> dict[str, str | None]:
        """0.9.6: the stored unit of each sensor's long-term statistics (metadata only, read-only)."""
        ids = [i for i in statistic_ids if isinstance(i, str) and RE_ENTITY_ID.fullmatch(i)][:50]
        if not ids:
            return {}
        msg = {"type": "recorder/get_statistics_metadata", "statistic_ids": ids}

        def guard(m: dict) -> None:
            if m != msg:
                raise ForbiddenCall("ws statistics metadata")
        sock = CoreSocket(self.ws_url, self._token, guard)
        try:
            rows = sock.command(msg, timeout=30)
        finally:
            sock.close()
        return {r["statistic_id"]: (r.get("statistics_unit_of_measurement")
                                    if isinstance(r.get("statistics_unit_of_measurement"), str) else None)
                for r in (rows if isinstance(rows, list) else []) if isinstance(r, dict)
                and r.get("statistic_id") in ids}

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
        if entity_id not in RECOVERY_READ_ENTITIES and entity_id not in CREDENTIAL_READ_ENTITIES:
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
        # 0.8.5: Home Assistant's automatic-backup bookkeeping from the same answer (times, state, agent ids only)
        if isinstance(info, dict):
            def when(v):
                return v[:40] if isinstance(v, str) else None
            errors = info.get("agent_errors")
            self.backup_auto = {"last_attempted": when(info.get("last_attempted_automatic_backup")),
                                "last_completed": when(info.get("last_completed_automatic_backup")),
                                "next": when(info.get("next_automatic_backup")),
                                "state": info.get("state") if isinstance(info.get("state"), str)
                                and re.fullmatch(r"[a-z_]{1,40}", info.get("state")) else None,
                                "agent_errors": sorted(a for a in (errors if isinstance(errors, dict) else {})
                                                       if isinstance(a, str) and RE_AGENT_ID.fullmatch(a))[:10]}
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

    # -- 0.8.7 Housekeeping (read-only) ------------------------------------------------------------
    def housekeeping_reads(self) -> dict:
        """Automations (projected), their stored run traces (projected) and every known entity id."""
        sock = self.shared_ws()
        try:
            states = sock.command({"type": "get_states"}, timeout=60)
            entities = sock.command({"type": "config/entity_registry/list"}, timeout=60)
            traces = sock.command({"type": "trace/list", "domain": "automation"}, timeout=60)
        finally:
            sock.close()

        def s(v, n: int) -> str | None:
            return str(v)[:n] if isinstance(v, (str, int, float)) and not isinstance(v, bool) else None
        known: set[str] = set()
        automations = []
        for row in (states if isinstance(states, list) else [])[:20000]:
            eid = row.get("entity_id") if isinstance(row, dict) else None
            if not isinstance(eid, str) or not RE_ENTITY_ID.fullmatch(eid):
                continue
            known.add(eid)
            if RE_AUTOMATION_ENTITY.fullmatch(eid) and len(automations) < 1000:
                attrs = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
                automations.append({"entity_id": eid, "name": s(attrs.get("friendly_name"), 80) or eid,
                                    "state": s(row.get("state"), 20), "id": s(attrs.get("id"), 80),
                                    "last_triggered": s(attrs.get("last_triggered"), 40)})
        for row in (entities if isinstance(entities, list) else [])[:20000]:
            eid = row.get("entity_id") if isinstance(row, dict) else None
            if isinstance(eid, str) and RE_ENTITY_ID.fullmatch(eid):
                known.add(eid)
        out_traces = []
        for row in (traces if isinstance(traces, list) else [])[:5000]:
            if not isinstance(row, dict) or row.get("domain") != "automation":
                continue
            stamp = row.get("timestamp") if isinstance(row.get("timestamp"), dict) else {}
            out_traces.append({"item_id": s(row.get("item_id"), 80), "execution": s(row.get("script_execution"), 40),
                               "error": s(row.get("error"), 300), "start": s(stamp.get("start"), 40)})
        return {"automations": automations, "traces": out_traces, "known": known}

    def related_entities(self, automation_ids: list[str]) -> dict[str, list[str]]:
        """What each automation refers to (``search/related``; entity ids only, existing or not)."""
        out: dict[str, list[str]] = {}
        sock = self.shared_ws()
        try:
            for aid in automation_ids:
                if not RE_AUTOMATION_ENTITY.fullmatch(aid):
                    continue
                try:
                    rel = sock.command({"type": "search/related", "item_type": "automation", "item_id": aid},
                                       timeout=30)
                except HAError as err:
                    if err.code == "WS_COMMAND":
                        continue                       # this one could not be searched; the others still can
                    raise
                ents = rel.get("entity") if isinstance(rel, dict) else None
                out[aid] = sorted({e for e in (ents if isinstance(ents, list) else [])[:500]
                                   if isinstance(e, str) and RE_ENTITY_ID.fullmatch(e)})
        finally:
            sock.close()
        return out

    def history_row_counts(self, hours: float = 1.0, now: float | None = None) -> dict[str, int]:
        """0.9.2: how many history rows each entity wrote in the last ``hours`` (every state change, attributes
        excluded, no start-of-window row). Only the counts leave this function; state values are never kept."""
        end = time.time() if now is None else now
        fmt = "%Y-%m-%dT%H:%M:%S+00:00"
        window = (time.strftime(fmt, time.gmtime(end - hours * 3600)), time.strftime(fmt, time.gmtime(end)))
        lease = self.shared_ws()
        try:
            states = lease.command({"type": "get_states"}, timeout=60)
        finally:
            lease.close()
        ids = sorted({row["entity_id"] for row in (states if isinstance(states, list) else [])[:20000]
                      if isinstance(row, dict) and isinstance(row.get("entity_id"), str)
                      and RE_ENTITY_ID.fullmatch(row["entity_id"])})
        counts: dict[str, int] = {}
        sock = self.ws(max_size=32 * 1024 * 1024)
        try:
            for i in range(0, len(ids), HISTORY_CHUNK):
                part = sock.command({"type": "history/history_during_period", "start_time": window[0],
                                     "end_time": window[1], "entity_ids": ids[i:i + HISTORY_CHUNK],
                                     "minimal_response": True, "no_attributes": True,
                                     "significant_changes_only": False, "include_start_time_state": False},
                                    timeout=120)
                for eid, rows in (part.items() if isinstance(part, dict) else ()):
                    if isinstance(eid, str) and RE_ENTITY_ID.fullmatch(eid) and isinstance(rows, list) and rows:
                        counts[eid] = len(rows)
                del part
        finally:
            sock.close()
        return counts

    def db_size_mib(self, timeout: float = 20.0) -> float | None:
        """The recorder's ``estimated_db_size`` from the system-health subscription; everything else dropped."""
        from .housekeeping import parse_size_mib
        sock = self.ws()
        try:
            sock.command({"type": "system_health/info"}, timeout=timeout)
            deadline = time.monotonic() + timeout
            while (remaining := deadline - time.monotonic()) > 0:
                event = sock.next_event(remaining)
                if not isinstance(event, dict) or not event:
                    if event is None:
                        return None
                    continue
                if event.get("type") == "initial":
                    rec = (event.get("data") or {}).get("recorder") if isinstance(event.get("data"), dict) else None
                    info = rec.get("info") if isinstance(rec, dict) else None
                    size = parse_size_mib(info.get("estimated_db_size")) if isinstance(info, dict) else None
                    if size is not None:
                        return size
                elif event.get("type") == "update" and event.get("domain") == "recorder" \
                        and event.get("key") == "estimated_db_size" and event.get("success") is not False:
                    return parse_size_mib(event.get("data"))
                elif event.get("type") == "finish":
                    return None
            return None
        finally:
            sock.close()

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
