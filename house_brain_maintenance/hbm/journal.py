"""Durable state under /data: transaction journal, append-only audit, ledger."""
from __future__ import annotations

import calendar
import json
import os
import time
from typing import Any

from . import net


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

    def mark_reuse(self, request_id: str, digest: str) -> None:
        ledger = self.ledger()
        if request_id in ledger:
            ledger[request_id]["reuse_digest"] = digest
            _atomic_write(self.ledger_path, ledger)

    # -- small named documents (update memory: quarantine, declined, history) -----
    def load_doc(self, name: str, default: Any) -> Any:
        return _read(os.path.join(self.dir, f"{name}.json"), default)

    def save_doc(self, name: str, obj: Any) -> None:
        _atomic_write(os.path.join(self.dir, f"{name}.json"), obj)

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

    def approvals_requested_since(self, seconds: float) -> int:
        cutoff = time.time() - seconds
        count = 0
        for rec in self.audit_events():
            if rec.get("event") == "APPROVAL_REQUESTED":
                try:
                    ts = calendar.timegm(time.strptime(rec["ts"], "%Y-%m-%dT%H:%M:%SZ"))
                except (KeyError, ValueError):
                    continue
                if ts >= cutoff:
                    count += 1
        return count
