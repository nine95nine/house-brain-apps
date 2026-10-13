#!/usr/bin/env python3
"""Process coordinator for Engineering Observer + bounded read worker."""

from __future__ import annotations

import os
import threading

import observer
from read_worker import ACTIVE_OPERATIONS, worker_loop


def main() -> int:
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if len(token) < 16:
        return observer.main()
    try:
        broker_url, write_key, _interval = observer.load_options()
    except RuntimeError:
        return observer.main()

    stop = threading.Event()
    thread = threading.Thread(
        target=worker_loop,
        args=(broker_url, write_key, token, stop),
        name="engineering-read-worker",
        daemon=True,
    )
    thread.start()
    print(
        "engineering_read_worker=started "
        f"operations={','.join(sorted(ACTIVE_OPERATIONS))} "
        "authority=read_only supervisor_get_only",
        flush=True,
    )
    try:
        return observer.main(read_worker_alive=thread.is_alive)
    finally:
        stop.set()
        thread.join(timeout=5.0)


if __name__ == "__main__":
    raise SystemExit(main())
