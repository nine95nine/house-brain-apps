"""0.7.3 after-update check (owner pop-up 2026-10-08: "After-update check").

Why: on 2026-10-08 Core 2026.10.0 passed the update's own health check (Core answers, runs normally), yet three
minutes later the GoveeLife integration was in ``setup_error``. Core health alone does not show that an
integration that ran before the update no longer starts.

* Before every Core, OS or App update the update path records which integrations were running (``loaded``,
  not disabled) in its journalled transaction. After a successful update (DONE) this check waits
  ``settle_seconds`` (option ``after_update_minutes``, default 10) and compares.
* Nothing broke: one line on the tracking issue, done.
* Something that ran before is now failing (``setup_error``, ``setup_retry``, ``migration_error``):
  1. the phone asks **Reload** (one tap) and the App reloads exactly those integrations and checks them;
  2. if any still fail, the phone asks **Undo the update**: Core goes back by the Home-Assistant-only restore of
     the full backup the update took; an App by restoring the App backup the update took. The undone version is
     put on the hold list, so it is not asked again until released. An OS update cannot be undone (no
     backup restores an OS); the check reports and leaves the fix to the problem watch.
  Reject on Undo keeps the update. No answer asks again after ``reask_hours``; after ``undo_hours`` (default 24)
  the offer closes. While a check is open no new update starts, and the problem watch does not ask about the
  same integrations (one prompt, not two).
* Only the existing calls are used: read ``config_entries/get``, reload one entry (``fix_target``), and the
  update paths' own restore calls (``system_target`` / ``update_target``). Never automatic: every change is
  owner-approved on the phone.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from . import approval, net
from .ha import ForbiddenCall, HAError, HomeAssistant
from .journal import Journal
from .updates import DONE, FAILED, Outcome

DOC = "after_update"
JOB = "AFTER_UPDATE_CHECK"
BROKEN_STATES = ("setup_error", "setup_retry", "migration_error")
LABEL = {"core": "Home Assistant Core", "os": "Home Assistant OS", "app": "App"}


@dataclass
class AfterPolicy:
    settle_seconds: float = 600.0      # wait after a successful update before comparing (option, minutes)
    undo_hours: float = 24.0           # how long Undo is offered
    reask_hours: float = 6.0           # no answer asks again after this (same as update_reask_hours)
    verify_seconds: float = 120.0      # how long a reload has to bring an integration back
    poll: float = 5.0
    enabled: bool = True


def loaded_snapshot(ha: HomeAssistant) -> list[dict] | None:
    """The integrations running right now (entry id, domain, title), or None when they cannot be read."""
    try:
        entries = ha.core_problems()["entries"]
    except (HAError, net.NetError, KeyError, TypeError):
        return None
    return [{"entry_id": e["entry_id"], "domain": e.get("domain", ""), "title": e.get("title", "")}
            for e in entries if e.get("state") == "loaded" and not e.get("disabled")]


def after_request_id(rid: str) -> str:
    return f"after-{rid}"[:64].rstrip("-")


class AfterUpdate:
    def __init__(self, ha: HomeAssistant, journal: Journal, settings, policy: AfterPolicy, *,
                 holds=None, system=None, updater=None, clock=time.time) -> None:
        self.ha = ha
        self.j = journal
        self.s = settings
        self.p = policy
        self.holds = holds
        self.system = system
        self.updater = updater
        self.clock = clock

    # -- state ---------------------------------------------------------------------------
    def active(self) -> dict | None:
        doc = self.j.load_doc(DOC, {})
        return doc if doc.get("request_id") else None

    def _save(self, doc: dict) -> None:
        self.j.save_doc(DOC, doc)

    def _clear(self) -> None:
        self.j.save_doc(DOC, {})

    def claimed(self) -> frozenset[str]:
        """Problem-watch keys this check is handling (the watch does not ask about them as well)."""
        doc = self.active()
        if not doc:
            return frozenset()
        return frozenset(f"entry:{e['entry_id']}" for e in doc.get("broken") or [])

    def start(self, kind: str, txn: dict) -> None:
        """Called by an update path right after a DONE result. ``txn`` carries ``loaded_before``."""
        before = txn.get("loaded_before")
        if not self.p.enabled or self.s.dry_run or not isinstance(before, list):
            return
        now = self.clock()
        self._save({"request_id": after_request_id(str(txn.get("request_id"))), "update_request": txn.get("request_id"),
                    "kind": kind, "slug": txn.get("slug"), "from": txn.get("from"), "to": txn.get("to"),
                    "backup": txn.get("backup"), "before": before, "broken": [], "stage": "WAITING",
                    "check_at": now + self.p.settle_seconds, "until": now + self.p.undo_hours * 3600,
                    "asked_at": 0.0})
        self.j.audit(str(txn.get("request_id")), "AFTER_UPDATE_WATCH", kind=kind, integrations=len(before))

    # -- reads ----------------------------------------------------------------------------
    def _broken(self, doc: dict) -> list[dict] | None:
        try:
            entries = {e["entry_id"]: e for e in self.ha.core_problems()["entries"]}
        except (HAError, net.NetError, KeyError, TypeError):
            return None
        out = []
        for e in doc["before"]:
            now = entries.get(e["entry_id"])
            if now is not None and not now.get("disabled") and now.get("state") in BROKEN_STATES:
                out.append({**e, "state": now["state"]})
        return out

    @staticmethod
    def _names(broken: list[dict]) -> str:
        return ", ".join(f"{b.get('title') or b.get('domain')}" for b in broken)[:200]

    def _what(self, doc: dict) -> str:
        if doc["kind"] == "app":
            return f"the {doc.get('slug')} update to {doc['to']}"
        return f"the {LABEL[doc['kind']]} update to {doc['to']}"

    def _can_undo(self, doc: dict) -> bool:
        return doc["kind"] in ("core", "app") and bool(doc.get("backup"))

    def _facts(self, doc: dict, broken: list[dict]) -> dict:
        return {"kind": doc["kind"].upper(), "slug": doc.get("slug"), "from": doc["from"], "to": doc["to"],
                "backup": doc.get("backup"), "update_request": doc.get("update_request"),
                "integrations_before": len(doc["before"]),
                "not_running": [{"domain": b["domain"], "title": b["title"], "state": b["state"]} for b in broken]}

    # -- one step per poll ------------------------------------------------------------------
    def cycle(self) -> Outcome | None:
        doc = self.active()
        if not doc:
            return None
        now = self.clock()
        rid = doc["request_id"]
        if doc["stage"] == "WAITING" and now < float(doc["check_at"]):
            return None
        broken = self._broken(doc)
        if broken is None:
            if now >= float(doc["until"]):
                self._clear()
                return Outcome(rid, FAILED, [f"after {self._what(doc)}: the integrations could not be read "
                                             "before the check closed; nothing was changed"], {"kind": doc["kind"].upper()})
            return None                                  # Core busy: try again next poll
        facts = self._facts(doc, broken)
        if not broken:
            self._clear()
            if doc["stage"] == "WAITING":
                return Outcome(rid, DONE, [f"after {self._what(doc)}: all {len(doc['before'])} integrations that "
                                           "ran before still run"], facts)
            return Outcome(rid, DONE, [f"after {self._what(doc)}: everything that ran before runs again"], facts)
        doc["broken"] = broken
        if doc["stage"] == "WAITING":
            doc["stage"] = "RELOAD"
            doc["asked_at"] = 0.0
        if now >= float(doc["until"]):
            self._clear()
            return Outcome(rid, FAILED, [f"after {self._what(doc)}: {self._names(broken)} still not running; "
                                         "the Undo offer has closed. The problem watch keeps offering its fixes"], facts)
        if doc.get("asked_at") and now - float(doc["asked_at"]) < self.p.reask_hours * 3600:
            self._save(doc)
            return None
        doc["asked_at"] = now
        self._save(doc)
        if doc["stage"] == "RELOAD":
            return self._reload_step(doc, broken, facts)
        return self._undo_step(doc, broken, facts)

    # -- reload ---------------------------------------------------------------------------
    def _reload_step(self, doc: dict, broken: list[dict], facts: dict) -> Outcome | None:
        rid = doc["request_id"]
        lines = [f"After {self._what(doc)} these integrations stopped working: {self._names(broken)}.",
                 "Fix: reload them (nothing else is changed).",
                 ("If that does not help, I offer to undo the update next." if self._can_undo(doc) else
                  "An OS update cannot be undone; the problem watch keeps offering fixes.")]
        decision = self._ask(rid, "Maintenance: reload after the update?", lines, "RELOAD")
        if decision == approval.APPROVE:
            still = self._reload(rid, broken)
            if not still:
                self._clear()
                return Outcome(rid, DONE, [f"after {self._what(doc)}: reloaded {self._names(broken)}; all run again"],
                               {**facts, "reloaded": True, "not_running": []})
            broken = still
            doc["broken"] = still
            facts = {**self._facts(doc, still), "reloaded": True}
        elif decision != approval.REJECT:
            return None                                  # no answer: asked again after reask_hours
        if not self._can_undo(doc):
            self._clear()
            return Outcome(rid, FAILED, [f"after {self._what(doc)}: {self._names(broken)} still not running",
                                         "an OS update cannot be undone; the problem watch keeps offering its fixes"],
                           facts)
        doc.update(stage="UNDO", asked_at=0.0)
        self._save(doc)
        return None                                      # the Undo ask follows on the next poll

    def _reload(self, rid: str, broken: list[dict]) -> list[dict]:
        for b in broken:
            try:
                with self.ha.fix_target("reload_entry", b["entry_id"]):
                    self.ha.reload_entry(b["entry_id"])
                self.j.audit(rid, "AFTER_UPDATE_RELOADED", domain=b["domain"])
            except (HAError, net.NetError, ForbiddenCall) as err:
                self.j.audit(rid, "AFTER_UPDATE_RELOAD_FAILED", domain=b["domain"], error=net.redact(str(err))[:200])
        deadline = time.monotonic() + self.p.verify_seconds
        ids = {b["entry_id"] for b in broken}
        while True:
            still = []
            try:
                entries = {e["entry_id"]: e for e in self.ha.core_problems()["entries"]}
                still = [b for b in broken if (entries.get(b["entry_id"]) or {}).get("state") != "loaded"]
            except (HAError, net.NetError, KeyError, TypeError):
                still = [b for b in broken if b["entry_id"] in ids]
            if not still or time.monotonic() >= deadline:
                return still
            time.sleep(self.p.poll)

    # -- undo -----------------------------------------------------------------------------
    def _undo_step(self, doc: dict, broken: list[dict], facts: dict) -> Outcome | None:
        rid = doc["request_id"]
        target = "Home Assistant (configuration and history)" if doc["kind"] == "core" else f"the {doc.get('slug')} App"
        lines = [f"{self._names(broken)} still not working after {self._what(doc)}.",
                 f"Undo: go back to {doc['from']} by restoring {target} from backup {doc.get('backup')}, taken right "
                 "before the update. Anything changed there since then is lost.",
                 f"{doc['to']} then goes on your hold list (not asked again until you release it).",
                 "Reject keeps the update."]
        decision = self._ask(rid, f"Maintenance: undo {self._what(doc)}?", lines, "UNDO")
        if decision == approval.REJECT:
            self._clear()
            return Outcome(rid, DONE, [f"you kept {self._what(doc)}; {self._names(broken)} still not running",
                                       "the problem watch keeps offering its fixes"], facts)
        if decision != approval.APPROVE:
            return None
        self._clear()
        item = f"core@{doc['to']}" if doc["kind"] == "core" else f"app:{doc.get('slug')}@{doc['to']}"
        if self.holds is not None:
            self.holds.hold(item, by=rid, note="undone after the after-update check")
        why = f"after-update check: {self._names(broken)} stopped working"
        if doc["kind"] == "core" and self.system is not None:
            out = self.system.undo(rid, str(doc["from"]), str(doc["to"]), str(doc["backup"]), why)
        elif doc["kind"] == "app" and self.updater is not None:
            out = self.updater.undo(rid, str(doc.get("slug")), str(doc["from"]), str(doc["to"]), str(doc["backup"]),
                                    why)
        else:
            return Outcome(rid, FAILED, ["undo is not available for this update; nothing changed"], facts)
        out.facts = {**facts, **out.facts, "held": item}
        return out

    def _ask(self, rid: str, title: str, lines: list[str], stage: str) -> str:
        self.j.audit(rid, "APPROVAL_REQUESTED", job=JOB, stage=stage)
        decision = approval.ask(self.ha, self.s.notify_service, self.s.owner_user_id, stage="AFTER",
                                title=title[:80], message="\n".join(lines) + f"\n\nRequest: {rid}",
                                timeout=self.s.approval_timeout, require_auth=self.s.require_auth,
                                board=self.s.board, open_url=self.s.open_url,
                                should_stop=getattr(self.s, "should_stop", None))
        self.j.audit(rid, "APPROVAL_" + decision.outcome, channel=decision.channel, stage=stage)
        return decision.outcome
