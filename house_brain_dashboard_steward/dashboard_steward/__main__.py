"""Command line.

  python3 -m dashboard_steward audit <dashboard.yaml> [--url <dashboard-url>]
  python3 -m dashboard_steward ledger <dashboard.yaml> [--url ...]
  python3 -m dashboard_steward check <baseline.yaml> <candidate.yaml> [--url ...]
  python3 -m dashboard_steward run [--options /data/options.json] [--db /data/steward.sqlite]

``run`` is the service (App entrypoint). The others are offline tools: they read local files only.
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import signal
import socket
import sys
import threading

from . import lovelace as L, ndec, validate as V, yamlio
from .haclient import CoreSocket
from .inference import LocalChatModel, NoModel
from .interlock import safety_interlock
from .runtime import Config, Steward
from .store import Store
from .web import serve


def _load(path):
    with open(path, "rb") as f:
        return yamlio.loads(f.read())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="dashboard_steward")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("audit", "ledger"):
        p = sub.add_parser(name)
        p.add_argument("file")
        p.add_argument("--url", default="dashboard")
    p = sub.add_parser("check")
    p.add_argument("baseline")
    p.add_argument("candidate")
    p.add_argument("--url", default="dashboard")
    p = sub.add_parser("run")
    p.add_argument("--options", default="/data/options.json")
    p.add_argument("--db", default="/data/steward.sqlite")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "audit":
            rep = V.validate(_load(a.file), dashboard_url=a.url, origin="baseline")
            print(json.dumps(rep.as_dict(), indent=1, sort_keys=True))
            return 0 if rep.verdict == "PASS" else 1
        if a.cmd == "ledger":
            print(json.dumps(ndec.build_ledger(_load(a.file), dashboard_url=a.url), indent=1, sort_keys=True))
            return 0
        if a.cmd == "check":
            base, cand = _load(a.baseline), _load(a.candidate)
            rep = V.validate(cand, dashboard_url=a.url, origin="generated", baseline=base)
            nd = ndec.check(ndec.build_ledger(base, dashboard_url=a.url), cand)
            out = {"validation": rep.as_dict(), "ndec": [f.__dict__ for f in nd.findings],
                   "verdict": "PASS" if rep.verdict == nd.verdict == "PASS" else "FAIL",
                   "baseline_sha256": L.config_sha256(base), "candidate_sha256": rep.sha256}
            print(json.dumps(out, indent=1, sort_keys=True))
            return 0 if out["verdict"] == "PASS" else 1
    except yamlio.YamlRefused as e:
        print(json.dumps({"verdict": "FAIL", "reason": e.code, "line": e.line}), file=sys.stderr)
        return 2
    return run(a.options, a.db)


def gate(states, action_class, now):
    """Kill-switch interlock (#56). Registry/dashboard reads are READ work; model calls and YAML proposals
    are ADVISORY work. Returns None to proceed, or the safety_interlock blocking reason to hold
    (FULL_STOP, or a missing, stale, future-dated or unknown switch)."""
    decision = safety_interlock(states, action_class, now)
    return None if decision == "ALLOW" else decision


def run(options_path: str, db_path: str) -> int:
    opts = {}
    if os.path.exists(options_path):
        with open(options_path) as f:
            opts = json.load(f)
    # Development containment, not a measured production headroom claim.
    try:
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    except (ValueError, OSError):
        pass
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise SystemExit("HA_API_TOKEN_REQUIRED: Supervisor did not provide the API token")
    url, name = opts.get("model_url") or "", opts.get("model_name") or ""
    try:
        model = LocalChatModel(url, name) if url and name else NoModel()
    except ValueError as e:
        model = NoModel(str(e))        # e.g. INFERENCE_HOST_NOT_LOCAL: report it, never call out
    cfg = Config(generation_targets=tuple(opts.get("generation_targets") or ()),
                 model_calls_per_day=int(opts.get("model_calls_per_day") or 48),
                 heartbeat_path=os.path.join(os.path.dirname(db_path), "heartbeat.json"), db_path=db_path)
    store = Store(db_path)
    steward = Steward(store, lambda: CoreSocket(token, connect=socket.create_connection), model, config=cfg,
                      gate=gate)
    print(json.dumps({"event": "start", "app": "house_brain_dashboard_steward", "model": model.name,
                      "safety_interlock": "gate injected: READ for reads, ADVISORY for model calls and proposals",
                      "generation_targets": list(cfg.generation_targets), "control_authority": "NONE"}), flush=True)
    srv = serve(steward, port=int(opts.get("port") or 8099))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        steward.run_forever(stop=stop.is_set, sleep=stop.wait)
    finally:
        srv.shutdown()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
