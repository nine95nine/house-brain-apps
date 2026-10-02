"""Problem detection: turn projected Home Assistant facts into findings with plain-English fixes.

Pure functions (no I/O, clock passed in). Owner decisions 2026-09-30 (Maintenance chat):
* watch Repairs + Supervisor health, crashed Apps + failing integrations, the error log, disk
  space + backups;
* serious problems notify at once, the rest in one morning summary; each problem once, plus a
  note when it clears;
* suggest fixes; a short list of safe fixes may run after the owner taps Approve (Face ID):
  restart/start one App, reload one integration, apply one of Supervisor's own safe suggestions;
* details go to the tracking issue with secrets, IP and e-mail addresses stripped.

A finding never changes anything by itself; ``Action`` only names the fix the owner may approve.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import net

CRITICAL = "critical"
WARNING = "warning"

REPAIRS_LINK = "https://my.home-assistant.io/redirect/repairs/"
APPS_LINK = "https://my.home-assistant.io/redirect/supervisor/"
INTEGRATIONS_LINK = "https://my.home-assistant.io/redirect/integrations/"
BACKUPS_LINK = "https://my.home-assistant.io/redirect/backup_list/"
LOGS_LINK = "https://my.home-assistant.io/redirect/logs/"
SYSTEM_LINK = "https://my.home-assistant.io/redirect/system_health/"

# Supervisor suggestions safe to apply after an owner tap. Never reboot, remove, wipe, clear
# backups, disk adoption/renaming, boot changes or updates (updates have their own job).
SAFE_SUGGESTIONS = frozenset({"execute_repair", "execute_reload", "execute_restart"})
SUGGESTION_LABELS = {
    "execute_repair": "let Home Assistant run its own repair",
    "execute_reload": "reload it",
    "execute_restart": "restart it",
}
ENTRY_PROBLEM_STATES = {"setup_error": CRITICAL, "migration_error": CRITICAL, "failed_unload": WARNING,
                        "setup_retry": WARNING}
RETRY_CRITICAL_SECONDS = 30 * 60

# House-specific known fixes (owner knowledge). Matched on integration domain + reason text.
# They replace the generic advice and the reload offer (a reload does not help these cases).
KNOWN_ENTRY_FIXES = {
    # Owner 2026-10-01: iAquaLink goes offline when the Wi-Fi extender by the pool equipment drops;
    # unplugging it for 10 seconds brings it back. A reload did not help (live NOT_FIXED 2026-09-30).
    ("iaqualink", "offline"): ("iAquaLink lost its connection",
                               ("Unplug the Wi-Fi extender by the pool equipment for 10 seconds, then plug it back in.",
                                "iAquaLink usually reconnects within a few minutes; Home Assistant retries by itself.")),
}
# Known fixes that may be offered as a one-tap power cycle of the configured switch (owner design
# approval 2026-10-02): only after the problem has lasted this long, only for these keys.
POWER_CYCLE_FIXES = {("iaqualink", "offline"): "power-cycle the pool Wi-Fi extender (IAquaLink WiFi plug)"}
POWER_CYCLE_AFTER_SECONDS = 10 * 60
# UPnP/IGD "Device not discovered" (live 2026-10-02, Orbi RBR840): Home Assistant waits for the
# router's network announcement (SSDP) and gives up after 10 s; a reload repeats the same wait, so it
# cannot help unless the router is heard again. What Home Assistant hears decides the real cause.
UPNP_NOT_DISCOVERED = "device not discovered:"
IGD_ST = "urn:schemas-upnp-org:device:InternetGatewayDevice:"
ROUTER_UPNP_STEPS = (
    "In Safari open orbilogin.com (router admin login) -> ADVANCED -> Advanced Setup -> UPnP: tick"
    " 'Turn UPnP On' and Apply.",
    "If it was already on: restart the Orbi router (internet off for about 3 minutes). Home Assistant"
    " reconnects by itself within 10 minutes; nothing to do in Home Assistant.",
    "Still not heard after that: the router's announcements are not reaching the Pi (for example through"
    " the Orbi satellite). Plug the Pi's switch into the Orbi router, or check Settings -> System -> Network"
    " uses the Pi's wired adapter.",
)
UPNP_DIAGNOSES = {
    # code: (title, steps, keep the reload offer)
    "UPNP_HEARD_NOW": ("UPnP router is announcing again", (
        "Home Assistant hears the router again; its own retry reconnects it within minutes.",
        "Or tap Approve to reload the integration now."), True),
    "UPNP_ROUTER_NEW_IDENTITY": ("UPnP router came back with a new identity", (
        "Settings -> Devices & services: under 'Discovered', add the UPnP/IGD router (RBR840).",
        "Then open the old UPnP/IGD entry -> three dots -> Delete. Only the router's own traffic sensors"
        " change; nothing else in the house uses them.",
        "Usually after a router firmware update or factory reset."), False),
    "UPNP_ROUTER_SILENT": ("Router stopped announcing UPnP", ROUTER_UPNP_STEPS, False),
    "UPNP_NOTHING_HEARD": ("Home Assistant hears no network announcements at all", (
        "This is on the Home Assistant side, not the router: Settings -> System -> Network -> Network adapter:"
        " turn on 'Auto configure' (or select the Pi's wired adapter) and Save.",
        "Then check the Pi's network cable and the switch it is plugged into."), False),
    "UPNP_UNCHECKED": ("UPnP router not heard on the network", ROUTER_UPNP_STEPS, False),
}
UNSURE_DIAGNOSES = frozenset({"UPNP_UNCHECKED"})
FULL_BACKUP_MAX_DAYS = 7
DISK_CRITICAL_GB = 2.0          # below this Home Assistant can stop recording and updating
DISK_WARNING = (5.0, 0.10)      # GB free, fraction free

SUPERVISOR_ISSUES = {
    "free_space": ("Disk almost full", CRITICAL,
                   ["Delete old backups you no longer need.", "Remove Apps you don't use."], BACKUPS_LINK),
    "docker_ratelimit": ("Download limit reached (Docker Hub)", WARNING,
                         ["Nothing to do: updates work again in a few hours."], None),
    "corrupt_docker": ("App storage is damaged", CRITICAL, ["Home Assistant can repair this itself."], SYSTEM_LINK),
    "corrupt_repository": ("An App store is damaged", WARNING, ["Home Assistant can repair this itself."], APPS_LINK),
    "missing_image": ("An App's files are missing", CRITICAL, ["Home Assistant can repair this itself."], APPS_LINK),
    "dns_server_failed": ("Name lookup (DNS) is failing", CRITICAL,
                          ["Check that the router is up and the internet works.", "Restarting the router often fixes it."],
                          None),
    "dns_server_ipv6_error": ("Name lookup (DNS) over IPv6 is failing", WARNING,
                              ["Usually harmless; if it stays, turn IPv6 off in Settings -> System -> Network."], None),
    "ipv4_connection_problem": ("No internet connection", CRITICAL,
                                ["Check the router and the network cable."], None),
    "pwned": ("A password in your setup is known from a data breach", CRITICAL,
              ["Change that password where it is used and in secrets.yaml. It is never shown here."], REPAIRS_LINK),
    "reboot_required": ("The system needs a reboot", WARNING,
                        ["When convenient: Settings -> System -> power icon (top right) -> Reboot system."], None),
    "update_failed": ("An update failed", CRITICAL, ["Open Repairs for details."], REPAIRS_LINK),
    "update_rollback": ("An update was rolled back", WARNING, ["Open Repairs for details."], REPAIRS_LINK),
}


@dataclass(frozen=True)
class Action:
    kind: str       # restart_app | start_app | apply_suggestion | reload_entry
    ref: str
    label: str      # plain English, e.g. "restart Zigbee2MQTT"


@dataclass(frozen=True)
class Finding:
    key: str
    source: str     # supervisor | repairs | app | integration | log | disk | backup
    severity: str
    title: str
    detail: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    link: str | None = None
    action: Action | None = None
    announce_clear: bool = True
    facts: dict = field(default_factory=dict, compare=False, hash=False)


# Log errors that only say "the internet/DNS was unreachable" (live summary 2026-10-02: Sense, NWS, ecobee and
# the relay all failed in the same blip). The summary merges them into one line (owner decision 2026-10-02).
NETWORK_ERROR_MARKERS = ("timeout while contacting dns", "cannot connect to host", "possible connectivity outage",
                         "temporary failure in name resolution", "name or service not known",
                         "network is unreachable", "max retries exceeded")


def group_network(items: list[dict]) -> list[dict]:
    """Merge summary items whose details only show an internet/DNS failure into one item."""
    net_items, rest = [], []
    for it in items:
        text = " ".join(str(d) for d in it.get("detail") or ()).lower()
        is_log = str(it.get("key", "")).startswith("log:")
        (net_items if is_log and any(m in text for m in NETWORK_ERROR_MARKERS) else rest).append(it)
    if len(net_items) < 2:
        return items
    names = []
    for it in net_items:
        title = str(it.get("title", ""))
        name = title[len("Error from "):].split(" (seen", 1)[0] if title.startswith("Error from ") else title
        if name not in names:
            names.append(name)
    merged = {"key": "net:blip", "title": f"Internet or DNS dropped briefly ({len(net_items)} errors)",
              "severity": WARNING, "detail": ["Affected: " + ", ".join(names)[:300]],
              "steps": ["Nothing to do if it was short; these recover by themselves.",
                        "If it keeps happening, restart the router and check the internet connection."],
              "link": None, "facts": {"merged": len(net_items)}, "fix": None}
    return [merged] + rest


def unique(values: list[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        if v not in out:
            out.append(v)
    return out


def humanize(ident: str) -> str:
    text = re.sub(r"[_\-.]+", " ", ident or "").strip()
    return (text[:1].upper() + text[1:])[:80] if text else "Unknown"


_SCRUB = (
    (re.compile(r"(?i)\b(bearer|token|password|passwd|pwd|secret|api[_-]?key|access[_-]?key|auth)"
                r"(\s*[=:]\s*|\s+)[^\s,;'\"]+"), r"\1\2[redacted]"),
    (re.compile(r"(?i)\b[\w.+-]+@[\w-]+(\.[\w-]+)+\b"), "[email]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[ip]"),
    (re.compile(r"(?i)\b(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}\b"), "[ip]"),
    (re.compile(r"(?i)\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b"), "[mac]"),
    (re.compile(r"(https?://[^\s?#]+)\?[^\s]*"), r"\1?[query]"),
    (re.compile(r"\b(?=[A-Za-z_\-]*\d)[A-Za-z0-9_\-]{32,}\b"), "[long-id]"),   # ids/keys contain digits
)


def scrub(text: str, limit: int = 600) -> str:
    """Remove secrets, addresses and long identifiers before text leaves Home Assistant."""
    out = net.redact(str(text))
    for pattern, repl in _SCRUB:
        out = pattern.sub(repl, out)
    return out[:limit]


# -- detectors --------------------------------------------------------------------------
def from_resolution(res: dict, app_names: dict[str, str]) -> list[Finding]:
    out: list[Finding] = []
    suggestions = list(res.get("suggestions") or [])
    used: set[str] = set()
    for reason in res.get("unhealthy") or []:
        out.append(Finding(f"sup:unhealthy:{reason}", "supervisor", CRITICAL,
                           f"Home Assistant reports it is unhealthy ({humanize(reason)})",
                           steps=("Read the explanation at the link; most causes need one setting changed.",),
                           link=f"https://www.home-assistant.io/more-info/unhealthy/{reason}"))
    for reason in res.get("unsupported") or []:
        out.append(Finding(f"sup:unsupported:{reason}", "supervisor", WARNING,
                           f"Home Assistant marks this setup as unsupported ({humanize(reason)})",
                           steps=("Nothing is broken; read the link to see what it means and whether to change it.",),
                           link=f"https://www.home-assistant.io/more-info/unsupported/{reason}"))
    for issue in res.get("issues") or []:
        kind, ctx, ref = issue["type"], issue["context"], issue["reference"]
        title, severity, steps, link = SUPERVISOR_ISSUES.get(
            kind, (humanize(kind), WARNING, ["Open Repairs for details."], REPAIRS_LINK))
        where = app_names.get(ref, ref) if ctx == "addon" else ctx
        if where and where not in ("system", "core"):
            title = f"{title}: {where}"
        action = None
        for sug in suggestions:
            if sug["uuid"] in used or sug["context"] != ctx or sug["reference"] != ref:
                continue
            if sug["type"] in SAFE_SUGGESTIONS and not (sug["type"] == "execute_restart" and ctx != "addon"):
                action = Action("apply_suggestion", sug["uuid"], SUGGESTION_LABELS[sug["type"]])
                used.add(sug["uuid"])
                break
        out.append(Finding(f"sup:issue:{kind}:{ctx}:{ref}", "supervisor", severity, title,
                           steps=tuple(steps), link=link, action=action,
                           facts={"type": kind, "context": ctx, "reference": ref}))
    for sug in suggestions:
        if sug["uuid"] in used or sug["type"] not in SAFE_SUGGESTIONS:
            continue
        if sug["type"] == "execute_restart" and sug["context"] != "addon":
            continue
        where = app_names.get(sug["reference"], sug["reference"]) or sug["context"]
        out.append(Finding(f"sup:suggestion:{sug['type']}:{sug['context']}:{sug['reference']}", "supervisor",
                           WARNING, f"Home Assistant suggests a fix for {where}",
                           action=Action("apply_suggestion", sug["uuid"], SUGGESTION_LABELS[sug["type"]]),
                           facts={"type": sug["type"], "context": sug["context"]}))
    return out


def from_repairs(issues: list[dict]) -> list[Finding]:
    out = []
    for row in issues:
        if row.get("ignored") or row.get("dismissed_version"):
            continue
        severity = CRITICAL if row.get("severity") in ("critical", "error") else WARNING
        domain = humanize(row.get("domain", ""))
        title = f"{domain}: {humanize(row.get('translation_key') or row.get('issue_id', ''))}"
        steps = ["Open Settings -> Repairs and tap this item."]
        if row.get("is_fixable"):
            steps.append("It has a built-in fix: follow the steps Home Assistant shows.")
        if row.get("breaks_in_ha_version"):
            steps.append(f"Fix it before updating Home Assistant to {row['breaks_in_ha_version']}.")
        out.append(Finding(f"repair:{row.get('domain')}:{row.get('issue_id')}", "repairs", severity, title,
                           steps=tuple(steps), link=row.get("learn_more_url") or REPAIRS_LINK,
                           facts={"domain": row.get("domain"), "issue": row.get("issue_id"),
                                  "severity": row.get("severity")}))
    return out


def from_apps(rows: list[tuple[str, str, str, str]]) -> list[Finding]:
    """rows: (slug, name, state, boot) for Apps worth checking."""
    out = []
    for slug, name, state, boot in rows:
        if state == "error" and boot != "auto":
            out.append(Finding(f"app:{slug}", "app", WARNING, f"App stopped with an error: {name}",
                               steps=("It is set to start only by hand, so I never restart it.",
                                      "If you expected it to be running, open its Log tab first."),
                               link=APPS_LINK, facts={"slug": slug, "state": state, "boot": boot}))
        elif state == "error":
            out.append(Finding(f"app:{slug}", "app", CRITICAL, f"App crashed: {name}",
                               steps=("Open the App's Log tab to see why.", "A restart often fixes it."),
                               link=APPS_LINK, action=Action("restart_app", slug, f"restart {name}"),
                               facts={"slug": slug, "state": state}))
        elif state == "stopped" and boot == "auto":
            out.append(Finding(f"app:{slug}", "app", CRITICAL, f"App stopped but should be running: {name}",
                               steps=("If you stopped it on purpose, tap Reject; it won't ask again until it"
                                      " runs and stops again.",),
                               link=APPS_LINK, action=Action("start_app", slug, f"start {name}"),
                               facts={"slug": slug, "state": state}))
    return out


def needs_network_check(entries: list[dict]) -> bool:
    """True when an integration waits for a network announcement (worth one read of what HA hears)."""
    return any(_upnp_not_discovered(e) and not e.get("disabled") for e in entries)


def _upnp_not_discovered(e: dict) -> bool:
    return e.get("domain") == "upnp" and e.get("state") == "setup_retry" and \
        UPNP_NOT_DISCOVERED in (e.get("reason") or "").lower()


def diagnose_upnp(reason: str, heard: list[dict] | None) -> str:
    """Why Home Assistant can't find the UPnP router, from what it hears on the network right now.

    ``heard`` is ``None`` when it could not be read. Home Assistant itself re-matches a router whose
    identity changed (same MAC or address, same IGD version) and updates the entry, so an IGD heard
    under another identity or IGD version is one it could not match: it waits under "Discovered".
    """
    if heard is None:
        return "UPNP_UNCHECKED"
    usn = reason.split(":", 1)[1].strip() if ":" in reason else ""
    udn, _, st = usn.partition("::")
    igd = [r for r in heard if (r.get("st") or "").startswith(IGD_ST)]
    if any(r.get("udn") == udn and r.get("st") == st for r in igd):
        return "UPNP_HEARD_NOW"
    if igd:
        return "UPNP_ROUTER_NEW_IDENTITY"
    if any(r.get("udn") or r.get("st") for r in heard):
        return "UPNP_ROUTER_SILENT"
    return "UPNP_NOTHING_HEARD"


def network_summary(heard: list[dict] | None) -> dict:
    """Counts only (no names, no identities) for the tracking-issue report."""
    if heard is None:
        return {"network_check": "unreadable"}
    igd = [r for r in heard if (r.get("st") or "").startswith(IGD_ST)]
    return {"devices_heard": len({r.get("udn") for r in heard if r.get("udn")}),
            "igd_heard": len({r.get("udn") for r in igd if r.get("udn")}),
            "igd_versions": sorted({r["st"].rsplit(":", 1)[-1] for r in igd})}


def from_entries(entries: list[dict], first_seen: dict[str, float], now: float,
                 power_cycle_entity: str = "", heard: list[dict] | None = None) -> list[Finding]:
    out = []
    for e in entries:
        state = e.get("state")
        if e.get("disabled") or state not in ENTRY_PROBLEM_STATES:
            continue
        key = f"entry:{e['entry_id']}"
        severity = ENTRY_PROBLEM_STATES[state]
        if state == "setup_retry" and now - first_seen.get(key, now) >= RETRY_CRITICAL_SECONDS:
            severity = CRITICAL
        name = f"{humanize(e.get('domain', ''))} ({e.get('title') or 'no name'})"
        what = {"setup_error": "failed to start", "migration_error": "failed to upgrade its settings",
                "failed_unload": "failed to stop cleanly", "setup_retry": "can't connect and keeps retrying"}[state]
        steps = ["Check the device or service is on and reachable."]
        action = None
        if state in ("setup_error", "setup_retry"):
            action = Action("reload_entry", e["entry_id"], f"reload {name}")
        if state == "migration_error":
            steps = ["This usually needs the integration removed and added again; I'll look at the details."]
        detail = (f"Reason: {e['reason']}",) if e.get("reason") else ()
        title = f"Integration {what}: {name}"
        for (domain, needle), (known_title, known_steps) in KNOWN_ENTRY_FIXES.items():
            if e.get("domain") == domain and needle in (e.get("reason") or "").lower():
                title, steps, action = f"{known_title} ({name})", list(known_steps), None
                label = POWER_CYCLE_FIXES.get((domain, needle))
                if label and power_cycle_entity and \
                        now - first_seen.get(key, now) >= POWER_CYCLE_AFTER_SECONDS:
                    action = Action("power_cycle", power_cycle_entity, label)
        facts = {"domain": e.get("domain"), "state": state}
        if _upnp_not_discovered(e):
            diagnosis = diagnose_upnp(e.get("reason") or "", heard)
            known_title, known_steps, keep_reload = UPNP_DIAGNOSES[diagnosis]
            title, steps = f"{known_title} ({name})", list(known_steps)
            action = action if keep_reload else None
            facts |= {"diagnosis": diagnosis} | network_summary(heard)
        out.append(Finding(key, "integration", severity, title, detail=detail,
                           steps=tuple(steps), link=INTEGRATIONS_LINK, action=action, facts=facts))
    return out


def from_log(rows: list[dict]) -> list[Finding]:
    out = []
    for row in rows:
        name = row.get("name") or ""
        parts = name.split(".")
        who = parts[2] if len(parts) > 2 and parts[:2] == ["homeassistant", "components"] else \
            (parts[1] if len(parts) > 1 and parts[0] == "custom_components" else name)
        detail = [row.get("message", "")]
        if row.get("exception"):
            detail.append(row["exception"])
        out.append(Finding(f"log:{name}:{row.get('source')}", "log", WARNING,
                           f"Error from {humanize(who)} (seen {row.get('count', 1)}x)",
                           detail=tuple(detail),
                           steps=("If it keeps happening, I'll review the details and suggest a fix.",),
                           link=LOGS_LINK, announce_clear=False,
                           facts={"logger": name, "source": row.get("source"), "count": row.get("count")}))
    return out


def from_disk(disk: tuple[float, float] | None) -> list[Finding]:
    if not disk:
        return []
    total, free = disk
    frac = free / total
    for severity, low in ((CRITICAL, free < DISK_CRITICAL_GB),
                          (WARNING, free < DISK_WARNING[0] or frac < DISK_WARNING[1])):
        if low:
            return [Finding(f"disk:{severity}", "disk", severity,
                            f"Low disk space: {free:.1f} GB free of {total:.0f} GB",
                            steps=("Delete old backups you no longer need (Settings -> System -> Backups).",
                                   "Remove Apps you don't use.",
                                   "Keep less history: lower the recorder's purge_keep_days."),
                            link=BACKUPS_LINK, facts={"free_gb": round(free, 1), "total_gb": round(total)})]
    return []


def _parse(date: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(date.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def from_backups(backups: list[tuple[str, str]], now: float) -> list[Finding]:
    full = [d for t, d in backups if t == "full"]
    dates = [dt for dt in (_parse(d) for d in full) if dt]
    newest = max(dates).timestamp() if dates else None
    if newest is not None and now - newest < FULL_BACKUP_MAX_DAYS * 86400:
        return []
    age = f"{int((now - newest) // 86400)} days ago" if newest else "never"
    return [Finding("backup:full", "backup", WARNING, f"No recent full backup (last one: {age})",
                    steps=("Settings -> System -> Backups -> Backup now (full backup).",
                           "Better: turn on automatic backups there, ideally with a copy off the Pi."),
                    link=BACKUPS_LINK, facts={"last_full": age})]
