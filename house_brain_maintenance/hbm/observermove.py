"""0.9.8 Engineering Observer from the store (owner pop-ups 2026-10-13: "Observer to the store", "One Approve does it
all"). The second step of the store move ("Scout first, then others", 2026-10-12).

The local Observer (``local_house_brain_engineering_observer``, installed by carrier) needed a chat and a
``STAGE_LOCAL_APP_SOURCE`` tap for every new version. From the House Brain store it updates like every store App
(review, AI review, backup, health watch, automatic restore).

Proposed by this App (never by a request) when the store Observer is installed. One owner Approve, then one
journalled transaction (``txn.json``, kind ``obsmove``):

  COPYING -> STOPPING_OLD -> STARTING_NEW -> WATCHING -> (DONE | UNDOING -> UNDONE)

* COPYING: the local Observer's three settings (Broker URL, write key, publish interval) are copied into the store
  Observer and read back by fingerprint. The key is registered as a secret and never logged.
* STOPPING_OLD: the local Observer is set to start manually and stopped, so two Observers never publish at once and
  a reboot cannot bring the old one back. It is never uninstalled.
* STARTING_NEW / WATCHING: the store Observer is started; its own log must show ``maintenance_publish=accepted``
  (the Broker took its first report, a new session from sequence 1) within ``watch_seconds``.
* Any failure after the copy: the store Observer is stopped and the local one is set to start at boot and started
  again (UNDONE). The local Observer's ``/data`` (its sequence state) is untouched, so it carries on. An undo only
  follows a watch that saw no accepted report, so the Broker still holds the local Observer's session.

After DONE this App uses the store Observer everywhere (key changes, health checks), recorded in ``observer_store``
so it survives a restart. Reject is final. ``CONTROL_AUTHORITY=NONE``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import approval, net
from .ha import LOCAL_OBSERVER, ForbiddenCall, HAError, HomeAssistant
from .journal import Journal

JOB = "OBSERVER_STORE_SWITCH"
STAGE = "OBSMOVE"
DOC = "observer_store"
TXN = "obsmove"
ACCEPTED = "maintenance_publish=accepted"
DONE, DRY_RUN_OK, REJECTED, REFUSED, UNDONE, FAILED_MANUAL = (
    "DONE", "DRY_RUN_OK", "REJECTED", "REFUSED", "UNDONE", "FAILED_MANUAL")


@dataclass
class ObserverMovePolicy:
    watch_seconds: float = 600.0        # the Observer publishes every 60-300 s (default 90 s)
    poll: float = 10.0
    reask_hours: float = 6.0
    practice_hours: float = 24.0


@dataclass
class Outcome:
    request_id: str
    result_outcome: str
    reasons: list[str] = field(default_factory=list)
    facts: dict = field(default_factory=dict)


class ObserverMove:
    def __init__(self, ha: HomeAssistant, journal: Journal, settings, policy: ObserverMovePolicy) -> None:
        self.ha, self.j, self.s, self.p = ha, journal, settings, policy

    def _mem(self) -> dict:
        mem = self.j.load_doc(DOC, {})
        mem = mem if isinstance(mem, dict) else {}
        for key in ("slug", "asked", "declined", "practiced", "done_at"):
            mem.setdefault(key, None)
        return mem

    def _save(self, mem: dict) -> None:
        self.j.save_doc(DOC, mem)

    @staticmethod
    def _recent(stamp, hours: float) -> bool:
        return bool(stamp) and time.time() - float(stamp) < hours * 3600

    def restore(self) -> str | None:
        """After a restart: use the recorded store Observer again while it is still installed. Returns its slug."""
        mem = self._mem()
        slug = mem["slug"]
        if not slug or self.ha.observer == slug:
            return None
        try:
            new = self.ha.store_observer_slug()
            installed = {a.slug for a in self.ha.installed_apps()}
        except (HAError, net.NetError):
            return None                             # retried on the next start; the configured Observer stays
        if slug != new or slug not in installed:
            self.j.audit("observer-store", "OBSERVER_STORE_GONE", slug=str(slug)[:80])
            mem.update(slug=None, done_at=None)
            self._save(mem)
            return None
        self.ha.retarget_observer(slug)
        return slug

    def _pair(self) -> tuple[str, str] | None:
        if self.ha.observer != LOCAL_OBSERVER:
            return None                             # already the store Observer, or a custom one
        new = self.ha.store_observer_slug()
        if new is None:
            return None
        installed = {a.slug for a in self.ha.installed_apps()}
        return (LOCAL_OBSERVER, new) if LOCAL_OBSERVER in installed and new in installed else None

    # -- one cycle -----------------------------------------------------------------------
    def cycle(self) -> Outcome | None:
        pair = self._pair()
        if pair is None:
            return None
        old, new = pair
        mem = self._mem()
        if mem["declined"] or mem["done_at"]:
            return None
        rid = f"observer-store-{new.split('_', 1)[0]}"
        if self._recent(mem["asked"], self.p.reask_hours) and not approval.decided(STAGE, rid):
            return None
        facts = {"old": old, "new": new}
        if self.s.dry_run:
            if self._recent(mem["practiced"], self.p.practice_hours):
                return None
            mem["practiced"] = time.time()
            self._save(mem)
            return Outcome(rid, DRY_RUN_OK, ["dry run: the Observer switch would be asked; nothing changed"], facts)
        mem["asked"] = time.time()
        self._save(mem)
        self.j.audit(rid, "APPROVAL_REQUESTED", job=JOB)
        decision = approval.ask(
            self.ha, self.s.notify_service, self.s.owner_user_id, stage=STAGE,
            title="Maintenance: use the store Engineering Observer?",
            message="\n".join((
                "Switch the Engineering Observer to the version from the House Brain store?",
                "Plan: copy its 3 settings (Broker address, key, interval) -> check them by fingerprint -> stop the "
                "local Observer and set it to manual start (kept, not removed) -> start the store Observer -> check "
                "the Broker accepts its reports. Any problem: the local one runs again automatically.",
                "Then Observer updates come as normal store updates (review, backup, health watch, restore).",
                "", f"Request: {rid}")),
            timeout=self.s.approval_timeout, require_auth=self.s.require_auth, board=self.s.board,
            open_url=self.s.open_url, should_stop=getattr(self.s, "should_stop", None))
        self.j.audit(rid, "APPROVAL_" + decision.outcome, channel=decision.channel, ignored=decision.ignored_events)
        if decision.outcome != approval.APPROVE:
            if decision.outcome == approval.REJECT:
                mem = self._mem()
                mem["declined"] = time.time()
                self._save(mem)
            return Outcome(rid, REJECTED, [f"owner decision: {decision.outcome}"], facts)
        out = self.apply(rid, old, new)
        out.facts = {**facts, **out.facts}
        return out

    # -- the switch-over -----------------------------------------------------------------
    def apply(self, rid: str, old: str, new: str) -> Outcome:
        try:
            if self.ha.app_detail(old).state != "started":
                return Outcome(rid, REFUSED, ["the local Observer is not running; nothing changed (start it, then it "
                                              "is asked again)"])
            if self.ha.app_detail(new).state not in ("stopped", "unknown"):
                return Outcome(rid, REFUSED, ["the store Observer is already running; stop it (this App starts it) "
                                              "and it is asked again. Nothing changed"])
        except (HAError, net.NetError) as err:
            return Outcome(rid, REFUSED, [f"could not read the Observers ({net.redact(str(err))[:160]}); nothing "
                                          "changed"])
        txn = {"kind": TXN, "request_id": rid, "old": old, "new": new, "stage": "COPYING", "started": time.time()}
        self.j.save_txn(txn)
        self.j.audit(rid, "OBSMOVE_COPYING", old=old, new=new)
        try:
            with self.ha.observer_move_target(old, new):
                url, fp = self.ha.copy_observer_settings()
                back = self.ha.store_observer_view()
                if back.broker_url != url or back.key_fingerprint != fp:
                    self.j.clear_txn()
                    return Outcome(rid, REFUSED, ["the store Observer does not read back the same settings; the "
                                                  "local Observer stays in use, nothing else changed"],
                                   {"key_fingerprint": fp[:12]})
                txn.update(stage="STOPPING_OLD", key_fingerprint=fp[:12], store_version=back.version)
                self.j.save_txn(txn)
                self.j.audit(rid, "OBSMOVE_STOPPING_OLD", fingerprint_prefix=fp[:12])
                self.ha.set_boot(old, "manual")
                self.ha.stop_app(old)
                txn["stage"] = "STARTING_NEW"
                self.j.save_txn(txn)
                self.j.audit(rid, "OBSMOVE_STARTING_NEW")
                self.ha.start_app(new)
        except ForbiddenCall as err:
            self.j.clear_txn()
            return Outcome(rid, REFUSED, [f"not allowed: {err}"])
        except (HAError, net.NetError) as err:
            return self._failed(txn, net.redact(str(err))[:200])
        txn["stage"] = "WATCHING"
        txn["watch_from"] = time.time()
        self.j.save_txn(txn)
        return self._watch(txn)

    def _watch(self, txn: dict) -> Outcome:
        rid, old, new = txn["request_id"], txn["old"], txn["new"]
        deadline = time.monotonic() + self.p.watch_seconds
        state = None
        while True:
            try:
                with self.ha.observer_move_target(old, new):
                    view = self.ha.store_observer_view()
                    state = view.state
                    log = self.ha.store_observer_log() if state == "started" else ""
            except (HAError, net.NetError, ForbiddenCall):
                log = ""
            if ACCEPTED in log:
                self.j.clear_txn()
                self.ha.retarget_observer(new)
                mem = self._mem()
                mem.update(slug=new, done_at=time.time())
                self._save(mem)
                self.j.audit(rid, "OBSMOVE_DONE", new=new)
                return Outcome(rid, DONE, [f"the store Observer ({new} {view.version}) publishes to the Broker (report "
                                           "accepted); it is the Observer from now on",
                                           "the local Observer is stopped and set to manual start (kept, not "
                                           "removed)"], {"store_version": view.version,
                                                         "key_fingerprint": txn.get("key_fingerprint")})
            if time.monotonic() >= deadline:
                break
            time.sleep(self.p.poll)
        return self._undo(txn, f"the store Observer did not get a report accepted in time (state: {state or 'unknown'})")

    def _failed(self, txn: dict, reason: str) -> Outcome:
        if txn.get("stage") == "COPYING":
            self.j.clear_txn()
            return Outcome(txn["request_id"], REFUSED, [f"stopped before anything was switched ({reason})"])
        return self._undo(txn, reason)

    def _undo(self, txn: dict, why: str) -> Outcome:
        rid, old, new = txn["request_id"], txn["old"], txn["new"]
        txn.update(stage="UNDOING", failure=why)
        self.j.save_txn(txn)
        self.j.audit(rid, "OBSMOVE_UNDOING", reason=why)
        errors = []
        try:
            with self.ha.observer_move_target(old, new):
                for step in (lambda: self.ha.stop_app(new), lambda: self.ha.set_boot(old, "auto"),
                             lambda: self.ha.start_app(old)):
                    try:
                        step()
                    except (HAError, net.NetError) as err:
                        errors.append(net.redact(str(err))[:120])
        except ForbiddenCall as err:
            errors.append(str(err)[:120])
        self.j.clear_txn()
        if errors:
            return Outcome(rid, FAILED_MANUAL, [f"Observer switch failed ({why}) and the undo did not finish: "
                                                + "; ".join(errors),
                                                "Settings -> Apps -> House Brain Engineering Observer (the local one) "
                                                "-> Start on boot on -> Start"])
        return Outcome(rid, UNDONE, [f"Observer switch failed ({why}); the local Observer runs again, nothing lost"],
                       {"failure": why})

    # -- recovery after a restart ----------------------------------------------------------
    def recover(self, txn: dict) -> Outcome:
        stage = txn.get("stage")
        rid = str(txn.get("request_id", "unknown"))
        if stage == "COPYING":
            self.j.clear_txn()
            return Outcome(rid, REFUSED, ["this App restarted before anything was switched; nothing changed"])
        if stage == "STOPPING_OLD":                  # the store Observer was never started: undo at once
            return self._undo(txn, "this App restarted in the middle of the switch")
        if stage in ("STARTING_NEW", "WATCHING"):
            # The store Observer may already be publishing. Once the Broker accepted its new session, starting the
            # local one again would be refused (its old session is behind), so watch first and undo only if no
            # report was accepted.
            return self._watch(txn)
        if stage == "UNDOING":
            return self._undo(txn, str(txn.get("failure") or "interrupted"))
        self.j.clear_txn()
        return Outcome(rid, FAILED_MANUAL, ["unknown journalled Observer switch stage"])
