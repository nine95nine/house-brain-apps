"""The always-on Dashboard Steward service.

Always-on means: a persistent process with durable state that keeps observing, reconciling and reporting
health whether or not a model is available; model reasoning runs when events justify it (not nonstop
inference on unchanged data). Health keeps four things apart and never collapses them into one green:

* service      the process runs and writes its heartbeat;
* observation  connected to Home Assistant and reconciled recently (registries + dashboards);
* inference    a qualified local model answered recently (``AI_OK``), or ``AI_DEGRADED`` / ``AI_DISABLED``;
* deployment   ``NOT_QUALIFIED``: Dashboard Steward has no write path. Writes are the Deployer's (D5).

Governance built in: generating YAML for a real, existing dashboard is the repository's pre-YAML gate
(``docs/governance/CHANGE_CONTROL.md``). The option ``generation_targets`` lists the dashboards the owner has
approved generation for; it ships empty, so out of the box the Steward records opportunities and debt but
writes no YAML for any real dashboard.
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import approval, discovery, generator, lovelace as L, ndec, plan as P, validate as V
from .haclient import EVENT_TYPES, HaError, Session
from .inference import ModelUnavailable

VERSION = "0.1.0"
HEALTH_SCHEMA = "dashboard_steward_health.v1"


@dataclass
class Config:
    poll_s: float = 5.0
    debounce_s: float = 30.0             # registry event floods coalesce into one reconcile
    min_reconcile_gap_s: float = 60.0
    reconcile_every_s: float = 6 * 3600  # periodic reconcile recovers missed events
    heartbeat_s: float = 60.0
    probe_every_s: float = 600.0
    backoff_min_s: float = 2.0
    backoff_max_s: float = 300.0
    stale_after_s: float = 7 * 3600
    model_calls_per_day: int = 48
    tasks_per_tick: int = 1
    generation_targets: tuple = ()        # dashboards with owner pre-YAML approval for generation
    critical_entities: tuple = ()
    heartbeat_path: str = ""
    db_path: str = ""
    max_dashboards: int = 64


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


class Steward:
    def __init__(self, store, connect, model, *, config: Config | None = None, clock=time.time,
                 rng: random.Random | None = None, gate=None):
        """``gate(states, action_class, now)`` is the kill-switch interlock (#56): None to proceed or the
        blocking reason. Without a gate nothing runs (fail closed)."""
        self.store, self.connect, self.model = store, connect, model
        self.gate = gate
        self.hold: str | None = None
        self.gate_checked = False             # until the switch has been read, nothing claims ALLOW
        self.retry_at = 0.0
        self.cycle_at: int | None = None
        self.good_at: int | None = None
        self.cycle_seconds: float | None = None
        self.cfg = config or Config()
        self.clock = clock
        self.rng = rng or random.Random()
        self.session: Session | None = None
        self.backoff = self.cfg.backoff_min_s
        self.next_connect = 0.0
        self.dirty_since: float | None = None
        self.force_reconcile = False
        self._lock = threading.Lock()
        self._public: dict = {}
        self.events_seen = 0
        self.last_heartbeat = 0.0
        self.last_probe = -1e18
        self._changed = True
        self.inference = {"status": "AI_DEGRADED", "reason": "NOT_PROBED", "last_ok": None}
        self.observation = {"status": "UNKNOWN", "connected": False, "last_reconcile": None, "last_error": ""}
        self.started_at = clock()
        self.recovered = store.recover()
        if hasattr(model, "budget") and model.budget is None:
            model.budget = lambda: self.store.spend("model_call", self.cfg.model_calls_per_day, _day(self.clock()))

    # ------------------------------------------------------------------ connection
    def _drop(self, err: str):
        if self.session is not None:
            try:
                self.session.close()
            except Exception:
                pass
        self.session = None
        self.observation.update(connected=False, last_error=err[:120])
        jitter = self.rng.uniform(0.5, 1.0)
        self.next_connect = self.clock() + self.backoff * jitter
        self.backoff = min(self.backoff * 2, self.cfg.backoff_max_s)

    def _ensure_session(self) -> bool:
        if self.session is not None:
            return True
        if self.clock() < self.next_connect:
            return False
        try:
            self.session = Session(self.connect())
            for et in sorted(EVENT_TYPES):
                self.session.subscribe(et)
        except (HaError, OSError, ValueError) as e:
            self._drop(getattr(e, "code", type(e).__name__))
            return False
        self.observation.update(connected=True, last_error="")
        self.backoff = self.cfg.backoff_min_s
        self.force_reconcile = True         # reconcile right after (re)connect: events may have been missed
        return True

    # ------------------------------------------------------------------ main loop step
    def tick(self):
        now = self.clock()
        t0 = time.monotonic()
        ok = True
        if self._ensure_session():
            try:
                evs = self.session.poll(self.cfg.poll_s)
                if evs:
                    self.events_seen += len(evs)
                    if self.dirty_since is None:
                        self.dirty_since = now
                last = self.observation["last_reconcile"]
                due_periodic = last is None or now - last >= self.cfg.reconcile_every_s
                due_events = self.dirty_since is not None and now - self.dirty_since >= self.cfg.debounce_s
                gap_ok = last is None or now - last >= self.cfg.min_reconcile_gap_s
                if (self.force_reconcile or due_periodic or (due_events and gap_ok)) and now >= self.retry_at:
                    self.reconcile()
                    self.force_reconcile = False
            except (HaError, OSError, ValueError) as e:
                ok = False
                self._drop(getattr(e, "code", type(e).__name__))
        else:
            ok = False
        self._probe_model()
        for _ in range(self.cfg.tasks_per_tick):
            if not self.process_one():
                break
        self.cycle_at = int(now)
        if ok:
            self.good_at = int(now)
        self.cycle_seconds = round(time.monotonic() - t0, 3)
        if now - self.last_heartbeat >= self.cfg.heartbeat_s or self._changed:
            self.write_heartbeat()

    # ------------------------------------------------------------------ reconcile
    def reconcile(self):
        s = self.session
        reg = s.registries()
        hold = self._gate(reg["states"], "READ")
        if hold:                              # kill switch: keep nothing from this read, try again later
            self.retry_at = self.clock() + self.cfg.min_reconcile_gap_s
            return
        snap = discovery.build_snapshot(reg["entities"], reg["devices"], reg["states"], reg["areas"])
        prev = self.store.get("snapshot")
        events = discovery.diff_snapshots(prev, snap)
        inventory = V.Inventory(discovery.inventory_entities(snap), None)
        dashboards = [{"url_path": None, "mode": "storage", "title": "Overview"}]
        dashboards += [d for d in (s.dashboards() or []) if isinstance(d, dict)][: self.cfg.max_dashboards]
        configs, hashes = {}, {}
        for d in dashboards:
            url = d.get("url_path")
            key = url or "lovelace"
            try:
                cfg = s.dashboard_config(url)
            except HaError as e:
                hashes[key] = {"status": "UNREADABLE", "reason": e.code}
                continue
            configs[key] = cfg
            hashes[key] = {"sha256": L.config_sha256(cfg), "mode": d.get("mode")}
        known = {k: [v.ident for v in L.walk(c).views] + [str(v.index) for v in L.walk(c).views]
                 for k, c in configs.items()}
        inventory.dashboards = known
        prev_hashes = self.store.get("dashboard_hashes", {}) or {}
        debt = {}
        for key, cfg in configs.items():
            if prev_hashes.get(key, {}).get("sha256") not in (None, hashes[key]["sha256"]):
                events.append(discovery.Event("DASHBOARD_CHANGED", "", "", {"dashboard": key}))
                for p in self.store.proposals():
                    if p["dashboard"] == key:
                        approval.recheck(self.store, p["id"], cfg)
            rep = V.validate(cfg, dashboard_url=key, inventory=inventory, origin="baseline")
            debt[key] = {"verdict": rep.verdict, "warnings": len([f for f in rep.findings if f.severity == "WARN"]),
                         "codes": sorted(rep.codes("WARN"))[:30],
                         "cards": sum(1 for e in rep.model.elements if e.level == "card")}
            self.store.put(f"dash:{key}", cfg)
        self.store.put("dashboard_hashes", hashes)
        self.store.put("dashboard_debt", debt)
        self.store.put("snapshot", snap)
        self._derive_tasks(events, snap, configs)
        recent = self.store.get("recent_events", []) or []
        recent = ([e.as_dict() for e in events] + recent)[:200]
        self.store.put("recent_events", recent)
        now = self.clock()
        self.observation.update(status="OK", last_reconcile=now, entities=len(snap["entities"]),
                                dashboards=len(configs), events_last=len(events))
        self.dirty_since = None
        self._changed = True

    def _derive_tasks(self, events, snap, configs):
        new_ids = [e.entity_id for e in events if e.kind == "ENTITY_NEW"]
        for b in discovery.bundle(new_ids, snap):
            if b.placement == "none":
                continue
            fp = approval.opportunity_fp("BUNDLE", b.category, {"entities": sorted(b.entities)})
            if self.store.suppressed(fp):
                continue
            self.store.enqueue("PROPOSE_BUNDLE", fp, {"bundle": b.as_dict(), "fp": fp})
        refs = {k: L.walk(c).entity_ids(("structured",)) for k, c in configs.items()}
        for e in events:
            if e.kind == "ENTITY_RENAMED":
                old = e.evidence["old_entity_id"]
                for key, ids in refs.items():
                    if old in ids:
                        fp = approval.opportunity_fp("REPOINT", key, {"old": old, "new": e.entity_id})
                        if not self.store.suppressed(fp):
                            self.store.enqueue("REPAIR_REFERENCE", fp, {"dashboard": key, "old": old,
                                                                        "new": e.entity_id, "fp": fp})

    # ------------------------------------------------------------------ model + tasks
    def _probe_model(self):
        now = self.clock()
        if now - self.last_probe < self.cfg.probe_every_s:
            return
        self.last_probe = now
        ok, reason = self.model.probe()
        was = self.inference["status"]
        if ok:
            self.inference.update(status="AI_OK", reason=reason, last_ok=now)
            if was != "AI_OK":
                self.store.release_waiting()
        else:
            self.inference.update(status="AI_DISABLED" if reason == "NO_MODEL_CONFIGURED" else "AI_DEGRADED",
                                  reason=reason)

    def _gate(self, states_list, action_class) -> str | None:
        states = {r.get("entity_id"): r for r in states_list or [] if isinstance(r, dict)}
        if self.gate is None:
            hold = "KILL_SWITCH_GATE_MISSING"
        else:
            try:
                hold = self.gate(states, action_class, self.clock())
            except Exception:                  # an interlock that cannot decide holds
                hold = "KILL_SWITCH_GATE_ERROR"
        if hold != self.hold or not self.gate_checked:
            self._changed = True
        self.hold = hold
        self.gate_checked = True
        return hold

    def process_one(self) -> bool:
        if not self.store.has_ready_task():
            return False
        # every task is ADVISORY work (model call or YAML proposal): check the kill switch fresh, right now
        if self.session is None:
            self._gate([], "ADVISORY")
            return False
        try:
            hold = self._gate(self.session.command({"type": "get_states"}), "ADVISORY")
        except (HaError, OSError, ValueError) as e:
            self._drop(getattr(e, "code", type(e).__name__))
            return False
        if hold:
            return False
        task = self.store.next_task()
        if task is None:
            return False
        self._changed = True
        try:
            self._run_task(task)
        except ModelUnavailable as e:
            self.inference.update(status="AI_DEGRADED", reason=e.code)
            self.store.finish(task["id"], "waiting_model", e.code)
        except Exception as e:          # a bad task must never stop the service
            self.store.finish(task["id"], "failed" if task["attempts"] >= 3 else "queued",
                              type(e).__name__, retry_in=300)
        return True

    def _run_task(self, task):
        pl = task["payload"]
        snap = self.store.get("snapshot") or {"entities": {}, "states": {}}
        inventory = V.Inventory(discovery.inventory_entities(snap), None)
        if task["kind"] == "REPAIR_REFERENCE":
            target = pl["dashboard"]
            if target not in self.cfg.generation_targets:
                self._opportunity(task, "AWAITING_GENERATION_APPROVAL", target)
                return
            base = self.store.get(f"dash:{target}")
            if base is None:
                self.store.finish(task["id"], "failed", "BASELINE_MISSING")
                return
            plan = {"schema": P.PLAN_SCHEMA, "summary": "repoint a renamed entity",
                    "operations": [{"op": "repoint_entity", "old": pl["old"], "new": pl["new"]}]}
            cand, _ = P.apply(plan, base)
            rep = V.validate(cand, dashboard_url=target, inventory=inventory, origin="generated", baseline=base,
                             renames={pl["old"]: pl["new"]})
            res = generator.Result("CANDIDATE_READY" if rep.verdict == "PASS" else "REFUSED",
                                   plan=plan, candidate=cand, plan_yaml="", baseline_sha256=L.config_sha256(base),
                                   candidate_sha256=rep.sha256, validation=rep.as_dict(), changes=P.diff(base, cand),
                                   model="deterministic", attempts=1)
            if res.status == "CANDIDATE_READY":
                from . import yamlio
                res.candidate_yaml = yamlio.dumps(cand)
                prop = approval.build(res, title=f"Repoint {pl['old']} to {pl['new']}",
                                      rationale="The entity was renamed (same platform and unique id).",
                                      trigger={"kind": "ENTITY_RENAMED", "evidence": {"old": pl["old"], "new": pl["new"]}},
                                      dashboard=target, category="repair", entities=[pl["old"], pl["new"]],
                                      now=self.clock())
                approval.admit(self.store, prop)
                self.store.finish(task["id"], "done")
            else:
                self._opportunity(task, "GENERATION_REFUSED:" + ",".join(sorted(rep.codes(V.BLOCK)))[:120], target)
            return
        if task["kind"] == "PROPOSE_BUNDLE":
            b = pl["bundle"]
            target = (self.cfg.generation_targets or (None,))[0]
            if target is None:
                self._opportunity(task, "AWAITING_GENERATION_APPROVAL", "")
                return
            base = self.store.get(f"dash:{target}")
            ledger = ndec.build_ledger(base, dashboard_url=target,
                                       critical_entities=self.cfg.critical_entities) if base else None
            res = generator.generate({"kind": "PLACE_CAPABILITY_BUNDLE", "title": b["title"],
                                      "entities": b["entities"], "placement": b["placement"],
                                      "category": b["category"]},
                                     model=self.model, snapshot=snap, baseline=base, dashboard_url=target,
                                     ledger=ledger, inventory=inventory)
            if res.status == "AI_DEGRADED":
                raise ModelUnavailable(res.reason or "MODEL_UNAVAILABLE")
            self.inference.update(status="AI_OK", last_ok=self.clock(), reason="MODEL_ANSWERED")
            if res.status == "CANDIDATE_READY":
                prop = approval.build(res, title=f"Show {b['title']} ({b['category']})",
                                      rationale="; ".join(b["reasons"]),
                                      trigger={"kind": "ENTITY_NEW", "evidence": {"entities": sorted(b["entities"])}},
                                      dashboard=target, category=b["category"], entities=b["entities"],
                                      now=self.clock())
                approval.admit(self.store, prop)
                self.store.finish(task["id"], "done")
            else:
                self._opportunity(task, "GENERATION_REFUSED:" + (res.reason or ""), target)
            return
        self.store.finish(task["id"], "failed", "TASK_KIND_UNKNOWN")

    def _opportunity(self, task, status, target):
        ops = self.store.get("opportunities", []) or []
        ops = [o for o in ops if o["fp"] != task["fp"]]
        ops.insert(0, {"fp": task["fp"], "kind": task["kind"], "status": status, "target": target,
                       "summary": task["payload"].get("bundle", {}).get("title") or
                       f"{task['payload'].get('old')} -> {task['payload'].get('new')}", "at": self.clock()})
        self.store.put("opportunities", ops[:200])
        self.store.finish(task["id"], "done")

    # ------------------------------------------------------------------ health
    def health(self) -> dict:
        now = self.clock()
        obs = dict(self.observation)
        last = obs.get("last_reconcile")
        if last is None:
            obs["status"] = "UNKNOWN"
        elif now - last > self.cfg.stale_after_s:
            obs["status"] = "STALE"
        elif not obs["connected"]:
            obs["status"] = "DISCONNECTED"
        obs["age_s"] = None if last is None else round(now - last)
        counts = self.store.task_counts()
        pending = len(self.store.proposals("PENDING"))
        stale = len(self.store.proposals("STALE"))
        inf = dict(self.inference)
        inf.update(model=getattr(self.model, "name", "none"),
                   calls_today=self.store.spent("model_call", _day(now)), calls_limit=self.cfg.model_calls_per_day)
        if self.hold:
            overall = "HELD"
        elif obs["status"] == "OK" and inf["status"] == "AI_OK":
            overall = "OPERATIONAL"
        elif obs["status"] == "OK":
            overall = "AI_DEGRADED"           # observing, but no qualified inference: not "AI operational"
        elif obs["status"] == "UNKNOWN":
            overall = "UNKNOWN"
        else:
            overall = "DEGRADED"
        return {"schema": HEALTH_SCHEMA, "version": VERSION, "at": now, "overall": overall,
                "service": {"status": "RUNNING", "started_at": self.started_at, "recovered": self.recovered},
                "observation": obs, "inference": inf, "safety_interlock": self.hold or ("ALLOW" if self.gate_checked else "NOT_CHECKED"),
                "proposals": {"pending": pending, "stale": stale},
                "queue": {**counts, "dropped": self.store.get("queue_dropped", 0) or 0,
                          "events_dropped": getattr(self.session, "events_dropped", 0) if self.session else 0},
                "generation_targets": list(self.cfg.generation_targets),
                "deployment": {"status": "NOT_QUALIFIED",
                               "detail": "no write path; dashboard writes are a separate Deployer operation (D5)"},
                "control_authority": "NONE"}

    def write_heartbeat(self):
        self.last_heartbeat = self.clock()
        self._changed = False
        h = self.health()
        self.store.put("health", h)
        self.publish(h)
        if self.cfg.heartbeat_path:
            tmp = self.cfg.heartbeat_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(h, f, sort_keys=True)
            os.replace(tmp, self.cfg.heartbeat_path)

    def publish(self, health: dict):
        """Snapshot for the web thread. The web layer never touches SQLite (one connection, one thread)."""
        props = {p["id"]: p["candidate_yaml"] for p in self.store.proposals("PENDING")}
        from .health import build as app_health
        snap = {"health": health, "app_health": app_health(self, self.clock(), self.cfg.db_path),
                "digest": approval.digest(self.store), "proposal_yaml": props,
                "debt": {"debt": self.store.get("dashboard_debt", {}),
                         "opportunities": self.store.get("opportunities", [])}}
        with self._lock:
            self._public = snap

    def public(self) -> dict:
        with self._lock:
            return self._public

    def run_forever(self, stop=lambda: False, sleep=time.sleep):
        while not stop():
            self.tick()
            if self.session is None:      # waiting to reconnect: do not spin
                sleep(max(0.5, min(5.0, self.next_connect - self.clock())))
