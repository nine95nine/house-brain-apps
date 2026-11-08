"""The shadow scorecard (design R1 §5). Counted, never executed: a met bar only makes a YAML retirement
eligible for its own owner pop-up; a failed bar keeps the YAML package live.

| Measure | Against | Bar |
| room baseline mean per period | Learning 1.4-R5 (the current period's `_baseline`) | median difference <= 0.2 °F over 14 days |
| outage class | NOA 1.0 `sensor.house_brain_network_outage_class` | same class in every natural outage (>= 3), no POWER called ISP |
| due dates | Predictive Maintenance 2.3 | same item and status, or a named reason |
| UPS retention | the next natural outage's measured runtime | within ±15 % |
"""
from statistics import median

from .outage import scorecard as outage_scorecard
from .series import local, local_day
from .ups import PREDICTION_TOLERANCE

ROOM_BAR_F = 0.2
ROOM_DAYS = 14
KEEP_DAYS = 21
KEEP_EPISODES = 200


class Scorecard:
    def __init__(self, store):
        self.store = store
        self.book = store.get('scorecard', {'rooms': {}, 'episodes': [], 'pm': {}, 'ups': {'prediction': None,
                                                                                           'scored': []},
                                            'noa_port': {'same': 0, 'different': 0, 'last_different': []}})

    def save(self):
        self.store.put('scorecard', self.book)

    # -- rooms ------------------------------------------------------------------------------------
    def rooms(self, now, views, live_baselines):
        """At most one sample per room per local hour: |ours - Learning's current-period baseline|."""
        day, hour = local_day(now), local(now).hour
        rows = self.book['rooms'].setdefault(day, {})
        for room, view in views.items():
            theirs = live_baselines.get(room)
            key = f'{room}:{hour}'
            if key in rows or view.get('baseline') is None or theirs is None:
                continue
            rows[key] = round(abs(view['baseline'] - theirs), 3)
        for old in sorted(self.book['rooms'])[:-KEEP_DAYS]:
            self.book['rooms'].pop(old)

    def rooms_report(self, now):
        days = sorted(d for d in self.book['rooms'] if d <= local_day(now))[-ROOM_DAYS:]
        diffs = [v for d in days for v in self.book['rooms'][d].values()]
        covered = sum(1 for d in days if self.book['rooms'][d])
        if not diffs:
            return {'samples': 0, 'days': 0, 'median_diff_f': None, 'status': 'WAITING', 'bar_f': ROOM_BAR_F}
        mid = round(median(diffs), 3)
        status = ('MET' if mid <= ROOM_BAR_F else 'NOT_MET') if covered >= ROOM_DAYS else 'WAITING'
        return {'samples': len(diffs), 'days': covered, 'median_diff_f': mid, 'status': status, 'bar_f': ROOM_BAR_F}

    # -- outage ------------------------------------------------------------------------------------
    def noa_port(self, ours, live):
        """Minute by minute: the exact NOA 1.0 port against the live NOA sensor (proves the port)."""
        if not isinstance(live, str) or live in ('unknown', 'unavailable', ''):
            return
        port = self.book['noa_port']
        if ours == live:
            port['same'] += 1
        else:
            port['different'] += 1
            port['last_different'] = (port['last_different'] + [{'ours': ours, 'live': live[:40]}])[-5:]

    def episode(self, record):
        self.book['episodes'] = (self.book['episodes'] + [record])[-KEEP_EPISODES:]

    # -- due dates ---------------------------------------------------------------------------------
    def pm(self, now, parity):
        self.book['pm'][local_day(now)] = parity
        for old in sorted(self.book['pm'])[:-KEEP_DAYS]:
            self.book['pm'].pop(old)

    def pm_report(self):
        days = list(self.book['pm'].values())
        results = [r['result'] for d in days for r in d.values()]
        unexplained = sum(r == 'UNEXPLAINED' for r in results)
        compared = sum(r != 'PM_MISSING' for r in results)
        return {'days': len(days), 'compared': compared, 'same': sum(r == 'SAME' for r in results),
                'named': sum(r not in ('SAME', 'UNEXPLAINED', 'PM_MISSING') for r in results),
                'unexplained': unexplained,
                'status': 'WAITING' if not compared else 'NOT_MET' if unexplained else 'MET'}

    # -- UPS ---------------------------------------------------------------------------------------
    def ups(self, now, view, predicted):
        """Freeze a prediction before the next natural event; score it on the first measured event after it."""
        book = self.book['ups']
        pred = book['prediction']
        if pred is not None:
            nxt = next((e for e in view.get('events', []) if e['start'] > pred['made_at']
                        and e.get('projected_full_min') is not None and e.get('status') == 'MEASURED'), None)
            if nxt is not None:
                error = abs(nxt['projected_full_min'] - pred['full_min']) / nxt['projected_full_min']
                book['scored'] = (book['scored'] + [{'event_start': nxt['start'], 'predicted': pred['full_min'],
                                                     'measured': nxt['projected_full_min'],
                                                     'within': error <= PREDICTION_TOLERANCE}])[-50:]
                pred = None
        if pred is None and not view.get('on_battery_now'):
            pred = {'made_at': now, 'full_min': predicted}
        book['prediction'] = pred

    def ups_report(self):
        scored = self.book['ups']['scored']
        if not scored:
            return {'scored': 0, 'status': 'WAITING', 'prediction': self.book['ups']['prediction']}
        return {'scored': len(scored), 'within': sum(s['within'] for s in scored), 'last': scored[-1],
                'status': 'MET' if scored[-1]['within'] else 'NOT_MET', 'prediction': self.book['ups']['prediction']}

    def report(self, now):
        port = self.book['noa_port']
        return {'rooms': self.rooms_report(now), 'outage': {**outage_scorecard(self.book['episodes']),
                                                              'last': self.book['episodes'][-5:]},
                'noa_port_minutes': {'same': port['same'], 'different': port['different'],
                                     'last_different': port['last_different']},
                'due_dates': self.pm_report(), 'ups': self.ups_report()}
