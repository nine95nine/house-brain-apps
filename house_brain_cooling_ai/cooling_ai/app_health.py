"""house_brain_app_health.v1: every House Brain App's self-health block (AI family plan layer 1).

Canonical source. Each App carries a byte-identical copy (no shared runtime dependency between
Apps); tests/test_app_health_contract.py checks the copies. Standard library only.

An App serves the block read-only at GET /health on the internal Supervisor network. Systems AI
reads it with observe(): unknown, stale, future-dated or invalid never reads as OK.
"""
import json
import math
import re

SCHEMA_VERSION = 'house_brain_app_health.v1'
HEALTH = ('OK', 'DEGRADED', 'FAILED')
ACTIVITY = ('RUNNING', 'PAUSED', 'HELD', 'STARTING', 'SETUP_REQUIRED')
DATA = ('GOOD', 'DEGRADED', 'UNKNOWN')
OBSERVED = ('OK', 'DEGRADED', 'FAILED', 'STALE', 'INVALID', 'UNREACHABLE')
MAX_BYTES = 4096
MAX_REASONS = 16
OVERDUE_CYCLES = 3
MIN_STALE_S = 120
MAX_FUTURE_S = 60
APP_RE = re.compile(r'house_brain_[a-z0-9_]{1,48}')
VERSION_RE = re.compile(r'[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(-[a-z0-9]{1,16})?')
CODE_RE = re.compile(r'[A-Z][A-Z0-9_]{2,63}')
KEYS = {'schema_version', 'app', 'version', 'at', 'health', 'activity', 'reasons', 'cycle', 'data',
        'resources', 'physical_actions', 'control_authority'}
CYCLE_KEYS = {'interval_s', 'last_at', 'last_good_at', 'last_seconds'}
DATA_KEYS = {'status', 'inputs', 'good', 'idle', 'bad'}
RESOURCE_KEYS = {'peak_rss_mb', 'db_bytes', 'address_cap_mb'}


def _int(value, lo, hi, nullable=False):
    if value is None and nullable:
        return True
    return type(value) is int and lo <= value <= hi


def _num(value, lo, hi, nullable=False):
    if value is None and nullable:
        return True
    return type(value) in (int, float) and math.isfinite(value) and lo <= value <= hi


def overdue(doc):
    """True when the App's own loop has not finished a cycle within OVERDUE_CYCLES intervals."""
    last = doc['cycle']['last_at']
    return last is None or doc['at'] - last > max(MIN_STALE_S, OVERDUE_CYCLES * doc['cycle']['interval_s'])


def validate(doc):
    """Return a sorted list of problems; [] means the block is a valid v1 health block."""
    if not isinstance(doc, dict):
        return ['HEALTH_NOT_OBJECT']
    problems = []
    if set(doc) != KEYS:
        return ['HEALTH_KEYS_INVALID']
    if doc['schema_version'] != SCHEMA_VERSION:
        problems.append('HEALTH_SCHEMA_VERSION_INVALID')
    if not isinstance(doc['app'], str) or not APP_RE.fullmatch(doc['app']):
        problems.append('HEALTH_APP_INVALID')
    if not isinstance(doc['version'], str) or not VERSION_RE.fullmatch(doc['version']):
        problems.append('HEALTH_VERSION_INVALID')
    if not _int(doc['at'], 1, 2**53):
        problems.append('HEALTH_TIME_INVALID')
    if doc['health'] not in HEALTH:
        problems.append('HEALTH_STATE_INVALID')
    if doc['activity'] not in ACTIVITY:
        problems.append('HEALTH_ACTIVITY_INVALID')
    reasons = doc['reasons']
    if (not isinstance(reasons, list) or len(reasons) > MAX_REASONS or len(set(map(str, reasons))) != len(reasons)
            or not all(isinstance(r, str) and CODE_RE.fullmatch(r) for r in reasons)):
        problems.append('HEALTH_REASONS_INVALID')
    cycle, data, res = doc['cycle'], doc['data'], doc['resources']
    if not isinstance(cycle, dict) or set(cycle) != CYCLE_KEYS:
        problems.append('HEALTH_CYCLE_INVALID')
    elif not (_int(cycle['interval_s'], 10, 3600) and _int(cycle['last_at'], 1, 2**53, True)
              and _int(cycle['last_good_at'], 1, 2**53, True) and _num(cycle['last_seconds'], 0, 3600, True)):
        problems.append('HEALTH_CYCLE_INVALID')
    elif type(doc['at']) is int and any(t is not None and t > doc['at'] for t in (cycle['last_at'], cycle['last_good_at'])):
        problems.append('HEALTH_CYCLE_AFTER_REPORT')
    if not isinstance(data, dict) or set(data) != DATA_KEYS:
        problems.append('HEALTH_DATA_INVALID')
    elif (data['status'] not in DATA or not all(_int(data[k], 0, 10000) for k in ('inputs', 'good', 'idle', 'bad'))
          or data['good'] + data['idle'] + data['bad'] != data['inputs']):
        problems.append('HEALTH_DATA_INVALID')
    elif data['status'] == 'GOOD' and (data['bad'] or not data['good']):
        problems.append('HEALTH_DATA_GOOD_WITH_BAD_INPUTS')
    if not isinstance(res, dict) or set(res) != RESOURCE_KEYS or not (
            _num(res['peak_rss_mb'], 0, 65536) and _int(res['db_bytes'], 0, 2**40, True)
            and _int(res['address_cap_mb'], 1, 65536, True)):
        problems.append('HEALTH_RESOURCES_INVALID')
    if doc['physical_actions'] is not False:
        problems.append('HEALTH_PHYSICAL_ACTIONS_NOT_FALSE')
    if doc['control_authority'] != 'NONE':
        problems.append('HEALTH_CONTROL_AUTHORITY_NOT_NONE')
    if problems:
        return sorted(set(problems))
    # Semantic rules: unknown never reads as OK.
    if doc['health'] == 'OK':
        if reasons:
            problems.append('HEALTH_OK_WITH_REASONS')
        if doc['activity'] in ('STARTING', 'SETUP_REQUIRED'):
            problems.append('HEALTH_OK_WHILE_NOT_STARTED')
        if doc['activity'] == 'RUNNING' and data['status'] != 'GOOD':
            problems.append('HEALTH_OK_WITHOUT_GOOD_DATA')
        if cycle['last_good_at'] is None or overdue(doc):
            problems.append('HEALTH_OK_WITHOUT_RECENT_CYCLE')
    elif not reasons:
        problems.append('HEALTH_NOT_OK_WITHOUT_REASON')
    try:
        if len(encode(doc).encode()) > MAX_BYTES:
            problems.append('HEALTH_TOO_LARGE')
    except (TypeError, ValueError):
        problems.append('HEALTH_NOT_JSON')
    return sorted(set(problems))


def encode(doc):
    return json.dumps(doc, allow_nan=False, sort_keys=True, separators=(',', ':'))


def _no_duplicates(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError('HEALTH_DUPLICATE_KEY')
        seen[key] = value
    return seen


def _no_constant(value):
    raise ValueError('HEALTH_NOT_JSON')


def parse(raw):
    """Strict parse of a served body: bounded, no duplicate keys, no NaN/Infinity."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_BYTES:
        raise ValueError('HEALTH_TOO_LARGE')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except UnicodeDecodeError:
        raise ValueError('HEALTH_NOT_JSON')
    except json.JSONDecodeError:
        raise ValueError('HEALTH_NOT_JSON')


def observe(raw, now, expected_app=None):
    """Consumer view (Systems AI). Returns {'status': one of OBSERVED, 'reasons': [...], 'doc': doc|None}.

    None (no answer) is UNREACHABLE; anything unparsable or invalid is INVALID; a block whose own time
    is too old or in the future, or whose App loop is overdue, is STALE. Only a valid, fresh block can
    read OK, and only when the App itself says OK.
    """
    if raw is None:
        return {'status': 'UNREACHABLE', 'reasons': ['HEALTH_UNREACHABLE'], 'doc': None}
    try:
        doc = parse(raw) if isinstance(raw, (bytes, bytearray)) else raw
    except ValueError as error:
        return {'status': 'INVALID', 'reasons': [str(error)], 'doc': None}
    problems = validate(doc)
    if not problems and expected_app is not None and doc['app'] != expected_app:
        problems = ['HEALTH_APP_MISMATCH']
    if problems:
        return {'status': 'INVALID', 'reasons': problems, 'doc': None}
    if not _num(now, 1, 2**53):
        return {'status': 'INVALID', 'reasons': ['HEALTH_NOW_INVALID'], 'doc': None}
    limit = max(MIN_STALE_S, OVERDUE_CYCLES * doc['cycle']['interval_s'])
    if doc['at'] > now + MAX_FUTURE_S:
        return {'status': 'STALE', 'reasons': ['HEALTH_FROM_FUTURE'], 'doc': doc}
    if now - doc['at'] > limit:
        return {'status': 'STALE', 'reasons': ['HEALTH_REPORT_STALE'], 'doc': doc}
    # A block from an App still in its first cycle (no finished cycle yet) is starting, not stale: it reads as the
    # App's own DEGRADED/NO_CYCLE_YET. Once a cycle has finished, a loop that stops finishing them is STALE.
    if doc['cycle']['last_at'] is not None and overdue(doc):
        return {'status': 'STALE', 'reasons': ['HEALTH_CYCLE_OVERDUE'], 'doc': doc}
    return {'status': doc['health'], 'reasons': list(doc['reasons']), 'doc': doc}
