"""The yard ledger for the B-hyve hose timers (design R1 §4). B-hyve keeps watering control.

A run is a valve `open` to `closed`. Its gallons are the rise in v2.2's Orbit-reported total
between the run's start and the next run (or three hours after it ended). A run with no rise is
NOT_REPORTED: a null Orbit volume is a valid gap, never runtime × GPM. Gallons are Orbit-reported,
never billing-grade. A valve that goes `unavailable` mid-run makes the run's end unknown, so it is a
gap too.
"""
import bisect
import math
import statistics
from datetime import datetime

from .entities import ZONES, zone_ids
from .ro import ZONE

AFTER_RUN_S = 3 * 3600
SCHEDULE_MATCH_S = 20 * 60
MISSED_AFTER_S = 2 * 3600
SHIFT_RUNS = 3
MIN_RUNS = 6
MIN_BATTERY_DAYS = 7
MAX_HORIZON_S = 365 * 86400   # a threshold date further out than this is shown as BEYOND_A_YEAR
MOVING = ('opening', 'closing')   # a valve on its way: neither the start nor the end of a run


def held(points, t):
    """The numeric value held at time t, or None."""
    value = None
    for pt, v, _ in points:
        if pt > t:
            break
        value = v
    return value


def run_list(valve_points, total_points, now):
    """Runs from valve edges, with Orbit gallons where reported."""
    out, opened = [], None
    for t, _, word in valve_points:
        if word in MOVING:
            continue
        if word == 'open' and opened is None:
            opened = t
        elif word == 'closed' and opened is not None:
            out.append({'start': opened, 'end': t, 'minutes': round((t - opened) / 60, 1)})
            opened = None
        elif word not in ('open', 'closed') and opened is not None:
            out.append({'start': opened, 'end': None, 'minutes': None, 'status': 'END_UNKNOWN'})
            opened = None
    if opened is not None:
        out.append({'start': opened, 'end': None, 'minutes': None, 'status': 'RUNNING' if now - opened < 6 * 3600
                    else 'END_UNKNOWN'})
    for i, run in enumerate(out):
        if run['end'] is None:
            run['gallons'] = None
            continue
        limit = min(out[i + 1]['start'] if i + 1 < len(out) else math.inf, run['end'] + AFTER_RUN_S)
        before, after = held(total_points, run['start']), held(total_points, min(limit, now))
        rise = None if before is None or after is None else after - before
        run['gallons'] = round(rise, 2) if rise is not None and rise > 0 else None
        # Orbit's total can rise late: until the window has closed, no volume yet is waiting, not unreported.
        run['settled'] = now >= limit
        run['status'] = 'OK' if run['gallons'] is not None else 'NOT_REPORTED' if run['settled'] else 'WAITING_FOR_ORBIT'
        run['gpm'] = round(run['gallons'] / run['minutes'], 3) if run['gallons'] and run['minutes'] and run['minutes'] >= 1 else None
    return out


def blind(valve_points, a, b):
    """True when the valve series was a gap (or not yet seen) at any time between a and b."""
    word = None
    for t, _, w in valve_points:
        if t > b:
            break
        if t <= a:
            word = w
        elif w not in ('open', 'closed') + MOVING:
            return True
    return word is None or word not in ('open', 'closed') + MOVING


def schedule(runs, next_points, rain_points, now, valve_points=()):
    """Scheduled starts (from next_watering) against actual runs: ON_TIME, MISSED, SKIPPED_RAIN_DELAY, UNSCHEDULED;
    NO_DATA when the valve was a gap around the scheduled time (never counted as missed). A run matches one plan."""
    planned = set()
    for _, _, word in next_points:
        try:
            planned.add(datetime.fromisoformat(word.replace('Z', '+00:00')).timestamp())
        except (AttributeError, ValueError):
            continue
    matched, rows = set(), []
    for when in sorted(planned):
        if when > now:
            continue
        hit = next((r for r in runs if abs(r['start'] - when) <= SCHEDULE_MATCH_S and r['start'] not in matched), None)
        if hit is not None:
            matched.add(hit['start'])
            rows.append((when, 'ON_TIME'))
        elif now - when > MISSED_AFTER_S and blind(valve_points, when - SCHEDULE_MATCH_S, when + MISSED_AFTER_S):
            rows.append((when, 'NO_DATA'))
        elif now - when > MISSED_AFTER_S:
            delayed = next((w for t, _, w in reversed(rain_points) if t <= when), None) == 'on'
            rows.append((when, 'SKIPPED_RAIN_DELAY' if delayed else 'MISSED'))
    rows += [(r['start'], 'UNSCHEDULED') for r in runs if r['start'] not in matched]
    return sorted(rows)


def theil_sen(points):
    pts = points[::max(1, len(points) // 200)]
    slopes = [(y2 - y1) / (x2 - x1) for i, (x1, y1) in enumerate(pts) for x2, y2 in pts[i + 1:] if x2 != x1]
    if not slopes:
        return None
    slope = statistics.median(slopes)
    return slope, statistics.median(y - slope * x for x, y in pts)


def battery(points, threshold, now):
    """Daily-median battery trend; the date it reaches the owner's own threshold (v2.2's helper), with a band."""
    days = {}
    for t, v, _ in points:
        if v is not None and 0 <= v <= 100:
            days.setdefault(datetime.fromtimestamp(t, ZONE).strftime('%Y-%m-%d'), []).append((t, v))
    daily = [(statistics.median(t for t, _ in vs), statistics.median(v for _, v in vs)) for _, vs in sorted(days.items())]
    if len(daily) < MIN_BATTERY_DAYS or daily[-1][0] - daily[0][0] < (MIN_BATTERY_DAYS - 1) * 86400:
        return {'status': 'LEARNING', 'days': len(daily), 'last': daily[-1][1] if daily else None}
    fit = theil_sen(daily)
    slope, icpt = fit
    resid = sorted(abs(v - (slope * t + icpt)) for t, v in daily)
    band = resid[min(len(resid) - 1, math.ceil(0.9 * (len(resid) + 1)) - 1)]
    out = {'status': 'ESTIMATED', 'last': daily[-1][1], 'per_week': round(slope * 7 * 86400, 2), 'band': round(band, 2)}
    if threshold is None:
        out['reaches_threshold'] = 'NO_THRESHOLD'      # never invented: v2.2's helper is unreadable
    elif slope >= 0:
        out['reaches_threshold'] = 'NOT_DECLINING'
    else:
        day = lambda at: (datetime.fromtimestamp(max(at, now), ZONE).strftime('%Y-%m-%d')
                          if at <= now + MAX_HORIZON_S else 'BEYOND_A_YEAR')
        out['reaches_threshold'] = day((threshold - icpt) / slope)
        out['earliest'] = day((threshold + band - icpt) / slope)
        out['threshold'] = threshold
    return out


def flow(runs):
    """Orbit gallons per minute: median and a robust band; a shift when the last runs all sit outside it."""
    gpm = [r['gpm'] for r in runs if r.get('gpm')]
    if len(gpm) < MIN_RUNS:
        return {'status': 'LEARNING', 'runs_with_volume': len(gpm)}
    base = gpm[:-SHIFT_RUNS]
    med = statistics.median(base)
    mad = statistics.median(abs(x - med) for x in base) * 1.4826
    width = max(3 * mad, 0.05 * med)
    shifted = all(abs(x - med) > width for x in gpm[-SHIFT_RUNS:])
    return {'status': 'REPORTED_FLOW_CHANGED' if shifted else 'STEADY', 'median_gpm': round(med, 3),
            'band_gpm': round(width, 3), 'last_gpm': gpm[-SHIFT_RUNS:], 'runs_with_volume': len(gpm),
            'note': 'Orbit-reported, advisory only; v2.2 flow anomaly and #124 stay the alert sources.'}


def correlation(runs, temps):
    """Pearson r of run gallons against the garden temperature that day (context only; no ET0 exists)."""
    pairs = []
    pts = sorted((t, v) for t, v, _ in temps if v is not None)
    times = [t for t, _ in pts]
    sums = [0.]
    for _, v in pts:
        sums.append(sums[-1] + v)
    for r in runs:
        if r.get('gallons') is None:
            continue
        i, j = bisect.bisect_left(times, r['start'] - 43200), bisect.bisect_right(times, r['start'] + 43200)
        if j > i:
            pairs.append(((sums[j] - sums[i]) / (j - i), r['gallons']))
    if len(pairs) < 10:
        return {'status': 'LEARNING', 'runs': len(pairs)}
    xs, ys = zip(*pairs)
    try:
        r = statistics.correlation(xs, ys)
    except statistics.StatisticsError:
        return {'status': 'NO_SPREAD', 'runs': len(pairs)}
    return {'status': 'ESTIMATED', 'r': round(r, 3), 'runs': len(pairs), 'note': 'Context only; not a water need.'}


def ledger(series, threshold, now, since):
    """Every active zone's runs, gallons, schedule, battery and flow, from the App's own series."""
    out = {}
    temps = series.points('sensor.garden_ble_temperature', since)
    for zone, stem, total in ZONES:
        ids = zone_ids(stem)
        runs = run_list(series.points(ids['valve'][0], since), series.points(total, since), now)
        settled = [r for r in runs if r.get('settled')]
        done = [r for r in runs if r.get('end')]
        valve = series.points(ids['valve'][0], since)
        sched = schedule(runs, series.points(ids['next'][0], since), series.points(ids['rain_delay'][0], since), now, valve)
        out[zone] = {'runs_30d': len(done), 'gap_runs': sum(r.get('status') == 'END_UNKNOWN' for r in runs),
                     'not_reported': sum(r.get('status') == 'NOT_REPORTED' for r in settled),
                     'waiting_for_orbit': sum(r.get('status') == 'WAITING_FOR_ORBIT' for r in done),
                     'minutes_median': statistics.median(r['minutes'] for r in done) if done else None,
                     'gallons_30d': round(sum(r['gallons'] for r in done if r.get('gallons')), 1),
                     'last_runs': [{k: r.get(k) for k in ('start', 'end', 'minutes', 'gallons', 'gpm', 'status', 'settled')}
                                   for r in runs[-5:]],
                     'schedule': {k: sum(1 for _, s in sched if s == k)
                                  for k in ('ON_TIME', 'MISSED', 'SKIPPED_RAIN_DELAY', 'UNSCHEDULED', 'NO_DATA')},
                     'battery': battery(series.points(ids['battery'][0], since), threshold, now),
                     'flow': flow(done), 'weather': correlation(done, temps), 'runs': runs}
    return out
