"""The App's own bounded history, and the recorder backfill that fills it once.

Two tables in the App's own SQLite (never the recorder):
- `psi`: the RO tank pressure, one value a minute (the state held at that minute; a gap stays empty);
- `pts`: change points of the yard and context series (valve open/closed, Orbit totals, battery,
  next watering, rain delay, garden temperature). Constant readings are not repeated: a reading
  holds until the next change, so a constant is idle, not missing.

A timer's `_2` spelling (the replacement device) is stored under the original id, so a series runs on
across the rename. The backfill reads one entity-day a minute, oldest day first, never today, for the
listed series only; a failed read is retried at most 3 times and never filled in.
"""
import bisect
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .entities import GAP, HISTORY, RO_PRESSURE, ZONES, changed_at, number, token, zone_ids

ZONE = ZoneInfo('America/Phoenix')
KEEP_DAYS = 120
PURGE_DAYS = 40
LOOKBACK_S = 3600
MAX_TRIES = 3
RETRY_S = 15 * 60          # a failed series-day waits 15, then 30 minutes before its next try
MAX_POINTS_PER_DAY = 5000
MAX_HISTORY_ROWS = 50000
RO_PIECES = 4              # the RO pressure day is read in 6-hour pieces: each reply stays far under the 4 MB cap
# The original id for every spelling: a `_2` id is stored under the id it replaced.
CANONICAL = {e: e for e in HISTORY}
for _, stem, _ in ZONES:
    for first, second in zone_ids(stem).values():
        CANONICAL[second] = first
# Each series once, with every spelling it may be read under (original first).
SPELLINGS = {}
for entity in sorted(HISTORY):
    SPELLINGS.setdefault(CANONICAL[entity], []).append(entity)
for key in SPELLINGS:
    SPELLINGS[key].sort(key=lambda e: e != key)


def local_midnight(day):
    return datetime.fromisoformat(day).replace(tzinfo=ZONE).timestamp()


def local_day(at):
    return datetime.fromtimestamp(at, ZONE).strftime('%Y-%m-%d')


def days_to_fetch(now):
    """Complete local days still in the recorder's window, oldest first (the next purge takes the oldest)."""
    today = datetime.fromtimestamp(now, ZONE).date()
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


def merge(per, start, end):
    """One series from its spellings' (time, state) lists, between start and end: at each change, the spelling
    holding a reading wins (the one that changed last when both do); it is a gap only when no spelling holds one."""
    times = [list(map(lambda p: p[0], pts)) for pts in per]
    marks = sorted({t for pts in per for t, _ in pts if start < t < end} | {start})
    out, last = [], object()
    for t in marks:
        best = None
        for ts, pts in zip(times, per):
            i = bisect.bisect_right(ts, t) - 1
            if i >= 0 and pts[i][1] not in GAP and (best is None or pts[i][0] > best[0]):
                best = pts[i]
        state = best[1] if best else None
        if state != last:
            out.append((t, state))
            last = state
    return out


def thin(rows):
    """At most one numeric reading a minute (the last in it), as live collection keeps; words (valve edges,
    rain delay) and gaps are never thinned."""
    out = []
    for row in rows:
        if out and row[2] is not None and out[-1][2] is not None and int(row[1] // 60) == int(out[-1][1] // 60):
            out[-1] = row
        else:
            out.append(row)
    return out


def minutes(points, start, end):
    """One value a minute from a piecewise-constant series: the state held at each minute; gaps stay empty."""
    out, i, held = [], 0, None
    for t in range(int(start), int(end), 60):
        while i < len(points) and points[i][0] <= t:
            held = number(points[i][1])
            i += 1
        if held is not None:
            out.append((t, held))
    return out


def as_point(state):
    """(value, word) for a stored change point: numbers as value, words as a safe token, a gap as 'na'."""
    if state in GAP:
        return None, 'na'
    n = number(state)
    return (n, None) if n is not None else (None, token(state))


class Series:
    def __init__(self, store):
        self.store = store
        self.db = store.db
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS psi (t INTEGER PRIMARY KEY, v REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS pts (entity TEXT NOT NULL, t REAL NOT NULL, v REAL, s TEXT,
                                          PRIMARY KEY (entity, t));
        ''')
        self.db.commit()
        self.days = store.get('history:days', {})
        self.last = store.get('series:last', {})

    # -- live -------------------------------------------------------------------------------
    def add_live(self, states, now):
        minute = int(now // 60 * 60)
        psi = number((states.get(RO_PRESSURE) or {}).get('state'), -5, 150)
        rows = []
        for key, spellings in sorted(SPELLINGS.items()):
            if key == RO_PRESSURE:
                continue
            seen = [states[e] for e in spellings if e in states]
            if not seen:
                continue
            # A renamed timer is one series: the spelling holding a reading wins (the one that changed last when
            # both do, so a stale old entity never hides its replacement); it is a gap only when none holds one.
            held = [x for x in seen if x.get('state') not in GAP]
            state = max(held, key=changed_at)['state'] if held else seen[0].get('state')
            value, word = as_point(state)
            mark = f'{value}|{word}'
            if self.last.get(key) != mark:
                self.last[key] = mark
                rows.append((key, float(now), value, word))
        with self.db:
            if psi is not None:
                self.db.execute('INSERT OR REPLACE INTO psi VALUES (?,?)', (minute, psi))
            self.db.executemany('INSERT OR REPLACE INTO pts VALUES (?,?,?,?)', rows)
        if rows:
            self.store.put('series:last', self.last)

    # -- backfill -----------------------------------------------------------------------------
    def _needs(self, entity, day, now):
        rec = self.days.get(entity, {}).get(day)
        return rec is None or (rec.get('status') == 'FAILED' and rec.get('tries', 0) < MAX_TRIES
                               and now - rec.get('at', 0) >= RETRY_S * rec.get('tries', 0))

    def pending(self, now):
        """The next (series, day) to read: oldest day first; every spelling of a series is read together. A failed
        one waits before its retry, so a few slow minutes in the recorder never use up its tries."""
        for day in days_to_fetch(now):
            for key in sorted(SPELLINGS):
                if any(self._needs(e, day, now) for e in SPELLINGS[key]):
                    return key, day
        return None

    def waiting(self, now):
        return sum(1 for d in days_to_fetch(now) for e in HISTORY if self.days.get(e, {}).get(d) is None)

    def backfill_step(self, read, now):
        """Read one series-day (each of its spellings) through `read(entity, start, end)`, which the runtime
        supplies and which returns None when the read failed (this module itself never touches the network).
        Returns 'IDLE' when nothing is pending, else the stored status. A failed spelling fails the series-day:
        it is retried at most MAX_TRIES times and never filled in."""
        nxt = self.pending(now)
        if nxt is None:
            return 'IDLE'
        key, day = nxt
        start = local_midnight(day)
        end = start + 86400
        recs = [self.days.setdefault(e, {}).setdefault(day, {'tries': 0}) for e in SPELLINGS[key]]
        for rec in recs:
            rec['tries'] = rec.get('tries', 0) + 1
        per = []
        try:
            for entity in SPELLINGS[key]:
                pieces = RO_PIECES if entity == RO_PRESSURE else 1
                step = 86400 // pieces
                got = []
                for n in range(pieces):
                    raw = read(entity, start + n * step - LOOKBACK_S, start + (n + 1) * step)
                    if raw is None:
                        raise ValueError('READ_FAILED')
                    got += changes(raw, entity)
                per.append(sorted(set(got), key=lambda p: p[0]))
        except ValueError:
            for rec in recs:
                rec.update(status='FAILED', at=int(now))
            self.store.put('history:days', self.days)
            return 'FAILED'
        merged = merge(per, start, end)
        truncated = False
        with self.db:
            if key == RO_PRESSURE:
                rows = minutes(merged, start, end)
                self.db.executemany('INSERT OR IGNORE INTO psi VALUES (?,?)', rows)
            else:
                rows = thin([(key, float(t)) + as_point(state) for t, state in merged])
                truncated = len(rows) > MAX_POINTS_PER_DAY
                if truncated:
                    # The kept points end with a gap, so the last kept reading is never held past what was kept.
                    rows = rows[:MAX_POINTS_PER_DAY - 1] + [(key, rows[MAX_POINTS_PER_DAY - 1][1], None, 'na')]
                # Live points win: a backfilled point never replaces one seen live.
                self.db.executemany('INSERT OR IGNORE INTO pts VALUES (?,?,?,?)', rows)
        status = 'DONE' if rows else 'EMPTY'
        for rec, pts in zip(recs, per):
            rec.update(status=status, rows=len(rows), at=int(now), truncated=truncated,
                       partial=not pts or pts[0][0] > start)
        self.store.put('history:days', self.days)
        return status

    # -- reading back ---------------------------------------------------------------------------
    def psi(self, since):
        return [tuple(r) for r in self.db.execute('SELECT t, v FROM psi WHERE t >= ? ORDER BY t', (since,))]

    def points(self, entity, since):
        """Change points of one series (by its original id), with the point held at `since` first."""
        key = CANONICAL.get(entity, entity)
        held = self.db.execute('SELECT t, v, s FROM pts WHERE entity=? AND t < ? ORDER BY t DESC LIMIT 1',
                               (key, since)).fetchall()
        rows = self.db.execute('SELECT t, v, s FROM pts WHERE entity=? AND t >= ? ORDER BY t', (key, since)).fetchall()
        return [tuple(r) for r in held + rows]

    def report(self, now):
        waiting = self.waiting(now)
        counts = {s: sum(r.get('status') == s for recs in self.days.values() for r in recs.values())
                  for s in ('DONE', 'EMPTY', 'FAILED')}
        return {'status': 'BACKFILLING' if waiting else 'KEEPING', 'entity_days_waiting': waiting, **counts,
                'psi_minutes': self.db.execute('SELECT COUNT(*) FROM psi').fetchone()[0],
                'points': self.db.execute('SELECT COUNT(*) FROM pts').fetchone()[0]}

    def prune(self, now):
        cut = now - KEEP_DAYS * 86400
        with self.db:
            self.db.execute('DELETE FROM psi WHERE t < ?', (cut,))
            self.db.execute('DELETE FROM pts WHERE t < ?', (cut,))
        oldest = local_day(cut)
        for recs in self.days.values():
            for day in [d for d in recs if d < oldest]:
                recs.pop(day)
        self.store.put('history:days', self.days)
