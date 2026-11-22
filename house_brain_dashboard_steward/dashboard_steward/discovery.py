"""Registry discovery: compare two registry snapshots and say what changed, with the evidence for it.

A snapshot is built from read-only registry lists (``config/entity_registry/list``,
``config/device_registry/list``, ``config/area_registry/list``) plus ``get_states``. Identity rules:

* an entity's identity is (platform, unique_id) when it has a unique_id, else its entity_id;
* a changed entity_id with the same identity is a RENAME, never "remove + new";
* a new device that looks like an old one (same manufacturer, model and area) is only a
  REPLACEMENT CANDIDATE. It is never mapped to the old one automatically: the owner decides (the 2026-11
  Ondilo case: the replacement probe's ``*_2`` entities must not take over names reserved for the spa).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field

from . import lovelace as L

SNAPSHOT_SCHEMA = "dashboard_steward_registry_snapshot.v1"
_ENT_FIELDS = ("entity_id", "unique_id", "platform", "device_id", "disabled_by", "hidden_by", "name",
               "original_name", "entity_category", "area_id", "labels", "device_class", "original_device_class")
_DEV_FIELDS = ("id", "name", "name_by_user", "manufacturer", "model", "area_id", "disabled_by", "sw_version")
UNAVAILABLE = ("unavailable", "unknown")


def build_snapshot(entity_list, device_list, state_list, area_list=()) -> dict:
    """Project raw registry/state lists onto the fields Dashboard Steward needs (nothing else is kept)."""
    ents = {}
    for e in entity_list or []:
        if isinstance(e, dict) and isinstance(e.get("entity_id"), str) and L.RE_ENTITY_ID.match(e["entity_id"]):
            ents[e["entity_id"]] = {k: e.get(k) for k in _ENT_FIELDS if k != "entity_id"}
    devs = {}
    for d in device_list or []:
        if isinstance(d, dict) and isinstance(d.get("id"), str):
            devs[d["id"]] = {k: d.get(k) for k in _DEV_FIELDS if k != "id"}
    states = {}
    for s in state_list or []:
        if isinstance(s, dict) and isinstance(s.get("entity_id"), str):
            attrs = s.get("attributes") if isinstance(s.get("attributes"), dict) else {}
            states[s["entity_id"]] = {"state": str(s.get("state"))[:64],
                                      "device_class": attrs.get("device_class"),
                                      "friendly_name": attrs.get("friendly_name"),
                                      "unit": attrs.get("unit_of_measurement")}
            if s["entity_id"] not in ents:   # state-only entities (no unique_id, e.g. template YAML)
                ents[s["entity_id"]] = {k: None for k in _ENT_FIELDS if k != "entity_id"}
                ents[s["entity_id"]]["platform"] = None
    areas = {a["area_id"]: a.get("name") for a in area_list or [] if isinstance(a, dict) and a.get("area_id")}
    return {"schema": SNAPSHOT_SCHEMA, "entities": ents, "devices": devs, "states": states, "areas": areas}


def inventory_entities(snapshot: dict) -> dict:
    """The validator's entity inventory: entity_id -> {disabled, available, domain}."""
    out = {}
    for eid, e in snapshot.get("entities", {}).items():
        st = snapshot.get("states", {}).get(eid)
        out[eid] = {"disabled": bool(e.get("disabled_by")),
                    "available": None if st is None else st["state"] not in UNAVAILABLE}
    return out


def _identity(eid, e):
    if e.get("unique_id") is not None and e.get("platform"):
        return ("u", e["platform"], str(e["unique_id"]))
    return ("e", eid)


def _display(e, st):
    return e.get("name") or e.get("original_name") or (st or {}).get("friendly_name")


@dataclass
class Event:
    kind: str
    entity_id: str = ""
    device_id: str = ""
    evidence: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        blob = json.dumps([self.kind, self.entity_id, self.device_id, self.evidence], sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:32]

    def as_dict(self):
        return {**asdict(self), "fingerprint": self.fingerprint}


def diff_snapshots(prev: dict | None, cur: dict) -> list:
    """Events from ``prev`` to ``cur``. ``prev=None`` (first run) yields no per-entity events: the first
    snapshot is the baseline, not a flood of 'new' things."""
    if prev is None:
        return []
    events: list[Event] = []
    pe, ce = prev.get("entities", {}), cur.get("entities", {})
    ps, cs = prev.get("states", {}), cur.get("states", {})
    pd, cd = prev.get("devices", {}), cur.get("devices", {})
    p_by_id = {_identity(k, v): k for k, v in pe.items()}
    c_by_id = {_identity(k, v): k for k, v in ce.items()}
    renamed_old = set()
    for ident, new_eid in c_by_id.items():
        old_eid = p_by_id.get(ident)
        if old_eid is None:
            continue
        e_new, e_old = ce[new_eid], pe[old_eid]
        if old_eid != new_eid:
            renamed_old.add(old_eid)
            events.append(Event("ENTITY_RENAMED", new_eid, e_new.get("device_id") or "",
                                {"old_entity_id": old_eid, "identity": "platform+unique_id"}))
        dn_old, dn_new = _display(e_old, ps.get(old_eid)), _display(e_new, cs.get(new_eid))
        if dn_old != dn_new and dn_old is not None:
            events.append(Event("DISPLAY_NAME_CHANGED", new_eid, "", {"old": dn_old, "new": dn_new}))
        if bool(e_old.get("disabled_by")) != bool(e_new.get("disabled_by")):
            events.append(Event("ENTITY_DISABLED" if e_new.get("disabled_by") else "ENTITY_ENABLED", new_eid,
                                e_new.get("device_id") or "", {"by": e_new.get("disabled_by")}))
        was = ps.get(old_eid, {}).get("state")
        now = cs.get(new_eid, {}).get("state")
        if now in UNAVAILABLE and was is not None and was not in UNAVAILABLE:
            events.append(Event("ENTITY_UNAVAILABLE", new_eid, e_new.get("device_id") or "", {"state": now}))
        elif was in UNAVAILABLE and now is not None and now not in UNAVAILABLE:
            events.append(Event("ENTITY_RECOVERED", new_eid, e_new.get("device_id") or "", {}))
    for ident, new_eid in c_by_id.items():
        if ident not in p_by_id:
            e = ce[new_eid]
            events.append(Event("ENTITY_NEW", new_eid, e.get("device_id") or "",
                                {"platform": e.get("platform"), "new_device": bool(e.get("device_id"))
                                 and e.get("device_id") not in pd}))
            base = re.sub(r"_\d+$", "", new_eid)
            if base != new_eid and base in ce:
                events.append(Event("ENTITY_SUFFIX_COLLISION", new_eid, "", {"existing": base,
                                     "note": "a second entity took a numbered name; do not repoint automatically"}))
    for ident, old_eid in p_by_id.items():
        if ident not in c_by_id and old_eid not in renamed_old:
            events.append(Event("ENTITY_REMOVED", old_eid, pe[old_eid].get("device_id") or "", {}))
    new_platforms = {e.get("platform") for e in ce.values()} - {e.get("platform") for e in pe.values()} - {None}
    for p in sorted(new_platforms):
        events.append(Event("INTEGRATION_NEW", "", "", {"platform": p}))
    for did, d in cd.items():
        if did in pd:
            continue
        events.append(Event("DEVICE_NEW", "", did, {"model": d.get("model"), "manufacturer": d.get("manufacturer")}))
        for odid, od in pd.items():
            if odid in cd and not _device_gone(odid, pe, cs):
                continue
            if od.get("model") and (od.get("manufacturer"), od.get("model"), od.get("area_id")) == \
                    (d.get("manufacturer"), d.get("model"), d.get("area_id")):
                events.append(Event("DEVICE_REPLACEMENT_CANDIDATE", "", did,
                                    {"old_device_id": odid, "match": "manufacturer+model+area",
                                     "proof": "NONE: identity not proven; owner decides any repointing"}))
    return events


def _device_gone(did, pe, cs) -> bool:
    ents = [k for k, v in pe.items() if v.get("device_id") == did]
    return bool(ents) and all(cs.get(k, {}).get("state", "unavailable") in UNAVAILABLE for k in ents)


# --------------------------------------------------------------------------- capability bundles + placement
CATEGORIES = ("safety", "climate", "pool_spa", "water", "energy", "vehicles", "environment", "engineering")
_KEYWORDS = [
    ("safety", r"smoke|carbon_monoxide|_co_|\bco\b|alarm|door|lock|garage|window_open|siren|intrusion"),
    ("water", r"\bro_|_ro_|leak|irrigation|hose|water_heater|sprinkler|tank|valve|b_hyve|moisture"),
    ("pool_spa", r"pool|spa|aquarite|ondilo|chlorin|_ph_|_orp|salt"),
    ("vehicles", r"tesla|bolt|vehicle|_ev_|charger|charging"),
    ("energy", r"solar|energy|power|grid|kwh|tariff|sense_|voltage|battery_storage|powerwall"),
    ("climate", r"cooling|evap|ecobee|thermostat|hvac|_ac_|a_c|heat_pump|attic|indoor"),
    ("environment", r"weather|outdoor|air_quality|pm25|pm2_5|aqi|wind|rain|uv_|pollen"),
]
_ENGINEERING = re.compile(r"esphome_version|uptime|wifi_signal|rssi|_adc|diagnostic|firmware|_version|linkquality"
                          r"|sample_age|calibration_status|signal_status")
_SAFETY_CLASSES = {"smoke", "carbon_monoxide", "gas", "moisture", "safety", "problem", "tamper"}


def categorize(text: str, device_class: str | None = None) -> str:
    t = text.lower()
    if device_class in _SAFETY_CLASSES:
        return "safety" if device_class != "moisture" else "water"
    for cat, rx in _KEYWORDS:
        if re.search(rx, t):
            return cat
    return "engineering" if _ENGINEERING.search(t) else "other"


@dataclass
class Bundle:
    key: str                     # device id or "entity:<id>"
    title: str
    entities: list
    category: str
    placement: str               # home_attention | category | advanced | owner_review_control | none
    reasons: list
    actuators: list
    diagnostics: list

    def as_dict(self):
        return asdict(self)


def bundle(entity_ids, snapshot: dict) -> list:
    """Group entities into capability bundles (one per device) and pick a deterministic placement.

    The model may refine wording and layout; it may not move a bundle to a more prominent placement than
    this classifier allows (``placement_rank``), and never onto Home as a control.
    """
    ents, devs, states = snapshot.get("entities", {}), snapshot.get("devices", {}), snapshot.get("states", {})
    groups: dict = {}
    for eid in sorted(set(entity_ids)):
        e = ents.get(eid, {})
        key = e.get("device_id") or f"entity:{eid}"
        groups.setdefault(key, []).append(eid)
    out = []
    for key, members in groups.items():
        dev = devs.get(key, {})
        title = dev.get("name_by_user") or dev.get("name") or members[0]
        actuators = [m for m in members if L.domain(m) in L.ACTUATOR_DOMAINS]
        diags = [m for m in members if (ents.get(m, {}).get("entity_category") in ("diagnostic", "config"))
                 or _ENGINEERING.search(m)]
        primary = [m for m in members if m not in diags and m not in actuators]
        dcs = {(states.get(m, {}) or {}).get("device_class") or ents.get(m, {}).get("device_class")
               or ents.get(m, {}).get("original_device_class") for m in members}
        cat_votes = [categorize(m + " " + str(title), (states.get(m, {}) or {}).get("device_class")) for m in primary or members]
        category = max(set(cat_votes), key=cat_votes.count) if cat_votes else "other"
        reasons = []
        if actuators:
            placement = "owner_review_control"
            reasons.append(f"{len(actuators)} entities can change the house; never placed without owner review")
        elif not primary:
            placement = "advanced"
            reasons.append("only diagnostic/config entities")
        elif category == "safety" or dcs & {"smoke", "carbon_monoxide", "gas"}:
            placement = "home_attention"
            reasons.append("safety signal: a conditional attention card on Home, details in Safety")
        elif category in CATEGORIES:
            placement = "category"
            reasons.append(f"belongs in the {category} area")
        else:
            placement = "none"
            reasons.append("no household use identified")
        if diags and placement != "advanced":
            reasons.append(f"{len(diags)} diagnostic entities go to Advanced, not the main card")
        out.append(Bundle(key, str(title)[:80], members, category, placement, reasons, actuators, diags))
    return out


PLACEMENT_RANK = {"none": 0, "advanced": 1, "category": 2, "owner_review_control": 2, "home_attention": 3}
