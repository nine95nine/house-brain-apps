"""App updates: find one pending update, review it, ask (or, in the owner-enabled night window,
auto-apply a bug-fix-level low-risk update), then backup -> update -> health watch -> automatic
restore of that App's own backup on failure (owner decision D7). Core/OS/Supervisor updates are
never touched by this module.

Transaction (journal ``txn.json``, kind ``update``), one App at a time:
  BACKING_UP -> BACKED_UP -> UPDATING -> UPDATED -> (DONE | RESTORING -> RESTORED)
After a restart ``recover`` reads the App's real version and continues from the facts, never
from the plan: an update that did not happen is reported as "nothing changed"; an update that
happened is health-checked; a restore in flight is verified.

Waiting period (0.5.0, owner decision 2026-10-04): with ``auto_low_risk`` an automatic install also
waits ``wait_days`` after this App first saw the version offered (other people find a bad release
first). While a low-risk update qualifies for automatic install it waits quietly for its time and the
night window instead of asking (INTENTIONALLY MODIFIED: 0.4.x asked when such an update was found
outside the window). Everything else still asks; ``ask`` mode is unchanged.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from . import approval, net
from .ha import ForbiddenCall, HAError, HomeAssistant, InstalledApp
from .journal import Journal
from .review import BLOCKED, LOW, Review, dependents_of, review

DONE = "DONE"
DRY_RUN_OK = "DRY_RUN_OK"
REJECTED = "REJECTED"
REFUSED = "REFUSED"
FAILED = "FAILED"
FAILED_MANUAL = "FAILED_MANUAL"
RESTORED = "RESTORED"
JOB = "UPDATE_APP"

ASK = "ask"
AUTO_LOW_RISK = "auto_low_risk"
TRACK_RECORD_NEEDED = 3


@dataclass
class Policy:
    mode: str = ASK                       # ask | auto_low_risk (owner option; ask is the default)
    window: tuple[int, int] = (2, 5)      # local hours [start, end) for automatic installs
    health_seconds: float = 180.0
    grace_seconds: float = 45.0
    reask_hours: float = 6.0              # 0.6.5: no answer asks again after this (owner option update_reask_hours)
    practice_hours: float = 24.0          # dry run: each version is reported once per this period (unchanged)
    poll: float = 5.0
    exclude: frozenset[str] = frozenset()
    wait_days: float = 3.0                # automatic installs wait this long after a version is first seen


@dataclass
class Outcome:
    request_id: str
    result_outcome: str
    reasons: list[str] = field(default_factory=list)
    facts: dict = field(default_factory=dict)


def request_id_for(slug: str, version: str) -> str:
    rid = re.sub(r"[^a-z0-9-]+", "-", f"upd-{slug[:36]}-{version[:20]}".lower()).strip("-")
    return rid[:64]


class Updater:
    def __init__(self, ha: HomeAssistant, journal: Journal, settings, policy: Policy) -> None:
        self.ha = ha
        self.j = journal
        self.s = settings        # jobs.Settings: notify service, owner, dry_run, approval board ...
        self.p = policy

    # -- memory ------------------------------------------------------------------
    def _mem(self) -> dict:
        mem = self.j.load_doc("updates", {})
        for key in ("quarantine", "declined", "asked", "blocked_noted", "practiced", "first_seen"):
            mem.setdefault(key, {})
        mem.setdefault("successes", 0)
        return mem

    def _save(self, mem: dict) -> None:
        self.j.save_doc("updates", mem)

    # -- selection and review -------------------------------------------------------
    def candidates(self) -> list[InstalledApp]:
        own = self.ha.self_slug()
        out = []
        for app in self.ha.installed_apps():
            if app.slug == own or app.slug in self.p.exclude:
                continue
            if app.update_available and app.version and app.version_latest and app.version != app.version_latest:
                out.append(app)
        return sorted(out, key=lambda a: a.slug)

    def _due(self, app: InstalledApp, mem: dict) -> bool:
        key = f"{app.slug}@{app.version_latest}"
        if mem["declined"].get(key):
            return False
        if self.s.dry_run:
            # Practice mode reports each version once per practice period (a day), not every hourly check.
            practiced = mem["practiced"].get(key)
            if practiced and time.time() - float(practiced) < self.p.practice_hours * 3600:
                return False
        asked = mem["asked"].get(key)
        return not (asked and time.time() - float(asked) < self.p.reask_hours * 3600)

    def review_app(self, app: InstalledApp, mem: dict) -> Review:
        installed = self.ha.app_detail(app.slug)
        store = self.ha.store_detail(app.slug)
        own = self.ha.self_slug()
        others = []
        for other in self.ha.installed_apps():
            if other.slug not in (app.slug, own) and other.state in ("started", "startup"):
                try:
                    others.append(self.ha.app_detail(other.slug))
                except (HAError, net.NetError, ForbiddenCall):
                    continue
        unhealthy, _ = self.ha.health_flags()
        return review(name=app.name, installed=installed, store=store,
                      changelog=self.ha.changelog(app.slug), core_version=self.ha.core_version(),
                      unhealthy=unhealthy,
                      quarantined=mem["quarantine"].get(app.slug) == store.version_latest,
                      dependents=dependents_of(installed, others))

    def _in_window(self) -> bool:
        hour = time.localtime().tm_hour
        start, end = self.p.window
        return start <= hour < end if start <= end else (hour >= start or hour < end)

    def _window_open_ever(self) -> bool:
        start, end = self.p.window
        return start != end

    def _waited(self, rev: Review, mem: dict) -> float:
        """Days since this App first saw ``rev.to_version`` offered."""
        first = mem["first_seen"].get(f"{rev.slug}@{rev.to_version}")
        return (time.time() - float(first)) / 86400 if first else 0.0

    def auto_allowed(self, rev: Review, mem: dict) -> tuple[bool, str]:
        if self.p.mode != AUTO_LOW_RISK:
            return False, "update_mode is ask"
        if not rev.auto_eligible:
            return False, f"not a low-risk bug-fix update ({rev.verdict}, {rev.bump})"
        if int(mem["successes"]) < TRACK_RECORD_NEEDED:
            return False, f"needs {TRACK_RECORD_NEEDED} approved updates first ({mem['successes']} so far)"
        waited = self._waited(rev, mem)
        if waited < self.p.wait_days:
            return False, f"waiting {self.p.wait_days:g} days after release ({waited:.1f} so far)"
        if not self._in_window():
            return False, "outside the night window"
        return True, "low-risk bug-fix update in the night window"

    def waits_for_auto(self, rev: Review, mem: dict) -> bool:
        """True when this update will install automatically later, so it must not be asked now."""
        return (self.p.mode == AUTO_LOW_RISK and rev.auto_eligible and self._window_open_ever()
                and int(mem["successes"]) >= TRACK_RECORD_NEEDED)

    def _note_first_seen(self, apps: list[InstalledApp], mem: dict) -> None:
        now = time.time()
        offered = {f"{a.slug}@{a.version_latest}" for a in apps}
        before = dict(mem["first_seen"])
        mem["first_seen"] = {k: v for k, v in before.items() if k in offered}
        for key in offered:
            mem["first_seen"].setdefault(key, now)
        if mem["first_seen"] != before:
            self._save(mem)

    # -- one cycle -----------------------------------------------------------------
    def cycle(self) -> Outcome | None:
        """Handle at most one App update. Returns None when there is nothing to do."""
        mem = self._mem()
        apps = self.candidates()
        self._note_first_seen(apps, mem)
        for app in apps:
            if not self._due(app, mem):
                continue
            rev = self.review_app(app, mem)
            rid = request_id_for(app.slug, rev.to_version)
            facts = self._facts(rev)
            if rev.verdict == BLOCKED:
                key = f"{app.slug}@{rev.to_version}"
                if mem["blocked_noted"].get(key) == "|".join(rev.reasons):
                    continue
                mem["blocked_noted"][key] = "|".join(rev.reasons)
                self._save(mem)
                return Outcome(rid, REFUSED, [f"update held: {r}" for r in rev.reasons], facts)
            auto, why = self.auto_allowed(rev, mem)
            facts["automatic"] = auto
            facts["automatic_reason"] = why
            if self.s.dry_run:
                mem["practiced"][f"{app.slug}@{rev.to_version}"] = time.time()
                self._save(mem)
                return Outcome(rid, DRY_RUN_OK, ["dry run: reviewed, nothing asked or changed"], facts)
            if not auto and self.waits_for_auto(rev, mem):
                continue                          # installs by itself later (waiting period / night window)
            return self._ask_then_apply(app, rev, rid, facts, automatic=auto)
        return None

    @staticmethod
    def _facts(rev: Review) -> dict:
        return {"slug": rev.slug, "from": rev.from_version, "to": rev.to_version, "bump": rev.bump,
                "verdict": rev.verdict, "reasons": list(rev.reasons), "dependents": list(rev.dependents)}

    def _ask_then_apply(self, app: InstalledApp, rev: Review, rid: str, facts: dict, *, automatic: bool) -> Outcome:
        """The one path to an update: ask (unless automatic) -> backup -> update -> health watch -> restore."""
        key = f"{app.slug}@{rev.to_version}"
        if not automatic:
            mem = self._mem()
            mem["asked"][key] = time.time()
            self._save(mem)
            decision = self._ask(rid, rev)
            if decision != approval.APPROVE:
                if decision == approval.REJECT:
                    mem = self._mem()
                    mem["declined"][key] = True
                    self._save(mem)
                return Outcome(rid, REJECTED, [f"owner decision: {decision}"], facts)
        result = self.apply(rid, rev, prior_state=app.state, automatic=automatic)
        result.facts = {**facts, **result.facts}
        return result

    def ask_now(self, slug: str, version: str) -> Outcome:
        """0.6.5 ``ASK_UPDATE_NOW``: ask again right away for an App update this App already asked about and that
        is still waiting, instead of after the re-ask wait. Same candidates, same review and refusals, same
        backup -> update -> health watch -> restore path as ``cycle``; only the wait is skipped. Reject is final."""
        rid = request_id_for(slug, version)
        facts = {"slug": slug, "to": version, "automatic": False}
        app = next((a for a in self.candidates() if a.slug == slug), None)
        if app is None or app.version_latest != version:
            return Outcome(rid, REFUSED, [f"{slug} {version} is not an App update this App offers right now "
                                          "(not waiting, another version, this App itself or excluded); "
                                          "nothing asked"], facts)
        mem = self._mem()
        key = f"{slug}@{version}"
        if mem["declined"].get(key):
            return Outcome(rid, REFUSED, ["you rejected this update; a Reject is final and is never asked again"],
                           facts)
        if not mem["asked"].get(key):
            return Outcome(rid, REFUSED, ["this App has not asked you about this update yet; it asks on its own "
                                          "first, then it can be asked again now"], facts)
        rev = self.review_app(app, mem)
        facts = {**self._facts(rev), "automatic": False}
        if rev.verdict == BLOCKED:
            return Outcome(rid, REFUSED, [f"update held: {r}" for r in rev.reasons], facts)
        if self.s.dry_run:
            return Outcome(rid, DRY_RUN_OK, ["dry run: reviewed, nothing asked or changed"], facts)
        return self._ask_then_apply(app, rev, rid, facts, automatic=False)

    def _ask(self, rid: str, rev: Review) -> str:
        label = {LOW: "LOW RISK", "risky": "RISKY - read the reasons"}.get(rev.verdict, rev.verdict)
        lines = [
            f"Update {rev.name} {rev.from_version} -> {rev.to_version} ({rev.bump})?",
            f"Review: {label}",
            *[f"- {r}" for r in rev.reasons[:4]],
            "Plan: back up this App only -> update -> watch its health "
            f"{int(self.p.health_seconds // 60)} min -> restore its backup automatically if it fails.",
            "Home Assistant itself is not updated or restarted.",
        ]
        self.j.audit(rid, "APPROVAL_REQUESTED", job=JOB)
        decision = approval.ask(self.ha, self.s.notify_service, self.s.owner_user_id, stage="UPDATE",
                                title=f"Maintenance: update {rev.name[:40]}?", message="\n".join(lines) +
                                f"\n\nRequest: {rid}", timeout=self.s.approval_timeout,
                                require_auth=self.s.require_auth, board=self.s.board, open_url=self.s.open_url,
                                should_stop=getattr(self.s, "should_stop", None))
        self.j.audit(rid, "APPROVAL_" + decision.outcome, channel=decision.channel, ignored=decision.ignored_events)
        return decision.outcome

    # -- the transaction ---------------------------------------------------------------
    def apply(self, rid: str, rev: Review, *, prior_state: str, automatic: bool) -> Outcome:
        slug = rev.slug
        base_unhealthy, _ = self.ha.health_flags()
        deps_started = [d for d in rev.dependents if self.ha.app_detail(d).state == "started"]
        if not self.ha.core_alive():
            return Outcome(rid, REFUSED, ["Home Assistant Core is not answering; nothing changed"])
        txn = {"kind": "update", "request_id": rid, "slug": slug, "from": rev.from_version,
               "to": rev.to_version, "prior_state": prior_state, "stage": "BACKING_UP",
               "baseline_unhealthy": sorted(base_unhealthy), "deps_started": deps_started,
               "automatic": automatic, "backup": None}
        self.j.save_txn(txn)
        self.j.audit(rid, "UPDATE_BACKING_UP", slug=slug, automatic=automatic)
        try:
            with self.ha.update_target(slug):
                name = f"hbm {slug} {rev.from_version} before {rev.to_version}"
                bslug = self.ha.backup_app(slug, name)
                if self.ha.backup_app_version(bslug, slug) != rev.from_version:
                    self.j.clear_txn()
                    return Outcome(rid, FAILED, ["backup does not contain this App at its current version; "
                                                 "nothing was updated"], {"backup": bslug})
                txn.update(stage="BACKED_UP", backup=bslug)
                self.j.save_txn(txn)
                current = self.ha.app_detail(slug)
                if current.version != rev.from_version or (current.version_latest or rev.to_version) != rev.to_version:
                    self.j.clear_txn()
                    return Outcome(rid, REFUSED, ["the App or its offered version changed since the review; "
                                                  "nothing was updated"], {"backup": bslug})
                txn["stage"] = "UPDATING"
                self.j.save_txn(txn)
                self.j.audit(rid, "UPDATE_UPDATING", slug=slug, backup=bslug)
                try:
                    self.ha.update_app(slug)
                except (HAError, net.NetError) as err:
                    self.j.audit(rid, "UPDATE_CALL_ERROR", error=net.redact(str(err))[:200])
        except ForbiddenCall as err:
            self.j.clear_txn()
            return Outcome(rid, REFUSED, [f"not allowed: {err}"])
        except (HAError, net.NetError) as err:
            return self._interrupted(txn, net.redact(str(err))[:200])
        return self._verify(txn)

    def _interrupted(self, txn: dict, reason: str) -> Outcome:
        if txn.get("stage") in ("BACKING_UP", "BACKED_UP"):
            self.j.clear_txn()
            return Outcome(txn["request_id"], FAILED, [f"stopped before the update ({reason}); nothing changed"])
        return Outcome(txn["request_id"], FAILED_MANUAL, [f"interrupted ({reason}); it is re-checked on the next poll"])

    def _verify(self, txn: dict) -> Outcome:
        rid, slug = txn["request_id"], txn["slug"]
        detail = self.ha.app_detail(slug)
        deadline = time.monotonic() + self.p.health_seconds
        while detail.version == txn["from"] and time.monotonic() < deadline:
            # The Supervisor may still be pulling/building (e.g. after an App restart mid-update).
            time.sleep(self.p.poll)
            detail = self.ha.app_detail(slug)
        if detail.version == txn["from"]:
            self.j.clear_txn()
            return Outcome(rid, FAILED, ["the update did not happen (still on the old version); nothing changed"],
                           {"backup": txn.get("backup")})
        txn["stage"] = "UPDATED"
        self.j.save_txn(txn)
        ok, why = self._watch(txn)
        if ok:
            self.j.clear_txn()
            mem = self._mem()
            mem["successes"] = int(mem["successes"]) + 1
            self._save(mem)
            self.j.audit(rid, "UPDATE_HEALTHY", slug=slug, to=txn["to"])
            return Outcome(rid, DONE, [f"updated {slug} {txn['from']} -> {txn['to']}; healthy after the watch"],
                           {"backup": txn.get("backup"), "automatic": txn.get("automatic")})
        return self._restore(txn, why)

    def _watch(self, txn: dict) -> tuple[bool, str]:
        slug, prior = txn["slug"], txn["prior_state"]
        start = time.monotonic()
        deadline = start + self.p.health_seconds
        while True:
            detail = self.ha.app_detail(slug)
            if detail.version != txn["to"]:
                return False, f"version is {detail.version}, expected {txn['to']}"
            elapsed = time.monotonic() - start
            if detail.state == "error" and elapsed >= self.p.grace_seconds:
                return False, "the App is in error state"
            if prior == "started" and elapsed >= self.p.grace_seconds and detail.state not in ("started",):
                return False, f"the App is {detail.state} (it was running before)"
            if time.monotonic() >= deadline:
                break
            time.sleep(self.p.poll)
        if prior == "started" and detail.state != "started":
            return False, f"the App is {detail.state} (it was running before)"
        if not self.ha.core_alive():
            return False, "Home Assistant Core stopped answering"
        unhealthy, _ = self.ha.health_flags()
        new = sorted(set(unhealthy) - set(txn.get("baseline_unhealthy") or []))
        if new:
            return False, "Home Assistant became unhealthy: " + ", ".join(new)[:120]
        for dep in txn.get("deps_started") or []:
            if self.ha.app_detail(dep).state != "started":
                return False, f"dependent App {dep} stopped"
        return True, "healthy"

    def _restore(self, txn: dict, why: str) -> Outcome:
        rid, slug = txn["request_id"], txn["slug"]
        mem = self._mem()
        mem["quarantine"][slug] = txn["to"]
        self._save(mem)
        txn.update(stage="RESTORING", failure=why)
        self.j.save_txn(txn)
        self.j.audit(rid, "UPDATE_RESTORING", slug=slug, reason=why)
        try:
            with self.ha.update_target(slug):
                self.ha.restore_app(txn["backup"], slug)
        except (HAError, net.NetError, ForbiddenCall) as err:
            self.j.audit(rid, "RESTORE_CALL_ERROR", error=net.redact(str(err))[:200])
        return self._verify_restore(txn)

    def _verify_restore(self, txn: dict) -> Outcome:
        rid, slug = txn["request_id"], txn["slug"]
        deadline = time.monotonic() + self.p.health_seconds
        detail = self.ha.app_detail(slug)
        while time.monotonic() < deadline:
            detail = self.ha.app_detail(slug)
            if detail.version == txn["from"] and (txn["prior_state"] != "started" or detail.state == "started"):
                break
            time.sleep(self.p.poll)
        facts = {"backup": txn.get("backup"), "failure": txn.get("failure"), "quarantined": txn["to"]}
        if detail.version == txn["from"] and (txn["prior_state"] != "started" or detail.state == "started"):
            self.j.clear_txn()
            return Outcome(rid, RESTORED, [f"update to {txn['to']} failed ({txn.get('failure')}); "
                                           f"restored {txn['from']} from its backup",
                                           f"{txn['to']} will not be offered again"], facts)
        self.j.clear_txn()
        return Outcome(rid, FAILED_MANUAL, [f"update failed ({txn.get('failure')}) and the automatic restore did "
                                            f"not bring back {txn['from']} (now {detail.version}, {detail.state})",
                                            f"restore backup {txn.get('backup')} manually; automation is paused"],
                       facts)

    # -- recovery after a restart ----------------------------------------------------------
    def recover(self, txn: dict) -> Outcome:
        stage = txn.get("stage")
        rid = str(txn.get("request_id", "unknown"))
        try:
            if stage in ("BACKING_UP", "BACKED_UP"):
                self.j.clear_txn()
                return Outcome(rid, FAILED, ["the App restarted before the update; nothing changed"])
            if stage in ("UPDATING", "UPDATED"):
                return self._verify(txn)
            if stage == "RESTORING":
                return self._verify_restore(txn)
        except (HAError, net.NetError) as err:
            return Outcome(rid, FAILED_MANUAL, [f"recovery read-back failed: {net.redact(str(err))[:200]}"])
        self.j.clear_txn()
        return Outcome(rid, FAILED_MANUAL, ["unknown journalled update stage"])
