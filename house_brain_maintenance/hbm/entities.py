"""HACS and device-firmware updates (0.7.0, owner decisions 2026-10-08: "Yes, design it", "Approve design",
"Yes, but always ask" for firmware; the owner's goal: "have the maintenance app handle any and all applications").

Every Home Assistant ``update.*`` entity that the Supervisor does not own (Apps, Core and OS stay with their own
engines) is found on the update cadence, reviewed and handled one at a time:

* **HACS card or theme** (display only, no restart): with ``update_mode: auto_low_risk`` a low-risk bug-fix
  update installs by itself in the night window after the waiting period, like an App; everything else asks.
* **HACS integration** (and other HACS code): always asks. One Approve covers the Home Assistant restart an
  integration needs and, if the health check fails, the rollback restart.
* **Device firmware** (Z-Wave, Matter, routers ...): always asks, never automatic, and only after a version has
  been offered for ``firmware_wait_days`` (default 30), so other people find a bad firmware first. Firmware
  cannot be rolled back by Home Assistant: a failure is reported, never "restored".

Transaction (journal ``txn.json``, kind ``entity``), one entity at a time:
  HACS:     BACKING_UP -> BACKED_UP -> INSTALLING -> (RESTARTING) -> DONE | ROLLING_BACK -> RESTORED
  firmware: INSTALLING -> DONE | FAILED | FAILED_MANUAL
After a restart of this App ``recover`` reads the entity's real version and continues from the facts.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from . import approval, net
from .ha import ForbiddenCall, HAError, HomeAssistant, UpdateEntity
from .journal import Journal
from .review import BLOCKED, LOW, EntityReview, entity_kind, review_entity
from .updates import (AUTO_LOW_RISK, DONE, DRY_RUN_OK, FAILED, FAILED_MANUAL, REFUSED, REJECTED, RESTORED,
                      TRACK_RECORD_NEEDED, Outcome)

DOC = "entity_updates"
JOBS = {"hacs": "UPDATE_HACS", "hacs_integration": "UPDATE_HACS", "firmware": "UPDATE_FIRMWARE"}


@dataclass
class EntityPolicy:
    mode: str = "ask"                       # update_mode (shared with App updates)
    window: tuple[int, int] = (2, 5)
    wait_days: float = 3.0                  # automatic HACS card/theme installs wait this long
    firmware_wait_days: float = 30.0        # firmware is asked only after it was offered this long
    reask_hours: float = 6.0
    practice_hours: float = 24.0
    health_seconds: float = 180.0
    boot_seconds: float = 900.0             # Core back after a restart within this time
    firmware_seconds: float = 3600.0        # a firmware install may take this long
    poll: float = 5.0
    hold: frozenset[str] = frozenset()      # update entities the owner holds (option update_hold)
    enabled: bool = True                    # option entity_updates


def request_id_for(kind: str, entity_id: str, version: str) -> str:
    prefix = "fw" if kind == "firmware" else "hacs"
    rid = re.sub(r"[^a-z0-9-]+", "-", f"{prefix}-{entity_id[len('update.'):][:40]}-{version[:20]}".lower())
    return rid.strip("-")[:64]


class EntityUpdater:
    def __init__(self, ha: HomeAssistant, journal: Journal, settings, policy: EntityPolicy,
                 app_successes=lambda: 0) -> None:
        self.ha = ha
        self.j = journal
        self.s = settings
        self.p = policy
        self.app_successes = app_successes  # the App-update track record counts for automatic installs too

    # -- memory ------------------------------------------------------------------------
    def _mem(self) -> dict:
        mem = self.j.load_doc(DOC, {})
        for key in ("quarantine", "declined", "asked", "blocked_noted", "practiced", "first_seen"):
            mem.setdefault(key, {})
        mem.setdefault("successes", 0)
        return mem

    def _save(self, mem: dict) -> None:
        self.j.save_doc(DOC, mem)

    # -- discovery and review (read-only) -------------------------------------------------
    def candidates(self) -> list[tuple[UpdateEntity, str, object]]:
        """(entity, kind, HACS repository or None) for each update waiting that this engine handles."""
        if not self.p.enabled:
            return []
        ents = [e for e in self.ha.update_entities()
                if e.state == "on" and e.installed and e.latest and e.installed != e.latest
                and e.skipped != e.latest and not e.auto_update and e.entity_id not in self.p.hold
                and e.platform not in ("", "hassio")]
        repos = self.ha.hacs_repos() if any(e.platform == "hacs" for e in ents) else {}
        out = []
        for ent in ents:
            repo = repos.get(ent.unique_id or "") if ent.platform == "hacs" else None
            kind = entity_kind(ent.platform, getattr(repo, "category", None))
            if kind is not None:
                out.append((ent, kind, repo))
        return out

    def _key(self, ent: UpdateEntity) -> str:
        return f"{ent.entity_id}@{ent.latest}"

    def _due(self, key: str, mem: dict) -> bool:
        if mem["declined"].get(key):
            return False
        if self.s.dry_run:
            practiced = mem["practiced"].get(key)
            if practiced and time.time() - float(practiced) < self.p.practice_hours * 3600:
                return False
        asked = mem["asked"].get(key)
        return not (asked and time.time() - float(asked) < self.p.reask_hours * 3600)

    def review(self, ent: UpdateEntity, kind: str, repo, mem: dict) -> EntityReview:
        unhealthy, _ = self.ha.health_flags()
        return review_entity(entity_id=ent.entity_id, name=ent.title or ent.entity_id, kind=kind,
                             category=getattr(repo, "category", None) or "firmware",
                             from_version=str(ent.installed), to_version=str(ent.latest),
                             notes=self.ha.release_notes(ent.entity_id), ha_min=getattr(repo, "homeassistant", None),
                             core_version=self.ha.core_version(), unhealthy=unhealthy,
                             quarantined=mem["quarantine"].get(ent.entity_id) == ent.latest,
                             in_progress=ent.in_progress)

    def _waited(self, key: str, mem: dict) -> float:
        first = mem["first_seen"].get(key)
        return (time.time() - float(first)) / 86400 if first else 0.0

    def _in_window(self) -> bool:
        hour = time.localtime().tm_hour
        start, end = self.p.window
        return start <= hour < end if start <= end else (hour >= start or hour < end)

    def _track_record(self, mem: dict) -> int:
        return int(mem["successes"]) + int(self.app_successes() or 0)

    def auto_allowed(self, rev: EntityReview, key: str, mem: dict) -> tuple[bool, str]:
        if self.p.mode != AUTO_LOW_RISK:
            return False, "update_mode is ask"
        if not rev.auto_eligible:
            return False, f"not a low-risk bug-fix update of a card or theme ({rev.category}, {rev.verdict}, {rev.bump})"
        if self._track_record(mem) < TRACK_RECORD_NEEDED:
            return False, f"needs {TRACK_RECORD_NEEDED} approved updates first"
        waited = self._waited(key, mem)
        if waited < self.p.wait_days:
            return False, f"waiting {self.p.wait_days:g} days after release ({waited:.1f} so far)"
        if not self._in_window():
            return False, "outside the night window"
        return True, "low-risk bug-fix update of a card or theme in the night window"

    def waits_for_auto(self, rev: EntityReview, mem: dict) -> bool:
        start, end = self.p.window
        return (self.p.mode == AUTO_LOW_RISK and rev.auto_eligible and start != end
                and self._track_record(mem) >= TRACK_RECORD_NEEDED)

    def _note_first_seen(self, found: list, mem: dict) -> None:
        now = time.time()
        offered = {self._key(ent) for ent, _, _ in found}
        before = dict(mem["first_seen"])
        mem["first_seen"] = {k: v for k, v in before.items() if k in offered}
        for key in offered:
            mem["first_seen"].setdefault(key, now)
        if mem["first_seen"] != before:
            self._save(mem)

    @staticmethod
    def _facts(rev: EntityReview) -> dict:
        return {"entity_id": rev.entity_id, "kind": rev.kind, "category": rev.category, "from": rev.from_version,
                "to": rev.to_version, "bump": rev.bump, "verdict": rev.verdict, "reasons": list(rev.reasons)}

    # -- one cycle -----------------------------------------------------------------------
    def cycle(self) -> Outcome | None:
        """At most one HACS or firmware update per call. None when there is nothing to do now."""
        mem = self._mem()
        found = self.candidates()
        self._note_first_seen(found, mem)
        for ent, kind, repo in found:
            key = self._key(ent)
            if not self._due(key, mem):
                continue
            if kind == "firmware" and self._waited(key, mem) < self.p.firmware_wait_days:
                continue                      # firmware is asked only after it has been out for a while
            rev = self.review(ent, kind, repo, mem)
            rid = request_id_for(kind, ent.entity_id, rev.to_version)
            facts = self._facts(rev)
            if rev.verdict == BLOCKED:
                if mem["blocked_noted"].get(key) == "|".join(rev.reasons):
                    continue
                mem["blocked_noted"][key] = "|".join(rev.reasons)
                self._save(mem)
                return Outcome(rid, REFUSED, [f"update held: {r}" for r in rev.reasons], facts)
            auto, why = self.auto_allowed(rev, key, mem)
            facts["automatic"] = auto
            facts["automatic_reason"] = why
            if self.s.dry_run:
                mem["practiced"][key] = time.time()
                self._save(mem)
                return Outcome(rid, DRY_RUN_OK, ["dry run: reviewed, nothing asked or changed"], facts)
            if not auto and self.waits_for_auto(rev, mem):
                continue
            return self._ask_then_apply(ent, rev, rid, key, facts, automatic=auto)
        return None

    def _ask_then_apply(self, ent: UpdateEntity, rev: EntityReview, rid: str, key: str, facts: dict, *,
                        automatic: bool) -> Outcome:
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
        result = self.apply(rid, ent, rev, automatic=automatic)
        result.facts = {**facts, **result.facts}
        return result

    def _lines(self, rev: EntityReview) -> list[str]:
        label = {LOW: "LOW RISK", "risky": "RISKY - read the reasons"}.get(rev.verdict, rev.verdict)
        if rev.kind == "firmware":
            plan = ["Plan: install the firmware on this device -> watch the device until it is back. The device can "
                    "be offline for several minutes; anything it controls may stop or switch off until it is back.",
                    "Firmware cannot be rolled back automatically: a failure is reported to you."]
        elif rev.kind == "hacs_integration":
            plan = ["Plan: back up the Home Assistant configuration (no database) -> install -> restart Home "
                    "Assistant -> check the integration loads and its devices are back -> if not, reinstall "
                    f"{rev.from_version} and restart again.",
                    "This Approve covers the restart (and the rollback restart if one is needed)."]
        else:
            plan = ["Plan: back up the Home Assistant configuration (no database) -> install -> check -> reinstall "
                    f"{rev.from_version} automatically if it fails. No restart. Refresh the app afterwards."]
        what = "firmware" if rev.kind == "firmware" else f"HACS {rev.category}"
        return [f"Update {rev.name} {rev.from_version} -> {rev.to_version} ({what}, {rev.bump})?",
                f"Review: {label}", *[f"- {r}" for r in rev.reasons[:4]], *plan]

    def _ask(self, rid: str, rev: EntityReview) -> str:
        self.j.audit(rid, "APPROVAL_REQUESTED", job=JOBS[rev.kind])
        decision = approval.ask(self.ha, self.s.notify_service, self.s.owner_user_id, stage="UPDATE",
                                title=f"Maintenance: update {rev.name[:40]}?",
                                message="\n".join(self._lines(rev)) + f"\n\nRequest: {rid}",
                                timeout=self.s.approval_timeout, require_auth=self.s.require_auth,
                                board=self.s.board, open_url=self.s.open_url,
                                should_stop=getattr(self.s, "should_stop", None))
        self.j.audit(rid, "APPROVAL_" + decision.outcome, channel=decision.channel, ignored=decision.ignored_events)
        return decision.outcome

    # -- the transaction -----------------------------------------------------------------
    def _baseline(self, kind: str, ent: UpdateEntity, domain: str | None) -> dict:
        if kind == "firmware":
            return self.ha.entity_health(device_id=ent.device_id) if ent.device_id else {}
        if kind == "hacs_integration" and domain:
            return self.ha.entity_health(domain=domain)
        return {}

    def apply(self, rid: str, ent: UpdateEntity, rev: EntityReview, *, automatic: bool) -> Outcome:
        kind = rev.kind
        if not self.ha.core_alive():
            return Outcome(rid, REFUSED, ["Home Assistant Core is not answering; nothing changed"])
        domain = None
        if kind == "hacs_integration":
            repo = self.ha.hacs_repos().get(ent.unique_id or "")
            domain = getattr(repo, "domain", None)
        base_unhealthy, _ = self.ha.health_flags()
        txn = {"kind": "entity", "entity_kind": kind, "request_id": rid, "entity_id": ent.entity_id,
               "platform": ent.platform, "device_id": ent.device_id, "unique_id": ent.unique_id, "domain": domain,
               "from": rev.from_version, "to": rev.to_version, "automatic": automatic, "backup": None,
               "baseline_unhealthy": sorted(base_unhealthy), "baseline": self._baseline(kind, ent, domain),
               "stage": "BACKING_UP" if kind != "firmware" else "INSTALLING", "started_at": time.time()}
        self.j.save_txn(txn)
        try:
            with self.ha.entity_target(ent.entity_id, kind, rev.to_version):
                if kind != "firmware":
                    self.j.audit(rid, "ENTITY_BACKING_UP", entity=ent.entity_id, automatic=automatic)
                    bslug = self.ha.backup_ha_config(ent.entity_id, rev.to_version)
                    if not self.ha.backup_has_ha(bslug):
                        self.j.clear_txn()
                        return Outcome(rid, FAILED, ["the configuration backup could not be verified; nothing was "
                                                     "updated"], {"backup": bslug})
                    txn.update(stage="BACKED_UP", backup=bslug)
                    self.j.save_txn(txn)
                now = self._read(txn)
                if now is None or now.installed != rev.from_version or now.latest != rev.to_version:
                    self.j.clear_txn()
                    return Outcome(rid, REFUSED, ["the installed or offered version changed since the review; "
                                                  "nothing was updated"], {"backup": txn.get("backup")})
                txn.update(stage="INSTALLING", started_at=time.time())
                self.j.save_txn(txn)
                self.j.audit(rid, "ENTITY_INSTALLING", entity=ent.entity_id, to=rev.to_version)
                try:
                    self.ha.install_update(ent.entity_id, None if kind == "firmware" else rev.to_version,
                                           timeout=self.p.firmware_seconds if kind == "firmware" else 600.0)
                except (HAError, net.NetError) as err:
                    self.j.audit(rid, "INSTALL_CALL_ERROR", error=net.redact(str(err))[:200])
                if kind == "hacs_integration":
                    if not self._wait_installed(txn, rev.to_version, self.p.health_seconds):
                        return self._not_installed(txn)
                    txn["stage"] = "RESTARTING"
                    self.j.save_txn(txn)
                    self.j.audit(rid, "ENTITY_RESTARTING", entity=ent.entity_id)
                    try:
                        self.ha.restart_core()
                    except (HAError, net.NetError) as err:
                        self.j.audit(rid, "RESTART_CALL_ERROR", error=net.redact(str(err))[:200])
        except ForbiddenCall as err:
            self.j.clear_txn()
            return Outcome(rid, REFUSED, [f"not allowed: {err}"])
        except (HAError, net.NetError) as err:
            if txn.get("stage") in ("BACKING_UP", "BACKED_UP"):
                self.j.clear_txn()
                return Outcome(rid, FAILED, [f"stopped before the update ({net.redact(str(err))[:200]}); "
                                             "nothing was updated"])
            return Outcome(rid, FAILED_MANUAL, [f"interrupted ({net.redact(str(err))[:200]}); it is re-checked "
                                                "on the next poll"])
        return self._verify(txn)

    def _read(self, txn: dict) -> UpdateEntity | None:
        try:
            return self.ha.update_entity(txn["entity_id"], txn.get("platform") or "", txn.get("device_id"),
                                         txn.get("unique_id"))
        except (HAError, net.NetError):
            return None

    def _wait_installed(self, txn: dict, version: str, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while True:
            now = self._read(txn)
            if now is not None and now.installed == version and not now.in_progress:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.p.poll)

    def _not_installed(self, txn: dict) -> Outcome:
        now = self._read(txn)
        self.j.clear_txn()
        if txn["entity_kind"] == "firmware" and not self._device_ok(txn)[0]:
            return Outcome(txn["request_id"], FAILED_MANUAL, [
                f"the firmware update to {txn['to']} did not finish (the device reports "
                f"{getattr(now, 'installed', None)}) and the device is not back", "check the device; automation is "
                "paused"], {"backup": txn.get("backup")})
        return Outcome(txn["request_id"], FAILED, [f"the update did not happen (still on "
                                                   f"{getattr(now, 'installed', None)}); nothing changed"],
                       {"backup": txn.get("backup")})

    # -- health ---------------------------------------------------------------------------
    def _core_ok(self, baseline_unhealthy: list) -> tuple[bool, str]:
        try:
            if not self.ha.core_alive():
                return False, "Home Assistant Core is not answering"
            state = self.ha.core_state()
            if state["safe_mode"] or state["recovery_mode"]:
                return False, "Core started in safe or recovery mode"
            if state["state"] != "RUNNING":
                return False, f"Core state is {state['state']}"
            unhealthy, _ = self.ha.health_flags()
        except (HAError, net.NetError) as err:
            return False, f"health read failed ({net.redact(str(err))[:80]})"
        new = sorted(set(unhealthy) - set(baseline_unhealthy or []))
        if new:
            return False, "Home Assistant became unhealthy: " + ", ".join(new)[:120]
        return True, "healthy"

    def _integration_ok(self, txn: dict) -> tuple[bool, str]:
        base = txn.get("baseline") or {}
        if not txn.get("domain"):
            return True, "healthy"
        try:
            now = self.ha.entity_health(domain=txn["domain"])
        except (HAError, net.NetError) as err:
            return False, f"health read failed ({net.redact(str(err))[:80]})"
        for entry, state in (base.get("entries") or {}).items():
            if state == "loaded" and now["entries"].get(entry) != "loaded":
                return False, f"the {txn['domain']} integration did not load ({now['entries'].get(entry)})"
        if now["unavailable"] > int(base.get("unavailable") or 0):
            return False, (f"{now['unavailable']} of its {now['total']} entities are unavailable "
                           f"(before: {base.get('unavailable') or 0})")
        return True, "healthy"

    def _device_ok(self, txn: dict) -> tuple[bool, str]:
        base = txn.get("baseline") or {}
        if not txn.get("device_id"):
            return True, "no device to check"
        try:
            now = self.ha.entity_health(device_id=txn["device_id"])
        except (HAError, net.NetError) as err:
            return False, f"health read failed ({net.redact(str(err))[:80]})"
        if now["unavailable"] > int(base.get("unavailable") or 0):
            return False, (f"{now['unavailable']} of the device's {now['total']} entities are unavailable "
                           f"(before: {base.get('unavailable') or 0})")
        return True, "healthy"

    def _watch(self, txn: dict, version: str) -> tuple[bool, str]:
        """After the install (and restart): Core healthy, the entity on ``version``, the integration/device back.
        Waits up to ``boot_seconds`` for Core and the version, then watches ``health_seconds``."""
        kind = txn["entity_kind"]
        deadline = time.monotonic() + self.p.boot_seconds
        why = "not checked"
        while True:
            ok, why = self._core_ok(txn.get("baseline_unhealthy") or [])
            now = self._read(txn) if ok else None
            if ok and now is not None and now.installed == version and not now.in_progress:
                break
            if ok:
                why = f"version is {getattr(now, 'installed', None)}, expected {version}"
            if time.monotonic() >= deadline:
                return False, why
            time.sleep(self.p.poll)
        settle = time.monotonic() + self.p.health_seconds
        while True:
            ok, why = self._core_ok(txn.get("baseline_unhealthy") or [])
            if ok and kind == "hacs_integration":
                ok, why = self._integration_ok(txn)
            if ok and kind == "firmware":
                ok, why = self._device_ok(txn)
            if ok and time.monotonic() >= settle:
                return True, "healthy"
            if not ok and time.monotonic() >= settle:
                return False, why
            time.sleep(self.p.poll)

    def _verify(self, txn: dict) -> Outcome:
        rid, kind = txn["request_id"], txn["entity_kind"]
        if kind == "firmware":
            # a firmware install can take long: wait for it to finish before the health watch
            if not self._wait_installed(txn, txn["to"], self.p.firmware_seconds):
                return self._not_installed(txn)
        elif kind == "hacs":
            if not self._wait_installed(txn, txn["to"], self.p.health_seconds):
                return self._not_installed(txn)
        ok, why = self._watch(txn, txn["to"])
        if ok:
            self.j.clear_txn()
            mem = self._mem()
            mem["successes"] = int(mem["successes"]) + 1
            self._save(mem)
            self.j.audit(rid, "ENTITY_HEALTHY", entity=txn["entity_id"], to=txn["to"])
            return Outcome(rid, DONE, [f"updated {txn['entity_id']} {txn['from']} -> {txn['to']}; healthy after the "
                                       "watch"], {"backup": txn.get("backup"), "automatic": txn.get("automatic")})
        mem = self._mem()
        mem["quarantine"][txn["entity_id"]] = txn["to"]
        self._save(mem)
        if kind == "firmware":
            self.j.clear_txn()
            return Outcome(rid, FAILED_MANUAL, [f"firmware {txn['to']} installed, but the device is not healthy "
                                                f"({why})", "firmware cannot be rolled back automatically; check the "
                                                "device; automation is paused", f"{txn['to']} will not be offered "
                                                "again"], {"failure": why})
        return self._rollback(txn, why)

    def _rollback(self, txn: dict, why: str) -> Outcome:
        rid = txn["request_id"]
        txn.update(stage="ROLLING_BACK", failure=why)
        self.j.save_txn(txn)
        self.j.audit(rid, "ENTITY_ROLLING_BACK", entity=txn["entity_id"], reason=why)
        try:
            with self.ha.entity_target(txn["entity_id"], txn["entity_kind"], txn["to"], txn["from"]):
                try:
                    self.ha.install_update(txn["entity_id"], txn["from"])
                except (HAError, net.NetError) as err:
                    self.j.audit(rid, "ROLLBACK_CALL_ERROR", error=net.redact(str(err))[:200])
                if txn["entity_kind"] == "hacs_integration":
                    self._wait_installed(txn, txn["from"], self.p.health_seconds)
                    try:
                        self.ha.restart_core()
                    except (HAError, net.NetError) as err:
                        self.j.audit(rid, "RESTART_CALL_ERROR", error=net.redact(str(err))[:200])
        except ForbiddenCall as err:
            self.j.audit(rid, "ROLLBACK_REFUSED", error=str(err)[:200])
        return self._verify_rollback(txn)

    def _verify_rollback(self, txn: dict) -> Outcome:
        rid = txn["request_id"]
        ok, why = self._watch(txn, txn["from"])
        facts = {"backup": txn.get("backup"), "failure": txn.get("failure"), "quarantined": txn["to"]}
        self.j.clear_txn()
        if ok:
            return Outcome(rid, RESTORED, [f"update to {txn['to']} failed ({txn.get('failure')}); reinstalled "
                                           f"{txn['from']}", f"{txn['to']} will not be offered again"], facts)
        return Outcome(rid, FAILED_MANUAL, [f"update failed ({txn.get('failure')}) and reinstalling {txn['from']} did "
                                            f"not bring it back ({why})", f"the configuration backup "
                                            f"{txn.get('backup')} holds the files from before; automation is paused"],
                       facts)

    # -- recovery after a restart of this App ---------------------------------------------------
    def recover(self, txn: dict) -> Outcome:
        stage = txn.get("stage")
        rid = str(txn.get("request_id", "unknown"))
        try:
            if stage in ("BACKING_UP", "BACKED_UP"):
                self.j.clear_txn()
                return Outcome(rid, FAILED, ["this App restarted before the update; nothing was updated"])
            if stage == "INSTALLING" and txn.get("entity_kind") == "hacs_integration":
                # Only the version tells whether the install happened. Not installed: nothing changed. Installed:
                # finish what the owner approved (the restart), then the same health check and rollback.
                now = self._read(txn)
                if now is None or now.installed != txn["to"]:
                    self.j.clear_txn()
                    return Outcome(rid, FAILED, ["this App restarted before the install finished; nothing changed"],
                                   {"backup": txn.get("backup")})
                txn["stage"] = "RESTARTING"
                self.j.save_txn(txn)
                try:
                    with self.ha.entity_target(txn["entity_id"], "hacs_integration", txn["to"]):
                        self.ha.restart_core()
                except (HAError, net.NetError, ForbiddenCall) as err:
                    self.j.audit(rid, "RESTART_CALL_ERROR", error=net.redact(str(err))[:200])
                return self._verify(txn)
            if stage in ("INSTALLING", "RESTARTING"):
                return self._verify(txn)
            if stage == "ROLLING_BACK":
                return self._verify_rollback(txn)
        except (HAError, net.NetError) as err:
            return Outcome(rid, FAILED_MANUAL, [f"recovery read-back failed: {net.redact(str(err))[:200]}"])
        self.j.clear_txn()
        return Outcome(rid, FAILED_MANUAL, ["unknown journalled HACS/firmware stage"])
