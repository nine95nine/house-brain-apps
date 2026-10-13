"""Durable state under /data: transaction journal, append-only audit, ledger."""
from __future__ import annotations

import calendar
import copy
import json
import os
import time
from typing import Any

from . import net

INSTALL_STAGES = ("DEPLOY", "DRYRUN")   # counted by the daily limit (0.3.2); RESTART/ROLLBACK are not
LOOKUP_STAGES = ("LOOKUP",)             # read-only lookups have their own, separate daily allowance
# D6(d): bound on installs.json (about 1 KB per record). Only Undo-resolved records are ever pruned to stay under it.
MAX_INSTALL_RECORDS = 500


class JournalError(RuntimeError):
    pass


def _atomic_write(path: str, obj: Any) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


class Journal:
    def __init__(self, data_dir: str, *, file_io: Any = None) -> None:
        self.dir = data_dir
        self._files = file_io
        self._file_pin = file_io
        if file_io is None:
            os.makedirs(data_dir, exist_ok=True)
        else:
            from .migration.deployer_bound_files import BoundStateFiles
            from .migration.deployer_bound_forward_io import BoundForwardState
            if type(file_io) not in (BoundStateFiles, BoundForwardState):
                raise JournalError('BOUND_JOURNAL_CUSTODY')
            file_io._check()
            fence = (file_io._lease if type(file_io) is BoundStateFiles
                     else getattr(file_io._runtime._local, 'fence', None))
            if data_dir != os.path.join(file_io._roots.path('working', fence=fence), *file_io._parts):
                raise JournalError('BOUND_JOURNAL_CUSTODY')
        self.txn_path = os.path.join(data_dir, "txn.json")
        self.audit_path = os.path.join(data_dir, "audit.jsonl")
        self.ledger_path = os.path.join(data_dir, "ledger.json")
        self.freeze_path = os.path.join(data_dir, "FROZEN.json")
        self.installs_path = os.path.join(data_dir, "installs.json")
        # Derived only: never persisted, never a substitute for the audit file.
        self._audit_cache: tuple[tuple[int, ...], list[dict]] | None = None

    def _check_files(self) -> None:
        if self._files is not self._file_pin:
            raise JournalError('BOUND_JOURNAL_CUSTODY')

    def _read_state(self, name: str, default: Any) -> Any:
        self._check_files()
        if self._files is None:
            return _read(os.path.join(self.dir, name), default)
        from .history import _json
        raw = self._files.read(name)
        return default if raw is None else _json(raw)

    def _write_state(self, name: str, obj: Any) -> None:
        self._check_files()
        if self._files is None:
            _atomic_write(os.path.join(self.dir, name), obj)
            return
        raw = json.dumps(obj, sort_keys=True, allow_nan=False).encode('utf-8')
        self._files.replace(name, raw, expected=self._files.read(name))

    def _clear_state(self, name: str) -> None:
        self._check_files()
        if self._files is not None:
            self._files.clear(name, expected=self._files.read(name))
            return
        try:
            os.unlink(os.path.join(self.dir, name))
        except FileNotFoundError:
            pass

    # -- audit ---------------------------------------------------------------
    def audit(self, request_id: str, event: str, **detail: Any) -> None:
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "request_id": request_id, "event": event}
        record.update(detail)
        line = net.redact(json.dumps(record, sort_keys=True, default=str))
        self._check_files()
        if self._files is not None:
            self._files.append_audit((line + '\n').encode('utf-8'), expected=self._files.read('audit.jsonl'))
            return
        with open(self.audit_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def audit_events(self, request_id: str | None = None) -> list[dict]:
        """Reuse an unchanged, bounded snapshot; rebuild after every file change.

        Do not assume a larger file is append-only: history import/recovery can
        replace or rewrite it. ctime also detects rewrites with restored mtime.
        Large histories retain full-scan behavior without deleting any history.
        """
        self._check_files()
        if self._files is not None:
            from .history import _json, MAX_EVENTS
            raw = self._files.read('audit.jsonl') or b''
            if raw and not raw.endswith(b'\n'):
                raise JournalError('BOUND_AUDIT_PARTIAL')
            lines = raw.splitlines()
            if len(lines) > MAX_EVENTS:
                raise JournalError('BOUND_AUDIT_CAP')
            records = [_json(line) for line in lines]
            if any(type(record) is not dict for record in records):
                raise JournalError('BOUND_AUDIT_RECORD')
            return copy.deepcopy([r for r in records if request_id is None or r.get('request_id') == request_id])

        def signature(st: os.stat_result) -> tuple[int, ...]:
            return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)

        try:
            current = signature(os.stat(self.audit_path))
        except FileNotFoundError:
            self._audit_cache = None
            return []
        if self._audit_cache is not None and self._audit_cache[0] == current:
            records = self._audit_cache[1]
            return copy.deepcopy([r for r in records if request_id is None or r.get("request_id") == request_id])
        self._audit_cache = None
        out = []
        try:
            with open(self.audit_path, encoding="utf-8") as fh:
                before = signature(os.fstat(fh.fileno()))
                for line in fh:
                    rec = json.loads(line)
                    if not isinstance(rec, dict):
                        raise ValueError("audit record must be an object")
                    out.append(rec)
                after = signature(os.fstat(fh.fileno()))
            if before != after or after != signature(os.stat(self.audit_path)):
                raise RuntimeError("audit changed during read; retry from source")
        except FileNotFoundError:
            # Initial absence was handled above; disappearance mid-read is drift.
            raise RuntimeError("audit disappeared during read") from None
        if after[2] <= 8 * 1024 * 1024 and len(out) <= 20_000:
            self._audit_cache = (after, out)
        return copy.deepcopy([r for r in out if request_id is None or r.get("request_id") == request_id])

    # -- transaction -----------------------------------------------------------
    def load_txn(self) -> dict | None:
        return self._read_state('txn.json', None)

    def save_txn(self, txn: dict) -> None:
        txn["saved_at"] = time.time()   # 0.3.5 (P5): how old the journal was when recovery found it
        self._write_state('txn.json', txn)

    def clear_txn(self) -> None:
        self._clear_state('txn.json')

    # -- ledger ----------------------------------------------------------------
    def ledger(self) -> dict:
        return self._read_state('ledger.json', {})

    def record(self, request_id: str, digest: str, outcome: str) -> None:
        ledger = self.ledger()
        ledger[request_id] = {"digest": digest, "outcome": outcome,
                              "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        self._write_state('ledger.json', ledger)

    def link_reask(self, original: str, reask_id: str) -> None:
        """0.3.8: remember on the ORIGINAL entry that ``reask_id`` asked it again (record() overwrites a whole
        entry, so the link is merged here). Written before the re-ask runs, so a crash still counts it."""
        ledger = self.ledger()
        entry = ledger.get(original)
        if not isinstance(entry, dict):
            return
        linked = [x for x in (entry.get("reasked_by") or []) if isinstance(x, str)]
        if reask_id not in linked:
            linked.append(reask_id)
        entry["reasked_by"] = linked
        self._write_state('ledger.json', ledger)

    def annotate(self, request_id: str, **fields: Any) -> None:
        """0.3.8: merge extra fields (e.g. ``reask_of``) into an existing ledger entry."""
        ledger = self.ledger()
        if isinstance(ledger.get(request_id), dict):
            ledger[request_id].update(fields)
            self._write_state('ledger.json', ledger)

    def mark_reuse(self, request_id: str, digest: str) -> None:
        ledger = self.ledger()
        if request_id in ledger:
            ledger[request_id]["reuse_digest"] = digest
            self._write_state('ledger.json', ledger)

    # -- freeze ------------------------------------------------------------------
    def freeze(self, request_id: str) -> None:
        self._write_state('FROZEN.json', {"request_id": request_id})

    def frozen_by(self) -> str | None:
        data = self._read_state('FROZEN.json', None)
        return data.get("request_id") if isinstance(data, dict) else None

    def clear_freeze(self) -> None:
        self._clear_state('FROZEN.json')

    def approval_times_since(self, seconds: float, stages: tuple[str, ...] | None = None) -> list[float]:
        """Epoch seconds of approval requests inside the window (rate limit and its reset time).

        ``stages`` limits the count, e.g. INSTALL_STAGES: since 0.3.2 the daily limit counts installs
        (the first approval of each request), not the restart approval that follows it."""
        cutoff = time.time() - seconds
        times: list[float] = []
        for rec in self.audit_events():
            if rec.get("event") == "APPROVAL_REQUESTED" and (stages is None or rec.get("stage") in stages):
                try:
                    ts = calendar.timegm(time.strptime(rec["ts"], "%Y-%m-%dT%H:%M:%SZ"))
                except (KeyError, ValueError):
                    continue
                if ts >= cutoff:
                    times.append(float(ts))
        return times

    def approvals_requested_since(self, seconds: float) -> int:
        return len(self.approval_times_since(seconds))

    # -- install records for owner Undo (0.3.3) --------------------------------------------------
    def installs(self) -> list[dict]:
        data = self._read_state('installs.json', [])
        return data if isinstance(data, list) else []

    def install(self, request_id: str) -> dict | None:
        return next((r for r in self.installs() if r.get("request_id") == request_id), None)

    def record_install(self, record: dict) -> None:
        records = [r for r in self.installs() if r.get("request_id") != record.get("request_id")]
        records.append(record)
        # 0.3.10 continuity: active offers are bounded by undo.MAX_OFFERS, not
        # by deleting historical provenance. Importing >20 records must not
        # lose the oldest metadata on the next successful installation.
        # D6(d): bounded at MAX_INSTALL_RECORDS. Only records already resolved by an Undo (undone_by) may be pruned,
        # oldest first; unresolved and Undo-eligible provenance is never dropped. If that is not enough, fail closed:
        # nothing is written (the caller audits INSTALL_RECORD_FAILED; that install is then not offered for Undo).
        if len(records) > MAX_INSTALL_RECORDS:
            excess = len(records) - MAX_INSTALL_RECORDS
            from collections import Counter
            from .undo import _claims
            # Undo uses newest claims as tombstones even after they are undone.
            # Removing such a claim can incorrectly offer an older install.
            counts = Counter(path for r in records for path in _claims(r))
            claimed, protected = set(), set()
            for r in sorted(records, key=lambda r: r.get("at", 0), reverse=True):
                mine = _claims(r)
                if r.get("undone_by") and any(path not in claimed and counts[path] > 1 for path in mine):
                    protected.add(id(r))
                claimed.update(mine)
            pending = self.load_txn() or {}
            references = {pending.get("request_id"), (pending.get("facts") or {}).get("undo_of"),
                          self.frozen_by()}
            resolved = sorted((r for r in records[:-1] if r.get("undone_by")
                               and id(r) not in protected and r.get("request_id") not in references),
                              key=lambda r: r.get("at", 0))
            if len(resolved) < excess:
                raise JournalError("INSTALL_HISTORY_FULL")
            pruned = {id(r) for r in resolved[:excess]}
            records = [r for r in records if id(r) not in pruned]
            self.audit(record.get("request_id", ""), "INSTALL_HISTORY_PRUNED", count=excess)
        self._write_state('installs.json', records)
        self.audit(record.get("request_id", ""), "INSTALL_RECORDED")

    def mark_undone(self, request_id: str, undo_id: str) -> None:
        records = self.installs()
        for r in records:
            if r.get("request_id") == request_id:
                r["undone_by"] = undo_id
                r["undone_at"] = time.time()
        self._write_state('installs.json', records)
        self.audit(request_id, "UNDONE", by=undo_id)

    # -- owner "Allow more today" (0.3.2) ---------------------------------------------------------
    def lift_limit(self, until: float, user_id: str) -> None:
        self.audit("", "LIMIT_LIFTED", until=until, by=user_id)

    def limit_lifted_until(self) -> float:
        until = 0.0
        for rec in self.audit_events():
            if rec.get("event") == "LIMIT_LIFTED":
                try:
                    until = max(until, float(rec.get("until") or 0))
                except (TypeError, ValueError):
                    continue
        return until

    # -- one-shot notices (0.3.1): a held request is announced once, ever ------------------------
    def noted(self, key: str) -> bool:
        return any(rec.get("event") == "HOLD_ANNOUNCED" and rec.get("key") == key for rec in self.audit_events())

    def note(self, request_id: str, key: str) -> None:
        self.audit(request_id, "HOLD_ANNOUNCED", key=key)
