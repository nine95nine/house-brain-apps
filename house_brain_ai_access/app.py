#!/usr/bin/env python3
"""HAOS configuration/identity admission and one native process lifetime.

s6 owns restarting. This wrapper never retries, chooses a remote destination from
options, grants HA authority, or logs credentials. The native process is non-root.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import hmac
import ssl
from datetime import datetime, timezone
from typing import Any
from urllib import error as urlerror, request as urlrequest

# s6 runs this wrapper with `python3 -I`, which ignores PYTHONDONTWRITEBYTECODE. As root it would then
# write __pycache__ into the read-only runtime/ tree when it imports the #56 store, and the next
# start would refuse the changed tree (RUNTIME_MEMBERSHIP): no restart ever succeeded. Found by the
# ARM64 s6 qualifier (run 36884634695) and reproduced locally, 2026-10-01.
sys.dont_write_bytecode = True

ROOT = Path('/opt/house-brain-ai-access')
DATA = Path('/data')
POLICY = Path('/run/house-brain-ai-access')
HOME = Path('/tmp/house-brain-ai-access')
# #56 kill switch. The hash-chained store lives in the App's persistent /data (root only, 0700).
# The MCP child runs as UID 10001 and must never be able to write it, so the wrapper publishes a
# root-owned, read-only copy for it at every start. No view = the child reads FULL_STOP.
KS_STORE = DATA / 'kill_switch'
KS_VIEW = Path('/run/house-brain-ai-kill-switch')
KS_STATES = ('FULL_STOP', 'READ_ONLY')
KS_ACTOR_REF = 'ha-app-options'
# #56 edge heartbeat to the Claude perimeter Worker (owner decision OD_212_HEARTBEAT_KEY, 2026-10-01):
# a heartbeat-only HMAC key, never a Cloudflare API token. Empty URL and key = no heartbeat, no egress.
HEARTBEAT_SCHEMA = 'house_brain_ai_kill_switch.edge_heartbeat.v1'
HEARTBEAT_URL = re.compile(r'https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*'
                           r'\.workers\.dev/v1/kill-switch/heartbeat', re.ASCII)
HEARTBEAT_KEY = re.compile(r'[\x21-\x7e]{32,4096}', re.ASCII)
HEARTBEAT_INTERVAL = 60.0
# `claude_only` (0.1.1-dev, owner approval 2026-10-04 "Build it"): the Claude perimeter needs the heartbeat,
# and before this mode the heartbeat ran only beside the OpenAI tunnel, which needs ChatGPT credentials.
# In this mode the wrapper applies the kill switch and publishes only the heartbeat: no tunnel process, no
# OpenAI egress, no tunnel or Broker capability accepted. It adds no tool, scope or Home Assistant access.
HEARTBEAT_TIMEOUT = 10.0
# `ha_bridge` (0.1.3-dev, owner approval 2026-10-10 "Approve design, build it";
# docs/architecture/AI_KILL_SWITCH_HA_BRIDGE_R1.md). Off by default. When on, every 30 s the wrapper reads the
# owner's HA selector, applies it to the local #56 store (Option A: the store stays authoritative; FULL_STOP
# applies at once, READ_ONLY only up to the App option `kill_switch`, ARMED_NORMAL is refused) and publishes
# the effective state to one HA sensor. The Supervisor token can reach the whole Core API, so the limit is
# enforced here: exactly these two (method, path) pairs, checked before any I/O. No service call, no other
# entity, no device control (CONTROL_AUTHORITY=NONE).
HA_BRIDGE_ORIGIN = 'http://supervisor'
HA_SELECTOR_PATH = '/core/api/states/input_select.house_brain_ai_kill_switch'
HA_EFFECTIVE_PATH = '/core/api/states/sensor.house_brain_ai_kill_switch_effective'
HA_SELECTOR_ENTITY = 'input_select.house_brain_ai_kill_switch'
# 0.1.5-dev (owner pop-up 2026-10-13 "Approve, alerts via Maintenance"): a third pair publishes the AI connector
# sign-in summary (expiry, refused refresh) that the perimeter returns on the heartbeat reply. Still no service
# call and no other entity; the Maintenance App turns this sensor into the phone heads-up.
HA_CONNECTORS_PATH = '/core/api/states/sensor.house_brain_ai_connectors'
HA_ACTIVITY_PATH = '/core/api/states/sensor.house_brain_ai_activity'
HA_BRIDGE_ALLOWED = frozenset({('GET', HA_SELECTOR_PATH), ('POST', HA_EFFECTIVE_PATH), ('POST', HA_CONNECTORS_PATH),
                               ('POST', HA_ACTIVITY_PATH)})
HA_BRIDGE_INTERVAL = 30.0
HA_BRIDGE_TIMEOUT = 5.0
HA_BRIDGE_MAX_BYTES = 65536
HA_BRIDGE_ACTOR_REF = 'ha-kill-switch-tile'
HA_BRIDGE_FRIENDLY_NAME = 'House Brain AI Kill Switch Effective'
HA_REQUEST_STATES = ('FULL_STOP', 'READ_ONLY', 'ARMED_NORMAL')
# s6-overlay v3 starts services with a reset environment unless a run script uses with-contenv. The bridge
# needs only this one variable, so it reads it from the container environment folder instead of widening
# the run script's environment (the run script is unchanged).
S6_CONTAINER_ENV = Path('/run/s6/container_environment')
SUPERVISOR_TOKEN_PATTERN = re.compile(r'[\x21-\x7e]{16,4096}', re.ASCII)
UID = GID = 10001
MANIFEST_SHA = '67eb7e063f422e45272ea7b3133cbf2b36dfe7832753873f2de03aac8b792644'
LOCK_SHA = 'bd5359643ba4e382fe84f72728eb8c1954496fad201eb687babf861c8b2932b5'
SCOPES = 'engineering.read.status,engineering.read.platform,engineering.read.backup'
KEY_PATTERN = re.compile(r'[A-Za-z0-9._~+/-]{20,4096}={0,2}', re.ASCII)
START_WINDOW = 600.0
MAX_STARTS = 3
RESTART_DELAY = 5.0
PERMANENT = 78
# run() return for the owner-chosen `enabled: false` state: still a whole-App stop, but not a fault.
DESIRED_DOWN = 79
# Container exit codes written for s6-overlay before the whole-App halt, so Supervisor shows an
# error (non-zero exit) instead of a plain stop. Only the visible status changes; the halt itself,
# the order (record THEN halt) and "never retry" are unchanged. 143 is avoided on purpose
# (Supervisor reports it as an unhandled SIGTERM).
CONTAINER_RESULT = Path('/run/s6-linux-init-container-results/exitcode')
EXIT_ADMISSION_HALT = 78    # admission/configuration refusal, incl. RESTART_BUDGET_EXHAUSTED
EXIT_CONTAINMENT_HALT = 70  # wrapper lost (s6 256 = signalled) or any unexpected status
# s6-overlay stage 2 (rc.init) waits, via legacy-services/s6-svwait, for this service to be
# seen up. A refusal that ends the service before that makes stage 2 fail, and rc.init then
# halts with exit code 1 (S6_BEHAVIOUR_IF_STAGE2_FAILS=2), overwriting the recorded code
# (observed R2 race). A startup refusal therefore waits, bounded, for stage 2 to finish.
# No native process exists during a refusal; this only makes the reported exit code stable.
STAGE2_SCRIPT = b'/run/s6/basedir/scripts/rc.init'
STAGE2_SETTLE_SECONDS = 5.0


class AdmissionError(ValueError):
    """Constant reason codes only; never insert input values into the message."""


def emit(reason: str, **values: int | str) -> None:
    print(json.dumps({'component': 'house_brain_ai_access', 'reason': reason,
                      'control_authority': 'NONE', **values}), flush=True)


def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise AdmissionError('DUPLICATE_JSON_KEY')
        result[key] = value
    return result


def decode(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(AdmissionError('INVALID_JSON')))
    except (ValueError, UnicodeError, RecursionError):
        raise AdmissionError('INVALID_JSON') from None


def read_file(root: Path, relative: str, limit: int, *, protect: bool = False) -> bytes:
    """Open by directory descriptors; reject links/devices/FIFOs at every level."""
    parts = relative.split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise AdmissionError('PATH_INVALID')
    descriptor = -1
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=descriptor)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
                raise AdmissionError('FILE_TYPE_OR_SIZE')
            if protect:
                if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                    raise AdmissionError('OPTIONS_OWNER_OR_WRITE_PERMISSIONS')
                os.fchmod(stream.fileno(), 0o600)
            raw = stream.read(limit + 1)
            if not 0 < len(raw) <= limit:
                raise AdmissionError('FILE_TYPE_OR_SIZE')
            return raw
    except OSError:
        raise AdmissionError('FILE_READ_REJECTED') from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def options(raw: bytes) -> dict[str, Any]:
    value = decode(raw)
    names = {'enabled', 'tunnel_id', 'tunnel_api_key', 'broker_read_key', 'kill_switch', 'kill_switch_repair',
             'perimeter_heartbeat_url', 'perimeter_heartbeat_key', 'claude_only', 'ha_bridge'}
    optional = {'perimeter_heartbeat_url': '', 'perimeter_heartbeat_key': '',  # absent = '' (heartbeat off)
                'claude_only': False,  # absent = False (older option files stay valid)
                'ha_bridge': False}  # 0.1.3-dev; absent = False (no HA call at all)
    booleans = {'enabled', 'kill_switch_repair', 'claude_only', 'ha_bridge'}
    if type(value) is not dict or not names - set(optional) <= set(value) <= names:
        raise AdmissionError('OPTIONS_SCHEMA_INVALID')
    value = {**optional, **value}
    if any(type(value[k]) is not bool for k in booleans):
        raise AdmissionError('OPTIONS_SCHEMA_INVALID')
    if any(type(value[k]) is not str or len(value[k]) > 4096 for k in names - booleans):
        raise AdmissionError('OPTIONS_SCHEMA_INVALID')
    if value['kill_switch'] not in KS_STATES:
        raise AdmissionError('KILL_SWITCH_OPTION_INVALID')
    if not value['enabled']:
        return value
    url, hb_key = value['perimeter_heartbeat_url'], value['perimeter_heartbeat_key']
    if value['claude_only']:
        # Heartbeat only: no tunnel or Broker capability may sit in the options unused, and the heartbeat
        # settings are required (without them this mode would do nothing but hold the App up).
        if value['tunnel_id'] or value['tunnel_api_key'] or value['broker_read_key']:
            raise AdmissionError('CLAUDE_ONLY_TUNNEL_FIELDS_SET')
        if url == '' or hb_key == '':
            raise AdmissionError('CLAUDE_ONLY_HEARTBEAT_REQUIRED')
        if HEARTBEAT_URL.fullmatch(url) is None or HEARTBEAT_KEY.fullmatch(hb_key) is None:
            raise AdmissionError('HEARTBEAT_OPTIONS_INVALID')
        return value
    if re.fullmatch(r'tunnel_[0-9a-f]{32}', value['tunnel_id']) is None:
        raise AdmissionError('TUNNEL_ID_INVALID')
    for key in ('tunnel_api_key', 'broker_read_key'):
        if not 20 <= len(value[key]) <= 4096 or KEY_PATTERN.fullmatch(value[key]) is None:
            raise AdmissionError('CAPABILITY_INVALID')
    if (url == '') != (hb_key == ''):
        raise AdmissionError('HEARTBEAT_OPTIONS_INCOMPLETE')
    if url and (HEARTBEAT_URL.fullmatch(url) is None or HEARTBEAT_KEY.fullmatch(hb_key) is None):
        raise AdmissionError('HEARTBEAT_OPTIONS_INVALID')
    return value


def verify(root: Path, machine: str | None = None) -> dict[str, Any]:
    raw = read_file(root / 'runtime', 'RUNTIME_SOURCE_MANIFEST.json', 16384)
    if hashlib.sha256(raw).hexdigest() != MANIFEST_SHA:
        raise AdmissionError('SOURCE_MANIFEST_IDENTITY')
    manifest = decode(raw)
    expected = set(manifest['files']) | {'RUNTIME_SOURCE_MANIFEST.json'}
    actual = set()
    for entry in (root / 'runtime').rglob('*'):
        if entry.is_symlink():
            raise AdmissionError('RUNTIME_MEMBERSHIP')
        if entry.is_file():
            actual.add(entry.relative_to(root / 'runtime').as_posix())
    if actual != expected:
        raise AdmissionError('RUNTIME_MEMBERSHIP')
    for name, identity in manifest['files'].items():
        content = read_file(root / 'runtime', name, 65536)
        if len(content) != identity['bytes'] or hashlib.sha256(content).hexdigest() != identity['sha256']:
            raise AdmissionError('RUNTIME_SOURCE_IDENTITY')
    raw = read_file(root, 'vendor.lock.json', 16384)
    if hashlib.sha256(raw).hexdigest() != LOCK_SHA:
        raise AdmissionError('VENDOR_LOCK_IDENTITY')
    arch = {'aarch64': 'aarch64', 'arm64': 'aarch64', 'x86_64': 'amd64'}.get(machine or platform.machine())
    if arch is None:
        raise AdmissionError('ARCHITECTURE_REJECTED')
    asset = decode(raw)['assets'][arch]
    native = read_file(root / 'vendor', 'tunnel-client', 33554432)
    if hashlib.sha256(native).hexdigest() != asset['files']['tunnel-client']:
        raise AdmissionError('NATIVE_BINARY_IDENTITY')
    if native[:6] != b'\x7fELF\x02\x01' or int.from_bytes(native[18:20], 'little') != asset['elf_machine']:
        raise AdmissionError('NATIVE_BINARY_ARCHITECTURE')
    return asset


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def publish_kill_switch_view(store: Any, view: Path) -> None:
    """Copy the verified state record and audit log into a fresh root-owned 0755/0644 directory."""
    # The UID 10001 child must not be able to create or swap entries next to the view, or it could
    # plant a looser view between App lifetimes. Refuse (fail closed) unless the parent is a real
    # directory owned by this wrapper and not group/world-writable (/run on HAOS is root 0755).
    parent = os.lstat(view.parent)
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
            or parent.st_mode & 0o022):
        raise AdmissionError('KILL_SWITCH_VIEW_PARENT_UNSAFE')
    staging = view.with_name(view.name + '.staging')
    _remove_path(staging)
    staging.mkdir(mode=0o755)
    os.chmod(staging, 0o755)
    for source in (store.state_path, store.log_path):
        target = staging / source.name
        target.write_bytes(source.read_bytes())
        os.chmod(target, 0o644)
    _remove_path(view)
    os.rename(staging, view)


def apply_kill_switch(requested: str, *, store_dir: Path | None = None, view: Path | None = None,
                      root: Path | None = None, now: str | None = None) -> str:
    """Owner App option -> audited store transition -> read-only view. Never raises.

    The view is withdrawn first, so any failure leaves the MCP child at FULL_STOP. The store is
    never reinitialized here: a store that does not verify stays unpublished until the owner
    repairs it (runbook), which keeps AI reads blocked.
    """
    # Resolved at call time (like ROOT/DATA/POLICY/HOME in main), never frozen at import.
    store_dir = KS_STORE if store_dir is None else store_dir
    view = KS_VIEW if view is None else view
    root = ROOT if root is None else root
    try:
        _remove_path(view)
        runtime = str(root / 'runtime')
        if runtime not in sys.path:
            sys.path.insert(0, runtime)
        from tools.ai_kill_switch_store import KillSwitchStore  # manifest-verified runtime copy
        stamp = now or datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        store = KillSwitchStore(store_dir)
        if not store.log_path.exists() and not store.state_path.exists():
            store.initialize(now=stamp, actor_ref='ai-access-app')
            emit('KILL_SWITCH_STORE_INITIALIZED_FULL_STOP')
        store.verify()
        current = store.read_view(stamp)
        if current['effective_state'] != requested or current['fail_closed']:
            store.transition(state=requested, actor_class='OWNER_LOCAL_UI', actor_ref=KS_ACTOR_REF,
                             reason_code='OWNER_ROUTINE', now=stamp)
        effective = store.read_view(stamp)
        if effective['fail_closed'] or effective['effective_state'] != requested:
            raise ValueError('KILL_SWITCH_STATE_NOT_APPLIED')
        publish_kill_switch_view(store, view)
        emit('KILL_SWITCH_APPLIED', state=requested, generation=effective['generation'])
        return requested
    except Exception:
        try:
            _remove_path(view)
        except OSError:
            pass
        emit('KILL_SWITCH_FAILED_CLOSED', state='FULL_STOP')
        return 'FULL_STOP'


def edge_heartbeat(view: Any, now_s: int) -> dict[str, Any] | None:
    """The #56 view as an edge heartbeat, or None when there is nothing trustworthy to send.

    A store that does not resolve sends nothing: the edge then goes stale and denies within 300 s
    (dead man). The state is the view's effective state, so a fail-closed view sends FULL_STOP.
    """
    if type(view) is not dict:
        return None
    gen, head, state = view.get('generation'), view.get('record_sha256'), view.get('effective_state')
    if type(gen) is not int or gen < 0 or type(head) is not str or re.fullmatch(r'[0-9a-f]{64}', head) is None:
        return None
    if state not in ('FULL_STOP', 'READ_ONLY', 'ARMED_NORMAL') or view.get('fail_closed') is True:
        state = 'FULL_STOP'
    return {'schema_version': HEARTBEAT_SCHEMA, 'state': state, 'generation': gen,
            'published_at_s': int(now_s), 'head_sha256': head}


def sign_heartbeat(key: str, body: bytes) -> str:
    return 'sha256=' + hmac.new(key.encode('utf-8'), body, hashlib.sha256).hexdigest()


class _NoHeartbeatRedirect(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


HEARTBEAT_USER_AGENT = 'house-brain-ai-access/0.1.9-dev'

# 0.1.5-dev connector heads-up. Perimeter 0.4.4 answers an accepted heartbeat with `X-HB-Connectors`
# (house_brain_ai_connectors.v1): per provider the last good sign-in/refresh, its expiry (90 days idle) and the
# last refused refresh. The newest value is kept here (bounded text, never logged) for the HA bridge to publish.
CONNECTORS_HEADER = 'X-HB-Connectors'
CONNECTORS_SCHEMA = 'house_brain_ai_connectors.v1'
CONNECTORS_MAX_BYTES = 1024
CONNECTORS_WARN_DAYS = 14
CONNECTOR_PROVIDERS = ('claude', 'chatgpt', 'grok')
CONNECTOR_LABELS = {'claude': 'Claude', 'chatgpt': 'ChatGPT', 'grok': 'Grok'}
CONNECTOR_RECONNECT_URLS = {'claude': 'https://claude.ai/customize/connectors',  # owner-given link
                            'chatgpt': 'https://chatgpt.com/', 'grok': 'https://grok.com/'}
CONNECTORS_STALE_S = 3600     # no fresh reply for an hour: the summary is reported UNKNOWN, never OK
_connectors_lock = threading.Lock()
_connectors_latest: dict[str, Any] = {'raw': None, 'at': None}


def remember_connectors(value: Any, now: float) -> None:
    """Keep the newest X-HB-Connectors value (text, bounded). Anything else is ignored."""
    if type(value) is not str or not 0 < len(value.encode('utf-8', 'replace')) <= CONNECTORS_MAX_BYTES:
        return
    with _connectors_lock:
        _connectors_latest['raw'], _connectors_latest['at'] = value, float(now)


def latest_connectors() -> tuple[str | None, float | None]:
    with _connectors_lock:
        return _connectors_latest['raw'], _connectors_latest['at']


def _iso_day(ts: Any) -> str | None:
    return (datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%d')
            if type(ts) is int and ts > 0 else None)


def connectors_payload(raw: str | None, received_at: float | None, now: float) -> dict[str, Any]:
    """The body POSTed to sensor.house_brain_ai_connectors. States, worst first: SIGN_IN_FAILED (a refresh was
    refused after the last good one), EXPIRING_SOON (<= 14 days left), OK, NO_CONNECTOR (none signed in yet),
    UNKNOWN (no fresh perimeter answer). Attributes change at most once a day (no per-cycle timestamps), so Home
    Assistant does not record a new state every 30 s."""
    attributes: dict[str, Any] = {'friendly_name': 'House Brain AI Connectors', 'schema': CONNECTORS_SCHEMA,
                                  'warn_days': CONNECTORS_WARN_DAYS, 'providers': {}, 'needs_owner': []}
    value = None
    if type(raw) is str and isinstance(received_at, (int, float)) and now - received_at <= CONNECTORS_STALE_S:
        try:
            value = json.loads(raw)
        except ValueError:
            value = None
    if (type(value) is not dict or value.get('schema') != CONNECTORS_SCHEMA
            or type(value.get('providers')) is not dict):
        return {'state': 'UNKNOWN', 'attributes': attributes}
    rank = {'NO_CONNECTOR': 0, 'OK': 1, 'EXPIRING_SOON': 2, 'SIGN_IN_FAILED': 3}
    overall = 'NO_CONNECTOR'
    for provider in CONNECTOR_PROVIDERS:
        item = value['providers'].get(provider)
        if type(item) is not dict:
            continue
        last_ok, expires, last_fail = item.get('last_ok_s'), item.get('expires_s'), item.get('last_fail_s')
        last_ok = last_ok if type(last_ok) is int and last_ok > 0 else None
        expires = expires if type(expires) is int and expires > 0 else None
        last_fail = last_fail if type(last_fail) is int and last_fail > 0 else None
        if last_fail is not None and item.get('fail') == 'INVALID_GRANT' and (last_ok is None or last_fail > last_ok):
            state, days = 'SIGN_IN_FAILED', None
        elif last_ok is not None and expires is not None:
            days = max(0, int((expires - now) // 86400))
            state = 'EXPIRING_SOON' if days <= CONNECTORS_WARN_DAYS else 'OK'
        else:
            continue
        attributes['providers'][provider] = {'name': CONNECTOR_LABELS[provider], 'state': state, 'days_left': days,
                                             'expires_on': _iso_day(expires), 'last_used_on': _iso_day(last_ok),
                                             'reconnect_url': CONNECTOR_RECONNECT_URLS[provider]}
        if state != 'OK':
            attributes['needs_owner'].append(provider)
        if rank[state] > rank[overall]:
            overall = state
    return {'state': overall, 'attributes': attributes}


# 0.1.6-dev weekly AI activity (owner pop-up 2026-10-18 "Build all 4 parts (Recommended)", Work Board #942 row 72).
# Perimeter 0.4.5 answers an accepted heartbeat with `X-HB-Activity` (house_brain_ai_activity.v1): per provider the
# last 7 days' request counts (counts only: no sensor id, value, prompt or answer). The newest value is kept here
# (bounded text, never logged) and published as sensor.house_brain_ai_activity by the HA bridge.
ACTIVITY_HEADER = 'X-HB-Activity'
ACTIVITY_SCHEMA = 'house_brain_ai_activity.v1'
ACTIVITY_MAX_BYTES = 1024
ACTIVITY_STALE_S = 3 * 3600   # the perimeter refreshes hourly; three hours without a fresh reply is UNKNOWN
ACTIVITY_COUNTS = ('requests', 'answered', 'refused', 'rate_limited', 'tool_calls', 'items')
ACTIVITY_SCALAR_COUNTS = ('requests', 'refused', 'rate_limited')  # 0.1.7-dev: flat per-AI copies for the snapshot
ACTIVITY_TOP_REFUSED = 3          # 0.1.8-dev: perimeter 0.4.7 top refusal reason codes per AI
ACTIVITY_CODE_RE = re.compile(r'^[A-Z][A-Z0-9_]{1,47}$')
ACTIVITY_REFUSED_MIN = 10     # "many refused": at least 10 and at least a fifth of the week's requests
ACTIVITY_SEEN = DATA / 'ai_activity_seen.json'
_activity_lock = threading.Lock()
_activity_latest: dict[str, Any] = {'raw': None, 'at': None}


def remember_activity(value: Any, now: float) -> None:
    """Keep the newest X-HB-Activity value (text, bounded). Anything else is ignored."""
    if type(value) is not str or not 0 < len(value.encode('utf-8', 'replace')) <= ACTIVITY_MAX_BYTES:
        return
    with _activity_lock:
        _activity_latest['raw'], _activity_latest['at'] = value, float(now)


def latest_activity() -> tuple[str | None, float | None]:
    with _activity_lock:
        return _activity_latest['raw'], _activity_latest['at']


def update_seen(seen: Any, active: list[str], now: float) -> tuple[dict[str, Any], list[str]]:
    """The providers the house has seen reading, and which of `active` are new this week.

    The first summary ever seeds the record silently (first_seen 0 = "already reading before 0.1.6-dev"), so
    an upgrade raises no alarm. A provider is NEW for 7 days after it first reads."""
    record = seen if type(seen) is dict and type(seen.get('providers')) is dict and seen.get('v') == 1 else None
    if record is None:
        return {'v': 1, 'providers': {p: 0 for p in active}}, []
    providers = {p: t for p, t in record['providers'].items() if p in CONNECTOR_PROVIDERS and type(t) in (int, float)}
    for p in active:
        providers.setdefault(p, int(now))
    new = sorted(p for p in active if providers[p] > 0 and now - providers[p] < 7 * 86400)
    return {'v': 1, 'providers': providers}, new


def activity_payload(raw: str | None, received_at: float | None, now: float,
                     new_providers: list[str] | None = None) -> dict[str, Any]:
    """The body POSTed to sensor.house_brain_ai_activity. States: UNUSUAL (a flag below), NORMAL (reads, no
    flag), QUIET (no AI request in 7 days), UNKNOWN (no fresh perimeter answer). Flags: MANY_REFUSED (>= 10 and
    >= 20 % of a provider's requests refused, e.g. kill switch or limits), RATE_LIMITED (the per-minute cap hit),
    NEW_AI (a provider reading for the first time this week). Counts change at most hourly.

    0.1.7-dev (owner pop-up 2026-10-18 "Both of the above", Work Board #942 row 72 follow-up, row 74): the same facts
    also as flat scalars, so the privacy-filtered snapshot (which exports scalar attributes only) can say which AI and
    which flag: flag_summary ("claude:MANY_REFUSED, grok:NEW_AI", "none", or None while UNKNOWN) and, per known AI,
    <ai>_requests, <ai>_refused and <ai>_rate_limited. The nested providers and flags stay as they were.

    0.1.8-dev (owner pop-up 2026-10-18 "Show the reasons (Recommended)", Work Board #942 row 74): perimeter 0.4.7 adds
    each AI's largest refusal reason codes; they are published flat as <ai>_top_refused, for example
    "KS_EDGE_STALE:120, HISTORY_NOT_AVAILABLE:40" ("none" when there were none, None while UNKNOWN or from an older
    perimeter). Only plain codes with positive integer counts are kept, at most three."""
    attributes: dict[str, Any] = {'friendly_name': 'House Brain AI Activity (7 days)', 'schema': ACTIVITY_SCHEMA,
                                  'from': None, 'to': None, 'total_requests': 0, 'providers': {}, 'flags': [],
                                  'flag_summary': None}
    for provider in CONNECTOR_PROVIDERS:
        for key in ACTIVITY_SCALAR_COUNTS:
            attributes[f'{provider}_{key}'] = 0
        attributes[f'{provider}_top_refused'] = None
    value = None
    if type(raw) is str and isinstance(received_at, (int, float)) and now - received_at <= ACTIVITY_STALE_S:
        try:
            value = json.loads(raw)
        except ValueError:
            value = None
    if (type(value) is not dict or value.get('schema') != ACTIVITY_SCHEMA
            or type(value.get('providers')) is not dict):
        return {'state': 'UNKNOWN', 'attributes': attributes}
    for key in ('from', 'to'):
        day = value.get(key)
        attributes[key] = day if type(day) is str and len(day) == 10 and day[4] == '-' and day[7] == '-' else None
    new = set(new_providers or ())
    for provider in CONNECTOR_PROVIDERS:
        item = value['providers'].get(provider)
        if type(item) is not dict:
            continue
        top = item.get('top_refused')
        if type(top) is list:
            pairs = [f'{p[0]}:{p[1]}' for p in top[:ACTIVITY_TOP_REFUSED]
                     if type(p) is list and len(p) == 2 and type(p[0]) is str and ACTIVITY_CODE_RE.match(p[0])
                     and type(p[1]) is int and p[1] > 0]
            attributes[f'{provider}_top_refused'] = ', '.join(pairs) or 'none'
        counts = {k: item[k] if type(item.get(k)) is int and item[k] >= 0 else 0 for k in ACTIVITY_COUNTS}
        if not counts['requests']:
            continue
        attributes['providers'][provider] = {'name': CONNECTOR_LABELS[provider], **counts}
        for key in ACTIVITY_SCALAR_COUNTS:
            attributes[f'{provider}_{key}'] = counts[key]
        attributes['total_requests'] += counts['requests']
        if counts['refused'] >= ACTIVITY_REFUSED_MIN and counts['refused'] * 5 >= counts['requests']:
            attributes['flags'].append({'provider': provider, 'flag': 'MANY_REFUSED'})
        if counts['rate_limited'] > 0:
            attributes['flags'].append({'provider': provider, 'flag': 'RATE_LIMITED'})
        if provider in new:
            attributes['flags'].append({'provider': provider, 'flag': 'NEW_AI'})
    attributes['flag_summary'] = ', '.join(f"{f['provider']}:{f['flag']}" for f in attributes['flags']) or 'none'
    state = 'UNUSUAL' if attributes['flags'] else 'NORMAL' if attributes['total_requests'] else 'QUIET'
    return {'state': state, 'attributes': attributes}


def activity_sensor(raw: str | None, received_at: float | None, now: float, seen_path: Path | None = None) -> dict[str, Any]:
    """activity_payload with the persisted seen-providers record. A record that cannot be read or written only
    drops the NEW_AI flag; it never changes the counts or the other flags."""
    seen_path = ACTIVITY_SEEN if seen_path is None else seen_path
    payload = activity_payload(raw, received_at, now)
    if payload['state'] == 'UNKNOWN':
        return payload
    active = sorted(payload['attributes']['providers'])
    try:
        try:
            seen = json.loads(seen_path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            seen = None
        record, new = update_seen(seen, active, now)
        if record != seen:
            tmp = seen_path.with_name(seen_path.name + '.tmp')
            tmp.write_text(json.dumps(record, sort_keys=True), encoding='utf-8')
            os.replace(tmp, seen_path)
    except Exception:
        return payload
    return activity_payload(raw, received_at, now, new) if new else payload


def post_heartbeat(url: str, key: str, heartbeat: dict[str, Any], *, opener: Any = None) -> int:
    """One signed POST. Returns the HTTP status (0 on transport failure). Never raises, never logs bodies."""
    body = json.dumps(heartbeat, sort_keys=True, separators=(',', ':')).encode('ascii')
    # 0.1.2-dev (live 2026-10-06): Cloudflare's edge answers 403 to Python's default "Python-urllib" user agent
    # before the Worker runs, so the App names itself (as the Maintenance App, Observer and Scout do).
    req = urlrequest.Request(url, data=body, method='POST', headers={
        'content-type': 'application/json', 'X-HB-Heartbeat-Signature': sign_heartbeat(key, body),
        'User-Agent': HEARTBEAT_USER_AGENT})
    if opener is None:
        # No proxy, no redirect, verified TLS: the key reaches only the exact configured Worker URL.
        opener = urlrequest.build_opener(urlrequest.ProxyHandler({}), _NoHeartbeatRedirect(),
                                         urlrequest.HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(req, timeout=HEARTBEAT_TIMEOUT) as response:
            status = int(response.status)
            if status == 204:  # 0.1.5-dev: perimeter 0.4.4 connector summary (absent from older perimeters)
                try:
                    remember_connectors(response.headers.get(CONNECTORS_HEADER), time.time())
                except Exception:
                    pass  # bookkeeping only: never changes the heartbeat result
                try:  # 0.1.6-dev: perimeter 0.4.5 activity counts (absent from older perimeters)
                    remember_activity(response.headers.get(ACTIVITY_HEADER), time.time())
                except Exception:
                    pass
            return status
    except urlerror.HTTPError as exc:
        return int(exc.code)
    except Exception:
        return 0


def heartbeat_loop(stop: threading.Event, url: str, key: str, *, store_dir: Path | None = None,
                   root: Path | None = None, clock=time.time, post=post_heartbeat,
                   interval: float = HEARTBEAT_INTERVAL) -> None:
    """Publish the kill-switch heartbeat every interval until stop is set. Never raises."""
    store_dir = KS_STORE if store_dir is None else store_dir
    root = ROOT if root is None else root
    last_reason = None
    while not stop.is_set():
        reason = 'HEARTBEAT_NOT_SENT_STORE_UNREADABLE'
        try:
            runtime = str(root / 'runtime')
            if runtime not in sys.path:
                sys.path.insert(0, runtime)
            from tools.ai_kill_switch_store import KillSwitchStore  # manifest-verified runtime copy
            now = clock()
            stamp = datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            heartbeat = edge_heartbeat(KillSwitchStore(store_dir).read_view(stamp), int(now))
            if heartbeat is not None:
                status = post(url, key, heartbeat)
                reason = 'HEARTBEAT_ACCEPTED' if status == 204 else f'HEARTBEAT_REFUSED_{status}'
        except Exception:
            reason = 'HEARTBEAT_NOT_SENT_STORE_UNREADABLE'
        if reason != last_reason:  # log changes only, never per beat
            emit(reason)
            last_reason = reason
        stop.wait(interval)


def run_heartbeat_only(url: str, key: str, *, loop=heartbeat_loop) -> int:
    """`claude_only` lifetime: publish the heartbeat until s6 stops the App (TERM/INT). Returns 0.

    No native process and no OpenAI egress. On stop the edge simply stops hearing from the house and
    denies within the 300 s staleness window (dead man); no synthetic FULL_STOP is sent, because it would
    raise the edge generation mark above the store and block the next start.
    """
    stop = threading.Event()
    old = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        loop(stop, url, key)
        return 0
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


class HaBridgeAllowlistError(RuntimeError):
    """Programming error: a (method, path) outside the two-pair allowlist. Raised before any I/O."""


class HaBridgeUnavailableError(Exception):
    """Constant reason code only; never a token, body or URL."""


_KS_RANK = {'FULL_STOP': 0, 'READ_ONLY': 1, 'ARMED_NORMAL': 2}


def ha_bridge_url(method: str, path: str) -> str:
    """The only way to build a bridge URL: exactly two (method, path) pairs, nothing else."""
    if (method, path) not in HA_BRIDGE_ALLOWED:
        raise HaBridgeAllowlistError('HA_BRIDGE_PATH_NOT_ALLOWLISTED')
    return HA_BRIDGE_ORIGIN + path


def supervisor_token(*, environ: Any = None, env_dir: Path | None = None) -> str:
    """The Supervisor-provided App token (never logged). Missing or malformed = HA_BRIDGE_TOKEN_MISSING."""
    environ = os.environ if environ is None else environ
    token = environ.get('SUPERVISOR_TOKEN', '')
    if not token:
        try:
            raw = read_file(S6_CONTAINER_ENV if env_dir is None else env_dir, 'SUPERVISOR_TOKEN', 4096)
            token = raw.decode('ascii').strip()
        except (AdmissionError, UnicodeError):
            token = ''
    if type(token) is not str or SUPERVISOR_TOKEN_PATTERN.fullmatch(token) is None:
        raise HaBridgeUnavailableError('HA_BRIDGE_TOKEN_MISSING')
    return token


def ha_bridge_http(method: str, path: str, body: Any = None, *, opener: Any = None,
                   token: str | None = None) -> tuple[int, bytes]:
    """One local Supervisor Core API call. No proxy, no redirect, bounded time and size. Returns (status, body)."""
    url = ha_bridge_url(method, path)  # before the token is read and before any I/O
    token = supervisor_token() if token is None else token
    headers = {'Authorization': 'Bearer ' + token, 'User-Agent': HEARTBEAT_USER_AGENT}
    data = None
    if method == 'POST':
        data = json.dumps(body, sort_keys=True, separators=(',', ':')).encode('ascii')
        headers['Content-Type'] = 'application/json'
    req = urlrequest.Request(url, data=data, method=method, headers=headers)
    if opener is None:
        opener = urlrequest.build_opener(urlrequest.ProxyHandler({}), _NoHeartbeatRedirect())
    try:
        with opener.open(req, timeout=HA_BRIDGE_TIMEOUT) as response:
            return int(response.status), response.read(HA_BRIDGE_MAX_BYTES + 1)
    except urlerror.HTTPError as exc:
        return int(exc.code), b''
    except Exception:
        raise HaBridgeUnavailableError('HA_BRIDGE_HA_UNREACHABLE') from None


def ha_bridge_call(http: Any, method: str, path: str, body: Any = None) -> tuple[int, bytes]:
    """Allowlist first, then the transport. A transport failure is HA_BRIDGE_HA_UNREACHABLE."""
    ha_bridge_url(method, path)
    try:
        status, raw = http(method, path, body)
    except (HaBridgeUnavailableError, HaBridgeAllowlistError):
        raise
    except Exception:
        raise HaBridgeUnavailableError('HA_BRIDGE_HA_UNREACHABLE') from None
    if type(status) is not int or type(raw) is not bytes:
        raise HaBridgeUnavailableError('HA_BRIDGE_HA_UNREACHABLE')
    return status, raw


def parse_selector(status: int, raw: bytes) -> str:
    """The owner's requested state from the selector reply, or HaBridgeUnavailableError (no loosening)."""
    if status in (401, 403):
        raise HaBridgeUnavailableError('HA_BRIDGE_UNAUTHORIZED')
    if status == 404:
        raise HaBridgeUnavailableError('HA_BRIDGE_SELECTOR_MISSING')
    if status != 200:
        raise HaBridgeUnavailableError('HA_BRIDGE_HA_HTTP_ERROR')
    if not 0 < len(raw) <= HA_BRIDGE_MAX_BYTES:
        raise HaBridgeUnavailableError('HA_BRIDGE_SELECTOR_INVALID')
    try:
        value = decode(raw)
    except AdmissionError:
        raise HaBridgeUnavailableError('HA_BRIDGE_SELECTOR_INVALID') from None
    if (type(value) is not dict or value.get('entity_id') != HA_SELECTOR_ENTITY
            or type(value.get('state')) is not str or value['state'] not in HA_REQUEST_STATES):
        raise HaBridgeUnavailableError('HA_BRIDGE_SELECTOR_INVALID')
    return value['state']


def ha_bridge_target(requested: str, ceiling: str, current: str) -> tuple[str, str]:
    """(target store state, reason). Never looser than the App option ceiling.

    FULL_STOP applies at once. READ_ONLY only when the App option `kill_switch` is READ_ONLY. ARMED_NORMAL is
    refused: it never loosens anything, so the target is the stricter of the current state, READ_ONLY and the
    ceiling. Anything unexpected keeps the current state or tightens; it never loosens.
    """
    if ceiling not in KS_STATES or current not in _KS_RANK:
        reason = 'HA_BRIDGE_CEILING_INVALID'
        return 'FULL_STOP', reason
    strictest = lambda *states: min(states, key=_KS_RANK.__getitem__)  # noqa: E731
    if requested == 'FULL_STOP':
        reason = 'HA_BRIDGE_FULL_STOP'
        return 'FULL_STOP', reason
    if requested == 'READ_ONLY':
        if ceiling != 'READ_ONLY':
            reason = 'HA_BRIDGE_CEILING_FULL_STOP'
            return 'FULL_STOP', reason
        reason = 'HA_BRIDGE_READ_ONLY'
        return 'READ_ONLY', reason
    if requested == 'ARMED_NORMAL':
        reason = 'HA_BRIDGE_ARMED_NORMAL_REFUSED'
        return strictest(current, 'READ_ONLY', ceiling), reason
    reason = 'HA_BRIDGE_SELECTOR_INVALID'
    return strictest(current, ceiling), reason


def ha_effective_payload(view: Any, reason: str, stamp: str) -> dict[str, Any]:
    """The exact body POSTed to sensor.house_brain_ai_kill_switch_effective."""
    ok = type(view) is dict and view.get('fail_closed') is False and view.get('effective_state') in _KS_RANK
    generation = view.get('generation') if type(view) is dict else None
    return {'state': view['effective_state'] if ok else 'FULL_STOP',
            'attributes': {'generation': generation if type(generation) is int else None, 'reason_code': reason,
                           'updated_at': stamp, 'friendly_name': HA_BRIDGE_FRIENDLY_NAME}}


def ha_bridge_cycle(ceiling: str, *, publish_view: bool = True, http: Any = None, store_dir: Path | None = None,
                    view: Path | None = None, root: Path | None = None, now: float | None = None) -> dict[str, Any]:
    """One bridge pass: read the selector, apply it to the #56 store, publish the effective state.

    Store writes go only through KillSwitchStore.transition (hash chain, staged recovery, authority checks)
    as OWNER_LOCAL_UI. HA errors never loosen: the stored state is kept. A fail-closed store is never
    loosened here (restart or repair fixes it). A publish failure changes nothing on the AI side.
    Raises only HaBridgeAllowlistError (a programming error); everything else is a reason code.
    """
    store_dir = KS_STORE if store_dir is None else store_dir
    view = KS_VIEW if view is None else view
    root = ROOT if root is None else root
    http = ha_bridge_http if http is None else http
    clock = time.time() if now is None else now
    stamp = datetime.fromtimestamp(clock, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    result: dict[str, Any] = {'reason': 'HA_BRIDGE_CYCLE_FAILED', 'requested': None, 'effective': 'FULL_STOP',
                              'generation': None, 'transition': None, 'publish_status': None}
    try:
        runtime = str(root / 'runtime')
        if runtime not in sys.path:
            sys.path.insert(0, runtime)
        from tools.ai_kill_switch_store import KillSwitchStore  # manifest-verified runtime copy
        store = KillSwitchStore(store_dir)
        try:
            requested = parse_selector(*ha_bridge_call(http, 'GET', HA_SELECTOR_PATH))
            result['requested'] = requested
        except HaBridgeUnavailableError as exc:
            requested, result['reason'] = None, str(exc)
        before = store.read_view(stamp)
        current = 'FULL_STOP' if before.get('fail_closed') is not False else before['effective_state']
        if requested is not None:
            target, result['reason'] = ha_bridge_target(requested, ceiling, current)
            if target != current:
                if before.get('fail_closed') is not False and _KS_RANK[target] > _KS_RANK[current]:
                    reason = 'HA_BRIDGE_STORE_FAIL_CLOSED'
                    result['reason'] = reason
                else:
                    try:
                        _remove_path(view)  # the MCP child reads FULL_STOP until the new view exists
                        record = store.transition(state=target, actor_class='OWNER_LOCAL_UI',
                                                  actor_ref=HA_BRIDGE_ACTOR_REF, reason_code='OWNER_ROUTINE', now=stamp)
                        result['transition'] = target
                        if publish_view:
                            publish_kill_switch_view(store, view)
                        emit('HA_BRIDGE_APPLIED', state=target, generation=record['generation'])
                    except Exception:
                        try:
                            _remove_path(view)
                        except OSError:
                            pass
                        reason = 'HA_BRIDGE_TRANSITION_FAILED'
                        result['reason'] = reason
        after = store.read_view(stamp)
        payload = ha_effective_payload(after, result['reason'], stamp)
        result['effective'], result['generation'] = payload['state'], payload['attributes']['generation']
        try:
            result['publish_status'] = ha_bridge_call(http, 'POST', HA_EFFECTIVE_PATH, payload)[0]
        except HaBridgeUnavailableError:
            result['publish_status'] = 0
        # 0.1.5-dev: the connector sign-in summary, once the perimeter has answered at least once.
        raw, received_at = latest_connectors()
        if raw is not None:
            connectors = connectors_payload(raw, received_at, clock)
            result['connectors'] = connectors['state']
            try:
                result['connectors_status'] = ha_bridge_call(http, 'POST', HA_CONNECTORS_PATH, connectors)[0]
            except HaBridgeUnavailableError:
                result['connectors_status'] = 0
        # 0.1.6-dev: the weekly AI activity counts, once the perimeter has answered at least once.
        raw, received_at = latest_activity()
        if raw is not None:
            activity = activity_sensor(raw, received_at, clock, store_dir.parent / ACTIVITY_SEEN.name)
            result['activity'] = activity['state']
            try:
                result['activity_status'] = ha_bridge_call(http, 'POST', HA_ACTIVITY_PATH, activity)[0]
            except HaBridgeUnavailableError:
                result['activity_status'] = 0
    except HaBridgeAllowlistError:
        raise
    except Exception:
        result['reason'] = 'HA_BRIDGE_CYCLE_FAILED'
    return result


def _bridge_log_key(result: dict[str, Any]) -> tuple[Any, ...]:
    status = result.get('publish_status')
    return (result.get('reason'), result.get('effective'), status in (200, 201), result.get('connectors'),
            result.get('activity'))


def _emit_bridge(result: dict[str, Any]) -> None:
    status = result.get('publish_status')
    emit(str(result.get('reason')), effective=str(result.get('effective')),
         published='YES' if status in (200, 201) else f'NO_{status if type(status) is int else 0}')
    if result.get('connectors') is not None:  # 0.1.5-dev: the summary state only, never times or links
        emit('AI_CONNECTORS', state=str(result['connectors']))
    if result.get('activity') is not None:  # 0.1.6-dev: the weekly state only, never counts or providers
        emit('AI_ACTIVITY', state=str(result['activity']))


def ha_bridge_loop(stop: threading.Event, ceiling: str, *, publish_view: bool = True, cycle: Any = None,
                   interval: float = HA_BRIDGE_INTERVAL, initial_wait: bool = False,
                   last: tuple[Any, ...] | None = None) -> None:
    """Run the bridge every interval until stop is set. Logs changes only. Never raises."""
    cycle = ha_bridge_cycle if cycle is None else cycle
    if initial_wait:
        stop.wait(interval)
    while not stop.is_set():
        try:
            result = cycle(ceiling, publish_view=publish_view)
        except Exception:
            result = {'reason': 'HA_BRIDGE_CYCLE_FAILED', 'effective': None, 'publish_status': None}
        key = _bridge_log_key(result)
        if key != last:
            _emit_bridge(result)
            last = key
        stop.wait(interval)


def start_ha_bridge(stop: threading.Event, ceiling: str, *, publish_view: bool = True, cycle: Any = None,
                    thread: Any = threading.Thread) -> dict[str, Any]:
    """First pass now (before the heartbeat or the MCP child start), then the 30 s loop in a daemon thread."""
    cycle = ha_bridge_cycle if cycle is None else cycle
    emit('HA_BRIDGE_STARTING')
    try:
        first = cycle(ceiling, publish_view=publish_view)
    except Exception:
        first = {'reason': 'HA_BRIDGE_CYCLE_FAILED', 'effective': None, 'publish_status': None}
    _emit_bridge(first)
    thread(target=ha_bridge_loop, name='ha-bridge', daemon=True, args=(stop, ceiling),
           kwargs={'publish_view': publish_view, 'cycle': cycle, 'initial_wait': True,
                   'last': _bridge_log_key(first)}).start()
    return first


def repair_kill_switch(*, store_dir: Path | None = None, view: Path | None = None,
                       root: Path | None = None, now: str | None = None) -> str:
    """Owner one-time repair (App option kill_switch_repair). Always ends at FULL_STOP, no view.

    Non-destructive first: recover() drops only a torn final log line and rolls the state file forward
    from the log. If the chain then verifies, the owner's repair is recorded as a tighten to FULL_STOP.
    Only a chain that does not verify is quarantined by the accepted reinitialize_after_tamper, which
    keeps the old files and binds their hash into the new genesis. The kill_switch option is ignored in
    this start, so a repair can never loosen anything by itself.
    """
    store_dir = KS_STORE if store_dir is None else store_dir
    view = KS_VIEW if view is None else view
    root = ROOT if root is None else root
    try:
        _remove_path(view)
        runtime = str(root / 'runtime')
        if runtime not in sys.path:
            sys.path.insert(0, runtime)
        from tools.ai_kill_switch_state import KillSwitchError  # manifest-verified runtime copy
        from tools.ai_kill_switch_store import KillSwitchStore
        stamp = now or datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        store = KillSwitchStore(store_dir)
        if not store.log_path.exists() and not store.state_path.exists():
            store.initialize(now=stamp, actor_ref='ai-access-app')
            action = 'INITIALIZED'
        else:
            try:
                store.recover()
                store.verify()
                action = 'RECOVERED'
            except KillSwitchError:
                store.reinitialize_after_tamper(now=stamp, actor_ref='ha-app-options-repair')
                action = 'REINITIALIZED'
            if action == 'RECOVERED':
                current = store.read_view(stamp)
                if current['fail_closed'] or current['effective_state'] != 'FULL_STOP':
                    store.transition(state='FULL_STOP', actor_class='OWNER_LOCAL_UI', actor_ref=KS_ACTOR_REF,
                                     reason_code='OWNER_RECOVERY', now=stamp)
        effective = store.read_view(stamp)
        if effective['fail_closed'] or effective['effective_state'] != 'FULL_STOP':
            raise ValueError('KILL_SWITCH_REPAIR_NOT_APPLIED')
        emit('KILL_SWITCH_REPAIRED_FULL_STOP', action=action, generation=effective['generation'])
    except Exception:
        try:
            _remove_path(view)
        except OSError:
            pass
        emit('KILL_SWITCH_FAILED_CLOSED', state='FULL_STOP')
    return 'FULL_STOP'


def consume_start(folder: Path, now: float) -> None:
    """Three starts per ten minutes. Corrupt/future state blocks rather than resets.

    Native s6 is the only retry scheduler. /run state intentionally resets only
    on container replacement/restart; after exhaustion the service stays down.
    """
    if not math.isfinite(now) or now < 0:
        raise AdmissionError('CLOCK_INVALID')
    folder.mkdir(mode=0o700, exist_ok=True)
    if folder.is_symlink() or folder.stat().st_uid != os.geteuid() or folder.stat().st_mode & 0o077:
        raise AdmissionError('POLICY_DIRECTORY_UNSAFE')
    fd = os.open(folder / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise AdmissionError('POLICY_LOCK_UNSAFE')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = folder / 'starts.json'
        if path.exists() or path.is_symlink():
            value = decode(read_file(folder, 'starts.json', 4096))
            if type(value) is not dict or set(value) != {'starts'} or type(value['starts']) is not list:
                raise AdmissionError('POLICY_STATE_INVALID')
            previous = value['starts']
        else:
            previous = []
        if len(previous) > MAX_STARTS or any(type(t) not in (int, float) or not math.isfinite(t) or not 0 <= t <= now for t in previous):
            raise AdmissionError('POLICY_STATE_INVALID')
        if previous != sorted(previous):
            raise AdmissionError('POLICY_STATE_INVALID')
        recent = [t for t in previous if now - t < START_WINDOW]
        if len(recent) >= MAX_STARTS:
            raise AdmissionError('RESTART_BUDGET_EXHAUSTED')
        temporary = folder / 'starts.new'
        with temporary.open('x') as stream:
            os.chmod(temporary, 0o600)
            stream.write(json.dumps({'starts': [*recent, now]}) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except (OSError, BlockingIOError):
        raise AdmissionError('POLICY_IO_REJECTED') from None
    finally:
        os.close(fd)


def plan(value: dict[str, Any], root: Path = ROOT, home: Path = HOME) -> tuple[list[str], dict[str, str]]:
    """Entire environment is rebuilt. No inherited proxy, token, CA or tool config."""
    if not value['enabled']:
        raise AdmissionError('DISABLED')
    command = [str(root / 'vendor/tunnel-client'), 'run',
               '--control-plane.base-url', 'https://api.openai.com',
               '--control-plane.tunnel-id', value['tunnel_id'],
               '--control-plane.max-inflight', '4', '--mcp.max-concurrent-requests', '4',
               '--mcp.command', '/usr/local/bin/python3 -B /opt/house-brain-ai-access/child.py',
               '--health.unix-socket', str(home / 'health.sock'),
               '--log.level', 'warn', '--log.format', 'json', '--admin-ui.log-buffer-events', '64']
    env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(home), 'TMPDIR': str(home),
           'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1',
           'CONTROL_PLANE_API_KEY': value['tunnel_api_key'],
           'MAINTENANCE_READ_KEY': value['broker_read_key']}
    return command, env


@contextmanager
def lifetime_claim(folder: Path):
    """A crash leaves an unreconciled claim; never overlap a surviving old group.

    No PID is trusted or killed from disk. Native s6 owns retry scheduling; its
    finish hook requests whole-App containment if the wrapper was lost. The
    root-owned /run claim is removed only after a proven clean local lifetime.
    """
    descriptor = -1
    try:
        folder.mkdir(mode=0o700, exist_ok=True)
        descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise AdmissionError('LIFETIME_DIRECTORY_UNSAFE')
        fd = os.open('active.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=descriptor)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(b'{"schema":"house_brain_lifetime.v1","state":"unreconciled"}\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(descriptor)
    except FileExistsError:
        if descriptor >= 0:
            os.close(descriptor)
        raise AdmissionError('PREVIOUS_LIFETIME_UNRECONCILED') from None
    except (OSError, AdmissionError):
        if descriptor >= 0:
            os.close(descriptor)
        raise AdmissionError('LIFETIME_ADMISSION_REJECTED') from None
    try:
        yield
    except BaseException:
        # Intentionally retain the claim on interruption or unproved cleanup.
        raise
    else:
        try:
            os.unlink('active.json', dir_fd=descriptor)
            os.fsync(descriptor)
        except OSError:
            raise AdmissionError('LIFETIME_CLEANUP_UNRECONCILED') from None
    finally:
        os.close(descriptor)


def _exit_observation(pid: int):
    # WNOWAIT keeps our direct child's PID reserved until all group signalling
    # is finished. Popen.poll()/wait() here would reap too early (PID reuse).
    return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)


def _group_has_live_members(group: int) -> bool:
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            fields = path.read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) == group and fields[0] not in ('Z', 'X', 'x'):
                return True
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, ValueError, IndexError):
            raise AdmissionError('PROCESS_GROUP_PROOF_UNAVAILABLE') from None
    return False


def _clean_group(child: subprocess.Popen[bytes]) -> None:
    """Signal only the owned, still-PID-reserved group; prove no running member."""
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 3.0
    while _group_has_live_members(child.pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 2.0
    while _group_has_live_members(child.pid):
        if time.monotonic() >= deadline:
            raise AdmissionError('PROCESS_GROUP_CLEANUP_UNPROVEN')
        time.sleep(0.02)
    # No signal by numeric PID/group is sent after this reap.
    child.wait(timeout=1)


def run_once(command: list[str], env: dict[str, str], cwd: Path, *, uid: int = UID, gid: int = GID) -> int:
    """One lifetime; crash claims and s6 finish containment handle wrapper loss."""
    stop = threading.Event()
    old = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGTERM, signal.SIGINT)}
    child: subprocess.Popen[bytes] | None = None
    try:
        child = subprocess.Popen(command, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                                 start_new_session=True, user=uid, group=gid, extra_groups=[])
        while not stop.is_set():
            observed = _exit_observation(child.pid)
            if observed is not None:
                return observed.si_status if observed.si_code == os.CLD_EXITED else -observed.si_status
            stop.wait(0.2)
        return 0
    finally:
        try:
            if child is not None:
                _clean_group(child)
        finally:
            for sig, handler in old.items():
                signal.signal(sig, handler)


def request_container_shutdown() -> None:
    """Request only this App's s6 shutdown; never an HA/Supervisor/host action.

    No fallback to reboot, a shell, or broad process killing. Final s6/container
    execution is a release gate; failure here must still suppress service retry.
    """
    subprocess.run(['/run/s6/basedir/bin/halt'], check=True, timeout=2,
                   env={'PATH': '/command:/usr/local/bin:/usr/bin:/bin'},
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)


def stage2_running(proc: Path = Path('/proc')) -> bool:
    me = os.getpid()
    for path in proc.glob('[0-9]*/cmdline'):
        try:
            if int(path.parent.name) != me and STAGE2_SCRIPT in path.read_bytes():
                return True
        except (OSError, ValueError):
            continue
    return False


def settle_before_refusal(*, running=stage2_running, timeout: float = STAGE2_SETTLE_SECONDS) -> None:
    """Bounded wait for s6 stage 2 before a startup refusal exits; TERM/INT end it early."""
    stop = threading.Event()
    old = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        deadline = time.monotonic() + timeout
        while not stop.is_set() and time.monotonic() < deadline and running():
            stop.wait(0.1)
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


def container_exit_code(code: object) -> int:
    """Exit code the container reports after a whole-App halt (never 0 for a fault)."""
    if type(code) is int and code == DESIRED_DOWN:
        return 0
    if type(code) is int and code == PERMANENT:
        return EXIT_ADMISSION_HALT
    return EXIT_CONTAINMENT_HALT


def record_container_exit(value: int, path: Path = CONTAINER_RESULT) -> None:
    """Overwrite ONLY the existing s6-overlay container-results file; never create one.

    Outside an s6-overlay container the file is absent and nothing is written.
    """
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError('CONTAINER_RESULT_NOT_REGULAR')
        os.write(fd, b'%d\n' % value)
    finally:
        os.close(fd)


def finish(code: int, *, sleep=time.sleep, halt=None, record=None) -> int:
    # Only 0/1 are normalised, cleaned returns from main's managed lifetime.
    # In particular 256 means the wrapper itself was signalled, NOT the native
    # child's exit. Retrying that case could overlap an orphaned old process.
    if type(code) is not int or code not in (0, 1):
        # Visibility only, and strictly before the halt (s6 reads the file during stage 3).
        # A failure here never skips or delays containment.
        try:
            (record_container_exit if record is None else record)(container_exit_code(code))
        except Exception:
            emit('APP_EXIT_CODE_RECORD_FAILED')
        try:
            (request_container_shutdown if halt is None else halt)()
        except Exception:
            emit('APP_CONTAINMENT_REQUEST_FAILED')
        return 125  # desired-down even when halt fails or is unavailable
    sleep(RESTART_DELAY)
    return 0


def main() -> int:
    try:
        mode = sys.argv[1] if len(sys.argv) > 1 else ''
        if mode == 'finish':
            try:
                code = int(sys.argv[2])
            except (ValueError, IndexError):
                code = -1
            return finish(code)
        if mode == 'verify':
            verify(ROOT)
            emit('SOURCE_AND_NATIVE_IDENTITY_VERIFIED')
            return 0
        if mode != 'run':
            raise AdmissionError('MODE_INVALID')
        value = options(read_file(DATA, 'options.json', 16384, protect=True))
        if not value['enabled']:
            raise AdmissionError('DISABLED')
        verify(ROOT)
        with lifetime_claim(POLICY):
            consume_start(POLICY, time.monotonic())
            HOME.mkdir(mode=0o700, exist_ok=True)
            if HOME.is_symlink() or not HOME.is_dir():
                raise AdmissionError('RUNTIME_DIRECTORY_INVALID')
            os.chmod(HOME, 0o700)
            os.chown(HOME, UID, GID)
            # A previous native process must have fully ended before s6 invokes us.
            socket_path = HOME / 'health.sock'
            if socket_path.exists() or socket_path.is_symlink():
                if not stat.S_ISSOCK(socket_path.lstat().st_mode):
                    raise AdmissionError('HEALTH_PATH_INVALID')
                socket_path.unlink()
            if value['kill_switch_repair']:
                repair_kill_switch()
            else:
                apply_kill_switch(value['kill_switch'])
            bridge_stop = threading.Event()
            try:
                if value['ha_bridge']:
                    # 0.1.3-dev: the App option stays the ceiling; a repair start is capped at FULL_STOP and
                    # publishes no view (as repair_kill_switch). First pass before the heartbeat or the child.
                    repair = value['kill_switch_repair']
                    start_ha_bridge(bridge_stop, 'FULL_STOP' if repair else value['kill_switch'],
                                    publish_view=not repair)
                if value['claude_only']:
                    emit('CLAUDE_ONLY_HEARTBEAT_STARTING')
                    code = run_heartbeat_only(value['perimeter_heartbeat_url'], value['perimeter_heartbeat_key'])
                    emit('CLAUDE_ONLY_STOPPED', exit_code=code)
                    return 0
                command, env = plan(value)
                stop = threading.Event()
                if value['perimeter_heartbeat_url']:
                    threading.Thread(target=heartbeat_loop, name='edge-heartbeat', daemon=True,
                                     args=(stop, value['perimeter_heartbeat_url'], value['perimeter_heartbeat_key'])).start()
                emit('STARTING_NOT_READY')
                try:
                    code = run_once(command, env, ROOT / 'runtime')
                finally:
                    stop.set()
                emit('NATIVE_EXIT', exit_code=code)
                return 0 if code == 0 else 1
            finally:
                bridge_stop.set()
    except AdmissionError as exc:
        emit(str(exc))
        if mode == 'run':
            settle_before_refusal()
        return DESIRED_DOWN if str(exc) == 'DISABLED' else PERMANENT
    except Exception:
        emit('STARTUP_OR_CLEANUP_FAILED')
        return PERMANENT


if __name__ == '__main__':
    raise SystemExit(main())
