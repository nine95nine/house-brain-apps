"""House Health AI cycles. Each minute: read the listed states, keep the change points of the listed series,
class the outage state (NOA 1.0 port plus the power roles) and read at most one recorder series-day. Every
5 minutes: relearn the room baselines, the mold index, UPS ageing, the replacement calendar and idle-vs-stale,
then update the shadow scorecard. Nothing here writes to Home Assistant.

A kill-switch hold stops collection, backfill and learning; it is shown as HELD, never as a problem
unless the switch itself is missing, stale or unknown.
"""
import resource
import sqlite3
import time
from pathlib import Path

from . import __version__
from . import devices, mold, outage, replacements, rooms, ups
from .entities import (CONTEXT, LEARNING_BASELINE, LEARNING_MEAN, LEARNING_PERIOD, MOLD, NETWORK, NOA, PM, ROOMS,
                       UPS, changed_at, number, token)
from .scorecard import Scorecard
from .series import Series
from .store import Store
from .transport import TransportError

FAST_S, LEARN_S, PRUNE_S = 60, 300, 3600
UPS_WINDOW_S = 45 * 86400
RESERVED = {
    'heat_safety': 'RESERVED: needs Cooling AI time constant over an internal read-only route (decision D5)',
    'per_room_mold': 'RESERVED: needs per-room humidity sensors',
    'leak_lifecycle': 'RESERVED: waits on the #124 leak sensors; detection stays Water AI\'s',
    'orbi_moca': 'RESERVED: waits on an owner measurement packet (#216, #131)',
    'aquarium_fans_co2': 'RESERVED: aquarium (#79, #80, #149), ceiling fans (#135), CO2 (#125)',
}
NOT_HERE = {'always_on_creep': 'Energy AI owns it (decision D1)', 'low_battery_alerts': 'Maintenance owns them',
            'solar_health': 'Energy AI', 'leak_detection': 'Water AI', 'app_health': 'Systems AI'}


class Runtime:
    def __init__(self, data_dir, client, gate, max_mb=64):
        self.store = Store(Path(data_dir) / 'house_health_ai.sqlite', max_mb=max_mb)
        self.client, self.gate = client, gate
        self.series = Series(self.store)
        self.score = Scorecard(self.store)
        self.memory = self.store.get('memory', {'drift': {}, 'mold': None, 'episode': None})
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
        self.now_view = self.view(states, now)
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

    def view(self, states, now):
        """Right now: the outage class each minute (and its episode), and which inputs are readable."""
        val = lambda e: (states.get(e) or {}).get('state')
        elevation = ((states.get(NETWORK['sun']) or {}).get('attributes') or {}).get('elevation')
        raw = {k: val(e) for k, e in NETWORK.items() if k != 'sun'}
        raw['elevation'] = elevation
        out = outage.classify(raw)
        live_noa = val(NOA['class'])
        self.score.noa_port(out['noa_port']['class'], live_noa)
        episode, done = outage.episode_step(self.memory.get('episode'), now, out['class'], out['detail'], live_noa)
        self.memory['episode'] = episode
        if done:
            self.score.episode(done)
        self.store.put('memory', self.memory)
        self.score.save()
        self.inputs['rooms'] = any(number(val(e)) is not None for e in ROOMS.values())
        self.inputs['mold'] = all(number(val(e)) is not None for e in MOLD.values())
        self.inputs['outage'] = val(NETWORK['ups_status']) not in (None, 'unknown', 'unavailable') or \
            val(NETWORK['wan_ping']) in ('on', 'off')
        self.inputs['ups'] = number(val(UPS['charge']), 0, 100) is not None
        return {'outage': {**out, 'live_noa_class': token(live_noa), 'episode_open': episode is not None},
                'arbiter_phase': token(val(CONTEXT['arbiter_phase'])),
                'outdoor_temp': number(val(CONTEXT['outdoor_temp'])),
                'learning_period': token(val(LEARNING_PERIOD))}

    def learn(self, now, states):
        self.last['learn'] = now
        val = lambda e: (states.get(e) or {}).get('state')
        phase = token(val(CONTEXT['arbiter_phase']))
        since7 = now - rooms.WINDOW_S
        views, live_baselines = {}, {}
        for room, entity in ROOMS.items():
            v = rooms.room_view(room, self.series.points(entity, since7), number(val(entity)), now,
                                self.memory['drift'].get(room), phase)
            self.memory['drift'][room] = v['drift']
            v['learning_means'] = {slug: number(val(LEARNING_MEAN[(room, slug)])) for slug in v['periods']}
            v['idle_vs_stale'] = devices.judge(val(entity), changed_at(states.get(entity)),
                                               self.series.points(entity, now - devices.WINDOW_S), now)
            views[room] = v
            live_baselines[room] = number(val(LEARNING_BASELINE[room]))
        since30 = now - mold.WINDOW_S
        mold_view = mold.index(self.series.points(MOLD['rh'], since30), self.series.points(MOLD['dew_point'], since30),
                               now, self.memory.get('mold'))
        self.memory['mold'] = mold_view
        for key, entity in MOLD.items():
            mold_view[key]['idle_vs_stale'] = devices.judge(val(entity), changed_at(states.get(entity)),
                                                            self.series.points(entity, now - devices.WINDOW_S), now)
        since_ups = now - UPS_WINDOW_S
        evs = ups.events(self.series.points(UPS['status'], since_ups), self.series.points(UPS['charge'], since_ups),
                         self.series.points(UPS['load'], since_ups), self.series.points(UPS['runtime_min'], since_ups))
        ups_view = ups.assess(evs, ups.live_values(states, UPS))
        calendar = replacements.calendar(states, now, ups_view['band'])
        parity = replacements.pm_parity(calendar, {k: val(e) for k, e in PM.items()})
        self.score.rooms(now, views, live_baselines)
        self.score.pm(now, parity)
        self.score.ups(now, ups_view, ups.predicted_full_min(ups_view))
        self.score.save()
        self.store.put('memory', self.memory)
        self.learned = {'rooms': views, 'mold': mold_view, 'ups': ups_view, 'calendar': calendar,
                        'pm_parity': parity}

    def report(self, now):
        try:
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
            db = self.store.path.stat().st_size
        except OSError:
            peak, db = 0, None
        rooms_out = {r: {k: v for k, v in d.items() if k != 'periods'} | {'periods': {
            s: p['mean'] for s, p in d['periods'].items()}} for r, d in (self.learned.get('rooms') or {}).items()}
        return {'state': 'HELD' if self.hold else 'SHADOW', 'hold_reason': self.hold, 'version': __version__,
                'now': self.now_view, 'rooms': rooms_out or None, 'mold': self.learned.get('mold'),
                'ups': self.learned.get('ups'), 'calendar': self.learned.get('calendar'),
                'pm_parity': self.learned.get('pm_parity'), 'scorecard': self.score.report(now),
                'reserved': RESERVED, 'not_here': NOT_HERE,
                'history': {**self.series.report(now), 'last_read': self.backfill_last},
                'resources': {'peak_rss_mb': round(peak, 1), 'db_bytes': db},
                'physical_actions': False, 'control_authority': 'NONE', 'executed': False}

    def close(self):
        self.store.close()
