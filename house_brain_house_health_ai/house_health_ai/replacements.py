"""Replacement calendar (design R1 §4.5; absorbs the Predictive Maintenance 2.3 dates).

Items, intervals and leads come from `contracts/house_brain_maintenance_policy_reference.v1.json` and
Predictive Maintenance 2.3 (`production/reference-sources/house-brain/house_brain_predictive_maintenance_v2_3.yaml`):
- A/C filter: life remaining %, DUE at 10 % or less, SOON at 25 % or less;
- evap pad: life remaining %, DUE at 5 % or less, SOON at 20 % or less;
- UPS battery: last replaced (owner-confirmed) + interval (1,095 days); SOON within 60 days; measured
  retention below the healthy band can only bring it earlier (SOON), never later;
- smoke/CO batteries: last checked (owner-confirmed) + interval (365 days); SOON within 30 days;
- smoke alarm end of life (10 years, external guidance) and CO alarm end of life (10 years, Kidde,
  provisional): BLOCKED, `MANUFACTURE_DATE_UNKNOWN`, until the owner reads the labels (decision D4);
- Ecobee room-sensor batteries: BLOCKED, `ECOBEE_SENSOR_BATTERY_DATE_NOT_RECORDED`.
Each item is GOOD, SOON, DUE or BLOCKED, and a BLOCKED item names the missing input. There is no
low-battery rule here: Maintenance owns low-battery and offline alerts. Pure functions; no network.
"""
from datetime import date, timedelta

from .entities import CALENDAR, UPS, number
from .series import local

ITEMS = ('ac_filter', 'evap_pad', 'ups_battery', 'smoke_co_batteries', 'smoke_end_of_life', 'co_end_of_life',
         'ecobee_sensor_batteries')
# Predictive Maintenance 2.3 status words -> calendar status; anything else is BLOCKED there.
PM_WORDS = {'Good': 'GOOD', 'Soon': 'SOON', 'Due': 'DUE', 'Check soon': 'SOON', 'Check due': 'DUE'}
# A known, named reason for a PM 2.3 word that this calendar deliberately does not reproduce.
PM_NAMED = {('ups_battery', 'Check now'): 'PM_CHECK_NOW_LOW_BATTERY_OR_RUNTIME_RULE',
            ('smoke_batteries', 'Replace now'): 'PM_LOW_BATTERY_RULE_MAINTENANCE_OWNS',
            ('smoke_batteries', 'Data unavailable'): 'PM_NEEDS_DETECTOR_BATTERY_LEVELS'}
PM_ITEM = {'ac_filter': 'ac_filter', 'evap_pad': 'evap_pad', 'ups_battery': 'ups_battery',
           'smoke_batteries': 'smoke_co_batteries'}


def parse_date(value):
    """An input_datetime state (date, or date and time) as a local date, or None."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _life(item, value, entity, due_at, soon_at):
    if value is None:
        return {'item': item, 'status': 'BLOCKED', 'missing': entity, 'life_remaining_pct': None}
    status = 'DUE' if value <= due_at else 'SOON' if value <= soon_at else 'GOOD'
    return {'item': item, 'status': status, 'missing': None, 'life_remaining_pct': value}


def _dated(item, last, confirmed, interval, lead, today, ids):
    if confirmed != 'on':
        return {'item': item, 'status': 'BLOCKED', 'missing': ids['confirmed'] + ' (not confirmed)', 'due_date': None}
    if last is None:
        return {'item': item, 'status': 'BLOCKED', 'missing': ids['date'], 'due_date': None}
    if last > today:
        return {'item': item, 'status': 'BLOCKED', 'missing': ids['date'] + ' (in the future)', 'due_date': None}
    due = last + timedelta(days=int(interval))
    left = (due - today).days
    return {'item': item, 'status': 'DUE' if left <= 0 else 'SOON' if left <= lead else 'GOOD', 'missing': None,
            'last': last.isoformat(), 'interval_days': int(interval), 'due_date': due.isoformat(), 'days_left': left}


def calendar(states, now, ups_band=None):
    """The replacement calendar from the live states: a list of items in ITEMS order."""
    val = lambda e: (states.get(e) or {}).get('state')
    today = local(now).date()
    items = [
        _life('ac_filter', number(val(CALENDAR['ac_filter_life']), 0, 100), CALENDAR['ac_filter_life'], 10, 25),
        _life('evap_pad', number(val(CALENDAR['evap_pad_life']), 0, 100), CALENDAR['evap_pad_life'], 5, 20),
    ]
    ups = _dated('ups_battery', parse_date(val(UPS['last_replaced'])), val(UPS['date_confirmed']),
                 number(val(UPS['interval_days']), 30, 3650) or 1095, 60, today,
                 {'confirmed': UPS['date_confirmed'], 'date': UPS['last_replaced']})
    if ups['status'] == 'GOOD' and ups_band == 'WATCH':
        ups = {**ups, 'status': 'SOON', 'moved_earlier': 'MEASURED_RETENTION_BELOW_HEALTHY'}
    items.append(ups)
    items.append(_dated('smoke_co_batteries', parse_date(val(CALENDAR['smoke_checked'])), val(CALENDAR['smoke_confirmed']),
                        number(val(CALENDAR['smoke_interval_days']), 30, 3650) or 365, 30, today,
                        {'confirmed': CALENDAR['smoke_confirmed'], 'date': CALENDAR['smoke_checked']}))
    items.append({'item': 'smoke_end_of_life', 'status': 'BLOCKED', 'missing': 'MANUFACTURE_DATE_UNKNOWN',
                  'life_years': 10, 'basis': 'EXTERNAL_GUIDANCE_CANDIDATE'})
    items.append({'item': 'co_end_of_life', 'status': 'BLOCKED', 'missing': 'MANUFACTURE_DATE_UNKNOWN',
                  'life_years': 10, 'basis': 'KIDDE_PROVISIONAL'})
    items.append({'item': 'ecobee_sensor_batteries', 'status': 'BLOCKED',
                  'missing': 'ECOBEE_SENSOR_BATTERY_DATE_NOT_RECORDED'})
    return items


def pm_parity(items, pm_states):
    """Compare with Predictive Maintenance 2.3's live words: {pm item: {'ours', 'pm', 'result'}}, where result is
    SAME, a named reason, PM_MISSING, or UNEXPLAINED."""
    ours = {i['item']: i['status'] for i in items}
    moved = {i['item'] for i in items if i.get('moved_earlier')}
    out = {}
    for pm_item, word in pm_states.items():
        mine = ours.get(PM_ITEM.get(pm_item))
        if not isinstance(word, str) or word in ('unknown', 'unavailable', ''):
            out[pm_item] = {'ours': mine, 'pm': None, 'result': 'PM_MISSING'}
            continue
        theirs = PM_WORDS.get(word, 'BLOCKED')
        named = PM_NAMED.get((pm_item, word)) or (
            'RETENTION_MOVED_EARLIER' if PM_ITEM.get(pm_item) in moved and theirs != mine else None)
        result = 'SAME' if theirs == mine and not named else named or 'UNEXPLAINED'
        out[pm_item] = {'ours': mine, 'pm': word, 'result': result}
    return out
