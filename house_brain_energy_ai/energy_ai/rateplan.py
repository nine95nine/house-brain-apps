"""Grandfathered-plan value check and a gated rate-plan what-if. Never recommends leaving.

The household plan (Legacy ET-2 with EPR-6 banking) is grandfathered and leaving it is permanent.
Two read-only views:
* stay_put_value: from retained statements, the imported kWh that banked credits offset and their
  value at the plan's own base energy rates (adjustors, taxes and fees excluded: a lower bound);
* what_if: WAITING until 90+ days of hourly import/export history and at least one owner-verified
  alternative tariff exist; then both plans are priced on the same hours with base charges only.
"""
from datetime import datetime
import re
from .contracts import number
from .planning import TZ, period

MIN_HISTORY_DAYS = 90
MAX_ALTERNATIVES = 4
SPEC_FIELDS = frozenset({'name', 'evidence_ref', 'verified_by_owner', 'daily_charge', 'default_import_rate',
                         'import_windows', 'export_rate', 'demand'})
WINDOW_FIELDS = frozenset({'weekdays_only', 'start_hour', 'end_hour', 'rate'})
DEMAND_FIELDS = frozenset({'per_kw', 'weekdays_only', 'start_hour', 'end_hour'})
NAME = re.compile(r'^[A-Za-z0-9 ._()+/-]{1,64}$')
WARNING = 'LEAVING_THE_GRANDFATHERED_PLAN_IS_PERMANENT'


def season(billing_month):
    return 'summer' if 5 <= billing_month <= 10 else 'winter'


def stay_put_value(bill_packet, tariff):
    """Offset kWh = energy used minus billed, per TOU bucket and statement, at base energy rates."""
    bills = (bill_packet or {}).get('bills') if isinstance(bill_packet, dict) else None
    if not isinstance(bills, list) or not bills:
        return {'status': 'NOT_AVAILABLE', 'reason': 'RETAINED_STATEMENTS_MISSING'}
    rates = tariff['charges']['energy_per_kwh']
    rows = []
    try:
        for bill in bills:
            label = bill['billing_period']
            month = int(label[5:7])
            row = {'billing_period': label, 'offset_kwh': {}, 'value_dollars': 0.}
            for bucket in ('on_peak', 'off_peak'):
                used = number(bill[bucket]['energy_use_kwh'], 0, 100000)
                billed = number(bill[bucket]['billed_kwh'], 0, 100000)
                offset = max(0., used - billed)
                row['offset_kwh'][bucket] = round(offset, 3)
                row['value_dollars'] += offset * rates[season(month)][bucket]
            row['value_dollars'] = round(row['value_dollars'], 2)
            rows.append(row)
    except (KeyError, TypeError, ValueError):
        return {'status': 'NOT_AVAILABLE', 'reason': 'RETAINED_STATEMENTS_UNREADABLE'}
    rows.sort(key=lambda r: r['billing_period'])
    last12 = rows[-12:]
    grandfathering = tariff.get('grandfathering', {})
    return {'status': 'COMPUTED', 'statements': len(rows),
            'last_12_statements_value_dollars': round(sum(r['value_dollars'] for r in last12), 2) if len(last12) == 12 else None,
            'all_statements_value_dollars': round(sum(r['value_dollars'] for r in rows), 2),
            'by_statement': rows[-24:],
            'basis': 'imported kWh offset by banked credits x Legacy ET-2 base energy rate; adjustors, taxes and fees '
                     'excluded, so this is a lower bound of what banking is worth',
            'grandfathering': {'leaving_is_permanent': grandfathering.get('leaving_is_permanent'),
                               'term_years_from_interconnection': grandfathering.get('term_years_from_interconnection'),
                               'interconnection_date': (grandfathering.get('interconnection_date') or {}).get('value'),
                               'term_end_note': (grandfathering.get('interconnection_date') or {}).get('tariff_rule_note')},
            'warning': WARNING, 'recommendation': None, 'physical_actions': False}


def _window(raw, fields, code):
    if not isinstance(raw, dict) or set(raw) != fields or type(raw['weekdays_only']) is not bool:
        raise ValueError(code)
    for key in ('start_hour', 'end_hour'):
        if type(raw[key]) is not int or not 0 <= raw[key] <= 24:
            raise ValueError(code)
    if raw['start_hour'] >= raw['end_hour']:
        raise ValueError(code)
    return raw


def validate_alternatives(rows):
    """Owner-verified tariffs only; any extra, missing or out-of-range field refuses the whole list."""
    if not isinstance(rows, list) or len(rows) > MAX_ALTERNATIVES:
        raise ValueError('RATE_PLAN_ALTERNATIVES_LIST')
    for spec in rows:
        if not isinstance(spec, dict) or set(spec) != SPEC_FIELDS:
            raise ValueError('RATE_PLAN_ALTERNATIVE_FIELDS')
        if not isinstance(spec['name'], str) or not NAME.fullmatch(spec['name']):
            raise ValueError('RATE_PLAN_ALTERNATIVE_NAME')
        if not isinstance(spec['evidence_ref'], str) or not 1 <= len(spec['evidence_ref']) <= 256:
            raise ValueError('RATE_PLAN_ALTERNATIVE_EVIDENCE')
        if spec['verified_by_owner'] is not True:
            raise ValueError('RATE_PLAN_ALTERNATIVE_NOT_VERIFIED')
        for key, high in (('daily_charge', 10), ('default_import_rate', 2), ('export_rate', 2)):
            number(spec[key], 0, high)
        if not isinstance(spec['import_windows'], list) or len(spec['import_windows']) > 4:
            raise ValueError('RATE_PLAN_ALTERNATIVE_WINDOWS')
        for window in spec['import_windows']:
            _window(window, WINDOW_FIELDS, 'RATE_PLAN_ALTERNATIVE_WINDOWS')
            number(window['rate'], 0, 2)
        if spec['demand'] is not None:
            _window(spec['demand'], DEMAND_FIELDS, 'RATE_PLAN_ALTERNATIVE_DEMAND')
            number(spec['demand']['per_kw'], 0, 100)
    return rows


def _in(window, local):
    return (not window['weekdays_only'] or local.weekday() < 5) and window['start_hour'] <= local.hour < window['end_hour']


def _alternative_cost(spec, hours):
    energy = 0.
    demand = {}
    for at, imp_wh, exp_wh in hours:
        local = datetime.fromtimestamp(at, TZ)
        rate = next((w['rate'] for w in spec['import_windows'] if _in(w, local)), spec['default_import_rate'])
        energy += imp_wh / 1000 * rate - exp_wh / 1000 * spec['export_rate']
        if spec['demand'] is not None and _in(spec['demand'], local):
            key = local.strftime('%Y-%m')
            demand[key] = max(demand.get(key, 0.), imp_wh / 1000)      # hourly mean kW
    days = len({datetime.fromtimestamp(at, TZ).date() for at, _, _ in hours})
    per_kw = spec['demand']['per_kw'] if spec['demand'] is not None else 0.
    return energy + days * spec['daily_charge'] + sum(kw * per_kw for kw in demand.values())


def _current_cost(tariff, hours, opening_bank):
    """EPR-6 netting per calendar month and TOU bucket (evidence-bound rule), base energy rates only."""
    months = {}
    for at, imp_wh, exp_wh in hours:
        key = datetime.fromtimestamp(at, TZ).strftime('%Y-%m')
        bucket = months.setdefault(key, {'on_peak': [0., 0.], 'off_peak': [0., 0.]})[period(at)]
        bucket[0] += imp_wh / 1000
        bucket[1] += exp_wh / 1000
    bank = dict(opening_bank)
    cost = 0.
    for key in sorted(months):
        rates = tariff['charges']['energy_per_kwh'][season(int(key[5:7]))]
        for name, (used, earned) in months[key].items():
            net = bank[name] + earned - used
            bank[name] = max(0., net)
            cost += max(0., -net) * rates[name]
    days = len({datetime.fromtimestamp(at, TZ).date() for at, _, _ in hours})
    return cost + days * tariff['charges']['basic_service_charge_per_day']


def what_if(import_rows, export_rows, tariff, alternatives, opening_bank=None):
    imports = dict(import_rows or [])
    exports = dict(export_rows or [])
    hours = sorted((at, imports[at], exports[at]) for at in imports.keys() & exports.keys())
    days = len({datetime.fromtimestamp(at, TZ).date() for at, _, _ in hours})
    missing = []
    if days < MIN_HISTORY_DAYS:
        missing.append('HOURLY_IMPORT_EXPORT_HISTORY_BELOW_90_DAYS')
    if not alternatives:
        missing.append('OWNER_VERIFIED_ALTERNATIVE_TARIFF')
    base = {'warning': WARNING, 'recommendation': None, 'physical_actions': False,
            'basis': 'base charges only (adjustors, taxes and fees excluded); hourly net import/export approximates '
                     'the meter registers; calendar months approximate billing cycles'}
    if missing:
        return {**base, 'status': 'WAITING', 'missing': missing, 'history_days': days}
    bank = {p: number((opening_bank or {}).get(p, 0.), 0, 100000) for p in ('on_peak', 'off_peak')}
    scale = 365. / days
    current = _current_cost(tariff, hours, bank) * scale
    plans = [{'name': spec['name'], 'evidence_ref': spec['evidence_ref'],
              'annualized_dollars': round(_alternative_cost(spec, hours) * scale, 2)} for spec in alternatives]
    for plan in plans:
        plan['difference_vs_current_dollars'] = round(plan['annualized_dollars'] - current, 2)
    return {**base, 'status': 'COMPUTED', 'history_days': days,
            'current_plan_annualized_dollars': round(current, 2), 'alternatives': plans,
            'note': 'A lower alternative figure is not advice to switch: the grandfathered plan cannot be regained.'}
