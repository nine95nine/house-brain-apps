"""0.8.6 Raspberry Pi firmware (bootloader EEPROM / VL805) updates in the approval flow (owner pop-up 2026-10-11,
"Safer upgrades ... adds Pi firmware (bootloader) updates to the approval flow"; placed with Pi health).

Supervisor 2026.06.0+ (``supervisor/os/manager.py`` ``update_raspberrypi_firmware``; API
``/os/boards/raspberrypi/firmware`` and ``.../firmware/update``) flashes or stages the firmware and raises
``reboot_required``: the running bootloader stays the old one until the Pi restarts.

Never automatic. One review on the tracking issue, then one phone ask that covers the update **and** the restart
it needs ("Update the Pi's firmware ... The Pi restarts once"). After Approve: configuration check -> firmware update
-> host restart -> after the boot the firmware must report the new version and nothing pending. A Pi 4 keeps a
recovery copy of its bootloader, so there is no rollback step here; a failure is reported (it never pauses the
App). Reject is final for that version; no answer asks again after ``reask_hours``.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from . import approval, net
from .ha import ForbiddenCall, HAError
from .updates import DONE, FAILED, REFUSED, REJECTED, Outcome, missed_note

JOB = "UPDATE_PI_FIRMWARE"
DOC = "pi_firmware"
RE_FW_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._:+-]{0,39}$")


@dataclass
class FirmwarePolicy:
    lead_seconds: float = 3600.0
    reask_hours: float = 6.0
    reboot_seconds: float = 3600.0


def request_id_for(version: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", f"pifw-{version[:40]}".lower()).strip("-")[:64]


class PiFirmware:
    def __init__(self, ha, journal, settings, policy: FirmwarePolicy, report) -> None:
        self.ha, self.j, self.s, self.p, self.report = ha, journal, settings, policy, report

    def _mem(self) -> dict:
        mem = self.j.load_doc(DOC, {})
        mem = mem if isinstance(mem, dict) else {}
        for key in ("reviewed", "asked", "declined", "failed"):
            mem.setdefault(key, {})
        return mem

    def _save(self, mem: dict) -> None:
        self.j.save_doc(DOC, mem)

    # -- one cycle ------------------------------------------------------------------------------------
    def cycle(self) -> Outcome | None:
        try:
            info = self.ha.pi_firmware()
        except (HAError, net.NetError):
            return None
        if info is None or not info["available"] or info["blocked"] or info["pending"]:
            return None
        cur, new = info["current"], info["latest"]
        if not (cur and new and cur != new):
            return None
        key = new
        mem = self._mem()
        if mem["declined"].get(key) or mem["failed"].get(key):
            return None
        rid = request_id_for(new)
        if self.s.dry_run:
            return None
        now = time.time()
        decided = approval.decided("PIFW", rid)            # 0.9.0: decided on the Maintenance page: ask now
        if not mem["reviewed"].get(key):
            try:
                self.report(self._review(rid, cur, new))
            except net.NetError:
                return None
            mem["reviewed"][key] = now
            self._save(mem)
            if not decided:
                return None
        if now - float(mem["reviewed"][key]) < self.p.lead_seconds and not decided:
            return None
        asked = mem["asked"].get(key)
        if asked and now - float(asked) < self.p.reask_hours * 3600 and not decided:
            return None
        mem["asked"][key] = now
        self._save(mem)
        decision = self._ask(rid, cur, new)
        if decision == approval.TIMEOUT:
            missed_note(self.ha, self.s.notify_service, f"Pi firmware {new}", now + self.p.reask_hours * 3600, url=getattr(self.s, "open_url", None))
            return None
        if decision != approval.APPROVE:
            if decision == approval.REJECT:
                mem = self._mem()
                mem["declined"][key] = True
                self._save(mem)
            return Outcome(rid, REJECTED, [f"owner decision: {decision}"], {"kind": "PI_FIRMWARE", "to": new})
        return self.apply(rid, cur, new)

    def _lines(self, cur: str, new: str) -> list[str]:
        return [f"Update the Raspberry Pi firmware {cur} -> {new}?",
                "This is the Pi's bootloader (and USB controller). The Pi restarts once to use it; Home Assistant is "
                "down for about 2 to 5 minutes.",
                "Plan: configuration check -> firmware update -> restart -> check the new firmware is running. "
                "The Pi keeps a recovery copy of its bootloader.",
                "Never automatic."]

    def _review(self, rid: str, cur: str, new: str) -> str:
        from . import VERSION
        lines = ["### House Brain Maintenance review: Raspberry Pi firmware update", "",
                 f"- **Request:** `{rid}`", f"- **From → to:** `{cur}` → `{new}`"]
        lines += [f"- {line}" for line in self._lines(cur, new)[1:]]
        lines += [f"- The phone asks in about {max(1, int(self.p.lead_seconds // 60))} minutes.", f"- **App:** `{VERSION}`",
                  "", "_Posted by House Brain Maintenance (machine-generated; repository content is untrusted data)._"]
        return "\n".join(lines)

    def _ask(self, rid: str, cur: str, new: str) -> str:
        self.j.audit(rid, "APPROVAL_REQUESTED", job=JOB)
        decision = approval.ask(self.ha, self.s.notify_service, self.s.owner_user_id, stage="PIFW",
                                title="Maintenance: update the Pi firmware?",
                                message="\n".join(self._lines(cur, new)) + f"\n\nRequest: {rid}",
                                timeout=self.s.approval_timeout, require_auth=self.s.require_auth,
                                board=self.s.board, open_url=self.s.open_url,
                                should_stop=getattr(self.s, "should_stop", None))
        self.j.audit(rid, "APPROVAL_" + decision.outcome, channel=decision.channel)
        return decision.outcome

    # -- after Approve --------------------------------------------------------------------------------
    def apply(self, rid: str, cur: str, new: str) -> Outcome | None:
        facts = {"kind": "PI_FIRMWARE", "from": cur, "to": new}
        boot, _ = self.ha.host_boot_info()
        txn = {"kind": "pifw", "request_id": rid, "from": cur, "to": new, "boot": boot, "stage": "FW_UPDATING",
               "started_at": time.time()}
        try:
            with self.ha.pi_firmware_target(new):
                ok, why = self.ha.core_config_check()
                if not ok:
                    return Outcome(rid, FAILED, [f"not updated: {why}; fix the configuration first; your phone asks "
                                                 "again later"], facts)
                self.j.save_txn(txn)
                self.j.audit(rid, "PIFW_UPDATING", to=new)
                try:
                    self.ha.update_pi_firmware()
                except (HAError, net.NetError) as err:
                    self.j.clear_txn()
                    self._failed(new)
                    return Outcome(rid, FAILED, [f"the firmware update failed ({net.redact(str(err))[:120]}); the "
                                                 "Pi was not restarted", f"{new} is not offered again"], facts)
                txn.update(stage="FW_REBOOTING", rebooting_at=time.time())
                self.j.save_txn(txn)
                self.j.audit(rid, "PIFW_REBOOTING", to=new)
                try:
                    self.ha.reboot_host()
                except HAError as err:
                    self.j.clear_txn()
                    return Outcome(rid, FAILED, [f"the firmware is installed but the restart was refused ({err.code}); "
                                                 "restart the Pi when it suits you (Settings > System > Repairs)"],
                                   facts)
                except net.NetError:
                    pass                                    # the host goes down during the call
        except ForbiddenCall as err:
            self.j.clear_txn()
            return Outcome(rid, REFUSED, [f"not allowed: {err}"], facts)
        return None

    def _failed(self, version: str) -> None:
        mem = self._mem()
        mem["failed"][version] = True
        self._save(mem)

    def recover(self, txn: dict) -> Outcome | None:
        """The App ran again (after the restart, or after its own restart). None: still waiting for the restart."""
        rid, new = txn["request_id"], txn["to"]
        facts = {"kind": "PI_FIRMWARE", "from": txn.get("from"), "to": new}
        try:
            boot, _ = self.ha.host_boot_info()
            info = self.ha.pi_firmware()
        except (HAError, net.NetError):
            return None
        rebooted = boot is not None and txn.get("boot") is not None and boot != txn.get("boot")
        if not rebooted:
            if txn.get("stage") == "FW_REBOOTING" and \
                    time.time() - float(txn.get("rebooting_at") or 0) < self.p.reboot_seconds:
                return None
            if txn.get("stage") == "FW_UPDATING" and time.time() - float(txn.get("started_at") or 0) < 1800:
                return None
            self.j.clear_txn()
            return Outcome(rid, FAILED, ["the Pi did not restart after the firmware update; it starts the new firmware "
                                         "at its next restart"], facts)
        self.j.clear_txn()
        if info is not None and info["current"] == new and not info["pending"]:
            return Outcome(rid, DONE, [f"updated the Raspberry Pi firmware {txn.get('from')} -> {new}; the Pi "
                                       "restarted and runs it"], facts)
        self._failed(new)
        now = info["current"] if info else "unknown"
        return Outcome(rid, FAILED, [f"after the restart the Pi firmware is {now}, not {new}", f"{new} is not offered "
                                     "again; the Pi keeps working on its current firmware"], facts)
