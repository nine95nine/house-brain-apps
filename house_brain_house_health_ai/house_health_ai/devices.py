"""Idle versus stale (design R1 §4.5).

A device that is still reporting but whose reading has not changed for unusually long is different from
one that has stopped reporting:
- NOT_REPORTING: the state is unknown or unavailable. That is Systems AI's to diagnose; House Health only
  names it and never reads it as OK.
- UNCHANGED_UNUSUALLY_LONG: a valid reading, unchanged for longer than LIMIT_K times the longest unchanged
  spell seen in its own last 7 days (never less than FLOOR_S); 12 hours while that is still being learned.
- REPORTING: a valid reading within its usual spell.
Only continuous measurements are watched (the room temperatures, house humidity and dew point), where a
long constant is unusual. A constant switch or flag is idle, not stale, and is not watched here. No alert:
Maintenance owns low-battery and offline alerts. Pure functions; no network.
"""
from .entities import GAP, number

WINDOW_S = 7 * 86400
LIMIT_K = 2.
FLOOR_S = 6 * 3600
LEARNING_S = 12 * 3600
MIN_CHANGES = 10


def usual_spell(points, now):
    """The longest unchanged spell (seconds) between valid value changes in the last 7 days, or None while
    fewer than MIN_CHANGES changes were seen."""
    times = [t for t, v, _ in points if v is not None and t >= now - WINDOW_S]
    if len(times) < MIN_CHANGES:
        return None
    return max(b - a for a, b in zip(times, times[1:]))


def judge(state, changed, points, now):
    """One device: {'status', 'unchanged_s', 'limit_s'}. state is its live state text, changed its
    last_changed (epoch seconds) or None."""
    if state in GAP or number(state) is None:
        return {'status': 'NOT_REPORTING', 'unchanged_s': None, 'limit_s': None, 'owner': 'SYSTEMS_AI'}
    usual = usual_spell(points, now)
    limit = LEARNING_S if usual is None else max(FLOOR_S, LIMIT_K * usual)
    if changed is None:
        return {'status': 'UNKNOWN', 'unchanged_s': None, 'limit_s': round(limit)}
    unchanged = max(0., now - changed)
    return {'status': 'UNCHANGED_UNUSUALLY_LONG' if unchanged > limit else 'REPORTING',
            'unchanged_s': round(unchanged), 'limit_s': round(limit)}
