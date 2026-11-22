"""Durable D5 dashboard intent and read-only crash reconciliation.

No dashboard write is performed by this module. Pending intent is written
only AFTER a genuine authenticated owner DEPLOY approval and successful
partial backup. On restart, recover_result NEVER resumes a write: it reads
the current HA dashboard and reports an exact hash match, absence, or
FAILED_MANUAL for any uncertain/concurrent state.

Keep this separate from the package-install txn.json; the existing
journal/lease/recovery model remains authoritative for package operations.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import dashboard_d5, dashboards
from .engine import FAILED_MANUAL, Result
from .ha import dashboard_storage_id_matches
from .manifest import RE_REQUEST_ID

SCHEMA = "hbd_dashboard_d5_pending.v1"
NAME = "dashboard_d5_pending.json"
MAX_BYTES = 4096


class D5RecoveryError(ValueError):
    pass


def _filepath(root: str) -> str:
    return os.path.join(root, NAME)


def exists(root: str) -> bool:
    return os.path.lexists(_filepath(root))


def _fsync_dir(folder: str) -> None:
    fd = os.open(folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic(path: str, data: dict) -> None:
    raw = (json.dumps(data, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise D5RecoveryError("D5_PENDING_TOO_LARGE")
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    temporary = path + ".next"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        _fsync_dir(parent)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _validated(data: object) -> dict:
    if type(data) is not dict or set(data) != {
        "schema", "request_id", "digest", "ledger_key", "kind",
        "tracking_issue", "dashboard", "before", "after", "created_id", "reask_of"
    }:
        raise D5RecoveryError("D5_PENDING_FIELDS")
    if data["schema"] != SCHEMA:
        raise D5RecoveryError("D5_PENDING_SCHEMA")
    if not isinstance(data["request_id"], str) or not RE_REQUEST_ID.fullmatch(data["request_id"]):
        raise D5RecoveryError("D5_PENDING_ID")
    if not isinstance(data["digest"], str) or not dashboard_d5.RE_SHA.fullmatch(data["digest"]):
        raise D5RecoveryError("D5_PENDING_DIGEST")
    if data["ledger_key"] not in {data["request_id"], data["request_id"] + "#dry"}:
        raise D5RecoveryError("D5_PENDING_LEDGER")
    if data["ledger_key"].endswith("#dry"):
        raise D5RecoveryError("D5_PENDING_DRY_WRITE")
    if data["kind"] not in {"patch", "create"}:
        raise D5RecoveryError("D5_PENDING_KIND")
    if type(data["tracking_issue"]) is not int or not 1 <= data["tracking_issue"] <= 10_000_000:
        raise D5RecoveryError("D5_PENDING_ISSUE")
    if not isinstance(data["dashboard"], str) or not dashboard_d5.RE_URL.fullmatch(data["dashboard"]):
        raise D5RecoveryError("D5_PENDING_DASHBOARD")
    if data["kind"] == "patch":
        if not isinstance(data["before"], str) or not dashboard_d5.RE_SHA.fullmatch(data["before"]):
            raise D5RecoveryError("D5_PENDING_BEFORE")
    elif data["before"] is not None:
        raise D5RecoveryError("D5_PENDING_CREATE_BEFORE")
    if not isinstance(data["after"], str) or not dashboard_d5.RE_SHA.fullmatch(data["after"]):
        raise D5RecoveryError("D5_PENDING_AFTER")
    rid = data["reask_of"]
    if rid is not None and (not isinstance(rid, str) or not RE_REQUEST_ID.fullmatch(rid)
                            or rid == data["request_id"]):
        raise D5RecoveryError("D5_PENDING_REASK")
    # Creation may fail before an ID is returned; a missing ID is never
    # sufficient evidence to clean up a created dashboard.
    cid = data["created_id"]
    if cid is not None and not dashboard_storage_id_matches(cid, data["dashboard"]):
        raise D5RecoveryError("D5_PENDING_CREATED_ID")
    if data["kind"] == "patch" and cid is not None:
        raise D5RecoveryError("D5_PENDING_PATCH_ID")
    return data


def begin(root: str, req: dashboard_d5.D5Request, *,
          ledger_key: str | None = None, reask_of: str | None = None) -> dict:
    """Fenced by Deployer writer lock; fail if any uncertain predecessor exists."""
    if exists(root):
        raise D5RecoveryError("D5_PENDING_EXISTS")
    record = _validated({
        "schema": SCHEMA,
        "request_id": req.request_id,
        "digest": req.digest,
        "ledger_key": ledger_key or req.request_id,
        "kind": req.kind,
        "tracking_issue": req.tracking_issue,
        "dashboard": req.dashboard,
        "before": req.expect_config_sha256,
        "after": req.expect_candidate_sha256,
        "created_id": None,
        "reask_of": reask_of,
    })
    _atomic(_filepath(root), record)
    return record


def created(root: str, req_id: str, returned_id: str) -> None:
    """Persist Core's exact new ID before attempting to save its configuration."""
    record = load(root)
    if not dashboard_storage_id_matches(returned_id, record["dashboard"]):
        raise D5RecoveryError("D5_CREATED_ID_UNKNOWN")
    if record["request_id"] != req_id or record["kind"] != "create" or record["created_id"] is not None:
        raise D5RecoveryError("D5_PENDING_CREATE_MISMATCH")
    record["created_id"] = returned_id
    _atomic(_filepath(root), _validated(record))


def load(root: str) -> dict:
    p = _filepath(root)
    if not os.path.isfile(p) or os.path.islink(p):
        raise D5RecoveryError("D5_PENDING_MISSING_OR_LINK")
    with open(p, "rb") as f:
        raw = f.read(MAX_BYTES + 1)
    if not raw or len(raw) > MAX_BYTES:
        raise D5RecoveryError("D5_PENDING_SIZE")

    def reject_dupes(pairs):
        obj = {}
        for k, v in pairs:
            if k in obj:
                raise D5RecoveryError("D5_PENDING_DUPLICATE")
            obj[k] = v
        return obj
    try:
        data = json.loads(raw, object_pairs_hook=reject_dupes,
                          parse_constant=lambda _x: (_ for _ in ()).throw(D5RecoveryError("D5_PENDING_NONFINITE")))
    except (ValueError, RecursionError) as e:
        raise D5RecoveryError("D5_PENDING_JSON") from e
    return _validated(data)


def clear(root: str, req_id: str, digest: str) -> None:
    """Call only after the final ledger outcome was durably recorded."""
    record = load(root)
    if record["request_id"] != req_id or record["digest"] != digest:
        raise D5RecoveryError("D5_CLEAR_MISMATCH")
    os.unlink(_filepath(root))
    _fsync_dir(root)


def recover_result(ha, record: dict) -> Result:
    """Inspect existing HA only; no write, retry, approval or cleanup."""
    from .dashboard_d5_runner import _existing
    record = _validated(dict(record))
    try:
        row = _existing(ha, record["dashboard"])
        if record["kind"] == "patch":
            if row is None or row.get("mode") != "storage":
                return Result(FAILED_MANUAL, ["D5 recovery: original dashboard identity unverified"])
            current = dashboards.config_sha256(dashboards.read_config(ha, record["dashboard"]))
            if current == record["after"]:
                return Result("D5_DASHBOARD_PATCHED", ["D5 recovered exact postwrite SHA; no replay"])
            if current == record["before"]:
                return Result("D5_DASHBOARD_FAILED", ["D5 original unchanged; no replay"])
            return Result(FAILED_MANUAL, ["D5 recovery: concurrent or unexpected dashboard state; no replay"])

        if row is None:
            return Result("D5_DASHBOARD_FAILED", ["D5 create absent; no replay"])
        # A new dashboard must have the exact original Core identity before any
        # post-crash success is inferred. No ID is never positive proof.
        if record["created_id"] is None or row.get("id") != record["created_id"] or row.get("mode") != "storage":
            return Result(FAILED_MANUAL, ["D5 recovery: created dashboard identity unverified"])
        current = dashboards.config_sha256(dashboards.read_config(ha, record["dashboard"]))
        if current == record["after"]:
            return Result("D5_DASHBOARD_CREATED", ["D5 recovered exact created dashboard SHA; no replay"])
        return Result(FAILED_MANUAL, ["D5 recovery: created dashboard incomplete/changed; no replay"])
    except Exception:  # noqa: BLE001 - no raw dashboard content in results
        return Result(FAILED_MANUAL, ["D5 recovery: dashboard unreadable; no replay"])
