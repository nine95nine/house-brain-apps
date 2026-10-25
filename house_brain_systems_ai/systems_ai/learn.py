"""Small honest learners: robust trends with conformal bands, and a frozen-forecast scorecard.

Forecasts are frozen when made and scored later against what happened, beside a "no change"
baseline (the last value). A trend is shown as a forecast only after it has beaten that baseline
on enough scored days; until then it is an early estimate. Nothing here changes any device.
"""
import math
import statistics

MIN_POINTS = 24
MIN_SPAN_S = 2 * 86400
COVERAGE = 0.9
HORIZONS_DAYS = (1, 7, 30)
MIN_SCORED = 5
BETTER = 0.95
KEEP_FORECASTS = 400


def theil_sen(points):
    """Median of pairwise slopes (per second) and the matching intercept; robust to outliers."""
    pts = points[::max(1, len(points) // 200)]
    slopes = [(y2 - y1) / (x2 - x1) for i, (x1, y1) in enumerate(pts) for x2, y2 in pts[i + 1:] if x2 != x1]
    if not slopes:
        return None
    slope = statistics.median(slopes)
    intercept = statistics.median(y - slope * x for x, y in pts)
    return slope, intercept


def _quantile(values, q):
    ordered = sorted(values)
    k = min(len(ordered) - 1, max(0, math.ceil((len(ordered) + 1) * q) - 1))
    return ordered[k]


def trend(points, now, horizon_s):
    """Value at now+horizon with a split-conformal band; None until there is enough history."""
    points = sorted((float(t), float(v)) for t, v in points if math.isfinite(t) and math.isfinite(v))
    if len(points) < MIN_POINTS or points[-1][0] - points[0][0] < MIN_SPAN_S:
        return None
    cut = len(points) * 2 // 3
    fit = theil_sen(points[:cut])
    if fit is None:
        return None
    # Residuals on the newer third of an older fit, at the same look-ahead distance where possible.
    residuals = [abs(v - (fit[0] * t + fit[1])) for t, v in points[cut:]]
    width = _quantile(residuals, COVERAGE) * (1 + horizon_s / max(1., points[-1][0] - points[cut][0])) ** .5
    slope, intercept = theil_sen(points)
    value = slope * (now + horizon_s) + intercept
    return {'value': value, 'low': value - width, 'high': value + width, 'slope_per_day': slope * 86400,
            'last': points[-1][1], 'points': len(points)}


def days_until(points, now, threshold, rising=True):
    """Days until a trend crosses threshold, with earliest/latest from the band; None when it never does."""
    fit = trend(points, now, 0)
    if fit is None:
        return {'status': 'INSUFFICIENT_HISTORY'}
    slope = fit['slope_per_day']
    gap = threshold - fit['value'] if rising else fit['value'] - threshold
    if gap <= 0:
        return {'status': 'ALREADY_PAST', 'value': fit['value']}
    toward = slope if rising else -slope
    if toward <= 0:
        return {'status': 'NOT_TRENDING_TOWARD', 'slope_per_day': slope}
    band = fit['high'] - fit['value']
    return {'status': 'ESTIMATED', 'days': gap / toward, 'earliest_days': max(0., (gap - band) / toward),
            'latest_days': (gap + band) / toward, 'slope_per_day': slope}


def freeze(store, metric, points, now):
    """Once a day per metric and horizon, freeze the trend forecast and the no-change baseline."""
    day = int(now // 86400)
    book = store.get('forecasts:' + metric, [])
    made = {(f['day'], f['horizon_days']) for f in book}
    for h in HORIZONS_DAYS:
        if (day, h) in made:
            continue
        fit = trend(points, now, h * 86400)
        if fit is None:
            continue
        book.append({'day': day, 'horizon_days': h, 'made_at': now, 'due_at': now + h * 86400,
                     'model': fit['value'], 'low': fit['low'], 'high': fit['high'], 'baseline': fit['last'],
                     'actual': None})
    store.put('forecasts:' + metric, book[-KEEP_FORECASTS:])


def score(store, metric, points, now):
    """Score due forecasts against the nearest actual within one hour; never rewrite a scored row."""
    book = store.get('forecasts:' + metric, [])
    changed = False
    for f in book:
        if f['actual'] is not None or f['due_at'] > now:
            continue
        near = [v for t, v in points if abs(t - f['due_at']) <= 3600]
        if near:
            f['actual'] = near[len(near) // 2]
            changed = True
        elif now - f['due_at'] > 2 * 86400:
            f['actual'] = 'MISSING'
            changed = True
    if changed:
        store.put('forecasts:' + metric, book)
    report = {}
    for h in HORIZONS_DAYS:
        rows = [f for f in book if f['horizon_days'] == h and isinstance(f['actual'], (int, float))]
        if not rows:
            report[str(h)] = {'scored': 0, 'status': 'WAITING'}
            continue
        model = statistics.mean(abs(f['model'] - f['actual']) for f in rows)
        base = statistics.mean(abs(f['baseline'] - f['actual']) for f in rows)
        covered = sum(f['low'] <= f['actual'] <= f['high'] for f in rows) / len(rows)
        proven = len(rows) >= MIN_SCORED and model <= base * BETTER
        report[str(h)] = {'scored': len(rows), 'model_mae': model, 'baseline_mae': base,
                          'band_coverage': covered, 'proven': proven,
                          'status': 'PROVEN' if proven else 'NOT_YET_BETTER' if len(rows) >= MIN_SCORED else 'WAITING'}
    return report


def robust_z(history, value):
    """(value - median) / (1.4826 * MAD); None with fewer than 7 points or no spread."""
    if len(history) < 7:
        return None
    med = statistics.median(history)
    mad = statistics.median(abs(x - med) for x in history) * 1.4826
    if mad == 0:
        # No spread: any change is far outside it; capped so it stays valid JSON.
        return None if value == med else math.copysign(99., value - med)
    return (value - med) / mad


def hourly(rows, max_gap=180):
    """Time-weighted means; insufficient coverage remains missing, never zero (Energy AI forecast.hourly)."""
    buckets = {}
    for (a,va),(b,vb) in zip(rows, rows[1:]):
        if not 0 < b-a <= max_gap:
            continue
        cursor = a
        while cursor < b:
            hour = int(cursor//3600)*3600
            end = min(b,hour+3600)
            total,seconds = buckets.get(hour,(0.,0.))
            buckets[hour]=(total+va*(end-cursor),seconds+end-cursor)
            cursor=end
    return [(h,total/sec) for h,(total,sec) in sorted(buckets.items()) if sec >= 3240]
