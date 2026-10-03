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
HEARTBEAT_TIMEOUT = 10.0
UID = GID = 10001
MANIFEST_SHA = 'ec20847b87a53dc8459a942f57dd458e7db537dc5b57cd6970ee6f0c25a562f5'
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
             'perimeter_heartbeat_url', 'perimeter_heartbeat_key'}
    optional = {'perimeter_heartbeat_url', 'perimeter_heartbeat_key'}  # absent = '' (heartbeat off)
    booleans = {'enabled', 'kill_switch_repair'}
    if type(value) is not dict or not names - optional <= set(value) <= names:
        raise AdmissionError('OPTIONS_SCHEMA_INVALID')
    value = {**{k: '' for k in optional}, **value}
    if any(type(value[k]) is not bool for k in booleans):
        raise AdmissionError('OPTIONS_SCHEMA_INVALID')
    if any(type(value[k]) is not str or len(value[k]) > 4096 for k in names - booleans):
        raise AdmissionError('OPTIONS_SCHEMA_INVALID')
    if value['kill_switch'] not in KS_STATES:
        raise AdmissionError('KILL_SWITCH_OPTION_INVALID')
    if not value['enabled']:
        return value
    if re.fullmatch(r'tunnel_[0-9a-f]{32}', value['tunnel_id']) is None:
        raise AdmissionError('TUNNEL_ID_INVALID')
    for key in ('tunnel_api_key', 'broker_read_key'):
        if not 20 <= len(value[key]) <= 4096 or KEY_PATTERN.fullmatch(value[key]) is None:
            raise AdmissionError('CAPABILITY_INVALID')
    url, hb_key = value['perimeter_heartbeat_url'], value['perimeter_heartbeat_key']
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


def post_heartbeat(url: str, key: str, heartbeat: dict[str, Any], *, opener: Any = None) -> int:
    """One signed POST. Returns the HTTP status (0 on transport failure). Never raises, never logs bodies."""
    body = json.dumps(heartbeat, sort_keys=True, separators=(',', ':')).encode('ascii')
    req = urlrequest.Request(url, data=body, method='POST', headers={
        'content-type': 'application/json', 'X-HB-Heartbeat-Signature': sign_heartbeat(key, body)})
    if opener is None:
        # No proxy, no redirect, verified TLS: the key reaches only the exact configured Worker URL.
        opener = urlrequest.build_opener(urlrequest.ProxyHandler({}), _NoHeartbeatRedirect(),
                                         urlrequest.HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(req, timeout=HEARTBEAT_TIMEOUT) as response:
            return int(response.status)
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
