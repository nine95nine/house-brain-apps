#!/usr/bin/env python3
"""Crash-safe local store for the House Brain AI kill switch R1 (Initiative #56).

Layout under one private directory (0700; files 0600)::

    audit.jsonl   append-only, hash-chained record log (AUTHORITY)
    state.json    materialized head record (fast read path; must equal the log head)
    .lock         fcntl lock serializing writers

Write order for every transition (under an exclusive lock): verify the whole chain -> check authority
against the effective head -> append + fsync the log line -> write temp state + fsync -> atomic
``os.replace`` -> fsync the directory. A crash between the log append and the replace leaves the
state file one generation behind the log; readers treat that divergence as FULL_STOP until
``recover`` rolls the state file forward from the verified log. A torn (newline-less) final log line
is likewise FULL_STOP until ``recover`` drops it.

Readers (``read_view``) never raise and fail closed. AI surfaces may use only ``read_view`` and
``request_tighten`` (actor AI_SURFACE, tighten-only); the conformance checker enforces that statically.
Library functions take ``now`` explicitly; only the CLI adapter reads the wall clock.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterator

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import ai_kill_switch_state as ks  # noqa: E402

STATE_NAME = "state.json"
LOG_NAME = "audit.jsonl"
LOCK_NAME = ".lock"
MAX_LOG_BYTES = 16 * 1024 * 1024
AI_TIGHTEN_API = ("read_view", "request_tighten")  # the only store API an AI surface may use


class StoreError(ks.KillSwitchError):
    pass


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class KillSwitchStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.state_path = self.root / STATE_NAME
        self.log_path = self.root / LOG_NAME
        self.lock_path = self.root / LOCK_NAME

    # ------------------------------------------------------------------ read path (never raises)
    def _read_bounded(self, path: Path, limit: int) -> bytes | None:
        try:
            with open(path, "rb") as fh:
                data = fh.read(limit + 1)
        except FileNotFoundError:
            return None
        return data

    def _log_head_sha256(self) -> str | None:
        try:
            size = self.log_path.stat().st_size
            with open(self.log_path, "rb") as fh:
                window = 2 * ks.MAX_RECORD_BYTES + 2
                fh.seek(max(0, size - window))
                tail = fh.read()
        except OSError:
            return None
        if not tail.endswith(b"\n"):
            return None  # empty or torn final line
        lines = tail[:-1].split(b"\n")
        try:
            record = ks.validate_record(ks.decode_record_bytes(lines[-1]))
        except ks.KillSwitchError:
            return None
        return record["record_sha256"]

    def read_view(self, now: str) -> dict[str, Any]:
        try:
            raw = self._read_bounded(self.state_path, ks.MAX_RECORD_BYTES)
            head = self._log_head_sha256()
        except OSError:
            return ks._fail_view("STORE_UNREADABLE")
        if raw is not None and len(raw) > ks.MAX_RECORD_BYTES:
            return ks._fail_view("STATE_RECORD_SIZE_INVALID")
        return ks.resolve(raw, now, log_head_sha256=head, require_log_head=True)

    # ------------------------------------------------------------------ chain
    def _load_log(self) -> list[dict[str, Any]]:
        try:
            size = self.log_path.stat().st_size
        except FileNotFoundError:
            return []
        if size > MAX_LOG_BYTES:
            raise StoreError("STORE_LOG_TOO_LARGE")
        data = self.log_path.read_bytes()
        if not data:
            return []
        if not data.endswith(b"\n"):
            raise StoreError("STORE_LOG_TORN_TAIL")
        records = []
        for line in data[:-1].split(b"\n"):
            records.append(ks.validate_record(ks.decode_record_bytes(line)))
        return records

    def verify(self, expect_head: str | None = None) -> dict[str, Any]:
        records = self._load_log()
        if not records:
            raise StoreError("STORE_NOT_INITIALIZED")
        summary = ks.verify_chain(records)
        if expect_head is not None and expect_head != summary["head_record_sha256"]:
            # An externally anchored head that is no longer in the chain = rewritten history.
            if expect_head not in {r["record_sha256"] for r in records}:
                raise StoreError("STORE_ANCHOR_NOT_IN_CHAIN")
        raw = self._read_bounded(self.state_path, ks.MAX_RECORD_BYTES)
        summary["state_file_matches_head"] = raw is not None and raw.rstrip(b"\n") == ks.canonical_bytes(records[-1])
        return summary

    # ------------------------------------------------------------------ write path
    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _append_log(self, record: dict[str, Any]) -> None:
        created = not self.log_path.exists()
        fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, ks.canonical_bytes(record) + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        if created:
            _fsync_dir(self.root)

    def _write_state(self, record: dict[str, Any]) -> None:
        tmp = self.root / f".{STATE_NAME}.tmp-{record['generation']}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, ks.canonical_bytes(record) + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.state_path)
        _fsync_dir(self.root)

    def _commit(self, prev: dict[str, Any] | None, record: dict[str, Any], now: str) -> dict[str, Any]:
        ks.validate_transition(prev, record, now)
        self._append_log(record)
        self._write_state(record)
        return record

    def initialize(self, *, now: str, actor_ref: str) -> dict[str, Any]:
        with self._locked():
            if self.log_path.exists() or self.state_path.exists():
                raise StoreError("STORE_ALREADY_INITIALIZED")
            record = ks.build_record(prev=None, state="FULL_STOP", actor_class="BOOTSTRAP",
                                     actor_ref=actor_ref, reason_code="GENESIS_FAIL_CLOSED", now=now)
            return self._commit(None, record, now)

    def transition(self, *, state: str, actor_class: str, actor_ref: str, reason_code: str, now: str,
                   expires_at: str | None = None, expected_generation: int | None = None) -> dict[str, Any]:
        with self._locked():
            records = self._load_log()
            if not records:
                raise StoreError("STORE_NOT_INITIALIZED")
            ks.verify_chain(records)
            head = records[-1]
            if expected_generation is not None and expected_generation != head["generation"]:
                raise StoreError("STORE_GENERATION_CONFLICT")
            record = ks.build_record(prev=head, state=state, actor_class=actor_class, actor_ref=actor_ref,
                                     reason_code=reason_code, now=now, expires_at=expires_at)
            return self._commit(head, record, now)

    def request_tighten(self, *, state: str, actor_ref: str, now: str,
                        reason_code: str = "AI_SELF_STOP") -> dict[str, Any]:
        """The only write an AI surface may make: move to an equal-or-stricter state as AI_SURFACE."""
        if state not in ("FULL_STOP", "READ_ONLY"):
            raise StoreError("AI_TIGHTEN_TARGET_INVALID")
        return self.transition(state=state, actor_class="AI_SURFACE", actor_ref=actor_ref,
                               reason_code=reason_code, now=now,
                               expires_at=None)

    def materialize_expiry(self, *, now: str, actor_ref: str = "dead-man-timer") -> dict[str, Any] | None:
        """Record a dead-man step-down in the audit log (optional; readers already apply it)."""
        with self._locked():
            records = self._load_log()
            if not records:
                raise StoreError("STORE_NOT_INITIALIZED")
            ks.verify_chain(records)
            head = records[-1]
            state, reasons = ks.effective_state(head, now)
            if not reasons:
                return None
            record = ks.build_record(prev=head, state=state, actor_class="DEAD_MAN_TIMER", actor_ref=actor_ref,
                                     reason_code="DEAD_MAN_EXPIRY", now=now, expires_at=None)
            return self._commit(head, record, now)

    def recover(self) -> dict[str, Any]:
        """Custody repair only: drop a torn final log line, then re-materialize state from the log head.

        Never creates a record and never changes the audited state.
        """
        with self._locked():
            actions: list[str] = []
            try:
                data = self.log_path.read_bytes()
            except FileNotFoundError:
                raise StoreError("STORE_NOT_INITIALIZED") from None
            if data and not data.endswith(b"\n"):
                keep = data[: data.rfind(b"\n") + 1]
                tmp = self.root / f".{LOG_NAME}.recover"
                tmp.write_bytes(keep)
                os.chmod(tmp, 0o600)
                with open(tmp, "rb") as fh:
                    os.fsync(fh.fileno())
                os.replace(tmp, self.log_path)
                _fsync_dir(self.root)
                actions.append("DROPPED_TORN_LOG_TAIL")
            records = self._load_log()
            if not records:
                raise StoreError("STORE_NOT_INITIALIZED")
            summary = ks.verify_chain(records)
            raw = self._read_bounded(self.state_path, ks.MAX_RECORD_BYTES)
            if raw is None or raw.rstrip(b"\n") != ks.canonical_bytes(records[-1]):
                self._write_state(records[-1])
                actions.append("STATE_ROLLED_FORWARD_FROM_LOG")
            return {"actions": actions or ["NONE"], **summary}

    def reinitialize_after_tamper(self, *, now: str, actor_ref: str) -> dict[str, Any]:
        """Quarantine an unverifiable store and start a new chain at FULL_STOP.

        The genesis ``prev_record_sha256`` binds the SHA-256 of the quarantined log bytes, so the
        break is itself evidenced. Refused while the existing chain still verifies.
        """
        with self._locked():
            chain_ok = False
            try:
                records = self._load_log()
                if records:
                    ks.verify_chain(records)
                    chain_ok = True
            except ks.KillSwitchError:
                pass
            if chain_ok:
                raise StoreError("STORE_CHAIN_VALID_REFUSING_REINITIALIZE")
            log_bytes = self.log_path.read_bytes() if self.log_path.exists() else b""
            digest = hashlib.sha256(log_bytes).hexdigest()
            tag = f"{now.replace(':', '').replace('-', '')}-{digest[:12]}"
            if self.log_path.exists():
                os.replace(self.log_path, self.root / f"audit.quarantine-{tag}.jsonl")
            if self.state_path.exists():
                os.replace(self.state_path, self.root / f"state.quarantine-{tag}.json")
            _fsync_dir(self.root)
            record = ks.build_record(prev=None, state="FULL_STOP", actor_class="BOOTSTRAP", actor_ref=actor_ref,
                                     reason_code="TAMPER_REINITIALIZE", now=now, genesis_prev=digest)
            return self._commit(None, record, now)


# ---------------------------------------------------------------------- CLI adapter (reads the clock)
def _now(arg: str | None) -> str:
    return arg if arg else ks.format_ts(datetime.now(timezone.utc))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="House Brain AI kill-switch local store (owner CLI)")
    p.add_argument("--root", required=True, type=Path)
    p.add_argument("--now", help="explicit YYYY-MM-DDTHH:MM:SSZ (default: wall clock)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    i = sub.add_parser("init")
    i.add_argument("--actor-ref", default="owner-cli")
    s = sub.add_parser("set")
    s.add_argument("--state", required=True, choices=ks.STATES)
    s.add_argument("--reason", required=True, choices=sorted(ks.REASON_CODES))
    s.add_argument("--actor-class", default="OWNER_CLI", choices=sorted(ks.ACTOR_AUTHORITY))
    s.add_argument("--actor-ref", default="owner-cli")
    exp = s.add_mutually_exclusive_group()
    exp.add_argument("--expires-in-s", type=int)
    exp.add_argument("--owner-default-expiry", action="store_true",
                     help=f"expire after the owner-decided arm default ({ks.OWNER_DEFAULT_ARM_TTL_S} s, 12 h)")
    s.add_argument("--expected-generation", type=int)
    v = sub.add_parser("verify")
    v.add_argument("--expect-head")
    sub.add_parser("recover")
    sub.add_parser("expire")
    r = sub.add_parser("reinitialize-after-tamper")
    r.add_argument("--actor-ref", default="owner-cli")
    a = p.parse_args(argv)
    store = KillSwitchStore(a.root)
    try:
        now = _now(a.now)
        ks.parse_ts(now, "NOW_INVALID")
        if a.cmd == "status":
            out: Any = store.read_view(now)
        elif a.cmd == "init":
            out = store.initialize(now=now, actor_ref=a.actor_ref)
        elif a.cmd == "set":
            expires = None
            expires_in = ks.OWNER_DEFAULT_ARM_TTL_S if a.owner_default_expiry else a.expires_in_s
            if expires_in is not None:
                expires = ks.format_ts(ks.parse_ts(now) + timedelta(seconds=expires_in))
            out = store.transition(state=a.state, actor_class=a.actor_class, actor_ref=a.actor_ref,
                                   reason_code=a.reason, now=now, expires_at=expires,
                                   expected_generation=a.expected_generation)
        elif a.cmd == "verify":
            out = store.verify(a.expect_head)
        elif a.cmd == "recover":
            out = store.recover()
        elif a.cmd == "expire":
            out = store.materialize_expiry(now=now) or {"materialized": False}
        else:
            out = store.reinitialize_after_tamper(now=now, actor_ref=a.actor_ref)
        print(json.dumps(out, sort_keys=True))
        return 0
    except (ks.KillSwitchError, OSError) as exc:
        code = str(exc) if isinstance(exc, ks.KillSwitchError) else "STORE_IO_ERROR"
        print(json.dumps({"ok": False, "error": code}, sort_keys=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
