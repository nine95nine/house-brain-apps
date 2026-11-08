"""House-level mold risk (design R1 §4.2): a slow 30-day index, never a real-time alarm.

In the style of ASHRAE 160 (a 30-day running average), applied to indoor air at house level, from the
Cooling Arbiter's house humidity and indoor dew point. Thresholds (PROVISIONAL, owner-tunable later):
- relative humidity, 30-day time-weighted mean: WATCH at 60 % (EPA: keep indoor RH below 60 %),
  RISK at 70 %, half-way to ASHRAE 160's 80 % surface criterion (room air is drier than cold surfaces);
- dew point, 30-day time-weighted mean: WATCH at 60 °F, RISK at 65 °F.
Hysteresis: a level is kept until the mean falls RH_BAND (3 points) or DP_BAND (2 °F) below its threshold.
The index is the worse of the two. A metric with less than MIN_COVERAGE of the window covered by a valid
reading is UNKNOWN; UNKNOWN is never OK. Cooling's stale-air advisory and Humid Boost keep real-time
humidity. Pure functions; nothing here reads the network.
"""

WINDOW_S = 30 * 86400
MIN_COVERAGE = 0.8
RH = {'watch': 60., 'risk': 70., 'band': 3., 'range': (1., 100.)}
DP = {'watch': 60., 'risk': 65., 'band': 2., 'range': (-40., 100.)}
ORDER = ('OK', 'WATCH', 'RISK')


def time_weighted(points, start, end, lo, hi):
    """(mean, covered fraction) over [start, end) from change points [(t, value, word)] sorted by time. A value
    holds until the next change (a constant is idle, not missing); a gap or an out-of-range value covers
    nothing."""
    if end <= start:
        return None, 0.
    total = covered = 0.
    for n, (t, v, _) in enumerate(points):
        a = max(t, start)
        b = min(points[n + 1][0] if n + 1 < len(points) else end, end)
        if b <= a or v is None or not lo <= v <= hi:
            continue
        covered += b - a
        total += v * (b - a)
    return (round(total / covered, 2) if covered else None), round(covered / (end - start), 3)


def level(value, coverage, limits, prev):
    """OK / WATCH / RISK for one metric with hysteresis against the previous level; UNKNOWN without data."""
    if value is None or coverage < MIN_COVERAGE:
        return 'UNKNOWN'
    raw = 'RISK' if value >= limits['risk'] else 'WATCH' if value >= limits['watch'] else 'OK'
    if prev == 'RISK' and value >= limits['risk'] - limits['band']:
        return 'RISK'
    if prev in ('RISK', 'WATCH') and raw == 'OK' and value >= limits['watch'] - limits['band']:
        return 'WATCH'
    return raw


def index(rh_points, dp_points, now, prev=None):
    """The house mold index: {'level', 'rh', 'dew_point'}; prev is the previous result (for hysteresis)."""
    prev = prev or {}
    start = now - WINDOW_S
    out = {}
    for key, pts, limits in (('rh', rh_points, RH), ('dew_point', dp_points, DP)):
        mean, coverage = time_weighted(pts, start, now, *limits['range'])
        out[key] = {'mean_30d': mean, 'coverage': coverage,
                    'level': level(mean, coverage, limits, (prev.get(key) or {}).get('level')),
                    'watch_at': limits['watch'], 'risk_at': limits['risk']}
    levels = [out['rh']['level'], out['dew_point']['level']]
    known = [x for x in levels if x != 'UNKNOWN']
    worst = max(known, key=ORDER.index) if known else 'UNKNOWN'
    # Unknown is never OK: one metric missing leaves the index UNKNOWN unless the other already shows a risk.
    out['level'] = worst if len(known) == 2 or worst in ('WATCH', 'RISK') else 'UNKNOWN'
    out['thresholds'] = 'PROVISIONAL'
    return out
