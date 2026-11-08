"""UPS ageing (design R1 §4.4; absorbs the offline #81 survival / retention / ageing models).

Runtime retention is learned from NATURAL on-battery events only: the App never causes one, and a pulled
plug is never asked for. An event is the NUT status holding `OB` until it returns to `OL`:
- shorter than MIN_EVENT_S: SHORT (it may be the UPS's own self-test), never used;
- ending in a gap (the status went unknown): END_UNKNOWN, never used.
For a complete event that started near full charge (>= 95 %, Predictive Maintenance 2.3's rule) at a low
load (<= 15 %), drained at least MIN_DROP_PP, the full runtime it implies is minutes / (charge drop / 100)
(a linear-drain inference, labelled PROVISIONAL). Retention compares that with the new battery's baseline,
160 min at 4 % load (`docs/validation/UPS_BATTERY_LOOKUP_AND_81_OWNER_INPUTS_2026-10-03.md`), only when the
event's load is within MAX_LOAD_DELTA_PP of 4 %; otherwise the event is NOT_COMPARABLE.
Bands (PROVISIONAL, IEEE 1188 by excerpt): HEALTHY at 80 % or more, WATCH below. No REPLACE band: the
50 % line has no replace-at standard behind it.

Floors (owner decision D2, 2026-11-08): the ALARM floor is 45 min (the live `ups_power` alert) and the
PLANNING floor is 60 min (the weekly digest and v2.3). The live runtime estimate is judged against them
only near full charge and at low load; otherwise NOT_JUDGED. House Health raises no alert of its own.

The battery's due date is the replacement date plus the interval (1,095 days); measured retention can only
bring it earlier (a WATCH retention marks it SOON), never later. Pure functions; no network.
"""
from statistics import median

from .entities import number

BASELINE_MIN = 160.
BASELINE_LOAD = 4.
MAX_LOAD_DELTA_PP = 5.
FULL_CHARGE = 95.
LOW_LOAD = 15.
MIN_DROP_PP = 10.
MIN_EVENT_S = 120
HEALTHY_PCT = 80.
ALARM_FLOOR_MIN = 45.
PLANNING_FLOOR_MIN = 60.
PREDICTION_TOLERANCE = 0.15


def flag(word):
    """True for on battery, False for on utility, None for a gap or no flag."""
    if not isinstance(word, str):
        return None
    flags = word.split()
    return True if 'OB' in flags else False if 'OL' in flags else None


def held(points, at):
    """The numeric value held at time `at` from change points [(t, value, word)], or None."""
    value = None
    for t, v, _ in points:
        if t > at:
            break
        value = v
    return value


def events(status_points, charge_points, load_points, runtime_points):
    """Natural on-battery events from the stored change points (each [(t, value, word)] sorted by time)."""
    out, start = [], None
    for t, _, word in status_points:
        on = flag(word)
        if start is None and on is True:
            start = t
        elif start is not None and on is not True:
            out.append(_event(start, t, 'COMPLETE' if on is False else 'END_UNKNOWN',
                              charge_points, load_points, runtime_points))
            start = None
    if start is not None:
        out.append({'start': start, 'end': None, 'status': 'ONGOING'})
    return out


def _event(start, end, status, charge_points, load_points, runtime_points):
    minutes = round((end - start) / 60, 1)
    if status == 'COMPLETE' and end - start < MIN_EVENT_S:
        status = 'SHORT'
    charges = [v for t, v, _ in charge_points if start <= t <= end and v is not None]
    loads = [v for t, v, _ in load_points if start <= t <= end and v is not None]
    start_charge = held(charge_points, start)
    start_load = held(load_points, start)
    low = min([c for c in charges] + ([start_charge] if start_charge is not None else []), default=None)
    load = median(loads + ([start_load] if start_load is not None else [])) if (loads or start_load is not None) else None
    return {'start': start, 'end': end, 'minutes': minutes, 'status': status, 'start_charge': start_charge,
            'end_charge': low, 'load': load, 'runtime_estimate_at_start': held(runtime_points, start)}


def retention(event):
    """{'status', 'projected_full_min', 'retention_pct'} for one event; only a complete, comparable event
    yields a retention."""
    if event.get('status') != 'COMPLETE':
        return {'status': event.get('status') or 'INVALID', 'projected_full_min': None, 'retention_pct': None}
    c0, c1, load = event['start_charge'], event['end_charge'], event['load']
    if c0 is None or c1 is None or load is None:
        return {'status': 'MISSING_CHARGE_OR_LOAD', 'projected_full_min': None, 'retention_pct': None}
    if c0 < FULL_CHARGE:
        return {'status': 'NOT_FROM_FULL_CHARGE', 'projected_full_min': None, 'retention_pct': None}
    if load > LOW_LOAD:
        return {'status': 'LOAD_TOO_HIGH', 'projected_full_min': None, 'retention_pct': None}
    drop = c0 - c1
    if drop < MIN_DROP_PP:
        return {'status': 'TOO_SHALLOW', 'projected_full_min': None, 'retention_pct': None}
    projected = round(event['minutes'] / (drop / 100.), 1)
    if abs(load - BASELINE_LOAD) > MAX_LOAD_DELTA_PP:
        return {'status': 'NOT_COMPARABLE', 'projected_full_min': projected, 'retention_pct': None}
    return {'status': 'MEASURED', 'projected_full_min': projected,
            'retention_pct': round(projected / BASELINE_MIN * 100., 1)}


def floors(runtime_min, charge, load):
    """The live runtime estimate against the alarm (45) and planning (60) floors, judged near full charge
    and at low load only."""
    if runtime_min is None or charge is None or load is None:
        return 'UNKNOWN'
    if charge < FULL_CHARGE or load > LOW_LOAD:
        return 'NOT_JUDGED'
    if runtime_min < ALARM_FLOOR_MIN:
        return 'BELOW_ALARM_FLOOR'
    if runtime_min < PLANNING_FLOOR_MIN:
        return 'BELOW_PLANNING_FLOOR'
    return 'ABOVE_PLANNING_FLOOR'


def assess(evs, live):
    """The UPS view: events with their retention, the latest measured retention and its band, and the floors.
    live = {'runtime_min', 'charge', 'load', 'status'} (numbers or None)."""
    rows = [{**e, **retention(e)} for e in evs if e.get('status') != 'ONGOING']
    measured = [r for r in rows if r['status'] == 'MEASURED']
    last = measured[-1] if measured else None
    band = 'LEARNING' if last is None else 'HEALTHY' if last['retention_pct'] >= HEALTHY_PCT else 'WATCH'
    return {'events': rows[-10:], 'natural_events': len(rows), 'measured': len(measured),
            'retention_pct': last['retention_pct'] if last else None, 'band': band, 'bands': 'PROVISIONAL',
            'on_battery_now': flag(live.get('status')), 'runtime_min': live.get('runtime_min'),
            'charge': live.get('charge'), 'load': live.get('load'),
            'floors': floors(live.get('runtime_min'), live.get('charge'), live.get('load')),
            'alarm_floor_min': ALARM_FLOOR_MIN, 'planning_floor_min': PLANNING_FLOOR_MIN,
            'baseline': {'full_runtime_min': BASELINE_MIN, 'load_pct': BASELINE_LOAD, 'battery_replaced': '2026-07-24'}}


def predicted_full_min(view):
    """The frozen prediction for the next natural event's full runtime: the baseline scaled by the latest
    measured retention (the baseline itself until one exists)."""
    pct = view.get('retention_pct')
    return round(BASELINE_MIN * (pct / 100. if isinstance(pct, (int, float)) else 1.), 1)


def live_values(states, ids):
    val = lambda e: (states.get(e) or {}).get('state')
    return {'runtime_min': number(val(ids['runtime_min']), 0, 10000), 'charge': number(val(ids['charge']), 0, 100),
            'load': number(val(ids['load']), 0, 200), 'status': val(ids['status'])}
