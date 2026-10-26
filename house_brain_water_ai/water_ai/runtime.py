"""Water AI cycles. Each minute: read the listed states, keep the RO minute and the change points,
and read at most one recorder entity-day. Every 5 minutes: relearn the RO episodes and the yard ledger,
then freeze and score forecasts. Nothing here writes to Home Assistant.

A kill-switch hold stops collection, backfill and learning; it is shown as HELD, never as a problem
unless the switch itself is missing, stale or unknown.
"""
import resource
import sqlite3
import time
from pathlib import Path

from . import __version__
from .entities import CONTEXT, OFFLINE_STEMS, R7_IDS, RO_IDS, ZONES, live, number, token, zone_ids
from .forecasts import Forecasts
from .ro import summary as ro_summary
from .series import Series
from .store import Store
from .transport import TransportError
from .yard import ledger

FAST_S, LEARN_S, PRUNE_S = 60, 300, 3600
RO_WINDOW_S = 14 * 86400
YARD_WINDOW_S = 30 * 86400


class Runtime:
    def __init__(self, data_dir, client, gate, max_mb=64):
        self.store = Store(Path(data_dir) / 'water_ai.sqlite', max_mb=max_mb)
        self.client, self.gate = client, gate
        self.series = Series(self.store)
        self.forecasts = Forecasts(self.store)
        self.last = {'fast': 0., 'learn': 0., 'prune': 0.}
        self.cycle_at = self.good_at = None
        self.cycle_seconds = None
        self.hold = None
        self.inputs = {}
        self.ro = self.yard = None
        self.now_view = {}
        self.backfill_last = None
        self.status = {'state': 'STARTING'}

    def step(self, now):
        # Once per clock minute: a loop that drifts (60.5 s cycles) never skips a minute's pressure.
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
        self.inputs['ro'] = self.now_view['ro']['ok']
        self.inputs['yard'] = any(z['live'] for z in self.now_view['zones'].values())
        try:
            self.backfill_last = self.series.backfill_step(self.read_history, now)
        except (OSError, ValueError):
            self.backfill_last = 'FAILED'
        self.inputs['history'] = {'IDLE': 'IDLE', 'DONE': True, 'EMPTY': True}.get(self.backfill_last, False)
        if now - self.last['learn'] >= LEARN_S:
            self.learn(now, self.now_view)
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
        """Right now, in plain values. The R7 probes are shown but not commissioned."""
        val = lambda e: (states.get(e) or {}).get('state')
        psi = number(val(RO_IDS['psi']), -5, 150)
        ro = {'psi': psi, 'trend_psi_h': number(val(RO_IDS['trend'])), 'signal': token(val(RO_IDS['signal'])),
              'calibration': token(val(RO_IDS['calibration'])), 'online': token(val(RO_IDS['online'])),
              'plausible': token(val(RO_IDS['plausible']))}
        ro['ok'] = psi is not None and ro['online'] == 'on' and ro['plausible'] == 'on'
        zones = {}
        for zone, stem, total in ZONES:
            ids = zone_ids(stem)
            used, valve = live(states, ids['valve'])
            zones[zone] = {'live': used is not None, 'valve': token(valve), 'spelling': '_2' if used and used.endswith('_2') else
                           'original' if used else None,
                           'battery': number(live(states, ids['battery'])[1]), 'fault': token(live(states, ids['fault'])[1]),
                           'rain_delay': token(live(states, ids['rain_delay'])[1]),
                           'next': token(live(states, ids['next'])[1]), 'gallons_total': number(val(total))}
        offline_live = [s for s in OFFLINE_STEMS if any(live(states, p)[0] for p in zone_ids(s).values())]
        context = {k: (number(val(e)) if k in ('budget_used', 'lowest_battery', 'low_battery_threshold', 'garden_temp',
                                             'garden_rh', 'garden_vpd') else token(val(e))) for k, e in CONTEXT.items()}
        r7 = {k: token(val(e)) for k, e in R7_IDS.items()}
        return {'ro': ro, 'zones': zones, 'context': context, 'offline_timers_live': offline_live,
                'slots': {'leak': 'NOT_CONNECTED', 'et0_rain': 'NOT_CONNECTED', 'indoor_plants': 'NOT_CONNECTED',
                          'r7_probes': {'status': 'NOT_COMMISSIONED', 'live': r7,
                                        'note': 'A disconnected leak lead reads dry: off is not proof of DRY.'},
                          'offline_timers': {'kept_offline': len(OFFLINE_STEMS), 'now_live': len(offline_live)}}}

    def learn(self, now, view):
        self.last['learn'] = now
        self.ro = ro_summary(self.series.psi(now - RO_WINDOW_S), now)
        self.yard = ledger(self.series, view.get('context', {}).get('low_battery_threshold'), now, now - YARD_WINDOW_S)
        self.forecasts.score(self.ro, self.yard, now)
        self.forecasts.freeze(self.ro, self.yard, now)

    def report(self, now):
        try:
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
            db = self.store.path.stat().st_size
        except OSError:
            peak, db = 0, None
        ro = dict(self.ro or {})
        ro.pop('episodes', None)
        if self.ro:
            ro['last_refills'] = self.ro['episodes']['refills'][-5:]
            ro['last_draws'] = self.ro['episodes']['draws'][-5:]
        yard = {z: {k: v for k, v in d.items() if k != 'runs'} for z, d in (self.yard or {}).items()}
        return {'state': 'HELD' if self.hold else 'SHADOW', 'hold_reason': self.hold, 'version': __version__,
                'now': self.now_view, 'ro': ro or None, 'yard': yard or None, 'forecasts': self.forecasts.report(),
                'history': {**self.series.report(now), 'last_read': self.backfill_last},
                'resources': {'peak_rss_mb': round(peak, 1), 'db_bytes': db},
                'physical_actions': False, 'control_authority': 'NONE', 'executed': False}

    def close(self):
        self.store.close()
