"""Durable state under /data: transaction journal, append-only audit, ledger."""
from __future__ import annotations

import calendar
import json
import os
import time
from typing import Any

from . import net

INSTALL_STAGES = ("DEPLOY", "DRYRUN")   # counted by the daily limit (0.3.2); RESTART/ROLLBACK are not
LOOKUP_STAGES = ("LOOKUP",)             # read-only lookups have their own, separate daily allowance


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
    def __init__(self, data_dir: str) -> None:
        self.dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.txn_path = os.path.join(data_dir, "txn.json")
        self.audit_path = os.path.join(data_dir, "audit.jsonl")
        self.ledger_path = os.path.join(data_dir, "ledger.json")
        self.freeze_path = os.path.join(data_dir, "FROZEN.json")
        self.installs_path = os.path.join(data_dir, "installs.json")

    # -- audit ---------------------------------------------------------------
    def audit(self, request_id: str, event: str, **detail: Any) -> None:
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "request_id": request_id, "event": event}
        record.update(detail)
        line = net.redact(json.dumps(record, sort_keys=True, default=str))
        with open(self.audit_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def audit_events(self, request_id: str | None = None) -> list[dict]:
        out = []
        try:
            with open(self.audit_path, encoding="utf-8") as fh:
                for line in fh:
                    rec = json.loads(line)
                    if request_id is None or rec.get("request_id") == request_id:
                        out.append(rec)
        except FileNotFoundError:
            pass
        return out

    # -- transaction -----------------------------------------------------------
    def load_txn(self) -> dict | None:
        return _read(self.txn_path, None)

    def save_txn(self, txn: dict) -> None:
        txn["saved_at"] = time.time()   # 0.3.5 (P5): how old the journal was when recovery found it
        _atomic_write(self.txn_path, txn)

    def clear_txn(self) -> None:
        try:
            os.unlink(self.txn_path)
        except FileNotFoundError:
            pass

    # -- ledger ----------------------------------------------------------------
    def ledger(self) -> dict:
        return _read(self.ledger_path, {})

    def record(self, request_id: str, digest: str, outcome: str) -> None:
        ledger = self.ledger()
        ledger[request_id] = {"digest": digest, "outcome": outcome,
                              "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        _atomic_write(self.ledger_path, ledger)

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
        _atomic_write(self.ledger_path, ledger)

    def annotate(self, request_id: str, **fields: Any) -> None:
        """0.3.8: merge extra fields (e.g. ``reask_of``) into an existing ledger entry."""
        ledger = self.ledger()
        if isinstance(ledger.get(request_id), dict):
            ledger[request_id].update(fields)
            _atomic_write(self.ledger_path, ledger)

    def mark_reuse(self, request_id: str, digest: str) -> None:
        ledger = self.ledger()
        if request_id in ledger:
            ledger[request_id]["reuse_digest"] = digest
            _atomic_write(self.ledger_path, ledger)

    # -- freeze ------------------------------------------------------------------
    def freeze(self, request_id: str) -> None:
        _atomic_write(self.freeze_path, {"request_id": request_id})

    def frozen_by(self) -> str | None:
        data = _read(self.freeze_path, None)
        return data.get("request_id") if isinstance(data, dict) else None

    def clear_freeze(self) -> None:
        try:
            os.unlink(self.freeze_path)
        except FileNotFoundError:
            pass

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
        data = _read(self.installs_path, [])
        return data if isinstance(data, list) else []

    def install(self, request_id: str) -> dict | None:
        return next((r for r in self.installs() if r.get("request_id") == request_id), None)

    def record_install(self, record: dict) -> None:
        records = [r for r in self.installs() if r.get("request_id") != record.get("request_id")]
        records.append(record)
        _atomic_write(self.installs_path, records[-20:])
        self.audit(record.get("request_id", ""), "INSTALL_RECORDED")

    def mark_undone(self, request_id: str, undo_id: str) -> None:
        records = self.installs()
        for r in records:
            if r.get("request_id") == request_id:
                r["undone_by"] = undo_id
                r["undone_at"] = time.time()
        _atomic_write(self.installs_path, records)
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
