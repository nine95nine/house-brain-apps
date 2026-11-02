"""0.9.49 re-login playbook (the AI reviewer chat's spec of 2026-11-02 after the Tuya re-login; owner "how can I avoid
this smart life app/Tuya issue in the future", pop-ups "Let's actually do both" and "Yes, build 0.9.49 as proposed").

One problem, not three: an integration that needs a new login shows up as its config entry (``entry:<id>`` in
``setup_error`` for an auth reason, or with a pending re-authentication flow), as Home Assistant's own Repairs card
for it (``config_entry_reauth``) and often as its coordinator's log error (``invalid auth``, ``401``, ...). ``family``
links the card and the log errors to the entry (they become children, like a blip's children: never alerted, queued
or counted), so the owner sees ONE problem with the steps for that integration.

The steps are a closed table (``STEPS``, each line at most STEP_CHARS characters, iPhone friendly), written from the
integration documentation; ``GENERIC`` is the fallback. The rule R9 (``hbm/rules.py``) gives the family OWNER_ACTION
with those steps within one check, so no model has to research a login.

The phone: one push per re-login episode, at once, with the steps; never more than one a day per integration
(``may_push``); quiet hours hold it unless the integration controls equipment (the Watcher raises those to CRITICAL).
The self-healing never reloads an auth failure (``autofix.eligible`` refuses S1 for it): a reload cannot fix a login
and burns the vendor's limits. Episodes are counted per integration over WINDOW_DAYS; at REPEAT_LINE_AT or more the
morning summary says so and points at the cloud-to-local audit on the tracking issue.
"""
from __future__ import annotations

import re

DOC = "reauth"
WINDOW_DAYS = 90
MAX_EPISODES = 30
MAX_DOMAINS = 100
REPEAT_LINE_AT = 2
PUSH_GAP_SECONDS = 86400.0
STEP_CHARS = 120
REAUTH_TRANSLATION_KEY = "config_entry_reauth"
RE_ENTRY_ID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")           # a Home Assistant config-entry id (ULID)
# Closed auth markers (the spec's list, plus Home Assistant's own exception and error names), matched case-insensitively.
AUTH_MARKERS = ("invalid auth", "invalid_auth", "unauthorized", "unauthorised", "token expired",
                "expired token", "re-authenticate", "reauthenticate", "reauth", "configentryauthfailed",
                "authentication failed", "auth failed", "invalid credentials", "invalid_credentials", "login failed",
                "invalid api key", "invalid_api_key", "api key invalid", "access denied", "not authorized",
                "forbidden (403)", "token_expired", "refresh token")
GENERIC = ("Home Assistant: Settings, Devices & services, the integration, Reconfigure, then sign in with the vendor account.",
           "Usually after a password change, an expired login or a new account security setting.")
STEPS: dict[str, tuple[str, ...]] = {
    "tuya": ("Smart Life app: Me, gear (settings), Account and Security, then note the User Code.",
             "Home Assistant: Settings, Devices & services, Tuya, Reconfigure, enter the User Code.",
             "Scan the QR code Home Assistant shows with the Smart Life app's scan icon (top right of Home)."),
    "govee": ("Govee Home app: Profile, settings (gear), Apply for API Key; the new key arrives by e-mail.",
              "Home Assistant: Settings, Devices & services, Govee, Reconfigure, paste the new API key."),
    "ondilo_ico": ("Home Assistant: Settings, Devices & services, Ondilo ICO, Reconfigure.",
                   "Sign in with the Ondilo account (the ICO app login) when the page asks."),
    "smartthings": ("Home Assistant: Settings, Devices & services, SmartThings, Reconfigure.",
                    "Sign in with the Samsung account when the page asks and allow the same locations again."),
    "ecobee": ("Home Assistant: Settings, Devices & services, ecobee, Reconfigure.",
               "Sign in at ecobee.com when the page asks and enter the PIN Home Assistant shows under My Apps."),
    "sense": ("Home Assistant: Settings, Devices & services, Sense, Reconfigure.",
              "Sign in with the Sense account; if it asks for a code, use the one from the Sense app's e-mail."),
}
STEPS["goveelife"] = STEPS["govee"]
STEPS["govee_light_local"] = GENERIC


RE_401 = re.compile(r"(?<![0-9.])401(?![0-9])")                # HTTP 401 as a number of its own, not "4010 rows"


def is_auth_text(text: str) -> bool:
    """Does this text say "the login failed"? A closed list; never a model."""
    low = f" {str(text or '').lower()} "
    return any(m in low for m in AUTH_MARKERS) or bool(RE_401.search(low))


def steps_for(domain: str) -> tuple[str, ...]:
    steps = STEPS.get(str(domain or "").lower(), GENERIC)
    return tuple(str(s)[:STEP_CHARS] for s in steps)


def entry_id_in(issue_id: str) -> str | None:
    m = RE_ENTRY_ID.search(str(issue_id or ""))
    return m.group(0) if m else None


def family(findings: list) -> dict[str, str]:
    """Child key -> the re-login entry's key: Home Assistant's ``config_entry_reauth`` card for that entry, and log
    errors with an auth marker from that integration while its entry needs a login."""
    parents: dict[str, str] = {}                                 # entry id -> key
    domains: dict[str, str] = {}                                 # domain -> key
    for f in findings:
        facts = f.facts or {}
        if facts.get("reauth") and f.key.startswith(("entry:", "reauth:")):
            entry_id = f.key.split(":", 1)[1]
            parents.setdefault(entry_id, f.key)
            if facts.get("domain"):
                domains.setdefault(str(facts["domain"]).lower(), f.key)
    if not parents:
        return {}
    out: dict[str, str] = {}
    for f in findings:
        facts = f.facts or {}
        if f.key in parents.values():
            continue
        if facts.get("reauth_repair"):
            parent = parents.get(str(facts.get("entry_id") or ""))
            if parent:
                out[f.key] = parent
        elif f.key.startswith("log:") and facts.get("auth"):
            from . import issues as I
            parent = domains.get(I.integration_of(str(facts.get("logger") or "")).lower())
            if parent:
                out[f.key] = parent
    return out


# -- the doc -----------------------------------------------------------------------------------------------------
def load(raw) -> dict:
    doc = raw if isinstance(raw, dict) else {}
    eps = doc.get("episodes") if isinstance(doc.get("episodes"), dict) else {}
    pushed = doc.get("pushed") if isinstance(doc.get("pushed"), dict) else {}
    out = {"episodes": {}, "pushed": {}}
    for dom, times in list(eps.items())[:MAX_DOMAINS]:
        if isinstance(times, list):
            clean = sorted(float(t) for t in times if isinstance(t, (int, float)))[-MAX_EPISODES:]
            if clean:
                out["episodes"][str(dom)] = clean
    out["pushed"] = {str(d): float(t) for d, t in list(pushed.items())[:MAX_DOMAINS] if isinstance(t, (int, float))}
    return out


def note_episode(doc: dict, domain: str, start: float, now: float) -> None:
    dom = str(domain or "").lower()
    if not dom:
        return
    times = [t for t in doc["episodes"].get(dom, []) if now - t <= WINDOW_DAYS * 86400]
    if not times or abs(times[-1] - float(start)) > 1.0:
        times.append(float(start))
    doc["episodes"][dom] = sorted(times)[-MAX_EPISODES:]
    if len(doc["episodes"]) > MAX_DOMAINS:
        for old in sorted(doc["episodes"], key=lambda d: doc["episodes"][d][-1])[:len(doc["episodes"]) - MAX_DOMAINS]:
            del doc["episodes"][old]


def episodes(doc: dict, domain: str, now: float) -> int:
    return len([t for t in doc["episodes"].get(str(domain or "").lower(), []) if now - t <= WINDOW_DAYS * 86400])


def may_push(doc: dict, domain: str, now: float) -> bool:
    """One push per integration per day, whatever the number of episodes or checks."""
    return now - float(doc["pushed"].get(str(domain or "").lower(), 0.0)) >= PUSH_GAP_SECONDS


def note_push(doc: dict, domain: str, now: float) -> None:
    doc["pushed"][str(domain or "").lower()] = float(now)
    if len(doc["pushed"]) > MAX_DOMAINS:
        for old in sorted(doc["pushed"], key=doc["pushed"].get)[:len(doc["pushed"]) - MAX_DOMAINS]:
            del doc["pushed"][old]


def repeat_lines(doc: dict, now: float) -> list[str]:
    """The morning summary's lines for integrations that needed a re-login REPEAT_LINE_AT or more times in the window."""
    from . import issues as I
    out = []
    for dom in sorted(doc["episodes"]):
        n = episodes(doc, dom, now)
        if n >= REPEAT_LINE_AT:
            out.append(f"{I.humanize(dom)} has needed a re-login {n} times in {WINDOW_DAYS} days; a local option may "
                       f"stop this (see the cloud-to-local audit on #454).")
    return out
