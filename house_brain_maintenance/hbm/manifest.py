"""Strict validation of maintenance request manifests.

A manifest is untrusted repository content: any push-capable principal (including an AI
session) can write one. Validation is allow-list only. A manifest can pick a job from a
closed catalog and supply bounded expectations; it can never choose a target App to change, a
route, a URL or a secret. Those come from the App options and from Supervisor. The one exception
(0.5.4, owner decision 2026-10-06) is ``APP_LOG_WINDOW``: it names one installed House Brain App
whose log is **read**; nothing is ever written to that App. ``ASK_UPDATE_NOW`` (0.6.5, owner decision
2026-10-07) names an update only to *match* the one this App already reviewed and asked about and Supervisor
still reports as waiting; it can never pick a new target, a version or a route, and anything else is REFUSED.
``STAGE_LOCAL_APP_SOURCE`` (0.7.4, owner decision 2026-10-10 "Approve design, build it") names one of three built-in
local House Brain Apps (``STAGE_APPS``; never this App, the Deployer, a store App or Home Assistant), a carrier path on
``main`` pinned by commit and SHA-256, and the versions it moves between. The App re-checks everything, runs the
carrier's own ``check``, and asks the owner once before anything is written.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from . import SCHEMA
from .holds import valid_item
from .logwindow import parse_keywords

MAX_MANIFEST_BYTES = 16 * 1024
RE_REQUEST_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
RE_SHA256 = re.compile(r"^[0-9a-f]{64}$")
RE_VERSION = re.compile(r"^[0-9]{1,4}(\.[0-9]{1,4}){1,3}$")
RE_NOTE = re.compile(r"^[\x20-\x7e\n]{0,300}$")
# 0.7.2 (owner-approved 2026-10-08): Grok, Gemini and Codex chats may file requests too (same rules as any requester).
REQUESTERS = ("claude", "chatgpt", "grok", "gemini", "codex", "owner")

ROTATE_SCOUT_KEY = "ROTATE_SCOUT_KEY"
RUN_SCOUT_ONCE = "RUN_SCOUT_ONCE"
CHECK_BROKER = "CHECK_BROKER"   # 0.5.3: read-only, no approval: which Broker version is live
APP_LOG_WINDOW = "APP_LOG_WINDOW"   # 0.5.4: read-only, no approval: one House Brain App's log in a UTC window
# 0.6.4: jobs that never ask the owner anything. The daily approval-ask limit does not hold them back (live
# 2026-10-07: an APP_LOG_WINDOW request waited 3 hours behind four unrelated approval asks).
# 0.7.3 hold list (owner pop-ups 2026-10-08): put an update on hold / release it. They ask nothing and only make
# the App ask less (this App's own memory; nothing in Home Assistant changes), so they skip the ask limit too.
HOLD_UPDATE = "HOLD_UPDATE"
RELEASE_UPDATE = "RELEASE_UPDATE"
# 0.8.0 automatic keys (owner pop-up K4 extra D, 2026-10-11): rotate a Broker key now (a suspected leak). It asks
# nothing (the owner's standing permission covers key changes; the very first one still asks once, in keys.py),
# so it skips the ask limit too.
ROTATE_KEYS_NOW = "ROTATE_KEYS_NOW"
KEY_ROLES = ("observer_write", "inspector_entity", "ai_read", "scout_inventory", "rotation", "all")
NO_APPROVAL_JOBS = frozenset({CHECK_BROKER, APP_LOG_WINDOW, HOLD_UPDATE, RELEASE_UPDATE, ROTATE_KEYS_NOW})
# 0.6.5: ask the phone again now for a waiting Core/OS/App update this App already reviewed (no re-ask wait).
# It asks the owner, so it is NOT in NO_APPROVAL_JOBS: it counts against max_approval_requests_per_day.
ASK_UPDATE_NOW = "ASK_UPDATE_NOW"
UPDATE_KINDS = ("core", "os", "app")
# 0.7.4 (owner-approved design R1, 2026-10-10): stage a local App source from a pinned carrier, then the normal App
# update path. It asks the owner, so it is NOT in NO_APPROVAL_JOBS. The slug allowlist is built in (never an option):
# local Supervisor slug -> (source folder the carrier names, short label for the phone).
STAGE_LOCAL_APP_SOURCE = "STAGE_LOCAL_APP_SOURCE"
STAGE_APPS: dict[str, tuple[str, str]] = {
    "local_house_brain_engineering_inspector": ("house_brain_engineering_inspector", "Inspector"),
    "local_house_brain_engineering_observer": ("house_brain_engineering_observer", "Observer"),
    "local_house_brain_maintenance_inventory_scout": ("house_brain_maintenance_inventory_scout", "Inventory Scout"),
}
RE_CARRIER_PATH = re.compile(r"^deploy/carriers/hb[0-9]+-[a-z0-9-]+\.tar$")
RE_COMMIT = re.compile(r"^[0-9a-f]{40}$")
RE_STAGE_VERSION = re.compile(r"^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$")
RE_UPDATE_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,39}$")
RE_APP_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")   # 0.9.22: hyphens, as ha.RE_SLUG (0.9.15)
RE_HB_APP_SLUG = re.compile(r"^(?:local|[0-9a-f]{8})_house_brain_[a-z0-9_]{1,60}$")
RE_UTC = re.compile(r"^(20[0-9]{2})-([01][0-9])-([0-3][0-9])T([0-2][0-9]):([0-5][0-9]):([0-5][0-9])Z$")
LOG_WINDOW_MAX_SECONDS = 6 * 3600
PHASES = ("prepare", "activate")
# job -> (required params, optional params)
JOBS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    ROTATE_SCOUT_KEY: (frozenset({"expect_scout_version", "phase"}), frozenset({"expect_new_fingerprint"})),
    RUN_SCOUT_ONCE: (frozenset({"expect_scout_version"}), frozenset({"expect_key_fingerprint"})),
    CHECK_BROKER: (frozenset({"expect_scout_version"}), frozenset()),
    APP_LOG_WINDOW: (frozenset({"target_slug", "since", "until"}), frozenset({"boot", "keywords"})),
    ASK_UPDATE_NOW: (frozenset({"kind", "version"}), frozenset({"slug"})),
    HOLD_UPDATE: (frozenset({"item"}), frozenset()),
    RELEASE_UPDATE: (frozenset({"item"}), frozenset()),
    STAGE_LOCAL_APP_SOURCE: (frozenset({"carrier", "sha256", "commit", "slug", "from_version", "to_version"}),
                             frozenset()),
    ROTATE_KEYS_NOW: (frozenset({"role"}), frozenset()),
}
# 0.9.4 (#985 enrollment plan R1 step E6, Deployer handoff #942 6133340527): the Maintenance half of the two-way
# busy test. Owner-approved; uses only the existing shared writer lock (never creates it) and writes nothing else.
WRITER_PROBE = "WRITER_PROBE"
PROBE_OPERATIONS = ("try", "hold")
JOBS[WRITER_PROBE] = (frozenset({"operation"}), frozenset())
# 0.9.5 (#985 Pi test plan R1 M2/M3, owner pop-up 2026-10-12 "Backup + shared folder"): back up the stopped legacy
# Deployer and stage its history. Owner-approved per phase; ``stage`` must name the pinned backup's custody digest.
LEGACY_STAGE = "LEGACY_STAGE"
LEGACY_PHASES = ("backup", "stage")
JOBS[LEGACY_STAGE] = (frozenset({"phase"}), frozenset({"custody_sha256"}))
# 0.9.24 (owner pop-up 2026-10-20 "Yes, build 0.9.24"): deliver Energy AI's private household settings once, to the
# store-installed Energy AI App only, through its Supervisor stdin in setup-only mode. It asks the owner first. The
# settings file sits next to the manifest (``household.bundle``) on this private repository's request branch and is
# pinned here by its SHA-256 and source commit; the frame itself is built on the Pi at send time (it expires in 5 min).
ENERGY_AI_SETTINGS = "ENERGY_AI_SETTINGS"
ENERGY_OPERATIONS = ("READBACK", "ACTIVATE")
JOBS[ENERGY_AI_SETTINGS] = (frozenset({"operation", "expect_energy_ai_version", "manifest_sha256"}),
                            frozenset({"bundle_sha256", "source_commit", "expected_current_sha256"}))
# 0.9.27 (history archive proposal R1 step 1; owner pop-ups 2026-10-24 "Yes, measure first" and "Yes, 0.9.27 next"):
# read one complete UTC day's history and statistics and report rows, entities, raw JSON bytes, the xz size and the
# largest entities. Read-only, no approval; nothing is kept, written or changed.
HISTORY_MEASURE = "HISTORY_MEASURE"
JOBS[HISTORY_MEASURE] = (frozenset({"day"}), frozenset({"chunk", "max_minutes"}))
NO_APPROVAL_JOBS = NO_APPROVAL_JOBS | {HISTORY_MEASURE}
RE_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MEASURE_CHUNK_DEFAULT, MEASURE_MINUTES_DEFAULT = 25, 30
# 0.9.34 (owner pop-ups 2026-10-26 "Build 0.9.34", "06:45 Arizona", "Both"): change two settings of the Google Drive
# Backup App (sabeechen/hassio-google-drive-backup) through the Supervisor: its daily backup time and whether every
# App is included (its ``exclude_addons`` list emptied). It asks the owner first and restarts that App once so the
# new settings are in use. The App's password is never read into a message, never set and never shown by this job.
# ``undo`` restores the two values the last run replaced (kept in this App's own memory).
ADDON_SETTINGS = "ADDON_SETTINGS"
ADDON_KINDS = ("google_drive_backup",)
RE_TIME_OF_DAY = re.compile(r"^[0-2][0-9]:[0-5][0-9]$")
JOBS[ADDON_SETTINGS] = (frozenset({"addon"}), frozenset({"backup_time_of_day", "include_all_addons", "undo"}))
_TOP = frozenset({"schema", "request_id", "job", "requested_by", "tracking_issue", "note", "params"})
_REQUIRED_TOP = frozenset({"schema", "request_id", "job", "requested_by", "params"})


class ManifestError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class Manifest:
    request_id: str
    job: str
    requested_by: str
    tracking_issue: int | None
    note: str
    expect_scout_version: str
    expect_key_fingerprint: str | None
    digest: str
    phase: str | None = None
    expect_new_fingerprint: str | None = None
    # 0.5.4 APP_LOG_WINDOW (the App to READ is named here; it can never be written to)
    target_slug: str | None = None
    since: float | None = None
    until: float | None = None
    boot: int = 0
    keywords: tuple[str, ...] = ()
    # 0.6.5 ASK_UPDATE_NOW (must match the update Supervisor reports as waiting right now)
    update_kind: str | None = None
    update_version: str | None = None
    update_slug: str | None = None
    # 0.7.3 HOLD_UPDATE / RELEASE_UPDATE (holds.RE_ITEM)
    hold_item: str | None = None
    # 0.7.4 STAGE_LOCAL_APP_SOURCE (validated again by stage.py before any download)
    stage_carrier: str | None = None
    stage_sha256: str | None = None
    stage_commit: str | None = None
    stage_slug: str | None = None
    stage_from: str | None = None
    stage_to: str | None = None
    # 0.8.0 ROTATE_KEYS_NOW
    key_role: str | None = None
    # 0.9.4 WRITER_PROBE
    probe_operation: str | None = None
    # 0.9.5 LEGACY_STAGE
    legacy_phase: str | None = None
    custody_sha256: str | None = None
    # 0.9.24 ENERGY_AI_SETTINGS
    energy_operation: str | None = None
    energy_version: str | None = None
    energy_manifest_sha256: str | None = None
    energy_bundle_sha256: str | None = None
    energy_source_commit: str | None = None
    energy_expected_current: str | None = None
    # 0.9.34 ADDON_SETTINGS
    addon: str | None = None
    addon_time_of_day: str | None = None
    addon_include_all: bool | None = None
    addon_undo: bool = False
    # 0.9.27 HISTORY_MEASURE
    measure_day: str | None = None
    measure_chunk: int = 25
    measure_minutes: int = 30


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ManifestError("DUPLICATE_KEY", key[:40])
        out[key] = value
    return out


def parse(raw: bytes) -> Manifest:
    if not isinstance(raw, bytes | bytearray) or len(raw) > MAX_MANIFEST_BYTES:
        raise ManifestError("SIZE")
    try:
        data = json.loads(bytes(raw).decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except ManifestError:
        raise
    except (UnicodeDecodeError, ValueError):
        raise ManifestError("JSON") from None
    if not isinstance(data, dict):
        raise ManifestError("SHAPE")
    keys = frozenset(data)
    if not keys <= _TOP or not _REQUIRED_TOP <= keys:
        raise ManifestError("KEYS")
    if data["schema"] != SCHEMA:
        raise ManifestError("SCHEMA")
    rid = data["request_id"]
    if not isinstance(rid, str) or not RE_REQUEST_ID.fullmatch(rid):
        raise ManifestError("REQUEST_ID")
    job = data["job"]
    if not isinstance(job, str) or job not in JOBS:
        raise ManifestError("JOB")
    who = data["requested_by"]
    if who not in REQUESTERS:
        raise ManifestError("REQUESTED_BY")
    issue = data.get("tracking_issue")
    if issue is not None and (isinstance(issue, bool) or not isinstance(issue, int) or not 1 <= issue <= 10**7):
        raise ManifestError("TRACKING_ISSUE")
    note = data.get("note", "")
    if not isinstance(note, str) or not RE_NOTE.fullmatch(note):
        raise ManifestError("NOTE")
    params = data["params"]
    if not isinstance(params, dict):
        raise ManifestError("PARAMS")
    required, optional = JOBS[job]
    pkeys = frozenset(params)
    if not required <= pkeys or not pkeys <= required | optional:
        raise ManifestError("PARAMS_KEYS")
    version = params.get("expect_scout_version", "")
    if "expect_scout_version" in required and (not isinstance(version, str) or not RE_VERSION.fullmatch(version)):
        raise ManifestError("EXPECT_SCOUT_VERSION")
    log = _log_window(params) if job == APP_LOG_WINDOW else {}
    log.update(_history_measure(params) if job == HISTORY_MEASURE else {})
    log.update(_ask_update(params) if job == ASK_UPDATE_NOW else {})
    if job in (HOLD_UPDATE, RELEASE_UPDATE):
        if not valid_item(params["item"]):
            raise ManifestError("HOLD_ITEM")
        log["hold_item"] = params["item"]
    if job == STAGE_LOCAL_APP_SOURCE:
        log.update(_stage(params))
    if job == ROTATE_KEYS_NOW:
        if params["role"] not in KEY_ROLES:
            raise ManifestError("KEY_ROLE")
        log["key_role"] = params["role"]
    if job == WRITER_PROBE:
        if params["operation"] not in PROBE_OPERATIONS:
            raise ManifestError("PROBE_OPERATION")
        log["probe_operation"] = params["operation"]
    if job == LEGACY_STAGE:
        phase, digest = params["phase"], params.get("custody_sha256")
        if phase not in LEGACY_PHASES or (phase == "backup") != (digest is None):
            raise ManifestError("LEGACY_PHASE")
        if digest is not None and (not isinstance(digest, str) or not RE_SHA256.fullmatch(digest)):
            raise ManifestError("CUSTODY_SHA256")
        log.update(legacy_phase=phase, custody_sha256=digest)
    if job == ENERGY_AI_SETTINGS:
        log.update(_energy(params))
    if job == ADDON_SETTINGS:
        log.update(_addon_settings(params))
    fp = params.get("expect_key_fingerprint")
    if fp is not None and (not isinstance(fp, str) or not RE_SHA256.fullmatch(fp)):
        raise ManifestError("EXPECT_KEY_FINGERPRINT")
    phase = params.get("phase")
    new_fp = params.get("expect_new_fingerprint")
    if job == ROTATE_SCOUT_KEY:
        if phase not in PHASES:
            raise ManifestError("PHASE")
        if phase == "activate" and (not isinstance(new_fp, str) or not RE_SHA256.fullmatch(new_fp)):
            raise ManifestError("EXPECT_NEW_FINGERPRINT")
        if phase == "prepare" and new_fp is not None:
            raise ManifestError("EXPECT_NEW_FINGERPRINT")
    return Manifest(
        request_id=rid,
        job=job,
        requested_by=who,
        tracking_issue=issue,
        note=note,
        expect_scout_version=version,
        expect_key_fingerprint=fp,
        digest=hashlib.sha256(bytes(raw)).hexdigest(),
        phase=phase,
        expect_new_fingerprint=new_fp,
        **log,
    )


def _energy(params: dict) -> dict:
    """READBACK names no settings file; ACTIVATE pins it (hash, source commit, the digest it replaces or null)."""
    op = params["operation"]
    if op not in ENERGY_OPERATIONS:
        raise ManifestError("ENERGY_OPERATION")
    version = params["expect_energy_ai_version"]
    if not isinstance(version, str) or not RE_STAGE_VERSION.fullmatch(version):
        raise ManifestError("ENERGY_VERSION")
    manifest = params["manifest_sha256"]
    if not isinstance(manifest, str) or not RE_SHA256.fullmatch(manifest):
        raise ManifestError("ENERGY_MANIFEST_SHA256")
    write = {"bundle_sha256", "source_commit", "expected_current_sha256"}
    if op == "READBACK":
        if write & set(params):
            raise ManifestError("ENERGY_READBACK_FIELDS")
        return {"energy_operation": op, "energy_version": version, "energy_manifest_sha256": manifest}
    if not write <= set(params):
        raise ManifestError("ENERGY_ACTIVATE_FIELDS")
    bundle, commit, current = params["bundle_sha256"], params["source_commit"], params["expected_current_sha256"]
    if not isinstance(bundle, str) or not RE_SHA256.fullmatch(bundle):
        raise ManifestError("ENERGY_BUNDLE_SHA256")
    if not isinstance(commit, str) or not RE_COMMIT.fullmatch(commit):
        raise ManifestError("ENERGY_SOURCE_COMMIT")
    if current is not None and (not isinstance(current, str) or not RE_SHA256.fullmatch(current)):
        raise ManifestError("ENERGY_EXPECTED_CURRENT")
    return {"energy_operation": op, "energy_version": version, "energy_manifest_sha256": manifest,
            "energy_bundle_sha256": bundle, "energy_source_commit": commit, "energy_expected_current": current}


def _addon_settings(params: dict) -> dict:
    """0.9.34: one known App; either ``undo`` alone, or at least one of the two settings, each well-formed."""
    addon = params["addon"]
    if addon not in ADDON_KINDS:
        raise ManifestError("ADDON")
    if "undo" in params:
        if params["undo"] is not True or set(params) != {"addon", "undo"}:
            raise ManifestError("ADDON_UNDO_FIELDS")
        return {"addon": addon, "addon_undo": True}
    if not ({"backup_time_of_day", "include_all_addons"} & set(params)):
        raise ManifestError("ADDON_NO_CHANGE")
    tod = params.get("backup_time_of_day")
    if tod is not None and (not isinstance(tod, str) or not RE_TIME_OF_DAY.fullmatch(tod) or int(tod[:2]) > 23):
        raise ManifestError("ADDON_TIME")
    include = params.get("include_all_addons")
    if include is not None and include is not True:
        raise ManifestError("ADDON_INCLUDE")
    return {"addon": addon, "addon_time_of_day": tod, "addon_include_all": include}


def _utc(value: Any, code: str) -> float:
    match = RE_UTC.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ManifestError(code)
    try:
        return datetime(*(int(g) for g in match.groups()), tzinfo=UTC).timestamp()
    except ValueError:
        raise ManifestError(code) from None


def _history_measure(params: dict) -> dict:
    """0.9.27: a real UTC calendar day, 1 to 200 entities per read, 5 to 60 minutes in all."""
    day = params["day"]
    if not isinstance(day, str) or not RE_DAY.fullmatch(day):
        raise ManifestError("MEASURE_DAY")
    try:
        time.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise ManifestError("MEASURE_DAY") from None
    chunk, minutes = params.get("chunk", MEASURE_CHUNK_DEFAULT), params.get("max_minutes", MEASURE_MINUTES_DEFAULT)
    if isinstance(chunk, bool) or not isinstance(chunk, int) or not 1 <= chunk <= 200:
        raise ManifestError("MEASURE_CHUNK")
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 5 <= minutes <= 60:
        raise ManifestError("MEASURE_MINUTES")
    return {"measure_day": day, "measure_chunk": chunk, "measure_minutes": minutes}


def _log_window(params: dict) -> dict:
    slug = params["target_slug"]
    if not isinstance(slug, str) or not RE_HB_APP_SLUG.fullmatch(slug):
        raise ManifestError("TARGET_SLUG")
    since, until = _utc(params["since"], "SINCE"), _utc(params["until"], "UNTIL")
    if not 0 < until - since <= LOG_WINDOW_MAX_SECONDS:
        raise ManifestError("WINDOW")
    boot = params.get("boot", 0)
    if isinstance(boot, bool) or not isinstance(boot, int) or not -5 <= boot <= 0:
        raise ManifestError("BOOT")
    try:
        keywords = tuple(parse_keywords(params.get("keywords")))
    except ValueError:
        raise ManifestError("KEYWORDS") from None
    return {"target_slug": slug, "since": since, "until": until, "boot": boot, "keywords": keywords}


def _ask_update(params: dict) -> dict:
    kind = params["kind"]
    if kind not in UPDATE_KINDS:
        raise ManifestError("UPDATE_KIND")
    version = params["version"]
    if not isinstance(version, str) or not RE_UPDATE_VERSION.fullmatch(version):
        raise ManifestError("UPDATE_VERSION")
    slug = params.get("slug")
    if (kind == "app") != (slug is not None) or (slug is not None and (
            not isinstance(slug, str) or not RE_APP_SLUG.fullmatch(slug))):
        raise ManifestError("UPDATE_SLUG")
    return {"update_kind": kind, "update_version": version, "update_slug": slug}


def _stage(params: dict) -> dict:
    """0.7.4: the closed STAGE_LOCAL_APP_SOURCE shape (allowlisted slug, carrier path, pins, plain versions)."""
    carrier, digest, commit, slug = params["carrier"], params["sha256"], params["commit"], params["slug"]
    if not isinstance(carrier, str) or not RE_CARRIER_PATH.fullmatch(carrier):
        raise ManifestError("STAGE_CARRIER")
    if not isinstance(digest, str) or not RE_SHA256.fullmatch(digest):
        raise ManifestError("STAGE_SHA256")
    if not isinstance(commit, str) or not RE_COMMIT.fullmatch(commit):
        raise ManifestError("STAGE_COMMIT")
    if not isinstance(slug, str) or slug not in STAGE_APPS:
        raise ManifestError("STAGE_SLUG")
    frm, to = params["from_version"], params["to_version"]
    if not isinstance(frm, str) or not isinstance(to, str) or not RE_STAGE_VERSION.fullmatch(frm) \
            or not RE_STAGE_VERSION.fullmatch(to) or frm == to:
        raise ManifestError("STAGE_VERSION")
    return {"stage_carrier": carrier, "stage_sha256": digest, "stage_commit": commit, "stage_slug": slug,
            "stage_from": frm, "stage_to": to}
