"""The house's existing outdoor-temperature entity as a read-only covariate.

Reading it from the shared states poll adds no provider, key or request. A temperature-aware daily load
model is fitted and scored on later days against a plain average; it is shown, never promoted automatically.
"""
import re
import statistics
from datetime import datetime
from .contracts import number
from .native import state
from .planning import TZ

FIELDS = frozenset({'outdoor_temperature', 'max_age_s', 'evidence_ref'})
ENTITY = re.compile(r'^sensor\.[a-z0-9_]{1,100}$')
BASE_F = 75.
MIN_DAYS = 21


def validate_weather(spec):
    if not isinstance(spec, dict) or set(spec) != FIELDS:
        raise ValueError('WEATHER_REFERENCE_FIELDS')
    if not isinstance(spec['outdoor_temperature'], str) or not ENTITY.fullmatch(spec['outdoor_temperature']):
        raise ValueError('WEATHER_REFERENCE_ENTITY')
    if type(spec['max_age_s']) is not int or not 300 <= spec['max_age_s'] <= 7200:
        raise ValueError('WEATHER_REFERENCE_AGE')
    if not isinstance(spec['evidence_ref'], str) or not 1 <= len(spec['evidence_ref']) <= 256:
        raise ValueError('WEATHER_REFERENCE_EVIDENCE')
    return spec


def observe(states, now, spec):
    """Outdoor temperature in degrees F, or None when missing, stale or not a temperature."""
    if spec is None:
        return None
    try:
        row = state(states, spec['outdoor_temperature'], now, fresh=True, max_age=spec['max_age_s'])
        unit = row.get('attributes', {}).get('unit_of_measurement')
        value = number(row['state'], -60, 160)
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return None
    if unit == '°C':
        return value * 9 / 5 + 32
    return value if unit == '°F' else None


def _fit(points):
    xs, ys = [x for x, _ in points], [y for _, y in points]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in points) / sxx if sxx else 0.
    return my - slope * mx, slope


def weather_load(load_hours, temp_hours):
    """Daily load kWh against cooling degree-hours above 75 F; older two thirds fit, newer third scored."""
    temps = dict(temp_hours or [])
    days = {}
    for at, watts in load_hours or []:
        if at in temps:
            day = days.setdefault(datetime.fromtimestamp(at, TZ).strftime('%Y-%m-%d'), [0., 0., 0])
            day[0] += watts / 1000
            day[1] += max(0., temps[at] - BASE_F)
            day[2] += 1
    rows = [(cdh, kwh) for _, (kwh, cdh, n) in sorted(days.items()) if n >= 20]
    if len(rows) < MIN_DAYS:
        return {'status': 'WAITING', 'missing': ['PAIRED_LOAD_TEMPERATURE_DAYS_BELOW_21'], 'days': len(rows),
                'promoted': False}
    split = len(rows) * 2 // 3
    older, newer = rows[:split], rows[split:]
    intercept, slope = _fit(older)
    flat = statistics.mean(k for _, k in older)
    weather_mae = statistics.mean(abs(intercept + slope * c - k) for c, k in newer)
    flat_mae = statistics.mean(abs(flat - k) for _, k in newer)
    intercept, slope = _fit(rows)
    return {'status': 'SCORED', 'days': len(rows), 'kwh_per_cooling_degree_hour': round(slope, 3),
            'base_kwh_per_day': round(intercept, 1), 'scored_days': len(newer),
            'weather_mae_kwh': round(weather_mae, 2), 'flat_mae_kwh': round(flat_mae, 2),
            'weather_model_better': weather_mae < flat_mae * .95, 'promoted': False,
            'basis': 'shadow comparison only; the deployed forecast and its promotion rules are unchanged'}


def weather_months(temp_hours, sensitivity):
    """Cooling degree-hours per calendar month and the load they imply at the fitted sensitivity."""
    months = {}
    for at, temp in temp_hours or []:
        month = months.setdefault(datetime.fromtimestamp(at, TZ).strftime('%Y-%m'), [0., 0])
        month[0] += max(0., temp - BASE_F)
        month[1] += 1
    out = {m: {'cooling_degree_hours': round(c), 'hours': n} for m, (c, n) in sorted(months.items())}
    if sensitivity is not None:
        for row in out.values():
            row['implied_cooling_kwh'] = round(row['cooling_degree_hours'] * sensitivity, 1)
    return dict(list(out.items())[-13:])
