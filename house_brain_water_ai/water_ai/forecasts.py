"""Frozen forecasts, scored later beside a simple baseline (design R1 §5). Never executed.

- RO time-to-full after a draw, against the last refill's duration;
- next-run Orbit gallons per zone, against the last run's gallons (only runs Orbit reported);
- battery level 14 days ahead per zone, against "no change".
A kind is called a forecast only after it beat its baseline on at least 5 scored days.
"""
import statistics
from datetime import datetime

from .ro import ZONE

MIN_SCORED_DAYS = 5
BETTER = 0.95
KEEP = 400
FRESH_DRAW_S = 1800          # only draws that just ended are forecast (not backfilled ones)
DRAW_SETTLED_S = 600         # ... and only once the draw has stopped, so one draw is one forecast
BATTERY_AHEAD_S = 14 * 86400


class Forecasts:
    def __init__(self, store):
        self.store = store
        self.book = store.get('forecasts', [])

    def _add(self, row):
        if any(f['kind'] == row['kind'] and f['key'] == row['key'] for f in self.book):
            return
        self.book = (self.book + [{**row, 'actual': None}])[-KEEP:]

    def freeze(self, ro, yard, now):
        eps = ro['episodes']
        learned = ro['learned']
        if eps['draws'] and eps['refills'] and learned.get('rise_psi_h_median') and learned.get('plateau_psi_median'):
            draw = eps['draws'][-1]
            if DRAW_SETTLED_S <= now - draw['end'] <= FRESH_DRAW_S:
                last = eps['refills'][-1]
                need = max(0., learned['plateau_psi_median'] - draw['to_psi'])
                self._add({'kind': 'ro_time_to_full', 'key': f"draw:{int(draw['start'])}", 'made_at': now,
                           'start': draw['end'], 'model': round(need / learned['rise_psi_h_median'] * 60, 1),
                           'baseline': last['minutes']})
        for zone, z in yard.items():
            done = [r for r in z['runs'] if r.get('gallons') and r.get('settled')]
            # Only a true forecast: frozen before the run it forecasts has started.
            if len(done) >= 3 and not any(r['start'] > done[-1]['start'] for r in z['runs']):
                last = done[-1]
                self._add({'kind': 'next_run_gallons', 'key': f"{zone}:{int(last['start'])}", 'made_at': now,
                           'zone': zone, 'after': last['start'],
                           'model': round(statistics.median(r['gallons'] for r in done[-5:]), 2),
                           'baseline': last['gallons']})
            b = z['battery']
            week = int(now // (7 * 86400))
            if b.get('status') == 'ESTIMATED' and b.get('per_week') is not None:
                self._add({'kind': 'battery_14d', 'key': f'{zone}:{week}', 'made_at': now, 'zone': zone,
                           'due_at': now + BATTERY_AHEAD_S,
                           'model': round(max(0., min(100., b['last'] + b['per_week'] * 2)), 1), 'baseline': b['last']})
        self.store.put('forecasts', self.book)

    def score(self, ro, yard, now):
        eps = ro['episodes']
        for f in self.book:
            if f['actual'] is not None:
                continue
            if f['kind'] == 'ro_time_to_full':
                refill = next((r for r in eps['refills'] if r['start'] >= f['start'] - 600), None)
                # A refill still running has no end yet: it is scored once it settled (a plateau) or something followed.
                ended = refill is not None and (refill['plateau_psi'] is not None or any(
                    e['start'] > refill['end'] for e in eps['refills'] + eps['draws']))
                if ended:
                    # Another draw before the tank was full again: not a test of the forecast.
                    interrupted = any(f['start'] < d['start'] < refill['end'] for d in eps['draws'])
                    f['actual'] = 'INTERRUPTED' if interrupted else round((refill['end'] - f['start']) / 60, 1)
                elif now - f['start'] > 12 * 3600:
                    f['actual'] = 'NO_REFILL_SEEN'
            elif f['kind'] == 'next_run_gallons':
                runs = (yard.get(f['zone']) or {}).get('runs', [])
                nxt = next((r for r in runs if r['start'] > f['after'] and r.get('end')), None)
                if nxt is not None and nxt.get('settled'):
                    f['actual'] = nxt['gallons'] if nxt.get('gallons') else 'NOT_REPORTED'
            elif f['kind'] == 'battery_14d' and now >= f['due_at']:
                last = ((yard.get(f['zone']) or {}).get('battery') or {}).get('last')
                f['actual'] = last if isinstance(last, (int, float)) else 'MISSING'
        self.store.put('forecasts', self.book)

    def report(self):
        out = {}
        for kind in ('ro_time_to_full', 'next_run_gallons', 'battery_14d'):
            rows = [f for f in self.book if f['kind'] == kind and isinstance(f['actual'], (int, float))]
            pending = sum(1 for f in self.book if f['kind'] == kind and f['actual'] is None)
            if not rows:
                out[kind] = {'scored': 0, 'pending': pending, 'status': 'WAITING'}
                continue
            days = {datetime.fromtimestamp(f['made_at'], ZONE).strftime('%Y-%m-%d') for f in rows}
            model = statistics.mean(abs(f['model'] - f['actual']) for f in rows)
            base = statistics.mean(abs(f['baseline'] - f['actual']) for f in rows)
            proven = len(days) >= MIN_SCORED_DAYS and model <= base * BETTER
            out[kind] = {'scored': len(rows), 'scored_days': len(days), 'pending': pending,
                         'model_mae': round(model, 3), 'baseline_mae': round(base, 3), 'proven': proven,
                         'status': 'PROVEN' if proven else 'NOT_YET_BETTER' if len(days) >= MIN_SCORED_DAYS else 'WAITING'}
        return out
