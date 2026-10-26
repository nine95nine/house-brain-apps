"""0.9.34 Google Drive Backup App settings (owner pop-ups 2026-10-26 "Build 0.9.34", "06:45 Arizona", "Both").

Why: two backup makers ran at 05:15 Arizona (Home Assistant's own automatic backup, which runs at a random moment
inside about an hour of its set time, and the Google Drive Backup App at its fixed minute). They blocked each other
("system is not running - freeze"), the App's copies left out an App installed later, and the owner chose to keep
**both** makers and stagger them (deep dive 2026-10-26, R2). This job changes exactly two of that App's settings
through the Supervisor, on the owner's Approve, and restarts that App once so the new settings are in use:

* ``backup_time_of_day``: the fixed time of its daily backup (default here 06:45, after Home Assistant's window);
* ``exclude_addons``: emptied, so every installed App is in its copies.

Never its password (the owner sets that in the App's own screen), never any other setting, never Home Assistant's
own backup schedule. Undo: the two values that were replaced are kept in this App's memory; an ADDON_SETTINGS request
with ``undo: true`` writes them back the same way (ask, write, restart).
"""
from __future__ import annotations

import time
from collections.abc import Callable

from . import approval, issues as I, net
from .ha import DRIVE_SETTINGS_NAMES, RE_DRIVE_BACKUP_SLUG, ForbiddenCall, HAError, HomeAssistant
from .jobs import DONE, DRY_RUN_OK, FAILED, REFUSED, REJECTED, Result
from .manifest import Manifest

APP_NAME = "Google Drive Backup App"
TIME_DEFAULT = "06:45"
UNDO_DOC = "drive_settings_undo"
FIX_KIND = "drive_backup_settings"
FIX_KEYS = ("backup:auto_failed", "backup:incomplete")
FIX_LABEL = f"move the {APP_NAME}'s daily backup to {TIME_DEFAULT} and include every App in it"
FIXED, NOT_FIXED = "FIXED", "NOT_FIXED"


def wanted(current: dict, time_of_day: str | None, include_all: bool | None) -> dict:
    """The two settings after the change (unchanged ones keep their stored value)."""
    after = {name: str(current.get(name, "")) for name in DRIVE_SETTINGS_NAMES}
    if time_of_day is not None:
        after["backup_time_of_day"] = time_of_day
    if include_all:
        after["exclude_addons"] = ""
    return after


def _excluded(value: str) -> str:
    slugs = [s.strip() for s in str(value or "").split(",") if s.strip()]
    if not slugs:
        return "none (every App included)"
    shown = ", ".join(slugs[:6]) + (f" and {len(slugs) - 6} more" if len(slugs) > 6 else "")
    return f"{len(slugs)} App{'s' if len(slugs) > 1 else ''} left out ({shown})"


def describe(before: dict, after: dict) -> list[str]:
    """Plain lines for the phone ask and the report; only the two settings, never another option."""
    lines = []
    if before.get("backup_time_of_day") != after.get("backup_time_of_day"):
        lines.append(f"Daily backup time: {before.get('backup_time_of_day') or 'not set'} -> "
                     f"{after.get('backup_time_of_day') or 'not set'}.")
    if before.get("exclude_addons") != after.get("exclude_addons"):
        lines.append(f"Apps left out of its backups: {_excluded(before.get('exclude_addons', ''))} -> "
                     f"{_excluded(after.get('exclude_addons', ''))}.")
    return lines


def attach_fix(findings: list[I.Finding], slugs: list[str]) -> list[I.Finding]:
    """0.9.34: the backup problems this change addresses get the Fix it action when the App is installed."""
    drive = [s for s in slugs if RE_DRIVE_BACKUP_SLUG.fullmatch(s)]
    if len(drive) != 1:
        return findings
    import dataclasses
    action = I.Action(FIX_KIND, drive[0], FIX_LABEL)
    return [dataclasses.replace(f, action=action, done="") if f.key in FIX_KEYS and f.action is None else f
            for f in findings]


class DriveSettings:
    def __init__(self, ha: HomeAssistant, journal, settings, ask: Callable[[Manifest, str, list[str]], str] | None,
                 clock: Callable[[], float] = time.time) -> None:
        self.ha, self.j, self.s, self.ask, self.clock = ha, journal, settings, ask, clock

    # -- the change itself (inside one target) ------------------------------------------------------------
    def _change(self, slug: str, after: dict, request_id: str, facts: dict) -> str | None:
        """Read, write the two settings, restart, read back. Returns None on success, else the problem."""
        state, version, before = self.ha.drive_settings_read()
        facts.update(state=state, version=version, before=before, after=after)
        if before == after:
            return "already set that way; nothing changed"
        self.ha.drive_settings_write(after)
        self.j.audit(request_id, "DRIVE_SETTINGS_WRITTEN", slug=slug)
        self.j.save_doc(UNDO_DOC, {"request_id": request_id, "slug": slug, "before": before, "after": after,
                                   "at": int(self.clock())})
        self.ha.drive_settings_restart()
        facts["restarted"] = True
        _, _, now = self.ha.drive_settings_read()
        facts["verified"] = now == after
        if now != after:
            return "the App reports other values after the write; check its Settings screen"
        return None

    # -- ADDON_SETTINGS request ----------------------------------------------------------------------------
    def run(self, m: Manifest) -> Result:
        facts: dict = {"addon": m.addon, "undo": m.addon_undo}
        slug = self.ha.drive_backup_slug()
        if slug is None:
            return Result(REFUSED, [f"the {APP_NAME} is not installed (or more than one is)"], facts)
        facts["slug"] = slug
        if m.addon_undo:
            doc = self.j.load_doc(UNDO_DOC, {})
            if not isinstance(doc, dict) or doc.get("slug") != slug or not isinstance(doc.get("before"), dict):
                return Result(REFUSED, ["nothing to undo: no earlier change of this App's settings is on record"],
                              facts)
            after = {name: str(doc["before"].get(name, "")) for name in DRIVE_SETTINGS_NAMES}
            facts["undo_of"] = str(doc.get("request_id") or "")[:80]
        with self.ha.drive_settings_target(slug):
            state, version, before = self.ha.drive_settings_read()
            facts.update(state=state, version=version, before=before)
            if not m.addon_undo:
                after = wanted(before, m.addon_time_of_day, m.addon_include_all)
            facts["after"] = after
            lines = describe(before, after)
            if not lines:
                return Result(REFUSED, [f"the {APP_NAME} already has these settings; nothing asked or changed"],
                              facts)
            if self.s.dry_run:
                facts["would_ask"] = True
                return Result(DRY_RUN_OK, ["dry run: nothing asked or changed"], facts)
            ask = [f"{APP_NAME} {version or ''}".strip() + (" (undo the last change)" if m.addon_undo else "") + ":",
                   *lines,
                   "The App is restarted once so the change takes effect. Its backups, its Google Drive copies and "
                   "Home Assistant's own backup schedule are not touched; its password is not read or changed.",
                   "Approve to apply; Reject changes nothing."]
            outcome = self.ask(m, f"Maintenance: {APP_NAME} settings", ask) if self.ask else approval.APPROVE
            if outcome != approval.APPROVE:
                return Result(REJECTED, [f"owner decision: {outcome}"], facts)
            problem = self._change(slug, after, m.request_id, facts)
        if problem:
            return Result(FAILED if facts.get("restarted") else REFUSED, [problem], facts)
        return Result(DONE, [f"{APP_NAME}: " + " ".join(lines) + " The App was restarted and reports the new values."],
                      facts)

    # -- "Fix it" on a backup problem (the Approve was given on the phone or the page) ---------------------
    def fix(self, slug: str, request_id: str) -> tuple[str, str]:
        facts: dict = {}
        try:
            with self.ha.drive_settings_target(slug):
                _, _, before = self.ha.drive_settings_read()
                after = wanted(before, TIME_DEFAULT, True)
                lines = describe(before, after)
                if not lines:
                    return FIXED, f"the {APP_NAME} already backs up at {TIME_DEFAULT} with every App included"
                problem = self._change(slug, after, request_id, facts)
        except (HAError, net.NetError, ForbiddenCall) as err:
            return NOT_FIXED, net.redact(f"the settings could not be changed: {err}")[:200]
        if problem:
            return NOT_FIXED, problem
        return FIXED, " ".join(lines) + " The App was restarted and reports the new values."
