"""Strict validation of maintenance request manifests.

A manifest is untrusted repository content: any push-capable principal (including an AI
session) can write one. Validation is allow-list only. A manifest can pick a job from a
closed catalog and supply bounded expectations; it can never choose a target App, a route,
a URL or a secret. Those come from the App options and from Supervisor.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from . import SCHEMA

MAX_MANIFEST_BYTES = 16 * 1024
RE_REQUEST_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
RE_SHA256 = re.compile(r"^[0-9a-f]{64}$")
RE_VERSION = re.compile(r"^[0-9]{1,4}(\.[0-9]{1,4}){1,3}$")
RE_NOTE = re.compile(r"^[\x20-\x7e\n]{0,300}$")
REQUESTERS = ("claude", "chatgpt", "owner")

ROTATE_SCOUT_KEY = "ROTATE_SCOUT_KEY"
RUN_SCOUT_ONCE = "RUN_SCOUT_ONCE"
CHECK_BROKER = "CHECK_BROKER"   # 0.5.3: read-only, no approval: which Broker version is live
PHASES = ("prepare", "activate")
# job -> (required params, optional params)
JOBS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    ROTATE_SCOUT_KEY: (frozenset({"expect_scout_version", "phase"}), frozenset({"expect_new_fingerprint"})),
    RUN_SCOUT_ONCE: (frozenset({"expect_scout_version"}), frozenset({"expect_key_fingerprint"})),
    CHECK_BROKER: (frozenset({"expect_scout_version"}), frozenset()),
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
    version = params["expect_scout_version"]
    if not isinstance(version, str) or not RE_VERSION.fullmatch(version):
        raise ManifestError("EXPECT_SCOUT_VERSION")
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
    )
