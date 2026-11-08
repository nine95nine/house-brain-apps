"""Exact App entrypoint: prepare the key directory, drop root for good, make keys once, run Tang.

1. As root only: create /data/tang (mode 700) and give it to Debian's `_tang` user, then drop to that
   user permanently (no supplementary groups). Nothing after this step runs as root.
2. Keys once: Tang's own `tangd-keygen` runs only when no active key exists (keys.py).
3. Tang: `socat TCP-LISTEN:7500,reuseaddr,fork EXEC:"/usr/libexec/tangd /data/tang"`; Debian's binaries
   only, no code of ours on the request path.
4. /health on 8099 (web.py, health.py); the signing-key thumbprint(s) logged once, from tang-show-keys.

SIGTERM stops socat and exits 0. If socat exits on its own the App exits non-zero. There is nothing to undo.
"""
import os
import pwd
import signal
import subprocess
import sys
from threading import Event, Thread
import time

from . import __version__
from . import keys, probe
from .health import Health
from .web import Server

TANG_USER = '_tang'
SOCAT_ARGV = ('/usr/bin/socat', 'TCP-LISTEN:7500,reuseaddr,fork', 'EXEC:/usr/libexec/tangd /data/tang')
READY_WAIT_S = 30


def log(message):
    print(f'house_brain_tang {__version__}: {message}', flush=True)


def drop_privileges(user=TANG_USER, key_dir=keys.KEY_DIR, os_=os, getpwnam=pwd.getpwnam):
    """Root: prepare the key directory, then become `user` for good. Non-root: nothing to drop."""
    if os_.geteuid() != 0:
        return False
    entry = getpwnam(user)
    if entry.pw_uid == 0 or entry.pw_gid == 0:
        raise SystemExit('PRIVILEGE_DROP_FAILED: the Tang user must not be root')
    keys.prepare(key_dir, entry.pw_uid, entry.pw_gid)
    os_.setgroups([])
    os_.setgid(entry.pw_gid)
    os_.setuid(entry.pw_uid)
    if os_.geteuid() == 0 or os_.getegid() == 0:
        raise SystemExit('PRIVILEGE_DROP_FAILED')
    try:
        os_.setuid(0)
    except PermissionError:
        return True
    raise SystemExit('PRIVILEGE_DROP_FAILED: root could be regained')


def wait_ready(check=probe.check, wait=READY_WAIT_S, sleep=time.sleep):
    for _ in range(int(wait)):
        ok, _reason = check()
        if ok:
            return True
        sleep(1)
    return False


def main():
    log('starting')
    dropped = drop_privileges()
    if dropped:
        log(f'running as {TANG_USER} (uid {os.geteuid()}), not root')
    elif os.geteuid() == 0:
        raise SystemExit('PRIVILEGE_DROP_FAILED')
    try:
        made = keys.ensure()
    except keys.KeyDirError as error:
        raise SystemExit(str(error))
    log('Tang keys created on first start by tangd-keygen' if made['generated']
        else f'Tang keys present ({made["count"]} active); none created')
    socat = subprocess.Popen(SOCAT_ARGV, stdin=subprocess.DEVNULL)
    stop = Event()

    def on_signal(*_):
        stop.set()
        if socat.poll() is None:
            socat.terminate()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, on_signal)
    server = Server(Health())
    Thread(target=server.run, daemon=True).start()
    if wait_ready():
        prints = probe.thumbprints()
        log('Tang answering on 7500/tcp; signing key thumbprint (SHA-256): ' + (', '.join(prints) if prints
            else 'TANG_THUMBPRINT_UNAVAILABLE'))
    else:
        log('TANG_ADVERTISEMENT_UNREACHABLE at start; /health reports FAILED until Tang answers')
    code = socat.wait()
    server.stop()
    if stop.is_set():
        log('stopped')
        return 0
    log(f'TANG_SERVER_EXITED (socat exit {code})')
    return 1


if __name__ == '__main__':
    sys.exit(main())
