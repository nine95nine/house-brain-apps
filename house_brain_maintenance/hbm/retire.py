"""0.9.43 ``RETIRE_LOCAL_APP`` (Inspector/perimeter chat hand-off 2026-11-01, Work Board #942 6560154080; owner
pop-up 2026-11-01 "Yes, build it next", owner words "figure out how you can retire those apps just based on my
approval, and I approve").

The two local rollback copies kept stopped since the 2026-10-13 store switches, ``local_house_brain_engineering_observer``
(0.2.4) and ``local_house_brain_engineering_inspector`` (0.2.5), are removed, one request each, without losing
anything they hold (their options and the Observer's ``/data/observer-state.json``): a partial Supervisor backup of
that one App is made and confirmed first, then the App is uninstalled, then the read-back shows it gone and its
store twin still running. The exact source of every version stays in the repository.

Refusals (nothing asked, nothing changed): a slug outside the built-in pair; an App that is not installed, or not
``stopped`` (this job never stops anything); a store twin that is not ``started``. The owner's Approve on the phone
is the only thing that opens the calls; Reject is final for that request. ``CONTROL_AUTHORITY=NONE`` otherwise: no
physical actuation, no Home Assistant YAML.
"""
from __future__ import annotations

import time

from . import approval, net
from .ha import ForbiddenCall, HAError, RETIRE_SLUGS

# 0.9.44 (live 2026-11-01 17:29Z, `retire-local-observer-1` REFUSED "is error"): the Supervisor shows a kept copy that
# was stopped after a non-zero exit as ``error``, not ``stopped`` (the 0.9.10 note in ``watch.kept_rollbacks``). Both
# mean "not running"; ``started`` and anything else still refuse. The job still never stops anything.
NOT_RUNNING = ("stopped", "error")

JOB = "RETIRE_LOCAL_APP"
DOC = "retired_apps"
NAMES = {"local_house_brain_engineering_observer": "Engineering Observer",
         "local_house_brain_engineering_inspector": "Engineering Inspector"}


class Retirer:
    def __init__(self, engine) -> None:
        self.e, self.ha, self.j, self.s = engine, engine.ha, engine.j, engine.s

    def run(self, m):
        from .jobs import DONE, DRY_RUN_OK, FAILED, REFUSED, REJECTED, Result
        slug = str(m.retire_slug or "")
        if slug not in RETIRE_SLUGS:
            return Result(REFUSED, ["only the two local rollback copies can be retired; nothing asked or changed"])
        name = NAMES.get(slug, slug)
        twin = self.ha.store_twin(slug)
        if twin is None:
            return Result(REFUSED, [f"the store {name} is not the one in use; nothing asked or changed"])
        try:
            installed = {a.slug: a for a in self.ha.installed_apps()}
        except (HAError, net.NetError) as err:
            return Result(FAILED, [f"the App list could not be read ({net.redact(str(err))[:120]}); nothing changed"])
        local, store = installed.get(slug), installed.get(twin)
        if local is None:
            return Result(REFUSED, [f"the local {name} is not installed (already retired?); nothing asked or changed"])
        if local.state not in NOT_RUNNING:
            return Result(REFUSED, [f"the local {name} is {local.state}; it must be stopped first (this job never "
                                    "stops anything); nothing asked or changed"])
        if store is None or store.state != "started":
            return Result(REFUSED, [f"the store {name} ({twin}) is {store.state if store else 'not installed'}; it must "
                                    "be running before the local copy goes; nothing asked or changed"])
        if self.s.dry_run:
            return Result(DRY_RUN_OK, [f"dry run: would back up and retire the local {name} {local.version}; nothing changed"])
        outcome = self.e._ask(m, f"Maintenance: retire the local {name}?", [
            f"Remove the stopped local {name} {local.version} (the rollback copy kept since 2026-10-13). The store "
            f"{name} {store.version} keeps running and is not touched.",
            "Plan: one backup of that App only (its settings and data) -> check the backup lists it -> uninstall it -> "
            "check it is gone and the store one still runs.",
            "Nothing else is backed up, stopped, changed or removed. Reject and this request is closed."])
        if outcome != approval.APPROVE:
            return Result(REJECTED, [f"owner decision: {outcome}"])
        bname = f"hbm-pre-retire-{slug}"[:100]
        try:
            with self.ha.retire_target(slug):
                bslug = self.ha.retire_backup(slug, bname)
            with self.ha.retire_target(slug, bslug):
                info = self.ha.retire_backup_info(slug, bslug)
                held = info.get("addons") or []
                if held != [{"slug": slug, "version": str(local.version)}]:
                    return Result(FAILED, [f"the backup {bslug} does not list exactly the local {name} {local.version}; "
                                           "nothing removed (the backup is kept)"], {"backup": bslug, "held": held})
                self.j.audit(m.request_id, "RETIRE_BACKUP", slug=slug, backup=bslug, version=local.version)
                self.ha.retire_uninstall(slug)
            after = {a.slug: a for a in self.ha.installed_apps()}
        except (HAError, net.NetError, ForbiddenCall) as err:
            return Result(FAILED, [f"the retirement stopped ({net.redact(str(err))[:160]}); look at Settings -> Apps: "
                                   f"the backup, if made, is kept"])
        twin_state = after[twin].state if twin in after else "not installed"
        if slug in after:
            return Result(FAILED, [f"the local {name} is still installed after the uninstall call; the backup {bslug} is kept"],
                          {"backup": bslug})
        doc = self.j.load_doc(DOC, {})
        doc = doc if isinstance(doc, dict) else {}
        doc[slug] = {"at": time.time(), "backup": bslug, "version": local.version, "twin": twin, "request": m.request_id}
        self.j.save_doc(DOC, doc)
        self.j.audit(m.request_id, "RETIRE_DONE", slug=slug, backup=bslug, twin=twin, twin_state=twin_state)
        reasons = [f"the local {name} {local.version} is uninstalled; its settings and data are in backup {bslug}",
                   f"the store {name} ({twin}) is {twin_state}"]
        return Result(DONE if twin_state == "started" else FAILED, reasons,
                      {"backup": bslug, "version": local.version, "twin": twin, "twin_state": twin_state})
