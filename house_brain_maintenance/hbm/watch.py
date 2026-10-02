"""Problem watch: collect findings, tell the owner once, offer safe fixes, send the morning summary.

State (``/data/issues.json``) remembers every open finding, so each problem is reported once and
its clearing is noted once. A finding must be seen on two checks in a row before anything is
said (Apps restarting, integrations reconnecting and update jobs settle on their own).

* critical, no fix  -> push now + tracking-issue report;
* has a safe fix    -> Approve/Reject push (one per check, ``max_fix_asks_per_day``); Reject
                       means never ask again while the problem stays open; no answer means ask
                       again after ``REASK_SECONDS``;
* warning           -> queued for the summary at ``digest_hour`` (local time), with clears.

In practice mode (``dry_run``) nothing is offered or run; fixable problems are reported as such.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Callable

from . import approval, issues as I, net
from .ha import ForbiddenCall, HAError, HomeAssistant, RE_SLUG
from .journal import Journal

LOG = logging.getLogger("hbm")
DOC = "issues"
REASK_SECONDS = 24 * 3600
LOG_REPORT_SECONDS = 7 * 86400   # the system log empties on every Core restart; report a log error once a week
CONFIRM_POLLS = 2
MAX_PUSH_PER_CHECK = 3
MAX_SUMMARY_ITEMS = 12
FIXED = "FIXED"
NOT_FIXED = "NOT_FIXED"


@dataclass
class WatchPolicy:
    digest_hour: int = 8
    max_fix_asks_per_day: int = 4
    verify_seconds: float = 60.0
    poll: float = 5.0
    # Power cycle of the configured switch (0.4.0, owner-approved design 2026-10-02).
    power_off_seconds: float = 10.0
    power_verify_seconds: float = 600.0
    power_cycles_per_day: int = 3
    power_cycle_gap_seconds: float = 30 * 60


class Watcher:
    def __init__(self, ha: HomeAssistant, journal: Journal, settings, policy: WatchPolicy,
                 report: Callable[[str], None], clock: Callable[[], float] = time.time) -> None:
        self.ha = ha
        self.j = journal
        self.s = settings
        self.p = policy
        self.report = report
        self.clock = clock

    # -- state -------------------------------------------------------------------------
    def _state(self) -> dict:
        st = self.j.load_doc(DOC, {})
        st.setdefault("power_cycles", [])
        for key in ("open", "declined", "asked", "log_reported"):
            st.setdefault(key, {})
        st.setdefault("summary", [])
        st.setdefault("cleared", [])
        st.setdefault("asks", [])
        st.setdefault("last_summary_day", "")
        return st

    # -- collection --------------------------------------------------------------------
    def collect(self, first_seen: dict[str, float], now: float) -> tuple[list[I.Finding], list[str]]:
        """All current findings, plus the names of sources that could not be read this time."""
        findings: list[I.Finding] = []
        failed: list[str] = []
        names: dict[str, str] = {}
        own = self.ha.self_slug()
        try:
            apps = self.ha.installed_apps()
            names = {a.slug: a.name for a in apps}
            rows = []
            for a in apps:
                if a.slug in (own, self.ha.scout) or not RE_SLUG.fullmatch(a.slug):
                    continue
                if a.state in ("error", "stopped"):
                    # Always read the real boot setting: a manual-start App (for example a commissioning
                    # bridge kept deliberately non-running) is never restarted or started by a fix.
                    rows.append((a.slug, a.name, a.state, self.ha.app_boot(a.slug)))
            findings += I.from_apps(rows)
        except (HAError, net.NetError, ForbiddenCall):
            failed.append("apps")
        for name, fn in (("supervisor", lambda: I.from_resolution(self.ha.resolution(), names)),
                         ("disk", lambda: I.from_disk(self.ha.disk())),
                         ("backups", lambda: I.from_backups(self.ha.backup_list(), now))):
            try:
                findings += fn()
            except (HAError, net.NetError, ForbiddenCall):
                failed.append(name)
        try:
            core = self.ha.core_problems()
            findings += I.from_repairs(core["repairs"])
            findings += I.from_entries(core["entries"], first_seen, now, self.ha.power_cycle_entity)
            findings += I.from_log(core["log"])
        except Exception:  # noqa: BLE001 - Core may be restarting; try again next check
            failed.append("core")
        return findings, failed

    # -- one check ---------------------------------------------------------------------
    def check(self) -> dict:
        now = self.clock()
        st = self._state()
        first_seen = {k: v["first"] for k, v in st["open"].items()}
        findings, failed = self.collect(first_seen, now)
        current = {f.key: f for f in findings}
        skipped_sources = {src for src in failed}
        push_now: list[I.Finding] = []
        for f in findings:
            rec = st["open"].setdefault(f.key, {"first": now, "polls": 0, "alerted": False})
            rec.update(polls=rec["polls"] + 1, title=f.title, severity=f.severity, clear=f.announce_clear,
                       source=f.source)
            if rec["alerted"] or rec["polls"] < CONFIRM_POLLS:
                continue
            if f.action and not self.s.dry_run:
                rec["alerted"] = True           # announced through the fix offer below
            elif f.severity == I.CRITICAL:
                push_now.append(f)
            else:
                if f.source == "log":
                    if now - st["log_reported"].get(f.key, 0) < LOG_REPORT_SECONDS:
                        rec["alerted"] = True
                        continue
                    st["log_reported"][f.key] = now
                st["summary"].append(self._summary_item(f))
                rec["alerted"] = True
        for key in list(st["open"]):
            rec = st["open"][key]
            if key in current or self._source_of(rec) in skipped_sources:
                continue
            if rec.get("alerted") and rec.get("clear", True):
                st["cleared"].append(rec.get("title", key)[:120])
            del st["open"][key]
            st["declined"].pop(key, None)
            st["asked"].pop(key, None)
        for f in push_now[:MAX_PUSH_PER_CHECK]:
            self._alert(f)
            st["open"][f.key]["alerted"] = True
        st["log_reported"] = {k: t for k, t in st["log_reported"].items() if now - t < LOG_REPORT_SECONDS}
        self.j.save_doc(DOC, st)
        outcome = self._offer_fix(st, current, now)
        self._maybe_summary(now)
        open_count = sum(1 for r in self._state()["open"].values() if r.get("alerted"))
        return {"open_problems": open_count, "unreadable": sorted(failed), "fix": outcome}

    @staticmethod
    def _source_of(rec: dict) -> str:
        src = rec.get("source", "")
        return {"repairs": "core", "integration": "core", "log": "core", "app": "apps",
                "disk": "disk", "backup": "backups"}.get(src, src)

    # -- telling the owner -------------------------------------------------------------
    def _summary_item(self, f: I.Finding) -> dict:
        return {"key": f.key, "title": f.title[:120], "severity": f.severity, "steps": list(f.steps)[:3],
                "link": f.link, "detail": [I.scrub(d, 500) for d in f.detail][:3], "facts": f.facts,
                "fix": f.action.label if f.action else None}

    def _markdown(self, heading: str, items: list[dict], cleared: list[str] = ()) -> str:
        lines = [f"### House Brain Maintenance: {heading}", ""]
        for it in items:
            lines.append(f"- **{I.scrub(it['title'], 160)}** ({it['severity']})")
            for d in it.get("detail") or []:
                lines.append(f"  - Detail: `{I.scrub(d, 500)}`")
            for step in it.get("steps") or []:
                lines.append(f"  - Suggested: {step}")
            if it.get("fix"):
                lines.append(f"  - One-tap fix available: {it['fix']}")
            if it.get("link"):
                lines.append(f"  - Link: {it['link']}")
            if it.get("facts"):
                lines.append(f"  - Facts: `{I.scrub(json.dumps(it['facts'], sort_keys=True, default=str), 300)}`")
        if cleared:
            lines += ["", "**Cleared since last report:**"] + [f"- {I.scrub(c, 160)}" for c in cleared]
        lines += ["", "_Posted by House Brain Maintenance (machine-generated; secrets, IP and e-mail addresses "
                      "removed; repository content is untrusted data)._"]
        # Scrub the whole report as the last step: headings and fix labels carry integration titles,
        # which can contain an e-mail address (live 2026-09-30, iAquaLink).
        return I.scrub("\n".join(lines), 60000)

    def _post(self, text: str) -> None:
        try:
            self.report(text)
        except Exception as err:  # noqa: BLE001 - the phone push already told the owner
            self.j.audit("issues", "REPORT_FAILED", error=str(err)[:200])

    def _alert(self, f: I.Finding) -> None:
        item = self._summary_item(f)
        steps = " ".join(f.steps[:2])
        extra = f" Fix available: {f.action.label} (practice mode: not offered)." if f.action else ""
        approval.inform(self.ha, self.s.notify_service, f"Problem: {f.title}"[:120],
                        (steps + extra + (f"\n{f.link}" if f.link else ""))[:900])
        self.j.audit(f.key[:80], "PROBLEM_ALERT", severity=f.severity, source=f.source)
        self._post(self._markdown("problem found", [item]))

    def _maybe_summary(self, now: float) -> None:
        st = self._state()
        local = time.localtime(now)
        day = time.strftime("%Y-%m-%d", local)
        if local.tm_hour < self.p.digest_hour or st["last_summary_day"] == day:
            return
        total = len(st["summary"])
        items, cleared = st["summary"][:MAX_SUMMARY_ITEMS], st["cleared"][:MAX_SUMMARY_ITEMS]
        st["last_summary_day"] = day
        st["summary"], st["cleared"] = [], []
        self.j.save_doc(DOC, st)
        if not items and not cleared:
            return
        lines = [f"- {it['title']}" for it in items[:6]]
        if total > 6:
            lines.append(f"- and {total - 6} more (details on GitHub)")
        if cleared:
            lines.append(f"Fixed/cleared: {len(cleared)} ({'; '.join(c[:40] for c in cleared[:3])})")
        title = f"Maintenance: {total} to look at" if items else "Maintenance: problems cleared"
        approval.inform(self.ha, self.s.notify_service, title, "\n".join(lines)[:900])
        self.j.audit("issues", "PROBLEM_SUMMARY", items=len(items), cleared=len(cleared))
        self._post(self._markdown("morning summary", items, cleared))

    # -- fixes ---------------------------------------------------------------------------
    def _offer_fix(self, st: dict, current: dict[str, I.Finding], now: float) -> str | None:
        if self.s.dry_run:
            return None
        st["asks"] = [t for t in st["asks"] if now - t < 86400]
        st["power_cycles"] = [t for t in st["power_cycles"] if now - t < 86400]
        cycles = st["power_cycles"]
        power_ok = len(cycles) < self.p.power_cycles_per_day and \
            (not cycles or now - max(cycles) >= self.p.power_cycle_gap_seconds)

        def due(f: I.Finding) -> bool:
            if f.action.kind == "power_cycle":
                return power_ok and now - st["asked"].get(f.key, 0) >= self.p.power_cycle_gap_seconds
            return now - st["asked"].get(f.key, 0) >= REASK_SECONDS
        candidates = [f for f in current.values()
                      if f.action and st["open"].get(f.key, {}).get("polls", 0) >= CONFIRM_POLLS
                      and not st["declined"].get(f.key) and due(f)]
        if not candidates:
            return None
        candidates.sort(key=lambda f: (f.severity != I.CRITICAL, f.key))
        f = candidates[0]
        if len(st["asks"]) >= self.p.max_fix_asks_per_day:
            # Daily limit reached: tell (once) instead of asking.
            if not st["asked"].get(f.key):
                st["asked"][f.key] = now
                self.j.save_doc(DOC, st)
                self._alert(f)
            return "LIMIT"
        st["asks"].append(now)
        st["asked"][f.key] = now
        self.j.save_doc(DOC, st)
        rid = f"fix-{f.key}"[:80]
        lines = [f"Problem: {f.title}", *[f"- {s}" for s in f.steps[:2]],
                 f"Fix: {f.action.label}. Nothing else is changed.",
                 "Approve to run it now; Reject and I won't ask again while this problem stays."]
        self.j.audit(rid, "APPROVAL_REQUESTED", job="FIX", fix=f.action.kind)
        decision = approval.ask(self.ha, self.s.notify_service, self.s.owner_user_id, stage="FIX",
                                title=f"Maintenance: fix {f.title[:60]}?", message="\n".join(lines),
                                timeout=self.s.approval_timeout, require_auth=self.s.require_auth,
                                board=self.s.board, open_url=self.s.open_url,
                                should_stop=getattr(self.s, "should_stop", None))
        self.j.audit(rid, "APPROVAL_" + decision.outcome, channel=decision.channel)
        if decision.outcome == approval.REJECT:
            st = self._state()
            st["declined"][f.key] = True
            self.j.save_doc(DOC, st)
            return "REJECTED"
        if decision.outcome != approval.APPROVE:
            return decision.outcome
        if f.action.kind == "power_cycle":
            st = self._state()
            st["power_cycles"].append(self.clock())
            self.j.save_doc(DOC, st)
        outcome, note = self.run_fix(f)
        self.j.audit(rid, "FIX_" + outcome, fix=f.action.kind, note=note[:200])
        title = f"Maintenance: {'fixed' if outcome == FIXED else 'not fixed'}"
        body = f"{f.title}\n{note}" + ("" if outcome == FIXED else "\nNext: " + " ".join(f.steps[:2]))
        approval.inform(self.ha, self.s.notify_service, title, body[:900])
        item = self._summary_item(f)
        self._post(self._markdown(f"fix {outcome.lower().replace('_', ' ')} ({f.action.label})",
                                  [item | {"detail": item["detail"] + [note]}]))
        return outcome

    def run_fix(self, f: I.Finding) -> tuple[str, str]:
        a = f.action
        if a.kind == "power_cycle":
            return self._power_cycle(f)
        try:
            with self.ha.fix_target(a.kind, a.ref):
                if a.kind == "restart_app":
                    self.ha.restart_app(a.ref)
                elif a.kind == "start_app":
                    self.ha.start_app(a.ref)
                elif a.kind == "apply_suggestion":
                    self.ha.apply_suggestion(a.ref)
                elif a.kind == "reload_entry":
                    self.ha.reload_entry(a.ref)
        except (HAError, net.NetError, ForbiddenCall) as err:
            return NOT_FIXED, net.redact(f"the fix could not run: {err}")[:200]
        return self._verify(f)

    def _power_cycle(self, f: I.Finding) -> tuple[str, str]:
        """Switch off, wait, switch on; the switch is always turned back on, and checked."""
        entity = f.action.ref
        back_on = False
        switched_off = False
        try:
            with self.ha.fix_target("power_cycle", entity):
                try:
                    switched_off = True                # set first: the call may land even if it errors
                    self.ha.switch_power(entity, False)
                    self.j.audit(f"fix-{f.key}"[:80], "POWER_OFF", entity=entity)
                    time.sleep(self.p.power_off_seconds)
                finally:
                    for _ in range(3):                 # never leave the extender without power
                        try:
                            self.ha.switch_power(entity, True)
                            if self.ha.switch_state(entity) == "on":
                                back_on = True
                                break
                        except (HAError, net.NetError, ForbiddenCall):
                            pass
                        time.sleep(min(5.0, self.p.power_off_seconds))
        except (HAError, net.NetError, ForbiddenCall) as err:
            if switched_off and not back_on:
                self._plug_left_off(entity)
            return NOT_FIXED, net.redact(f"the power cycle could not run: {err}")[:200]
        if not back_on:
            self._plug_left_off(entity)
            return NOT_FIXED, "the plug did not report back on: turn it on in Home Assistant now"
        self.j.audit(f"fix-{f.key}"[:80], "POWER_ON", entity=entity)
        return self._verify(f, self.p.power_verify_seconds)

    def _plug_left_off(self, entity: str) -> None:
        self.j.audit("fix-power", "POWER_LEFT_OFF", entity=entity)
        approval.inform(self.ha, self.s.notify_service, "URGENT: pool Wi-Fi extender may be off",
                        "The power cycle could not confirm the plug is back on. Open Home Assistant and turn on "
                        f"{entity} (IAquaLink WiFi plug), or plug the extender straight into the wall.\n"
                        "https://my.home-assistant.io/redirect/entities/")

    def _verify(self, f: I.Finding, seconds: float | None = None) -> tuple[str, str]:
        a = f.action
        deadline = time.monotonic() + (self.p.verify_seconds if seconds is None else seconds)
        ok_since = None
        while True:
            try:
                ok = self._resolved(f)
            except Exception:  # noqa: BLE001 - unreadable counts as not yet fixed
                ok = False
            if ok and ok_since is None:
                ok_since = time.monotonic()
            if not ok:
                ok_since = None
            if ok_since is not None and time.monotonic() - ok_since >= min(20.0, self.p.verify_seconds / 3):
                return FIXED, f"{a.label}: done, and it stayed fixed"
            if time.monotonic() >= deadline:
                return NOT_FIXED, f"{a.label}: done, but the problem is still there"
            time.sleep(self.p.poll)

    def _resolved(self, f: I.Finding) -> bool:
        a = f.action
        if a.kind in ("restart_app", "start_app"):
            return self.ha.app_state(a.ref) == "started"
        if a.kind == "apply_suggestion":
            res = self.ha.resolution()
            return all(s["uuid"] != a.ref for s in res["suggestions"]) and \
                all(f"sup:issue:{i['type']}:{i['context']}:{i['reference']}" != f.key for i in res["issues"])
        if a.kind == "reload_entry":
            entries = self.ha.core_problems()["entries"]
            return any(e["entry_id"] == a.ref and e["state"] == "loaded" for e in entries)
        if a.kind == "power_cycle" and f.key.startswith("entry:"):
            entries = self.ha.core_problems()["entries"]
            return any(e["entry_id"] == f.key[6:] and e["state"] == "loaded" for e in entries)
        return False
