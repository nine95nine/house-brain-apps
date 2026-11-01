"""0.9.45 retired devices (owner 2026-11-01: "Hide them for good, because I'm assuming they're all broken and they're
actually at another location, but leave something behind so that way if one day I do decide to fix them or
reactivate them, I have a path forward"; pop-up "Yes, as proposed").

The ``retired_devices`` doc keeps, per device: its stable key (0.9.41 ``devicekeys``), name, integration, the date it
was retired and the owner's note. A retired device is never listed, queued, alerted or counted by this App; nothing
in Home Assistant is deleted or disabled. The path back: when a retired device reports online again, ONE morning
line says so and the page offers **Reactivate** (an ``UNRETIRE`` decision); the option ``unretire`` (names) does the
same at the next sweep. ``seed()`` lists every B-hyve device offline SEED_DAYS or more; the owner confirmed the first
list by pop-up on 2026-11-01, so the seeding happens on the first sweep after install.
"""
from __future__ import annotations

import time

DOC = "retired_devices"
SEED_INTEGRATIONS = ("orbit_bhyve", "bhyve")
SEED_DAYS = 14
MAX_RETIRED = 200
STAGE = "UNRETIRE"
DEFAULT_NOTE = "assumed broken or at another location (owner 2026-11-01)"


def load(raw) -> dict:
    doc = raw if isinstance(raw, dict) else {}
    devices = doc.get("devices") if isinstance(doc.get("devices"), dict) else {}
    out = {"devices": {}, "seeded": bool(doc.get("seeded")), "back": {}, "declined": {}}
    for key, rec in list(devices.items())[:MAX_RETIRED]:
        if isinstance(rec, dict) and isinstance(key, str):
            out["devices"][key] = {"name": str(rec.get("name") or "")[:80], "integration": str(rec.get("integration") or "")[:64],
                                   "retired_at": str(rec.get("retired_at") or "")[:10], "note": str(rec.get("note") or "")[:200],
                                   "device_id": str(rec.get("device_id") or "")[:64]}
    back = doc.get("back") if isinstance(doc.get("back"), dict) else {}
    out["back"] = {str(k): float(v) for k, v in back.items() if isinstance(v, (int, float)) and k in out["devices"]}
    declined = doc.get("declined") if isinstance(doc.get("declined"), dict) else {}
    out["declined"] = {str(k): float(v) for k, v in declined.items() if isinstance(v, (int, float)) and k in out["devices"]}
    return out


def retire(doc: dict, key: str, name: str, integration: str, device_id: str, now: float, note: str = DEFAULT_NOTE) -> bool:
    if key in doc["devices"] or len(doc["devices"]) >= MAX_RETIRED:
        return False
    doc["devices"][key] = {"name": str(name)[:80], "integration": str(integration)[:64],
                           "retired_at": time.strftime("%Y-%m-%d", time.gmtime(now)), "note": str(note)[:200],
                           "device_id": str(device_id)[:64]}
    return True


def unretire(doc: dict, key: str) -> dict | None:
    doc["back"].pop(key, None)
    doc["declined"].pop(key, None)
    return doc["devices"].pop(key, None)


def decline(doc: dict, key: str, now: float) -> None:
    """The owner declined to reactivate: it stays retired and is not asked again while it stays online."""
    doc["back"].pop(key, None)
    doc["declined"][key] = now


def by_name(doc: dict, names) -> list[str]:
    """Keys of retired devices whose name matches one of ``names`` (case-insensitive, exact)."""
    wanted = {str(n).strip().lower() for n in (names or []) if str(n).strip()}
    return [k for k, r in doc["devices"].items() if r["name"].lower() in wanted]


def seed_candidates(snapshot: dict, memory: dict, now: float, key_of) -> list[tuple[str, str, str, str]]:
    """(key, name, integration, device_id) of every SEED_INTEGRATIONS device offline for SEED_DAYS or more
    (``memory`` = the 0.9.38 offline_since doc; ``key_of`` = devicekeys.Keys.key_of)."""
    from . import devicekeys as DK
    registry = snapshot.get("registry") or {}
    devices = snapshot.get("devices") or {}
    platform_of: dict[str, str] = {}
    for reg in registry.values():
        if isinstance(reg, dict) and reg.get("device_id"):
            platform_of.setdefault(reg["device_id"], str(reg.get("platform") or ""))
    out = []
    for dev, since in sorted((memory or {}).items()):
        if dev not in devices or not isinstance(since, (int, float)) or now - float(since) < SEED_DAYS * 86400:
            continue
        domain = DK.domain_of(devices.get(dev), platform_of.get(dev))
        if domain not in SEED_INTEGRATIONS:
            continue
        out.append((key_of("offline", dev, domain), str(devices[dev].get("name") or "a device"), domain, dev))
    return out


def hide(findings: list, doc: dict) -> list:
    """Findings without the retired devices: their own ``offline:``/``battery:`` keys, and their names dropped from
    a group's or the long-offline list's facts (a group of only retired devices disappears)."""
    retired_keys = set(doc["devices"])
    retired_h = {k.rsplit(":", 1)[1] for k in retired_keys if k.count(":") >= 2}
    retired_names = {r["name"] for r in doc["devices"].values()}
    out = []
    for f in findings:
        if f.key in retired_keys:
            continue
        if f.key.startswith(("battery:", "offline:")) and f.key.count(":") >= 2 and f.key.rsplit(":", 1)[1] in retired_h \
                and not f.key.startswith("offline:group:"):
            continue
        if f.key.startswith("offline:group:") or f.key == "offline:long":
            facts = dict(f.facts or {})
            names_key = "devices" if f.key.startswith("offline:group:") else "names"
            names = [n for n in (facts.get(names_key) or []) if n not in retired_names]
            if not names:
                continue
            if len(names) != len(facts.get(names_key) or []):
                import dataclasses
                facts[names_key] = names
                facts["count" if names_key == "devices" else "devices"] = len(names)
                f = dataclasses.replace(f, facts=facts)
        out.append(f)
    return out


def back_online(doc: dict, snapshot: dict, memory: dict, now: float) -> list[str]:
    """Retired devices (by device id) that report anything but ``unavailable`` now and are not in the offline memory:
    their keys, remembered in ``doc["back"]`` the first time (one morning line each)."""
    present = {reg.get("device_id") for reg in (snapshot.get("registry") or {}).values() if isinstance(reg, dict)}
    new = []
    for key, rec in doc["devices"].items():
        dev = rec.get("device_id")
        if not dev or dev not in present or dev in (memory or {}):
            doc["declined"].pop(key, None)                             # offline again: a later return asks anew
            continue
        if key in doc["declined"]:
            continue
        if key not in doc["back"]:
            doc["back"][key] = now
            new.append(key)
    return new


def page_rows(doc: dict) -> list[dict]:
    """The page's "Needs your decision" rows for retired devices that came back: Approve = Reactivate."""
    rows = []
    for key in sorted(doc["back"]):
        rec = doc["devices"].get(key) or {}
        rows.append({"key": f"{STAGE}:{key}|*", "stage": STAGE, "problem": key,
                     "title": f"Reactivate {rec.get('name', key)}? (retired, back online)",
                     "label": "Reactivate",
                     "detail": f"{rec.get('name', key)} ({rec.get('integration', '')}) was retired on {rec.get('retired_at', '')}: "
                               f"{rec.get('note', '')}. It reports online again. Approve to watch it again; Decline keeps it retired.",
                     "ai": "", "ai_url": "", "ai_verdict": ""})
    return rows


def record_markdown(doc: dict) -> str:
    """The repository record of the retired list and the way back (survives an App reinstall)."""
    lines = ["| Device | Integration | Retired | Note | Key |", "|---|---|---|---|---|"]
    for key, r in sorted(doc["devices"].items(), key=lambda kv: kv[1]["name"]):
        lines.append(f"| {r['name']} | {r['integration']} | {r['retired_at']} | {r['note']} | `{key}` |")
    return "\n".join(lines)
