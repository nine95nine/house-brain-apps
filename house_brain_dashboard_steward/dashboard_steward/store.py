"""Durable state: one SQLite file (stdlib), WAL, every write in a transaction.

* tasks: bounded, idempotent by fingerprint; a crash leaves ``running`` rows, which ``recover`` puts back
  in the queue (attempts counted, given up after MAX_ATTEMPTS);
* proposals and owner decisions, with the exact hashes each decision was bound to;
* memory: declined / deferred opportunity fingerprints, so the same idea is not proposed again unless the
  evidence changes (the fingerprint includes the evidence) or the deferral expires;
* budget: per-day counters (model calls, research reads), enforced fail-closed;
* kv: snapshots, dashboard hashes, health timestamps.
"""
from __future__ import annotations

import json
import sqlite3
import time

MAX_QUEUED = 500
MAX_ATTEMPTS = 3
SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, fp TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
  status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, not_before REAL NOT NULL DEFAULT 0,
  created REAL NOT NULL, updated REAL NOT NULL, last_error TEXT);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, not_before);
CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY, fp TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
  payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, proposal_id TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT,
  decided_by TEXT NOT NULL, at REAL NOT NULL, binding TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory (fp TEXT PRIMARY KEY, decision TEXT NOT NULL, until REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS budget (day TEXT NOT NULL, kind TEXT NOT NULL, n INTEGER NOT NULL, PRIMARY KEY(day, kind));
"""


class Store:
    def __init__(self, path: str, clock=time.time):
        self.clock = clock
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(_DDL)
        if self.get("schema_version") is None:
            self.put("schema_version", SCHEMA_VERSION)
        elif self.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeError("STORE_SCHEMA_MISMATCH")
        self.dropped = 0

    def close(self):
        self.db.close()

    # ---- kv
    def put(self, k: str, v):
        self.db.execute("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                        (k, json.dumps(v, sort_keys=True)))

    def get(self, k: str, default=None):
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(row[0]) if row else default

    # ---- tasks
    def enqueue(self, kind: str, fp: str, payload: dict, not_before: float = 0.0) -> bool:
        """Idempotent: a task with the same fingerprint is never queued twice. Bounded: when the queue is
        full the new task is refused and counted (the periodic reconcile re-derives it later)."""
        now = self.clock()
        cur = self.db.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('queued','waiting_model')").fetchone()[0]
        if cur >= MAX_QUEUED:
            self.dropped += 1
            self.put("queue_dropped", (self.get("queue_dropped", 0) or 0) + 1)
            return False
        try:
            self.db.execute("INSERT INTO tasks(kind, fp, payload, status, not_before, created, updated) "
                            "VALUES(?, ?, ?, 'queued', ?, ?, ?)", (kind, fp, json.dumps(payload), not_before, now, now))
            return True
        except sqlite3.IntegrityError:
            return False

    def has_ready_task(self) -> bool:
        return self.db.execute("SELECT 1 FROM tasks WHERE status='queued' AND not_before<=? LIMIT 1",
                               (self.clock(),)).fetchone() is not None

    def next_task(self, kinds=None):
        now = self.clock()
        q = "SELECT id, kind, fp, payload, attempts FROM tasks WHERE status='queued' AND not_before<=?"
        args: list = [now]
        if kinds:
            q += " AND kind IN (%s)" % ",".join("?" * len(kinds))
            args += list(kinds)
        q += " ORDER BY id LIMIT 1"
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(q, args).fetchone()
            if row is None:
                self.db.execute("COMMIT")
                return None
            self.db.execute("UPDATE tasks SET status='running', attempts=attempts+1, updated=? WHERE id=?",
                            (now, row[0]))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return {"id": row[0], "kind": row[1], "fp": row[2], "payload": json.loads(row[3]), "attempts": row[4] + 1}

    def finish(self, task_id: int, status: str = "done", error: str = "", retry_in: float = 0.0):
        if status not in ("done", "failed", "queued", "waiting_model"):
            raise ValueError(status)
        self.db.execute("UPDATE tasks SET status=?, last_error=?, updated=?, not_before=? WHERE id=?",
                        (status, error[:200], self.clock(), self.clock() + retry_in, task_id))

    def release_waiting(self) -> int:
        """Model is back: put tasks that waited for it back in the queue."""
        return self.db.execute("UPDATE tasks SET status='queued', attempts=MAX(attempts-1, 0) "
                               "WHERE status='waiting_model'").rowcount

    def recover(self) -> dict:
        """After a crash/restart: running tasks go back to the queue; those that kept crashing are failed."""
        failed = self.db.execute("UPDATE tasks SET status='failed', last_error='CRASH_LOOP' "
                                 "WHERE status='running' AND attempts>=?", (MAX_ATTEMPTS,)).rowcount
        requeued = self.db.execute("UPDATE tasks SET status='queued' WHERE status='running'").rowcount
        return {"requeued": requeued, "failed": failed}

    def task_counts(self) -> dict:
        return dict(self.db.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status").fetchall())

    # ---- proposals / decisions / memory
    def save_proposal(self, pid: str, fp: str, status: str, payload: dict):
        now = self.clock()
        self.db.execute("INSERT INTO proposals(id, fp, status, created, updated, payload) VALUES(?,?,?,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET status=excluded.status, updated=excluded.updated, "
                        "payload=excluded.payload", (pid, fp, status, now, now, json.dumps(payload, sort_keys=True)))

    def proposal(self, pid: str):
        row = self.db.execute("SELECT id, fp, status, created, updated, payload FROM proposals WHERE id=?",
                              (pid,)).fetchone()
        return None if row is None else {"id": row[0], "fp": row[1], "status": row[2], "created": row[3],
                                         "updated": row[4], **json.loads(row[5])}

    def proposals(self, status=None) -> list:
        q, args = "SELECT id FROM proposals", ()
        if status:
            q, args = q + " WHERE status=?", (status,)
        return [self.proposal(r[0]) for r in self.db.execute(q + " ORDER BY created", args).fetchall()]

    def record_decision(self, pid: str, decision: str, reason: str, by: str, binding: dict):
        self.db.execute("INSERT INTO decisions(proposal_id, decision, reason, decided_by, at, binding) "
                        "VALUES(?,?,?,?,?,?)", (pid, decision, reason[:500], by[:80], self.clock(),
                                                json.dumps(binding, sort_keys=True)))

    def decisions(self, pid: str) -> list:
        return [{"decision": r[0], "reason": r[1], "by": r[2], "at": r[3], "binding": json.loads(r[4])}
                for r in self.db.execute("SELECT decision, reason, decided_by, at, binding FROM decisions "
                                         "WHERE proposal_id=? ORDER BY id", (pid,))]

    def remember(self, fp: str, decision: str, until: float | None, reason: str = ""):
        self.db.execute("INSERT INTO memory(fp, decision, until, reason) VALUES(?,?,?,?) ON CONFLICT(fp) DO "
                        "UPDATE SET decision=excluded.decision, until=excluded.until, reason=excluded.reason",
                        (fp, decision, until, reason[:500]))

    def suppressed(self, fp: str) -> str | None:
        row = self.db.execute("SELECT decision, until FROM memory WHERE fp=?", (fp,)).fetchone()
        if row is None:
            return None
        if row[1] is not None and row[1] <= self.clock():
            return None
        return row[0]

    # ---- budget
    def spend(self, kind: str, limit: int, day: str) -> bool:
        """Count one unit of ``kind`` for ``day``; refuse (fail closed) when ``limit`` is reached."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT n FROM budget WHERE day=? AND kind=?", (day, kind)).fetchone()
            n = row[0] if row else 0
            if n >= limit:
                self.db.execute("COMMIT")
                return False
            self.db.execute("INSERT INTO budget(day, kind, n) VALUES(?,?,1) ON CONFLICT(day, kind) DO UPDATE "
                            "SET n=n+1", (day, kind))
            self.db.execute("COMMIT")
            return True
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def spent(self, kind: str, day: str) -> int:
        row = self.db.execute("SELECT n FROM budget WHERE day=? AND kind=?", (day, kind)).fetchone()
        return row[0] if row else 0
