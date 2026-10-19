"""Bounded installed-instance observations; not source or execution authority.

Production exchange is fixed to Supervisor's internal host and two Deployer info
paths. No caller URL, mutation, redirect, retry or response body is published.
Use in a dedicated Linux main-thread reader. App wiring remains uninstalled.
"""
import hashlib
import http.client
import json
import math
import re
import signal
import threading
import time

from .. import VERSION

SOURCE = 'local_house_brain_deployer'
DESTINATION = re.compile(r'[0-9a-f]{8}_house_brain_deployer')
MAX_BYTES = 65536
MAX_SECONDS = 5
# Explicit published releases plus this exact candidate; membership is compatibility,
# never qualification. The candidate entry follows hbd.VERSION so a renumber cannot
# leave the candidate unable to observe itself. 0.3.13 and 0.3.17 were draft labels, never published.
DESTINATION_VERSIONS = ('0.3.9', '0.3.10', '0.3.11', '0.3.12', '0.3.14', '0.3.15', '0.3.16', '0.3.18', '0.3.19',
                        '0.3.20', '0.3.21', '0.3.22', '0.3.23', '0.3.25', '0.3.26', '0.3.27', '0.3.28', '0.3.29', '0.3.30', VERSION)  # 0.3.29 installed 2026-10-18; 0.3.30 published 2026-10-19


class CustodyError(ValueError): pass
class _Expired(BaseException): pass


def require(value, code):
    if not value: raise CustodyError(code)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _path(path):
    require(type(path) is str and (path == '/addons/'+SOURCE+'/info' or
            re.fullmatch(r'/addons/[0-9a-f]{8}_house_brain_deployer/info', path)), 'READ_SCOPE')


def _token(token):
    require(type(token) is str and 20 <= len(token) <= 4096 and
            all(33 <= ord(c) <= 126 for c in token), 'AUTH_UNAVAILABLE')


def supervisor_exchange(path, token, deadline):
    _path(path); _token(token)
    require(_finite(deadline), 'DEADLINE')
    remaining = deadline-time.monotonic()
    require(0 < remaining <= MAX_SECONDS, 'DEADLINE')
    require(threading.current_thread() is threading.main_thread() and
            threading.active_count() == 1 and signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0),
            'DEDICATED_READER_REQUIRED')
    previous = signal.getsignal(signal.SIGALRM)
    def expire(*_): raise _Expired()
    signal.signal(signal.SIGALRM, expire)
    conn = None
    try:
        signal.setitimer(signal.ITIMER_REAL, remaining)
        conn = http.client.HTTPConnection('supervisor', 80, timeout=min(remaining, 1.0))
        conn.request('GET', path, headers={'Authorization':'Bearer '+token,
                     'Accept':'application/json', 'Accept-Encoding':'identity',
                     'User-Agent':'house-brain-deployer-custody'})
        response = conn.getresponse()
        require(response.status == 200, 'SUPERVISOR_STATUS')
        headers = {}
        for key, value in response.getheaders():
            key = key.lower()
            require(key not in headers, 'RESPONSE_HEADERS')
            headers[key] = value
        require(headers.get('content-encoding', 'identity') == 'identity' and
                headers.get('content-type', '').split(';')[0].strip().lower() == 'application/json',
                'RESPONSE_ENCODING')
        size = headers.get('content-length')
        require(size is None or (size.isascii() and size.isdigit() and int(size) <= MAX_BYTES), 'RESPONSE_SIZE')
        output = bytearray()
        while True:
            require(time.monotonic() < deadline, 'DEADLINE')
            chunk = response.read1(min(8192, MAX_BYTES+1-len(output)))
            require(time.monotonic() < deadline, 'DEADLINE')
            if not chunk: break
            output.extend(chunk)
            require(len(output) <= MAX_BYTES, 'RESPONSE_SIZE')
        require(size is None or len(output) == int(size), 'RESPONSE_TRUNCATED')
        return bytes(output)
    except CustodyError: raise
    except _Expired: raise CustodyError('DEADLINE') from None
    except Exception: raise CustodyError('SUPERVISOR_UNAVAILABLE') from None  # noqa: BLE001 - fail closed to a fixed refusal code
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if conn is not None:
            try: conn.close()
            except Exception: pass  # Never expose private transport exception text.  # noqa: BLE001, S110 - fail closed to a fixed refusal code; fail closed without exposing exception text


def _pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, 'DUPLICATE_JSON')
        result[key] = value
    return result


def _info(raw, slug, version):
    require(type(raw) is bytes and 0 < len(raw) <= MAX_BYTES, 'RESPONSE_SIZE')
    try:
        body = json.loads(raw, object_pairs_hook=_pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(CustodyError('NONFINITE_JSON')))
    except (UnicodeError, ValueError, RecursionError):
        raise CustodyError('RESPONSE_JSON') from None
    require(type(body) is dict and body.get('result') == 'ok' and type(body.get('data')) is dict,
            'SUPERVISOR_RESULT')
    data = body['data']
    require(data.get('slug') == slug and data.get('version') == version and
            data.get('state') == 'stopped', 'INSTANCE_NOT_QUIESCENT')
    require(data.get('boot') in ('auto', 'manual') and type(data.get('watchdog')) is bool,
            'INSTANCE_SHAPE')
    # Discard options and every unrelated field, including credentials.
    return {key:data[key] for key in ('slug', 'version', 'state', 'boot', 'watchdog')}


class SupervisorCustodyReader:
    def __init__(self, token, destination, destination_version, *, transport=supervisor_exchange,
                 clock=time.monotonic):
        _token(token)
        require(type(destination) is str and DESTINATION.fullmatch(destination) and
                type(destination_version) is str and destination_version in DESTINATION_VERSIONS, 'DESTINATION_SCOPE')
        require(callable(transport) and callable(clock), 'READER_DEPENDENCY')
        self._token = token
        self.destination = destination
        self.destination_version = destination_version
        self.transport = transport
        self.clock = clock

    def read(self, nonce):
        require(type(nonce) is str and re.fullmatch(r'[0-9a-f]{32}', nonce), 'OBSERVATION_NONCE')
        transport, clock = self.transport, self.clock
        destination, version, token = self.destination, self.destination_version, self._token
        require(type(destination) is str and DESTINATION.fullmatch(destination) and
                type(version) is str and version in DESTINATION_VERSIONS, 'DESTINATION_SCOPE')
        _token(token)
        start = clock()
        require(_finite(start), 'CLOCK')
        def check():
            now = clock()
            require(_finite(now) and start <= now < start+MAX_SECONDS, 'OBSERVATION_EXPIRED')
            require(self.transport is transport and self.clock is clock and
                    (self.destination, self.destination_version, self._token) == (destination, version, token),
                    'READER_DEPENDENCY_CHANGED')
            return now
        try:
            result = []
            for slug, expected in ((SOURCE, '0.3.6'), (destination, version)):
                check()
                raw = transport('/addons/'+slug+'/info', token, start+MAX_SECONDS)
                check()
                result.append(_info(raw, slug, expected))
            finished = check()
            body = {'schema':'hbd.supervisor_instance_observation.v1', 'nonce':nonce,
                    'source':result[0], 'destination':result[1]}
            digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            return {**body, 'observation_sha256':digest, 'elapsed_seconds':finished-start,
                    'source_verified':False, 'backup_verified':False, 'writer_exclusion_verified':False,
                    'native_qualified':False, 'import_allowed':False, 'uninstall_allowed':False}
        except CustodyError: raise
        except Exception: raise CustodyError('OBSERVATION_UNAVAILABLE') from None  # noqa: BLE001 - fail closed to a fixed refusal code
