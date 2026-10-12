"""Core and OS updates (0.6.0, owner decision 2026-10-06, "Core/OS update gate").

Owner-approved design, verbatim: "The Maintenance App would post a review whenever Core or OS has an
update; I add the release-note check. You tap Approve on your phone. The App then takes a full backup,
updates (Core and OS separately, never automatically), checks health, and restores the backup on its own
if Core fails. OS falls back to its previous version if it can't boot."

* Detection is read-only (``/core/info``, ``/os/info``), on the App-update cadence. One review per offered
  version is posted to the tracking issue; the phone asks no earlier than ``lead_seconds`` later, so the
  AI's release-note check can land first. Never automatic: ``update_mode`` and ``auto_low_risk`` do not
  apply here, every Core/OS update needs the owner's Approve. One item per cycle (Core first), so there
  is one pending Core/OS approval at a time and Core and OS never run together (nor with an App update).
* Core (journal ``txn.json``, kind ``system``):
  BACKING_UP -> BACKED_UP -> UPDATING -> (DONE | RESTORING -> ROLLED_BACK | FAILED_MANUAL).
  A full backup is made and verified first (no backup, no update). After the update Core must answer,
  report the new version, run normally (not safe/recovery mode) and add no Supervisor unhealthy reason;
  otherwise Home Assistant only is restored from that backup and the old version is verified. A restore
  that does not bring the old version back is FAILED_MANUAL, which pauses all work (as for App updates).
* OS (0.8.3; Supervisor >= 2026.07.0 installs without rebooting, verified in supervisor/os/manager.py):
  BACKING_UP -> BACKED_UP -> OS_INSTALLING (journalled before the call) -> OS_STAGED (``version_pending`` is the
  target: written to the spare slot, the old OS still runs) -> the phone asks "restart now?" (owner pop-up
  2026-10-11 "Ask me, then reboot") -> config check -> OS_REBOOTING (``/host/reboot``) -> after the boot: new
  version + Core healthy -> DONE; old version -> ROLLED_BACK ("OS fell back ... (RAUC A/B)"); new version but
  Core unhealthy -> OS_ROLLBACK_OFFER: the phone offers the previous OS (``/os/boot-slot``) -> OS_ROLLING_BACK.
  A restart nobody asked for (power cut; on a Pi 4 any restart activates the staged slot) is verified the same
  way. Waiting for the owner never pauses the App; a Reject keeps the OS installed until the next restart.
  The App reboots the host only to finish an OS install, or to roll one back, each after its own Approve.
  Older Supervisors reboot during the call: a changed boot after OS_INSTALLING is verified as above.
"""
from __future__ import annotations

import datetime as dt
import re
import time
from dataclasses import dataclass

from . import approval, issues as I, net, upgradeinfo
from .ha import ForbiddenCall, HAError, HomeAssistant, OsState, SystemInfo
from .journal import Journal
from .manifest import RE_UPDATE_VERSION
from .updates import DONE, DRY_RUN_OK, FAILED, FAILED_MANUAL, REFUSED, REJECTED, Outcome, missed_note, utc_iso

ROLLED_BACK = "ROLLED_BACK"
KINDS = ("core", "os")                  # Core first: one item per cycle
JOBS = {"core": "UPDATE_CORE", "os": "UPDATE_OS"}
LABEL = {"core": "Home Assistant Core", "os": "Home Assistant OS"}
RELEASE_NOTE_LINE = "Release-note check: by the AI in chat; approve only after it"
FELL_BACK = "OS fell back to previous version (RAUC A/B)"
MIN_FREE_GB = 2.0
DOC = "system_updates"
OS_DOC = "os_activation"            # 0.8.3: a staged OS update between install and restart (not a transaction)
OS_STAGES = ("OS_STAGED", "OS_REBOOTING", "OS_ROLLBACK_OFFER", "OS_ROLLING_BACK")
OS_BLOCKING = ("OS_REBOOTING", "OS_ROLLING_BACK")   # a restart this App started: nothing else starts meanwhile


@dataclass
class SystemPolicy:
    lead_seconds: float = 3600.0        # the review is on the tracking issue this long before the phone asks
    reask_hours: float = 6.0            # 0.6.5: no answer asks again after this (owner option update_reask_hours)
    practice_hours: float = 24.0        # dry run: each version is reported once per this period (unchanged)
    boot_seconds: float = 900.0         # Core must be back and healthy within this time
    settle_seconds: float = 60.0        # ... and still healthy this much later
    reboot_seconds: float = 3600.0      # OS: an old Supervisor's own reboot, or ours, is seen within this time
    poll: float = 5.0


def request_id_for(kind: str, version: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", f"sys-{kind}-{version[:40]}".lower()).strip("-")[:64]


def required_free_gb(backups: list[tuple]) -> float:
    """Free space needed for the full backup: twice the newest full backup, at least ``MIN_FREE_GB``."""
    sizes = [row[2] for row in sorted(backups, key=lambda r: r[1]) if row[3] == "full" and row[2]]
    return round(max(MIN_FREE_GB, 2 * sizes[-1]) if sizes else MIN_FREE_GB, 1)


def latest_backup_age_days(backups: list[tuple], now: float) -> float | None:
    """Age of the newest backup that includes Home Assistant (days), or None when there is none."""
    stamps = []
    for kind, date, _gb, _content in backups:
        if kind != "full":
            continue
        try:
            stamps.append(dt.datetime.fromisoformat(date.replace("Z", "+00:00")).timestamp())
        except ValueError:
            continue
    return round((now - max(stamps)) / 86400, 1) if stamps else None


class SystemUpdater:
    def __init__(self, ha: HomeAssistant, journal: Journal, settings, policy: SystemPolicy, report) -> None:
        self.ha = ha
        self.j = journal
        self.s = settings              # jobs.Settings (notify service, owner, dry_run, approval board ...)
        self.p = policy
        self.report = report           # posts one markdown text to the tracking issue (or nowhere)
        self.holds = None              # 0.7.3: the hold list (holds.Holds); a held update is never asked
        self.ai = None                 # 0.8.9: aireview.AiReviews (no ask before the AI review, or 48 h)
        self.queue: dict[str, dict] = {}   # 0.8.9: Core/OS updates offered and why they are (not) asked yet
        self.on_done = None            # 0.7.3: after-update check hook (kind, txn) on a DONE result
        self.snapshot = None           # 0.7.3: () -> integrations running now (afterupdate.loaded_snapshot)
        self.offered: dict[str, str] = {}   # 0.7.2: kind -> offered version, as the last check read them
        self.asking = ""                    # 0.7.2: the update whose ask is on the phone right now
        self.upgrade_info = None            # 0.8.4: upgradeinfo.UpgradeInfo (known issues, release notes); None = off
        self._intel: dict[str, dict] = {}   # 0.8.4: target -> facts of the last read (review and ask share it)

    # -- memory ------------------------------------------------------------------------
    def _mem(self) -> dict:
        mem = self.j.load_doc(DOC, {})
        for key in ("reviewed", "asked", "declined", "practiced", "held", "quarantine", "hold_noted"):
            mem.setdefault(key, {})
        return mem

    def _save(self, mem: dict) -> None:
        self.j.save_doc(DOC, mem)

    # -- detection (read-only) -------------------------------------------------------------
    def pending(self) -> list[SystemInfo]:
        out = []
        for kind in KINDS:
            try:
                info = self.ha.system_info(kind)
            except (HAError, net.NetError):
                continue                        # e.g. no /os/info on a non-OS install: nothing to offer
            if info.update_available and info.version and info.version_latest and info.version != info.version_latest:
                out.append(info)
        self.offered = {i.kind: str(i.version_latest) for i in out}
        return out

    def waiting(self) -> list[dict]:
        """0.7.2: Core/OS updates still offered whose ask timed out (``waiting_reask``) or whose review is posted
        and the phone has not asked yet (``waiting_review_lead``). Not declined, not quarantined, not on the phone
        right now. Pure memory read; no Supervisor call. Empty in dry run."""
        if self.s.dry_run:
            return []
        mem = self._mem()
        out = []
        for kind in KINDS:
            version = self.offered.get(kind)
            if version is None or not RE_UPDATE_VERSION.fullmatch(version):
                continue
            key = f"{kind}@{version}"
            if key == self.asking or mem["declined"].get(key) or mem["quarantine"].get(key) or self.held(kind, version):
                continue                                  # 0.7.3: a held update is not "waiting" either
            asked, reviewed = mem["asked"].get(key), mem["reviewed"].get(key)
            if asked:
                out.append({"kind": kind, "version": version, "asked_at": utc_iso(float(asked)),
                            "next_reask_at": utc_iso(float(asked) + self.p.reask_hours * 3600),
                            "state": "waiting_reask"})
            elif reviewed:
                out.append({"kind": kind, "version": version, "asked_at": None,
                            "next_reask_at": utc_iso(float(reviewed) + self.p.lead_seconds),
                            "state": "waiting_review_lead"})
        return out

    def _facts(self, info: SystemInfo) -> dict:
        backups = self.ha.backup_list()
        disk = self.ha.disk()
        facts = {"kind": info.kind.upper(), "from": info.version, "to": info.version_latest, "automatic": False,
                 "latest_backup_age_days": latest_backup_age_days(backups, time.time()),
                 "free_gb": round(disk[1], 1) if disk else None, "needed_free_gb": required_free_gb(backups)}
        if info.kind == "core" and self.upgrade_info is not None and info.version_latest:
            intel = self.upgrade_info.for_core(str(info.version_latest), self.ha.loaded_components())
            self._intel[str(info.version_latest)] = intel
            issues = intel.get("known_issues")
            notes = intel.get("notes") or {}
            facts.update(known_issues=None if issues is None else [i["integration"] for i in issues],
                         breaking_changes=notes.get("breaking_total") if notes.get("read") else None)
        return facts

    def _lines(self, rid: str, info: SystemInfo, facts: dict) -> list[str]:
        age = facts["latest_backup_age_days"]
        free = facts["free_gb"]
        plan = ("Plan: full backup -> update Core only -> health check (Core answers, runs normally, not in safe "
                "mode) -> restore Home Assistant from that backup automatically if Core fails."
                if info.kind == "core" else
                "Plan: full backup -> install the new OS into the spare slot (the Pi keeps running) -> your phone "
                "asks before the Pi restarts -> health check. If the new OS cannot boot, the Pi falls back by itself "
                "(A/B); if Home Assistant is not healthy on it, your phone offers a one-tap rollback to this OS.")
        intel = self._intel.get(str(info.version_latest)) if info.kind == "core" else None
        return [f"Update {LABEL[info.kind]} {info.version} -> {info.version_latest}?",
                f"Kind: {facts['kind']}",
                "Latest backup with Home Assistant: " + (f"{age:g} days old" if age is not None else "none found"),
                "Free disk: " + (f"{free:g} GB" if free is not None else "unknown")
                + f" (the backup needs {facts['needed_free_gb']:g} GB)",
                *([upgradeinfo.phone_line(intel)] if intel else []),
                self._ai_text(info),
                plan,
                "Never automatic. The Supervisor, Apps and this App are not updated."]

    def _ai_text(self, info: SystemInfo) -> str:
        """0.8.9: the AI-review line (before 0.8.9 a request to the AI in chat that nothing ever sent)."""
        if self.ai is None:
            return RELEASE_NOTE_LINE
        key = f"{info.kind}@{info.version_latest}"
        reviewed = self._mem()["reviewed"].get(key)
        gate = self.ai.gate(key, float(reviewed) if reviewed else None)
        if gate.state == "wait":
            return (f"AI review: requested on the tracking issue as `{key}`; the phone asks after it (or from "
                    f"{self.ai.ask_anyway_at(float(reviewed) if reviewed else None)} at the latest)")
        return gate.line

    def _review_text(self, rid: str, info: SystemInfo, facts: dict) -> str:
        from . import VERSION
        lines = ["### House Brain Maintenance review: " + f"{LABEL[info.kind]} update", "",
                 f"- **Request:** `{rid}`", f"- **Kind:** `{facts['kind']}`",
                 f"- **From → to:** `{info.version}` → `{info.version_latest}`"]
        lines += [f"- {line}" for line in self._lines(rid, info, facts)[2:]]
        intel = self._intel.get(str(info.version_latest)) if info.kind == "core" else None
        if intel:                                       # 0.8.4: details on GitHub, the phone gets one line
            lines += [f"- {line}" if not line.startswith("  ") else line for line in upgradeinfo.review_lines(intel)]
        lines += [f"- The phone asks for Approve in about {max(1, int(self.p.lead_seconds // 60))} minutes; "
                  "no answer or Reject changes nothing.", f"- **App:** `{VERSION}`", "",
                  "_Posted by House Brain Maintenance (machine-generated; repository content is untrusted data)._"]
        return "\n".join(lines)

    # -- one cycle -------------------------------------------------------------------------
    def cycle(self) -> Outcome | None:
        """At most one Core/OS item per call; Core before OS. None when there is nothing to do now."""
        mem = self._mem()
        pending = self.pending()
        old = self.queue
        self.queue = {f"{i.kind}@{i.version_latest}": {
            "kind": i.kind, "item": f"{i.kind}@{i.version_latest}", "name": LABEL[i.kind], "from": i.version,
            "to": i.version_latest, "state": (old.get(f"{i.kind}@{i.version_latest}") or {}).get("state", "")}
            for i in pending}

        def note(key: str, state: str) -> None:
            if key in self.queue:
                self.queue[key]["state"] = state
        for info in pending:
            key = f"{info.kind}@{info.version_latest}"
            if mem["declined"].get(key) or mem["quarantine"].get(key):
                note(key, "you rejected this version (final)" if mem["declined"].get(key)
                     else "failed here before; not offered again")
                continue
            rid = request_id_for(info.kind, str(info.version_latest))
            why = self.held(info.kind, str(info.version_latest))
            if why:
                note(key, f"held: {why}")
                if mem["hold_noted"].get(key) != why:     # said once on the tracking issue, never on the phone
                    try:
                        self.report(self._held_text(rid, info, why))
                    except net.NetError as err:
                        self.j.audit(rid, "SYSTEM_HOLD_POST_FAILED", error=net.redact(str(err))[:200])
                        continue
                    mem["hold_noted"][key] = why
                    self._save(mem)
                    self.j.audit(rid, "SYSTEM_HELD", kind=info.kind, to=info.version_latest, why=why)
                continue
            facts = self._facts(info)
            now = time.time()
            if self.s.dry_run:
                practiced = mem["practiced"].get(key)
                if practiced and now - float(practiced) < self.p.practice_hours * 3600:
                    return None
                mem["practiced"][key] = now
                self._save(mem)
                return Outcome(rid, DRY_RUN_OK, ["dry run: reviewed, nothing asked or changed"], facts)
            unhealthy, _ = self.ha.health_flags()
            if unhealthy:
                why = "Home Assistant reports it is unhealthy: " + ", ".join(sorted(unhealthy))[:120]
                if mem["held"].get(key) == why:
                    return None
                mem["held"][key] = why
                self._save(mem)
                return Outcome(rid, REFUSED, [f"update held: {why}"], facts)
            reviewed = mem["reviewed"].get(key)
            if not reviewed:
                try:
                    self.report(self._review_text(rid, info, facts))
                except net.NetError as err:
                    self.j.audit(rid, "SYSTEM_REVIEW_POST_FAILED", error=net.redact(str(err))[:200])
                    return None
                mem["reviewed"][key] = now
                self._save(mem)
                self.j.audit(rid, "SYSTEM_REVIEW_POSTED", kind=info.kind, to=info.version_latest)
                return None
            if now - float(reviewed) < self.p.lead_seconds:
                note(key, "review posted; the phone asks after the lead time")
                return None
            if self.ai is not None and not self.ai.gate(key, float(reviewed)).proceed:
                note(key, f"waiting for the AI review (asked after it, or from {self.ai.ask_anyway_at(float(reviewed))})")
                continue
            asked = mem["asked"].get(key)
            if asked and now - float(asked) < self.p.reask_hours * 3600:
                note(key, "asked on your phone; asks again at "
                     + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(float(asked) + self.p.reask_hours * 3600)))
                return None
            note(key, "asking on your phone now")
            return self._ask_then_apply(rid, key, info, facts)
        return None

    def held(self, kind: str, version: str) -> str | None:
        return self.holds.why_held(kind, version) if self.holds is not None else None

    def _held_text(self, rid: str, info: SystemInfo, why: str) -> str:
        from . import VERSION
        item = f"{info.kind}@{info.version_latest}"
        return "\n".join([f"### House Brain Maintenance: {LABEL[info.kind]} update held", "",
                          f"- **Request:** `{rid}`", f"- **From → to:** `{info.version}` → `{info.version_latest}`",
                          f"- Held: {why}. Your phone is **not** asked.",
                          f"- To let it through, tell the AI; it files `RELEASE_UPDATE` `{item}`.",
                          f"- **App:** `{VERSION}`", "",
                          "_Posted by House Brain Maintenance (machine-generated; repository content is untrusted "
                          "data)._"])

    def _ask_then_apply(self, rid: str, key: str, info: SystemInfo, facts: dict) -> Outcome | None:
        """The one path to a Core/OS update: ask -> full backup -> update -> health check -> restore/fallback."""
        mem = self._mem()
        asked = mem["asked"][key] = time.time()
        self._save(mem)
        self.asking = key
        try:
            decision = self._ask(rid, info, facts)
        finally:
            self.asking = ""
        if decision != approval.APPROVE:
            if decision == approval.TIMEOUT:
                missed_note(self.ha, self.s.notify_service, f"{LABEL[info.kind]} {info.version_latest}",
                            asked + self.p.reask_hours * 3600)
            if decision == approval.REJECT:
                mem = self._mem()
                mem["declined"][key] = True
                self._save(mem)
            return Outcome(rid, REJECTED, [f"owner decision: {decision}"], facts)
        result = self.apply(rid, info, facts)
        if result is not None:
            result.facts = {**facts, **result.facts}
        return result

    def ask_now(self, kind: str, version: str) -> Outcome | None:
        """0.6.5 ``ASK_UPDATE_NOW``: ask again right away for a Core/OS update whose review is already on the
        tracking issue and that is still waiting, instead of after the re-ask wait. Everything ``cycle`` checks
        still holds (review posted and its lead time passed, Core before OS, health, Reject final, failed
        versions never again) and the same full-backup path follows. None: an OS install waits (restart ask)."""
        rid = request_id_for(kind, version)
        key = f"{kind}@{version}"
        facts = {"kind": kind.upper(), "to": version, "automatic": False}
        why = self.held(kind, version)
        if why:
            return Outcome(rid, REFUSED, [f"update held: {why}; nothing asked (release it first)"], facts)
        pending = {i.kind: i for i in self.pending()}
        info = pending.get(kind)
        if info is None or str(info.version_latest) != version:
            return Outcome(rid, REFUSED, [f"{LABEL.get(kind, kind)} {version} is not an update waiting right now; "
                                          "nothing asked"], facts)
        mem = self._mem()
        if mem["declined"].get(key):
            return Outcome(rid, REFUSED, ["you rejected this update; a Reject is final and is never asked again"],
                           facts)
        if mem["quarantine"].get(key):
            return Outcome(rid, REFUSED, ["this version failed before and is not offered again"], facts)
        core = pending.get("core")
        if kind == "os" and core is not None and not (mem["declined"].get(f"core@{core.version_latest}")
                                                       or mem["quarantine"].get(f"core@{core.version_latest}")
                                                       or self.held("core", str(core.version_latest))):
            return Outcome(rid, REFUSED, ["a Core update is waiting; Core is handled before the OS"], facts)
        reviewed = mem["reviewed"].get(key)
        if not reviewed:
            return Outcome(rid, REFUSED, ["the review of this update is not on the tracking issue yet; the App "
                                          "posts it first"], facts)
        if time.time() - float(reviewed) < self.p.lead_seconds:
            return Outcome(rid, REFUSED, [f"the review was posted less than {max(1, int(self.p.lead_seconds // 60))} "
                                          "minutes ago; the phone asks after the release-note check time"], facts)
        facts = self._facts(info)
        if self.s.dry_run:
            return Outcome(rid, DRY_RUN_OK, ["dry run: reviewed, nothing asked or changed"], facts)
        unhealthy, _ = self.ha.health_flags()
        if unhealthy:
            return Outcome(rid, REFUSED, ["update held: Home Assistant reports it is unhealthy: "
                                          + ", ".join(sorted(unhealthy))[:120]], facts)
        return self._ask_then_apply(rid, key, info, facts)

    def _ask(self, rid: str, info: SystemInfo, facts: dict) -> str:
        self.j.audit(rid, "APPROVAL_REQUESTED", job=JOBS[info.kind])
        decision = approval.ask(self.ha, self.s.notify_service, self.s.owner_user_id, stage="SYSTEM",
                                title=f"Maintenance: update {LABEL[info.kind]}?",
                                message="\n".join(self._lines(rid, info, facts)) + f"\n\nRequest: {rid}",
                                timeout=self.s.approval_timeout, require_auth=self.s.require_auth,
                                board=self.s.board, open_url=self.s.open_url,
                                should_stop=getattr(self.s, "should_stop", None))
        self.j.audit(rid, "APPROVAL_" + decision.outcome, channel=decision.channel, ignored=decision.ignored_events)
        return decision.outcome

    # -- the transaction (after Approve) -------------------------------------------------------
    def apply(self, rid: str, info: SystemInfo, facts: dict) -> Outcome | None:
        kind, frm, to = info.kind, str(info.version), str(info.version_latest)
        if not self.ha.core_alive():
            return Outcome(rid, REFUSED, ["Home Assistant Core is not answering; nothing changed"])
        disk = self.ha.disk()
        need = float(facts.get("needed_free_gb") or MIN_FREE_GB)
        if disk is None or disk[1] < need:
            free = "unknown" if disk is None else f"{disk[1]:g} GB"
            return Outcome(rid, FAILED, [f"not enough free disk for the full backup ({free} free, {need:g} GB "
                                         "needed); nothing was backed up or updated"])
        core_before = self.ha.system_info("core").version
        boot, _ = self.ha.host_boot_info()
        base_unhealthy, _ = self.ha.health_flags()
        txn = {"kind": "system", "system": kind, "request_id": rid, "from": frm, "to": to, "stage": "BACKING_UP",
               "core": core_before, "boot": boot, "baseline_unhealthy": sorted(base_unhealthy), "backup": None,
               "started_at": time.time(), "loaded_before": self.snapshot() if self.snapshot else None}
        self.j.save_txn(txn)
        self.j.audit(rid, "SYSTEM_BACKING_UP", kind=kind, to=to)
        try:
            with self.ha.system_target(kind, to):
                bslug = self.ha.backup_full(f"hbm-pre-{kind}-{to}")
                found = self.ha.backup_facts(bslug)
                if found["type"] != "full" or found["homeassistant"] != core_before:
                    self.j.clear_txn()
                    return Outcome(rid, FAILED, ["the full backup could not be verified (type "
                                                 f"{found['type']}, Core {found['homeassistant']}); nothing was "
                                                 "updated"], {"backup": bslug})
                txn.update(stage="BACKED_UP", backup=bslug)
                self.j.save_txn(txn)
                now_info = self.ha.system_info(kind)
                if now_info.version != frm or now_info.version_latest != to:
                    self.j.clear_txn()
                    return Outcome(rid, REFUSED, ["the installed or offered version changed since the review; "
                                                  "nothing was updated"], {"backup": bslug})
                if kind == "core":
                    txn["stage"] = "UPDATING"
                    self.j.save_txn(txn)
                    self.j.audit(rid, "SYSTEM_UPDATING", kind=kind, backup=bslug)
                    try:
                        self.ha.update_core(to)
                    except (HAError, net.NetError) as err:
                        self.j.audit(rid, "UPDATE_CALL_ERROR", error=net.redact(str(err))[:200])
                else:
                    txn.update(stage="OS_INSTALLING", started_at=time.time())
                    self.j.save_txn(txn)                   # before the call: an old Supervisor reboots during it
                    self.j.audit(rid, "SYSTEM_UPDATING", kind=kind, backup=bslug)
                    try:
                        self.ha.update_os(to)
                    except (HAError, net.NetError) as err:
                        refused = isinstance(err, HAError) or 400 <= err.status < 500
                        self.j.audit(rid, "UPDATE_CALL_ERROR", error=net.redact(str(err))[:200])
                        if refused and self._os_nothing_staged(txn):
                            self.j.clear_txn()
                            return Outcome(rid, FAILED, ["the Supervisor refused the OS update "
                                                         f"({net.redact(str(err))[:120]}); nothing changed"],
                                           {"backup": bslug})
        except ForbiddenCall as err:
            self.j.clear_txn()
            return Outcome(rid, REFUSED, [f"not allowed: {err}"])
        except (HAError, net.NetError) as err:
            self.j.clear_txn()
            return Outcome(rid, FAILED, [f"stopped before the update ({net.redact(str(err))[:200]}); "
                                         "nothing was updated"])
        if kind == "core":
            return self._verify_core(txn)
        return self._os_progress(txn)

    # -- health -------------------------------------------------------------------------------
    def _core_check(self, expect: str, baseline: list) -> tuple[bool, str]:
        try:
            if not self.ha.core_alive():
                return False, "Home Assistant Core is not answering"
            version = self.ha.system_info("core").version
            if version != expect:
                return False, f"Core version is {version}, expected {expect}"
            state = self.ha.core_state()
            if state["safe_mode"] or state["recovery_mode"]:
                return False, "Core started in safe or recovery mode"
            if state["state"] != "RUNNING":
                return False, f"Core state is {state['state']}"
            unhealthy, _ = self.ha.health_flags()
        except (HAError, net.NetError) as err:
            return False, f"health read failed ({net.redact(str(err))[:80]})"
        new = sorted(set(unhealthy) - set(baseline or []))
        if new:
            return False, "Home Assistant became unhealthy: " + ", ".join(new)[:120]
        return True, "healthy"

    def _core_health(self, expect: str, baseline: list) -> tuple[bool, str]:
        """Core must reach ``expect`` and be healthy within ``boot_seconds``, and still be after ``settle``."""
        deadline = time.monotonic() + self.p.boot_seconds
        while True:
            ok, why = self._core_check(expect, baseline)
            if ok:
                time.sleep(self.p.settle_seconds)
                ok, why = self._core_check(expect, baseline)
                if ok:
                    return True, why
            if time.monotonic() >= deadline:
                return False, why
            time.sleep(self.p.poll)

    # -- Core ---------------------------------------------------------------------------------
    def _verify_core(self, txn: dict) -> Outcome:
        rid = txn["request_id"]
        ok, why = self._core_health(txn["to"], txn.get("baseline_unhealthy") or [])
        if ok:
            self.j.clear_txn()
            self.j.audit(rid, "SYSTEM_HEALTHY", kind="core", to=txn["to"])
            self._done(txn)
            return Outcome(rid, DONE, [f"updated Home Assistant Core {txn['from']} -> {txn['to']}; healthy "
                                       "after the check"], {"backup": txn.get("backup")})
        return self._restore(txn, why)

    def _done(self, txn: dict) -> None:
        if self.on_done is not None:
            try:
                self.on_done(str(txn.get("system")), txn)
            except Exception as err:  # noqa: BLE001 - the check is extra; it never turns a DONE into a failure
                self.j.audit(str(txn.get("request_id")), "AFTER_UPDATE_START_FAILED", error=str(err)[:200])

    def undo(self, rid: str, frm: str, to: str, backup: str, why: str) -> Outcome:
        """0.7.3 owner-approved Undo (after-update check): Home-Assistant-only restore of the full backup the
        update to ``to`` took, back to ``frm``. Same calls and the same verification as the automatic restore."""
        try:
            now = self.ha.system_info("core").version
        except (HAError, net.NetError) as err:
            return Outcome(rid, FAILED, [f"undo: Core version could not be read ({net.redact(str(err))[:120]}); "
                                         "nothing changed"])
        if now != to:
            return Outcome(rid, FAILED, [f"undo: Core runs {now}, not {to}; nothing changed"], {"backup": backup})
        base_unhealthy, _ = self.ha.health_flags()
        txn = {"kind": "system", "system": "core", "request_id": rid, "from": frm, "to": to, "stage": "RESTORING",
               "backup": backup, "undo": True, "failure": why, "baseline_unhealthy": sorted(base_unhealthy),
               "started_at": time.time()}
        self.j.save_txn(txn)
        self.j.audit(rid, "SYSTEM_UNDO", kind="core", backup=backup, reason=why)
        try:
            with self.ha.system_target("core", to, backup=backup):
                self.ha.restore_core(backup)
        except (HAError, net.NetError, ForbiddenCall) as err:
            self.j.audit(rid, "RESTORE_CALL_ERROR", error=net.redact(str(err))[:200])
        return self._verify_restore(txn)

    def _restore(self, txn: dict, why: str) -> Outcome:
        rid = txn["request_id"]
        mem = self._mem()
        mem["quarantine"][f"core@{txn['to']}"] = True
        self._save(mem)
        txn.update(stage="RESTORING", failure=why)
        self.j.save_txn(txn)
        self.j.audit(rid, "SYSTEM_RESTORING", kind="core", reason=why, backup=txn.get("backup"))
        try:
            with self.ha.system_target("core", txn["to"], backup=txn["backup"]):
                self.ha.restore_core(txn["backup"])
        except (HAError, net.NetError, ForbiddenCall) as err:
            self.j.audit(rid, "RESTORE_CALL_ERROR", error=net.redact(str(err))[:200])
        return self._verify_restore(txn)

    def _verify_restore(self, txn: dict) -> Outcome:
        rid = txn["request_id"]
        ok, why = self._core_health(txn["from"], txn.get("baseline_unhealthy") or [])
        self.j.clear_txn()
        if txn.get("undo"):
            facts = {"backup": txn.get("backup"), "reason": txn.get("failure"), "undone": txn["to"]}
            if ok:
                return Outcome(rid, ROLLED_BACK, [f"undo approved ({txn.get('failure')}): Home Assistant was "
                                                  f"restored from backup {txn.get('backup')} and runs {txn['from']} "
                                                  f"again", f"{txn['to']} is on your hold list"], facts)
            return Outcome(rid, FAILED_MANUAL, [f"undo: the restore did not bring back {txn['from']} ({why})",
                                                f"restore backup {txn.get('backup')} by hand (Settings > System > "
                                                "Backups); automation is paused"], facts)
        facts = {"backup": txn.get("backup"), "failure": txn.get("failure"), "quarantined": txn["to"]}
        if ok:
            return Outcome(rid, ROLLED_BACK, [f"Core update to {txn['to']} failed ({txn.get('failure')}); Home "
                                              f"Assistant was restored from backup {txn.get('backup')} and runs "
                                              f"{txn['from']} again", f"{txn['to']} will not be offered again"], facts)
        return Outcome(rid, FAILED_MANUAL, [f"Core update failed ({txn.get('failure')}) and the automatic restore "
                                            f"did not bring back {txn['from']} ({why})",
                                            f"restore backup {txn.get('backup')} by hand (Settings > System > "
                                            "Backups); automation is paused"], facts)

    # -- OS (0.8.3) -------------------------------------------------------------------------------
    def _os_read(self) -> tuple[str | None, OsState]:
        boot, _ = self.ha.host_boot_info()
        return boot, self.ha.os_state()

    def _os_nothing_staged(self, txn: dict) -> bool:
        """After a refused install call: fresh evidence that nothing was written (the original OS runs, no
        pending version, same boot). Anything else keeps the transaction (a concurrent install may be staged)."""
        try:
            boot, st = self._os_read()
        except (HAError, net.NetError):
            return False
        return st.running == txn["from"] and st.pending is None and (boot is None or boot == txn.get("boot"))

    def _os_progress(self, txn: dict) -> Outcome | None:
        """The install step (journalled transaction). Once the image is staged, the wait for the restart moves
        to its own record (``os_activation``), so waiting for the owner never blocks other work."""
        rid, now = txn["request_id"], time.time()
        try:
            boot, st = self._os_read()
        except (HAError, net.NetError):
            return None                                  # read again next poll
        if boot is not None and txn.get("boot") is not None and boot != txn.get("boot"):
            self.j.clear_txn()                           # an old Supervisor rebooted during the call
            return self._os_after_boot({**txn, "stage": "OS_STAGED"}, boot, st, planned=False)
        if st.pending == txn["to"] and st.running == txn["from"]:
            self._os_save({**txn, "stage": "OS_STAGED", "staged_at": now})
            self.j.clear_txn()
            self.j.audit(rid, "OS_STAGED", to=txn["to"])
            return self._os_activate_ask(self._os_load())
        if now - float(txn.get("started_at") or 0) < self.p.reboot_seconds:
            return None                                  # an old Supervisor may still reboot by itself
        self.j.clear_txn()
        return Outcome(rid, FAILED, [f"the OS install of {txn['to']} could not be confirmed: nothing waits for a "
                                     f"restart and the Pi runs OS {st.running}; nothing switched"],
                       {"backup": txn.get("backup"), "os_now": st.running})

    def _os_load(self) -> dict:
        doc = self.j.load_doc(OS_DOC, {})
        return doc if isinstance(doc, dict) and doc.get("stage") in OS_STAGES else {}

    def _os_save(self, doc: dict) -> None:
        self.j.save_doc(OS_DOC, doc)

    def _os_clear(self) -> None:
        self.j.save_doc(OS_DOC, {})

    def os_step(self) -> tuple[Outcome | None, bool]:
        """0.8.3, every poll: one step of a staged OS update. (outcome, blocking): blocking is True only while
        a restart this App started is under way (nothing else may start then)."""
        doc = self._os_load()
        out, blocking = self._os_step(doc)
        if out is not None:
            out.facts = {"kind": "OS", "from": doc.get("from"), "to": doc.get("to"), **out.facts}
        return out, blocking

    def _os_step(self, doc: dict) -> tuple[Outcome | None, bool]:
        if not doc or self.s.dry_run:
            return None, False
        rid, stage, now = doc["request_id"], doc["stage"], time.time()
        try:
            boot, st = self._os_read()
        except (HAError, net.NetError):
            return None, stage in OS_BLOCKING
        rebooted = boot is not None and doc.get("boot") is not None and boot != doc.get("boot")
        if stage == "OS_STAGED":
            if rebooted:
                return self._os_after_boot(doc, boot, st, planned=False), False
            if st.pending != doc["to"]:
                self._os_clear()
                return Outcome(rid, FAILED, [f"OS {doc['to']} no longer waits for a restart (the Pi runs OS "
                                             f"{st.running}); nothing switched"],
                               {"backup": doc.get("backup"), "os_now": st.running}), False
            return self._os_activate_ask(doc), False
        if stage == "OS_REBOOTING":
            if rebooted:
                return self._os_after_boot(doc, boot, st, planned=True), False
            if now - float(doc.get("rebooting_at") or 0) < self.p.reboot_seconds:
                return None, True
            self._os_save({**doc, "stage": "OS_STAGED", "activate_asked_at": now})
            return Outcome(rid, FAILED, [f"the Pi did not restart within {int(self.p.reboot_seconds // 60)} minutes "
                                         f"after your Approve; OS {doc['to']} still waits and your phone asks again "
                                         "later"], {"backup": doc.get("backup")}), False
        if stage == "OS_ROLLBACK_OFFER":
            return self._os_rollback_ask(doc, st), False
        # OS_ROLLING_BACK
        if rebooted:
            ok, why = self._core_health(str(doc.get("core")), doc.get("baseline_unhealthy") or [])
            facts = {"backup": doc.get("backup"), "os_now": st.running, "core_health": why}
            self._os_clear()
            self._quarantine(doc)
            if st.running == doc["from"] and ok:
                return Outcome(rid, ROLLED_BACK, [f"rolled back to OS {doc['from']} as you approved; Core is healthy",
                                                  f"{doc['to']} will not be offered again"], facts), False
            return Outcome(rid, FAILED_MANUAL, [f"after the rollback the Pi runs OS {st.running} and Core is "
                                                f"{'healthy' if ok else 'not healthy (' + why + ')'}",
                                                "check Settings > System; automation is paused"], facts), False
        if now - float(doc.get("rollback_at") or 0) < self.p.reboot_seconds:
            return None, True
        self._os_clear()
        return Outcome(rid, FAILED_MANUAL, [f"the rollback to OS {doc['from']} did not restart the Pi within "
                                            f"{int(self.p.reboot_seconds // 60)} minutes",
                                            "check Settings > System; automation is paused"],
                       {"backup": doc.get("backup")}), False

    def _quarantine(self, doc: dict) -> None:
        mem = self._mem()
        mem["quarantine"][f"os@{doc['to']}"] = True
        self._save(mem)

    def _os_after_boot(self, doc: dict, boot: str, st: OsState, planned: bool) -> Outcome | None:
        rid = doc["request_id"]
        how = "the Pi restarted as you approved" if planned else \
            "the Pi restarted without this App asking (a power cut or a manual restart)"
        if st.pending == doc["to"] and st.running == doc["from"]:
            self._os_save({**doc, "stage": "OS_STAGED", "boot": boot})   # restarted, the update still waits
            return None
        ok, why = self._core_health(str(doc.get("core")), doc.get("baseline_unhealthy") or [])
        facts = {"backup": doc.get("backup"), "os_now": st.running, "core_health": why, "planned": planned}
        if st.running == doc["to"]:
            if ok:
                self._os_clear()
                self.j.audit(rid, "SYSTEM_HEALTHY", kind="os", to=doc["to"])
                self._done(doc)
                return Outcome(rid, DONE, [f"updated Home Assistant OS {doc['from']} -> {doc['to']}; {how} and "
                                           "Core is healthy"], facts)
            doc = {**doc, "stage": "OS_ROLLBACK_OFFER", "boot": boot, "failure": why}
            self._os_save(doc)
            self.j.audit(rid, "OS_UNHEALTHY", reason=why)
            return self._os_rollback_ask(doc, st)
        self._os_clear()
        if st.running == doc["from"]:
            self._quarantine(doc)
            if ok:
                return Outcome(rid, ROLLED_BACK, [FELL_BACK, f"the Pi runs OS {doc['from']} again and Core is "
                                                             "healthy", f"{doc['to']} will not be offered again"],
                               facts)
            return Outcome(rid, FAILED_MANUAL, [FELL_BACK, f"Core is not healthy ({why}); automation is paused"],
                           facts)
        return Outcome(rid, FAILED_MANUAL, [f"after the OS update the Pi runs OS {st.running}, neither "
                                            f"{doc['from']} nor {doc['to']}; automation is paused"], facts)

    def _due(self, doc: dict, field_name: str) -> bool:
        asked = doc.get(field_name)
        return not asked or time.time() - float(asked) >= self.p.reask_hours * 3600

    def _os_activate_ask(self, doc: dict) -> Outcome | None:
        """The one question before this App restarts the Pi (owner pop-up 2026-10-11 "Ask me, then reboot")."""
        rid, to, frm = doc["request_id"], doc["to"], doc["from"]
        if doc.get("activation_declined") or not self._due(doc, "activate_asked_at"):
            return None
        doc["activate_asked_at"] = time.time()
        self._os_save(doc)
        lines = [f"Home Assistant OS {to} is installed and waits for a restart.",
                 "Restart the Pi now to finish it? Home Assistant is down for about 2 to 5 minutes.",
                 "Until then, any restart or power cut switches to the new OS.",
                 f"After the restart: health check; if Home Assistant is not healthy, your phone offers a one-tap "
                 f"rollback to OS {frm}.", f"Request: {rid}"]
        self.j.audit(rid, "APPROVAL_REQUESTED", job="OS_ACTIVATE")
        decision = approval.ask(self.ha, self.s.notify_service, self.s.owner_user_id, stage="OSBOOT",
                                title="Maintenance: restart to finish the OS update?", message="\n".join(lines),
                                timeout=self.s.approval_timeout, require_auth=self.s.require_auth,
                                board=self.s.board, open_url=self.s.open_url,
                                should_stop=getattr(self.s, "should_stop", None))
        self.j.audit(rid, "APPROVAL_" + decision.outcome, channel=decision.channel, job="OS_ACTIVATE")
        if decision.outcome == approval.TIMEOUT:
            missed_note(self.ha, self.s.notify_service, f"OS {to} (the restart that finishes it)",
                        float(doc["activate_asked_at"]) + self.p.reask_hours * 3600)
            return None
        if decision.outcome != approval.APPROVE:
            self._os_save({**doc, "activation_declined": True})
            return Outcome(rid, REJECTED, [f"you chose not to restart now; OS {to} stays installed and starts at the "
                                           "next restart (a power cut too); this App does not ask again"],
                           {"backup": doc.get("backup")})
        try:
            with self.ha.os_activation_target("reboot", to):
                ok, why = self.ha.core_config_check()
                if not ok:
                    return Outcome(rid, FAILED, [f"not restarted: {why}; fix the configuration first (Settings > "
                                                 "System > Logs); your phone asks again later"],
                                   {"backup": doc.get("backup")})
                self._os_save({**doc, "stage": "OS_REBOOTING", "rebooting_at": time.time()})
                self.j.audit(rid, "OS_REBOOTING", to=to)    # recorded before the call: the host goes down in it
                try:
                    self.ha.reboot_host()
                except HAError as err:
                    self._os_save({**doc, "stage": "OS_STAGED"})
                    return Outcome(rid, FAILED, [f"the Supervisor refused the restart ({err.code}); OS {to} still "
                                                 "waits; your phone asks again later"], {"backup": doc.get("backup")})
                except net.NetError as err:              # the connection may drop as the host goes down
                    self.j.audit(rid, "REBOOT_CALL_ERROR", error=net.redact(str(err))[:200])
        except ForbiddenCall as err:
            return Outcome(rid, REFUSED, [f"not allowed: {err}"])
        return None

    def _os_rollback_ask(self, doc: dict, st: OsState) -> Outcome | None:
        rid, to, frm = doc["request_id"], doc["to"], doc["from"]
        slot = next((name for name, ver in sorted(st.slots.items()) if ver == frm and name != st.boot), None)
        if slot is None:
            self._os_clear()
            self._quarantine(doc)
            return Outcome(rid, FAILED_MANUAL, [f"Home Assistant is not healthy on OS {to} ({doc.get('failure')}) and "
                                                f"OS {frm} is not in the other slot, so there is no rollback",
                                                "automation is paused"], {"backup": doc.get("backup")})
        if not self._due(doc, "rollback_asked_at"):
            return None
        doc["rollback_asked_at"] = time.time()
        self._os_save(doc)
        lines = [f"After the update to OS {to}, Home Assistant is not healthy: {doc.get('failure')}.",
                 f"Roll back to OS {frm}? The Pi restarts into the previous OS (about 2 to 5 minutes).",
                 f"Reject keeps OS {to}.", f"Request: {rid}"]
        self.j.audit(rid, "APPROVAL_REQUESTED", job="OS_ROLLBACK")
        decision = approval.ask(self.ha, self.s.notify_service, self.s.owner_user_id, stage="OSBACK",
                                title="Maintenance: roll back the OS?", message="\n".join(lines),
                                timeout=self.s.approval_timeout, require_auth=self.s.require_auth,
                                board=self.s.board, open_url=self.s.open_url,
                                should_stop=getattr(self.s, "should_stop", None))
        self.j.audit(rid, "APPROVAL_" + decision.outcome, channel=decision.channel, job="OS_ROLLBACK")
        if decision.outcome == approval.TIMEOUT:
            return None
        if decision.outcome != approval.APPROVE:
            self._os_clear()
            self._quarantine(doc)
            return Outcome(rid, FAILED, [f"you chose to keep OS {to} although Home Assistant is not healthy "
                                         f"({doc.get('failure')})"], {"backup": doc.get("backup")})
        try:
            with self.ha.os_activation_target("boot_slot", frm, slot):
                self._os_save({**doc, "stage": "OS_ROLLING_BACK", "rollback_at": time.time(), "rollback_slot": slot})
                self.j.audit(rid, "OS_ROLLING_BACK", slot=slot, to=frm)
                try:
                    self.ha.set_os_boot_slot(slot)
                except HAError as err:
                    self._os_save({**doc, "stage": "OS_ROLLBACK_OFFER"})
                    return Outcome(rid, FAILED, [f"the Supervisor refused the rollback ({err.code}); your phone "
                                                 "asks again later"], {"backup": doc.get("backup")})
                except net.NetError as err:
                    self.j.audit(rid, "REBOOT_CALL_ERROR", error=net.redact(str(err))[:200])
        except ForbiddenCall as err:
            return Outcome(rid, REFUSED, [f"not allowed: {err}"])
        return None

    def findings(self) -> list[I.Finding]:
        """0.8.3: an OS update that is installed but not running yet (any origin). On a Pi 4 any restart or power
        cut switches to it, so the owner should know it is there."""
        st = self.ha.os_state()
        if not st.pending:
            return []
        doc = self._os_load()
        mine = doc.get("to") == st.pending and doc.get("stage") == "OS_STAGED" and not doc.get("activation_declined")
        return [I.Finding(f"os:pending:{st.pending}", "os_pending", I.WARNING,
                          f"Home Assistant OS {st.pending} is installed and waits for a restart",
                          steps=("Until then, any restart or power cut switches to it.",
                                 "Your phone asks before this App restarts the Pi." if mine else
                                 "Restart when it suits you: Settings > System > Repairs (Reboot required)."),
                          facts={"running": st.running, "pending": st.pending})]

    # -- recovery after a restart ---------------------------------------------------------------
    def recover(self, txn: dict) -> Outcome | None:
        stage = txn.get("stage")
        rid = str(txn.get("request_id", "unknown"))
        try:
            if stage in ("BACKING_UP", "BACKED_UP"):
                self.j.clear_txn()
                return Outcome(rid, FAILED, ["the App restarted before the update; nothing was updated"])
            if txn.get("system") == "os" and stage in ("OS_INSTALLING", "OS_UPDATING"):   # OS_UPDATING: 0.6.0-0.8.2
                return self._os_progress(txn)
            if stage == "UPDATING" and txn.get("system") == "core":
                return self._verify_core(txn)
            if stage == "RESTORING" and txn.get("system") == "core":
                return self._verify_restore(txn)
        except (HAError, net.NetError) as err:
            return Outcome(rid, FAILED_MANUAL, [f"recovery read-back failed: {net.redact(str(err))[:200]}"])
        self.j.clear_txn()
        return Outcome(rid, FAILED_MANUAL, ["unknown journalled Core/OS stage"])
