"""Salt runway: the days until pool salt falls below 3,200 ppm (design R1 §4.3).

Daily points: the time-weighted mean of "AquaRite Salt Level Statistics" over each complete local day
(pool mode only: the sensor is unavailable in spa mode and while the AquaRite is not reporting; those
hours are uncovered, never zero). Salt the owner logged ("Log salt added", the salt balance package's
running total) is taken out of the trend the same way that package does (1 lb raises 19,000 gal by about
6.3 ppm), so adding salt is not read as the trend turning.

Step events: a day at least 150 ppm below the best of the three days before it starts a step down;
consecutive step days are one event. An event with rain (weather condition rainy, pouring, hail,
lightning-rainy or snowy-rainy) in the two days before or during it is RAIN_DILUTION (the 2026-11-01
storm: 2,800 -> 2,600, owner: rain dilution), else STEP_DOWN. A step resets the trend: only the days
after the last event are fitted. Hidden water loss stays the salt balance package's job.

Runway: from the trend (at least 7 days, least squares over the last 28) and today's salt (the Insight
last-known pool-mode salt). Below 3,200 now is BELOW_TARGET_NOW (Insight's "salt to add" applies);
a flat or rising trend gives no date.

Scorecard (shadow, walk-forward): the trend fitted up to a day predicts the salt 28 days later; it
matches when within 50 ppm and no step event happened in between. The "predicted date within 7 days"
check needs salt to fall through 3,200 from above, which cannot happen while salt is below it.
"""
from datetime import date, timedelta
from statistics import median

from .demand import segments
from .entities import number
from .series import local_day, local_midnight

TARGET_PPM = 3200.
GALLONS = 19000.
PPM_PER_LB = 1_000_000 / (GALLONS * 8.345)      # aquarite_salt_balance 1.0-R2 K
STEP_PPM = 150.
STEP_LOOKBACK_DAYS = 3
RAIN_BEFORE_DAYS = 2
RAIN_WORDS = frozenset({'rainy', 'pouring', 'hail', 'lightning-rainy', 'snowy-rainy'})
TREND_DAYS = 28
MIN_TREND_DAYS = 7
MIN_COVERED_S = 1800
MATCH_PPM = 50.
SCORE_SHARE = 0.9
KEEP_DAYS = 400


def daily(level, added, weather, day):
    """One complete local day: time-weighted pool-mode salt, logged salt at its end, and rain seen."""
    a = local_midnight(day)
    b = a + 86400
    total = covered = 0.
    for lo, hi, v, _ in segments(level, a, b):
        s = number(v, 500, 10000)
        if s is not None:
            total += s * (hi - lo)
            covered += hi - lo
    lb = None
    for t, v, _ in added:
        if t < b:
            lb = number(v, 0, 1e5)
    rain = any(s in RAIN_WORDS for _, _, _, s in segments(weather, a, b))
    return {'day': day, 'mean': round(total / covered, 1) if covered >= MIN_COVERED_S else None,
            'covered_h': round(covered / 3600, 2), 'added_lb': lb, 'rain': rain}


def adjusted(row):
    """Salt with the logged salt taken out (absolute: only differences are used)."""
    if row.get('mean') is None or row.get('added_lb') is None:
        return None
    return row['mean'] - row['added_lb'] * PPM_PER_LB


def _ord(day):
    return date.fromisoformat(day).toordinal()


def events(rows):
    """Step-down events over the daily rows (sorted by day)."""
    known = [(_ord(r['day']), adjusted(r), r) for r in rows if adjusted(r) is not None]
    steps = []
    for x, y, r in known:
        before = [yy for xx, yy, _ in known if x - STEP_LOOKBACK_DAYS <= xx < x]
        if before and max(before) - y >= STEP_PPM:
            steps.append((x, max(before) - y))
    out = []
    for x, drop in steps:
        if out and x - out[-1]['end_x'] <= STEP_LOOKBACK_DAYS:
            out[-1].update(end_x=x, drop_ppm=max(out[-1]['drop_ppm'], round(drop, 1)))
        else:
            out.append({'start_x': x, 'end_x': x, 'drop_ppm': round(drop, 1)})
    rain_days = {_ord(r['day']) for r in rows if r.get('rain')}
    for e in out:
        e['kind'] = 'RAIN_DILUTION' if any(e['start_x'] - RAIN_BEFORE_DAYS <= x <= e['end_x'] for x in rain_days) \
            else 'STEP_DOWN'
        e['start'] = date.fromordinal(e['start_x']).isoformat()
        e['end'] = date.fromordinal(e['end_x']).isoformat()
    return out


def trend(rows, upto_x, evs):
    """Least squares over the adjusted daily salt after the last step event, within 28 days up to `upto_x`."""
    last_event = max((e['end_x'] for e in evs if e['end_x'] <= upto_x), default=None)
    pts = [(_ord(r['day']), adjusted(r)) for r in rows
           if adjusted(r) is not None and upto_x - TREND_DAYS < _ord(r['day']) <= upto_x
           and (last_event is None or _ord(r['day']) > last_event)]
    if len(pts) < MIN_TREND_DAYS or pts[-1][0] - pts[0][0] < MIN_TREND_DAYS - 1:
        return None
    n = len(pts)
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    slope = sum((x - mx) * (y - my) for x, y in pts) / sxx
    return {'slope_ppm_day': round(slope, 3), 'n': n, 'x_mean': mx, 'y_mean': my, 'since_event': last_event is not None}


def at_x(fit, x):
    return fit['y_mean'] + fit['slope_ppm_day'] * (x - fit['x_mean'])


def scorecard(rows, evs):
    """Walk-forward: each fitted day predicts the adjusted salt 28 days later."""
    by_x = {_ord(r['day']): adjusted(r) for r in rows if adjusted(r) is not None}
    scored = []
    for x in sorted(by_x):
        target = x + TREND_DAYS
        if target not in by_x or any(x < e['start_x'] <= target for e in evs):
            continue
        fit = trend(rows, x, evs)
        if fit is None:
            continue
        scored.append(abs(by_x[target] - at_x(fit, target)))
    if not scored:
        return {'scored': 0, 'median_abs_ppm': None, 'share_within_50': None, 'status': 'WAITING', 'bar_ppm': MATCH_PPM}
    share = round(sum(e <= MATCH_PPM for e in scored) / len(scored), 3)
    return {'scored': len(scored), 'median_abs_ppm': round(median(scored), 1), 'share_within_50': share,
            'status': 'MET' if share >= SCORE_SHARE else 'NOT_MET', 'bar_ppm': MATCH_PPM}


def view(rows, live_salt, now):
    """Runway, events, trend and scorecard from the stored daily rows (sorted by day)."""
    evs = events(rows)
    today = _ord(local_day(now))
    fit = trend(rows, today, evs)
    latest = next((r['mean'] for r in reversed(rows) if r.get('mean') is not None), None)
    current = number(live_salt, 500, 10000)
    if current is None:
        current = latest
    out = {'current_ppm': current, 'target_ppm': TARGET_PPM, 'trend': None if fit is None else
           {'slope_ppm_day': fit['slope_ppm_day'], 'days': fit['n'], 'after_step_event': fit['since_event']},
           'events': [{k: e[k] for k in ('start', 'end', 'drop_ppm', 'kind')} for e in evs][-6:],
           'days_kept': len(rows), 'scorecard': scorecard(rows, evs), 'runway_days': None, 'below_on': None}
    if current is None:
        return {**out, 'status': 'BLOCKED', 'reason': 'POOL_SALT_UNREADABLE'}
    if current < TARGET_PPM:
        return {**out, 'status': 'BELOW_TARGET_NOW', 'runway_days': 0, 'reason': None}
    if fit is None:
        return {**out, 'status': 'LEARNING', 'reason': 'SALT_TREND_NEEDS_7_DAYS'}
    if fit['slope_ppm_day'] >= -0.5:
        return {**out, 'status': 'NOT_FALLING', 'reason': None}
    days = (current - TARGET_PPM) / -fit['slope_ppm_day']
    when = date.fromordinal(today) + timedelta(days=round(days))
    return {**out, 'status': 'FALLING', 'runway_days': round(days, 1), 'below_on': when.isoformat(), 'reason': None}
