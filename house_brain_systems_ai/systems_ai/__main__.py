"""Exact App entrypoint. SIGTERM stops cleanly; there is no action of any kind to undo."""
import argparse
import json
import os
from pathlib import Path
import resource
import signal
from threading import Event, Thread
import time

from .archive import day_csv
from .health import build, digest, proposals
from .interlock import safety_interlock
from .runtime import Runtime
from .transport import Client
from .web import Server


def gate(states, now):
    """Kill-switch interlock (#56): every Systems AI cycle is ADVISORY work. Returns None to run, or the
    safety_interlock blocking reason to hold (FULL_STOP, or a missing/stale/unknown switch)."""
    decision = safety_interlock(states, 'ADVISORY', now)
    return None if decision == 'ALLOW' else decision


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--options', default='/data/options.json')
    parser.add_argument('--data', default='/data')
    args = parser.parse_args()
    options = json.loads(Path(args.options).read_text())
    if options.get('mode', 'SHADOW') != 'SHADOW':
        raise SystemExit('MODE_NOT_SUPPORTED: Systems AI runs in SHADOW only')
    # Development containment, not a measured production headroom claim.
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    try:
        client = Client(os.environ.get('SUPERVISOR_TOKEN'))
    except ValueError:
        raise SystemExit('HA_API_TOKEN_REQUIRED: Supervisor did not provide the API token')
    runtime = Runtime(args.data, client, gate)
    stop = Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    server = Server(lambda: {**runtime.status, 'safety_interlock': runtime.hold or 'ALLOW'}, lambda: build(runtime, time.time()),
                    lambda: digest(runtime, time.time()), lambda: proposals(runtime, time.time()),
                    lambda day: day_csv(runtime.store.path, day))
    thread = Thread(target=server.run, daemon=True)
    thread.start()
    try:
        while not stop.is_set():
            runtime.step(time.time())
            stop.wait(1)
    finally:
        server.stop()
        runtime.close()


if __name__ == '__main__':
    main()
