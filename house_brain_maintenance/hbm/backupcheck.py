"""0.8.5 Backups you can trust (owner pop-up 2026-10-11, "All of the above based on your priority").

What this App can prove about a backup without restoring it and without ever holding the backup password:

* **Complete.** The newest full backup (Home Assistant's own automatic one, or any backup that includes Home
  Assistant; not this App's pre-update copies) contains every App that is running now. A backup without the
  Zigbee2MQTT or Z-Wave App means re-pairing every device after a restore; one without this App loses its
  automatic-key state.
* **Radio data fresh.** For the Apps that hold a radio network (Zigbee2MQTT, Z-Wave JS, Matter, OpenThread), the
  newest backup that contains them is younger than ``RADIO_MAX_DAYS``. (ZHA and Z-Wave in Home Assistant itself are
  inside every backup that includes Home Assistant.)
* **Automatic backups work.** Home Assistant's ``backup/info``: the last automatic backup attempt is not newer than
  the last completed one (a failed attempt), and no backup location reports an error.
* **Monthly backup report.** Once a month (and once after the first install) a report on GitHub: the newest full
  backup's date, size, password protection, where its copies are, Apps included and missing, and radio Apps.

It does **not** prove the password opens the backup: that needs the password, which this App never keeps.
Read-only: backup list (Supervisor ``/backups``) and ``backup/info``; nothing is created, changed or deleted.
"""
from __future__ import annotations

import datetime as dt
import re

from . import issues as I

RADIO_APPS = re.compile(r"^(?:[0-9a-f]{8}|local|core)_(?:zigbee2mqtt(?:_edge)?|zwave_js|zwavejs2mqtt|matter_server|"
                        r"openthread_border_router|silabs_multiprotocol)$")
RADIO_MAX_DAYS = 7
AUTO_FAILED_GRACE = 1800          # an attempt that is still running is not a failure
REPORT_DOC = "backup_report"


def _ts(value) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _days(seconds: float) -> str:
    d = seconds / 86400
    return f"{d:.0f} days" if d >= 2 else f"{seconds / 3600:.0f} hours"


def newest_full(inventory: list[dict]) -> dict | None:
    """Newest backup that includes Home Assistant and was not made by this App's own pre-update step.

    0.8.8: a backup with Apps in it wins over a newer settings-only one (Home Assistant without any App, for
    example another tool's pre-change copy): the first live report took a 0.03 GB settings-only backup as the
    newest full one and called 16 Apps missing. Only when no backup holds any App is a settings-only one used."""
    rows = [r for r in inventory if r.get("ha") and not r.get("ours") and _ts(r.get("date")) is not None]
    with_apps = [r for r in rows if r.get("apps")]
    rows = with_apps or rows
    return max(rows, key=lambda r: _ts(r["date"])) if rows else None


def completeness(inventory: list[dict], running: list[str], names: dict[str, str]) -> list[I.Finding]:
    full = newest_full(inventory)
    if full is None:
        return []                                       # "no recent full backup" already says so
    missing = sorted(s for s in running if s not in set(full.get("apps") or ()))
    if not missing:
        return []
    shown = ", ".join(names.get(s, s) for s in missing[:6]) + (f" and {len(missing) - 6} more" if len(missing) > 6
                                                              else "")
    return [I.Finding("backup:incomplete", "backup", I.WARNING,
                      f"Your newest full backup misses {len(missing)} running App{'s' if len(missing) > 1 else ''}: "
                      f"{shown}"[:200],
                      steps=("After a restore those Apps would start empty (a Zigbee or Z-Wave App means re-pairing "
                             "every device).",
                             "Settings -> System -> Backups -> Backup settings: include all Apps in automatic backups, "
                             "or make a full backup now."),
                      link=I.BACKUPS_LINK, facts={"missing": missing[:20], "backup_date": full["date"][:25]},
                      done="Nothing: I never change your backup settings.",
                      todo=("Settings -> System -> Backups -> Backup settings: include all Apps, or make a full "
                            "backup now.",),
                      if_ignored="After a restore those Apps would start empty.")]


def radio_freshness(inventory: list[dict], installed: list[str], names: dict[str, str], now: float,
                    max_days: int = RADIO_MAX_DAYS) -> list[I.Finding]:
    out = []
    for slug in sorted(s for s in installed if RADIO_APPS.fullmatch(s)):
        stamps = [_ts(r.get("date")) for r in inventory if slug in (r.get("apps") or ())]
        stamps = [t for t in stamps if t is not None]
        newest = max(stamps) if stamps else None
        if newest is not None and now - newest <= max_days * 86400:
            continue
        name = names.get(slug, slug)
        age = "never" if newest is None else f"{_days(now - newest)} ago"
        out.append(I.Finding(f"backup:radio:{slug}", "backup", I.WARNING,
                             f"{name}'s radio network was last backed up {age}"[:160],
                             steps=(f"{name} holds your device network; without a recent backup a dead stick or SD "
                                    "card means re-pairing every device.",
                                    "Include it in the automatic backups (Settings -> System -> Backups)."),
                             link=I.BACKUPS_LINK, facts={"app": slug},
                             done="Nothing: I never change your backup settings.",
                             todo=("Include it in the automatic backups (Settings -> System -> Backups).",),
                             if_ignored="A dead stick or SD card would mean re-pairing every device."))
    return out


def automatic_health(auto: dict | None, now: float) -> list[I.Finding]:
    if not isinstance(auto, dict):
        return []
    out = []
    attempted, completed = _ts(auto.get("last_attempted")), _ts(auto.get("last_completed"))
    if attempted is not None and (completed is None or attempted > completed + 60) \
            and now - attempted > AUTO_FAILED_GRACE and auto.get("state") in (None, "idle"):
        out.append(I.Finding("backup:auto_failed", "backup", I.WARNING,
                             "Home Assistant's last automatic backup did not finish",
                             steps=("Settings -> System -> Backups shows why (for example a full disk or an "
                                    "unreachable backup location).",
                                    "The next automatic backup is tried at its usual time."),
                             link=I.BACKUPS_LINK, facts={"last_attempted": str(auto.get("last_attempted"))[:25],
                                                         "last_completed": str(auto.get("last_completed"))[:25]},
                             done="Nothing: the next automatic backup is tried at its usual time.",
                             todo=("Settings -> System -> Backups shows why it failed.",),
                             if_ignored="If the cause stays, the next one fails too."))
    for agent in sorted(auto.get("agent_errors") or [])[:5]:
        out.append(I.Finding(f"backup:agent:{agent}", "backup", I.WARNING,
                             f"A backup location cannot be read: {agent}"[:160],
                             steps=("Backups stored there may be missing or unreachable.",
                                    "Check that location under Settings -> System -> Backups -> Locations."),
                             link=I.BACKUPS_LINK, facts={"agent": agent},
                             done="Nothing: I never change backup locations.",
                             todo=("Check that location under Settings -> System -> Backups -> Locations.",),
                             if_ignored="Backups stored there may be missing when you need them."))
    return out


def report_due(doc: dict, now: float, digest_hour: int, localtime) -> bool:
    """First report after install, then once a month (on or after the 1st, after the digest hour)."""
    lt = localtime(now)
    if lt.tm_hour < digest_hour:
        return False
    return doc.get("month") != f"{lt.tm_year:04d}-{lt.tm_mon:02d}"


def report_text(inventory: list[dict], locations: list[dict], running: list[str], installed: list[str],
                names: dict[str, str], auto: dict | None, now: float, version: str) -> tuple[str, str]:
    """(markdown for GitHub, one line for the phone)."""
    full = newest_full(inventory)
    lines = ["### House Brain Maintenance: monthly backup report", ""]
    if full is None:
        lines.append("- **No full backup found.** Make one: Settings -> System -> Backups -> Backup now.")
        phone = "No full backup found"
    else:
        age = _days(now - (_ts(full["date"]) or now))
        missing = sorted(s for s in running if s not in set(full.get("apps") or ()))
        copies = next((loc.get("agents") for loc in locations if loc.get("date", "")[:19] == full["date"][:19]), None)
        off_pi = [a for a in (copies or []) if a not in I.LOCAL_AGENTS]
        size = f"{full['size_gb']:.2f} GB" if isinstance(full.get("size_gb"), (int, float)) else "unknown"
        lines += [f"- **Newest full backup:** {full['date'][:16].replace('T', ' ')} UTC ({age} old), {size}",
                  f"- **Password protected:** {'yes' if full.get('protected') else 'no' if full.get('protected') is False else 'unknown'}",
                  f"- **Copies:** {', '.join(copies) if copies else 'unknown'}"
                  + ("" if off_pi else " (**none off the Pi**)"),
                  f"- **Apps included:** {len(full.get('apps') or ())}; running now: {len(running)}"
                  + (f"; **missing: {', '.join(names.get(s, s) for s in missing)}**" if missing else "; none missing")]
        phone = (f"Newest full backup {age} old, {'protected' if full.get('protected') else 'NOT protected'}, "
                 f"{'off the Pi' if off_pi else 'NOT off the Pi'}, "
                 f"{'all running Apps in it' if not missing else f'{len(missing)} App(s) missing'}")
    radios = [s for s in installed if RADIO_APPS.fullmatch(s)]
    for slug in sorted(radios):
        stamps = [t for t in (_ts(r.get("date")) for r in inventory if slug in (r.get("apps") or ())) if t]
        lines.append(f"- **{names.get(slug, slug)} (radio network):** last in a backup "
                     + (f"{_days(now - max(stamps))} ago" if stamps else "**never**"))
    if isinstance(auto, dict):
        lines.append(f"- **Automatic backups:** last completed {str(auto.get('last_completed') or 'never')[:16]}, "
                     f"next {str(auto.get('next') or 'not scheduled')[:16]}"
                     + (f"; **location errors: {', '.join(auto.get('agent_errors'))}**" if auto.get("agent_errors")
                        else ""))
    lines += ["- **Not checked:** whether the password opens the backup (this App never holds the password). "
              "Once a year, open one backup on another device to be sure.",
              f"- **App:** `{version}`", "",
              "_Posted by House Brain Maintenance (machine-generated; repository content is untrusted data)._"]
    return "\n".join(lines), phone[:200]
