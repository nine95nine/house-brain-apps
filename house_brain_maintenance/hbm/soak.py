"""0.9.58 soak watch (owner decision 2026-11-08 "Tiers A + B", rules-first updates part 3).

The update path's own health watch lasts minutes and the after-update check (0.7.3) looks once after ten minutes.
An update that installed **by itself** (tier A or the tier B weekly batch) is watched for longer here:

* from the install until ``MAX_HOURS`` (72) later, one read every ``SPACING`` (10 minutes) at most: the App's
  ``/addons/<slug>/info`` (version, state), or ``/core/info`` for Core and the OS;
* the App is on another version than the one installed -> someone changed it (a restore, an approved update); the
  watch ends quietly;
* the App ran before and is not running (``stopped``/``error``) on **two reads in a row** at least ``SPACING`` apart
  -> ``SOAK_FAILED``: one ledger row ``<update request id>-soak`` (so the 90-day history check holds every further
  automatic update of this App, and the reversibility score drops by 3), one post on the tracking issue and one line
  in the next morning summary. Nothing is changed: the problem watch and self-healing keep their own paths;
* ``MIN_HOURS`` (24) clean is noted; ``MAX_HOURS`` clean ends the watch with one morning line "soak passed".

Read-only (``app_detail`` / ``core_alive`` / ``core_version``); never a phone ask.
"""
from __future__ import annotations

import contextlib
import time

from . import issues as I
from . import net
from .ha import HAError
from .reversibility import SOAK_FAILED

DOC = "soak"
MIN_HOURS = 24.0
MAX_HOURS = 72.0
SPACING = 600.0
MAX_ITEMS = 12
RUNNING = ("started",)


class Soak:
    def __init__(self, journal, ha, report, clock=time.time) -> None:
        self.j, self.ha, self.report, self.clock = journal, ha, report, clock
        self.hours_scale = 1.0                      # tests shrink the window

    def _doc(self) -> dict:
        doc = self.j.load_doc(DOC, {})
        doc = doc if isinstance(doc, dict) else {}
        doc.setdefault("items", {})
        doc.setdefault("overnight", [])
        return doc

    def _save(self, doc: dict) -> None:
        self.j.save_doc(DOC, doc)

    # -- start / state ----------------------------------------------------------------------
    def start(self, *, kind: str, slug: str, name: str, rid: str, to_version: str, prior_state: str = "started") -> None:
        """Called after an automatic install finished DONE (``kind`` app | core | os)."""
        doc = self._doc()
        now = self.clock()
        key = f"{kind}:{slug}" if kind == "app" else kind
        doc["items"][key] = {"kind": kind, "slug": slug, "name": I.scrub(str(name), 60), "rid": str(rid)[:64],
                             "to": str(to_version)[:40], "prior": prior_state, "started": now,
                             "until": now + MAX_HOURS * 3600 * self.hours_scale, "checked_at": 0.0,
                             "bad_since": 0.0, "day1": False, "checks": 0}
        doc["items"] = dict(list(doc["items"].items())[-MAX_ITEMS:])
        self._save(doc)
        self.j.audit(str(rid), "SOAK_WATCH", kind=kind, slug=slug, to=to_version, hours=MAX_HOURS)

    def status(self) -> dict:
        doc = self._doc()
        return {"watching": [{"name": it["name"], "to": it["to"], "hours": round((self.clock() - it["started"]) / 3600, 1)}
                             for it in doc["items"].values()]}

    # -- one read per item per SPACING ----------------------------------------------------------
    def _read(self, it: dict) -> tuple[str | None, str]:
        """(version, state) or (None, "") when unreadable."""
        try:
            if it["kind"] == "app":
                d = self.ha.app_detail(it["slug"])
                return d.version, d.state
            if not self.ha.core_alive():
                return it["to"], "stopped"
            return self.ha.core_version(), "started"
        except (HAError, net.NetError, KeyError, TypeError, ValueError):
            return None, ""

    def cycle(self) -> list[dict]:
        """Returns the items that ended this cycle (passed or failed), for tests and the status."""
        doc = self._doc()
        if not doc["items"]:
            return []
        now = self.clock()
        ended = []
        for key, it in list(doc["items"].items()):
            if now - float(it["checked_at"]) < SPACING * self.hours_scale:
                continue
            it["checked_at"] = now
            version, state = self._read(it)
            if version is None:
                continue                                          # unreadable: look again later
            it["checks"] = int(it["checks"]) + 1
            if version != it["to"]:
                doc["items"].pop(key)
                self.j.audit(it["rid"], "SOAK_ENDED", reason="version changed", now=version)
                ended.append({**it, "result": "changed"})
                continue
            running = state in RUNNING or it["prior"] not in RUNNING
            if not running:
                if it["bad_since"] and now - float(it["bad_since"]) >= SPACING * self.hours_scale:
                    doc["items"].pop(key)
                    self._failed(doc, it, state, now)
                    ended.append({**it, "result": SOAK_FAILED})
                    continue
                it["bad_since"] = it["bad_since"] or now
            else:
                it["bad_since"] = 0.0
            hours = (now - float(it["started"])) / (3600 * self.hours_scale)
            if hours >= MIN_HOURS and not it["day1"]:
                it["day1"] = True
                self.j.audit(it["rid"], "SOAK_DAY1_OK", kind=it["kind"], slug=it["slug"])
            if now >= float(it["until"]):
                doc["items"].pop(key)
                doc["overnight"] = (doc["overnight"] + [{"name": it["name"], "to": it["to"], "result": "passed", "at": now}])[-20:]
                self.j.audit(it["rid"], "SOAK_PASSED", kind=it["kind"], slug=it["slug"], hours=MAX_HOURS)
                ended.append({**it, "result": "passed"})
        self._save(doc)
        return ended

    def _failed(self, doc: dict, it: dict, state: str, now: float) -> None:
        hours = round((now - float(it["started"])) / (3600 * self.hours_scale), 1)
        rid = f"{it['rid']}-soak"
        self.j.record(rid, "soak", SOAK_FAILED)
        self.j.audit(it["rid"], "SOAK_FAILED", kind=it["kind"], slug=it["slug"], state=state, hours=hours)
        doc["overnight"] = (doc["overnight"] + [{"name": it["name"], "to": it["to"], "result": "failed", "hours": hours, "at": now}])[-20:]
        with contextlib.suppress(Exception):               # the ledger row is the record; the post is best effort
            self.report(f"### House Brain Maintenance: soak watch\n\n**{it['name']} `{it['to']}`** installed by itself "
                        f"{hours} h ago and is **{state}** now (it ran before). Nothing was changed: the problem watch "
                        f"handles the App itself. Automatic updates of this App are held for 90 days (ledger `{rid}`); "
                        f"its reversibility score drops by 3.")

    def overnight(self) -> list[str]:
        doc = self._doc()
        lines = []
        for e in doc["overnight"]:
            if e["result"] == "failed":
                lines.append(f"{e['name']} {e['to']} stopped running {e.get('hours', '?')} h after it updated itself; its automatic updates are held 90 days")
            else:
                lines.append(f"{e['name']} {e['to']} ran fine for 3 days after it updated itself")
        if doc["overnight"]:
            doc["overnight"] = []
            self._save(doc)
        return lines
