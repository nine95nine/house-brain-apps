"""The exact Home Assistant ids Cooling AI reads, and how a reading becomes a plant mode.

Four truths stay separate (DO_NOT_REGRESS): local intent != recommendation != transition
qualification != permission != actual operation. The Arbiter's phase is its *intent*; what the
house actually did comes from native relay feedback (`sensor.evap_actual_relay_mode`, MEASURED)
and the Ecobee's call (`binary_sensor.ac_compressor_cooling`, INFERRED). Cooling AI's own plan is a
*recommendation* and is only ever compared with actual operation. Sense never proves the A/C ran:
`sensor.air_conditioning_power` is read as energy only, labelled Sense.

Plant and tariff data only: no person, presence, location, camera, lock or alarm id, and never
attributes (the transport drops them before anything is kept).
"""
import math
import re

# key -> entity id. Numbers are kept as floats; everything else as a bounded safe token.
NUMBERS = {
    't_out': 'sensor.evap_local_outdoor_temperature',
    'rh_out': 'sensor.evap_local_outdoor_humidity',
    'dp_out': 'sensor.evap_local_dew_point_consensus',
    'wb': 'sensor.evap_local_wet_bulb_temperature',
    't_in': 'sensor.evap_robust_indoor_temperature',
    'rh_in': 'sensor.cooling_arbiter_v2_indoor_house_humidity',
    'dp_in': 'sensor.cooling_arbiter_v2_indoor_dew_point',
    'attic1': 'sensor.attic_thermometer',
    'attic2': 'sensor.attic_thermometer_2',
    'supply': 'sensor.evap_local_estimated_supply_temperature',     # MODELLED until the #119 probe
    'vent_floor': 'sensor.cooling_arbiter_v2_vent_floor',
    'price': 'sensor.aps_energy_price_now',
    'ac_w': 'sensor.air_conditioning_power',                         # Sense: energy only, never identity
}
# Read live only (not in the Systems AI archive).
LIVE_NUMBERS = {
    'comfort': 'input_number.cooling_arbiter_v2_comfort_target',
    'floor': 'sensor.cooling_arbiter_v2_cooling_floor',
    'charge_target': 'sensor.cooling_arbiter_v2_charge_target',
    'evap_target': 'sensor.cooling_arbiter_v2_evap_target',
    'evap_w': 'sensor.evap_estimated_power_now',                     # MODELLED evap power
    'boost_rh': 'sensor.cooling_humid_boost_projected_rh',
    'bank_kwh': 'sensor.house_brain_epr6_on_peak_bank_estimate',
}
TEXTS = {
    'relay': 'sensor.evap_actual_relay_mode',
    'ac': 'binary_sensor.ac_compressor_cooling',
    'phase': 'input_select.cooling_arbiter_v2_phase',
    'owner': 'input_select.cooling_arbiter_v2_owner',
    'kind': 'input_select.cooling_arbiter_v2_kind',
    'decision': 'sensor.cooling_arbiter_v2_decision',
    'shadow': 'sensor.cooling_arbiter_v2_shadow_selection',
    'wet_ok': 'binary_sensor.cooling_arbiter_v2_wet_evap_eligible',
    'vent_ok': 'binary_sensor.cooling_arbiter_v2_vent_start_ok',
    'vent_stop': 'binary_sensor.cooling_arbiter_v2_vent_comfort_stop',
    'evap_perm': 'binary_sensor.cooling_arbiter_v2_evap_permitted',
}
LIVE_TEXTS = {
    'pause': 'input_boolean.cooling_arbiter_v2_pause_auto',
    'human_until': 'input_datetime.cooling_arbiter_v2_human_override_until',
    'boost': 'input_boolean.cooling_humid_boost_session',
    'charge_ok': 'binary_sensor.cooling_arbiter_v2_charge_opportunity',
    'thermostat': 'climate.ecobee_thermostat',                       # the hvac mode only; attributes dropped
    'evap_mode': 'input_select.evap_mode',
    'panic': 'sensor.evap_panic_temperature_status',
    'on_peak': 'binary_sensor.aps_on_peak',
    'holiday': 'binary_sensor.aps_tou_holiday_today',
}
# Other learners' results, shown as context with their source; never recomputed, never identity.
CONTEXT = {
    'ac_insight_efficiency': 'sensor.house_brain_ac_efficiency_check',
    'ac_insight_baseline': 'sensor.house_brain_ac_health_baseline',
    'session_learning': 'sensor.cooling_session_learning',
    'day_ledger': 'sensor.cooling_day_ledger',
}
KILL_SWITCH = 'sensor.house_brain_ai_kill_switch_effective'
ALL_NUMBERS = {**NUMBERS, **LIVE_NUMBERS}
ALL_TEXTS = {**TEXTS, **LIVE_TEXTS}
READ = frozenset(ALL_NUMBERS.values()) | frozenset(ALL_TEXTS.values()) | frozenset(CONTEXT.values()) | {KILL_SWITCH}
BY_ENTITY = {e: k for k, e in {**ALL_NUMBERS, **ALL_TEXTS}.items()}
ARCHIVE_KEYS = {e: k for k, e in {**NUMBERS, **TEXTS}.items()}
RE_TOKEN = re.compile(r'[A-Za-z0-9_.:+| -]{1,128}')
NUMBER_RANGE = (-80., 20000.)

MODES = ('IDLE', 'VENT', 'WET_EVAP', 'AC')
COOLING_MODES = ('VENT', 'WET_EVAP', 'AC')


def number(value):
    """A finite float in range, or None. `unknown`/`unavailable`/text are gaps, never zero."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if math.isfinite(n) and NUMBER_RANGE[0] <= n <= NUMBER_RANGE[1] else None


def token(value):
    """A bounded safe word, or None for a gap. Anything else reads 'other', never its text."""
    if not isinstance(value, str) or value in ('unknown', 'unavailable', ''):
        return None
    return value if RE_TOKEN.fullmatch(value) else 'other'


def decision(text):
    """`ACTION|PHASE|OWNER|KIND|REASON|FLAG` (the Arbiter's own contract), or None."""
    if not isinstance(text, str):
        return None
    parts = text.split('|')
    if len(parts) != 6 or not all(parts[:5]):
        return None
    return dict(zip(('action', 'phase', 'owner', 'kind', 'reason', 'flag'), parts))


def actual_mode(row):
    """What the plant actually did: native relay feedback plus the Ecobee's call.

    Returns one of MODES, or a reason the moment is not a clean mode: PUMP_ONLY (pre-wet),
    RELAY_CONFLICT, OVERLAP (evap and A/C together), UNKNOWN (feedback missing)."""
    relay, ac = row.get('relay'), row.get('ac')
    if relay is None or ac not in ('on', 'off'):
        return 'UNKNOWN'
    evap = {'Off': None, 'Low Fan Only': 'VENT', 'High Fan Only': 'VENT', 'Low': 'WET_EVAP', 'High': 'WET_EVAP',
            'Pump Only': 'PUMP_ONLY', 'Conflict': 'RELAY_CONFLICT'}.get(relay, 'UNKNOWN')
    if evap in ('PUMP_ONLY', 'RELAY_CONFLICT', 'UNKNOWN'):
        return evap
    if ac == 'on':
        return 'OVERLAP' if evap else 'AC'
    return evap or 'IDLE'


def fan_speed(row):
    return {'Low Fan Only': 'LOW', 'Low': 'LOW', 'High Fan Only': 'HIGH', 'High': 'HIGH', 'Off': 'OFF',
            'Pump Only': 'OFF'}.get(row.get('relay'), 'UNKNOWN')


def pump(row):
    return {'Low': 'ON', 'High': 'ON', 'Pump Only': 'ON', 'Off': 'OFF', 'Low Fan Only': 'OFF',
            'High Fan Only': 'OFF'}.get(row.get('relay'), 'UNKNOWN')


SHADOW_MODE = {'IDLE': 'IDLE', 'AC': 'AC', 'WOULD_START_VENT': 'VENT', 'WOULD_START_EVAP': 'WET_EVAP',
               'WOULD_START_EVAP_PRECOOL': 'WET_EVAP', 'WOULD_TRANSFER_AC_TO_EVAP': 'WET_EVAP'}
