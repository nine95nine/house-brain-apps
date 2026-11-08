"""Pool AI cycles. Each minute: read the listed states, keep the change points of the listed series and
read at most one recorder series-day. Every 5 minutes: relearn chlorine demand from the logged tests, the
cell factor and dates, the salt runway, the pump runs and their cost, and the next best test day, then
update the ledgers that outlive the 45-day series. Nothing here writes to Home Assistant.

A kill-switch hold stops collection, backfill and learning; it is shown as HELD, never as a problem
unless the switch itself is missing, stale or unknown.
"""
import resource
import sqlite3
import time
from datetime import timedelta
from pathlib import Path

from . import __version__
from . import cell, demand, nextbest, pump, salt
from .entities import CELL, GAP, GENERATION, PUMP, SALT, TEMPERATURE, TESTS, number
from .series import KEEP_DAYS, Series, local, local_day
from .store import Store
from .transport import TransportError

FAST_S, LEARN_S, PRUNE_S = 60, 300, 3600
WINDOW_S = KEEP_DAYS * 86400
KEEP_TESTS, KEEP_PAIRS, KEEP_RUNS = 400, 400, 500
RESERVED = {
    'temperature_demand': 'RESERVED: needs a trusted pool water temperature (Ondilo _2 once calibrated, or Jandy '
                          'once its open check is closed)',
    'lsi_csi': 'RESERVED: needs TA, calcium hardness and TDS inputs (an owner YAML gate, D2) and a trusted water '
               'temperature',
    'ondilo_local': 'RESERVED: Ondilo local readings once the connectable proxy passes its soak and they are '
                    'decision-grade (#1455)',
    'pump_protections': 'RESERVED: #111 Phase 2/3 pump protections stay in their own lane; Pool AI only scores them',
}
NOT_HERE = {'fc_advice': 'Advisor 1.4-R2 (a manual test is the authority)', 'cleaning_reminder': 'Insight 1.3',
            'hidden_water_loss': 'the salt balance package', 'link_reliability': 'AquaRite health alerts 1.4',
            'app_health': 'Systems AI'}
BLOCKED_BY_TEMPERATURE = ('temperature_dependent_demand', 'lsi_csi')


def temperature_view(val):
    """Water temperature is MISSING in 0.1 (design R1 §2, D3), with the reasons; never a number."""
    reasons = ['ONDILO_CALIBRATION_NOT_COMPLETE' if val(TEMPERATURE['ondilo_calibrated']) != 'on'
               else 'ONDILO_NOT_DECISION_GRADE_IN_0_1', 'JANDY_POOL_TEMP_OPEN_OWNER_CHECK']
    pump_on = val(PUMP['switch']) == 'on'
    jandy = val(TEMPERATURE['jandy'])
    return {'status': 'MISSING', 'reasons': reasons,
            'jandy_now': ('UNKNOWN_WHILE_RUNNING' if pump_on else 'UNKNOWN') if jandy in GAP else 'REPORTING_NOT_USED',
            'blocked': {m: 'blocked: no trusted water temperature' for m in BLOCKED_BY_TEMPERATURE}}


def merge(ledger, fresh, key, cut, keep):
    """Rows older than the series window stay as recorded; rows inside it are recomputed."""
    rows = [r for r in ledger if r[key] < cut] + [r for r in fresh if r[key] >= cut]
    return sorted(rows, key=lambda r: r[key])[-keep:]


class Runtime:
    def __init__(self, data_dir, client, gate, max_mb=64):
        self.store = Store(Path(data_dir) / 'pool_ai.sqlite', max_mb=max_mb)
        self.client, self.gate = client, gate
        self.series = Series(self.store)
        self.last = {'fast': 0., 'learn': 0., 'prune': 0.}
        self.cycle_at = self.good_at = None
        self.cycle_seconds = None
        self.hold = None
        self.inputs = {}
        self.now_view = {}
        self.learned = {}
        self.backfill_last = None
        self.status = {'state': 'STARTING'}

    def step(self, now):
        # Once per clock minute: a loop that drifts never skips a minute.
        if now // FAST_S == self.last['fast'] // FAST_S:
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
        try:
            return self.collect(states, now)
        except sqlite3.Error:
            # The App's own database is full or broken: a failed cycle (shown as a problem), never a crash loop.
            self.inputs['store'] = False
            return False

    def collect(self, states, now):
        self.inputs['store'] = True
        self.series.add_live(states, now)
        self.now_view = self.view(states)
        try:
            self.backfill_last = self.series.backfill_step(self.read_history, now)
        except (OSError, ValueError):
            self.backfill_last = 'FAILED'
        self.inputs['history'] = {'IDLE': 'IDLE', 'DONE': True, 'EMPTY': True}.get(self.backfill_last, False)
        if now - self.last['learn'] >= LEARN_S:
            self.learn(now, states)
        if now - self.last['prune'] >= PRUNE_S:
            self.last['prune'] = now
            self.series.prune(now)
            self.store.prune(now)
        return True

    def read_history(self, entity, start, end):
        """One recorder read for the series backfill; None when it failed (never filled in)."""
        try:
            return self.client.history(entity, start, end)
        except (OSError, ValueError, TransportError):
            return None

    def view(self, states):
        """Right now: which inputs are readable, and why water temperature is missing."""
        val = lambda e: (states.get(e) or {}).get('state')
        self.inputs['tests'] = number(val(TESTS['fc']), *demand.FC_RANGE) is not None \
            and number(val(TESTS['cya']), *demand.CYA_RANGE) is not None
        self.inputs['generation'] = number(val(GENERATION['rate']), 0, 1) is not None \
            and number(val(GENERATION['hours']), 0, 1e6) is not None
        self.inputs['salt'] = number(val(SALT['last_known']), 500, 10000) is not None
        self.inputs['pump'] = val(PUMP['switch']) in ('on', 'off')
        self.inputs['sense'] = number(val(PUMP['sense_w']), 0, 200000) is not None
        return {'pump': val(PUMP['switch']) if val(PUMP['switch']) in ('on', 'off') else 'UNKNOWN',
                'spa_mode': val(GENERATION['spa']) if val(GENERATION['spa']) in ('on', 'off') else 'UNKNOWN',
                'salt_last_known_ppm': number(val(SALT['last_known']), 500, 10000),
                'on_peak': val(PUMP['on_peak']) if val(PUMP['on_peak']) in ('on', 'off') else 'UNKNOWN',
                'temperature': temperature_view(val)}

    def learn(self, now, states):
        self.last['learn'] = now
        val = lambda e: (states.get(e) or {}).get('state')
        cut = now - WINDOW_S + 86400          # the first day of the window is only partly backfilled
        pts = lambda e: self.series.points(e, cut)
        rate, spa = pts(GENERATION['rate']), pts(GENERATION['spa'])
        # Tests and pairs: recomputed inside the window, kept as recorded once older.
        fresh = [t for t in demand.tests(pts(TESTS['logged']), pts(TESTS['fc']), pts(TESTS['cya']), pts(TESTS['ph']))
                 if cut <= t['at'] <= now]
        test_rows = merge(self.store.get('ledger:tests', []), fresh, 'at', cut, KEEP_TESTS)
        pair_rows = merge(self.store.get('ledger:pairs', []),
                          [p for p in demand.pairs(test_rows, rate, spa) if p['start'] >= cut], 'start', cut, KEEP_PAIRS)
        demand_view = demand.view(test_rows, pair_rows, rate, spa, now)
        cell_view = {'factor': cell.factor_trend(pair_rows),
                     'dates': cell.dates(lambda k: val({**CELL, 'hours': GENERATION['hours']}[k]),
                                         self.series.points(GENERATION['hours'], now - cell.RATE_WINDOW_S), now)}
        # Salt: one row per complete local day.
        level, added, weather = pts(SALT['level']), pts(SALT['added_total_lb']), pts(SALT['weather'])
        first, today = local(cut).date() + timedelta(days=1), local(now).date()
        days = [(first + timedelta(days=n)).isoformat() for n in range((today - first).days)]
        salt_rows = merge(self.store.get('ledger:salt', []), [salt.daily(level, added, weather, d) for d in days],
                          'day', days[0] if days else local_day(now), salt.KEEP_DAYS)
        salt_view = salt.view(salt_rows, val(SALT['last_known']), now)
        # Pump runs over the window.
        runs = pump.assess(pts(PUMP['switch']), lambda a, b: self.series.between(PUMP['sense_w'], a, b),
                           pts(PUMP['on_peak']), now)
        run_rows = merge(self.store.get('ledger:runs', []), runs, 'start', cut, KEEP_RUNS)
        pump_view = pump.view(run_rows)
        best = nextbest.view(test_rows, pair_rows, rate, spa, now)
        self.store.put('ledger:tests', test_rows)
        self.store.put('ledger:pairs', pair_rows)
        self.store.put('ledger:salt', salt_rows)
        self.store.put('ledger:runs', run_rows)
        self.learned = {'demand': demand_view, 'cell': cell_view, 'salt': salt_view, 'pump': pump_view,
                        'next_test': best}

    def report(self, now):
        try:
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
            db = self.store.path.stat().st_size
        except OSError:
            peak, db = 0, None
        d = self.learned.get('demand') or {}
        return {'state': 'HELD' if self.hold else 'SHADOW', 'hold_reason': self.hold, 'version': __version__,
                'now': self.now_view, 'demand': d or None, 'cell': self.learned.get('cell'),
                'salt': self.learned.get('salt'), 'pump': self.learned.get('pump'),
                'next_test': self.learned.get('next_test'),
                'scorecard': {'fc_forecast': d.get('scorecard'), 'salt_trend': (self.learned.get('salt') or {}).get('scorecard'),
                              'pump_kwh': (self.learned.get('pump') or {}).get('scorecard')},
                'reserved': RESERVED, 'not_here': NOT_HERE,
                'history': {**self.series.report(now), 'last_read': self.backfill_last},
                'resources': {'peak_rss_mb': round(peak, 1), 'db_bytes': db},
                'physical_actions': False, 'control_authority': 'NONE', 'executed': False}

    def close(self):
        self.store.close()
