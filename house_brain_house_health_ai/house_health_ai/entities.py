"""The exact Home Assistant ids House Health AI reads (design R1 §3), and the small parsing rules they share.

House readings only: no person, presence, location, camera, lock or alarm id. Attributes are dropped
by the transport before anything is kept, with one exception: the sun's elevation (a number), which
NOA 1.0's "solar expected" check needs. Every id here is already public in the repository. Nothing
here is a command.

Sources:
- rooms: Learning/Diagnosis 1.4-R5 (`production/candidates/house-brain-learning/
  house_brain_learning_diagnosis_v1_4_R5.yaml.gz.b64`): the five room sensors, its six periods
  (local hour 0-4, 4-8, 8-12, 12-16, 16-20, 20-24) and its baselines for parity;
- outage: NOA 1.0 (`production/accepted/network-advisory/house_brain_network_outage_advisory_v1_0_R1.yaml`);
- UPS and calendar: Predictive Maintenance 2.3 (`production/reference-sources/house-brain/
  house_brain_predictive_maintenance_v2_3.yaml`) and `docs/validation/UPS_BATTERY_LOOKUP_AND_81_OWNER_INPUTS_2026-10-03.md`;
- mold and cooling context: the Cooling Arbiter v2 ids Cooling AI also reads.
"""
from datetime import datetime
import math
import re

# Room key -> its temperature sensor (Learning 1.4-R5 `_live`/period samples read these).
ROOMS = {
    'living_room': 'sensor.living_room_temperature',
    'master_bedroom': 'sensor.master_bedroom_temperature',
    'ledgers_room': 'sensor.ledgers_bedroom3_temperature',
    'lakes_room': 'sensor.lakes_bedroom_2_temperature',
    'maris_room': 'sensor.maris_bedroom_1_temperature',
}
ROOM_NAMES = {'living_room': 'Living room', 'master_bedroom': 'Master bedroom', 'ledgers_room': "Ledger's room",
              'lakes_room': "Lake's room", 'maris_room': "Mari's room"}
# Learning 1.4-R5 `sensor.house_brain_learning_period`: (slug, label, first hour, end hour), local time.
PERIODS = (('late_overnight', 'Late overnight', 0, 4), ('early_morning', 'Early morning', 4, 8),
           ('morning', 'Morning', 8, 12), ('afternoon', 'Afternoon', 12, 16),
           ('evening', 'Evening', 16, 20), ('night', 'Night', 20, 24))
# Shadow parity: the live Learning baselines (the current period's) and the per-period 7-day means.
LEARNING_BASELINE = {room: f'sensor.house_brain_learning_{room}_baseline' for room in ROOMS}
LEARNING_MEAN = {(room, slug): f'sensor.house_brain_learning_{room}_{slug}_mean_7d'
                 for room in ROOMS for slug, *_ in PERIODS}
LEARNING_PERIOD = 'sensor.house_brain_learning_period'

CONTEXT = {
    'arbiter_phase': 'input_select.cooling_arbiter_v2_phase',
    'outdoor_temp': 'sensor.evap_outdoor_temperature',
}
MOLD = {'rh': 'sensor.cooling_arbiter_v2_indoor_house_humidity',
        'dew_point': 'sensor.cooling_arbiter_v2_indoor_dew_point'}

# NOA 1.0 inputs, and its live sensors for parity.
NETWORK = {
    'ups_status': 'sensor.ups_status_data',
    'wan_ping': 'binary_sensor.house_wan_ping',
    'lan_ping': 'binary_sensor.house_lan_gateway_ping',
    'cloud_down': 'binary_sensor.cloud_services_down',
    'volts': 'sensor.sense_51446_l1_voltage',
    'solar_w': 'sensor.enphase_solar_power_now',
    'sun': 'sun.sun',
}
NOA = {'class': 'sensor.house_brain_network_outage_class', 'wan': 'sensor.house_brain_network_wan_status',
       'lan': 'sensor.house_brain_network_lan_status', 'power': 'sensor.house_brain_network_utility_power'}

UPS = {
    'status': 'sensor.ups_status_data',
    'runtime_min': 'sensor.ups_runtime_minutes',
    'charge': 'sensor.ups_battery_charge',
    'load': 'sensor.ups_load',
    'last_replaced': 'input_datetime.house_brain_ups_battery_last_replaced',
    'date_confirmed': 'input_boolean.house_brain_ups_battery_date_confirmed',
    'interval_days': 'input_number.house_brain_ups_battery_interval_days',
    'minimum_runtime': 'input_number.house_brain_ups_minimum_full_runtime_minutes',
}
CALENDAR = {
    'ac_filter_life': 'sensor.ac_filter_life_remaining',
    'evap_pad_life': 'sensor.evap_pad_life_remaining',
    'smoke_checked': 'input_datetime.house_brain_smoke_batteries_last_checked',
    'smoke_confirmed': 'input_boolean.house_brain_smoke_batteries_date_confirmed',
    'smoke_interval_days': 'input_number.house_brain_smoke_battery_check_interval_days',
}
# Predictive Maintenance 2.3's live status sensors, for the due-date scorecard only.
PM = {
    'ac_filter': 'sensor.house_brain_ac_filter_maintenance_status',
    'evap_pad': 'sensor.house_brain_evap_pad_maintenance_status',
    'ups_battery': 'sensor.house_brain_ups_battery_maintenance_status',
    'smoke_batteries': 'sensor.house_brain_smoke_battery_maintenance_status',
}
KILL_SWITCH = 'sensor.house_brain_ai_kill_switch_effective'

READ = frozenset(ROOMS.values()) | frozenset(LEARNING_BASELINE.values()) | frozenset(LEARNING_MEAN.values()) \
    | {LEARNING_PERIOD} | frozenset(CONTEXT.values()) | frozenset(MOLD.values()) | frozenset(NETWORK.values()) \
    | frozenset(NOA.values()) | frozenset(UPS.values()) | frozenset(CALENDAR.values()) | frozenset(PM.values()) \
    | {KILL_SWITCH}
# History is read only for the series the learners need (design R1 §3).
HISTORY = frozenset(ROOMS.values()) | frozenset(MOLD.values()) | {CONTEXT['arbiter_phase']} \
    | {UPS['status'], UPS['runtime_min'], UPS['charge'], UPS['load']}
# The one attribute kept, and only as a number: the sun's elevation (NOA 1.0's solar-expected check).
ATTRIBUTES = {'sun.sun': ('elevation',)}
REFUSED_DOMAINS = frozenset({'person', 'device_tracker', 'zone', 'camera', 'lock', 'alarm_control_panel',
                             'media_player'})
# Systems AI's shared untrusted-reading list (systems_ai/runtime.py UNTRUSTED, S10). None of those readings
# (Ondilo temperature and pH, the AquaRite CELL-5 seed, the evap local dew point when sources disagree, a null
# B-hyve volume) is read here; tests check this list stays disjoint from READ.
UNTRUSTED_NOT_READ = ('ondilo', 'aquarite', 'evap_local_dew_point', 'bhyve', 'irrigation')
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
    """When a state row last changed (epoch seconds); None when it does not say."""
    try:
        dt = datetime.fromisoformat(str((state or {}).get('last_changed') or '').replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt.timestamp() if dt.tzinfo else None
