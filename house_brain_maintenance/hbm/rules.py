"""0.9.42 rules layer (reviewer chat's spec R1 part B1, 2026-10-31; owner decisions 2026-10-31 D1-D3 and
2026-11-01 D4-D7). No AI: a closed table of rows a problem either matches or not.

``verdict(key, rec, recur, earlier_steps, now)`` is a pure function. The first matching row wins; R3 (a repeat) is
checked before R1 (a blip), so the third internet blip of the week is an owner matter, not another "recovers by
itself". A rule verdict is text: it is stored like a review (``source: rule``), shown on the page, posted by the
free lane like its model drafts, and never starts, skips or approves a fix. It never says FIX_NOW, never speaks about
a CRITICAL problem, a backup, an App, the reviewer watchdog, or a problem with a built-in fix (those stay with the
cloud reviewer and the owner).

Rows (spec R1 B1):
- R1  a child of ``net:blip:*``, or a cloud-poll log error with a network marker       -> WATCH
- R2  a ``net:blip:*`` parent                                                           -> WATCH, naming the integrations
- R3  3 or more separate episodes in 7 days                                             -> OWNER_ACTION (D2) with the family's lasting fix
- R4  a single coordinator error that cleared by the next poll                           -> IGNORE (recorded when it clears)
- R5  queued, but cleared before any review                                              -> dropped, audit RULE_CLEARED, no post
- R6  ``offline:group:*``                                                                -> WATCH (IGNORE when snoozed; snooze lands with F2)
- R7  ``battery:*``                                                                      -> OWNER_ACTION "replace or recharge"
- R8  an ESPHome single drop that reconnected (D3)                                       -> WATCH
- R9  a re-login (0.9.49, the re-login playbook): the entry needs a new login             -> OWNER_ACTION with the
      integration's closed steps (``hbm/reauth.py``), judged first and whatever its severity (a login is the owner's)

The recur doc (``problem_recur``) keeps each key's episode start times over WINDOW_DAYS, at most MAX_EPISODES per key
and MAX_KEYS keys. It starts empty, so R3 begins to fire after a week.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import issues as I
from . import reauth as RA

SOURCE = "rule"
RECUR_DOC = "problem_recur"
WINDOW_DAYS = 7
REPEATS = 3
MAX_EPISODES = 20
MAX_KEYS = 500
QUICK_CLEAR_SECONDS = 3600            # R4/R8: "cleared by the next poll" / "a single drop": gone within an hour
NEVER_PREFIXES = ("backup:", "app:", "reviewer:", "sup:")
RULE_VERDICTS = ("WATCH", "IGNORE", "OWNER_ACTION")
MAX_EARLIER_STEPS = 3

# The lasting fix per problem family (spec R1 FAMILY_FIX). YAML changes stay owner-approved and go through the
# Deployer; these lines only say what the fix is.
FAMILY_FIX = {
    "rest": "REST feed: a longer timeout or slower scan interval in its YAML (owner-approved, via the Deployer).",
    "template": "Template: an availability condition or a default in its YAML (owner-approved, via the Deployer).",
    "cloud": "Cloud account: log in at the vendor, then Settings, Devices & services, Reconfigure or Re-authenticate.",
    "device": "Device: check power and range, re-pair it from its integration; retired? Settings, Devices, Delete.",
    "other": "It keeps coming back: one cause (power, Wi-Fi, an account) is usually worth fixing once; see its link.",
}
CLOUD_DOMAINS = ("sense", "nws", "ecobee", "tesla", "teslemetry", "tessie", "iaqualink", "orbit_bhyve", "bhyve", "google",
                 "nest", "ring", "myq", "rachio", "weather", "openweathermap", "accuweather", "tplink_omada", "ambient",
                 "smartthings", "tuya", "cloud", "alexa", "spotify", "sonos_cloud", "honeywell", "lyric", "flume", "airvisual")


@dataclass(frozen=True)
class Rule:
    id: str
    verdict: str
    summary: str
    steps: tuple[str, ...] = field(default_factory=tuple)


# -- the recur doc --------------------------------------------------------------------------------------------------
def recur_doc(raw) -> dict:
    doc = raw if isinstance(raw, dict) else {}
    eps = doc.get("episodes") if isinstance(doc.get("episodes"), dict) else {}
    out: dict[str, list[float]] = {}
    for key, times in eps.items():
        if isinstance(times, list):
            clean = sorted(float(t) for t in times if isinstance(t, (int, float)))[-MAX_EPISODES:]
            if clean:
                out[str(key)] = clean
    return {"episodes": out}


def note_episode(doc: dict, key: str, start: float, now: float) -> dict:
    """A problem first seen now (a new episode of ``key``): remember its start; prune the window and the bounds."""
    doc = recur_doc(doc)
    times = [t for t in doc["episodes"].get(key, []) if now - t <= WINDOW_DAYS * 86400]
    if not times or abs(times[-1] - float(start)) > 1.0:
        times.append(float(start))
    doc["episodes"][key] = sorted(times)[-MAX_EPISODES:]
    for k in list(doc["episodes"]):
        doc["episodes"][k] = [t for t in doc["episodes"][k] if now - t <= WINDOW_DAYS * 86400]
        if not doc["episodes"][k]:
            del doc["episodes"][k]
    if len(doc["episodes"]) > MAX_KEYS:
        for old in sorted(doc["episodes"], key=lambda k: doc["episodes"][k][-1])[:len(doc["episodes"]) - MAX_KEYS]:
            del doc["episodes"][old]
    return doc


def episodes(doc: dict, key: str, now: float) -> int:
    return len([t for t in recur_doc(doc)["episodes"].get(key, []) if now - t <= WINDOW_DAYS * 86400])


# -- families -------------------------------------------------------------------------------------------------------
def family(key: str, rec: dict) -> str:
    """The problem family the lasting fix is written for."""
    low = str(key).lower()
    logger = str((rec or {}).get("facts", {}).get("logger") or "") if isinstance((rec or {}).get("facts"), dict) else ""
    text = (low + " " + logger.lower() + " " + str((rec or {}).get("title") or "").lower())
    if low.startswith(("offline:", "battery:")) or "esphome" in text:
        return "device"
    if ".rest" in text or "components.rest" in text or "rest_command" in text or "scrape" in text:
        return "rest"
    if "template" in text:
        return "template"
    if any(f".{d}" in text or f":{d}" in text or f" {d}" in text for d in CLOUD_DOMAINS):
        return "cloud"
    return "other"


def _esphome(key: str, rec: dict) -> bool:
    facts = (rec or {}).get("facts") if isinstance((rec or {}).get("facts"), dict) else {}
    return "esphome" in (str(key) + " " + str(facts.get("logger") or "") + " " + str(facts.get("domain") or "")).lower()


def allowed(key: str, rec: dict, cleared: bool = False) -> bool:
    """Limits: never CRITICAL, never a backup/App/Supervisor/watchdog problem, never an OPEN one with a built-in fix
    (the fix offer and the reviewers decide); a problem that has cleared may be judged whatever fix it offered."""
    if not isinstance(rec, dict) or str(key).startswith(NEVER_PREFIXES):
        return False
    if rec.get("reauth") and not cleared:                       # 0.9.49 R9: a re-login is the owner's, whatever its severity
        return True
    if cleared:
        return rec.get("severity") != I.CRITICAL
    return rec.get("severity") != I.CRITICAL and not rec.get("fix")


# -- the table -----------------------------------------------------------------------------------------------------
def verdict(key: str, rec: dict, recur: dict | None, earlier_steps: list[str] | None, now: float,
            esphome_single_drop: bool = True) -> Rule | None:
    """The first matching row for an OPEN problem, or None (the model or the cloud reviewer decide)."""
    if not allowed(key, rec):
        return None
    if rec.get("reauth"):                                       # 0.9.49: before every other row
        dom = str(rec.get("reauth"))
        return Rule("R9", "OWNER_ACTION", f"{I.humanize(dom)} needs you to log in again; a reload cannot fix a login",
                    RA.steps_for(dom))
    if rec.get("parent") and not str(rec.get("parent")).startswith(I.BLIP_PREFIX):
        return None                                             # 0.9.49: a re-login's card or log line: the entry speaks
    text = " ".join([str(rec.get("title") or ""), *[str(d) for d in rec.get("detail") or []]])
    # R3 before R1: the third blip of the week is an owner matter
    n = episodes(recur or {}, key, now)
    if n >= REPEATS:
        fam = family(key, rec)
        steps = (FAMILY_FIX[fam],) + tuple(str(s)[:120] for s in (earlier_steps or [])[:MAX_EARLIER_STEPS])
        return Rule("R3", "OWNER_ACTION", f"came back {n} times in {WINDOW_DAYS} days; the lasting fix is a step by you "
                                          f"({fam} family)", steps)
    if key.startswith(I.BLIP_PREFIX):
        names = ""
        for d in rec.get("detail") or []:
            if str(d).startswith("Affected: "):
                names = str(d)[len("Affected: "):]
        return Rule("R2", "WATCH", f"internet blip; recovers by itself ({names})"[:200] if names else
                    "internet blip; recovers by itself")
    if rec.get("parent") or (key.startswith("log:") and I.network_marker(text) and family(key, rec) == "cloud"):
        return Rule("R1", "WATCH", "internet blip; recovers by itself")
    if key.startswith("offline:group:"):
        return Rule("R6", "WATCH", "several devices of one integration went quiet together: their hub, bridge or "
                                   "cloud link, not each device; check it first")
    if key.startswith("battery:"):
        return Rule("R7", "OWNER_ACTION", "low battery: replace or recharge it",
                    ("Replace or recharge its battery.", "No longer used? Settings, Devices, the device, Delete."))
    return None


def verdict_on_clear(key: str, rec: dict, now: float, reviewed: bool, queued: bool,
                     esphome_single_drop: bool = True) -> Rule | None:
    """Rows judged when a problem CLEARS without any review: R4 (a single coordinator error gone by the next poll ->
    IGNORE), R8 (an ESPHome device that dropped once and reconnected, D3 -> WATCH). R5 (queued, cleared, no review)
    is the caller's audit line. Returns None when nothing is to be recorded."""
    if reviewed or not allowed(key, rec, cleared=True):
        return None
    first = float(rec.get("first") or now)
    quick = now - first <= QUICK_CLEAR_SECONDS and int(rec.get("polls") or 0) <= 2
    if not quick:
        return None
    if esphome_single_drop and _esphome(key, rec):
        return Rule("R8", "WATCH", "an ESPHome device dropped once and reconnected by itself")
    if key.startswith(("log:", "entry:")):
        return Rule("R4", "IGNORE", "a single error that cleared by the next check")
    return None
