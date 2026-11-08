"""App-owned bounded SQLite state. Never opens the HA recorder database."""
import json
import os
from pathlib import Path
import sqlite3
import time


def encode(value):
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))


class Store:
    def __init__(self, path, max_mb=128):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=3)
        os.chmod(self.path, 0o600)
        if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            self.db.close()
            raise ValueError("STORE_CORRUPT_RESTORE_REQUIRED")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise ValueError("STORE_NEWER_THAN_RUNTIME")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(f"PRAGMA max_page_count={int(max_mb * 1024 * 1024 / 4096)}")
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS samples (
            entity TEXT, source_at REAL, observed_at REAL, value REAL,
            quality TEXT, family TEXT, classification TEXT,
            PRIMARY KEY(entity, source_at));
          CREATE INDEX IF NOT EXISTS samples_time ON samples(observed_at);
          CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY, at REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS hourly (
            entity TEXT, at REAL, watts REAL, PRIMARY KEY(entity,at));
          PRAGMA user_version=1;
        ''')
        self.db.commit()

    def put(self, key, value):
        payload = encode(value)
        if len(payload) > 2_000_000:
            raise ValueError("STATE_TOO_LARGE")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, payload))

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def ingest(self, samples):
        with self.db:
            for s in samples:
                # A missing sample must not replace a previous actual measurement.
                if s["source_at"] is not None and s["quality"] == "GOOD":
                    self.db.execute("INSERT OR IGNORE INTO samples VALUES (?,?,?,?,?,?,?)",
                                    (s["entity"], s["source_at"], s["observed_at"], s["value"],
                                     s["quality"], s["family"], s["classification"]))

    def series(self, entity, since, limit=130000):
        rows = self.db.execute("SELECT source_at,value FROM samples WHERE entity=? AND source_at>=? "
                               "AND quality='GOOD' ORDER BY source_at LIMIT ?", (entity, since, limit))
        return [tuple(r) for r in rows]

    def event(self, identity, at, kind, payload):
        data = encode(payload)
        if len(data) > 65536:
            raise ValueError("EVENT_TOO_LARGE")
        with self.db:
            return self.db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?)",
                                   (identity, at, kind, data)).rowcount == 1

    def rollup(self, entity, rows, now, allow_older=False):
        latest=self.db.execute("SELECT MAX(at) FROM hourly WHERE entity=?",(entity,)).fetchone()[0]
        fresh=[(entity,t,v) for t,v in rows if t+3600<=now and (allow_older or latest is None or t>latest)]
        if fresh:
            with self.db:
                self.db.executemany("INSERT OR IGNORE INTO hourly VALUES (?,?,?)",fresh)
        return [tuple(r) for r in self.db.execute(
            "SELECT at,watts FROM hourly WHERE entity=? AND at>=? ORDER BY at LIMIT 9000",
            (entity,now-365*86400))]

    def prune(self, now, retention_days=7, max_events=10000):
        with self.db:
            self.db.execute("DELETE FROM samples WHERE observed_at<?", (now-retention_days*86400,))
            self.db.execute("DELETE FROM hourly WHERE at<?",(now-365*86400,))
            # Confirmations/models are in kv and are never pruned as transient history.
            self.db.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY at DESC LIMIT ?)",
                            (max_events,))

    def backup(self, destination):
        target = Path(destination)
        if target.exists():
            raise ValueError("BACKUP_EXISTS")
        out = sqlite3.connect(target)
        try:
            self.db.backup(out)
            if out.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("BACKUP_INVALID")
        finally:
            out.close()
        os.chmod(target, 0o600)

    def close(self):
        self.db.close()
