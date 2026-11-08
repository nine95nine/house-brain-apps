"""The App's own bounded history, and the recorder backfill that fills it once.

One table in the App's own SQLite (never the recorder): `pts`, the change points of the listed
series (room temperatures, house humidity and dew point, the Arbiter phase, the UPS status, runtime,
charge and load). Constant readings are not repeated: a reading holds until the next change, so a
constant is idle, not missing. A gap (`unknown`/`unavailable`) is stored as a gap and is never filled.

The backfill reads one series-day a minute, oldest day first, never today, for the listed series only;
a failed read is retried at most 3 times (15, then 30 minutes apart) and never filled in.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .entities import GAP, HISTORY, number, token

ZONE = ZoneInfo('America/Phoenix')
KEEP_DAYS = 45
PURGE_DAYS = 40
LOOKBACK_S = 3600
MAX_TRIES = 3
RETRY_S = 15 * 60
MAX_POINTS_PER_DAY = 5000
MAX_HISTORY_ROWS = 50000


def local(at):
    return datetime.fromtimestamp(at, ZONE)


def local_midnight(day):
    return datetime.fromisoformat(day).replace(tzinfo=ZONE).timestamp()


def local_day(at):
    return local(at).strftime('%Y-%m-%d')


def days_to_fetch(now):
    """Complete local days still in the recorder's window, oldest first (the next purge takes the oldest)."""
    today = local(now).date()
    return [(today - timedelta(days=n)).isoformat() for n in range(PURGE_DAYS - 1, 0, -1)]


def _stamp(value):
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('TIMEZONE_REQUIRED')
    return dt.timestamp()


def changes(rows, entity):
    """(time, state) pairs from a history reply for one entity; strict, sorted by time (stable). Anything
    malformed is ValueError, never a crash; a state that is not text is a gap."""
    if not isinstance(rows, list) or len(rows) > 1:
        raise ValueError('HISTORY_SHAPE')
    first = rows[0] if rows else []
    if not isinstance(first, list):
        raise ValueError('HISTORY_SHAPE')
    if len(first) > MAX_HISTORY_ROWS:
        raise ValueError('HISTORY_TOO_MANY_ROWS')   # a failed read, retried, never a silently cut day
    out = []
    for row in first:
        if not isinstance(row, dict):
            raise ValueError('HISTORY_ROW_INVALID')
        if 'entity_id' in row and row['entity_id'] != entity:
            raise ValueError('HISTORY_ENTITY_MISMATCH')
        state = row.get('state')
        out.append((_stamp(row.get('last_changed') or row.get('last_updated')), state if isinstance(state, str) else None))
    return sorted(out, key=lambda p: p[0])


def as_point(state):
    """(value, word) for a stored change point: numbers as value, words as a safe token, a gap as 'na'."""
    if state in GAP:
        return None, 'na'
    n = number(state)
    return (n, None) if n is not None else (None, token(state))


def thin(rows):
    """At most one numeric reading a minute (the last in it), as live collection keeps; words and gaps are
    never thinned."""
    out = []
    for row in rows:
        if out and row[2] is not None and out[-1][2] is not None and int(row[1] // 60) == int(out[-1][1] // 60):
            out[-1] = row
        else:
            out.append(row)
    return out


class Series:
    def __init__(self, store):
        self.store = store
        self.db = store.db
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS pts (entity TEXT NOT NULL, t REAL NOT NULL, v REAL, s TEXT,
                                          PRIMARY KEY (entity, t));
        ''')
        self.db.commit()
        self.days = store.get('history:days', {})
        self.last = store.get('series:last', {})

    # -- live -------------------------------------------------------------------------------
    def add_live(self, states, now):
        rows = []
        for entity in sorted(HISTORY):
            if entity not in states:
                continue
            value, word = as_point(states[entity].get('state'))
            mark = f'{value}|{word}'
            if self.last.get(entity) != mark:
                self.last[entity] = mark
                rows.append((entity, float(now), value, word))
        with self.db:
            self.db.executemany('INSERT OR REPLACE INTO pts VALUES (?,?,?,?)', rows)
        if rows:
            self.store.put('series:last', self.last)

    # -- backfill -----------------------------------------------------------------------------
    def _needs(self, entity, day, now):
        rec = self.days.get(entity, {}).get(day)
        return rec is None or (rec.get('status') == 'FAILED' and rec.get('tries', 0) < MAX_TRIES
                               and now - rec.get('at', 0) >= RETRY_S * rec.get('tries', 0))

    def pending(self, now):
        """The next (series, day) to read: oldest day first. A failed one waits before its retry."""
        for day in days_to_fetch(now):
            for entity in sorted(HISTORY):
                if self._needs(entity, day, now):
                    return entity, day
        return None

    def waiting(self, now):
        return sum(1 for d in days_to_fetch(now) for e in HISTORY if self.days.get(e, {}).get(d) is None)

    def backfill_step(self, read, now):
        """Read one series-day through `read(entity, start, end)`, which the runtime supplies and which returns
        None when the read failed (this module itself never touches the network). Returns 'IDLE' when nothing
        is pending, else the stored status. A failed read is retried at most MAX_TRIES times, never filled in."""
        nxt = self.pending(now)
        if nxt is None:
            return 'IDLE'
        entity, day = nxt
        start = local_midnight(day)
        end = start + 86400
        rec = self.days.setdefault(entity, {}).setdefault(day, {'tries': 0})
        rec['tries'] = rec.get('tries', 0) + 1
        try:
            raw = read(entity, start - LOOKBACK_S, end)
            if raw is None:
                raise ValueError('READ_FAILED')
            got = changes(raw, entity)
        except ValueError:
            rec.update(status='FAILED', at=int(now))
            self.store.put('history:days', self.days)
            return 'FAILED'
        held = [p for p in got if p[0] <= start][-1:]
        inside = [(max(t, start), s) for t, s in held] + [p for p in got if start < p[0] < end]
        rows = thin([(entity, float(t)) + as_point(s) for t, s in inside])
        truncated = len(rows) > MAX_POINTS_PER_DAY
        if truncated:
            # The kept points end with a gap, so the last kept reading is never held past what was kept.
            rows = rows[:MAX_POINTS_PER_DAY - 1] + [(entity, rows[MAX_POINTS_PER_DAY - 1][1], None, 'na')]
        with self.db:
            # Live points win: a backfilled point never replaces one seen live.
            self.db.executemany('INSERT OR IGNORE INTO pts VALUES (?,?,?,?)', rows)
        status = 'DONE' if rows else 'EMPTY'
        rec.update(status=status, rows=len(rows), at=int(now), truncated=truncated)
        self.store.put('history:days', self.days)
        return status

    # -- reading back ---------------------------------------------------------------------------
    def points(self, entity, since):
        """Change points (t, value, word) of one series, with the point held at `since` first."""
        held = self.db.execute('SELECT t, v, s FROM pts WHERE entity=? AND t < ? ORDER BY t DESC LIMIT 1',
                               (entity, since)).fetchall()
        rows = self.db.execute('SELECT t, v, s FROM pts WHERE entity=? AND t >= ? ORDER BY t', (entity, since)).fetchall()
        return [tuple(r) for r in held + rows]

    def report(self, now):
        waiting = self.waiting(now)
        counts = {s: sum(r.get('status') == s for recs in self.days.values() for r in recs.values())
                  for s in ('DONE', 'EMPTY', 'FAILED')}
        return {'status': 'BACKFILLING' if waiting else 'KEEPING', 'entity_days_waiting': waiting, **counts,
                'points': self.db.execute('SELECT COUNT(*) FROM pts').fetchone()[0]}

    def prune(self, now):
        cut = now - KEEP_DAYS * 86400
        with self.db:
            self.db.execute('DELETE FROM pts WHERE t < ?', (cut,))
        oldest = local_day(cut)
        for recs in self.days.values():
            for day in [d for d in recs if d < oldest]:
                recs.pop(day)
        self.store.put('history:days', self.days)
