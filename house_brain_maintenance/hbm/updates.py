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

0.7.2 (owner-approved 2026-10-08): ``waiting`` lists the updates that were asked and timed out (status attributes
and the page's "Ask me again"), computed from this App's own memory and the offered versions the last check read
(no extra Supervisor call); an ask that ends TIMEOUT sends one informational "Missed" push (never for Reject).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from . import approval, net
from .ha import ForbiddenCall, HAError, HomeAssistant, InstalledApp
from .journal import Journal
from .manifest import RE_APP_SLUG, RE_UPDATE_VERSION
from .review import BLOCKED, LOW, Review, dependents_of, review
from .writer import WriterError, is_deployer

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


def utc_iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def missed_note(ha: HomeAssistant, notify_service: str, what: str, next_at: float, url: str | None = None) -> None:
    """0.7.2: one informational push after an update ask timed out (no actions; its own tag, so the result push
    that follows does not replace it). 0.9.0: it points to Approve / Decline on the Maintenance page."""
    lt = time.localtime(next_at)
    approval.inform(ha, notify_service, "Maintenance: missed update ask",
                    f"Missed: update {what}. Tap to approve or decline it on the Maintenance page, or I'll ask again "
                    f"at {lt.tm_hour:02d}:{lt.tm_min:02d} (local).", tag="hbm-missed", url=url)


def request_id_for(slug: str, version: str) -> str:
    rid = re.sub(r"[^a-z0-9-]+", "-", f"upd-{slug[:36]}-{version[:20]}".lower()).strip("-")
    return rid[:64]


class Updater:
    def __init__(self, ha: HomeAssistant, journal: Journal, settings, policy: Policy) -> None:
        self.ha = ha
        self.j = journal
        self.s = settings        # jobs.Settings: notify service, owner, dry_run, approval board ...
        self.p = policy
        self.holds = None        # 0.7.3: the hold list (holds.Holds); a held App update is never asked
        self.ai = None           # 0.8.9: aireview.AiReviews; no ask and no automatic install before the AI review
        self.queue: dict[str, dict] = {}   # 0.8.9: every offered App update and why it is (not) asked yet
        self.on_done = None      # 0.7.3: after-update check hook (kind, txn) on a DONE result
        self.snapshot = None     # 0.7.3: () -> integrations running now (afterupdate.loaded_snapshot)
        self.offered: dict[str, str] = {}    # 0.7.2: slug -> offered version, as the last check read them
        self.asking = ""                     # 0.7.2: the update whose ask is on the phone right now

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
        queue = {}
        for app in self.ha.installed_apps():
            if app.slug == own or app.slug in self.p.exclude:
                continue
            if app.update_available and app.version and app.version_latest and app.version != app.version_latest:
                key = f"{app.slug}@{app.version_latest}"
                why = self.held(app.slug, app.version_latest)
                old = self.queue.get(key) or {}
                queue[key] = {"kind": "app", "item": f"app:{key}", "name": app.name, "from": app.version,
                              "to": app.version_latest, "state": f"held: {why}" if why else old.get("state", "")}
                if not why:
                    out.append(app)
        self.queue = queue
        self.offered = {a.slug: str(a.version_latest) for a in out}
        return sorted(out, key=lambda a: a.slug)

    def held(self, slug: str, version: str | None) -> str | None:
        return self.holds.why_held("app", version, slug) if self.holds is not None else None

    def waiting(self) -> list[dict]:
        """0.7.2: App updates asked and not answered (timed out) that are still offered: not declined, not
        quarantined, not on the phone right now. Pure memory read; no Supervisor call. Empty in dry run."""
        if self.s.dry_run:
            return []
        mem = self._mem()
        out = []
        for slug, version in sorted(self.offered.items()):
            key = f"{slug}@{version}"
            asked = mem["asked"].get(key)
            if not asked or key == self.asking or mem["declined"].get(key) or mem["quarantine"].get(slug) == version:
                continue
            if not RE_APP_SLUG.fullmatch(slug) or not RE_UPDATE_VERSION.fullmatch(version):
                continue                                  # never anything but a plain slug and version
            out.append({"kind": "app", "slug": slug, "version": version, "asked_at": utc_iso(float(asked)),
                        "next_reask_at": utc_iso(float(asked) + self.p.reask_hours * 3600),
                        "state": "waiting_reask"})
        return out

    def _note(self, key: str, state: str) -> None:
        if key in self.queue:
            self.queue[key]["state"] = state

    def page_decided(self, slug: str, version) -> bool:
        """0.9.0: the owner decided this exact update on the Maintenance page: ask now (answered from the page)."""
        return approval.decided("UPDATE", request_id_for(slug, str(version)))

    def _due(self, app: InstalledApp, mem: dict) -> bool:
        key = f"{app.slug}@{app.version_latest}"
        if mem["declined"].get(key):
            self._note(key, "you rejected this version (final)")
            return False
        if self.page_decided(app.slug, app.version_latest):
            return True                                   # 0.9.0: no re-ask wait after a page decision
        if self.s.dry_run:
            # Practice mode reports each version once per practice period (a day), not every hourly check.
            practiced = mem["practiced"].get(key)
            if practiced and time.time() - float(practiced) < self.p.practice_hours * 3600:
                return False
        asked = mem["asked"].get(key)
        if asked and time.time() - float(asked) < self.p.reask_hours * 3600:
            self._note(key, "asked on your phone; asks again at "
                       + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(float(asked) + self.p.reask_hours * 3600)))
            return False
        return True

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
        apps.sort(key=lambda a: not self.page_decided(a.slug, a.version_latest))   # 0.9.0: page decisions first
        for app in apps:
            if not self._due(app, mem):
                continue
            rev = self.review_app(app, mem)
            rid = request_id_for(app.slug, rev.to_version)
            facts = self._facts(rev)
            key = f"{app.slug}@{rev.to_version}"
            if key in self.queue:
                self.queue[key].update(verdict=rev.verdict, reasons=list(rev.reasons[:4]))
            if rev.verdict == BLOCKED:
                self._note(key, "held by this App's review: " + "; ".join(rev.reasons)[:160])
                if mem["blocked_noted"].get(key) == "|".join(rev.reasons):
                    continue
                mem["blocked_noted"][key] = "|".join(rev.reasons)
                self._save(mem)
                return Outcome(rid, REFUSED, [f"update held: {r}" for r in rev.reasons], facts)
            decided = self.page_decided(app.slug, rev.to_version)
            gate = self.ai.gate(f"app:{key}", mem["first_seen"].get(key)) if self.ai is not None else None
            if gate is not None and not gate.proceed and not decided:
                self._note(key, "waiting for the AI review (then asked, or installed at night if low risk; asked "
                           f"anyway from {self.ai.ask_anyway_at(mem['first_seen'].get(key))})")
                continue
            auto, why = self.auto_allowed(rev, mem)
            if auto and gate is not None and not gate.auto_ok:
                auto, why = False, gate.line
            facts["automatic"] = auto
            facts["automatic_reason"] = why
            if gate is not None and gate.state != "off":
                facts["ai_review"] = gate.verdict or gate.state
            if self.s.dry_run:
                mem["practiced"][f"{app.slug}@{rev.to_version}"] = time.time()
                self._save(mem)
                return Outcome(rid, DRY_RUN_OK, ["dry run: reviewed, nothing asked or changed"], facts)
            if decided:
                auto, why = False, "decided by you on the Maintenance page"
                facts["automatic"], facts["automatic_reason"] = auto, why
            if not auto and not decided and (gate is None or gate.auto_ok) and self.waits_for_auto(rev, mem):
                self._note(key, f"low risk: installs by itself in the {self.p.window[0]:02d}-{self.p.window[1]:02d} h "
                           f"window once it is {self.p.wait_days:g} days old, with a backup, health watch and "
                           "automatic restore")
                continue                          # installs by itself later (waiting period / night window)
            self._note(key, "installing by itself now" if auto else "asking on your phone now")
            return self._ask_then_apply(app, rev, rid, facts, automatic=auto, ai_line=gate.line if gate else "")
        return None

    @staticmethod
    def _facts(rev: Review) -> dict:
        return {"slug": rev.slug, "from": rev.from_version, "to": rev.to_version, "bump": rev.bump,
                "verdict": rev.verdict, "reasons": list(rev.reasons), "dependents": list(rev.dependents)}

    def _ask_then_apply(self, app: InstalledApp, rev: Review, rid: str, facts: dict, *, automatic: bool,
                        ai_line: str = "") -> Outcome:
        """The one path to an update: ask (unless automatic) -> backup -> update -> health watch -> restore."""
        key = f"{app.slug}@{rev.to_version}"
        if not automatic:
            mem = self._mem()
            asked = mem["asked"][key] = time.time()
            self._save(mem)
            self.asking = key
            try:
                decision = self._ask(rid, rev, ai_line)
            finally:
                self.asking = ""
            if decision != approval.APPROVE:
                if decision == approval.TIMEOUT:
                    missed_note(self.ha, self.s.notify_service, f"{rev.name} {rev.to_version}",
                                asked + self.p.reask_hours * 3600, url=getattr(self.s, "open_url", None))
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
        why = self.held(slug, version)
        if why:
            return Outcome(rid, REFUSED, [f"update held: {why}; nothing asked (release it first)"], facts)
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
        # 0.8.9: the owner asked for it, so no AI-review wait; a review that exists is still shown on the phone.
        gate = self.ai.gate(f"app:{slug}@{version}", None) if self.ai is not None else None
        line = gate.line if gate is not None and gate.state == "ok" else ""
        return self._ask_then_apply(app, rev, rid, facts, automatic=False, ai_line=line)

    def _ask(self, rid: str, rev: Review, ai_line: str = "") -> str:
        label = {LOW: "LOW RISK", "risky": "RISKY - read the reasons"}.get(rev.verdict, rev.verdict)
        lines = [
            f"Update {rev.name} {rev.from_version} -> {rev.to_version} ({rev.bump})?",
            f"Review: {label}",
            *[f"- {r}" for r in rev.reasons[:4]],
            *([ai_line] if ai_line else []),
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
    def _locked(self, rid: str, slug: str, step):
        """0.9.1: a step that changes the Deployer App runs only while this App holds the shared writer lock
        (``writer.py``); WRITER_BUSY and every other lock fault refuse before anything is written."""
        writer = getattr(self, "writer", None)
        if writer is None or not is_deployer(slug):
            return step(None)
        try:
            with writer.hold() as lease:
                if lease is not None:
                    self.j.audit(rid, "WRITER_HELD", slug=slug)
                return step(lease)
        except WriterError as err:
            code = str(err)
            self.j.audit(rid, "WRITER_REFUSED", slug=slug, code=code)
            why = ("the Deployer is writing right now (shared writer lock busy)" if code == "WRITER_BUSY"
                   else f"the shared writer lock is not usable ({code})")
            return Outcome(rid, REFUSED, [f"{why}; nothing changed; it is asked again later"], {"writer": code})

    def apply(self, rid: str, rev: Review, *, prior_state: str, automatic: bool) -> Outcome:
        return self._locked(rid, rev.slug, lambda lease: self._apply(rid, rev, prior_state=prior_state,
                                                                      automatic=automatic, lease=lease))

    def _apply(self, rid: str, rev: Review, *, prior_state: str, automatic: bool, lease=None) -> Outcome:
        slug = rev.slug
        base_unhealthy, _ = self.ha.health_flags()
        deps_started = [d for d in rev.dependents if self.ha.app_detail(d).state == "started"]
        if not self.ha.core_alive():
            return Outcome(rid, REFUSED, ["Home Assistant Core is not answering; nothing changed"])
        txn = {"kind": "update", "request_id": rid, "slug": slug, "from": rev.from_version,
               "to": rev.to_version, "prior_state": prior_state, "stage": "BACKING_UP",
               "baseline_unhealthy": sorted(base_unhealthy), "deps_started": deps_started,
               "automatic": automatic, "backup": None,
               "loaded_before": self.snapshot() if self.snapshot else None}
        self.j.save_txn(txn)
        self.j.audit(rid, "UPDATE_BACKING_UP", slug=slug, automatic=automatic)
        try:
            with self.ha.update_target(slug):
                if lease is not None:
                    lease.check()                          # 0.9.1: the lock is still ours before each change
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
                if lease is not None:
                    lease.check()                          # 0.9.1: still ours, before the update starts
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
        except WriterError as err:                         # 0.9.1: the lock changed under us before the update
            return self._interrupted(txn, f"shared writer lock: {err}")
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
            if self.on_done is not None:
                try:
                    self.on_done("app", txn)
                except Exception as err:  # noqa: BLE001 - the check is extra; it never turns DONE into a failure
                    self.j.audit(rid, "AFTER_UPDATE_START_FAILED", error=str(err)[:200])
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

    def undo(self, rid: str, slug: str, frm: str, to: str, backup: str, why: str) -> Outcome:
        """0.7.3 owner-approved Undo (after-update check): restore the App backup the update to ``to`` took."""
        return self._locked(rid, slug, lambda lease: self._undo(rid, slug, frm, to, backup, why))

    def _undo(self, rid: str, slug: str, frm: str, to: str, backup: str, why: str) -> Outcome:
        detail = self.ha.app_detail(slug)
        if detail.version != to:
            return Outcome(rid, FAILED, [f"undo: {slug} runs {detail.version}, not {to}; nothing changed"],
                           {"backup": backup})
        txn = {"kind": "update", "request_id": rid, "slug": slug, "from": frm, "to": to,
               "prior_state": "started" if detail.state == "started" else "stopped", "stage": "RESTORING",
               "backup": backup, "undo": True, "failure": why, "automatic": False}
        self.j.save_txn(txn)
        self.j.audit(rid, "UPDATE_UNDO", slug=slug, backup=backup, reason=why)
        try:
            with self.ha.update_target(slug):
                self.ha.restore_app(backup, slug)
        except (HAError, net.NetError, ForbiddenCall) as err:
            self.j.audit(rid, "RESTORE_CALL_ERROR", error=net.redact(str(err))[:200])
        return self._verify_restore(txn)

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
        back = detail.version == txn["from"] and (txn["prior_state"] != "started" or detail.state == "started")
        if txn.get("undo"):
            self.j.clear_txn()
            facts = {"backup": txn.get("backup"), "reason": txn.get("failure"), "undone": txn["to"]}
            if back:
                return Outcome(rid, RESTORED, [f"undo approved ({txn.get('failure')}): restored {slug} "
                                               f"{txn['from']} from its backup", f"{txn['to']} is on your hold list"],
                               facts)
            return Outcome(rid, FAILED_MANUAL, [f"undo: the restore did not bring back {slug} {txn['from']} (now "
                                                f"{detail.version}, {detail.state})",
                                                f"restore backup {txn.get('backup')} manually; automation is paused"],
                           facts)
        facts = {"backup": txn.get("backup"), "failure": txn.get("failure"), "quarantined": txn["to"]}
        if back:
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
