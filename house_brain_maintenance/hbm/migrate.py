"""0.6.6 (Credential Autopilot R2, owner pop-up 2026-10-07 "Move it to the store"): switch the House Brain
Deployer from the local folder (``local_house_brain_deployer``, installed by carrier, Terminal and Rebuild) to
the same App from the House Brain store (``<hash>_house_brain_deployer``), so its updates become store updates.

Proposed by this App (never by a request) when both are installed. One owner Approve, then one transaction
(journal ``txn.json``, kind ``migrate``):

  COPYING -> COPIED -> STOPPING_OLD -> STARTING_NEW -> WATCHING -> (DONE | UNDOING -> UNDONE)

* COPYING: the old Deployer's options (the known names only, ``ha.DEPLOYER_OPTION_KEYS``; its old token
  included, so the new Deployer can retire it itself) are copied into the store Deployer, Pi-locally.
* STOPPING_OLD: the old Deployer is set to start manually and stopped (never uninstalled here).
* STARTING_NEW / WATCHING: the store Deployer is started; it must publish ``sensor.house_brain_deployer_status``
  as its own instance (its container host name) within ``watch_seconds``. A Deployer 0.3.7 waits in standby
  while another Deployer is active, so two never poll at once.
* Any failure after the copy: the store Deployer is stopped and the old one is set to start at boot and started
  again (UNDONE). Nothing is lost: the old Deployer's ``/data`` is untouched.

Seven days after a healthy switch it asks once more, separately: remove the stopped old Deployer (uninstall).
Refused while the Deployer reports ``DEPLOYING`` (an install waiting for the owner's approval).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import approval, net
from .ha import DEPLOYER_STATUS_ENTITY, MIGRATE_OLD, ForbiddenCall, HAError, HomeAssistant
from .journal import Journal

JOB = "MIGRATE_DEPLOYER"
DONE = "DONE"
DRY_RUN_OK = "DRY_RUN_OK"
REJECTED = "REJECTED"
REFUSED = "REFUSED"
UNDONE = "UNDONE"
FAILED_MANUAL = "FAILED_MANUAL"
REMOVE_AFTER_DAYS = 7.0


@dataclass
class MigratePolicy:
    watch_seconds: float = 1200.0       # an older Deployer that never says STOPPED counts as active for 15 minutes
    poll: float = 5.0
    reask_hours: float = 24.0
    practice_hours: float = 24.0
    remove_after_days: float = REMOVE_AFTER_DAYS


@dataclass
class Outcome:
    request_id: str
    result_outcome: str
    reasons: list[str] = field(default_factory=list)
    facts: dict = field(default_factory=dict)


def instance_of(slug: str) -> str:
    """The container host name the Supervisor gives an App (its slug with dashes): the Deployer's ``instance``."""
    return slug.replace("_", "-")


class Migrator:
    def __init__(self, ha: HomeAssistant, journal: Journal, settings, policy: MigratePolicy) -> None:
        self.ha = ha
        self.j = journal
        self.s = settings
        self.p = policy

    def _mem(self) -> dict:
        mem = self.j.load_doc("migrate", {})
        for key in ("asked", "practiced", "declined", "done_at", "remove_asked", "remove_declined", "removed"):
            mem.setdefault(key, None)
        return mem

    def _save(self, mem: dict) -> None:
        self.j.save_doc("migrate", mem)

    def _pair(self) -> tuple[str, str] | None:
        new = self.ha.store_deployer_slug()
        if new is None:
            return None
        installed = {a.slug for a in self.ha.installed_apps()}
        return (MIGRATE_OLD, new) if MIGRATE_OLD in installed and new in installed else None

    def _deployer_busy(self) -> bool:
        st = self.ha.entity_state(DEPLOYER_STATUS_ENTITY) or {}
        return str(st.get("state") or "").upper() == "DEPLOYING"

    @staticmethod
    def _recent(stamp: float | None, hours: float) -> bool:
        return bool(stamp) and time.time() - float(stamp) < hours * 3600

    # -- one cycle -----------------------------------------------------------------------
    def cycle(self) -> Outcome | None:
        pair = self._pair()
        if pair is None:
            return None
        old, new = pair
        mem = self._mem()
        if mem["done_at"]:
            return self._remove_cycle(old, new, mem)
        rid = f"mig-deployer-{time.strftime('%Y%m%d')}"
        facts = {"old": old, "new": new}
        if mem["declined"] or self._recent(mem["asked"], self.p.reask_hours):
            return None
        if self._deployer_busy():
            return None                              # asked again on a later check
        if self.s.dry_run:
            if self._recent(mem["practiced"], self.p.practice_hours):
                return None
            mem["practiced"] = time.time()
            self._save(mem)
            return Outcome(rid, DRY_RUN_OK, ["dry run: the Deployer switch-over would be asked; nothing changed"],
                           facts)
        mem["asked"] = time.time()
        self._save(mem)
        decision = self._ask(rid, (
            "Switch the House Brain Deployer to the store version?",
            "Plan: copy its settings to the store Deployer -> stop the old one (kept, not removed) -> start the "
            "store one -> check it reports in. Any problem: the old one is started again automatically.",
            "Then connect the store Deployer to GitHub once (3 taps; its page shows how).",
        ), "MIGRATE", "Maintenance: switch the Deployer to the store?")
        if decision != approval.APPROVE:
            if decision == approval.REJECT:
                mem = self._mem()
                mem["declined"] = time.time()
                self._save(mem)
            return Outcome(rid, REJECTED, [f"owner decision: {decision}"], facts)
        out = self.apply(rid, old, new)
        out.facts = {**facts, **out.facts}
        return out

    def _ask(self, rid: str, lines: tuple[str, ...], stage: str, title: str) -> str:
        self.j.audit(rid, "APPROVAL_REQUESTED", job=JOB)
        decision = approval.ask(self.ha, self.s.notify_service, self.s.owner_user_id, stage=stage, title=title,
                                message="\n".join(lines) + f"\n\nRequest: {rid}", timeout=self.s.approval_timeout,
                                require_auth=self.s.require_auth, board=self.s.board, open_url=self.s.open_url,
                                should_stop=getattr(self.s, "should_stop", None))
        self.j.audit(rid, "APPROVAL_" + decision.outcome, channel=decision.channel, ignored=decision.ignored_events)
        return decision.outcome

    # -- the switch-over ---------------------------------------------------------------------
    def apply(self, rid: str, old: str, new: str) -> Outcome:
        if self._deployer_busy():
            return Outcome(rid, REFUSED, ["the Deployer has an install waiting for your approval; nothing changed"])
        txn = {"kind": "migrate", "request_id": rid, "old": old, "new": new, "stage": "COPYING",
               "started": time.time()}
        self.j.save_txn(txn)
        self.j.audit(rid, "MIGRATE_COPYING", old=old, new=new)
        try:
            with self.ha.migrate_target(old, new):
                options = self.ha.deployer_options()
                if not options:
                    self.j.clear_txn()
                    return Outcome(rid, REFUSED, ["the old Deployer has no settings to copy; nothing changed"])
                self.ha.set_deployer_options(options)
                del options                                   # the old token leaves memory with it
                txn["stage"] = "STOPPING_OLD"
                self.j.save_txn(txn)
                self.j.audit(rid, "MIGRATE_STOPPING_OLD")
                self.ha.set_boot(old, "manual")
                self.ha.stop_app(old)
                txn["stage"] = "STARTING_NEW"
                self.j.save_txn(txn)
                self.j.audit(rid, "MIGRATE_STARTING_NEW")
                self.ha.start_app(new)
        except ForbiddenCall as err:
            self.j.clear_txn()
            return Outcome(rid, REFUSED, [f"not allowed: {err}"])
        except (HAError, net.NetError) as err:
            return self._failed(txn, net.redact(str(err))[:200])
        txn["stage"] = "WATCHING"
        self.j.save_txn(txn)
        return self._watch(txn)

    def _watch(self, txn: dict) -> Outcome:
        rid, new = txn["request_id"], txn["new"]
        want = instance_of(new)
        deadline = time.monotonic() + self.p.watch_seconds
        last = None
        while True:
            try:
                st = self.ha.entity_state(DEPLOYER_STATUS_ENTITY) or {}
            except (HAError, net.NetError, ForbiddenCall):
                st = {}
            attrs = st.get("attrs") or {}
            last = attrs.get("instance")
            if last == want and str(st.get("state") or "").upper() not in ("", "STOPPED", "ERROR", "STANDBY"):
                self.j.clear_txn()
                mem = self._mem()
                mem["done_at"] = time.time()
                self._save(mem)
                self.j.audit(rid, "MIGRATE_DONE", new=new)
                return Outcome(rid, DONE, [f"the store Deployer ({new}) is in charge; the old one is stopped and "
                                           "kept for 7 days", "next: connect it to GitHub on its page (3 taps)"],
                               {"version": attrs.get("version")})
            if time.monotonic() >= deadline:
                break
            time.sleep(self.p.poll)
        return self._undo(txn, f"the store Deployer did not report in (last seen: {last or 'nothing'})")

    def _failed(self, txn: dict, reason: str) -> Outcome:
        if txn.get("stage") == "COPYING":
            self.j.clear_txn()
            return Outcome(txn["request_id"], REFUSED, [f"stopped before anything was switched ({reason})"])
        return self._undo(txn, reason)

    def _undo(self, txn: dict, why: str) -> Outcome:
        rid, old, new = txn["request_id"], txn["old"], txn["new"]
        txn.update(stage="UNDOING", failure=why)
        self.j.save_txn(txn)
        self.j.audit(rid, "MIGRATE_UNDOING", reason=why)
        errors = []
        try:
            with self.ha.migrate_target(old, new):
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
            return Outcome(rid, FAILED_MANUAL, [f"switch-over failed ({why}) and the undo did not finish: "
                                                + "; ".join(errors),
                                                "Settings -> Apps -> House Brain Deployer (the local one) -> Start"])
        return Outcome(rid, UNDONE, [f"switch-over failed ({why}); the old Deployer runs again, nothing lost"],
                       {"failure": why})

    # -- remove the old Deployer (separate approval, 7 days later) ---------------------------------
    def _remove_cycle(self, old: str, new: str, mem: dict) -> Outcome | None:
        if mem["removed"] or mem["remove_declined"] or self.s.dry_run:
            return None
        if time.time() - float(mem["done_at"]) < self.p.remove_after_days * 86400:
            return None
        if self._recent(mem["remove_asked"], self.p.reask_hours):
            return None
        rid = f"mig-deployer-remove-{time.strftime('%Y%m%d')}"
        mem["remove_asked"] = time.time()
        self._save(mem)
        decision = self._ask(rid, (
            "Remove the old, stopped House Brain Deployer?",
            f"The store Deployer has been in charge for {self.p.remove_after_days:g} days. The old one only "
            "takes disk space. Its data stays in your backups.",
        ), "MIGRATE", "Maintenance: remove the old Deployer?")
        if decision != approval.APPROVE:
            if decision == approval.REJECT:
                mem = self._mem()
                mem["remove_declined"] = time.time()
                self._save(mem)
            return Outcome(rid, REJECTED, [f"owner decision: {decision}"], {"old": old})
        detail = self.ha.app_detail(old)
        if detail.state == "started":
            return Outcome(rid, REFUSED, ["the old Deployer is running again; nothing removed"], {"old": old})
        try:
            with self.ha.migrate_target(old, new, remove=True):
                self.ha.uninstall_app(old)
        except (HAError, net.NetError, ForbiddenCall) as err:
            return Outcome(rid, FAILED_MANUAL, [f"could not remove it ({net.redact(str(err))[:160]})"], {"old": old})
        mem = self._mem()
        mem["removed"] = time.time()
        self._save(mem)
        return Outcome(rid, DONE, ["the old local Deployer was removed"], {"old": old})

    # -- recovery after a restart ----------------------------------------------------------------
    def recover(self, txn: dict) -> Outcome:
        stage = txn.get("stage")
        rid = str(txn.get("request_id", "unknown"))
        if stage == "COPYING":
            self.j.clear_txn()
            return Outcome(rid, REFUSED, ["this App restarted before anything was switched; nothing changed"])
        if stage in ("COPIED", "STOPPING_OLD", "STARTING_NEW", "WATCHING"):
            return self._watch(txn)
        if stage == "UNDOING":
            return self._undo(txn, str(txn.get("failure") or "interrupted"))
        self.j.clear_txn()
        return Outcome(rid, FAILED_MANUAL, ["unknown journalled switch-over stage"])
