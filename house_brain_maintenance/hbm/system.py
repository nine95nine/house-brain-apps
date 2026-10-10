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
* OS: BACKING_UP -> BACKED_UP -> OS_UPDATING (journalled before the call, because the host reboots and
  this App restarts) -> after the reboot: new version -> DONE; previous version -> ROLLED_BACK ("OS fell
  back to previous version (RAUC A/B)"). Core health is checked in both cases; there is no automatic
  restore for the OS beyond the A/B fallback, so an unhealthy Core after an OS update is FAILED_MANUAL.
"""
from __future__ import annotations

import datetime as dt
import re
import time
from dataclasses import dataclass

from . import approval, net
from .ha import ForbiddenCall, HAError, HomeAssistant, SystemInfo
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


@dataclass
class SystemPolicy:
    lead_seconds: float = 3600.0        # the review is on the tracking issue this long before the phone asks
    reask_hours: float = 6.0            # 0.6.5: no answer asks again after this (owner option update_reask_hours)
    practice_hours: float = 24.0        # dry run: each version is reported once per this period (unchanged)
    boot_seconds: float = 900.0         # Core must be back and healthy within this time
    settle_seconds: float = 60.0        # ... and still healthy this much later
    reboot_seconds: float = 3600.0      # OS: the host must have rebooted within this time
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
        self.offered: dict[str, str] = {}   # 0.7.2: kind -> offered version, as the last check read them
        self.asking = ""                    # 0.7.2: the update whose ask is on the phone right now

    # -- memory ------------------------------------------------------------------------
    def _mem(self) -> dict:
        mem = self.j.load_doc(DOC, {})
        for key in ("reviewed", "asked", "declined", "practiced", "held", "quarantine"):
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
            if key == self.asking or mem["declined"].get(key) or mem["quarantine"].get(key):
                continue
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
        return {"kind": info.kind.upper(), "from": info.version, "to": info.version_latest, "automatic": False,
                "latest_backup_age_days": latest_backup_age_days(backups, time.time()),
                "free_gb": round(disk[1], 1) if disk else None, "needed_free_gb": required_free_gb(backups)}

    def _lines(self, rid: str, info: SystemInfo, facts: dict) -> list[str]:
        age = facts["latest_backup_age_days"]
        free = facts["free_gb"]
        plan = ("Plan: full backup -> update Core only -> health check (Core answers, runs normally, not in safe "
                "mode) -> restore Home Assistant from that backup automatically if Core fails."
                if info.kind == "core" else
                "Plan: full backup -> update the OS only (the Pi reboots) -> health check. If the new OS cannot "
                "boot, the Pi falls back to the previous OS by itself (A/B); nothing else is restored automatically.")
        return [f"Update {LABEL[info.kind]} {info.version} -> {info.version_latest}?",
                f"Kind: {facts['kind']}",
                "Latest backup with Home Assistant: " + (f"{age:g} days old" if age is not None else "none found"),
                "Free disk: " + (f"{free:g} GB" if free is not None else "unknown")
                + f" (the backup needs {facts['needed_free_gb']:g} GB)",
                RELEASE_NOTE_LINE,
                plan,
                "Never automatic. The Supervisor, Apps and this App are not updated."]

    def _review_text(self, rid: str, info: SystemInfo, facts: dict) -> str:
        from . import VERSION
        lines = ["### House Brain Maintenance review: " + f"{LABEL[info.kind]} update", "",
                 f"- **Request:** `{rid}`", f"- **Kind:** `{facts['kind']}`",
                 f"- **From → to:** `{info.version}` → `{info.version_latest}`"]
        lines += [f"- {line}" for line in self._lines(rid, info, facts)[2:]]
        lines += [f"- The phone asks for Approve in about {max(1, int(self.p.lead_seconds // 60))} minutes; "
                  "no answer or Reject changes nothing.", f"- **App:** `{VERSION}`", "",
                  "_Posted by House Brain Maintenance (machine-generated; repository content is untrusted data)._"]
        return "\n".join(lines)

    # -- one cycle -------------------------------------------------------------------------
    def cycle(self) -> Outcome | None:
        """At most one Core/OS item per call; Core before OS. None when there is nothing to do now."""
        mem = self._mem()
        for info in self.pending():
            key = f"{info.kind}@{info.version_latest}"
            if mem["declined"].get(key) or mem["quarantine"].get(key):
                continue
            rid = request_id_for(info.kind, str(info.version_latest))
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
                return None
            asked = mem["asked"].get(key)
            if asked and now - float(asked) < self.p.reask_hours * 3600:
                return None
            return self._ask_then_apply(rid, key, info, facts)
        return None

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
        versions never again) and the same full-backup path follows. None: an OS update started (reboot)."""
        rid = request_id_for(kind, version)
        key = f"{kind}@{version}"
        facts = {"kind": kind.upper(), "to": version, "automatic": False}
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
                                                       or mem["quarantine"].get(f"core@{core.version_latest}")):
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
               "started_at": time.time()}
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
                    txn.update(stage="OS_UPDATING", started_at=time.time())
                    self.j.save_txn(txn)                   # before the call: the host reboots during it
                    self.j.audit(rid, "SYSTEM_UPDATING", kind=kind, backup=bslug)
                    try:
                        self.ha.update_os(to)
                    except net.NetError as err:
                        if 400 <= err.status < 500:    # the Supervisor refused: no reboot follows
                            self.j.clear_txn()
                            return Outcome(rid, FAILED, [f"the Supervisor refused the OS update "
                                                         f"({net.redact(str(err))[:120]}); nothing changed"],
                                           {"backup": bslug})
                        self.j.audit(rid, "UPDATE_CALL_ERROR", error=net.redact(str(err))[:200])
                    except HAError as err:
                        self.j.clear_txn()
                        return Outcome(rid, FAILED, [f"the Supervisor refused the OS update ({err.code}); "
                                                     "nothing changed"], {"backup": bslug})
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
            return Outcome(rid, DONE, [f"updated Home Assistant Core {txn['from']} -> {txn['to']}; healthy "
                                       "after the check"], {"backup": txn.get("backup")})
        return self._restore(txn, why)

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
        facts = {"backup": txn.get("backup"), "failure": txn.get("failure"), "quarantined": txn["to"]}
        self.j.clear_txn()
        if ok:
            return Outcome(rid, ROLLED_BACK, [f"Core update to {txn['to']} failed ({txn.get('failure')}); Home "
                                              f"Assistant was restored from backup {txn.get('backup')} and runs "
                                              f"{txn['from']} again", f"{txn['to']} will not be offered again"], facts)
        return Outcome(rid, FAILED_MANUAL, [f"Core update failed ({txn.get('failure')}) and the automatic restore "
                                            f"did not bring back {txn['from']} ({why})",
                                            f"restore backup {txn.get('backup')} by hand (Settings > System > "
                                            "Backups); automation is paused"], facts)

    # -- OS -------------------------------------------------------------------------------------
    def _os_progress(self, txn: dict) -> Outcome | None:
        """None while the host has not rebooted yet (the job stays journalled); otherwise the result."""
        rid = txn["request_id"]
        try:
            boot, _ = self.ha.host_boot_info()
            version = self.ha.system_info("os").version
        except (HAError, net.NetError):
            boot, version = txn.get("boot"), None
        rebooted = (boot is not None and boot != txn.get("boot")) or version == txn["to"]
        if not rebooted or version is None:
            if time.time() - float(txn.get("started_at") or 0) < self.p.reboot_seconds:
                return None
            self.j.clear_txn()
            return Outcome(rid, FAILED_MANUAL, [f"the OS update to {txn['to']} did not finish with a reboot within "
                                                f"{int(self.p.reboot_seconds // 60)} minutes (OS now {version})",
                                                "check Settings > System > Updates; automation is paused"],
                           {"backup": txn.get("backup")})
        ok, why = self._core_health(str(txn.get("core")), txn.get("baseline_unhealthy") or [])
        facts = {"backup": txn.get("backup"), "os_now": version, "core_health": why}
        self.j.clear_txn()
        if version == txn["to"]:
            if ok:
                self.j.audit(rid, "SYSTEM_HEALTHY", kind="os", to=txn["to"])
                return Outcome(rid, DONE, [f"updated Home Assistant OS {txn['from']} -> {txn['to']}; the Pi "
                                           "rebooted and Core is healthy"], facts)
            return Outcome(rid, FAILED_MANUAL, [f"OS updated to {txn['to']}, but Core is not healthy ({why})",
                                                "an OS update has no automatic restore; automation is paused"], facts)
        if version == txn["from"]:
            mem = self._mem()
            mem["quarantine"][f"os@{txn['to']}"] = True
            self._save(mem)
            if ok:
                return Outcome(rid, ROLLED_BACK, [FELL_BACK, f"the Pi runs OS {txn['from']} again and Core is "
                                                             "healthy", f"{txn['to']} will not be offered again"],
                               facts)
            return Outcome(rid, FAILED_MANUAL, [FELL_BACK, f"Core is not healthy ({why}); automation is paused"],
                           facts)
        return Outcome(rid, FAILED_MANUAL, [f"after the OS update the Pi runs OS {version}, neither {txn['from']} "
                                            f"nor {txn['to']}; automation is paused"], facts)

    # -- recovery after a restart ---------------------------------------------------------------
    def recover(self, txn: dict) -> Outcome | None:
        stage = txn.get("stage")
        rid = str(txn.get("request_id", "unknown"))
        try:
            if stage in ("BACKING_UP", "BACKED_UP"):
                self.j.clear_txn()
                return Outcome(rid, FAILED, ["the App restarted before the update; nothing was updated"])
            if stage == "OS_UPDATING" and txn.get("system") == "os":
                return self._os_progress(txn)
            if stage == "UPDATING" and txn.get("system") == "core":
                return self._verify_core(txn)
            if stage == "RESTORING" and txn.get("system") == "core":
                return self._verify_restore(txn)
        except (HAError, net.NetError) as err:
            return Outcome(rid, FAILED_MANUAL, [f"recovery read-back failed: {net.redact(str(err))[:200]}"])
        self.j.clear_txn()
        return Outcome(rid, FAILED_MANUAL, ["unknown journalled Core/OS stage"])
