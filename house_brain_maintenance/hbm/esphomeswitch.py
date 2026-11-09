"""0.9.59 ESPHome stable switch (owner decision 2026-11-08 "Switch to stable"; rules-first updates part 4).

The house runs the **ESPHome Device Builder (beta)** App (``5c53de3b_esphome-beta``). Its beta releases rang the
phone every week and never qualify for an automatic install (tier C: pre-release, radio). The owner chose the
stable App (``5c53de3b_esphome``) instead. Both Apps come from ESPHome's own repository and both map the Home
Assistant configuration folder (``homeassistant_config``), so the device YAML files under ``/config/esphome`` are
shared: nothing is copied, and no new folder access is needed.

Proposed by this App (never by a request) when the beta (or dev) App is installed and the stable one is not.
One owner Approve on the phone, then one transaction (journal ``txn.json``, kind ``esphome``):

  BACKING_UP -> INSTALLING -> COPYING -> STOPPING_BETA -> STARTING_STABLE -> WATCHING -> (DONE | UNDOING -> UNDONE)

* BACKING_UP: a partial backup of the beta App (its version read back from the backup), kept;
* INSTALLING: the stable App is installed from the store (``POST /store/addons/5c53de3b_esphome/install``);
* COPYING: the beta's three known options (``OPTION_NAMES``) are set on the stable App;
* STOPPING_BETA: the beta is set to start manually and stopped (never uninstalled: a later, separate decision);
  both Apps use the host network and port 6052, so they never run at once;
* STARTING_STABLE / WATCHING: the stable App is started and must be ``started`` within ``watch_seconds``;
* any failure after the install: the stable App is stopped, the beta set to start at boot and started again
  (UNDONE). The stable App stays installed, stopped.

**No device is flashed, no ESPHome API is called**: the devices keep running their firmware; the dashboard shows
them again from the shared folder. Reject asks again after ``REASK_DAYS_AFTER_REJECT`` days (ESPHome stable keeps
improving); no answer asks again after ``reask_hours``. Dry run reviews only.
"""
from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass

from . import approval, net
from .ha import ForbiddenCall, HAError
from .journal import Journal

JOB = "ESPHOME_STABLE_SWITCH"
TXN = "esphome"
STAGE = "ESPHOME"
DOC = "esphome_switch"
STABLE = "5c53de3b_esphome"
BETAS = ("5c53de3b_esphome-beta", "5c53de3b_esphome-dev")
OPTION_NAMES = ("home_assistant_dashboard_integration", "default_compile_process_limit", "leave_front_door_open")
DONE, DRY_RUN_OK, REJECTED, REFUSED, UNDONE, FAILED_MANUAL = "DONE", "DRY_RUN_OK", "REJECTED", "REFUSED", "UNDONE", "FAILED_MANUAL"
REASK_DAYS_AFTER_REJECT = 30.0


@dataclass
class EsphomePolicy:
    watch_seconds: float = 300.0
    poll: float = 5.0
    reask_hours: float = 24.0
    practice_hours: float = 24.0


@dataclass
class Outcome:
    request_id: str
    result_outcome: str
    reasons: list[str]
    facts: dict


class EsphomeSwitch:
    def __init__(self, ha, journal: Journal, settings, policy: EsphomePolicy, *, busy=lambda: False) -> None:
        self.ha, self.j, self.s, self.p, self.busy = ha, journal, settings, policy, busy
        self.on_done = None                     # 0.9.58 soak: (slug, version) after a healthy switch

    def _mem(self) -> dict:
        mem = self.j.load_doc(DOC, {})
        mem = mem if isinstance(mem, dict) else {}
        for key in ("asked", "declined", "practiced", "done_at"):
            mem.setdefault(key, 0.0)
        return mem

    def _save(self, mem: dict) -> None:
        self.j.save_doc(DOC, mem)

    def pair(self) -> tuple[str, str] | None:
        """(beta slug, stable slug) while a beta/dev App is installed and the stable one is not."""
        try:
            installed = {a.slug for a in self.ha.installed_apps()}
        except (HAError, net.NetError):
            return None
        if STABLE in installed:
            return None
        for beta in BETAS:
            if beta in installed:
                return beta, STABLE
        return None

    def status(self) -> dict:
        mem = self._mem()
        return {"done_at": mem["done_at"], "declined_at": mem["declined"], "asked_at": mem["asked"]}

    @staticmethod
    def _recent(stamp, hours: float) -> bool:
        return bool(stamp) and time.time() - float(stamp) < hours * 3600

    def cycle(self) -> Outcome | None:
        pair = self.pair()
        if pair is None or self.busy():
            return None
        beta, stable = pair
        mem = self._mem()
        rid = f"esphome-stable-{time.strftime('%Y%m%d')}"
        facts = {"beta": beta, "stable": stable}
        if self._recent(mem["declined"], REASK_DAYS_AFTER_REJECT * 24) or \
                (self._recent(mem["asked"], self.p.reask_hours) and not approval.decided(STAGE, rid)):
            return None
        try:
            detail = self.ha.app_detail(beta)
            store = self.ha.store_detail(stable)
        except (HAError, net.NetError, ForbiddenCall):
            return None
        facts.update(beta_version=detail.version, stable_version=store.version_latest or store.version)
        if self.s.dry_run:
            if self._recent(mem["practiced"], self.p.practice_hours):
                return None
            mem["practiced"] = time.time()
            self._save(mem)
            return Outcome(rid, DRY_RUN_OK, ["dry run: the ESPHome stable switch would be asked; nothing changed"], facts)
        mem["asked"] = time.time()
        self._save(mem)
        lines = (f"Switch ESPHome from the beta App ({detail.version}) to the stable App ({facts['stable_version']})?",
                 ("Plan: back up the beta -> install the stable App from the store -> copy its three settings -> stop "
                  "the beta (kept, not removed) -> start the stable App -> check it runs. Any problem: the beta is started "
                  "again automatically."),
                 ("Your device files stay where they are (both Apps use the same folder). No device is flashed; nothing "
                  "changes on your ESP boards."),
                 "Approve = switch now. Reject = ask again in 30 days.")
        self.j.audit(rid, "APPROVAL_REQUESTED", job=JOB)
        try:
            decision = approval.ask(self.ha, self.s.notify_service, self.s.owner_user_id, stage=STAGE,
                                    title="Maintenance: switch ESPHome to the stable App?",
                                    message="\n".join(lines) + f"\n\nRequest: {rid}", timeout=self.s.approval_timeout,
                                    require_auth=self.s.require_auth, board=self.s.board, open_url=self.s.open_url,
                                    should_stop=getattr(self.s, "should_stop", None))
        except Exception as err:  # noqa: BLE001 - no phone: asked again later
            return Outcome(rid, REFUSED, [f"the phone could not be asked ({net.redact(str(err))[:120]})"], facts)
        self.j.audit(rid, "APPROVAL_" + decision.outcome, channel=decision.channel, ignored=decision.ignored_events)
        if decision.outcome != approval.APPROVE:
            if decision.outcome == approval.REJECT:
                mem = self._mem()
                mem["declined"] = time.time()
                self._save(mem)
            return Outcome(rid, REJECTED, [f"owner decision: {decision.outcome}"], facts)
        out = self.apply(rid, beta, stable, str(detail.version), detail.state)
        out.facts = {**facts, **out.facts}
        return out

    # -- the switch ------------------------------------------------------------------------------
    def apply(self, rid: str, beta: str, stable: str, beta_version: str, beta_state: str) -> Outcome:
        txn = {"kind": TXN, "request_id": rid, "old": beta, "new": stable, "from": beta_version,
               "prior_state": beta_state, "stage": "BACKING_UP", "started": time.time(), "backup": None}
        self.j.save_txn(txn)
        self.j.audit(rid, "ESPHOME_BACKING_UP", old=beta)
        try:
            with self.ha.esphome_target(beta, stable):
                bslug = self.ha.backup_app(beta, f"hbm esphome beta {beta_version} before stable"[:100])
                if self.ha.backup_app_version(bslug, beta) != beta_version:
                    self.j.clear_txn()
                    return Outcome(rid, REFUSED, ["the backup does not hold the beta App at its current version; nothing changed"],
                                   {"backup": bslug})
                txn.update(stage="INSTALLING", backup=bslug)
                self.j.save_txn(txn)
                self.j.audit(rid, "ESPHOME_INSTALLING", new=stable, backup=bslug)
                self.ha.install_store_app(stable)
                installed = self._wait_installed(stable)
                if installed is None:
                    self.j.clear_txn()
                    return Outcome(rid, REFUSED, ["the stable App did not appear as installed; the beta keeps running"], {"backup": bslug})
                txn.update(stage="COPYING", to=installed)
                self.j.save_txn(txn)
                options = self.ha.esphome_options(beta)
                try:
                    if options:
                        self.ha.set_esphome_options(stable, options)
                    txn["options_copied"] = sorted(options)
                except (HAError, net.NetError) as err:           # cosmetic settings: the switch goes on, the result says so
                    txn["options_copied"] = None
                    self.j.audit(rid, "ESPHOME_OPTIONS_NOT_COPIED", error=net.redact(str(err))[:200])
                txn["stage"] = "STOPPING_BETA"
                self.j.save_txn(txn)
                self.j.audit(rid, "ESPHOME_STOPPING_BETA", options=txn["options_copied"])
                self.ha.set_boot(beta, "manual")
                self.ha.stop_app(beta)
                txn["stage"] = "STARTING_STABLE"
                self.j.save_txn(txn)
                self.j.audit(rid, "ESPHOME_STARTING_STABLE")
                self.ha.start_app(stable)
        except ForbiddenCall as err:
            self.j.clear_txn()
            return Outcome(rid, REFUSED, [f"not allowed: {err}"], {})
        except (HAError, net.NetError) as err:
            return self._failed(txn, net.redact(str(err))[:200])
        txn["stage"] = "WATCHING"
        self.j.save_txn(txn)
        return self._watch(txn)

    def _wait_installed(self, stable: str) -> str | None:
        deadline = time.monotonic() + self.p.watch_seconds
        while True:
            try:
                d = self.ha.app_detail(stable)
                if d.installed and d.version:
                    return str(d.version)
            except (HAError, net.NetError):
                pass
            if time.monotonic() >= deadline:
                return None
            time.sleep(self.p.poll)

    def _watch(self, txn: dict) -> Outcome:
        rid, stable = txn["request_id"], txn["new"]
        deadline = time.monotonic() + self.p.watch_seconds
        last = "unknown"
        while True:
            try:
                d = self.ha.app_detail(stable)
                last = d.state
                if d.state == "started":
                    self.j.clear_txn()
                    mem = self._mem()
                    mem["done_at"] = time.time()
                    self._save(mem)
                    self.j.audit(rid, "ESPHOME_DONE", new=stable, version=d.version)
                    if self.on_done is not None:
                        with contextlib.suppress(Exception):            # the soak watch is extra
                            self.on_done(stable, str(d.version), rid)
                    reasons = [(f"ESPHome runs on the stable App ({stable} {d.version}); the beta "
                                f"({txn['old']} {txn['from']}) is stopped and kept, its backup is {txn.get('backup')}"),
                               "no device was flashed; your device files are shared by both Apps"]
                    if txn.get("options_copied") is None:
                        reasons.append("the beta's settings could not be copied: check the stable App's Configuration tab")
                    return Outcome(rid, DONE, reasons, {"backup": txn.get("backup"), "from": txn["from"], "to": d.version,
                                                         "options_copied": txn.get("options_copied")})
                if d.state == "error":
                    break
            except (HAError, net.NetError, ForbiddenCall):
                pass
            if time.monotonic() >= deadline:
                break
            time.sleep(self.p.poll)
        return self._undo(txn, f"the stable App did not start (state {last})")

    def _failed(self, txn: dict, reason: str) -> Outcome:
        if txn.get("stage") in ("BACKING_UP", "INSTALLING"):
            self.j.clear_txn()
            return Outcome(txn["request_id"], REFUSED, [f"stopped before anything was switched ({reason}); the beta keeps running"],
                           {"backup": txn.get("backup")})
        return self._undo(txn, reason)

    def _undo(self, txn: dict, why: str) -> Outcome:
        rid, beta, stable = txn["request_id"], txn["old"], txn["new"]
        txn.update(stage="UNDOING", failure=why)
        self.j.save_txn(txn)
        self.j.audit(rid, "ESPHOME_UNDOING", reason=why)
        errors = []
        try:
            with self.ha.esphome_target(beta, stable):
                for step in (lambda: self.ha.stop_app(stable), lambda: self.ha.set_boot(beta, "auto"),
                             lambda: self.ha.start_app(beta)):
                    try:
                        step()
                    except (HAError, net.NetError) as err:
                        errors.append(net.redact(str(err))[:120])
        except ForbiddenCall as err:
            errors.append(str(err)[:120])
        self.j.clear_txn()
        facts = {"failure": why, "backup": txn.get("backup")}
        if errors:
            return Outcome(rid, FAILED_MANUAL, [f"the switch failed ({why}) and the undo did not finish: " + "; ".join(errors),
                                                "Settings -> Apps -> ESPHome Device Builder (beta) -> Start"], facts)
        return Outcome(rid, UNDONE, [(f"the switch failed ({why}); the beta App runs again, nothing lost; the stable App "
                                      "stays installed, stopped")], facts)

    def recover(self, txn: dict) -> Outcome:
        rid = str(txn.get("request_id", "unknown"))
        stage = txn.get("stage")
        if stage in ("BACKING_UP", "INSTALLING", "COPYING"):
            self.j.clear_txn()
            return Outcome(rid, REFUSED, ["this App restarted before the switch; the beta keeps running"], {"backup": txn.get("backup")})
        if stage in ("STOPPING_BETA", "STARTING_STABLE", "WATCHING"):
            return self._watch(txn)
        return self._undo(txn, "this App restarted during the undo")
