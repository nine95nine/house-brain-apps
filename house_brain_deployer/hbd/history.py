"""Inactive private-history staging; no importer, route or execution authority.

Only hash/count receipts may leave the local process. Private snapshots and
planned bytes are never a completed-migration receipt or proof of Undo validity.
The caller must separately establish stopped-state, custody and writer exclusion.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import stat
from collections import Counter
from dataclasses import dataclass, field

FILES = ("ledger.json", "installs.json", "audit.jsonl", "FROZEN.json", "txn.json")
MAX_BYTES = 8 * 1024 * 1024  # staging admission only; never a retention/delete rule
MAX_EVENTS = 20_000
MAX_DEPTH = 64


class HistoryError(ValueError):
    """Fixed reason only: never include private bytes, paths or parser messages."""


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise HistoryError("DUPLICATE_KEY")
        result[key] = value
    return result


def _nonfinite(_):
    raise HistoryError("NONFINITE")


def _json(raw):
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_nonfinite)
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            if depth > MAX_DEPTH:
                raise HistoryError("DEPTH_LIMIT")
            if isinstance(item, dict):
                for key in item:
                    key.encode("utf-8")
                pending.extend((v, depth + 1) for v in item.values())
            elif isinstance(item, list):
                pending.extend((v, depth + 1) for v in item)
            elif isinstance(item, float) and not math.isfinite(item):
                raise HistoryError("NONFINITE")
            elif isinstance(item, str):
                item.encode("utf-8")
        _canonical(value)  # rejects unpaired surrogates as well
        return value
    except (ValueError, UnicodeError, RecursionError, OverflowError) as err:
        code = str(err) if isinstance(err, HistoryError) else "MALFORMED_HISTORY"
        raise HistoryError(code) from None


def _finite_number(value) -> bool:
    try:
        return (not isinstance(value, bool) and isinstance(value, (int, float))
                and math.isfinite(value))
    except OverflowError:
        return False


def _decode(files):
    if not isinstance(files, dict) or set(files) - set(FILES):
        raise HistoryError("UNSUPPORTED_FILE")
    if any(not isinstance(raw, bytes) for raw in files.values()):
        raise HistoryError("BYTES_REQUIRED")
    if sum(map(len, files.values())) > MAX_BYTES:
        raise HistoryError("SIZE_LIMIT")
    def get(name, default):
        return _json(files[name]) if name in files else default
    if get("txn.json", None) is not None:
        raise HistoryError("TRANSACTION_PRESENT")
    ledger, installs, freeze = get("ledger.json", {}), get("installs.json", []), get("FROZEN.json", None)
    if not isinstance(ledger, dict) or not isinstance(installs, list):
        raise HistoryError("BAD_SHAPE")
    if any(not key or not isinstance(rec, dict) for key, rec in ledger.items()):
        raise HistoryError("BAD_LEDGER")
    ids = set()
    for rec in installs:
        if (not isinstance(rec, dict) or not isinstance(rec.get("request_id"), str)
                or not rec["request_id"] or rec["request_id"] in ids
                or not _finite_number(rec.get("at"))):
            raise HistoryError("BAD_INSTALL")
        ids.add(rec["request_id"])
        # Preserve unknown fields without claiming complete Undo schema validation.
        if any(not isinstance(rec.get(key, []), list) for key in ("files", "retire")):
            raise HistoryError("BAD_INSTALL")
    if freeze is not None and (not isinstance(freeze, dict) or set(freeze) != {"request_id"}
                               or not isinstance(freeze["request_id"], str) or not freeze["request_id"]):
        raise HistoryError("BAD_FREEZE")
    lines = files.get("audit.jsonl", b"").split(b"\n")
    if lines[-1] == b"":
        lines.pop()
    if len(lines) > MAX_EVENTS:
        raise HistoryError("EVENT_LIMIT")
    audit = []
    for line in lines:
        rec = _json(line)
        if (not isinstance(rec, dict)
                or any(not isinstance(rec.get(key), str) for key in ("ts", "event", "request_id"))):
            raise HistoryError("BAD_AUDIT")
        if rec["event"] == "LIMIT_LIFTED" and not _finite_number(rec.get("until")):
            raise HistoryError("BAD_LIMIT_GRANT")
        audit.append(rec)
    return {"ledger": ledger, "installs": installs, "freeze": freeze, "audit": audit}


def fingerprint(files: dict[str, bytes]) -> str:
    """Includes absence versus explicit empty files, not just parsed values."""
    return hashlib.sha256(_canonical({key: hashlib.sha256(raw).hexdigest()
                                    for key, raw in sorted(files.items())})).hexdigest()


def _signature(st):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def read_snapshot(root: str) -> dict[str, bytes]:
    """Read only fixed names relative to a held Linux directory descriptor.

    Metadata checks detect observed drift; they are not writer exclusion or a
    proof against a privileged adversary able to change and restore everything.
    No caller is wired to this helper in the normal App or public lookup API.
    """
    if not isinstance(root, str) or not os.path.isabs(root) or os.path.realpath(root) != root:
        raise HistoryError("UNSAFE_ROOT")
    root_fd = None
    handles = {}
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        root_identity = _signature(os.fstat(root_fd))
        before = {}
        # Capture all identities/absences before reading any bytes.
        for name in FILES:
            try:
                before[name] = _signature(os.stat(name, dir_fd=root_fd, follow_symlinks=False))
            except FileNotFoundError:
                before[name] = None
        result = {}
        total = 0
        for name in FILES:
            if before[name] is None:
                continue
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
            handles[name] = fd
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise HistoryError("UNSAFE_FILE")
            if _signature(st) != before[name]:
                raise HistoryError("SNAPSHOT_DRIFT")
            total += st.st_size
            if total > MAX_BYTES:
                raise HistoryError("SIZE_LIMIT")
            chunks, left = [], st.st_size + 1
            while left:
                chunk = os.read(fd, min(left, 65_536))
                if not chunk:
                    break
                chunks.append(chunk)
                left -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) != st.st_size or _signature(os.fstat(fd)) != before[name]:
                raise HistoryError("SNAPSHOT_DRIFT")
            result[name] = raw
        for name in FILES:
            try:
                after = _signature(os.stat(name, dir_fd=root_fd, follow_symlinks=False))
            except FileNotFoundError:
                after = None
            if before[name] != after or (name in handles and _signature(os.fstat(handles[name])) != after):
                raise HistoryError("SNAPSHOT_DRIFT")
        if _signature(os.stat(root, follow_symlinks=False)) != root_identity:
            raise HistoryError("SNAPSHOT_DRIFT")
        _decode(result)
        return result
    except (OSError, ValueError, OverflowError) as err:
        code = str(err) if isinstance(err, HistoryError) else "HISTORY_UNAVAILABLE"
        raise HistoryError(code) from None
    finally:
        for fd in handles.values():
            os.close(fd)
        if root_fd is not None:
            os.close(root_fd)


@dataclass(frozen=True)
class Plan:
    source_sha256: str
    destination_sha256: str
    result_sha256: str
    _files: dict[str, bytes] = field(repr=False)
    _counts: dict[str, int] = field(repr=False)

    def private_files(self) -> dict[str, bytes]:
        """Local staging only; a detached copy, never a remote response."""
        return dict(self._files)

    def receipt(self) -> dict:
        return {"schema": "hbd.history_staging.v1", "source_sha256": self.source_sha256,
                "destination_sha256": self.destination_sha256, "result_sha256": self.result_sha256,
                "files": {key: hashlib.sha256(raw).hexdigest() for key, raw in self._files.items()},
                "counts": dict(self._counts), "applied": False, "runtime_verified": False,
                "undo_verified": False, "stopped_verified": False, "uninstall_allowed": False}


def plan(source: dict[str, bytes], destination: dict[str, bytes], *, now: float) -> Plan:
    """Validate and plan locally. Never apply or grant migration permission."""
    if not _finite_number(now):
        raise HistoryError("BAD_CLOCK")
    # Detach caller-owned mappings before validation and fingerprinting; bytes
    # are immutable. A caller changing its mapping must not rebind our receipt.
    source = dict(source) if isinstance(source, dict) else source
    destination = dict(destination) if isinstance(destination, dict) else destination
    old, new = _decode(source), _decode(destination)
    if any(rec["event"] == "LIMIT_LIFTED" and rec["until"] > now for rec in old["audit"]):
        raise HistoryError("ACTIVE_SOURCE_LIMIT_GRANT")
    ledger = copy.deepcopy(new["ledger"])
    for key, rec in old["ledger"].items():
        if key in ledger and _canonical(ledger[key]) != _canonical(rec):
            raise HistoryError("LEDGER_CONFLICT")
        ledger[key] = copy.deepcopy(rec)
    installs = {rec["request_id"]: copy.deepcopy(rec) for rec in new["installs"]}
    for rec in old["installs"]:
        key = rec["request_id"]
        if key in installs and _canonical(installs[key]) != _canonical(rec):
            raise HistoryError("INSTALL_CONFLICT")
        installs[key] = copy.deepcopy(rec)
    if old["freeze"] and new["freeze"] and old["freeze"] != new["freeze"]:
        raise HistoryError("FREEZE_CONFLICT")
    # Keep destination order, append only surplus source occurrences. Exact
    # multiset maximum is replay-safe while preserving genuine repeated events.
    audit = copy.deepcopy(new["audit"])
    remaining = Counter(_canonical(rec) for rec in new["audit"])
    for rec in old["audit"]:
        key = _canonical(rec)
        if remaining[key]:
            remaining[key] -= 1
        else:
            audit.append(copy.deepcopy(rec))
    if len(audit) > MAX_EVENTS:
        raise HistoryError("EVENT_LIMIT")
    files = {"ledger.json": _canonical(ledger),
             "installs.json": _canonical(sorted(installs.values(), key=lambda rec: (rec["at"], rec["request_id"]))),
             "audit.jsonl": b"".join(_canonical(rec) + b"\n" for rec in audit)}
    frozen = old["freeze"] or new["freeze"]
    if frozen:
        files["FROZEN.json"] = _canonical(frozen)
    _decode(files)  # merged bounds too; never silently drop oldest records
    return Plan(fingerprint(source), fingerprint(destination), fingerprint(files), files,
                {"ledger": len(ledger), "installs": len(installs), "audit": len(audit)})
