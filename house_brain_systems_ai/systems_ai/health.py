"""Systems AI's own house_brain_app_health.v1 block, and the family digest Maintenance relays.

The digest is the one family status line for Companion (owner pop-up 2026-10-20 "Maintenance
relays it"): Maintenance reads GET /digest on the internal network and writes the sensor. Systems
AI itself writes nothing to Home Assistant.
"""
import math
import re

from . import __version__
from .app_health import SCHEMA_VERSION, OVERDUE_CYCLES, MIN_STALE_S, validate

APP = 'house_brain_systems_ai'
INTERVAL_S = 60
ADDRESS_CAP_MB = 256
DIGEST_SCHEMA = 'house_brain_systems_ai_digest.v1'
PROPOSALS_SCHEMA = 'house_brain_systems_ai_proposals.v1'
DIGEST_CHARS = 255
LINE_CHARS = 80
# Only these Maintenance page fix kinds, only for a House Brain App slug.
PROPOSAL_KINDS = frozenset({'start_app', 'restart_app'})
RE_TARGET = re.compile(r'(?:local|[0-9a-f]{8})_house_brain_[a-z0-9_]{1,48}')
INPUT_REASONS = {'core_api': 'CORE_API_UNREADABLE', 'supervisor_api': 'SUPERVISOR_UNREADABLE',
                 'websocket': 'CORE_WEBSOCKET_UNREADABLE', 'db_size': 'DB_SIZE_UNREADABLE'}
SHORT = {'house_brain_energy_ai': 'Energy AI', 'house_brain_maintenance': 'Maintenance',
         'house_brain_deployer': 'Deployer', 'house_brain_ai_access': 'AI Access',
         'house_brain_engineering_inspector': 'Inspector', 'house_brain_engineering_observer': 'Observer',
         'house_brain_maintenance_inventory_scout': 'Scout'}


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
            if ok is not True:
                reasons.append(INPUT_REASONS[name])
    overdue = runtime.cycle_at is not None and at - runtime.cycle_at > max(MIN_STALE_S, OVERDUE_CYCLES * INTERVAL_S)
    if overdue:
        reasons.append('CYCLE_OVERDUE')
    good = sum(v is True for v in inputs.values()) if activity == 'RUNNING' else 0
    bad = sum(v is not True for v in inputs.values()) if activity == 'RUNNING' else 0
    data = {'status': 'UNKNOWN' if not good + bad else 'GOOD' if good and not bad else 'DEGRADED',
            'inputs': good + bad, 'good': good, 'idle': 0, 'bad': bad}
    resources = runtime.status.get('resources') or {}
    peak = resources.get('peak_rss_mb')
    db = resources.get('db_bytes')
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


def _fit(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + '…'


def digest(runtime, now):
    """One family status line (<= 255 characters) plus one line per App; counts and states only."""
    me = build(runtime, now)
    status = runtime.status
    view = status.get('view') or {}
    if me['health'] != 'OK' and me['activity'] != 'HELD':
        state = 'UNKNOWN' if me['activity'] == 'STARTING' else me['health']
    elif me['activity'] == 'HELD':
        state = 'HELD'
    else:
        open_ = [p for p in status.get('open_problems') or [] if p.get('owner') != 'maintenance']
        state = 'PROBLEMS' if any(p['severity'] in ('ERROR', 'CRITICAL') for p in open_) else \
            'WATCH' if open_ else 'OK'
    parts = []
    open_ = status.get('open_problems') or []
    if state in ('OK', 'WATCH', 'PROBLEMS'):
        parts.append(f"{len(open_)} open" + (f" (top: {open_[0]['reason'].lower().replace('_', ' ')})" if open_ else ''))
        db = view.get('db_mib')
        if isinstance(db, (int, float)):
            parts.append(f"database {db / 1024:.1f} GB")
        disk = (view.get('disk') or {}).get('free_gb')
        if isinstance(disk, (int, float)):
            parts.append(f"disk {disk:.0f} GB free")
        mem = (view.get('memory') or {}).get('free')
        if isinstance(mem, (int, float)):
            parts.append(f"memory {mem:.0f} MB free")
        m = view.get('maintenance_open_problems')
        if isinstance(m, int):
            parts.append(f"Maintenance {m} open")
    text = _fit(f"House Brain systems {state}: " + ('; '.join(parts) if parts else
                                                     'no current reading') + '.', DIGEST_CHARS)
    apps = {}
    for slug, a in sorted((view.get('apps') or {}).items()):
        name = SHORT.get(a.get('name'), a.get('name'))
        word = a.get('health') or ('running' if a.get('state') == 'started' else a.get('state') or 'unknown')
        apps[a.get('name')] = _fit(f"{name}: {str(word).lower()}" + (f" ({a['activity'].lower()})" if a.get('activity')
                                   and a.get('activity') != 'RUNNING' else ''), LINE_CHARS)
    return {'schema_version': DIGEST_SCHEMA, 'at': me['at'], 'state': state, 'text': text, 'apps': apps,
            'physical_actions': False}


def proposals(runtime, now):
    """What Maintenance may show under "Needs your decision"; never executed by Systems AI."""
    me = build(runtime, now)
    rows = [] if me['activity'] != 'RUNNING' or me['health'] == 'FAILED' else [
        {k: p[k] for k in ('id', 'kind', 'target', 'reason', 'problem', 'opened_at', 'expires_at')}
        for p in runtime.proposals if p['expires_at'] > now and p['kind'] in PROPOSAL_KINDS
        and isinstance(p['target'], str) and RE_TARGET.fullmatch(p['target'])]
    return {'schema_version': PROPOSALS_SCHEMA, 'at': me['at'], 'source_health': me['health'],
            'proposals': rows, 'executed_by_systems_ai': False}
