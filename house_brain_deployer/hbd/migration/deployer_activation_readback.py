"""Inactive activation readback verifier; no I/O, mutation or trust issuer.

Inputs must eventually come from a separately qualified host provider. Matching
these supplied bytes proves consistency only, never production activation, owner
consent, native acceptance or permission to remove the legacy App.
"""
from __future__ import annotations

import hashlib
import re

from .. import history
from ..policy import check_backup_path, PolicyError
from .deployer_history_store import Scope, MAX_META, MAX_RECOVERY_FILES
from .qualify_deployer_history import _ledger_safety, _record, _timestamp, MAX_RECOVERY_BYTES


class ReadbackError(ValueError):
    """Fixed sanitized reason; never return private metadata or parser output."""


def _require(ok, reason):
    if not ok:
        raise ReadbackError(reason)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def verify_readback(seal_bytes, actual_history, actual_recovery, *, expected_pointer,
                    observed_pointer, expected_scope, observed_at, now, max_age=30):
    """Compare complete supplied post-copy bytes with an independently pinned seal.

    actual_recovery contains only the exact private inventory selected by the
    provider, including retained copies. No path lookup occurs here. Freshness
    checks are fixture-clock checks until the native reader/clock is qualified.
    """
    try:
        _require(isinstance(expected_scope, Scope), "SCOPE_INVALID")
        expected_scope.validate(now)
        _require(history._finite_number(observed_at) and history._finite_number(now)
                 and history._finite_number(max_age) and 0 < max_age <= 30
                 and 0 <= now - observed_at <= max_age, "READBACK_STALE")
        _require(isinstance(seal_bytes, bytes) and len(seal_bytes) <= MAX_META, "SEAL_BYTES")
        wanted = {"generation": "g-" + _sha(seal_bytes), "seal_sha256": _sha(seal_bytes)}
        _require(isinstance(expected_pointer, dict) and expected_pointer == wanted,
                 "SEAL_IDENTITY")
        _require(isinstance(observed_pointer, dict) and observed_pointer == wanted,
                 "ACTIVE_GENERATION_CHANGED")
        seal = history._json(seal_bytes)
        _require(isinstance(seal, dict) and set(seal) == {
            "schema", "scope", "source_sha256", "destination_sha256", "result_sha256",
            "selected", "history", "recovery", "private_recovery_index", "expected_head"}
            and seal["schema"] == "hbd.generation_preparation.v1", "SEAL_SHAPE")
        _require(seal["scope"] == vars(expected_scope), "SCOPE_MISMATCH")
        _require(all(isinstance(seal[k], str) and re.fullmatch(r"[0-9a-f]{64}", seal[k])
                     for k in ("source_sha256", "destination_sha256", "result_sha256")), "SEAL_SHAPE")
        _require(isinstance(actual_history, dict) and isinstance(actual_recovery, dict), "READBACK_BYTES")
        actual_history, actual_recovery = dict(actual_history), dict(actual_recovery)
        _require(all(isinstance(k, str) and isinstance(v, bytes)
                     for data in (actual_history, actual_recovery) for k, v in data.items()), "READBACK_BYTES")
        inventory, recovery, index = seal["history"], seal["recovery"], seal["private_recovery_index"]
        _require(isinstance(inventory, dict) and set(inventory) <= set(history.FILES)
                 and isinstance(recovery, dict) and isinstance(index, dict)
                 and len(recovery) <= MAX_RECOVERY_FILES
                 and len(index) == len(recovery), "SEAL_SHAPE")
        _require(all(isinstance(path, str) and isinstance(member, str)
                     for path, member in index.items()), "RECOVERY_INDEX")
        _require(set(index.values()) == set(recovery), "RECOVERY_INDEX")
        for path, member in index.items():
            check_backup_path(path)
            _require(member == _sha(path.encode()) + ".bak", "RECOVERY_INDEX")
        _require(all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v)
                     for data in (inventory, recovery) for v in data.values()), "SEAL_SHAPE")
        _require(set(actual_history) == set(inventory), "HISTORY_INVENTORY")
        _require(set(actual_recovery) == set(index), "RECOVERY_INVENTORY")
        _require(sum(map(len, actual_history.values())) <= history.MAX_BYTES
                 and sum(map(len, actual_recovery.values())) <= MAX_RECOVERY_BYTES, "READBACK_LIMIT")
        _require(all(_sha(raw) == inventory[name] for name, raw in actual_history.items()), "HISTORY_HASH")
        _require(all(_sha(raw) == recovery[index[path]] for path, raw in actual_recovery.items()), "RECOVERY_HASH")
        _require(history.fingerprint(actual_history) == seal["result_sha256"], "HISTORY_HASH")
        decoded = history._decode(actual_history)
        _ledger_safety(decoded)
        times = [_timestamp(event["ts"]) for event in decoded["audit"]]
        _require(all(a <= b for a, b in zip(times, times[1:])), "AUDIT_CHRONOLOGY")  # noqa: B905 - pairing kept as written (no strict)
        records = {rec["request_id"]: rec for rec in decoded["installs"]}
        built = {key: _record(rec) for key, rec in records.items()}
        selected = seal["selected"]
        _require(isinstance(selected, list) and all(isinstance(v, str) for v in selected)
                 and len(selected) == len(set(selected)) and set(selected) <= set(records), "RECOVERY_SELECTION")
        for key in selected:
            _require(not records[key].get("undone_by"), "ALREADY_UNDONE")
            for path, digest in built[key][1].values():
                _require(path in actual_recovery and _sha(actual_recovery[path]) == digest, "SELECTED_RECOVERY")
        return {"schema": "hbd.activation_readback_preparation.v1",
                "generation_sha256": wanted["seal_sha256"], "history_sha256": seal["result_sha256"],
                "history_files": len(actual_history), "install_records": len(records),
                "recovery_files": len(actual_recovery), "snapshot_consistent": True,
                "applied": False, "journal_activated": False, "capture_trusted": False,
                "native_verified": False, "undo_verified": False, "uninstall_allowed": False}
    except ReadbackError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError, PolicyError):
        raise ReadbackError("READBACK_REFUSED") from None
