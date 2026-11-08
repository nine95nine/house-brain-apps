"""The exact Home Assistant ids Pool AI reads (design R1 §3), and the small parsing rules they share.

Pool readings only: no person, presence, location, camera, lock or alarm id. Attributes are dropped
by the transport before anything is kept (none is needed). Every id here is already public in the
repository. Nothing here is a command.

Sources (verified against the live YAML in the repository):
- manual tests: AquaRite Advisor 1.4-R2 (`production/accepted/aquarite/aquarite_advisor_v1_4_R2.yaml`):
  `input_number.pool_manual_free_chlorine`, `pool_manual_cya`, `pool_manual_ph`, and
  `input_button.pool_log_water_test`, whose state is the test time;
- generation and cell: AquaRite Insight 1.3-R1 (`aquarite_insight_v1_3_R1.yaml`): "AquaRite Generation
  Rate" (output fraction while the pump runs and telemetry is live), "AquaRite Cell Generation Hours"
  (its integral), "AquaRite Cell Life Used", "AquaRite Cell Hours Since Cleaning", the cleaning date;
  spa time from `binary_sensor.aquarite_spa_mode` (AquaRite health alerts 1.3-R1+);
- salt: "AquaRite Salt Level Statistics" (pool mode only: unavailable in spa mode and while not
  reporting), "AquaRite Salt Last Known", and the salt balance package's logged-salt total
  (`aquarite_salt_balance_v1_0_R2.yaml`);
- pump: `switch.pool_pump`, whole-home Sense power `sensor.sense_51446_energy` (W; the pool pump
  timeline recorder samples the same entity), `binary_sensor.aps_on_peak` (APS Legacy ET-2 on-peak);
- water temperature: NOT read as a number. Only `input_boolean.pool_ondilo_calibration_complete` and
  the Jandy `sensor.pool_temp` state are read, to say why temperature is missing (design R1 D3).

Never read (DO_NOT_REGRESS): the old Ondilo probe (ids without `_2`) is the spa's and is never pool
water; no Ondilo id is read at all in 0.1 (pH and temperature are not decision-grade until calibration);
ORP is never read (ORP is not FC); the CELL-5 production seed is quarantined and is not a constant here.
"""
from datetime import datetime
import math
import re

TESTS = {
    'fc': 'input_number.pool_manual_free_chlorine',
    'cya': 'input_number.pool_manual_cya',
    'ph': 'input_number.pool_manual_ph',
    'logged': 'input_button.pool_log_water_test',
}
GENERATION = {
    'rate': 'sensor.aquarite_generation_rate',
    'hours': 'sensor.aquarite_cell_generation_hours',
    'spa': 'binary_sensor.aquarite_spa_mode',
}
CELL = {
    'life_used': 'sensor.aquarite_cell_life_used',
    'since_cleaning': 'sensor.aquarite_cell_hours_since_cleaning',
    'last_cleaned': 'input_datetime.aquarite_cell_last_cleaned',
}
SALT = {
    'level': 'sensor.aquarite_salt_level_statistics',
    'last_known': 'sensor.aquarite_salt_last_known',
    'added_total_lb': 'input_number.aquarite_salt_added_total_lb',
    'weather': 'weather.forecast_home',
}
PUMP = {
    'switch': 'switch.pool_pump',
    'sense_w': 'sensor.sense_51446_energy',
    'on_peak': 'binary_sensor.aps_on_peak',
}
TEMPERATURE = {
    'ondilo_calibrated': 'input_boolean.pool_ondilo_calibration_complete',
    'jandy': 'sensor.pool_temp',
}
KILL_SWITCH = 'sensor.house_brain_ai_kill_switch_effective'

READ = frozenset(TESTS.values()) | frozenset(GENERATION.values()) | frozenset(CELL.values()) \
    | frozenset(SALT.values()) | frozenset(PUMP.values()) | frozenset(TEMPERATURE.values()) | {KILL_SWITCH}
# History is read only for the series the learners need (design R1 §3).
HISTORY = frozenset(TESTS.values()) | {GENERATION['rate'], GENERATION['hours'], GENERATION['spa']} \
    | {SALT['level'], SALT['added_total_lb'], SALT['weather']} | frozenset(PUMP.values())
# No attribute is kept.
ATTRIBUTES = {}
REFUSED_DOMAINS = frozenset({'person', 'device_tracker', 'zone', 'camera', 'lock', 'alarm_control_panel',
                             'media_player'})
# Readings that must never be read here (DO_NOT_REGRESS; Systems AI's untrusted list): any Ondilo reading (all
# are `sensor.tonto_ln_pool_*`; the old probe, without `_2`, is the spa's; `_2` pH and temperature are not
# decision-grade), ORP, and a water temperature number. The Advisor's calibration flag is a helper, not a reading.
NEVER_READ_WORDS = ('tonto_ln', 'oxydo', 'reduction_potential', 'water_temperature')
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


def stamp(value):
    """An ISO time with a zone (the log button's state, a cleaning date) as epoch seconds; None otherwise.
    A plain date is taken as local midnight by the caller, not here."""
    if not isinstance(value, str) or value in GAP:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt.timestamp() if dt.tzinfo else None


def changed_at(state):
    """When a state row last changed (epoch seconds); None when it does not say."""
    return stamp(str((state or {}).get('last_changed') or ''))
