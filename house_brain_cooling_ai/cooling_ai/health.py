"""Cooling AI's own house_brain_app_health.v1 block, its one-line digest, and its proposals list,
which is always empty in 0.1: Cooling AI never proposes an action (design R1 §7)."""
import math

from . import __version__
from .app_health import SCHEMA_VERSION, OVERDUE_CYCLES, MIN_STALE_S, validate

APP = 'house_brain_cooling_ai'
INTERVAL_S = 60
ADDRESS_CAP_MB = 256
DIGEST_SCHEMA = 'house_brain_cooling_ai_digest.v1'
PROPOSALS_SCHEMA = 'house_brain_cooling_ai_proposals.v1'
DIGEST_CHARS = 255
INPUT_REASONS = {'core_api': 'CORE_API_UNREADABLE', 'plant': 'PLANT_INPUTS_MISSING',
                 'archive': 'SYSTEMS_AI_ARCHIVE_UNREADABLE'}


def build(runtime, now):
    at = int(now) if isinstance(now, (int, float)) and math.isfinite(now) and now >= 1 else 1
    inputs = {k: v for k, v in runtime.inputs.items() if k in INPUT_REASONS}
    reasons, activity = [], 'RUNNING'
    if runtime.cycle_at is None:
        activity = 'STARTING'
        reasons.append('NO_CYCLE_YET')
    elif runtime.hold is not None:
        activity = 'HELD'
        if runtime.hold != 'KILL_SWITCH_FULL_STOP':
            reasons.append(runtime.hold)
    failed = runtime.cycle_at is not None and runtime.good_at != runtime.cycle_at
    if failed:
        reasons.append('CYCLE_FAILED')
    if activity == 'RUNNING':
        for name, ok in sorted(inputs.items()):
            if ok not in (True, 'IDLE'):
                reasons.append(INPUT_REASONS[name])
    overdue = runtime.cycle_at is not None and at - runtime.cycle_at > max(MIN_STALE_S, OVERDUE_CYCLES * INTERVAL_S)
    if overdue:
        reasons.append('CYCLE_OVERDUE')
    live = activity == 'RUNNING'
    good = sum(v is True for v in inputs.values()) if live else 0
    idle = sum(v == 'IDLE' for v in inputs.values()) if live else 0
    bad = sum(v not in (True, 'IDLE') for v in inputs.values()) if live else 0
    data = {'status': 'UNKNOWN' if not good + bad else 'GOOD' if good and not bad else 'DEGRADED',
            'inputs': good + idle + bad, 'good': good, 'idle': idle, 'bad': bad}
    resources = runtime.status.get('resources') or {}
    peak, db = resources.get('peak_rss_mb'), resources.get('db_bytes')
    doc = {'schema_version': SCHEMA_VERSION, 'app': APP, 'version': __version__, 'at': at,
           'health': 'FAILED' if failed or overdue else 'DEGRADED' if reasons else 'OK',
           'activity': activity, 'reasons': reasons[:16],
           'cycle': {'interval_s': INTERVAL_S, 'last_at': runtime.cycle_at, 'last_good_at': runtime.good_at,
                     'last_seconds': round(runtime.cycle_seconds, 3) if isinstance(runtime.cycle_seconds, float)
                     and 0 <= runtime.cycle_seconds <= 3600 else None},
           'data': data,
           'resources': {'peak_rss_mb': peak if isinstance(peak, (int, float)) and 0 <= peak <= 65536 else 0,
                         'db_bytes': db if type(db) is int and db >= 0 else None, 'address_cap_mb': ADDRESS_CAP_MB},
           'physical_actions': False, 'control_authority': 'NONE'}
    if validate(doc):
        doc.update(health='FAILED', reasons=['HEALTH_SELF_CHECK_FAILED'],
                   cycle={'interval_s': INTERVAL_S, 'last_at': None, 'last_good_at': None, 'last_seconds': None},
                   data={'status': 'UNKNOWN', 'inputs': 0, 'good': 0, 'idle': 0, 'bad': 0},
                   resources={'peak_rss_mb': 0, 'db_bytes': None, 'address_cap_mb': ADDRESS_CAP_MB})
    return doc


def _words(code):
    return code.lower().replace('_', ' ')


def digest(runtime, now):
    """One line (<= 255 characters): today's agreement and how many models are learned. Counts only."""
    me = build(runtime, now)
    status = runtime.status
    if me['activity'] == 'HELD':
        state = 'HELD'
    elif me['health'] != 'OK':
        state = 'UNKNOWN' if me['activity'] == 'STARTING' else me['health']
    else:
        state = 'SHADOW'
    parts = []
    today = (status.get('scorecard') or {}).get('today') or {}
    if state == 'SHADOW':
        if today.get('agreement') is not None:
            parts.append(f"agreed {today['agreement'] * 100:.0f} % of {today['comparable_minutes']:.0f} comparable "
                         'minutes today')
            if today.get('top_differences'):
                parts.append('top difference: ' + _words(today['top_differences'][0]['code']))
        else:
            parts.append('no comparable minutes yet today')
        models = status.get('models') or {}
        parts.append(f"{sum(m.get('status') == 'LEARNED' for m in models.values())}/4 modes learned")
    text = f'Cooling AI {state}: ' + ('; '.join(parts) if parts else 'no current reading') + '.'
    if len(text) > DIGEST_CHARS:
        text = text[:DIGEST_CHARS - 1] + '…'
    return {'schema_version': DIGEST_SCHEMA, 'at': me['at'], 'state': state, 'text': text, 'physical_actions': False}


def proposals(runtime, now):
    """Always empty in 0.1. Any later "tune the Arbiter" idea is its own owner decision and YAML change."""
    me = build(runtime, now)
    return {'schema_version': PROPOSALS_SCHEMA, 'at': me['at'], 'source_health': me['health'], 'proposals': [],
            'executed_by_cooling_ai': False}
