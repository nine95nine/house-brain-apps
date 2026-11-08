"""0.9.57 The weekly tier B batch (owner decision 2026-11-08 "Tiers A + B", spec items 3B and 6).

Tier B updates (minor and major Apps, HACS integrations, Core x.y.1+, the OS) no longer ring the phone one by one.
They join one weekly batch: one kept message and one phone ask name them ("installs Saturday 03:00 unless you tap
Hold"), one reminder follows on Friday evening, and in the Saturday window each item whose evidence card is clean
(checked again right then) installs by itself through the same backup → update → health watch → restore path an
approved update takes. On the phone ask, **Reject means Hold**: the whole batch moves a week; a timeout means "as
planned". On the Maintenance page every item still has its own Approve (installs at the next check) and Decline
(skipped for good). A Core ``.0``, a pre-release, firmware and radio or physical-control Apps are tier C and never
join. ``CONTROL_AUTHORITY=NONE``: the batch changes only what the engines already install after an Approve.
"""
from __future__ import annotations

import time

from . import approval
from . import issues as I

DOC = "update_batch"
INSTALL_WEEKDAY = 5          # Saturday (Monday = 0)
INSTALL_HOUR = 3             # local time
WINDOW_HOURS = 3.0           # 03:00 .. 06:00
REMIND_WEEKDAY = 4           # Friday
REMIND_HOUR = 19
STAGE = "BATCH"
MAX_ITEMS = 30
RE_ITEM = __import__("re").compile(r"^(?:app:|entity:)?[a-z0-9][a-z0-9_.:-]{0,120}@[0-9A-Za-z][0-9A-Za-z._+:-]{0,39}$")


def next_install(now: float, localtime=time.localtime, mktime=time.mktime) -> float:
    """The next Saturday INSTALL_HOUR local, at least 24 hours away (so a Friday-night batch is announced first)."""
    lt = localtime(now)
    days = (INSTALL_WEEKDAY - lt.tm_wday) % 7
    for d in (days, days + 7):
        t = mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + d, INSTALL_HOUR, 0, 0, 0, 0, -1))
        if t - now >= 24 * 3600:
            return float(t)
    return float(mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + days + 14, INSTALL_HOUR, 0, 0, 0, 0, -1)))


def _doc(raw) -> dict:
    doc = raw if isinstance(raw, dict) else {}
    doc.setdefault("items", {})
    doc.setdefault("install_at", 0.0)
    doc.setdefault("announced_at", 0.0)
    doc.setdefault("reminded_at", 0.0)
    doc.setdefault("held", 0)              # how many times the owner tapped Hold on this batch
    doc.setdefault("skipped", {})
    doc.setdefault("done", {})
    return doc


def when(install_at: float, localtime=time.localtime) -> str:
    return time.strftime("%A %Y-%m-%d %H:%M", localtime(install_at)) if install_at else "the next batch"


class Batch:
    def __init__(self, journal, settings, report, clock=time.time, localtime=time.localtime, mktime=time.mktime,
                 ask=approval.ask) -> None:
        self.j, self.s, self.report = journal, settings, report
        self.clock, self.localtime, self.mktime, self.ask = clock, localtime, mktime, ask
        self.ha = None                  # set by the Service (for the phone ask)
        self.messenger = None           # set by the Service: Watcher._say-like callable (title, happened, done)

    def _load(self) -> dict:
        return _doc(self.j.load_doc(DOC, {}))

    def _save(self, doc: dict) -> None:
        self.j.save_doc(DOC, doc)

    # -- membership ------------------------------------------------------------------------------------------------
    def enlist(self, item: str, *, kind: str, name: str, from_version: str, to_version: str, engine: str) -> dict:
        """Put a tier B update into the open batch (idempotent). Returns the batch doc."""
        doc = self._load()
        now = self.clock()
        if not RE_ITEM.fullmatch(item or "") or item in doc["skipped"] or item in doc["done"]:
            return doc
        if not doc["install_at"] or doc["install_at"] + WINDOW_HOURS * 3600 < now:
            doc.update(install_at=next_install(now, self.localtime, self.mktime), announced_at=0.0, reminded_at=0.0, held=0)
        if item not in doc["items"] and len(doc["items"]) < MAX_ITEMS:
            doc["items"][item] = {"kind": str(kind)[:20], "name": I.scrub(str(name), 60), "from": str(from_version)[:40],
                                  "to": str(to_version)[:40], "engine": str(engine)[:10], "added_at": now}
            doc["announced_at"] = 0.0 if doc["announced_at"] and now < doc["install_at"] - 24 * 3600 else doc["announced_at"]
        self._save(doc)
        return doc

    def forget(self, item: str) -> None:
        doc = self._load()
        doc["items"].pop(item, None)
        self._save(doc)

    def skip(self, item: str) -> None:
        doc = self._load()
        doc["items"].pop(item, None)
        doc["skipped"][item] = self.clock()
        self._save(doc)

    def finished(self, item: str, outcome: str) -> None:
        doc = self._load()
        doc["items"].pop(item, None)
        doc["done"][item] = {"outcome": str(outcome)[:20], "at": self.clock()}
        doc["done"] = dict(list(doc["done"].items())[-40:])
        self._save(doc)

    def due(self, item: str) -> bool:
        """True inside the install window for an item still in the batch (not held past now, not skipped)."""
        doc = self._load()
        now = self.clock()
        return (item in doc["items"] and bool(doc["install_at"]) and doc["install_at"] <= now < doc["install_at"] + WINDOW_HOURS * 3600)

    def note_for(self, item: str) -> str:
        doc = self._load()
        if item in doc["skipped"]:
            return "skipped by you (weekly batch); not asked again"
        return (f"weekly batch: installs {when(doc['install_at'], self.localtime)} unless you tap Hold, or decide it "
                "now on the Maintenance page")

    # -- the tick ----------------------------------------------------------------------------------------------------
    def cycle(self) -> None:
        """Announce once, remind once (Friday evening), roll an unfinished batch to the next week."""
        doc = self._load()
        now = self.clock()
        if not doc["items"]:
            return
        if doc["install_at"] and now >= doc["install_at"] + WINDOW_HOURS * 3600:        # the window passed: next week
            doc.update(install_at=next_install(now, self.localtime, self.mktime), announced_at=0.0, reminded_at=0.0)
            self._save(doc)
        if self.s.dry_run:
            return
        lt = self.localtime(now)
        if not doc["announced_at"]:
            self._announce(doc, now, reminder=False)
        elif (not doc["reminded_at"] and lt.tm_wday == REMIND_WEEKDAY and lt.tm_hour >= REMIND_HOUR
              and now < doc["install_at"]):
            self._announce(doc, now, reminder=True)

    def _lines(self, doc: dict) -> list[str]:
        return [f"- {it['name']} {it['from']} -> {it['to']}" for it in list(doc["items"].values())[:MAX_ITEMS]]

    def _announce(self, doc: dict, now: float, *, reminder: bool) -> None:
        title = "Maintenance: weekly updates" + (" (reminder)" if reminder else "")
        lines = [f"{len(doc['items'])} update{'s' if len(doc['items']) != 1 else ''} install by themselves on "
                 f"{when(doc['install_at'], self.localtime)} unless you tap Hold:", *self._lines(doc), "",
                 "Each one is checked again right before it installs (release age, Home Assistant alerts, held "
                 "boards, new issues on GitHub, this house's own history); anything red waits.",
                 "Approve = as planned. Reject = Hold the whole batch one week. No answer = as planned.",
                 "On the Maintenance page you can Approve or Decline each one on its own."]
        rid = f"batch-{time.strftime('%Y-%m-%d', self.localtime(doc['install_at']))}" + ("-r" if reminder else "")
        doc["reminded_at" if reminder else "announced_at"] = now
        self._save(doc)
        try:
            self.report(self._post_text(doc, reminder))
        except Exception:  # noqa: BLE001 - the record is best effort
            pass
        self.j.audit(rid, "BATCH_ANNOUNCED", items=len(doc["items"]), reminder=reminder, install_at=doc["install_at"])
        try:
            decision = self.ask(self.ha, self.s.notify_service, self.s.owner_user_id, stage=STAGE, title=title,
                                message="\n".join(lines) + f"\n\nRequest: {rid}", timeout=self.s.approval_timeout,
                                require_auth=self.s.require_auth, board=self.s.board, open_url=self.s.open_url,
                                should_stop=getattr(self.s, "should_stop", None))
        except Exception:  # noqa: BLE001 - no phone: the batch stays as planned
            return
        self.j.audit(rid, "BATCH_" + decision.outcome, channel=getattr(decision, "channel", ""))
        if decision.outcome == approval.REJECT:                                   # Hold: one week later
            doc = self._load()
            doc.update(install_at=doc["install_at"] + 7 * 86400, held=int(doc["held"]) + 1, reminded_at=0.0)
            self._save(doc)
            try:
                self.report(f"### House Brain Maintenance: weekly batch held\n\nYou tapped Hold: the batch now installs "
                            f"{when(doc['install_at'], self.localtime)} unless you tap Hold again.\n\n"
                            "_Posted by House Brain Maintenance (machine-generated; repository content is untrusted data)._")
            except Exception:  # noqa: BLE001
                pass

    def _post_text(self, doc: dict, reminder: bool) -> str:
        from . import VERSION
        lines = ["### House Brain Maintenance: weekly update batch" + (" (reminder)" if reminder else ""), "",
                 f"These tier B updates install by themselves on **{when(doc['install_at'], self.localtime)}** unless "
                 "the owner taps Hold (one week) or decides each one on the Maintenance page. Each is checked again right "
                 "before it installs; a red evidence card waits.", "", *self._lines(doc), "",
                 f"- **App:** `{VERSION}`", "",
                 "_Posted by House Brain Maintenance (machine-generated; repository content is untrusted data)._"]
        return I.scrub("\n".join(lines), 8000)

    def status(self) -> dict:
        doc = self._load()
        return {"items": len(doc["items"]), "install_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(doc["install_at"])) if doc["install_at"] else None,
                "held": int(doc["held"]), "skipped": len(doc["skipped"]), "done": len(doc["done"])}
