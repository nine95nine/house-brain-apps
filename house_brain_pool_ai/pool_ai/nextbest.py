"""Next Best Measurement (design R1 D1, R3.1 I04): "next test helps most on <day>". No nagging.

Shown only on the App's own page and in its digest: never a push, an alert, a count of missed days or a
reminder. The learner needs about two logged tests a week, and pairs whose generation-per-day differs
from the pairs it already has (a fixed schedule alone cannot tell the cell's output from the pool's use).

The day: among 2 to 5 days after the last test, the one whose expected generation per day (the pool
generation measured for each weekday over the last 28 days) is furthest from every pair already logged;
with no pair yet, 3 days after the last test (about twice a week); with no test at all, today. A day in
the past becomes today. Ties go to the earlier day.
"""
from datetime import timedelta

from .demand import pool_generation, valid
from .series import local, local_midnight

FIRST_GAP_DAYS = 3
GAPS = (2, 3, 4, 5)
PROFILE_DAYS = 28


def weekday_profile(rate, spa, now):
    """Mean pool generation hours per weekday over the last 28 complete local days (covered days only)."""
    sums, counts = [0.] * 7, [0] * 7
    today = local(now).date()
    for back in range(1, PROFILE_DAYS + 1):
        day = today - timedelta(days=back)
        a = local_midnight(day.isoformat())
        g, _, coverage = pool_generation(rate, spa, a, a + 86400)
        if coverage >= 0.9:
            sums[day.weekday()] += g
            counts[day.weekday()] += 1
    return [round(s / c, 3) if c else None for s, c in zip(sums, counts)]


def pick(test_rows, pair_rows, profile, now):
    today = local(now).date()
    if not test_rows:
        return _out(today, today, 'NO_TEST_LOGGED_YET')
    last = local(test_rows[-1]['at']).date()
    if not valid(pair_rows) or any(p is None for p in profile):
        return _out(max(last + timedelta(days=FIRST_GAP_DAYS), today), today, 'TWICE_A_WEEK_RHYTHM')
    ratios = [p['gen_h'] / p['days'] for p in valid(pair_rows)]
    best = None
    for gap in GAPS:
        day = last + timedelta(days=gap)
        expected = sum(profile[(last + timedelta(days=n)).weekday()] for n in range(1, gap + 1)) / gap
        score = round(min(abs(expected - r) for r in ratios), 4)
        if best is None or score > best[0]:
            best = (score, day)
    return _out(max(best[1], today), today, 'DIFFERENT_GENERATION_PER_DAY')


def _out(day, today, reason):
    when = 'today' if day == today else day.strftime('%A %d %b').replace(' 0', ' ')
    return {'day': day.isoformat(), 'weekday': day.strftime('%A'), 'reason': reason,
            'text': f'next test helps most {"today" if day == today else "on " + when}', 'nag': False}


def view(test_rows, pair_rows, rate, spa, now):
    return pick(test_rows, pair_rows, weekday_profile(rate, spa, now), now)
