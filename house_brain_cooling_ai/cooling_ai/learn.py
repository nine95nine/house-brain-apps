"""How the house actually cools: rates per plant mode, overnight drift, modelled evap effectiveness and
the A/C's response, learned from 5-minute history and scored honestly.

Each model is a robust line (Theil-Sen) of the indoor cooling rate (°F/h) against the mode's driving
difference: outdoor minus indoor for IDLE drift, VENT and AC, wet bulb minus indoor for wet evap.
It is fitted on TRAIN days and its band is the split-conformal 90 % quantile of the error on
HOLDOUT days (one local day in five). Thermal response only: never COP, EER, SEER, capacity,
airflow or equipment health.

Forecasts are frozen when made and scored later beside "no change"; a model is called a forecast
only after it beat that on at least 5 scored days.
"""
import hashlib
import json
import math
import statistics
import sys
from datetime import datetime, timezone

from .entities import ARCHIVE_KEYS, MODES, actual_mode, fan_speed, number, pump, token, ALL_NUMBERS
from .plan import ZONE, local_day, predict

BUCKET_S = 300
KEEP_DAYS = 120
MIN_SEGMENT = 6            # 30 minutes in one clean mode
SKIP_START = 2             # the first 10 minutes of a mode are transition, not response
MIN_TRAIN = 36             # 3 hours of interior points ...
MIN_TRAIN_DAYS = 2         # ... over at least two local days
MIN_HOLDOUT = 12
COVERAGE = 0.9
MIN_SCORED_DAYS = 5
BETTER = 0.95
MAX_RATE = 15.             # °F/h; anything steeper is a sensor jump, not a thermal response
EPSILON_MIN_DEPRESSION = 3.
KEEP_FORECASTS = 600
NIGHT_COVERAGE = 0.9
KEEP_EPISODES = 200
EPISODE_SCHEMA = 'house_brain_cooling_thermal_episode.v1'
NUMERIC_KEYS = frozenset(ALL_NUMBERS)
# What learning, the plan list and the forecasts read back from history; nothing else is loaded.
LEARN_KEYS = frozenset({'t_in', 't_out', 'wb', 'rh_in', 'rh_out', 'dp_out', 'relay', 'ac', 'owner', 'supply', 'ac_w',
                        'evap_w', 'price', 'on_peak', 'holiday', 'bank_kwh'})


def holdout(day):
    """Split by local day, never by minute: one day in five (stable hash) is HOLDOUT."""
    return int(hashlib.sha256(day.encode()).hexdigest()[:8], 16) % 5 == 0


def theil_sen(points):
    pts = points[::max(1, len(points) // 200)]
    slopes = [(y2 - y1) / (x2 - x1) for i, (x1, y1) in enumerate(pts) for x2, y2 in pts[i + 1:] if x2 != x1]
    if not slopes:
        return None
    slope = statistics.median(slopes)
    return slope, statistics.median(y - slope * x for x, y in pts)


def quantile(values, q):
    ordered = sorted(values)
    k = min(len(ordered) - 1, max(0, math.ceil((len(ordered) + 1) * q) - 1))
    return ordered[k]


# -- the 5-minute grid ---------------------------------------------------------------------------
def archive_rows(doc):
    """Systems AI archive day -> {bucket_start: row}. Edges hold their state until the next edge;
    numbers are the archive's own 5-minute values. An entity not read that day stays a gap."""
    day_start = datetime.strptime(doc['day'], '%Y-%m-%d').replace(tzinfo=ZONE).timestamp()
    keys_ok = {ARCHIVE_KEYS[e] for e, st in doc['status'].items()
               if e in ARCHIVE_KEYS and isinstance(st, dict) and st.get('status') in ('DONE', 'EMPTY')}
    values, edges = {}, {}
    for entity, at, value, text, kind in doc['rows']:
        key = ARCHIVE_KEYS.get(entity)
        if key is None or key not in keys_ok or not isinstance(at, (int, float)):
            continue
        if kind == '5min':
            n = number(value)
            if n is not None:
                values.setdefault(int(at // BUCKET_S * BUCKET_S), {})[key] = n
        elif kind == 'edge':
            edges.setdefault(key, []).append((at, token(text) if text is not None else None))
    out = {}
    for t in range(int(day_start), int(day_start) + 86400, BUCKET_S):
        row = dict(values.get(t, {}))
        for key, series in edges.items():
            state = None
            for at, s in series:
                if at <= t + BUCKET_S / 2:
                    state = s
                else:
                    break
            if state is not None:
                row[key] = state
        if row:
            out[t] = row
    return out


class Minutes:
    """Live readings folded into 5-minute rows: numbers averaged, words the last seen."""

    def __init__(self):
        self.bucket, self.sums, self.words = None, {}, {}

    def add(self, row, now):
        bucket = int(now // BUCKET_S * BUCKET_S)
        done = None
        if self.bucket is not None and bucket != self.bucket:
            done = self.close()
        self.bucket = bucket
        for k, v in row.items():
            if k in NUMERIC_KEYS:
                if v is not None:
                    total, n = self.sums.get(k, (0., 0))
                    self.sums[k] = (total + v, n + 1)
            elif v is not None:
                self.words[k] = v
        return done

    def close(self):
        if self.bucket is None:
            return None
        row = {k: total / n for k, (total, n) in self.sums.items() if n}
        row.update(self.words)
        out = (self.bucket, row)
        self.bucket, self.sums, self.words = None, {}, {}
        return out


class Grid:
    """The App's own bounded 5-minute history (live rows win over archive rows for the same bucket)."""

    def __init__(self, store):
        self.db = store.db
        self.db.execute('CREATE TABLE IF NOT EXISTS grid (t INTEGER PRIMARY KEY, src TEXT NOT NULL, row TEXT NOT NULL)')
        self.db.commit()

    def put(self, t, row, src):
        verb = 'INSERT OR REPLACE' if src == 'LIVE' else 'INSERT OR IGNORE'
        if src == 'LIVE':
            old = self.db.execute("SELECT row FROM grid WHERE t=? AND src='LIVE'", (int(t),)).fetchone()
            if old:     # the same bucket written before a restart: keep what it saw, the newer minutes win
                row = {**json.loads(old[0]), **{k: v for k, v in row.items() if v is not None}}
        with self.db:
            self.db.execute(f'{verb} INTO grid VALUES (?,?,?)',
                            (int(t), src, json.dumps(row, allow_nan=False, separators=(',', ':'))))

    def put_many(self, rows, src):
        with self.db:
            self.db.executemany('INSERT OR IGNORE INTO grid VALUES (?,?,?)',
                                [(int(t), src, json.dumps(r, allow_nan=False, separators=(',', ':')))
                                 for t, r in sorted(rows.items())])

    def rows(self, since=0, keys=None):
        """Rows in time order. With `keys`, only those keys are kept and words are interned, so 120 days
        of history stay small in memory (the App runs under a 256 MiB address cap)."""
        out = []
        for t, raw in self.db.execute('SELECT t,row FROM grid WHERE t>=? ORDER BY t', (since,)):
            row = json.loads(raw)
            if keys is not None:
                row = {k: sys.intern(v) if isinstance(v, str) else v for k, v in row.items() if k in keys}
            out.append((t, row))
        return out

    def count(self):
        return {src: n for src, n in self.db.execute('SELECT src,COUNT(*) FROM grid GROUP BY src')}

    def prune(self, now):
        with self.db:
            self.db.execute('DELETE FROM grid WHERE t<?', (now - KEEP_DAYS * 86400,))


# -- episodes ------------------------------------------------------------------------------------
def segments(rows):
    """Runs of one clean actual mode, ≥ 30 minutes, contiguous 5-minute buckets."""
    out, run = [], []
    for t, row in rows:
        mode = actual_mode(row)
        clean = mode in MODES and row.get('t_in') is not None and row.get('owner') not in (
            'LOCAL_MANUAL', 'HUMAN_THERMOSTAT', 'LIFE_SAFETY')
        if run and (not clean or mode != run[-1][2] or t - run[-1][0] != BUCKET_S):
            if len(run) >= MIN_SEGMENT:
                out.append(run)
            run = []
        if clean:
            run.append((t, row, mode))
    if len(run) >= MIN_SEGMENT:
        out.append(run)
    return out


def points(run):
    """(day, drive, rate °F/h, row) for interior buckets: centred difference over 10 minutes."""
    mode = run[0][2]
    out = []
    for i in range(SKIP_START, len(run) - 1):
        (_, before, _), (t, row, _), (_, after, _) = run[i - 1], run[i], run[i + 1]
        rate = (after['t_in'] - before['t_in']) / (2 * BUCKET_S / 3600)
        other = row.get('wb') if mode == 'WET_EVAP' else row.get('t_out')
        if other is None or abs(rate) > MAX_RATE:
            continue
        out.append((local_day(t), other - row['t_in'], rate, row))
    return out


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def episode(run, manifest_sha256, policy='cooling_arbiter_v2_1_r19'):
    """One schema-valid house_brain_cooling_thermal_episode.v1 (contracts/cooling_thermal_episode.v1)."""
    mode = run[0][2]
    samples, missing = [], 0
    for t, row, _ in run:
        need = (row.get('t_in'), row.get('rh_in'), row.get('t_out'), row.get('rh_out'))
        if any(v is None for v in need) or not (0 <= row['rh_in'] <= 100 and 0 <= row['rh_out'] <= 100):
            missing += 1
            continue
        samples.append({'timestamp_utc': iso(t), 'indoor_temp_f': round(row['t_in'], 2),
                        'indoor_rh_percent': round(row['rh_in'], 1), 'outdoor_temp_f': round(row['t_out'], 2),
                        'outdoor_rh_percent': round(row['rh_out'], 1),
                        'outdoor_dew_point_f': round(row['dp_out'], 2) if row.get('dp_out') is not None else None,
                        'ac_operation': {'on': 'ON', 'off': 'OFF'}.get(row.get('ac'), 'UNKNOWN'),
                        'evap_fan_operation': fan_speed(row), 'evap_pump_operation': pump(row),
                        'operation_feedback_class': 'INFERRED' if mode == 'AC' else 'INDEPENDENT',
                        'permission_state': 'UNKNOWN'})
    if len(samples) < 2:
        return None
    day = local_day(run[0][0])
    return {'schema_version': EPISODE_SCHEMA,
            'episode_id': f'COOLING_AI:{mode}:{run[0][0]}',
            'evidence_class': 'MEASURED_PHYSICAL',
            'dataset_role': 'HOLDOUT' if holdout(day) else 'TRAIN',
            'start_utc': iso(run[0][0]), 'end_utc': iso(run[-1][0]),
            'nominal_sample_interval_seconds': BUCKET_S,
            'source_identity': {'cooling_policy_revision': policy,
                                'episode_capture_revision': 'house_brain_cooling_ai.grid.v1',
                                'ha_core_version': None, 'source_manifest_sha256': manifest_sha256,
                                'source_export_sha256': None},
            'episode_context': {'primary_mode': {'IDLE': 'OFF', 'AC': 'AC'}.get(mode, 'EVAP'),
                                'relief_path_state': 'UNKNOWN',
                                'confounder_flags': ['SENSOR_STALE'] if missing else [],
                                'evidence_refs': ['5-minute time-weighted recorder values via Systems AI or live states',
                                                  'actual operation: sensor.evap_actual_relay_mode, '
                                                  'binary_sensor.ac_compressor_cooling']},
            'quality': {'required_sources_fresh': missing == 0, 'actual_operation_feedback_present': True,
                        'single_primary_mode': True, 'clock_monotonic_verified': True,
                        'missing_sample_fraction': round(missing / len(run), 4)},
            'samples': samples,
            'privacy': {'contains_person_identity': False, 'contains_precise_presence': False,
                        'contains_camera_audio': False, 'household_control_authority': False,
                        'physical_actions': False}}


# -- models --------------------------------------------------------------------------------------
def fit(mode, pts):
    train = [(x, y) for d, x, y, _ in pts if not holdout(d)]
    held = [(x, y) for d, x, y, _ in pts if holdout(d)]
    train_days = len({d for d, *_ in pts if not holdout(d)})
    base = {'mode': mode, 'train_points': len(train), 'holdout_points': len(held), 'train_days': train_days,
            'unit': '°F/h', 'drive': 'wet bulb - indoor (°F)' if mode == 'WET_EVAP' else 'outdoor - indoor (°F)'}
    if len(train) < MIN_TRAIN or train_days < MIN_TRAIN_DAYS:
        return {**base, 'status': 'LEARNING', 'band': None}
    line = theil_sen(train)
    if line is None:
        return {**base, 'status': 'LEARNING', 'band': None}
    slope, intercept = line
    out = {**base, 'slope': slope, 'intercept': intercept}
    if len(held) < MIN_HOLDOUT:
        return {**out, 'status': 'EARLY', 'band': None}
    errors = [abs(y - (slope * x + intercept)) for x, y in held]
    return {**out, 'status': 'LEARNED', 'band': quantile(errors, COVERAGE),
            'holdout_mae': statistics.mean(errors)}


def learn(rows, manifest_sha256):
    """All models, the A/C response, ε, powers and recent episodes from the grid."""
    runs = segments(rows)
    by_mode = {m: [] for m in MODES}
    for run in runs:
        by_mode[run[0][2]].extend(points(run))
    models = {m: fit(m, by_mode[m]) for m in MODES}
    idle = models['IDLE']
    if idle.get('status') in ('LEARNED', 'EARLY') and idle['slope'] > 0:
        idle['time_constant_hours'] = 1 / idle['slope']
    episodes = [e for e in (episode(r, manifest_sha256) for r in runs[-KEEP_EPISODES:]) if e]
    return {'models': models, 'ac_response': ac_response(rows), 'epsilon': epsilon(rows), 'power': power(rows),
            'prices': prices(rows), 'episodes': episodes, 'segments': len(runs),
            'points': {m: len(by_mode[m]) for m in MODES}}


def ac_response(rows):
    """Per local day: A/C runtime and cycles from the Ecobee's call, per outdoor degree-hour over 65 °F."""
    days = {}
    last = None
    for t, row in rows:
        day = local_day(t)
        rec = days.setdefault(day, {'runtime_h': 0., 'cycles': 0, 'degree_h': 0., 'buckets': 0})
        ac = row.get('ac')
        if ac in ('on', 'off'):
            rec['buckets'] += 1
            rec['runtime_h'] += BUCKET_S / 3600 if ac == 'on' else 0
            rec['cycles'] += 1 if ac == 'on' and last == 'off' else 0
            last = ac
        if row.get('t_out') is not None:
            rec['degree_h'] += max(0., row['t_out'] - 65.) * BUCKET_S / 3600
    out = {}
    for day, rec in sorted(days.items())[-30:]:
        if rec['buckets'] < 200:
            continue
        out[day] = {'runtime_h': round(rec['runtime_h'], 2), 'cycles': rec['cycles'],
                    'runtime_min_per_degree_h': round(rec['runtime_h'] * 60 / rec['degree_h'], 3)
                    if rec['degree_h'] > 1 else None,
                    'cycles_per_runtime_h': round(rec['cycles'] / rec['runtime_h'], 2) if rec['runtime_h'] else None}
    return {'days': out, 'source': 'binary_sensor.ac_compressor_cooling (Ecobee call, INFERRED)',
            'note': 'Thermal response only; not COP, capacity or equipment health.'}


def epsilon(rows):
    """MODELLED evap effectiveness per day: (outdoor - supply) / (outdoor - wet bulb) during wet evap.
    The supply is the model's estimate until the #119 probe exists, so this shows the model's
    assumption, not pad scaling."""
    days = {}
    for t, row in rows:
        if actual_mode(row) != 'WET_EVAP':
            continue
        t_out, wb, supply = row.get('t_out'), row.get('wb'), row.get('supply')
        if None in (t_out, wb, supply) or t_out - wb < EPSILON_MIN_DEPRESSION:
            continue
        days.setdefault(local_day(t), []).append((t_out - supply) / (t_out - wb))
    return {'days': {d: round(statistics.median(v), 3) for d, v in sorted(days.items())[-30:] if len(v) >= 6},
            'evidence_class': 'MODELLED', 'note': 'Uses the modelled supply temperature until the #119 probe.'}


def power(rows):
    """Median kW per actual mode: A/C from Sense (energy only, labelled), evap from its modelled watts."""
    ac = [row['ac_w'] for _, row in rows if actual_mode(row) == 'AC' and (row.get('ac_w') or 0) > 200]
    out = {'IDLE': 0.}
    if len(ac) >= 12:
        out['AC'] = statistics.median(ac) / 1000
    for mode in ('VENT', 'WET_EVAP'):
        w = [row['evap_w'] for _, row in rows if actual_mode(row) == mode and row.get('evap_w') is not None]
        if len(w) >= 12:
            out[mode] = statistics.median(w) / 1000
    return out


def prices(rows):
    on = [r['price'] for _, r in rows if r.get('price') is not None and r.get('on_peak') == 'on']
    off = [r['price'] for _, r in rows if r.get('price') is not None and r.get('on_peak') == 'off']
    if len(on) < 6 or len(off) < 6:
        return {}
    return {'on_peak': statistics.median(on), 'off_peak': statistics.median(off),
            'spread': statistics.median(on) - statistics.median(off)}


# -- frozen forecasts ----------------------------------------------------------------------------
class Forecasts:
    """1-hour indoor temperature under the current mode, frozen hourly and scored beside "no change"."""

    def __init__(self, store):
        self.store = store
        self.book = store.get('forecasts:1h', [])

    def freeze(self, row, models, now):
        hour = int(now // 3600)
        if any(f['hour'] == hour for f in self.book):
            return
        mode = actual_mode(row)
        model = models.get(mode)
        rate = predict(model, row) if mode in MODES and model and model.get('status') == 'LEARNED' else None
        if rate is None or row.get('t_in') is None:
            return
        self.book.append({'hour': hour, 'made_at': now, 'due_at': now + 3600, 'mode': mode,
                          'model': row['t_in'] + rate[0], 'low': row['t_in'] + rate[0] - rate[1],
                          'high': row['t_in'] + rate[0] + rate[1], 'baseline': row['t_in'], 'actual': None})
        self.book = self.book[-KEEP_FORECASTS:]
        self.store.put('forecasts:1h', self.book)

    def score(self, row, now):
        changed = False
        mode = actual_mode(row)
        for f in self.book:
            if f['actual'] is not None:
                continue
            if mode in MODES and mode != f['mode'] and now < f['due_at']:
                f['actual'] = 'MODE_CHANGED'      # not a test of the model: the plant did something else
                changed = True
            elif now >= f['due_at']:
                if now - f['due_at'] <= 300 and row.get('t_in') is not None:
                    f['actual'] = row['t_in']
                elif now - f['due_at'] > 300:
                    f['actual'] = 'MISSING'
                changed = True
        if changed:
            self.store.put('forecasts:1h', self.book)

    def report(self):
        rows = [f for f in self.book if isinstance(f['actual'], (int, float))]
        days = {local_day(f['made_at']) for f in rows}
        if not rows:
            return {'scored': 0, 'status': 'WAITING'}
        model = statistics.mean(abs(f['model'] - f['actual']) for f in rows)
        base = statistics.mean(abs(f['baseline'] - f['actual']) for f in rows)
        covered = sum(f['low'] <= f['actual'] <= f['high'] for f in rows) / len(rows)
        proven = len(days) >= MIN_SCORED_DAYS and model <= base * BETTER
        return {'scored': len(rows), 'scored_days': len(days), 'model_mae': round(model, 3),
                'baseline_mae': round(base, 3), 'band_coverage': round(covered, 3), 'proven': proven,
                'status': 'PROVEN' if proven else 'NOT_YET_BETTER' if len(days) >= MIN_SCORED_DAYS else 'WAITING'}


class Overnight:
    """At 22:00 local, the 06:00 indoor temperature if the plant stays idle: the drift model stepped
    through last night's outdoor temperatures (a persistence weather guess, no external forecast).
    Scored at 06:00 beside "no change", and only when the plant really stayed idle all night."""

    def __init__(self, store):
        self.store = store
        self.book = store.get('forecasts:overnight', [])

    def freeze(self, row, rows, models, now):
        local = datetime.fromtimestamp(now, ZONE)
        day = local.strftime('%Y-%m-%d')
        idle = models.get('IDLE')
        if local.hour != 22 or any(f['day'] == day for f in self.book) or row.get('t_in') is None \
                or not idle or idle.get('status') != 'LEARNED':
            return
        last_night = {t: r for t, r in rows if now - 86400 <= t < now - 86400 + 8 * 3600}
        t_sim, steps = row['t_in'], 0
        for t in sorted(last_night):
            t_out = last_night[t].get('t_out')
            if t_out is None:
                continue
            t_sim += (idle['slope'] * (t_out - t_sim) + idle['intercept']) * BUCKET_S / 3600
            steps += 1
        if steps < 80:          # under ~7 of the 8 hours of last night's outdoor readings: no forecast
            return
        band = idle['band'] * 8         # a steady rate error adds up over 8 hours; never narrower
        self.book = (self.book + [{'day': day, 'made_at': now, 'due_at': now + 8 * 3600, 'model': t_sim,
                                   'low': t_sim - band, 'high': t_sim + band, 'baseline': row['t_in'],
                                   'actual': None}])[-60:]
        self.store.put('forecasts:overnight', self.book)

    def score(self, rows, now):
        changed = False
        for f in self.book:
            if f['actual'] is not None or now < f['due_at']:
                continue
            night = [actual_mode(r) for t, r in rows if f['made_at'] <= t <= f['due_at']]
            end = [r['t_in'] for t, r in rows if abs(t - f['due_at']) <= 600 and r.get('t_in') is not None]
            if any(m in MODES and m != 'IDLE' for m in night):
                f['actual'] = 'PLANT_RAN'
            elif sum(m == 'IDLE' for m in night) < NIGHT_COVERAGE * 96:
                f['actual'] = 'MISSING' if now - f['due_at'] > 3600 else None   # not enough night seen: not "idle"
                if f['actual'] is None:
                    continue
            elif end:
                f['actual'] = end[len(end) // 2]
            elif now - f['due_at'] > 3600:
                f['actual'] = 'MISSING'
            else:
                continue
            changed = True
        if changed:
            self.store.put('forecasts:overnight', self.book)

    def report(self):
        rows = [f for f in self.book if isinstance(f['actual'], (int, float))]
        if not rows:
            return {'scored': 0, 'status': 'WAITING', 'not_scored_plant_ran': sum(f['actual'] == 'PLANT_RAN'
                                                                                   for f in self.book)}
        model = statistics.mean(abs(f['model'] - f['actual']) for f in rows)
        base = statistics.mean(abs(f['baseline'] - f['actual']) for f in rows)
        proven = len(rows) >= MIN_SCORED_DAYS and model <= base * BETTER
        return {'scored': len(rows), 'model_mae': round(model, 3), 'baseline_mae': round(base, 3),
                'band_coverage': round(sum(f['low'] <= f['actual'] <= f['high'] for f in rows) / len(rows), 3),
                'proven': proven, 'not_scored_plant_ran': sum(f['actual'] == 'PLANT_RAN' for f in self.book),
                'status': 'PROVEN' if proven else 'NOT_YET_BETTER' if len(rows) >= MIN_SCORED_DAYS else 'WAITING'}


class Boost:
    """Humid Boost: the projected indoor RH at a session's start against the measured RH at its end."""

    def __init__(self, store):
        self.store = store
        self.state = store.get('boost', {'open': None, 'runs': []})

    def add(self, row, now):
        on = row.get('boost')
        if on == 'on' and self.state['open'] is None and row.get('boost_rh') is not None:
            self.state['open'] = {'at': now, 'projected': row['boost_rh']}
        elif on == 'off' and self.state['open'] is not None:
            if row.get('rh_in') is not None:
                self.state['runs'] = (self.state['runs'] + [{**self.state['open'], 'ended': now,
                                                              'measured': row['rh_in'],
                                                              'error': row['rh_in'] - self.state['open']['projected']}])[-30:]
            self.state['open'] = None
        else:
            return
        self.store.put('boost', self.state)

    def report(self):
        runs = self.state['runs']
        return {'runs': len(runs), 'open': self.state['open'] is not None,
                'mean_error_rh': round(statistics.mean(r['error'] for r in runs), 2) if runs else None,
                'mean_abs_error_rh': round(statistics.mean(abs(r['error']) for r in runs), 2) if runs else None,
                'last': runs[-5:]}
