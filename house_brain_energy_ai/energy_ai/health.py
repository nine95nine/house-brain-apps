"""Energy AI's house_brain_app_health.v1 block (0.1.2): what GET /health serves.

Built from the cycle status the runtime already keeps; it reads nothing new and writes nothing.
An owner pause or a kill-switch FULL_STOP is OK with activity PAUSED/HELD; an unreadable kill
switch, bad inputs, a resource hold or a failed cycle is not OK; before the first cycle, or when the
loop stops finishing cycles, it is never OK.
"""
import math

from . import __version__
from .app_health import SCHEMA_VERSION, OVERDUE_CYCLES, MIN_STALE_S, validate

APP = 'house_brain_energy_ai'
INTERVAL_S = 60
ADDRESS_CAP_MB = 256
INPUT_REASONS = {'MISSING': 'DATA_INPUT_MISSING', 'STALE_OR_FUTURE': 'DATA_INPUT_STALE_OR_FUTURE',
                 'UNIT_MISMATCH': 'DATA_INPUT_UNIT_MISMATCH', 'INVALID': 'DATA_INPUT_INVALID'}


def _stamp(value):
    if type(value) in (int, float) and math.isfinite(value) and value >= 1:
        return int(value)
    return None


class Tracker:
    """Cycle times, kept in memory; a restart starts again at STARTING."""

    def __init__(self):
        self.last_at = None
        self.last_good_at = None
        self.status = {'state': 'STARTING'}

    def record(self, status, now):
        self.status = status
        self.last_at = _stamp(now)
        if status.get('state') in ('SHADOW', 'DEGRADED', 'PAUSED', 'KILL_SWITCH_HOLD') and 'all' not in (
                status.get('data_quality') or {}):
            self.last_good_at = self.last_at

    def build(self, now):
        return build(self.status, now, self.last_at, self.last_good_at)


def _data(status):
    if status.get('state') in ('KILL_SWITCH_HOLD', 'STARTING') or 'all' in (status.get('data_quality') or {}):
        return {'status': 'UNKNOWN', 'inputs': 0, 'good': 0, 'idle': 0, 'bad': 0}
    samples = [s for s in status.get('samples') or [] if isinstance(s, dict)][:10000]
    good = sum(s.get('quality') == 'GOOD' for s in samples)
    idle = sum(s.get('quality') == 'IDLE' for s in samples)
    bad = len(samples) - good - idle
    state = 'GOOD' if good and not bad else 'DEGRADED' if samples else 'UNKNOWN'
    return {'status': state, 'inputs': len(samples), 'good': good, 'idle': idle, 'bad': bad}


def build(status, now, last_at, last_good_at):
    state = status.get('state')
    data = _data(status)
    reasons = []
    activity = 'RUNNING'
    if state == 'STARTING' or last_at is None:
        activity = 'STARTING'
        reasons.append('NO_CYCLE_YET')
    elif state == 'PAUSED':
        activity = 'PAUSED'
    elif state == 'KILL_SWITCH_HOLD':
        activity = 'HELD'
        if status.get('hold_reason') != 'KILL_SWITCH_FULL_STOP':
            reasons.append(status.get('hold_reason') or 'KILL_SWITCH_STATE_UNKNOWN')
    failed = 'all' in (status.get('data_quality') or {})
    if failed:
        reasons.append('DATA_OR_STORAGE_FAILURE')
    elif state not in ('STARTING', 'KILL_SWITCH_HOLD'):
        for sample in status.get('samples') or []:
            code = INPUT_REASONS.get(sample.get('quality')) if isinstance(sample, dict) else None
            if code is None and isinstance(sample, dict) and sample.get('quality') not in ('GOOD', 'IDLE'):
                code = 'DATA_INPUT_INVALID'
            if code and code not in reasons:
                reasons.append(code)
        if activity == 'RUNNING' and data['status'] != 'GOOD' and not reasons:
            reasons.append('DATA_INPUTS_UNKNOWN')
    resources = status.get('health') or {}
    if resources.get('optional_training_suspended') is True:
        reasons.append('RESOURCE_HOLD')
    if status.get('publication') == 'FAILED':
        reasons.append('STATUS_PUBLICATION_FAILED')
    at = _stamp(now) or 1
    overdue = last_at is not None and at - last_at > max(MIN_STALE_S, OVERDUE_CYCLES * INTERVAL_S)
    if overdue:
        reasons.append('CYCLE_OVERDUE')
    health = 'FAILED' if failed or overdue else 'DEGRADED' if reasons else 'OK'
    peak = resources.get('peak_ram_mb')
    db = resources.get('db_bytes')
    seconds = resources.get('cycle_seconds')
    doc = {'schema_version': SCHEMA_VERSION, 'app': APP, 'version': __version__, 'at': at,
           'health': health, 'activity': activity, 'reasons': reasons[:16],
           'cycle': {'interval_s': INTERVAL_S, 'last_at': last_at, 'last_good_at': last_good_at,
                     'last_seconds': round(seconds, 3) if type(seconds) in (int, float) and math.isfinite(seconds)
                     and 0 <= seconds <= 3600 else None},
           'data': data,
           'resources': {'peak_rss_mb': round(peak, 1) if type(peak) in (int, float) and math.isfinite(peak)
                         and 0 <= peak <= 65536 else 0,
                         'db_bytes': db if type(db) is int and db >= 0 else None,
                         'address_cap_mb': ADDRESS_CAP_MB},
           'physical_actions': False, 'control_authority': 'NONE'}
    if validate(doc):
        # Never serve an invalid block; a fixed FAILED block says so, still with no household data.
        doc.update(health='FAILED', activity=activity, reasons=['HEALTH_SELF_CHECK_FAILED'],
                   cycle={'interval_s': INTERVAL_S, 'last_at': None, 'last_good_at': None, 'last_seconds': None},
                   data={'status': 'UNKNOWN', 'inputs': 0, 'good': 0, 'idle': 0, 'bad': 0},
                   resources={'peak_rss_mb': 0, 'db_bytes': None, 'address_cap_mb': ADDRESS_CAP_MB})
    return doc
