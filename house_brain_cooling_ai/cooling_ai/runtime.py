"""Cooling AI cycles. Each minute: read the listed states, plan, score, keep the 5-minute row,
freeze/score forecasts, and read at most one Systems AI archive day. Each hour: relearn.

Nothing here writes to Home Assistant. A kill-switch hold stops collection, learning and scoring;
it is shown as HELD, never as a problem unless the switch itself is missing, stale or unknown.
"""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import resource
import time

from . import __version__
from .entities import ALL_NUMBERS, ALL_TEXTS, ARCHIVE_KEYS, CONTEXT, MODES, actual_mode, number, token
from .learn import LEARN_KEYS, Boost, Forecasts, Grid, Minutes, Overnight, archive_rows, learn
from .plan import ZONE, Scorecard, compare, local_day, plan, precool_value, target_of
from .store import Store
from .transport import TransportError

FAST_S, LEARN_S = 60, 3600
ARCHIVE_TRY_S = 3600
ARCHIVE_FINAL_DAYS = 41          # the recorder has purged it: what Systems AI kept is all there will be
ARCHIVE_MAX_TRIES = 6
TERMINAL = frozenset({'DONE', 'FINAL', 'UNAVAILABLE', 'GAVE_UP'})
KEEP_DAYS = 120
MANIFEST = Path(__file__).resolve().parents[1] / 'SOURCE_MANIFEST.sha256'
REQUIRED = ('t_in', 'relay', 'ac', 'decision')
ARCHIVE_ENTITIES = tuple(ARCHIVE_KEYS)


def manifest_sha256():
    try:
        return hashlib.sha256(MANIFEST.read_bytes()).hexdigest()
    except OSError:
        return '0' * 64


def reading(states):
    """One row of plant keys from the listed states; a gap stays None, never zero."""
    row = {}
    for key, entity in ALL_NUMBERS.items():
        row[key] = number((states.get(entity) or {}).get('state'))
    for key, entity in ALL_TEXTS.items():
        row[key] = token((states.get(entity) or {}).get('state'))
    return row


class Runtime:
    def __init__(self, data_dir, client, gate, max_mb=96):
        self.store = Store(Path(data_dir) / 'cooling_ai.sqlite', max_mb=max_mb)
        self.client, self.gate = client, gate
        self.grid = Grid(self.store)
        self.minutes = Minutes()
        self.scorecard = Scorecard(self.store)
        self.forecasts = Forecasts(self.store)
        self.overnight = Overnight(self.store)
        self.boost = Boost(self.store)
        self.archive = self.store.get('archive:days', {})
        self.learned = self.store.get('learned', None)
        self.sha = manifest_sha256()
        self.last = {'fast': 0., 'learn': 0.}
        self.cycle_at = self.good_at = None
        self.cycle_seconds = None
        self.hold = None
        self.inputs = {}
        self.ai = self.store.get('ai:last', {'mode': None})['mode']
        self.status = {'state': 'STARTING'}
        self.recent = []
        self.row = {}
        self.target, self.target_until, self.archive_wait, self.status_note = None, 0., 0., None

    # -- loop ---------------------------------------------------------------------------------
    def step(self, now):
        if now - self.last['fast'] < FAST_S:
            return
        self.last['fast'] = now
        started = time.monotonic()
        self.cycle_at = int(now)
        ok = False
        try:
            ok = self.fast(now)
        finally:
            self.cycle_seconds = time.monotonic() - started
            if ok:
                self.good_at = int(now)
            self.status = self.report(now)

    def fast(self, now):
        try:
            states = self.client.states()
            self.inputs['core_api'] = True
        except (OSError, ValueError, TransportError):
            self.inputs['core_api'] = False
            self.hold = None
            return False
        self.hold = self.gate(states, now)
        if self.hold is not None:
            return True
        row = self.row = reading(states)
        self.context = {k: token((states.get(e) or {}).get('state')) for k, e in CONTEXT.items()}
        self.inputs['plant'] = all(row.get(k) is not None for k in REQUIRED)
        models = (self.learned or {}).get('models', {})
        power = (self.learned or {}).get('power', {})
        ai, why = plan(row, models, self.ai)
        code = compare(row, ai, now)
        self.scorecard.add(row, ai, code, models, power, now)
        self.now = {'plan': ai, 'why': why, 'code': code, 'actual': actual_mode(row), 'target': target_of(row),
                    't_in': row.get('t_in'), 'phase': row.get('phase'), 'decision': row.get('decision')}
        self.ai = ai if ai in MODES else None
        self.store.put('ai:last', {'mode': self.ai})
        done = self.minutes.add(row, now)
        if done is not None:
            self.grid.put(done[0], done[1], 'LIVE')
            self.recent = (self.recent + [done])[-400:]
        if now - self.last['learn'] >= LEARN_S or self.learned is None:     # first cycle after a start too
            self.relearn(now)
            models = self.learned.get('models', {})
        self.forecasts.score(row, now)
        self.forecasts.freeze(row, models, now)
        self.boost.add(row, now)
        self.overnight.score(self.recent, now)
        self.overnight.freeze(row, self.recent, models, now)
        self.archive_step(now)
        return True

    # -- Systems AI archive: one day a minute while catching up -------------------------------
    def waiting(self, now):
        """Days not yet settled (oldest first), whether or not they are due again yet."""
        days = [local_day(now - n * 86400) for n in range(KEEP_DAYS - 1, 0, -1)]
        return [d for d in days if (self.archive.get(d) or {}).get('status') not in TERMINAL]

    def pending(self, now):
        return [d for d in self.waiting(now) if now >= (self.archive.get(d) or {}).get('next_try', 0)]

    def archive_step(self, now):
        if now < self.archive_wait:
            return          # no Systems AI with the route: asked again within the hour, not every minute
        due = self.pending(now)
        if not due:
            if not self.waiting(now):
                self.inputs['archive'] = 'IDLE'
            return
        day = due[0]
        rec = self.archive.get(day) or {'tries': 0}
        age_days = (now - datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=ZONE).timestamp()) / 86400
        if self.target is None or now >= self.target_until:
            try:
                self.target, self.target_until = self.client.systems_ai(), now + ARCHIVE_TRY_S
            except (OSError, ValueError, TransportError):
                self.target = None       # finding Systems AI failed: not this day's fault, never counted against it
        if self.target is None:
            self.inputs['archive'] = False
            self.archive_wait = now + ARCHIVE_TRY_S
            self.status_note = 'NO_SYSTEMS_AI'
            return
        try:
            doc = self.client.archive_day(self.target, day)
        except TransportError as error:
            if str(error) == 'ARCHIVE_ROUTE_MISSING':
                self.inputs['archive'] = False
                self.archive_wait = now + ARCHIVE_TRY_S
                self.status_note = 'SYSTEMS_AI_BEFORE_0_1_2'
                return
            self._failed(day, rec, now, str(error))
            return
        except (OSError, ValueError):
            self._failed(day, rec, now, 'ARCHIVE_UNREADABLE')
            return
        if not isinstance(doc, dict):
            self._failed(day, rec, now, 'ARCHIVE_UNREADABLE')
            return
        self.inputs['archive'] = True
        self.status_note = None
        rec['next_try'] = now + ARCHIVE_TRY_S
        self.archive[day] = rec
        # Only the entities Cooling AI reads decide whether a day is complete.
        states = [(doc['status'].get(e) or {}).get('status') if isinstance(doc['status'].get(e), dict) else None
                  for e in ARCHIVE_ENTITIES]
        complete = all(st in ('DONE', 'EMPTY') for st in states)
        nothing = all(st in ('NOT_READ', None) for st in states)
        if nothing:
            rec['status'] = 'UNAVAILABLE' if age_days > ARCHIVE_FINAL_DAYS else 'NOT_KEPT_YET'
        elif complete or age_days > ARCHIVE_FINAL_DAYS:
            rows = archive_rows(doc)
            self.grid.put_many(rows, 'ARCHIVE')
            rec.update(status='DONE' if complete else 'FINAL', buckets=len(rows))
        else:
            rec['status'] = 'PARTIAL_WAITING'
        self.store.put('archive:days', self.archive)

    def _failed(self, day, rec, now, reason):
        """A read that failed is tried again within the hour, at most ARCHIVE_MAX_TRIES times; never filled in."""
        self.inputs['archive'] = False
        self.archive[day] = rec
        rec['tries'] = rec.get('tries', 0) + 1
        rec.update(next_try=now + ARCHIVE_TRY_S, reason=reason,
                   status='GAVE_UP' if rec['tries'] >= ARCHIVE_MAX_TRIES else 'FAILED')
        self.store.put('archive:days', self.archive)

    # -- hourly ---------------------------------------------------------------------------------
    def relearn(self, now):
        self.last['learn'] = now
        self.grid.prune(now)
        keep = {local_day(now - n * 86400) for n in range(KEEP_DAYS)}
        self.archive = {d: r for d, r in self.archive.items() if d in keep}
        rows = self.grid.rows(now - KEEP_DAYS * 86400, LEARN_KEYS)
        self.recent = [r for r in rows if r[0] >= now - 26 * 3600]
        learned = learn(rows, self.sha)
        by_day = {}
        for t, r in rows:
            by_day.setdefault(local_day(t), []).append(r)
        learned['precool'] = {d: precool_value(rs, learned['models'], learned['power'], learned['prices'])
                              for d, rs in sorted(by_day.items())[-14:]}
        self.episodes = learned.pop('episodes')
        self.learned = learned
        self.store.put('learned', learned)
        self.store.prune(now)

    # -- status for the page ----------------------------------------------------------------------
    def report(self, now):
        try:
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
            db = self.store.path.stat().st_size
        except OSError:
            peak, db = 0, None
        learned = self.learned or {}
        waiting = len(self.waiting(now)) if self.hold is None else None
        return {'state': 'HELD' if self.hold else 'SHADOW', 'hold_reason': self.hold, 'archive_note': self.status_note,
                'version': __version__,
                'now': getattr(self, 'now', None), 'context': getattr(self, 'context', {}),
                'models': learned.get('models'), 'ac_response': learned.get('ac_response'),
                'epsilon': learned.get('epsilon'), 'power_kw': learned.get('power'),
                'prices': learned.get('prices'), 'precool': learned.get('precool'),
                'scorecard': self.scorecard.report(now), 'forecast_1h': self.forecasts.report(),
                'forecast_overnight': self.overnight.report(), 'humid_boost': self.boost.report(),
                'archive': {'days_waiting': waiting,
                            'days': {s: sum(r.get('status') == s for r in self.archive.values())
                                     for s in sorted({r.get('status') for r in self.archive.values()} - {None})},
                            'grid_rows': self.grid.count()},
                'episodes': {'count': len(getattr(self, 'episodes', []) or [])},
                'resources': {'peak_rss_mb': round(peak, 1), 'db_bytes': db},
                'physical_actions': False, 'control_authority': 'NONE', 'executed': False}

    def episodes_doc(self, limit=20):
        return {'schema_version': 'house_brain_cooling_ai_episodes.v1',
                'episodes': (getattr(self, 'episodes', []) or [])[-limit:], 'physical_actions': False}

    def close(self):
        last = self.minutes.close()
        if last is not None:
            self.grid.put(last[0], last[1], 'LIVE')
        self.store.close()


def encode(doc):
    return json.dumps(doc, allow_nan=False)
