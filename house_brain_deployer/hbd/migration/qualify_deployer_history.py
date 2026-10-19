"""Offline package-history qualification preparation, never an import receipt.

Consumes already private byte mappings only. No filesystem/network/engine calls,
routes, source capture, mutation, owner grants, or runtime qualification. Admission
limits refuse oversize inputs and never prune retained history or recovery files.
"""
from __future__ import annotations

import hashlib
import heapq
import re
from datetime import datetime, timezone

from .. import history, manifest, undo
from ..policy import check_backup_path, check_package_target

MAX_RECOVERY_BYTES = 32 * 1024 * 1024  # fixture admission; real inventory uncalibrated
INSTALL_FIELDS = {"request_id", "title", "summary", "tracking_issue", "at", "files", "retire",
                  "preconditions", "expect_unchanged", "log_tokens", "log_fail_levels", "settle_seconds"}
OUTCOMES = {"SUCCEEDED", "DRY_RUN_OK", "DRY_RUN_BLOCKED", "ALREADY_APPLIED", "REFUSED", "REJECTED",
            "TIMED_OUT", "ABORTED", "ROLLED_BACK", "ROLLED_BACK_RESTARTED", "FAILED_MANUAL",
            "LOOKUP_OK", "LOOKUP_FAILED", "HTTP_INSPECTION_OK",
            # Owner-approved lookups recorded by published 0.3.19-0.3.22 and by 0.3.23 itself: the live store
            # history already holds them (#561 6125839870, 6138174545), so a merge must accept them.
            "WRITER_ENROLLED", "WRITER_PROBE_ACQUIRED", "WRITER_PROBE_BUSY",
            "LEGACY_PREPARED", "LEGACY_SELECTED", "LEGACY_UNSELECTED", "LEGACY_FAILED", "LEGACY_RESET",
            # 0.3.33: the Companion requests of 0.3.30 record these; the live moved history holds them (#561
            # 2026-10-19), and without them every start after 0.3.30 was held with STARTUP_REFUSED.
            "COMPANION_KEY_READY", "COMPANION_SETUP_DONE", "COMPANION_SETUP_FAILED", "COMPANION_ALREADY_SET_UP"}
STAGES = {"DEPLOY", "DRYRUN", "RESTART", "ROLLBACK", "LOOKUP"}
UNKNOWN_DIGEST = "unknown"   # main.recover / _raw_digest when the request could not be read again


def _require(ok, reason="INSTALL_SCHEMA"):
    if not ok:
        raise history.HistoryError(reason)


def _timestamp(value):
    _require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value),
             "AUDIT_TIMESTAMP")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)  # noqa: UP017 - timezone.utc kept
    except ValueError:
        raise history.HistoryError("AUDIT_TIMESTAMP") from None


def _request_key(key):
    _require(isinstance(key, str), "LEDGER_SCHEMA")
    base = key[:-4] if key.endswith("#dry") else key
    is_undo = base.endswith(undo.SUFFIX)
    base = base[:-len(undo.SUFFIX)] if is_undo else base
    _require(manifest.RE_REQUEST_ID.fullmatch(base), "LEDGER_SCHEMA")
    return base, is_undo


def _ledger_safety(decoded):
    """Known structural semantics only: no historical approval becomes a grant."""
    ledger = decoded["ledger"]
    for key, rec in ledger.items():
        base, is_undo = _request_key(key)
        _require({"digest", "outcome", "at"} <= set(rec)
                 and set(rec) <= {"digest", "outcome", "at", "reuse_digest", "reask_of", "reasked_by", "restart_timed_out"},
                 "LEDGER_SCHEMA")
        _require(isinstance(rec["outcome"], str) and rec["outcome"] in OUTCOMES, "LEDGER_SCHEMA")
        if "restart_timed_out" in rec:
            # 0.3.27 (shapes 4 and 6): an Undo also asks RESTART, and recovery during dry run writes the
            # "#dry" key, so both carry this flag in real histories. The flag stays exact.
            _require(rec["restart_timed_out"] is True and rec["outcome"] == "ROLLED_BACK", "RESTART_TIMEOUT_SCHEMA")
        _timestamp(rec["at"])
        digest = rec["digest"]
        _require(isinstance(digest, str), "LEDGER_SCHEMA")
        if digest == UNKNOWN_DIGEST or (is_undo and manifest.RE_SHA256.fullmatch(digest)):
            # 0.3.27 (shapes 2 and 3): restart recovery records "unknown" when the request could not be read
            # again, and a recovered Undo records the rebuilt manifest's sha256. Both are past results only.
            pass
        elif is_undo:
            if digest == "undo":
                _require(rec["outcome"] == "REFUSED", "LEDGER_SCHEMA")
            else:
                _require(digest.startswith("undo:"), "LEDGER_SCHEMA")
                try:
                    at = float(digest[5:])
                except ValueError:
                    raise history.HistoryError("LEDGER_SCHEMA") from None
                _require(history._finite_number(at) and at >= 0, "LEDGER_SCHEMA")
        else:
            _require(manifest.RE_SHA256.fullmatch(digest), "LEDGER_SCHEMA")
        if "reuse_digest" in rec:
            _require(isinstance(rec["reuse_digest"], str) and manifest.RE_SHA256.fullmatch(rec["reuse_digest"]),
                     "LEDGER_SCHEMA")
        if "reask_of" in rec:
            original = rec["reask_of"]
            _require(isinstance(original, str) and manifest.RE_REQUEST_ID.fullmatch(original)
                     and original != base and not is_undo, "REASK_SCHEMA")
            # Missing targets can represent interrupted linking: retain them as
            # unresolved, never synthesize a successful result or fresh consent.
            # 0.3.27 (shape 5): a refused re-ask of a re-ask is recorded with its link, so a chain is history,
            # not a grant; the live re-ask check still refuses running one (reask.check_ledger).
        if "reasked_by" in rec:
            linked = rec["reasked_by"]
            _require(isinstance(linked, list) and all(isinstance(v, str) and manifest.RE_REQUEST_ID.fullmatch(v)
                                                     and v != base for v in linked), "REASK_SCHEMA")
            _require(len(linked) == len(set(linked)) and not is_undo, "REASK_SCHEMA")
    for rec in decoded["audit"]:
        kind = rec["event"]
        if kind == "LIMIT_LIFTED":
            _require(rec["request_id"] == "" and isinstance(rec.get("by"), str) and bool(rec["by"])
                     and history._finite_number(rec.get("until")) and rec["until"] >= 0, "LIMIT_GRANT_SCHEMA")
        elif kind in ("APPROVAL_REQUESTED", "APPROVAL_STOPPED", "APPROVAL_DECISION"):
            _request_key(rec["request_id"])
            _require(isinstance(rec.get("stage"), str) and rec["stage"] in STAGES, "APPROVAL_SCHEMA")
            if kind == "APPROVAL_DECISION":
                _require(isinstance(rec.get("outcome"), str)
                         and rec["outcome"] in {"APPROVE", "REJECT", "TIMEOUT", "UNAVAILABLE"}, "APPROVAL_SCHEMA")
                _require(type(rec.get("ignored_events")) is int and rec["ignored_events"] >= 0,
                         "APPROVAL_SCHEMA")
                # 0.3.27 (shape 1): 0.1.0 wrote no "channel"; when present it is still a string.
                _require(isinstance(rec.get("detail"), str) and isinstance(rec.get("channel", ""), str),
                         "APPROVAL_SCHEMA")
        elif kind == "OUTCOME":
            _request_key(rec["request_id"])
            _require(isinstance(rec.get("outcome"), str) and rec["outcome"] in OUTCOMES, "OUTCOME_SCHEMA")
            _require(isinstance(rec.get("reasons"), list)
                     and all(isinstance(v, str) for v in rec["reasons"]), "OUTCOME_SCHEMA")


def _record(rec):
    """Validate the complete record_success shape before invoking actual Undo builder."""
    _require(set(rec) in (INSTALL_FIELDS, INSTALL_FIELDS | {"undone_by", "undone_at"}))
    _require(isinstance(rec["request_id"], str) and manifest.RE_REQUEST_ID.fullmatch(rec["request_id"]))
    _require(history._finite_number(rec["at"]) and rec["at"] >= 0)
    for key, limit in (("title", 80), ("summary", 160)):
        _require(isinstance(rec[key], str) and len(rec[key]) <= limit)
    _require(type(rec["tracking_issue"]) is int and 1 <= rec["tracking_issue"] <= 10_000_000)
    _require(type(rec["settle_seconds"]) is int and 60 <= rec["settle_seconds"] <= 900)
    _require(isinstance(rec["files"], list) and len(rec["files"]) <= manifest.MAX_FILES)
    _require(isinstance(rec["retire"], list) and len(rec["retire"]) <= manifest.MAX_RETIRE)
    _require(rec["files"] or rec["retire"])
    touched = []
    for op in rec["files"]:
        _require(isinstance(op, dict) and set(op) == {"target", "mode", "backup_as", "pre", "post"})
        _require(isinstance(op["target"], str))
        check_package_target(op["target"])
        touched.append(op["target"])
        _require(isinstance(op["post"], str) and manifest.RE_SHA256.fullmatch(op["post"]))
        if op["mode"] == "replace":
            _require(isinstance(op["pre"], str) and manifest.RE_SHA256.fullmatch(op["pre"]))
            _require(isinstance(op["backup_as"], str))
            check_backup_path(op["backup_as"])
            _require(op["backup_as"].startswith(op["target"] + "."))
            touched.append(op["backup_as"])
        else:
            _require(op["mode"] == "create" and op["pre"] is None and op["backup_as"] is None)
    for op in rec["retire"]:
        _require(isinstance(op, dict) and set(op) == {"path", "rename_to", "sha"})
        _require(isinstance(op["path"], str) and isinstance(op["rename_to"], str))
        check_package_target(op["path"])
        check_backup_path(op["rename_to"])
        _require(op["rename_to"].startswith(op["path"] + "."))
        _require(isinstance(op["sha"], str) and manifest.RE_SHA256.fullmatch(op["sha"]))
        touched.extend((op["path"], op["rename_to"]))
    _require(len(touched) == len(set(touched)), "INSTALL_PATH_COLLISION")
    _require(isinstance(rec["preconditions"], list) and len(rec["preconditions"]) <= manifest.MAX_PRECONDITIONS)
    # asdict(StateCheck) emits all five fields, including inactive None fields.
    for check in rec["preconditions"]:
        _require(isinstance(check, dict) and set(check) == {"entity_id", "attribute", "equals", "one_of", "number"})
        active = {k: v for k, v in check.items() if v is not None}
        manifest._state_check(active, "private history")
    for key, pattern, limit in (("expect_unchanged", manifest.RE_ENTITY, manifest.MAX_UNCHANGED),
                                ("log_tokens", manifest.RE_LOG_TOKEN, 8)):
        _require(isinstance(rec[key], list) and len(rec[key]) <= limit)
        _require(all(isinstance(v, str) and pattern.fullmatch(v) for v in rec[key]))
    levels = rec["log_fail_levels"]
    _require(isinstance(levels, list) and 2 <= len(levels) <= 3 and len(set(levels)) == len(levels))
    _require(all(v in manifest.LOG_LEVELS for v in levels) and {"ERROR", "CRITICAL"} <= set(levels))
    if "undone_by" in rec:
        _require(rec["undone_by"] == rec["request_id"] + undo.SUFFIX)
        # 0.3.27 (shape 7): both stamps come from time.time(); the Pi has no clock battery and NTP can step it
        # back, so the Undo time is only required to be a real time, not later than the install.
        _require(history._finite_number(rec["undone_at"]) and rec["undone_at"] >= 0)
    return undo.build(rec)


def plan_chronology(source, destination, *, now):
    """Offline interleaving proposal preserving each stream's occurrence order.

    Reuses staging's exact multiset-maximum overlap rule. Matching the nth equal
    event is a model assumption, not proof of common capture lineage. A shared
    same-second ordering cycle refuses rather than inventing a causal order.
    """
    source = dict(source) if isinstance(source, dict) else source
    destination = dict(destination) if isinstance(destination, dict) else destination
    base = history.plan(source, destination, now=now)
    streams = [history._decode(files)["audit"] for files in (destination, source)]
    records, timestamps, order, edges, degree = {}, {}, {}, {}, {}
    for stream in streams:
        seen, previous, last_time = {}, None, None
        for rec in stream:
            ts = _timestamp(rec["ts"])
            _require(last_time is None or last_time <= ts, "AUDIT_CHRONOLOGY")
            last_time = ts
            raw = history._canonical(rec)
            ordinal = seen.get(raw, 0)
            seen[raw] = ordinal + 1
            node = (raw, ordinal)
            if node not in records:
                order[node] = len(order)
                records[node], timestamps[node], edges[node], degree[node] = rec, ts, set(), 0
            if previous is not None and node not in edges[previous]:
                edges[previous].add(node)
                degree[node] += 1
            previous = node
    _require(len(records) <= history.MAX_EVENTS, "EVENT_LIMIT")
    ready = [(timestamps[n], order[n], n) for n in records if degree[n] == 0]
    heapq.heapify(ready)
    merged = []
    while ready:
        _, _, node = heapq.heappop(ready)
        merged.append(records[node])
        for successor in edges[node]:
            degree[successor] -= 1
            if degree[successor] == 0:
                heapq.heappush(ready, (timestamps[successor], order[successor], successor))
    _require(len(merged) == len(records), "AUDIT_ORDER_CONFLICT")
    files = base.private_files()
    files["audit.jsonl"] = b"".join(history._canonical(rec) + b"\n" for rec in merged)
    decoded = history._decode(files)
    counts = {key: len(decoded[key]) for key in ("ledger", "installs", "audit")}
    return history.Plan(base.source_sha256, base.destination_sha256, history.fingerprint(files), files, counts)


def required_recovery(source, destination, *, selected, now):
    """Derive exact selected backup paths from fully validated byte histories.

    Comparison inventory only, never authenticated custody or owner consent.
    """
    _require(type(selected) is tuple and all(type(v) is str for v in selected)
             and len(selected) == len(set(selected)), "RECOVERY_SELECTION")
    try:
        planned = plan_chronology(source, destination, now=now)
        decoded = history._decode(planned.private_files())
        for files in (source, destination, planned.private_files()):
            _ledger_safety(history._decode(files))
        # Check the proposal independently again, without changing either input.
        for files in (source, destination, planned.private_files()):
            times = [_timestamp(rec["ts"]) for rec in history._decode(files)["audit"]]
            _require(all(a <= b for a, b in zip(times, times[1:])), "AUDIT_CHRONOLOGY")  # noqa: B905 - pairing kept as written (no strict)
        records = {rec["request_id"]: rec for rec in decoded["installs"]}
        built = {key: _record(rec) for key, rec in records.items()}
        _require(set(selected) <= records.keys(), "RECOVERY_SELECTION")
        required = {}
        for key in selected:
            _require(not records[key].get("undone_by"), "ALREADY_UNDONE")
            for path, sha in built[key][1].values():
                _require(path not in required or required[path] == sha, "RECOVERY_CONFLICT")
                required[path] = sha
        return planned, dict(required)
    except history.HistoryError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
        raise history.HistoryError("INSTALL_SCHEMA") from None


def qualify(source: dict[str, bytes], destination: dict[str, bytes], recovery: dict[str, bytes],
            *, selected: tuple[str, ...], now: float) -> dict:
    """Refuse incomplete schema/chronology or missing selected actual recovery bytes.

    Strict known package schema only; unsupported metadata refuses without editing
    any source bytes. Selection is an explicit offline fixture input, not owner
    authorization. Hash equality never proves capture trust or actual restoration.
    """
    try:
        source = dict(source) if isinstance(source, dict) else source
        destination = dict(destination) if isinstance(destination, dict) else destination
        _require(isinstance(selected, tuple) and all(isinstance(v, str) for v in selected)
                 and len(selected) == len(set(selected)), "RECOVERY_SELECTION")
        _require(isinstance(recovery, dict) and all(isinstance(k, str) and isinstance(v, bytes)
                                                  for k, v in recovery.items()), "RECOVERY_BYTES")
        recovery = dict(recovery)
        _require(sum(map(len, recovery.values())) <= MAX_RECOVERY_BYTES, "RECOVERY_SIZE")
        planned, required = required_recovery(source, destination, selected=selected, now=now)
        _require(set(recovery) == set(required), "RECOVERY_INVENTORY")
        _require(all(hashlib.sha256(recovery[p]).hexdigest() == sha for p, sha in required.items()),
                 "RECOVERY_HASH")
        receipt = planned.receipt()
        receipt.update(schema="hbd.offline_history_recovery.v1", install_schema_checked=True,
                       ledger_schema_checked=True, known_safety_structure_checked=True,
                       timestamp_order_checked=True, selected_records=len(selected),
                       recovery_files=len(required), recovery_bytes=sum(map(len, recovery.values())),
                       restore_executed=False, capture_trusted=False)
        return receipt
    except history.HistoryError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
        raise history.HistoryError("INSTALL_SCHEMA") from None
