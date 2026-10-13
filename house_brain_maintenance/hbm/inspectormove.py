"""0.9.9 Engineering Inspector from the store (owner pop-ups 2026-10-13: "Inspector 0.2.6 adds it", "Only in export
mode"). The last step of the store move ("Scout first, then others", 2026-10-12).

The same one-Approve, journalled sequence as the Observer switch (``observermove``), with three differences:

* **Only in export mode.** The switch is offered only while the local Inspector runs with
  ``entity_snapshot_mode: export`` (its live mode, the AI read plane's entity snapshots). Only then can success be
  proved: DONE when the store Inspector's own log shows ``ENTITY_SNAPSHOT_STORED`` (the Broker stored its snapshot,
  HTTP 204; it sends every 300 s), within ``watch_seconds``.
* **No boot setting.** Both Inspectors are ``manual_only`` Apps, so nothing about Start on boot is read or written;
  the local one is only stopped (kept, never uninstalled) and started again on undo.
* **Its three settings** are ``broker_entity_url``, ``entity_snapshot_mode`` (copied as ``export``) and
  ``entity_write_key`` (registered as a secret, read back by fingerprint).

After DONE, key changes (role ``inspector_entity``) use the store Inspector, recorded in ``inspector_store`` so it
survives a restart. ``CONTROL_AUTHORITY=NONE``.
"""
from __future__ import annotations

from .ha import INSPECTOR_SLUG
from .observermove import (DONE, DRY_RUN_OK, FAILED_MANUAL, REFUSED, REJECTED, UNDONE,  # noqa: F401 (re-exported)
                           ObserverMove, ObserverMovePolicy, Outcome)

JOB = "INSPECTOR_STORE_SWITCH"
STAGE = "INSMOVE"
DOC = "inspector_store"
TXN = "insmove"
STORED = "ENTITY_SNAPSHOT_STORED"


class InspectorMove(ObserverMove):
    JOB, STAGE, DOC, TXN, MARKER = JOB, STAGE, DOC, TXN, STORED
    LOCAL = INSPECTOR_SLUG
    NAME = "Engineering Inspector"
    PREFIX = "inspector-store"
    AUDIT = "INSMOVE"
    SET_BOOT = False                    # manual_only Apps: no Start-on-boot setting exists
    ASK = ("Switch the Engineering Inspector to the version from the House Brain store?",
           "Plan: copy its 3 settings (Broker address, export mode, key) -> check them by fingerprint -> stop the "
           "local Inspector (kept, not removed) -> start the store Inspector -> check the Broker stores its entity "
           "snapshot. Any problem: the local one runs again automatically.",
           "Then Inspector updates come as normal store updates (review, backup, health watch, restore).")
    DONE_WHY = "sends entity snapshots the Broker stores"
    KEPT_WHY = "the local Inspector is stopped (kept, not removed)"
    MANUAL = "Settings -> Apps -> House Brain Engineering Inspector (the local one) -> Start"

    def _current(self) -> str:
        return self.ha.inspector

    def _store_slug(self) -> str | None:
        return self.ha.store_inspector_slug()

    def _target(self, old: str, new: str):
        return self.ha.inspector_move_target(old, new)

    def _copy(self) -> tuple[str, str]:
        return self.ha.copy_inspector_settings()

    def _view(self):
        return self.ha.store_inspector_view()

    def _log(self) -> str:
        return self.ha.store_inspector_log()

    def _retarget(self, new: str) -> None:
        self.ha.retarget_inspector(new)

    def _precheck(self) -> str | None:
        mode = self.ha.inspector_mode()
        if mode != "export":
            return f"the local Inspector is not exporting snapshots (mode {mode or 'unknown'}), so a switch cannot be checked"
        return None
