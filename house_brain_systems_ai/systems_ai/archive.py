"""Archive keeper (0.1.0): the cooling season's full-detail history, kept before the recorder purges it.

Owner pop-up 2026-10-24 "Yes, add to Systems AI 0.1" (AI family plan F1, rolling archive; the season
archive proposal docs/architecture/COOLING_SEASON_DATA_ARCHIVE_PROPOSAL_R1.md, windows A and B). It reads
Home Assistant's own recorder history, read-only, one entity-day per call and at most one call a minute,
for exactly the entities below: plant and physical data only (temperatures, humidity, dew points, relays,
the Arbiter phase, eligibility). Oldest days first, because the 40-day purge removes them first.

Numeric entities are kept as 5-minute time-weighted means. The recorder writes a state only when it
changes, so a reading holds until the next recorded change: a constant reading (0 W all night) is idle,
not missing. Only `unknown` and `unavailable` are gaps, and nothing is invented across them. Relays, phases and codes are kept as exact change edges. Everything
stays in this App's own database for 120 days (about 10 MB for a cooling season, about 25 MB at most),
which the owner's backups copy off the Pi; one day downloads as CSV from the ingress page. No attribute,
person, location or camera data is read or kept.
"""
import re
import sqlite3
from datetime import datetime, timedelta, timezone

PHOENIX = timezone(timedelta(hours=-7))
# The 18 entities of the archive proposal's History list (core for #112, #116 or #45).
ENTITIES = (
    'sensor.evap_local_dew_point_consensus', 'sensor.evap_local_outdoor_temperature',
    'sensor.evap_local_outdoor_humidity', 'sensor.evap_local_wet_bulb_temperature',
    'sensor.evap_robust_indoor_temperature', 'sensor.error_ecobee_thermostat_temperature',
    'sensor.error_ecobee_thermostat_humidity', 'sensor.cooling_arbiter_v2_indoor_dew_point',
    'sensor.cooling_arbiter_v2_indoor_house_humidity', 'switch.backyard_zen_16_multirelay_1',
    'switch.backyard_zen_16_multirelay_2', 'switch.backyard_zen_16_multirelay_3',
    'sensor.evap_actual_relay_mode', 'input_select.cooling_arbiter_v2_phase',
    'sensor.evap_v24_eligibility_code', 'sensor.air_conditioning_power',
    'sensor.attic_thermometer', 'sensor.attic_thermometer_2')
# Relays, the relay mode, the Arbiter phase and the eligibility code are states, not quantities: kept as
# exact change edges, never averaged (a mean of two codes is not a code).
EDGE_ENTITIES = frozenset(e for e in ENTITIES if e.startswith(('switch.', 'input_select.'))) | {
    'sensor.evap_actual_relay_mode', 'sensor.evap_v24_eligibility_code'}
# Restricted domains of contracts/house_brain_privacy_data_classes.v1.json: never read, whatever the list says.
REFUSED_DOMAINS = ('person', 'device_tracker', 'zone', 'geo_location', 'camera', 'image', 'lock',
                   'alarm_control_panel')
RE_ENTITY = re.compile(r'(sensor|switch|input_select)\.[a-z0-9_]{1,80}')
# A state word: letters, digits, spaces and . _ : + - only (no comma, quote or newline: the CSV stays plain).
RE_TEXT = re.compile(r'[A-Za-z0-9_.:+ -]{1,64}')
PURGE_DAYS = 40
BUCKET_S = 300
LOOKBACK_S = 3600  # read from an hour before midnight, so the day opens with the state then held
MAX_EDGES_PER_DAY = 2000
MAX_TRIES = 3
KEEP_DAYS = 120
UNAVAILABLE = ('unknown', 'unavailable', '', None)


def allowed(entity):
    return entity in ENTITIES and RE_ENTITY.fullmatch(entity) is not None \
        and entity.split('.', 1)[0] not in REFUSED_DOMAINS


def local_midnight(day):
    """Unix time of 00:00 America/Phoenix for an ISO day."""
    return datetime.fromisoformat(day).replace(tzinfo=PHOENIX).timestamp()


def days_to_fetch(now):
    """Complete local days still inside the purge window, oldest first (the next purge takes the oldest)."""
    today = datetime.fromtimestamp(now, PHOENIX).date()
    return [(today - timedelta(days=n)).isoformat() for n in range(PURGE_DAYS - 1, 0, -1)]


def _stamp(value):
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('TIMEZONE_REQUIRED')
    return dt.timestamp()


def _number(state):
    try:
        n = float(state)
    except (TypeError, ValueError):
        return None
    return n if n == n and abs(n) < 1e7 else None


def changes(rows, entity):
    """(time, state) pairs from a history reply for one entity; strict, sorted."""
    if not isinstance(rows, list) or len(rows) > 1:
        raise ValueError('HISTORY_SHAPE')
    out = []
    for row in (rows[0] if rows else [])[:20000]:
        if not isinstance(row, dict):
            raise ValueError('HISTORY_ROW_INVALID')
        if 'entity_id' in row and row['entity_id'] != entity:
            raise ValueError('HISTORY_ENTITY_MISMATCH')
        out.append((_stamp(row.get('last_changed') or row.get('last_updated')), row.get('state')))
    return sorted(out)


def buckets(points, start, end):
    """5-minute time-weighted means of a piecewise-constant numeric series; unknown/unavailable stay empty,
    and a bucket needs at least half its time covered."""
    totals = {}
    series = [(t, _number(s)) for t, s in points]
    for i, (t, value) in enumerate(series):
        if value is None:
            continue
        stop = min(series[i + 1][0] if i + 1 < len(series) else end, end)
        cursor = max(t, start)
        while cursor < stop:
            slot = start + (int(cursor - start) // BUCKET_S) * BUCKET_S
            upto = min(stop, slot + BUCKET_S)
            total, seconds = totals.get(slot, (0., 0.))
            totals[slot] = (total + value * (upto - cursor), seconds + upto - cursor)
            cursor = upto
    return [(slot, round(total / seconds, 3)) for slot, (total, seconds) in sorted(totals.items())
            if seconds >= BUCKET_S / 2]


def edges(points, start, end):
    """Exact change edges inside the day, opened by the state held at midnight; text bounded to a safe
    token, anything else 'other'."""
    before = [p for p in points if p[0] < start]
    inside = [p for p in points if start <= p[0] < end]
    out, last = [], object()
    for t, state in before[-1:] + inside:
        text = 'na' if state in UNAVAILABLE else state if isinstance(state, str) and RE_TEXT.fullmatch(state) else 'other'
        if text != last:
            out.append((max(t, start), text))
            last = text
    return out[:MAX_EDGES_PER_DAY]


class Archive:
    def __init__(self, store, client):
        self.store, self.client = store, client
        self.db = store.db
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS archive (
            entity TEXT NOT NULL, at REAL NOT NULL, value REAL, text TEXT, kind TEXT NOT NULL,
            PRIMARY KEY(entity, at, kind)) WITHOUT ROWID;''')
        self.db.commit()
        self.days = store.get('archive:days', {})

    def pending(self, now):
        """Next (entity, day) to read, oldest day first; failed days are retried at most MAX_TRIES times."""
        for day in days_to_fetch(now):
            for entity in ENTITIES:
                rec = self.days.get(entity, {}).get(day)
                if rec is None or (rec.get('status') == 'FAILED' and rec.get('tries', 0) < MAX_TRIES):
                    return entity, day
        return None

    def step(self, now):
        """Read one entity-day. Returns 'IDLE' when nothing is pending, else the stored status."""
        nxt = self.pending(now)
        if nxt is None:
            return 'IDLE'
        entity, day = nxt
        if not allowed(entity):
            raise ValueError('ARCHIVE_ENTITY_REFUSED')
        start = local_midnight(day)
        end = start + 86400
        rec = self.days.setdefault(entity, {}).setdefault(day, {'tries': 0})
        rec['tries'] = rec.get('tries', 0) + 1
        try:
            points = changes(self.client.history(entity, start - LOOKBACK_S, end), entity)
        except Exception:  # noqa: BLE001 - one failed read: retried later, never filled in
            rec.update(status='FAILED')
            self.store.put('archive:days', self.days)
            return 'FAILED'
        numeric = entity not in EDGE_ENTITIES
        rows = ([(entity, t, v, None, '5min') for t, v in buckets(points, start, end)] if numeric
                else [(entity, t, None, s, 'edge') for t, s in edges(points, start, end)])
        with self.db:
            self.db.executemany('INSERT OR REPLACE INTO archive VALUES (?,?,?,?,?)', rows)
        # A day the purge has already cut into has no state at midnight: kept, but marked partial.
        rec.update(status='DONE' if rows else 'EMPTY', rows=len(rows), kind='5min' if numeric else 'edge', at=int(now),
                   partial=not points or points[0][0] > start)
        self.store.put('archive:days', self.days)
        return rec['status']

    def prune(self, now):
        with self.db:
            self.db.execute('DELETE FROM archive WHERE at < ?', (now - KEEP_DAYS * 86400,))
        # The per-day bookkeeping goes with the data, so it never outgrows the store's value bound.
        oldest = datetime.fromtimestamp(now - KEEP_DAYS * 86400, PHOENIX).date().isoformat()
        for recs in self.days.values():
            for day in [d for d in recs if d < oldest]:
                recs.pop(day)
        self.store.put('archive:days', self.days)

    def report(self, now):
        window = days_to_fetch(now)
        per = {}
        for entity in ENTITIES:
            recs = self.days.get(entity, {})
            per[entity] = {'done': sum(r.get('status') == 'DONE' for r in recs.values()),
                           'empty': sum(r.get('status') == 'EMPTY' for r in recs.values()),
                           'failed': sum(r.get('status') == 'FAILED' for r in recs.values()),
                           'partial': sum(bool(r.get('partial')) for r in recs.values()),
                           'earliest': min((d for d, r in recs.items() if r.get('status') == 'DONE'), default=None)}
        waiting = sum(1 for d in window for e in ENTITIES if self.days.get(e, {}).get(d) is None)
        total = self.db.execute('SELECT COUNT(*) FROM archive').fetchone()[0]
        return {'status': 'BACKFILLING' if waiting else 'KEEPING', 'entity_days_waiting': waiting,
                'rows': total, 'earliest_day': min((p['earliest'] for p in per.values() if p['earliest']), default=None),
                'entities': per, 'purge_days': PURGE_DAYS, 'one_read_per_minute': True}


RE_DAY = re.compile(r'20[0-9]{2}-[01][0-9]-[0-3][0-9]')


def day_csv(path, day):
    """One local day, every entity: entity_id,time_utc,value,kind (the owner's download on the ingress
    page). Its own read-only connection, because the web thread may not share the loop's."""
    if not isinstance(day, str) or not RE_DAY.fullmatch(day):
        raise ValueError('ARCHIVE_DAY_INVALID')
    start = local_midnight(day)
    lines = ['entity_id,time_utc,value,kind']
    db = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=3)
    try:
        for entity, at, value, text, kind in db.execute(
                'SELECT entity, at, value, text, kind FROM archive WHERE at >= ? AND at < ? ORDER BY entity, at',
                (start, start + 86400)):
            stamp = datetime.fromtimestamp(at, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            lines.append(f'{entity},{stamp},{value if value is not None else text},{kind}')
    finally:
        db.close()
    return '\n'.join(lines) + '\n'
