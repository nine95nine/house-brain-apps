"""The exact Home Assistant ids Water AI reads (design R1 §3), and the small parsing rules they share.

Plant and water data only: no person, presence, location, camera, lock or alarm id, and never
attributes (the transport drops them before anything is kept). Every id here is already public in
the repository. B-hyve keeps watering control; nothing here is a command.
"""
from datetime import datetime
import math
import re

RO = 'sensor.fish_room_ro_tank_pressure_monitor_'
RO_PRESSURE = RO + 'ro_tank_pressure'
RO_IDS = {
    'psi': RO_PRESSURE,
    'trend': RO + 'ro_tank_pressure_trend',
    'sample_age': RO + 'ro_adc_sample_age',
    'signal': RO + 'ro_signal_status',
    'calibration': RO + 'ro_calibration_status',
    'plausible': 'binary_sensor.fish_room_ro_tank_pressure_monitor_ro_pressure_signal_plausible',
    'online': 'binary_sensor.fish_room_ro_tank_pressure_monitor_ro_monitor_online',
}
# R7 probes: read, shown, but NOT commissioned (docs/validation/RO_MONITOR_LIVE_FIRMWARE_OBSERVED_2026-10-26.md).
R7_IDS = {
    'leak_lead': 'binary_sensor.fish_room_ro_tank_pressure_monitor_fish_room_leak_ro_monitor',
    'air_temp': RO + 'fish_room_air_temperature_ro_monitor',
    'air_rh': RO + 'fish_room_air_humidity_ro_monitor',
    'water_temp': RO + 'fish_room_tank_water_temperature_ro_monitor',
}

# The three active B-hyve timers: (zone key, device stem, v2.2 total-gallons id). The pool-equipment timer's
# entities may carry `_2` after its replacement; both spellings are read and the live one is used.
ZONES = (
    ('front_yard', 'tonto_lane_front_of_house', 'sensor.house_brain_irrigation_front_yard_total_gallons'),
    ('back_yard_by_pool', 'hose_timer_back_yard_by_pool', 'sensor.house_brain_irrigation_back_yard_by_pool_total_gallons'),
    ('pool_equipment', 'pool_equipment_smart_hose_time', 'sensor.house_brain_irrigation_pool_equipment_total_gallons'),
)
# Offline timers kept by owner decision 2026-10-10: read live so they join the ledger if they come back.
OFFLINE_STEMS = tuple(
    [f'garden_{n}' for n in ('1', '1_5', '2', '2_5', '3', '3_5', '4', '5', '5_5', '6', '6_5', '7', '8', '8_25',
                             '8_5', '8_75')]
    + [f'pollination_station_{n}' for n in range(1, 5)]
    + ['tree_timer_2', 'tree_timer_4', 'tree_timer_by_ac', 'tree_timer_by_pond', 'smart_hose_timer_5_by_chickens'])
PARTS = {'valve': 'valve.{s}_zone', 'battery': 'sensor.{s}_battery_level', 'next': 'sensor.{s}_next_watering',
         'state': 'sensor.{s}_state', 'fault': 'binary_sensor.{s}_fault', 'rain_delay': 'switch.{s}_rain_delay'}


def zone_ids(stem):
    """{part: (id, id_2)} for one timer stem; the `_2` spelling is the replacement device's."""
    return {part: (tpl.format(s=stem), tpl.format(s=stem) + '_2') for part, tpl in PARTS.items()}


CONTEXT = {
    'data_health': 'sensor.house_brain_irrigation_data_health',
    'accounting_health': 'sensor.house_brain_irrigation_accounting_health',
    'action_code': 'sensor.house_brain_irrigation_action_code',
    'budget_used': 'sensor.house_brain_irrigation_monthly_budget_used',
    'lowest_battery': 'sensor.house_brain_irrigation_lowest_battery',
    'low_battery_threshold': 'input_number.house_brain_irrigation_low_battery_threshold_percent',
    'garden_temp': 'sensor.garden_ble_temperature',
    'garden_rh': 'sensor.garden_ble_humidity',
    'garden_vpd': 'sensor.garden_vpd',
}
KILL_SWITCH = 'sensor.house_brain_ai_kill_switch_effective'

ACTIVE_IDS = frozenset(i for _, stem, total in ZONES for pair in zone_ids(stem).values() for i in pair) \
    | {total for _, _, total in ZONES}
OFFLINE_IDS = frozenset(i for stem in OFFLINE_STEMS for pair in zone_ids(stem).values() for i in pair)
READ = frozenset(RO_IDS.values()) | frozenset(R7_IDS.values()) | ACTIVE_IDS | OFFLINE_IDS \
    | frozenset(CONTEXT.values()) | {KILL_SWITCH}
# History is read only for the series the learner needs (design R1 §3), never for the offline timers.
HISTORY = frozenset({RO_PRESSURE}) | frozenset(
    i for _, stem, total in ZONES for part, pair in zone_ids(stem).items()
    if part in ('valve', 'battery', 'next', 'rain_delay') for i in pair) | {total for _, _, total in ZONES} \
    | {CONTEXT['garden_temp']}
REFUSED_DOMAINS = frozenset({'person', 'device_tracker', 'zone', 'camera', 'lock', 'alarm_control_panel',
                             'media_player'})
RE_TOKEN = re.compile(r'[A-Za-z0-9_.:+| -]{1,128}')
GAP = ('unknown', 'unavailable', '', None)


def number(value, lo=-1e6, hi=1e7):
    """A finite float in range, or None. `unknown`/`unavailable`/text are gaps, never zero."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if math.isfinite(n) and lo <= n <= hi else None


def token(value):
    """A bounded safe word, or None for a gap. Anything else reads 'other', never its text."""
    if not isinstance(value, str) or value in GAP:
        return None
    return value if RE_TOKEN.fullmatch(value) else 'other'


def changed_at(state):
    """When a state row last changed (epoch seconds); 0 when it does not say, so it never wins a tie."""
    try:
        dt = datetime.fromisoformat(str(state.get('last_changed') or '').replace('Z', '+00:00'))
    except ValueError:
        return 0.
    return dt.timestamp() if dt.tzinfo else 0.


def live(states, pair):
    """The state of whichever spelling of a timer entity holds a reading (the one that changed last when both
    do, so a stale old entity never hides its replacement); (None, None) when neither does."""
    held = [(e, states[e]) for e in pair if (states.get(e) or {}).get('state') not in GAP]
    if not held:
        return None, None
    entity, row = max(held, key=lambda x: changed_at(x[1]))
    return entity, row['state']
