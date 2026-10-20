"""House Brain AI kill-switch interlock consumer (#56). It can only hold work, never loosen.

The AI Access App publishes the effective kill-switch state every 30 seconds. FULL_STOP
holds every Energy AI cycle; READ_ONLY admits READ and ADVISORY work (collection, shadow
analytics) and blocks MUTATION (status publication); ARMED_NORMAL admits all three.
A missing, stale, future-dated or unknown state holds everything.
"""
from .contracts import timestamp

ENTITY = 'sensor.house_brain_ai_kill_switch_effective'
MAX_AGE_S = 300
MAX_FUTURE_S = 60
ADMITS = {'ARMED_NORMAL': frozenset({'READ', 'ADVISORY', 'MUTATION'}),
          'READ_ONLY': frozenset({'READ', 'ADVISORY'})}


def safety_interlock(states, action_class, now):
    """The safety_interlock decision: exactly 'ALLOW' or a blocking reason code; unreadable input blocks."""
    if action_class not in {'READ', 'ADVISORY', 'MUTATION'}:
        return 'KILL_SWITCH_ACTION_CLASS_INVALID'
    row = states.get(ENTITY) if isinstance(states, dict) else None
    if not isinstance(row, dict) or row.get('entity_id') != ENTITY:
        return 'KILL_SWITCH_SENSOR_MISSING'
    try:
        age = now - timestamp(row.get('last_updated') or row['last_changed'])
    except (ValueError, KeyError, TypeError, OverflowError):
        return 'KILL_SWITCH_SENSOR_INVALID'
    if not -MAX_FUTURE_S <= age <= MAX_AGE_S:
        return 'KILL_SWITCH_SENSOR_STALE_OR_FUTURE'
    effective = row.get('state')
    if effective == 'FULL_STOP':
        return 'KILL_SWITCH_FULL_STOP'
    if effective not in ADMITS:
        return 'KILL_SWITCH_STATE_UNKNOWN'
    return 'ALLOW' if action_class in ADMITS[effective] else 'KILL_SWITCH_BLOCKS_' + action_class
