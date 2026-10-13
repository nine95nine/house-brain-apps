"""Problem watch: collect findings, tell the owner once, offer safe fixes, send the morning summary.

State (``/data/issues.json``) remembers every open finding, so each problem is reported once and
its clearing is noted once. A finding must be seen on two checks in a row before anything is
said (Apps restarting, integrations reconnecting and update jobs settle on their own).

* critical, no fix  -> push now + tracking-issue report;
* has a safe fix    -> Approve/Reject push (one per check, ``max_fix_asks_per_day``); Reject
                       means never ask again while the problem stays open; no answer means ask
                       again after ``WatchPolicy.reask_hours`` (0.7.2: the owner option
                       ``update_reask_hours``, default 6; it was a fixed 24 h);
* warning           -> queued for the summary at ``digest_hour`` (local time), with clears.

In practice mode (``dry_run``) nothing is offered or run; fixable problems are reported as such.

0.5.0: a backup copy off the Pi, low batteries / devices offline for days (read at most every
``DEVICE_SWEEP_SECONDS``, reused in between) and a disk-fill forecast from one free-space sample per
``DISK_SAMPLE_SECONDS``.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Callable

from . import backupcheck as B, housekeeping as K, pihealth as P, approval, issues as I, net
from . import problems as PR
from .ha import DEPLOYER_STATUS_ENTITY, INSPECTOR_SLUG, LOCAL_OBSERVER, ForbiddenCall, HAError, HomeAssistant, RE_SLUG
from .journal import Journal

LOG = logging.getLogger("hbm")
DOC = "issues"
LOG_REPORT_SECONDS = 7 * 86400   # the system log empties on every Core restart; report a log error once a week
CONFIRM_POLLS = 2
REDIAGNOSE_SECONDS = 6 * 3600
MAX_PUSH_PER_CHECK = 3
MAX_SUMMARY_ITEMS = 12
LIVENESS_DOC = "liveness"           # written by the Recovery Report's ping (0.5.1)
DEVICE_SWEEP_SECONDS = 6 * 3600
DISK_SAMPLE_SECONDS = 12 * 3600
DISK_SAMPLES_KEPT = 40
CREDENTIAL_SOURCES = ("credential", "credential_deployer", "credential_relay",   # 0.6.3 Credential Autopilot
                      "credential_keys")                                         # 0.8.0 automatic Broker keys
RELAY_DOC = "relay_credential"
RELAY_READ_EVERY = 20 * 3600       # the relay self-tests weekly; one read a day is plenty
FIXED = "FIXED"
NOT_FIXED = "NOT_FIXED"


@dataclass
class WatchPolicy:
    digest_hour: int = 8
    max_fix_asks_per_day: int = 4
    reask_hours: float = 6.0       # 0.7.2: a fix ask with no answer is asked again after this (update_reask_hours)
    verify_seconds: float = 60.0
    poll: float = 5.0
    # Power cycle of the configured switch (0.4.0, owner-approved design 2026-10-02).
    power_off_seconds: float = 10.0
    power_verify_seconds: float = 600.0
    power_cycles_per_day: int = 3
    power_cycle_gap_seconds: float = 30 * 60
    backup_report: bool = True     # 0.8.5: the monthly backup report on GitHub (+ one phone line)
    pi_health: bool = True         # 0.8.6: power/heat, disk errors, App outliers, weekly health score
    housekeeping: bool = True      # 0.8.7: failing automations, missing entities, error flood, database size


def summary_lists(summary: list[dict], cleared: list, open_keys: set[str]) -> tuple[list[dict], list[str]]:
    """0.8.3 (live summaries 2026-10-09..11): a problem that flaps (reported, gone for one check, back) was listed
    twice and also under "Cleared" while still open. Now: one item per key (the latest text); an item that is no
    longer open is marked "(cleared since)" instead of being listed again under Cleared; a "cleared" entry whose
    problem is open again is dropped. Old unkeyed (text) entries from before 0.8.3 are kept, once each."""
    latest: dict[str, dict] = {}
    for it in summary:
        latest[str(it.get("key") or it.get("title"))] = it        # keeps the first position, the latest text
    items, done = [], set()
    for key, it in latest.items():
        if key not in open_keys and it.get("key"):
            it = {**it, "title": f"{it['title']} (cleared since)"[:140]}
            done.add(key)
        items.append(it)
    out: list[str] = []
    for c in cleared:
        key, title = (c.get("key"), c.get("title")) if isinstance(c, dict) else (None, c)
        if not isinstance(title, str) or (key is not None and (key in open_keys or key in done)) or title in out:
            continue
        out.append(title)
    return items, out


class Watcher:
    def __init__(self, ha: HomeAssistant, journal: Journal, settings, policy: WatchPolicy,
                 report: Callable[[str], None], clock: Callable[[], float] = time.time) -> None:
        self.ha = ha
        self.j = journal
        self.s = settings
        self.p = policy
        self.report = report
        self.clock = clock
        # 0.6.3 Credential Autopilot: this App's own GitHub sign-in summary (set by the Service)
        self.credentials: Callable[[], dict] | None = None
        # 0.6.3: the relay's public dispatch-key view (option relay_credential_url; "" = off)
        self.relay_url = ""
        self.key_findings: Callable[[], list] | None = None   # 0.8.0: automatic Broker keys (failures only)
        self.os_findings: Callable[[], list] | None = None    # 0.8.3: an OS update waiting for a restart
        self.sysfs = P.SysReader()                            # 0.8.6: Pi health reads (tests point it elsewhere)
        self.pi_now: dict = {}                                # 0.8.6: this check's temperature/throttle/write rate
        self.log_now: list | None = None                      # 0.8.7: this check's system-log rows
        # 0.7.3: problem keys an open after-update check is handling (it asks Reload/Undo; no second prompt here)
        self.claimed: Callable[[], frozenset] = frozenset
        self.relay_fetch: Callable[[str], object] = lambda url: net.request("GET", url, timeout=15)[1]
        self.problem_ai: PR.ProblemReviews | None = None     # 0.9.6: AI problem reviews (set by the Service)

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
        st.setdefault("disk_samples", [])
        st.setdefault("device_sweep", {"at": 0, "rows": []})
        st.setdefault("stopped_since", {})
        return st

    # -- collection --------------------------------------------------------------------
    def kept_rollbacks(self) -> set[str]:
        """0.9.10: the local Observer / Inspector that a finished store switch stopped on purpose and keeps as the
        rollback. Supervisor shows them as ``error`` after the stop, which is not a problem to report. Only while the
        store one is in use (the switch retargets ``ha.observer`` / ``ha.inspector``)."""
        kept = set()
        if self.ha.observer != LOCAL_OBSERVER:
            kept.add(LOCAL_OBSERVER)
        if self.ha.inspector != INSPECTOR_SLUG:
            kept.add(INSPECTOR_SLUG)
        return kept

    def collect(self, first_seen: dict[str, float], now: float) -> tuple[list[I.Finding], list[str]]:
        """All current findings, plus the names of sources that could not be read this time."""
        findings: list[I.Finding] = []
        failed: list[str] = []
        self.apps_now = None              # set below only when the App list was read this time
        self.locations_now = None         # 0.8.5: backup locations as read this time (monthly report)
        names: dict[str, str] = {}
        own = self.ha.self_slug()
        try:
            apps = self.ha.installed_apps()
            names = {a.slug: a.name for a in apps}
            self.apps_now = [(a.slug, a.state) for a in apps if a.slug not in (own, self.ha.scout)]
            self.app_names = names
            rows = []
            kept = self.kept_rollbacks()
            for a in apps:
                if a.slug in (own, self.ha.scout) or a.slug in kept or not RE_SLUG.fullmatch(a.slug):
                    continue
                if a.state in ("error", "stopped"):
                    # Always read the real boot setting: a manual-start App (for example a commissioning
                    # bridge kept deliberately non-running) is never restarted or started by a fix.
                    rows.append((a.slug, a.name, a.state, self.ha.app_boot(a.slug)))
            findings += I.from_apps(rows)
        except (HAError, net.NetError, ForbiddenCall):
            failed.append("apps")
        self.disk_now = None

        def backups() -> list[I.Finding]:
            rows = self.ha.backup_list()
            out = (I.from_backups(rows, now) + I.from_backup_size(rows, now)
                   + I.from_backup_protection(getattr(self.ha, "backup_protection", [])))
            inventory = getattr(self.ha, "backup_inventory", None)
            if self.apps_now is not None and inventory is not None:     # 0.8.5: Backups you can trust
                running = sorted({s for s, st in self.apps_now if st == "started"} | {own})
                installed = [s for s, _ in self.apps_now]
                out += B.completeness(inventory, running, names)
                out += B.radio_freshness(inventory, installed, names, now)
            return out

        def disk() -> list[I.Finding]:
            self.disk_now = self.ha.disk()
            return I.from_disk(self.disk_now)
        for name, fn in (("supervisor", lambda: I.from_resolution(self.ha.resolution(), names)),
                         ("disk", disk),
                         ("backups", backups)):
            try:
                findings += fn()
            except (HAError, net.NetError, ForbiddenCall):
                failed.append(name)
        try:
            core = self.ha.core_problems()
            findings += I.from_repairs(core["repairs"])
            heard = None
            if I.needs_network_check(core["entries"]):
                try:
                    heard = self.ha.ssdp_heard()
                except Exception:  # noqa: BLE001 - unreadable: the finding says it was not checked
                    heard = None
            findings += I.from_entries(core["entries"], first_seen, now, self.ha.power_cycle_entity, heard,
                                       frozenset(core.get("reauth") or ()))
            findings += I.from_log(core["log"])
            self.log_now = core["log"]                                     # 0.8.7: the error-rate trend
        except Exception:  # noqa: BLE001 - Core may be restarting; try again next check
            failed.append("core")
        try:
            try:
                drive = self.ha.drive_backup_state()
            except Exception:  # noqa: BLE001 - unreadable: judged on Home Assistant's own locations only
                drive = None
            self.locations_now = self.ha.backup_locations()
            findings += I.from_offsite(self.locations_now, now, drive)
            findings += B.automatic_health(getattr(self.ha, "backup_auto", None), now)   # 0.8.5
        except Exception:  # noqa: BLE001 - older Core without backup/info, or Core restarting
            failed.append("offsite")
        return findings, failed

    def _device_findings(self, st: dict, now: float, failed: list[str]) -> list[I.Finding]:
        """Low batteries / offline devices: a fresh sweep at most every DEVICE_SWEEP_SECONDS, else the last one."""
        sweep = st["device_sweep"]
        if now - float(sweep.get("at") or 0) >= DEVICE_SWEEP_SECONDS:
            try:
                open_keys = frozenset(k for k in st["open"] if k.startswith(("battery:", "offline:")))
                found = I.device_findings(self.ha.device_health(), now, open_keys)
                sweep.update(at=now, rows=[{"key": f.key, "title": f.title, "steps": list(f.steps), "link": f.link,
                                            "facts": f.facts} for f in found])
            except Exception:  # noqa: BLE001 - Core restarting or the read failed: keep open items as they are
                failed.append("devices")
                return []
        return [I.Finding(r["key"], "devices", I.WARNING, r["title"], steps=tuple(r["steps"]), link=r["link"],
                          facts=r["facts"]) for r in sweep.get("rows") or []]

    def _disk_findings(self, st: dict, now: float) -> list[I.Finding]:
        disk = getattr(self, "disk_now", None)
        samples = st["disk_samples"]
        if disk and (not samples or now - samples[-1][0] >= DISK_SAMPLE_SECONDS):
            samples.append([now, round(disk[1], 3)])
            del samples[:-DISK_SAMPLES_KEPT]
        return I.from_disk_trend(samples, now)

    # -- one check ---------------------------------------------------------------------
    def check(self) -> dict:
        now = self.clock()
        st = self._state()
        first_seen = {k: v["first"] for k, v in st["open"].items()}
        findings, failed = self.collect(first_seen, now)
        findings += self._device_findings(st, now, failed)
        findings += I.from_liveness(self.j.load_doc(LIVENESS_DOC, {}), now)
        findings += self._credential_findings(failed)
        if self.os_findings is not None:
            try:
                findings += self.os_findings()
            except Exception:  # noqa: BLE001 - never a false clear: keep what is open
                failed.append("os_pending")
        if self.p.housekeeping:
            try:
                findings += self._hk_findings(now)
            except Exception:  # noqa: BLE001 - never a false clear: keep what is open
                failed.append("housekeeping")
        if self.p.pi_health:
            try:
                findings += self._pi_findings(now)
            except Exception:  # noqa: BLE001 - never a false clear: keep what is open
                failed.append("pihealth")
        if getattr(self, "apps_now", None) is not None:
            stopped = st["stopped_since"]
            stopped_now = {slug for slug, state in self.apps_now if state == "stopped"}
            for slug in list(stopped):
                if slug not in stopped_now:
                    del stopped[slug]
            for slug in stopped_now:
                stopped.setdefault(slug, now)
            findings += I.from_stopped_apps(stopped, getattr(self, "app_names", {}), now)
        if "disk" not in failed:
            findings += self._disk_findings(st, now)
        current = {f.key: f for f in findings}
        skipped_sources = {src for src in failed}
        push_now: list[I.Finding] = []
        for f in findings:
            rec = st["open"].setdefault(f.key, {"first": now, "polls": 0, "alerted": False})
            rec.update(polls=rec["polls"] + 1, title=f.title, severity=f.severity, clear=f.announce_clear,
                       source=f.source)
            diagnosis = f.facts.get("diagnosis")
            if diagnosis and diagnosis not in I.UNSURE_DIAGNOSES and rec.get("diagnosis") != diagnosis \
                    and now - rec.get("diagnosed_at", 0) >= REDIAGNOSE_SECONDS:
                # The cause was just found, or changed: tell the owner again with the new steps
                # (at most once per REDIAGNOSE_SECONDS, so a flapping cause cannot spam the phone).
                rec.update(diagnosis=diagnosis, diagnosed_at=now, alerted=False)
            if rec["alerted"] or rec["polls"] < CONFIRM_POLLS:
                continue
            if f.action and not self.s.dry_run:
                rec["alerted"] = True           # announced through the fix offer below
            elif f.severity == I.CRITICAL or f.source in CREDENTIAL_SOURCES:
                push_now.append(f)
                if f.source in CREDENTIAL_SOURCES:
                    # 0.6.3: each credential ladder step is pushed once (a new step is a new key) and is
                    # also in the next morning summary (owner decision 2026-10-06)
                    st["summary"].append(self._summary_item(f))
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
                st["cleared"].append({"key": key, "title": rec.get("title", key)[:120]})   # 0.8.3: keyed
            del st["open"][key]
            st["declined"].pop(key, None)
            st["asked"].pop(key, None)
        for f in push_now[:MAX_PUSH_PER_CHECK]:
            self._alert(f)
            st["open"][f.key]["alerted"] = True
        st["log_reported"] = {k: t for k, t in st["log_reported"].items() if now - t < LOG_REPORT_SECONDS}
        self.j.save_doc(DOC, st)
        outcome = self._offer_fix(st, current, now)
        self._maybe_escalate(now)                              # 0.9.6: open 7 days -> one push
        self._maybe_summary(now)
        self._maybe_still_open(now)                            # 0.9.6: weekly still-open list
        self._maybe_backup_report(now)
        self._maybe_health_score(now)
        self._maybe_housekeeping_report(now)
        open_count = sum(1 for r in self._state()["open"].values() if r.get("alerted"))
        return {"open_problems": open_count, "unreadable": sorted(failed), "fix": outcome}

    def _credential_findings(self, failed: list[str]) -> list[I.Finding]:
        """0.6.3 Credential Autopilot: this App's own GitHub sign-in, and the Deployer's from its status entity."""
        out: list[I.Finding] = []
        own = self.credentials
        if own is not None:
            try:
                out += I.from_credentials("maintenance", own())
            except Exception:  # noqa: BLE001 - never a false clear: keep what is open
                failed.append("credential")
        try:
            st = self.ha.entity_state(DEPLOYER_STATUS_ENTITY)
        except (HAError, net.NetError, ForbiddenCall):
            st = None
        auth = ((st or {}).get("attrs") or {}).get("github_auth")
        if auth is None:
            failed.append("credential_deployer")   # unreadable or an older Deployer: keep what is open
        else:
            out += I.from_credentials("deployer", auth)
        out += self._relay_findings(failed)
        if self.key_findings is not None:
            try:
                out += self.key_findings()
            except Exception:  # noqa: BLE001 - never a false clear: keep what is open
                failed.append("credential_keys")
        return out

    def _relay_findings(self, failed: list[str]) -> list[I.Finding]:
        if not self.relay_url:
            return []
        now = self.clock()
        doc = self.j.load_doc(RELAY_DOC, {})
        view = I.project_relay_credential(doc.get("view"))
        if now - float(doc.get("at") or 0) >= RELAY_READ_EVERY:
            try:
                fresh = I.project_relay_credential(self.relay_fetch(self.relay_url))
            except (net.NetError, ValueError, TypeError):
                fresh = None
            if fresh is None:
                failed.append("credential_relay")   # unreadable: keep what is open, try again next check
                return I.from_relay_credential(view)
            view = fresh
            self.j.save_doc(RELAY_DOC, {"at": now, "view": view})
        if view is None or view["state"] == "unknown":
            failed.append("credential_relay")        # nothing known yet: never a false clear
        return I.from_relay_credential(view)

    @staticmethod
    def _source_of(rec: dict) -> str:
        src = rec.get("source", "")
        return {"repairs": "core", "integration": "core", "log": "core", "app": "apps",
                "disk": "disk", "backup": "backups", "offsite": "offsite", "devices": "devices",
                "liveness": "liveness"}.get(src, src)

    # -- telling the owner -------------------------------------------------------------
    def _summary_item(self, f: I.Finding) -> dict:
        return {"key": f.key, "title": f.title[:120], "severity": f.severity, "steps": list(f.steps)[:3],
                "link": f.link, "detail": [I.scrub(d, 500) for d in f.detail][:3], "facts": f.facts,
                "fix": f.action.label if f.action else None, "source": f.source}

    def _ai_line(self, key: str) -> str:
        try:
            return self.problem_ai.line(key) if self.problem_ai is not None else ""
        except Exception:  # noqa: BLE001 - a review read problem never hides the problem itself
            return ""

    def _maybe_escalate(self, now: float) -> None:
        """0.9.6: a problem still open after PR.ESCALATE_DAYS gets one push (all of them in one push)."""
        st = self._state()
        due = [(k, r, age) for k, r, age in PR.still_open(st["open"], now, PR.ESCALATE_DAYS) if not r.get("escalated")]
        if not due or self.s.dry_run:
            return
        for k, _, _ in due:
            st["open"][k]["escalated"] = True
        self.j.save_doc(DOC, st)
        names = "; ".join(str(r.get("title", k))[:60] for k, r, _ in due[:4])
        approval.inform(self.ha, self.s.notify_service, "Maintenance: still not fixed after a week",
                        f"{len(due)} open {PR.ESCALATE_DAYS}+ days: {names}. Open the Maintenance page or GitHub "
                        "for the fix.", url=getattr(self.s, "open_url", None))
        self.j.audit("issues", "PROBLEM_ESCALATED", items=len(due))

    def _maybe_still_open(self, now: float) -> None:
        """0.9.6: Sunday after the digest hour: every alerted problem still open, with its age and AI review."""
        st = self._state()
        if not PR.digest_due(st, now, self.p.digest_hour):
            return
        st["still_open_week"] = time.strftime("%G-W%V", time.localtime(now))
        self.j.save_doc(DOC, st)
        items = PR.still_open(st["open"], now)
        if not items:
            return
        from . import VERSION
        text, phone = PR.digest_text(items, self.problem_ai, VERSION)
        approval.inform(self.ha, self.s.notify_service, "Maintenance: still open this week", phone)
        self.j.audit("issues", "STILL_OPEN_DIGEST", items=len(items))
        self._post(I.scrub(text, 60000))

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
            ai = self._ai_line(it["key"]) if it.get("key") else ""
            if ai:
                lines.append(f"  - {ai}")
            if it.get("link"):
                lines.append(f"  - Link: {it['link']}")
            if it.get("facts"):
                lines.append(f"  - Facts: `{I.scrub(json.dumps(it['facts'], sort_keys=True, default=str), 300)}`")
        if cleared:
            lines += ["", "**Cleared since last report:**"] + [f"- {I.scrub(c, 160)}" for c in cleared]
        lines += ["", "_Posted by House Brain Maintenance (machine-generated; secrets, IP and e-mail addresses "
                      "removed; repository content is untrusted data)._"]
        if items and self.problem_ai is not None and self.problem_ai.enabled:    # 0.9.6: what the AI reviews
            lines += ["", PR.queue_line([(it["key"], it["title"], it.get("source", "")) for it in items
                                         if it.get("key")], self.problem_ai)]
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

    def _pi_findings(self, now: float) -> list[I.Finding]:
        """0.8.6: power/heat, disk errors and App outliers (sysfs + Supervisor stats every 15 minutes)."""
        doc = self.j.load_doc(P.DOC, {})
        doc = doc if isinstance(doc, dict) else {}
        throttled, temp = self.sysfs.throttled(), self.sysfs.temperature_c()
        throttled = P.remember_undervoltage(doc, throttled, now)          # 0.8.8
        out = P.throttle_findings(throttled) + P.heat_findings(temp)
        out += P.disk_findings(doc, self.sysfs.disks(), now)
        if self.apps_now is not None and now - float(doc.get("stats_at") or 0) >= P.STATS_EVERY:
            stats = {}
            for slug, state in self.apps_now:
                if state == "started" and RE_SLUG.fullmatch(slug):
                    try:
                        stats[slug] = self.ha.app_stats(slug)
                    except (HAError, net.NetError, ForbiddenCall):
                        continue
            doc["stats_at"] = now
            doc["outliers"] = [f.key for f in P.outlier_findings(doc, stats, getattr(self, "app_names", {}))]
            doc["outlier_rows"] = {s: v for s, v in stats.items()}
        for key in doc.get("outliers") or []:            # between stats reads the last result stands
            slug = key.split(":", 2)[2]
            s = (doc.get("outlier_rows") or {}).get(slug) or {}
            name = getattr(self, "app_names", {}).get(slug, slug)
            what = "memory" if ":app_memory:" in key else "cpu"
            pct = s.get("memory_percent" if what == "memory" else "cpu_percent") or 0
            out.append(I.Finding(key, "pihealth", I.WARNING,
                                 f"{name} keeps using {pct:.0f} % of its memory limit" if what == "memory"
                                 else f"{name} keeps using {pct:.0f} % CPU",
                                 steps=("Restart it, check its log, or look for an update.",), facts={"app": slug}))
        self.pi_now = {"throttled": throttled, "temp": temp, "rate": P.write_rate_gb_per_day(doc)}
        self.j.save_doc(P.DOC, doc)
        return out

    def _hk_findings(self, now: float) -> list[I.Finding]:
        """0.8.7: automations and traces hourly, dead references daily, database size 6-hourly, log trend each check.
        Between reads the last result stands; a read that fails keeps the last result and is retried next time."""
        doc = self.j.load_doc(K.DOC, {})
        doc = doc if isinstance(doc, dict) else {}
        if now - float(doc.get("read_at") or 0) >= K.READ_EVERY:
            reads = self.ha.housekeeping_reads()
            autos = reads["automations"]
            doc.update(read_at=now, automation_count=len(autos),
                       names={a["entity_id"]: a["name"] for a in autos},
                       failing=[{"key": f.key, "title": f.title, "detail": list(f.detail), "facts": f.facts}
                                for f in K.failing_automations(autos, reads["traces"])],
                       unused=K.unused_automations(autos, now)[:50])
            if now - float(doc.get("related_at") or 0) >= K.RELATED_EVERY:
                ids = sorted(a["entity_id"] for a in autos)[:K.RELATED_MAX]
                related = self.ha.related_entities(ids)
                doc["related_at"] = now
                doc["dead"] = [{"key": f.key, "title": f.title, "facts": f.facts}
                               for f in K.dead_references(related, reads["known"], doc["names"])]
        if now - float(doc.get("db_at") or 0) >= K.DB_EVERY:
            doc["db_at"] = now
            try:
                K.db_sample(doc, self.ha.db_size_mib(), now)
            except Exception:  # noqa: BLE001 - size not readable this time: the report says so
                pass
        if K.db_top_due(doc, now):                  # 0.9.2: who fills it, once a day while it is large
            doc["db_top_at"] = now
            try:
                counts = self.ha.history_row_counts(K.TOP_HOURS, now)
                doc["db_top"], doc["db_families"] = K.top_writers(counts), K.top_families(counts)
                del counts
            except Exception:  # noqa: BLE001 - not readable this time: the last list stands, retried tomorrow
                pass
        out = [I.Finding(r["key"], "housekeeping", I.WARNING, r["title"], detail=tuple(r["detail"]),
                         steps=("Open the automation, then Traces, to see the step that failed.",
                                "A renamed or removed device or entity is the usual cause."),
                         link=K.AUTOMATIONS_LINK, facts=r["facts"]) for r in doc.get("failing") or []]
        out += [I.Finding(r["key"], "housekeeping", I.WARNING, r["title"],
                          steps=("The entity was renamed or its device was removed; the automation will fail or do "
                                 "nothing at that step.", "Open the automation and pick the current entity."),
                          link=K.AUTOMATIONS_LINK, facts=r["facts"]) for r in doc.get("dead") or []]
        if self.log_now is not None:
            out += K.log_trend(doc, self.log_now, now)
        out += K.db_findings(doc)
        self.j.save_doc(K.DOC, doc)
        return out

    def _maybe_housekeeping_report(self, now: float) -> None:
        """0.8.7: the monthly housekeeping report (first one right after install), from what the checks stored."""
        if not self.p.housekeeping:
            return
        doc = self.j.load_doc(K.DOC, {})
        doc = doc if isinstance(doc, dict) else {}
        if not doc.get("read_at") or not K.report_due(doc, now, self.p.digest_hour):
            return
        from . import VERSION
        text, phone = K.report_text(doc, VERSION)
        lt = time.localtime(now)
        doc["month"] = f"{lt.tm_year:04d}-{lt.tm_mon:02d}"
        self.j.save_doc(K.DOC, doc)
        self.j.audit("housekeeping", "HOUSEKEEPING_REPORT")
        approval.inform(self.ha, self.s.notify_service, "Maintenance: monthly housekeeping report", phone)
        self._post(text)

    def _maybe_health_score(self, now: float) -> None:
        """0.8.6: every Sunday after the morning summary hour, one number for the week (GitHub + one phone line)."""
        if not self.p.pi_health:
            return
        doc = self.j.load_doc(P.DOC, {})
        doc = doc if isinstance(doc, dict) else {}
        if not P.weekly_due(doc, now, self.p.digest_hour):
            return
        from . import VERSION
        score, parts = P.health_score(self._state()["open"])
        history = doc.get("scores") or []
        text, phone = P.weekly_text(score, parts, history, self.pi_now.get("temp"), self.pi_now.get("rate"),
                                    self.pi_now.get("throttled"), VERSION, dict(self.sysfs.why))
        doc["score_day"] = time.strftime("%Y-%m-%d", time.localtime(now))
        doc["scores"] = (history + [[doc["score_day"], score]])[-12:]
        self.j.save_doc(P.DOC, doc)
        self.j.audit("health", "HEALTH_SCORE", score=score)
        approval.inform(self.ha, self.s.notify_service, "Maintenance: weekly health score", phone)
        self._post(text)

    def _maybe_backup_report(self, now: float) -> None:
        """0.8.5: the monthly backup report (first one right after install), from what this check already read."""
        inventory = getattr(self.ha, "backup_inventory", None)
        if not self.p.backup_report or inventory is None or self.apps_now is None or self.locations_now is None:
            return
        doc = self.j.load_doc(B.REPORT_DOC, {})
        if not B.report_due(doc if isinstance(doc, dict) else {}, now, self.p.digest_hour, time.localtime):
            return
        from . import VERSION
        own = self.ha.self_slug()
        running = sorted({s for s, st in self.apps_now if st == "started"} | {own})
        names = getattr(self, "app_names", {})
        text, phone = B.report_text(inventory, self.locations_now, running, [s for s, _ in self.apps_now], names,
                                    getattr(self.ha, "backup_auto", None), now, VERSION)
        lt = time.localtime(now)
        self.j.save_doc(B.REPORT_DOC, {"month": f"{lt.tm_year:04d}-{lt.tm_mon:02d}", "at": now})
        self.j.audit("backups", "BACKUP_REPORT")
        approval.inform(self.ha, self.s.notify_service, "Maintenance: monthly backup report", phone)
        self._post(text)

    def _maybe_summary(self, now: float) -> None:
        st = self._state()
        local = time.localtime(now)
        day = time.strftime("%Y-%m-%d", local)
        if local.tm_hour < self.p.digest_hour or st["last_summary_day"] == day:
            return
        summary, cleared_all = summary_lists(st["summary"], st["cleared"], set(st["open"]))
        grouped = I.group_network(summary)
        total = len(grouped)
        items, cleared = grouped[:MAX_SUMMARY_ITEMS], cleared_all[:MAX_SUMMARY_ITEMS]
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
            if approval.decided("FIX", f"fix-{f.key}"[:80]):    # 0.9.0: decided on the Maintenance page
                return True
            if f.action.kind == "power_cycle":
                return power_ok and now - st["asked"].get(f.key, 0) >= self.p.power_cycle_gap_seconds
            return now - st["asked"].get(f.key, 0) >= self.p.reask_hours * 3600
        candidates = [f for f in current.values()
                      if f.action and st["open"].get(f.key, {}).get("polls", 0) >= CONFIRM_POLLS
                      and not st["declined"].get(f.key) and due(f) and f.key not in self.claimed()]
        if not candidates:
            return None
        candidates.sort(key=lambda f: (f.severity != I.CRITICAL, f.key))
        f = candidates[0]
        if len(st["asks"]) >= self.p.max_fix_asks_per_day and not approval.decided("FIX", f"fix-{f.key}"[:80]):
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
        ai = self._ai_line(f.key)
        lines = [f"Problem: {f.title}", *[f"- {s}" for s in f.steps[:2]], *([ai] if ai else []),
                 f"Fix: {f.action.label}. Nothing else is changed.",
                 "Approve to run it now; Reject and I won't ask again while this problem stays.",
                 f"Request: {rid}"]
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
        if a.kind == "relabel_stats":
            return self._relabel(f)
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

    def _relabel(self, f: I.Finding) -> tuple[str, str]:
        """0.9.6: re-read the issues now and relabel only those still eligible (label only, never a delete);
        verified by reading the stored units back. Home Assistant clears the repairs at its next 5-minute check."""
        try:
            items = I.relabel_items(self.ha.core_problems()["repairs"])
            if not items:
                return FIXED, "nothing to relabel any more (the issues are gone)"
            with self.ha.fix_target("relabel_stats", "units_changed"):
                for it in items:
                    self.ha.relabel_statistics(it["statistic_id"], it["unit_class"], it["to"])
            units = self.ha.statistics_units([it["statistic_id"] for it in items])
        except (HAError, net.NetError, ForbiddenCall) as err:
            return NOT_FIXED, net.redact(f"the relabel could not run: {err}")[:200]
        wrong = [it["statistic_id"] for it in items if units.get(it["statistic_id"]) != it["to"]]
        if wrong:
            return NOT_FIXED, f"relabelled, but {len(wrong)} still read back wrong: {', '.join(wrong[:4])}"
        return FIXED, (f"relabelled {len(items)} sensors' history to "
                       f"{', '.join(sorted({it['to'] for it in items}))}; numbers unchanged (read back). Home "
                       "Assistant clears the repairs within 5 minutes")

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
