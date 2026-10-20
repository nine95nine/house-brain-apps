"""Small ridge model vs seasonal baseline, with chronological prequential scoring.

No numerical dependency; a 7-feature linear system, not an LLM or NILM network.
Forecasts carry empirical error bands, not calibrated confidence probabilities.
"""
from datetime import datetime
from zoneinfo import ZoneInfo
import math
import statistics

TZ = ZoneInfo("America/Phoenix")


def features(at, origin=0.):
    d = datetime.fromtimestamp(at, TZ)
    h = (d.hour + d.minute / 60) * math.pi / 12
    return [1., math.sin(h), math.cos(h), math.sin(2*h), math.cos(2*h),
            float(d.weekday() >= 5), (at-origin)/(30*86400)]


def solve(matrix, vector):
    a = [list(row) + [v] for row, v in zip(matrix, vector)]
    for k in range(len(a)):
        pivot = max(range(k, len(a)), key=lambda i: abs(a[i][k]))
        a[k], a[pivot] = a[pivot], a[k]
        if abs(a[k][k]) < 1e-12:
            raise ValueError("SINGULAR_MODEL")
        scale = a[k][k]
        a[k] = [x/scale for x in a[k]]
        for i in range(len(a)):
            if i != k:
                scale = a[i][k]
                a[i] = [x-scale*y for x, y in zip(a[i], a[k])]
    return [r[-1] for r in a]


def fit(rows, ridge=1.):
    if len(rows) < 48 or len(rows) > 9000:
        raise ValueError("TRAINING_SIZE")
    origin=rows[0][0]
    n = len(features(rows[0][0],origin))
    matrix = [[ridge if i == j else 0. for j in range(n)] for i in range(n)]
    vector = [0.] * n
    for at, value in rows:
        x = features(at,origin)
        if not math.isfinite(value) or value < 0 or value > 200000:
            raise ValueError("INVALID_TRAINING_VALUE")
        for i in range(n):
            vector[i] += x[i] * value
            for j in range(n):
                matrix[i][j] += x[i] * x[j]
    return dict(kind="ridge-time-v1", weights=solve(matrix, vector), origin=origin,
                trained_through=rows[-1][0], count=len(rows))


def predict(model, at):
    return max(0., min(200000., sum(a*b for a, b in zip(model["weights"], features(at,model["origin"])))))


def seasonal(rows, at):
    d = datetime.fromtimestamp(at, TZ)
    match = [y for t,y in rows if t < at and datetime.fromtimestamp(t,TZ).hour == d.hour
             and (datetime.fromtimestamp(t,TZ).weekday() >= 5) == (d.weekday() >= 5)]
    if not match:
        match = [y for t,y in rows if t < at]
    return statistics.mean(match) if match else None


def seasonal_many(rows, times):
    """Equivalent future-only baseline in O(history + horizon), not their product."""
    if not times:
        return []
    first=min(times)
    if not rows or any(t>=first for t,_ in rows):
        raise ValueError("BASELINE_FUTURE_ONLY")
    groups={}
    for t,y in rows:
        d=datetime.fromtimestamp(t,TZ)
        groups.setdefault((d.hour,d.weekday()>=5),[]).append(y)
    means={k:statistics.mean(v) for k,v in groups.items()}
    fallback=statistics.mean(y for _,y in rows)
    result=[]
    for at in times:
        d=datetime.fromtimestamp(at,TZ)
        result.append(means.get((d.hour,d.weekday()>=5),fallback))
    return result


def evaluate(rows, minimum_days=14):
    rows = sorted(rows)
    if len({t for t,_ in rows}) != len(rows):
        raise ValueError("DUPLICATE_TRAINING_TIME")
    if not rows or rows[-1][0]-rows[0][0] < minimum_days*86400 or len(rows) < 168:
        return {"status":"INSUFFICIENT_HISTORY", "promote":False}
    cut = int(len(rows)*0.75)
    train, test = rows[:cut], rows[cut:]
    model = fit(train)
    errors = [abs(predict(model,t)-y) for t,y in test]
    baseline = [abs(p-y) for p,(_,y) in zip(seasonal_many(train,[t for t,_ in test]),test)]
    mae, base = statistics.mean(errors), statistics.mean(baseline)
    return dict(status="EVALUATED", promote=mae < base*.95, mae_w=mae, baseline_mae_w=base,
                error_band_w=sorted(errors)[min(len(errors)-1, int(len(errors)*.9))],
                train_end=train[-1][0], test_start=test[0][0], test_end=test[-1][0],
                test_count=len(test), model=fit(rows), weather="NO_WEATHER_FEATURES",
                evaluation="chronological holdout; no operational savings claim")


def hourly(rows, max_gap=180):
    """Time-weighted means; insufficient coverage remains missing, never zero."""
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
