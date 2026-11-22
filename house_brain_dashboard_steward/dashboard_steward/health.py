"""Dashboard Steward's house_brain_app_health.v1 block (AI family plan layer 1), served at GET /health.

Unknown never reads OK:
* no model is a bad input (``AI_DEGRADED``), so health stays DEGRADED until a qualified local model answers;
* a held kill switch is ``activity: HELD``;
* a lost Home Assistant link is a bad input.
"""
import math
import os

from .app_health import MIN_STALE_S, OVERDUE_CYCLES, SCHEMA_VERSION, validate
from .runtime import VERSION

APP = 'house_brain_dashboard_steward'
INTERVAL_S = 60
ADDRESS_CAP_MB = 256
OBSERVATION_REASONS = {'DISCONNECTED': 'HA_DISCONNECTED', 'STALE': 'HA_STALE', 'UNKNOWN': 'HA_UNKNOWN'}


def _peak_rss_mb():
    try:
        import resource
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except (ImportError, OSError, ValueError):
        return 0


def build(steward, now, db_path=None):
    at = int(now) if isinstance(now, (int, float)) and math.isfinite(now) and now >= 1 else 1
    reasons, activity = [], 'RUNNING'
    if steward.cycle_at is None or steward.observation.get('last_reconcile') is None and not steward.hold:
        activity = 'STARTING'
        reasons.append('NO_CYCLE_YET' if steward.cycle_at is None else 'NO_RECONCILE_YET')
    elif steward.hold:
        activity = 'HELD'
        if steward.hold != 'KILL_SWITCH_FULL_STOP':
            reasons.append(steward.hold)
    inputs = {}
    if activity == 'RUNNING':
        obs = steward.health()['observation']['status']
        inputs['home_assistant'] = obs == 'OK'
        if obs != 'OK':
            reasons.append(OBSERVATION_REASONS.get(obs, 'HA_UNKNOWN'))
        inputs['model'] = steward.inference['status'] == 'AI_OK'
        if not inputs['model']:
            reasons.append('AI_DEGRADED')
    failed = steward.cycle_at is not None and steward.good_at != steward.cycle_at and activity == 'RUNNING'
    if failed:
        reasons.append('CYCLE_FAILED')
    overdue = steward.cycle_at is not None and at - steward.cycle_at > max(MIN_STALE_S, OVERDUE_CYCLES * INTERVAL_S)
    if overdue:
        reasons.append('CYCLE_OVERDUE')
    good = sum(v is True for v in inputs.values())
    bad = sum(v is not True for v in inputs.values())
    data = {'status': 'UNKNOWN' if not good + bad else 'GOOD' if good and not bad else 'DEGRADED',
            'inputs': good + bad, 'good': good, 'idle': 0, 'bad': bad}
    db = None
    if db_path:
        try:
            db = os.path.getsize(db_path)
        except OSError:
            db = None
    peak = _peak_rss_mb()
    doc = {'schema_version': SCHEMA_VERSION, 'app': APP, 'version': VERSION, 'at': at,
           'health': 'FAILED' if failed or overdue else 'DEGRADED' if reasons else 'OK',
           'activity': activity, 'reasons': list(dict.fromkeys(reasons))[:16],
           'cycle': {'interval_s': INTERVAL_S, 'last_at': steward.cycle_at, 'last_good_at': steward.good_at,
                     'last_seconds': steward.cycle_seconds if isinstance(steward.cycle_seconds, float)
                     and 0 <= steward.cycle_seconds <= 3600 else None},
           'data': data,
           'resources': {'peak_rss_mb': peak if isinstance(peak, (int, float)) and 0 <= peak <= 65536 else 0,
                         'db_bytes': db, 'address_cap_mb': ADDRESS_CAP_MB},
           'physical_actions': False, 'control_authority': 'NONE'}
    if validate(doc):
        doc.update(health='FAILED', reasons=['HEALTH_SELF_CHECK_FAILED'],
                   cycle={'interval_s': INTERVAL_S, 'last_at': None, 'last_good_at': None, 'last_seconds': None},
                   data={'status': 'UNKNOWN', 'inputs': 0, 'good': 0, 'idle': 0, 'bad': 0},
                   resources={'peak_rss_mb': 0, 'db_bytes': None, 'address_cap_mb': ADDRESS_CAP_MB})
    return doc
