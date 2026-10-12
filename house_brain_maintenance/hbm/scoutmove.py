"""0.9.3 Inventory Scout from the store (owner pop-up 2026-10-12, "Scout first, then others").

Before 0.9.3 the Scout was a local App: every new version needed a chat to file a ``STAGE_LOCAL_APP_SOURCE`` request
and the owner's tap. From the House Brain store it updates like every store App (review, AI review, backup, health
watch, restore). The pilot of the store move; Observer and Inspector follow, one at a time.

Steps (each needs the owner):

1. The owner installs "House Brain Maintenance Inventory Scout" from the House Brain store (never started: it is a
   one-shot App that this App runs).
2. This App sees it and asks once: "Switch the Inventory Scout to the store version?".
3. On Approve: the local Scout must be stopped with exactly its two settings. They are copied to the store Scout in
   memory (the key is registered as a secret and never logged), then read back: the store Scout must show the same
   Broker URL and the same key fingerprint. Only then does the store Scout become the Scout this App runs, and the
   switch is recorded (``scout_store``), so it survives a restart.
4. The local Scout is kept, stopped (it starts only on demand, and only the store one is started now). It is never
   uninstalled by this App.

A failed copy or read-back leaves the local Scout in use and changes nothing else. Reject is final.
``CONTROL_AUTHORITY=NONE``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import approval, net
from .ha import LOCAL_SCOUT, ForbiddenCall, HAError, HomeAssistant
from .journal import Journal

JOB = "SCOUT_STORE_SWITCH"
STAGE = "SCOUTMOVE"
DOC = "scout_store"
STOPPED_STATES = ("stopped", "unknown")
DONE, REFUSED, REJECTED, FAILED, DRY_RUN_OK = "DONE", "REFUSED", "REJECTED", "FAILED", "DRY_RUN_OK"


@dataclass
class Outcome:
    request_id: str
    result_outcome: str
    reasons: list[str]
    facts: dict = field(default_factory=dict)


class ScoutMove:
    def __init__(self, ha: HomeAssistant, journal: Journal, settings, reask_hours: float = 6.0) -> None:
        self.ha, self.j, self.s, self.reask_hours = ha, journal, settings, reask_hours

    def _mem(self) -> dict:
        mem = self.j.load_doc(DOC, {})
        mem = mem if isinstance(mem, dict) else {}
        for key in ("slug", "asked", "declined", "practiced", "done_at"):
            mem.setdefault(key, None)
        return mem

    def restore(self) -> str | None:
        """After a restart: use the recorded store Scout again while it is still installed. Returns the slug used."""
        mem = self._mem()
        slug = mem["slug"]
        if not slug or self.ha.scout == slug:
            return None
        try:
            new = self.ha.store_scout_slug()
            installed = {a.slug for a in self.ha.installed_apps()}
        except (HAError, net.NetError):
            return None                             # retried on the next start; the configured Scout stays
        if slug != new or slug not in installed:
            self.j.audit("scout-store", "SCOUT_STORE_GONE", slug=str(slug)[:80])
            mem.update(slug=None, done_at=None)
            self.j.save_doc(DOC, mem)
            return None
        self.ha.retarget_scout(slug)
        return slug

    def cycle(self) -> Outcome | None:
        if self.ha.scout != LOCAL_SCOUT:
            return None                             # already the store Scout, or a custom one: nothing to switch
        new = self.ha.store_scout_slug()
        if new is None or new not in {a.slug for a in self.ha.installed_apps()}:
            return None
        mem = self._mem()
        rid = f"scout-store-{new.split('_', 1)[0]}"
        if mem["declined"]:
            return None
        if mem["asked"] and time.time() - float(mem["asked"]) < self.reask_hours * 3600 \
                and not approval.decided(STAGE, rid):
            return None
        facts = {"old": LOCAL_SCOUT, "new": new}
        if self.s.dry_run:
            if mem["practiced"] and time.time() - float(mem["practiced"]) < 86400:
                return None
            mem["practiced"] = time.time()
            self.j.save_doc(DOC, mem)
            return Outcome(rid, DRY_RUN_OK, ["dry run: the Scout switch would be asked; nothing changed"], facts)
        mem["asked"] = time.time()
        self.j.save_doc(DOC, mem)
        self.j.audit(rid, "APPROVAL_REQUESTED", job=JOB)
        decision = approval.ask(
            self.ha, self.s.notify_service, self.s.owner_user_id, stage=STAGE,
            title="Maintenance: use the store Inventory Scout?",
            message="\n".join((
                "Switch the Inventory Scout to the version from the House Brain store?",
                "Plan: copy its two settings (Broker address and key) -> check them by fingerprint -> use the store "
                "Scout from now on. The local Scout is kept, stopped; nothing is uninstalled.",
                "Then Scout updates come as normal store updates (review, backup, health watch, restore).",
                "", f"Request: {rid}")),
            timeout=self.s.approval_timeout, require_auth=self.s.require_auth, board=self.s.board,
            open_url=self.s.open_url, should_stop=getattr(self.s, "should_stop", None))
        self.j.audit(rid, "APPROVAL_" + decision.outcome, channel=decision.channel, ignored=decision.ignored_events)
        if decision.outcome != approval.APPROVE:
            if decision.outcome == approval.REJECT:
                mem = self._mem()
                mem["declined"] = time.time()
                self.j.save_doc(DOC, mem)
            return Outcome(rid, REJECTED, [f"owner decision: {decision.outcome}"], facts)
        out = self.apply(rid, new)
        out.facts = {**facts, **out.facts}
        return out

    def apply(self, rid: str, new: str) -> Outcome:
        try:
            old = self.ha.scout_view()
            if old.state not in STOPPED_STATES:
                return Outcome(rid, REFUSED, [f"the local Scout is {old.state}; it must be stopped. Nothing changed; "
                                              "it is asked again later"])
            target = self.ha.store_scout_view(new)
            if target.state not in STOPPED_STATES:
                return Outcome(rid, REFUSED, [f"the store Scout is {target.state}; leave it stopped (this App starts "
                                              "it). Nothing changed"])
            self.j.audit(rid, "SCOUT_STORE_COPYING", new=new)
            url, fp = self.ha.copy_scout_to(new)
            back = self.ha.store_scout_view(new)
        except ForbiddenCall as err:
            return Outcome(rid, REFUSED, [f"not allowed: {err}"])
        except (HAError, net.NetError) as err:
            return Outcome(rid, FAILED, [f"the copy did not finish ({net.redact(str(err))[:160]}); the local Scout "
                                         "stays in use"])
        facts = {"key_fingerprint": fp[:12], "store_version": back.version}
        if back.broker_url != url or back.key_fingerprint != fp:
            return Outcome(rid, FAILED, ["the store Scout does not read back the same settings; the local Scout "
                                         "stays in use"], facts)
        self.ha.retarget_scout(new)
        mem = self._mem()
        mem.update(slug=new, done_at=time.time())
        self.j.save_doc(DOC, mem)
        self.j.audit(rid, "SCOUT_STORE_DONE", new=new, fingerprint_prefix=fp[:12])
        return Outcome(rid, DONE, [f"the store Scout ({new} {back.version}) is the Scout from now on; settings read "
                                   "back by fingerprint", "the local Scout is kept, stopped (not uninstalled)"], facts)
