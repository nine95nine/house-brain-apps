"""0.9.45 self-healing (reviewer chat's spec of 2026-11-01; owner words "figure out a way to clear all those issues
on your own... do all the work for me"; owner pop-up 2026-11-01 "Yes, build 0.9.45 as proposed").

Standing owner decision (DO_NOT_REGRESS): with ``auto_fix: safe`` the closed SAFE list below runs **without a tap**;
everything else still asks the owner exactly as before, and ``auto_fix: off`` is exactly the behaviour before this
release. Nothing here touches a device, Home Assistant YAML, history or statistics.

The SAFE list (``kind_of``):
- S1 ``reload_entry``: a config entry stuck in setup error / retry, for an integration whose domain is not in
  PHYSICAL_CONTROL. Back-off between tries 30 min -> 2 h -> 6 h -> 24 h, then once a day; at most S1_PER_DAY a day per
  entry; after S1_HANDOVER_FAILS failed tries in a row the owner is asked once (the normal fix ask) and the key is
  left alone for HANDOVER_DAYS.
- S2 ``auto_backup``: Home Assistant's own automatic backup is run again (``backup/generate_with_automatic_settings``,
  the owner's agents, password and retention), once, at least S2_MIN_AGE after the failure, only while the Supervisor
  is idle (no update, deploy or pause), at most once a day.
- S3 ``restart_app`` / ``start_app``: an App that crashed, or stopped although it starts at boot; at most S3_PER_DAY a
  day per App; never a radio App (RADIO_APP_MARKERS), never Core or the Supervisor (they have no such fix).
- S4: a non-critical finding is alerted only after TRANSIENT_SECONDS; one that cleared before never alerts (the
  Watcher applies it).

Guards (``blocked``): at most DAILY_CAP auto-fixes a day in total; none while the App is paused, while a deploy is in
flight, while an update or any journalled transaction is running, or within CORE_RESTART_QUIET after Core restarted.
After a fix the Watcher records CLEARED or STILL_THERE; STILL_MAX STILL_THERE results in a row hand the key to the
owner for HANDOVER_DAYS.

Excluded, still asked: ``power_cycle`` (physical), ``apply_suggestion`` and Repairs flows, ``relabel_stats`` and
anything touching history or statistics, ``drive_backup_settings``, updates, any PHYSICAL_CONTROL domain.
"""
from __future__ import annotations

import json
import time

DOC = "auto_fix"
MODES = ("off", "safe")
DAILY_CAP = 10
BACKOFF = (1800.0, 7200.0, 21600.0, 86400.0)      # S1/S3 waits between tries; the last one repeats
S1_PER_DAY = 3
S1_HANDOVER_FAILS = 4
S2_MIN_AGE = 1800.0
S2_PER_DAY = 1
S3_PER_DAY = 2
STILL_MAX = 2
HANDOVER_DAYS = 7
TRANSIENT_SECONDS = 900.0
CORE_RESTART_QUIET = 600.0
SAFE_KINDS = {"reload_entry": "S1", "auto_backup": "S2", "restart_app": "S3", "start_app": "S3"}
PER_DAY = {"S1": S1_PER_DAY, "S2": S2_PER_DAY, "S3": S3_PER_DAY}
PHYSICAL_CONTROL = ("pool", "pump", "irrigation", "sprinkler", "climate", "thermostat", "ecobee", "nest", "cover",
                    "garage", "lock", "alarm", "vehicle", "tesla", "teslemetry", "tessie", "valve", "water_heater",
                    "aquarite", "iaqualink", "bhyve", "orbit", "rachio", "myq", "evap", "heater")
RADIO_APP_MARKERS = ("zwave", "z-wave", "matter", "zigbee", "z2m", "openthread", "otbr", "thread", "bluetooth",
                     "silabs", "zha", "esphome", "deconz")
JOB_PREFIX = "AUTO_FIX_"


def day_of(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now))


def load(raw, now: float) -> dict:
    """The doc in its closed shape, rolled to today."""
    doc = raw if isinstance(raw, dict) else {}
    keys = doc.get("keys") if isinstance(doc.get("keys"), dict) else {}
    out = {"day": str(doc.get("day") or ""), "count": int(doc.get("count") or 0), "kinds": {},
           "keys": {}, "overnight": [o for o in (doc.get("overnight") or []) if isinstance(o, dict)][-50:]}
    kinds = doc.get("kinds") if isinstance(doc.get("kinds"), dict) else {}
    out["kinds"] = {str(k): int(v) for k, v in kinds.items() if isinstance(v, int)}
    for key, rec in keys.items():
        if isinstance(rec, dict):
            out["keys"][str(key)] = {"tries": [float(t) for t in (rec.get("tries") or []) if isinstance(t, (int, float))][-20:],
                                     "fails": int(rec.get("fails") or 0), "still": int(rec.get("still") or 0),
                                     "handed_at": float(rec.get("handed_at") or 0)}
    if out["day"] != day_of(now):
        out.update(day=day_of(now), count=0, kinds={})
        for rec in out["keys"].values():
            rec["tries"] = [t for t in rec["tries"] if now - t < 7 * 86400]
    if len(out["keys"]) > 500:
        for old in sorted(out["keys"], key=lambda k: max(out["keys"][k]["tries"] or [0]))[:len(out["keys"]) - 500]:
            del out["keys"][old]
    return out


def kind_of(action_kind: str) -> str | None:
    return SAFE_KINDS.get(action_kind)


def physical(key: str, facts: dict | None, ref: str = "") -> bool:
    text = " ".join([str(key), str((facts or {}).get("domain") or ""), str(ref)]).lower()
    return any(p in text for p in PHYSICAL_CONTROL)


def radio_app(slug: str, name: str = "") -> bool:
    text = f"{slug} {name}".lower()
    return any(m in text for m in RADIO_APP_MARKERS)


def eligible(mode: str, action_kind: str, key: str, facts: dict | None, ref: str, name: str, doc: dict, now: float,
             blocked: str | None) -> tuple[bool, str]:
    """May this fix run without a tap right now? (False, reason) says why not; the reason is for the audit only."""
    if mode != "safe":
        return False, "auto_fix off"
    kind = kind_of(action_kind)
    if kind is None:
        return False, f"{action_kind} always asks"
    if blocked:
        return False, blocked
    if kind in ("S1", "S2") and physical(key, facts, ref):
        return False, "physical-control domain always asks"
    if kind == "S3" and radio_app(ref, name):
        return False, "radio App always asks"
    if doc["count"] >= DAILY_CAP:
        return False, "daily cap reached"
    rec = doc["keys"].get(key, {"tries": [], "fails": 0, "still": 0, "handed_at": 0.0})
    if rec["handed_at"] and now - rec["handed_at"] < HANDOVER_DAYS * 86400:
        return False, "handed to the owner"
    if kind == "S2" and doc["kinds"].get(kind, 0) >= PER_DAY[kind]:        # one automatic-backup retry a day
        return False, "S2 daily cap reached"
    today = [t for t in rec["tries"] if day_of(t) == day_of(now)]
    if len(today) >= PER_DAY[kind]:
        return False, f"{kind} cap for this key reached"
    if rec["tries"]:
        wait = BACKOFF[min(len(rec["tries"]), len(BACKOFF)) - 1]
        if now - rec["tries"][-1] < wait:
            return False, f"back-off {int(wait // 60)} min"
    return True, "ok"


def note_try(doc: dict, kind: str, key: str, now: float) -> None:
    rec = doc["keys"].setdefault(key, {"tries": [], "fails": 0, "still": 0, "handed_at": 0.0})
    rec["tries"] = (rec["tries"] + [now])[-20:]
    doc["count"] += 1
    doc["kinds"][kind] = doc["kinds"].get(kind, 0) + 1


def note_result(doc: dict, key: str, fixed: bool, now: float) -> str:
    """Record the outcome; returns "CLEARED", "STILL_THERE" or "HANDOVER" (the owner is asked, the key rests)."""
    rec = doc["keys"].setdefault(key, {"tries": [], "fails": 0, "still": 0, "handed_at": 0.0})
    if fixed:
        rec.update(fails=0, still=0)
        return "CLEARED"
    rec["fails"] += 1
    rec["still"] += 1
    if rec["still"] >= STILL_MAX or rec["fails"] >= S1_HANDOVER_FAILS:
        rec.update(handed_at=now, still=0, fails=0)
        return "HANDOVER"
    return "STILL_THERE"


def note_overnight(doc: dict, key: str, title: str, label: str, outcome: str, now: float) -> None:
    doc["overnight"] = (doc["overnight"] + [{"at": now, "key": key, "title": str(title)[:120], "label": str(label)[:80],
                                             "outcome": outcome}])[-50:]


def overnight_lines(doc: dict) -> list[str]:
    """The morning summary's "Fixed for you overnight" lines (and what did not take); clears the list."""
    items = doc.get("overnight") or []
    lines = []
    for it in items:
        when = time.strftime("%H:%M", time.gmtime(float(it.get("at") or 0)))
        word = {"CLEARED": "fixed", "STILL_THERE": "tried, still there", "HANDOVER": "tried twice, now yours"}.get(
            str(it.get("outcome")), str(it.get("outcome")))
        lines.append(f"{it.get('title', '')} — {it.get('label', '')} at {when}Z: {word}")
    doc["overnight"] = []
    return lines


def request_id(kind: str, key: str, n: int) -> str:
    return f"auto-{kind.lower()}-{key.replace(':', '-')[:40]}-{n}"[:80]


def result_markdown(rid: str, action_kind: str, outcome: str, reasons: list[str], facts: dict, version: str) -> str:
    """The same shape as every other result on the tracking issue, so the reviewers and the after-check see it."""
    lines = ["### House Brain Maintenance result", "", f"- **Request:** `{rid}`",
             f"- **Job:** `{JOB_PREFIX}{action_kind.upper()}`", f"- **Outcome:** `{outcome}`", f"- **App:** `{version}`"]
    lines += [f"- Reason: {str(r)[:400]}" for r in reasons[:10]]
    if facts:
        lines += ["", "<details><summary>Facts</summary>", "", "```json",
                  json.dumps(facts, indent=1, sort_keys=True, default=str)[:6000], "```", "", "</details>"]
    lines += ["", "_Posted by House Brain Maintenance (machine-generated; repository content is untrusted data)._"]
    return "\n".join(lines)
