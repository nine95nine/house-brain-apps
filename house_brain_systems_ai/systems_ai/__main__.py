"""Exact App entrypoint. SIGTERM stops cleanly; there is no action of any kind to undo."""
import argparse
import json
import os
from pathlib import Path
import resource
import signal
from threading import Event, Thread
import time

from .archive import day_csv, day_json
from .health import build, digest, proposals
from .interlock import safety_interlock
from .investigations import stall_threshold
from . import radar  # noqa: F401 - 0.2.1: the World Change Radar runs only inside Runtime.step, behind gate()
from .runtime import Runtime
from . import __version__
from .transport import Client, PublicClient
from .web import Server
from . import link
from .readings import parse_ids as readings_parse_ids
from .interlock import ENTITY as KILL_ENTITY


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
    runtime = Runtime(args.data, client, gate, PublicClient(__version__))
    # 0.3.4 (W26-C6): reviews without progress before an investigation reads STALLED; 2..10, anything else is 3.
    runtime.investigations.stall_reviews = stall_threshold(options.get('stall_reviews'))
    stop = Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    server = Server(lambda: {**runtime.status, 'safety_interlock': runtime.hold or 'ALLOW'}, lambda: build(runtime, time.time()),
                    lambda: digest(runtime, time.time()), lambda: proposals(runtime, time.time()),
                    lambda day: day_csv(runtime.store.path, day),
                    archive_json=lambda day: day_json(runtime.store.path, day))
    thread = Thread(target=server.run, daemon=True)
    thread.start()
    # 0.3.5 House link: only when the owner set both link_key and link_peer (design MACBOOK_HOUSE_LINK_R1).
    linked = link.configured(options)
    link_server = None
    if linked:
        link_server = link.LinkServer(
            linked[0], linked[1],
            state=lambda: link.state_view(runtime.status, runtime.states, KILL_ENTITY),
            questions=lambda: link.questions_view(runtime.status, runtime.cases.state['cases']),
            health=lambda: build(runtime, time.time()), held=lambda: runtime.hold is not None,
            inbox=runtime.inbox, day=lambda: time.strftime('%Y-%m-%d', time.gmtime()),
            readings=lambda ids: runtime.readings.view(time.time(), ids), parse_ids=readings_parse_ids)
        Thread(target=link_server.run, daemon=True).start()
    try:
        while not stop.is_set():
            runtime.step(time.time())
            stop.wait(1)
    finally:
        server.stop()
        if link_server is not None:
            link_server.stop()
        runtime.close()


if __name__ == '__main__':
    main()
