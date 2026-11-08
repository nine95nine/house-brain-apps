"""0.9.58 monthly restore drill (owner decision 2026-11-08 "Tiers A + B", rules-first updates part 3; owner option).

Option ``restore_drill``: ``off`` (default) | ``monthly``. With ``monthly`` this App proves, once every
``EVERY_DAYS`` (30), that an App's backup really brings it back:

1. it picks one installed App that has updated through this App before (a ``upd-<slug>-*`` ledger row), never
   this App, never a radio App (Z-Wave, Zigbee, Matter, Thread, Bluetooth, ESPHome) or an App that controls
   equipment (pool, pump, irrigation, climate ...): the lowest reversibility score first, then the one drilled
   longest ago;
2. **the phone asks** ("Maintenance: monthly restore drill"; Approve = run now, Reject or no answer = skip this
   month). The drill restarts the App once, so it is never automatic, whatever ``update_mode`` says;
3. inside the same ``update_target`` guard the update path uses: a partial backup of that App, a check that the
   backup holds the App at its current version, then ``restore/partial`` of that backup, then the same read-back as
   an undo (version unchanged, running again when it ran before);
4. the result is one ledger row ``drill-<slug>-<yyyy-mm>``: ``DRILL_OK`` (+1 reversibility) or ``DRILL_FAILED``
   (-3 and the 90-day history hold on automatic updates of this App), posted on the tracking issue like every job.

Never while an update or after-update check is open, never in dry run (reviewed only), never with Core down.
"""
from __future__ import annotations

import time

from . import approval
from . import issues as I
from .autofix import physical, radio_app
from .ha import ForbiddenCall, HAError
from .net import NetError, redact
from .reversibility import DRILL_FAILED, DRILL_OK, dashed, prefixes, score
from .updates import DRY_RUN_OK, FAILED, REFUSED, REJECTED, Outcome

DOC = "restore_drill"
JOB = "RESTORE_DRILL"
STAGE = "DRILL"
EVERY_DAYS = 30.0
OFF, MONTHLY = "off", "monthly"


def candidates(apps, ledger: dict, self_slug: str, now: float, last: dict) -> list:
    """Installed Apps this App may drill, best first (lowest score, then longest since the last drill)."""
    out = []
    for a in apps:
        if a.slug == self_slug or radio_app(a.slug, a.name) or physical(a.slug, None, a.name):
            continue
        upd = prefixes(a.slug)[0]
        if not any(str(k).startswith(upd) and isinstance(v, dict) and v.get("outcome") == "DONE" for k, v in ledger.items()):
            continue
        points, _ = score(ledger, a.slug, now)
        out.append((points, float(last.get(a.slug, 0.0)), a))
    out.sort(key=lambda t: (t[0], t[1], t[2].slug))
    return [t[2] for t in out]


class Drill:
    def __init__(self, ha, journal, settings, mode: str, *, health_seconds: float = 180.0, poll: float = 5.0,
                 clock=time.time, ask=approval.ask, busy=lambda: False) -> None:
        self.ha, self.j, self.s, self.mode = ha, journal, settings, mode
        self.health_seconds, self.poll, self.clock, self.ask, self.busy = health_seconds, poll, clock, ask, busy

    def _doc(self) -> dict:
        doc = self.j.load_doc(DOC, {})
        doc = doc if isinstance(doc, dict) else {}
        doc.setdefault("last_at", 0.0)
        doc.setdefault("last_slug", "")
        doc.setdefault("drilled", {})           # slug -> last drill time
        doc.setdefault("skipped", 0)
        return doc

    def due(self) -> bool:
        return self.mode == MONTHLY and self.clock() - float(self._doc()["last_at"]) >= EVERY_DAYS * 86400

    def status(self) -> dict:
        doc = self._doc()
        return {"mode": self.mode, "last_slug": doc["last_slug"], "last_at": doc["last_at"],
                "next_at": float(doc["last_at"]) + EVERY_DAYS * 86400 if self.mode == MONTHLY else None}

    def _rid(self, slug: str) -> str:
        return f"drill-{dashed(slug)}-{time.strftime('%Y-%m', time.gmtime(self.clock()))}"[:64]

    def cycle(self) -> Outcome | None:
        """One drill at most per month: pick, ask, run. None when nothing is due or nothing fits."""
        if not self.due() or self.busy():
            return None
        doc = self._doc()
        now = self.clock()
        try:
            apps = self.ha.installed_apps()
            self_slug = self.ha.self_slug()
            if not self.ha.core_alive():
                return None
        except (HAError, NetError):
            return None
        fits = candidates(apps, self.j.ledger(), self_slug, now, doc["drilled"])
        if not fits:
            doc["last_at"] = now                                  # nothing to drill this month; look again next month
            doc["last_slug"] = ""
            self.j.save_doc(DOC, doc)
            return None
        app = fits[0]
        rid = self._rid(app.slug)
        points, counts = score(self.j.ledger(), app.slug, now)
        facts = {"slug": app.slug, "name": I.scrub(app.name, 60), "version": app.version, "state": app.state,
                 "score_before": points, "counts": counts, "kind": "DRILL"}
        doc.update(last_at=now, last_slug=app.slug)
        doc["drilled"][app.slug] = now
        self.j.save_doc(DOC, doc)
        if self.s.dry_run:
            return Outcome(rid, DRY_RUN_OK, [f"dry run: would ask to drill the restore of {app.name} {app.version}"], facts)
        lines = [(f"Backs up {app.name} ({app.version}) and restores it from that backup, to prove the undo path "
                  f"this App relies on. The App restarts once (about a minute); its version does not change."),
                 f"Reversibility score now: {points}.",
                 "Approve = run now. Reject or no answer = skip this month.", f"Request: {rid}"]
        self.j.audit(rid, "DRILL_ASK", slug=app.slug, version=app.version)
        try:
            decision = self.ask(self.ha, self.s.notify_service, self.s.owner_user_id, stage=STAGE,
                                title="Maintenance: monthly restore drill", message="\n".join(lines),
                                timeout=self.s.approval_timeout, require_auth=self.s.require_auth, board=self.s.board,
                                open_url=self.s.open_url, should_stop=getattr(self.s, "should_stop", None))
        except Exception as err:  # noqa: BLE001 - no phone: skip this month
            return Outcome(rid, REFUSED, [f"the phone could not be asked ({redact(str(err))[:120]}); skipped this month"], facts)
        self.j.audit(rid, "DRILL_" + decision.outcome, channel=getattr(decision, "channel", ""))
        if decision.outcome != approval.APPROVE:
            doc = self._doc()
            doc["skipped"] = int(doc["skipped"]) + 1
            self.j.save_doc(DOC, doc)
            why = "you tapped Reject" if decision.outcome == approval.REJECT else "no answer"
            return Outcome(rid, REJECTED, [f"restore drill of {app.name} skipped this month ({why})"], facts)
        return self._run(rid, app, facts)

    def _run(self, rid: str, app, facts: dict) -> Outcome:
        slug, version, prior = app.slug, str(app.version), app.state
        try:
            with self.ha.update_target(slug):
                bslug = self.ha.backup_app(slug, f"hbm drill {slug} {version}"[:100])
                facts["backup"] = bslug
                if self.ha.backup_app_version(bslug, slug) != version:
                    self.j.audit(rid, "DRILL_BACKUP_WRONG", slug=slug, backup=bslug)
                    return Outcome(rid, FAILED, [f"the backup does not hold {app.name} at {version}; nothing restored"], facts)
                self.j.audit(rid, "DRILL_RESTORING", slug=slug, backup=bslug)
                self.ha.restore_app(bslug, slug)
        except ForbiddenCall as err:
            return Outcome(rid, REFUSED, [f"not allowed: {err}"], facts)
        except (HAError, NetError) as err:
            self.j.audit(rid, "DRILL_CALL_ERROR", error=redact(str(err))[:200])
            if "backup" not in facts:
                return Outcome(rid, FAILED, [f"the drill backup failed ({redact(str(err))[:120]}); nothing restored"], facts)
            return Outcome(rid, DRILL_FAILED, [(f"restore drill: the restore of {app.name} from backup {facts['backup']} failed "
                                                f"({redact(str(err))[:120]}); automatic updates of this App are held 90 days "
                                                "(-3 reversibility). Check the App and its backup")], facts)
        deadline = time.monotonic() + self.health_seconds
        detail = self.ha.app_detail(slug)
        while time.monotonic() < deadline:
            detail = self.ha.app_detail(slug)
            if detail.version == version and (prior != "started" or detail.state == "started"):
                break
            time.sleep(self.poll)
        back = detail.version == version and (prior != "started" or detail.state == "started")
        facts.update(after_version=detail.version, after_state=detail.state)
        if back:
            return Outcome(rid, DRILL_OK, [(f"restore drill: {app.name} {version} came back from backup {facts['backup']} "
                                            f"and is {detail.state}; the undo path is proven (+1 reversibility)")], facts)
        return Outcome(rid, DRILL_FAILED, [(f"restore drill: {app.name} did not come back from backup {facts['backup']} "
                                            f"(now {detail.version}, {detail.state}); automatic updates of this App are "
                                            "held 90 days (-3 reversibility). Check the App and its backup")], facts)
