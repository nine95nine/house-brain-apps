"""Chlorine demand for this pool, learned from pairs of logged free-chlorine (FC) tests (design R1 §4.1).

Between two logged tests the FC changes by what the cell made minus what the pool used:

    FC_next - FC_prev = k * G - d * D

G is the pool generation hours between the tests (AquaRite "Generation Rate", the output fraction while
the pump runs and telemetry is live, integrated over time, with spa time left out), D the days between
the tests, k the learned cell rate (ppm per generation hour) and d the demand (ppm per day). Both are
fitted by least squares over the paired tests; nothing starts from a seed (the CELL-5 production seed is
quarantined, DO_NOT_REGRESS). When the pairs cannot tell k and d apart (a fixed schedule gives the same
G per day every time) or the fit is not physical, only the net change per day is used, and demand says why.

Scoring is walk-forward (a holdout): each test is forecast from the pairs before it only. The bar (family
plan): median |error| <= 0.5 ppm, 90 % within 1.0 ppm, and no false-safe, i.e. never a forecast at or
above the Advisor's FC minimum when the test is below it. The minimum is the Advisor 1.4-R2 rule:
max(1, 7.5 % of CYA rounded up to the next 0.5 ppm). Until 8 paired tests, demand is "learning (n of 8)".
Shadow only: a forecast is never advice in 0.1.
"""
from bisect import bisect_left, bisect_right
import math
from statistics import median

from .entities import number, stamp
from .series import local

MIN_PAIRS = 8
MERGE_S = 30 * 60
MIN_PAIR_S = 6 * 3600
MAX_PAIR_S = 7 * 86400
MIN_COVERAGE = 0.9
SEPARABLE_MIN = 0.02          # 1 - cos^2 of the angle between the G and D columns
BAR_MEDIAN_PPM = 0.5
BAR_P90_PPM = 1.0
BAR_SHARE = 0.9
FC_RANGE = (0., 20.)          # the Advisor's input_number range
CYA_RANGE = (0., 200.)
PH_RANGE = (6.5, 8.5)


def fc_minimum(cya):
    """Advisor 1.4-R2 `manual_fc_minimum`: [1.0, (cya * 15 / 100) | round(0, 'ceil') / 2] | max."""
    if cya is None or cya < 0:
        return None
    return max(1.0, math.ceil(round(cya * 15 / 100, 9)) / 2)


def held(points, at):
    """The (value, word) a change-point series held at `at`, or (None, None)."""
    i = bisect_right(points, at, key=lambda p: p[0])
    return (points[i - 1][1], points[i - 1][2]) if i else (None, None)


def tests(logged, fc, cya, ph):
    """The logged water tests: each distinct log-button time (its state IS the test time), with the FC, CYA
    and pH the inputs held at that moment, as the Advisor's logbook entry pairs them. Presses within 30
    minutes are one test (the later wins: a corrected entry)."""
    times = sorted({at for at in (stamp(s) for _, _, s in logged) if at is not None})
    out = []
    for at in times:
        row = {'at': at, 'fc': number(held(fc, at)[0], *FC_RANGE), 'cya': number(held(cya, at)[0], *CYA_RANGE),
               'ph': number(held(ph, at)[0], *PH_RANGE)}
        if out and at - out[-1]['at'] < MERGE_S:
            out[-1] = row
        else:
            out.append(row)
    return out


def window(points, a, b):
    """The points of a sorted change-point series that matter for [a, b): the one held at `a`, then those
    inside. Binary search, so a long series costs little."""
    lo = bisect_left(points, a, key=lambda p: p[0])
    hi = bisect_left(points, b, key=lambda p: p[0])
    return points[max(0, lo - 1):hi]


def segments(points, a, b):
    """(start, end, value, word) pieces of a change-point series over [a, b)."""
    pts = window(points, a, b)
    out = []
    for i, (t, v, s) in enumerate(pts):
        end = pts[i + 1][0] if i + 1 < len(pts) else b
        lo, hi = max(t, a), min(end, b)
        if hi > lo:
            out.append((lo, hi, v, s))
    return out


def pool_generation(rate, spa, a, b):
    """Generation hours that went into the pool over [a, b): the rate (0..1) integrated over time, left
    method like Insight's integration sensor, leaving out spa time. Returns (hours, spa_hours, coverage):
    time with no rate or no spa state is uncovered, never zero."""
    events = sorted([(t, 0, v, s) for t, v, s in window(rate, a, b)] + [(t, 1, v, s) for t, v, s in window(spa, a, b)],
                    key=lambda e: (e[0], e[1]))
    pool_h = spa_h = covered = 0.
    r = mode = None
    last = a
    for t, kind, v, s in events + [(b, 2, None, None)]:
        hi = min(t, b)
        if hi > last:
            if r is not None and mode in ('on', 'off'):
                covered += hi - last
                if mode == 'on':
                    spa_h += r * (hi - last) / 3600
                else:
                    pool_h += r * (hi - last) / 3600
            last = hi
        if kind == 0:
            r = number(v, 0., 1.)
        elif kind == 1:
            mode = s
    return pool_h, spa_h, (covered / (b - a) if b > a else 0.)


def pairs(test_rows, rate, spa):
    """Consecutive tests as pairs, each VALID or with the reason it cannot be used."""
    out = []
    for a, b in zip(test_rows, test_rows[1:]):
        span = b['at'] - a['at']
        row = {'start': a['at'], 'end': b['at'], 'days': round(span / 86400, 4), 'fc_start': a['fc'],
               'fc_end': b['fc'], 'cya_end': b['cya'], 'gen_h': None, 'spa_gen_h': None, 'dfc': None,
               'status': 'VALID', 'reason': None}
        if a['fc'] is None or b['fc'] is None:
            row.update(status='SKIPPED', reason='FC_TEST_VALUE_MISSING')
        elif span < MIN_PAIR_S:
            row.update(status='SKIPPED', reason='TESTS_TOO_CLOSE')
        elif span > MAX_PAIR_S:
            row.update(status='SKIPPED', reason='TESTS_TOO_FAR_APART')
        else:
            g, sg, coverage = pool_generation(rate, spa, a['at'], b['at'])
            row.update(gen_h=round(g, 4), spa_gen_h=round(sg, 4), dfc=round(b['fc'] - a['fc'], 3),
                       coverage=round(coverage, 3))
            if coverage < MIN_COVERAGE:
                row.update(status='SKIPPED', reason='GENERATION_HISTORY_GAP')
            elif b['fc'] <= 0:
                row.update(status='SKIPPED', reason='FC_CENSORED_AT_ZERO')
            elif row['dfc'] > 0 and g <= 0:
                row.update(status='SKIPPED', reason='FC_ROSE_WITHOUT_GENERATION')
        out.append(row)
    return out


def valid(pair_rows):
    return [p for p in pair_rows if p['status'] == 'VALID']


def fit(pair_rows):
    """Least squares of dFC = k*G - d*D over the valid pairs. FULL (k > 0, d >= 0, separable), else NET
    (net ppm per day only, with the reason), else NONE."""
    rows = [(p['gen_h'], p['days'], p['dfc']) for p in valid(pair_rows)]
    if not rows:
        return {'kind': 'NONE', 'n': 0, 'cell_rate': None, 'demand_ppm_day': None, 'net_ppm_day': None,
                'reason': 'NO_PAIRED_TESTS'}
    sgg = sum(g * g for g, _, _ in rows)
    sdd = sum(d * d for _, d, _ in rows)
    sgd = sum(g * d for g, d, _ in rows)
    sgy = sum(g * y for g, _, y in rows)
    sdy = sum(d * y for _, d, y in rows)
    out = {'kind': 'NET', 'n': len(rows), 'cell_rate': None, 'demand_ppm_day': None,
           'net_ppm_day': round(sdy / sdd, 4), 'reason': 'TOO_FEW_PAIRS_TO_SEPARATE'}
    if len(rows) >= 2 and sgg > 0:
        det = sgg * sdd - sgd * sgd
        if det / (sgg * sdd) < SEPARABLE_MIN:
            out['reason'] = 'GENERATION_AND_DAYS_NOT_SEPARABLE'
        else:
            k = (sgy * sdd - sgd * sdy) / det
            d = (sgd * sgy - sgg * sdy) / det
            if k > 0 and d >= 0:
                out.update(kind='FULL', cell_rate=round(k, 5), demand_ppm_day=round(d, 4), reason=None)
            else:
                out['reason'] = 'FIT_NOT_PHYSICAL'
    return out


def predict(model, fc_start, gen_h, days):
    """The FC a model expects after `days` with `gen_h` pool generation hours; never below 0."""
    if model['kind'] == 'FULL':
        value = fc_start + model['cell_rate'] * gen_h - model['demand_ppm_day'] * days
    elif model['kind'] == 'NET':
        value = fc_start + model['net_ppm_day'] * days
    else:
        return None
    return round(max(0., value), 3)


def walk_forward(pair_rows):
    """Each valid pair's end test forecast from the earlier valid pairs only (a holdout)."""
    good = valid(pair_rows)
    out = []
    for j, p in enumerate(good):
        model = fit(good[:j])
        guess = predict(model, p['fc_start'], p['gen_h'], p['days'])
        if guess is None:
            continue
        floor = fc_minimum(p['cya_end'])
        out.append({'at': p['end'], 'predicted': guess, 'actual': p['fc_end'],
                    'error': round(guess - p['fc_end'], 3), 'fc_minimum': floor, 'model': model['kind'],
                    'false_safe': floor is not None and guess >= floor and p['fc_end'] < floor})
    return out


def scorecard(scored):
    """The FC bar over the walk-forward forecasts. LEARNING until 8 are scored; a false-safe always counts."""
    n = len(scored)
    false_safe = sum(s['false_safe'] for s in scored)
    if not n:
        return {'scored': 0, 'median_abs_ppm': None, 'share_within_1': None, 'false_safe': 0, 'status': 'LEARNING',
                'bar': {'median_abs_ppm': BAR_MEDIAN_PPM, 'p90_ppm': BAR_P90_PPM, 'false_safe': 0}}
    errors = [abs(s['error']) for s in scored]
    mid = round(median(errors), 3)
    share = round(sum(e <= BAR_P90_PPM for e in errors) / n, 3)
    met = mid <= BAR_MEDIAN_PPM and share >= BAR_SHARE and false_safe == 0
    status = 'NOT_MET' if false_safe else 'LEARNING' if n < MIN_PAIRS else 'MET' if met else 'NOT_MET'
    return {'scored': n, 'median_abs_ppm': mid, 'share_within_1': share, 'false_safe': false_safe, 'status': status,
            'bar': {'median_abs_ppm': BAR_MEDIAN_PPM, 'p90_ppm': BAR_P90_PPM, 'false_safe': 0}}


def by_month(pair_rows, model):
    """Median per-pair demand (ppm/day) by local month, using the learned cell rate; FULL models only."""
    if model['kind'] != 'FULL':
        return {}
    groups = {}
    for p in valid(pair_rows):
        groups.setdefault(local(p['end']).strftime('%Y-%m'), []).append(
            (model['cell_rate'] * p['gen_h'] - p['dfc']) / p['days'])
    return {m: round(median(v), 3) for m, v in sorted(groups.items())}


def view(test_rows, pair_rows, rate, spa, now):
    """Everything the page shows for demand, the forecast for the next test, and the scorecard."""
    model = fit(pair_rows)
    n = model['n']
    scored = walk_forward(pair_rows)
    score = scorecard(scored)
    out = {'tests': len(test_rows), 'paired': n, 'model': model, 'by_month': by_month(pair_rows, model),
           'status': 'LEARNING' if n < MIN_PAIRS else 'READY' if score['status'] == 'MET' else 'NOT_MET',
           'label': f'learning ({n} of {MIN_PAIRS} tests)' if n < MIN_PAIRS else None,
           'skipped': {}, 'forecast': None, 'scorecard': score, 'recent_scores': scored[-5:], 'advice': False}
    for p in pair_rows:
        if p['status'] != 'VALID':
            out['skipped'][p['reason']] = out['skipped'].get(p['reason'], 0) + 1
    last = test_rows[-1] if test_rows else None
    if last is not None and last['fc'] is not None and now > last['at']:
        g, _, coverage = pool_generation(rate, spa, last['at'], now)
        guess = predict(model, last['fc'], g, (now - last['at']) / 86400)
        floor = fc_minimum(last['cya'])
        out['forecast'] = {'from_test_at': last['at'], 'gen_h_since': round(g, 3), 'coverage': round(coverage, 3),
                           'fc_now': guess, 'fc_minimum': floor,
                           'shadow_verdict': None if guess is None or floor is None or coverage < MIN_COVERAGE
                           else 'OK' if guess >= floor else 'LOW', 'advice': False}
    return out
