"""Existing Home Assistant solar production forecasts as a read-only reference.

The household's forecast integration already publishes per-plane kWh estimates.
Reading them from the shared states poll adds no provider, API key, polling or
paid call. They are scored against later observed solar, never trusted blindly.
"""
import re
from .contracts import number
from .native import state

FIELDS = frozenset({'next_hour', 'day_ahead', 'max_age_s', 'evidence_ref'})
ENTITY = re.compile(r'^sensor\.[a-z0-9_]{1,100}$')
UNITS = {'kWh': 1.0, 'Wh': 0.001}


def validate_reference(spec):
    if not isinstance(spec, dict) or set(spec) != FIELDS:
        raise ValueError('SOLAR_REFERENCE_FIELDS')
    for field in ('next_hour', 'day_ahead'):
        rows = spec[field]
        if (not isinstance(rows, list) or not 1 <= len(rows) <= 4 or len(set(rows)) != len(rows)
                or any(not isinstance(e, str) or not ENTITY.fullmatch(e) for e in rows)):
            raise ValueError('SOLAR_REFERENCE_ENTITY')
    if type(spec['max_age_s']) is not int or not 900 <= spec['max_age_s'] <= 7200:
        raise ValueError('SOLAR_REFERENCE_AGE')
    if not isinstance(spec['evidence_ref'], str) or not 1 <= len(spec['evidence_ref']) <= 256:
        raise ValueError('SOLAR_REFERENCE_EVIDENCE')
    return spec


def kwh(states, entities, now, max_age):
    total = 0.
    for entity in entities:
        row = state(states, entity, now, fresh=True, max_age=max_age)
        unit = row.get('attributes', {}).get('unit_of_measurement')
        if unit not in UNITS:
            raise ValueError('SOLAR_REFERENCE_UNIT')
        total += number(row['state'], 0, 1000000) * UNITS[unit]
    return total


def observe(states, now, spec):
    """Sum all planes; any missing, stale or non-energy plane makes the reference unavailable."""
    if spec is None:
        return {'status': 'NOT_CONFIGURED', 'external_calls': 0}
    try:
        next_hour = kwh(states, spec['next_hour'], now, spec['max_age_s'])
        day_ahead = kwh(states, spec['day_ahead'], now, spec['max_age_s'])
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return {'status': 'UNAVAILABLE', 'external_calls': 0}
    if next_hour > 100 or day_ahead > 2000:
        return {'status': 'UNAVAILABLE', 'external_calls': 0}
    # kWh over the next clock hour equals its mean power in kW.
    return {'status': 'GOOD', 'next_hour_mean_w': next_hour*1000, 'day_ahead_kwh': day_ahead,
            'source': 'existing Home Assistant forecast entities', 'external_calls': 0,
            'alignment': 'provider next-hour treated as the next clock hour; scored only against later observation'}
