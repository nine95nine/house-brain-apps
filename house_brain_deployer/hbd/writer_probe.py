"""0.3.21: owner-approved shared writer lock probe (#985 enrollment plan R1 step E6, Deployer half).

A request ``house_brain_writer_probe_request.v1`` names who asks, where to report and one operation:

* ``try``  - take the lock without waiting; report ``ACQUIRED`` or ``WRITER_BUSY``; release at once.
* ``hold`` - take the lock and keep it for a fixed 10 minutes (0.3.22; was 60 s; no field changes that), re-checking its identity
             every second; then release.

Both use ``WriterLease(create=False)``: a missing lock is refused, never created. Nothing else is read or
written. Evidence is the lock's dev/ino and the outcome only.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable

from .manifest import RE_REQUEST_ID, REQUESTERS, ManifestError, _obj, _str
from .writer import LOCK_NAME, WriterError, WriterLease

SCHEMA = "house_brain_writer_probe_request.v1"
OPERATIONS = ("try", "hold")
# 0.3.22 (owner pop-up 2026-10-13 "Longer hold"): 60 s was too short for the other App's prompt to arrive and be
# approved inside the hold (E6 rounds 1a-1c, #942 6136601251). Holding only makes the other App wait.
HOLD_SECONDS = 600


def hold_seconds() -> float:
    if os.environ.get("HBD_TEST_MODE") == "1" and os.environ.get("HBD_WRITER_PROBE_SECONDS"):
        return float(os.environ["HBD_WRITER_PROBE_SECONDS"])
    return float(HOLD_SECONDS)


def is_probe(raw: bytes) -> bool:
    try:
        value = json.loads(raw)
        return isinstance(value, dict) and value.get("schema") == SCHEMA
    except (ValueError, UnicodeDecodeError, RecursionError):
        return False


def parse(raw: bytes) -> tuple[str, int, str]:
    """(request_id, tracking_issue, operation) or ManifestError. Exactly five fields; nothing selects a path."""
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ManifestError("JSON") from None
    obj = _obj(data, "writer probe", {"schema", "request_id", "requested_by", "tracking_issue", "operation"},
               set())
    if obj["schema"] != SCHEMA:
        raise ManifestError("SCHEMA")
    rid = _str(obj["request_id"], "request_id", RE_REQUEST_ID, 64)
    if obj["requested_by"] not in REQUESTERS:
        raise ManifestError("REQUESTER")
    issue = obj["tracking_issue"]
    if isinstance(issue, bool) or not isinstance(issue, int) or not 1 <= issue <= 10_000_000:
        raise ManifestError("RANGE", "tracking_issue")
    if obj["operation"] not in OPERATIONS:
        raise ManifestError("OPERATION")
    return rid, issue, obj["operation"]


def run(root: str, operation: str, *, seconds: float | None = None,
        sleep: Callable[[float], None] = time.sleep, should_stop: Callable[[], bool] = lambda: False) -> dict:
    """Return evidence; raises WriterError for any refusal other than WRITER_BUSY (which is a result)."""
    if operation not in OPERATIONS:
        raise WriterError("WRITER_PROBE_OPERATION")
    lease = WriterLease(root, create=False)
    try:
        lease.__enter__()
    except WriterError as err:
        if str(err) == "WRITER_BUSY":
            busy = {"schema": "hbd.writer_probe.v1", "operation": operation, "result": "WRITER_BUSY",
                    "held_seconds": 0}
            # 0.3.23: name the lock found busy (E6 gap, #561 6138174545); stat only, never opened or created.
            try:
                st = os.stat(os.path.join(root, LOCK_NAME), follow_symlinks=False)
            except OSError:
                return busy
            return {**busy, "dev": st.st_dev, "ino": st.st_ino}
        raise
    try:
        st = os.fstat(lease._file)  # noqa: SLF001 - identity of the exact inode held
        held = 0.0
        if operation == "hold":
            total = hold_seconds() if seconds is None else seconds
            while held < total and not should_stop():
                step = min(1.0, total - held)
                sleep(step)
                held += step
                lease.check()
        lease.check()
        return {"schema": "hbd.writer_probe.v1", "operation": operation, "result": "ACQUIRED",
                "dev": st.st_dev, "ino": st.st_ino, "held_seconds": round(held, 1)}
    finally:
        lease.close()
