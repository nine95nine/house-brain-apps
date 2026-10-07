"""Strict validation of maintenance request manifests.

A manifest is untrusted repository content: any push-capable principal (including an AI
session) can write one. Validation is allow-list only. A manifest can pick a job from a
closed catalog and supply bounded expectations; it can never choose a target App to change, a
route, a URL or a secret. Those come from the App options and from Supervisor. The one exception
(0.5.4, owner decision 2026-10-06) is ``APP_LOG_WINDOW``: it names one installed House Brain App
whose log is **read**; nothing is ever written to that App. ``ASK_UPDATE_NOW`` (0.6.5, owner decision
2026-10-07) names an update only to *match* the one this App already reviewed and asked about and Supervisor
still reports as waiting; it can never pick a new target, a version or a route, and anything else is REFUSED.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from . import SCHEMA
from .logwindow import parse_keywords

MAX_MANIFEST_BYTES = 16 * 1024
RE_REQUEST_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
RE_SHA256 = re.compile(r"^[0-9a-f]{64}$")
RE_VERSION = re.compile(r"^[0-9]{1,4}(\.[0-9]{1,4}){1,3}$")
RE_NOTE = re.compile(r"^[\x20-\x7e\n]{0,300}$")
REQUESTERS = ("claude", "chatgpt", "owner")

ROTATE_SCOUT_KEY = "ROTATE_SCOUT_KEY"
RUN_SCOUT_ONCE = "RUN_SCOUT_ONCE"
CHECK_BROKER = "CHECK_BROKER"   # 0.5.3: read-only, no approval: which Broker version is live
APP_LOG_WINDOW = "APP_LOG_WINDOW"   # 0.5.4: read-only, no approval: one House Brain App's log in a UTC window
# 0.6.4: jobs that never ask the owner anything. The daily approval-ask limit does not hold them back (live
# 2026-10-07: an APP_LOG_WINDOW request waited 3 hours behind four unrelated approval asks).
NO_APPROVAL_JOBS = frozenset({CHECK_BROKER, APP_LOG_WINDOW})
# 0.6.5: ask the phone again now for a waiting Core/OS/App update this App already reviewed (no re-ask wait).
# It asks the owner, so it is NOT in NO_APPROVAL_JOBS: it counts against max_approval_requests_per_day.
ASK_UPDATE_NOW = "ASK_UPDATE_NOW"
UPDATE_KINDS = ("core", "os", "app")
RE_UPDATE_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,39}$")
RE_APP_SLUG = re.compile(r"^[a-z0-9][a-z0-9_]{0,99}$")
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
}
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
    log.update(_ask_update(params) if job == ASK_UPDATE_NOW else {})
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


def _utc(value: Any, code: str) -> float:
    match = RE_UTC.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ManifestError(code)
    try:
        return datetime(*(int(g) for g in match.groups()), tzinfo=UTC).timestamp()
    except ValueError:
        raise ManifestError(code) from None


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
