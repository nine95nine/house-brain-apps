"""Shadow comfort readout (0.3.0): the ASHRAE 55 adaptive band and an air-speed credit, beside the fixed target.

It is fixed arithmetic on measured readings and FEEDS NOTHING: not the plan, the scorecard, the regret ledger
or any proposal. It answers one question for a later, separately approved design: on how many minutes did the
fixed target call the house warm while the adaptive band still called it acceptable?

Guards (design review A10): the adaptive method is valid only for a prevailing mean outdoor temperature of
10-33.5 °C, so a Phoenix midsummer week reads OUT_OF_RANGE and claims nothing; a minute with indoor dew point
above 62 °F is never counted acceptable (the adaptive method ignores humidity); room air speed is NOT measured,
so the credit is a separate "if at least 0.6 m/s" column for HIGH fan only, never a claim.

Constants: neutral = 0.31 x prevailing mean + 17.8 °C, 80 % limits +/- 3.5 °C, 90 % +/- 2.5 °C (ASHRAE 55
adaptive method). The applicability range and the 1.2 °C credit at 0.6 m/s are as recalled from the standard;
the primary text was not retrieved (2026-11-16), and the output says so.
"""
import statistics

from .entities import actual_mode, fan_speed
from .learn import BUCKET_S
from .plan import local_day

SCHEMA = 'house_brain_cooling_ai_comfort.v1'
MEAN_DAYS = 7
MIN_MEAN_DAYS = 5               # at least 5 of the 7 earlier days, each with most of its readings
MIN_DAY_BUCKETS = 200
VALID_C = (10., 33.5)
HALF_80_C, HALF_90_C = 3.5, 2.5
CREDIT_C = 1.2                  # upper limit raised at >= 0.6 m/s; air speed is NOT measured here
MAX_DEW_POINT_F = 62.
WINDOWS = (7, 28)
MODES = ('IDLE', 'VENT', 'WET_EVAP')
FIELDS = ('minutes', 'no_target', 'out_of_range', 'fixed_warm', 'humid', 'ok_90', 'ok_80', 'ok_80_if_air_speed')
SOURCE = 'ASHRAE 55 adaptive method; applicability range and air-speed credit recalled, primary text not verified'


def to_c(f):
    return (f - 32.) / 1.8


def to_f(c):
    return c * 1.8 + 32.


def limits(mean_out_f):
    """Upper limits in °F for a prevailing mean outdoor temperature in °F, or None outside the valid range."""
    mean_c = to_c(mean_out_f)
    if not VALID_C[0] <= mean_c <= VALID_C[1]:
        return None
    neutral = 0.31 * mean_c + 17.8
    return {'neutral': to_f(neutral), 'upper_90': to_f(neutral + HALF_90_C), 'upper_80': to_f(neutral + HALF_80_C),
            'upper_80_if_air_speed': to_f(neutral + HALF_80_C + CREDIT_C)}


def prevailing(day_means, days):
    """The mean of the earlier 7 local days' mean outdoor temperatures; None with fewer than 5 of them."""
    held = [day_means[d] for d in days if d in day_means]
    return statistics.fmean(held) if len(held) >= MIN_MEAN_DAYS else None


def report(rows, now):
    """Per window: minutes by mode. A minute counts in `fixed_warm` when indoor is above the Arbiter's own
    target; the ok_* fields count how many of THOSE the adaptive band still accepts. Unknown is never zero:
    minutes without a target or outside the valid range are counted in their own fields."""
    by_day, sums = {}, {}
    for t, row in rows:
        day = local_day(t)
        by_day.setdefault(day, []).append((t, row))
        if row.get('t_out') is not None:
            total, n = sums.get(day, (0., 0))
            sums[day] = (total + row['t_out'], n + 1)
    day_means = {d: total / n for d, (total, n) in sums.items() if n >= MIN_DAY_BUCKETS}
    ordered = sorted(by_day)
    per_day = {}
    for i, day in enumerate(ordered):
        mean = prevailing(day_means, ordered[max(0, i - MEAN_DAYS):i])
        lim = limits(mean) if mean is not None else None
        rec = per_day[day] = {'mean_out_f': mean, 'limits': lim, 'modes': {m: dict.fromkeys(FIELDS, 0.) for m in MODES},
                              'headroom': []}
        for _, row in by_day[day]:
            mode = actual_mode(row)
            t_in = row.get('t_in')
            if mode not in MODES or t_in is None:
                continue
            c = rec['modes'][mode]
            minutes = BUCKET_S / 60
            c['minutes'] += minutes
            target = row.get('comfort')
            if target is None or mean is None:
                c['no_target'] += minutes
                continue
            if lim is None:
                c['out_of_range'] += minutes
                continue
            rec['headroom'].append(lim['upper_80'] - target)
            if t_in <= target:
                continue
            c['fixed_warm'] += minutes
            dew = row.get('dp_in')
            if dew is None or dew > MAX_DEW_POINT_F:
                c['humid'] += minutes           # unknown or humid: never counted acceptable
                continue
            c['ok_90'] += minutes if t_in <= lim['upper_90'] else 0
            c['ok_80'] += minutes if t_in <= lim['upper_80'] else 0
            credit = mode != 'IDLE' and fan_speed(row) == 'HIGH'
            c['ok_80_if_air_speed'] += minutes if t_in <= (lim['upper_80_if_air_speed'] if credit
                                                           else lim['upper_80']) else 0
    windows = {}
    for days in WINDOWS:
        keep = [d for d in ordered if d >= local_day(now - (days - 1) * 86400)]
        modes = {m: {f: round(sum(per_day[d]['modes'][m][f] for d in keep), 1) for f in FIELDS} for m in MODES}
        headroom = [h for d in keep for h in per_day[d]['headroom']]
        cooling = [modes[m] for m in ('VENT', 'WET_EVAP')]
        judged = sum(c['minutes'] - c['no_target'] for c in cooling)
        out = sum(c['out_of_range'] for c in cooling)
        windows[f'{days}d'] = {'modes': modes,
                               'median_headroom_f': round(statistics.median(headroom), 1) if headroom else None,
                               'informative': not (judged and out / judged > 0.5)}
    today = per_day.get(local_day(now)) or {}
    return {'schema_version': SCHEMA, 'windows': windows,
            'today': {'prevailing_mean_out_f': round(today['mean_out_f'], 1) if today.get('mean_out_f') is not None
                      else None,
                      'limits_f': {k: round(v, 1) for k, v in today['limits'].items()} if today.get('limits') else None,
                      'status': 'NO_PREVAILING_MEAN' if today.get('mean_out_f') is None
                      else 'OUT_OF_RANGE' if not today.get('limits') else 'IN_RANGE'},
            'air_speed': 'NOT_MEASURED', 'constants_source': SOURCE, 'evidence_class': 'MODELLED',
            'feeds': [], 'physical_actions': False, 'control_authority': 'NONE'}
