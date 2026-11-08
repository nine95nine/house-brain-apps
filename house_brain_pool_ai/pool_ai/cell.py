"""The salt cell: its learned efficiency over time, and its wear and cleaning dates (design R1 §4.2, §4.5).

Efficiency factor: the cell rate k (ppm per pool generation hour) fitted over each run of 8 consecutive
valid test pairs, against the first such fit. There is no rated figure to compare with: the CELL-5
production seed is quarantined (DO_NOT_REGRESS), so the factor is "this cell now against this cell when
Pool AI first learned it". A factor below 0.8 is FALLING: a cleaning or wear signal, shown beside
Insight's hours, never a second reminder.

Dates: Insight 1.3's own numbers (generation hours, hours since cleaning, last cleaning date and its
10,000 h rating, 500 h / 90-day cleaning rule) projected to calendar dates at the run rate the App
measures from the generation-hours history (the last 28 days, at least 7). Insight keeps its reminder;
Pool AI only shows a date.
"""
from datetime import datetime, timedelta

from .demand import MIN_PAIRS, fit, valid
from .entities import number
from .series import ZONE, local

RATED_HOURS = 10_000.     # Insight 1.3-R1 `rated`
CLEAN_HOURS = 500.        # Insight 1.3-R1 cleaning reminder
CLEAN_DAYS = 90
RATE_WINDOW_S = 28 * 86400
MIN_RATE_SPAN_S = 7 * 86400
FALLING_FACTOR = 0.8


def factor_trend(pair_rows):
    """The cell rate over rolling windows of 8 valid pairs, and today's factor against the first window."""
    good = valid(pair_rows)
    windows = []
    for end in range(MIN_PAIRS, len(good) + 1):
        model = fit(good[end - MIN_PAIRS:end])
        if model['kind'] == 'FULL':
            windows.append({'at': good[end - 1]['end'], 'cell_rate': model['cell_rate']})
    if len(windows) < 2:
        return {'status': 'LEARNING', 'factor': None, 'windows': len(windows), 'first': windows[0] if windows else None,
                'latest': windows[-1] if windows else None, 'reason': 'NEEDS_TWO_SEPARABLE_WINDOWS_OF_8_PAIRS'}
    factor = round(windows[-1]['cell_rate'] / windows[0]['cell_rate'], 3)
    return {'status': 'FALLING' if factor < FALLING_FACTOR else 'STEADY', 'factor': factor, 'windows': len(windows),
            'first': windows[0], 'latest': windows[-1], 'reason': None}


def run_rate(hours_points, now):
    """Generation hours per day over the last 28 days (at least 7), from the cumulative counter. A counter
    that goes backwards (a reset) restarts the measurement after it."""
    since = now - RATE_WINDOW_S
    first = prev = None
    for t, v, _ in hours_points:
        if v is None or v < 0:
            continue
        if prev is not None and v < prev[1]:
            first = (t, v)                       # the counter was reset: measure from here
        elif first is None or t <= since:
            first = (max(t, since), v)
        prev = (t, v)
    # The counter holds its last value until it changes (the pump is off): the span runs to now. A counter
    # that is not reporting now gives no rate.
    if hours_points and hours_points[-1][1] is None:
        return None
    if first is None or now - first[0] < MIN_RATE_SPAN_S:
        return None
    return round((prev[1] - first[1]) / ((now - first[0]) / 86400), 4)


def _date(at):
    return local(at).strftime('%Y-%m-%d')


def dates(states_get, hours_points, now):
    """Wear and cleaning dates, or BLOCKED with the missing input named."""
    hours = number(states_get('hours'), 0, 1e6)
    since = number(states_get('since_cleaning'), 0, 1e6)
    life = number(states_get('life_used'), 0, 1000)
    rate = run_rate(hours_points, now)
    out = {'generation_hours': hours, 'hours_since_cleaning': since, 'life_used_pct': life,
           'run_rate_h_per_day': rate, 'rated_hours': RATED_HOURS}
    if hours is None:
        return {**out, 'status': 'BLOCKED', 'missing': 'GENERATION_HOURS_UNREADABLE'}
    if rate is None or rate <= 0:
        return {**out, 'status': 'BLOCKED', 'missing': 'RUN_RATE_NEEDS_7_DAYS'}
    wear_at = now + (RATED_HOURS - hours) / rate * 86400 if hours < RATED_HOURS else now
    out['wear_10000h_around'] = local(wear_at).strftime('%B %Y')
    out['wear_10000h_date'] = _date(wear_at)
    by_hours = None if since is None else now + max(0., CLEAN_HOURS - since) / rate * 86400
    cleaned = states_get('last_cleaned')
    try:
        by_days = datetime.fromisoformat(cleaned).replace(tzinfo=ZONE).timestamp() + CLEAN_DAYS * 86400 \
            if isinstance(cleaned, str) and len(cleaned) == 10 else None
    except ValueError:
        by_days = None
    options = [t for t in (by_hours, by_days) if t is not None]
    if not options:
        return {**out, 'status': 'PARTIAL', 'missing': 'CLEANING_BASELINE_UNREADABLE'}
    nxt = min(options)
    out['next_cleaning_date'] = _date(max(nxt, now))
    out['next_cleaning_by'] = 'HOURS' if nxt == by_hours else 'DAYS'
    out['cleaning_due_now'] = nxt <= now
    out['status'] = 'OK'
    return out
