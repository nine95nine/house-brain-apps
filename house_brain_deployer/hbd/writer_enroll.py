"""0.3.19: one-tap shared writer lock enrollment (owner pop-up 2026-10-12, #985 plan R1 step E3).

A request ``house_brain_writer_enroll_request.v1`` names nothing but who asks and where to report.
The path is fixed. After the owner's Approve, ``enroll_writer`` creates ONLY an absent 0700 directory
and an absent empty 0600 lock, refusing anything already there in another shape (never repaired).
Evidence is dev/ino/mode only. A later release removes the temporary directory-create permission.
"""
from __future__ import annotations

import json
import os

from .manifest import RE_REQUEST_ID, REQUESTERS, ManifestError, _obj, _str

SCHEMA = "house_brain_writer_enroll_request.v1"
WRITER_ROOT = "/share/house_brain_writer_v1"


def root() -> str:
    if os.environ.get("HBD_TEST_MODE") == "1" and os.environ.get("HBD_WRITER_ROOT"):
        return os.environ["HBD_WRITER_ROOT"]
    return WRITER_ROOT


def is_enroll(raw: bytes) -> bool:
    try:
        value = json.loads(raw)
        return isinstance(value, dict) and value.get("schema") == SCHEMA
    except (ValueError, UnicodeDecodeError, RecursionError):
        return False


def parse(raw: bytes) -> tuple[str, int]:
    """(request_id, tracking_issue) or ManifestError. Exactly four fields; nothing selects a path."""
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ManifestError("JSON") from None
    obj = _obj(data, "writer enroll", {"schema", "request_id", "requested_by", "tracking_issue"}, set())
    if obj["schema"] != SCHEMA:
        raise ManifestError("SCHEMA")
    rid = _str(obj["request_id"], "request_id", RE_REQUEST_ID, 64)
    if obj["requested_by"] not in REQUESTERS:
        raise ManifestError("REQUESTER")
    issue = obj["tracking_issue"]
    if isinstance(issue, bool) or not isinstance(issue, int) or not 1 <= issue <= 10_000_000:
        raise ManifestError("RANGE", "tracking_issue")
    return rid, issue
