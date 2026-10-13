#!/usr/bin/env python3
"""Build the privacy-filtered House Brain entity snapshot (Initiative #212 gate G0, R1).

Owner decisions 2026-09-30: "Allowlisted snapshot" and "Broad (non-sensitive)". This pure, offline
builder turns a Home Assistant ``/api/states`` list plus an entity-registry map
(``entity_id -> {"platform", "device_id"}``) into:

* ``mode="dry_run"`` (default): a report with per-reason exclusion **counts** and the entity ids that
  would be exported, never a state value and never an excluded id. The owner reviews it before the
  first real export.
* ``mode="export"``: the sanitized snapshot. Refused unless every owner precondition in the policy is
  true (health-bridge prefix denied, dry-run report reviewed).

It reuses the accepted #60 surfaces unchanged: restricted-category name tokens from
``contracts/house_brain_privacy_data_classes.v1.json`` and content detectors from
``tools/privacy_screen.py``. It performs no I/O besides reading the two contracts, reads no clock
(``now`` is explicit), and has no control authority. Any shape problem refuses the whole snapshot.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Final

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.privacy_screen import _contains, _tokens, scan_content  # noqa: E402  (accepted #60 library)

POLICY_PATH: Final = ROOT / "contracts/house_brain_entity_read_policy.v1.json"
CLASS_MAP_PATH: Final = ROOT / "contracts/house_brain_privacy_data_classes.v1.json"
INSPECTION_PATH: Final = ROOT / "contracts/house_brain_ai_inspection_plane.v1.json"
SNAPSHOT_SCHEMA: Final = "house_brain_entity_snapshot.v1"
REPORT_SCHEMA: Final = "house_brain_entity_snapshot.dry_run.v1"
ENTITY_ID_RE: Final = re.compile(r"\A[a-z0-9_]{1,64}\.[a-z0-9_]{1,128}\Z")
ALIAS_LABEL_RE: Final = re.compile(r"\A[a-z][a-z0-9_]{0,39}\Z")
ALIAS_FORM_RE: Final = re.compile(r"\A[a-z0-9]+(?:_[a-z0-9]+)*\Z")
ALIAS_WORD_RE: Final = re.compile(r"\A[a-z]{3,40}\Z")
TS_RE: Final = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})\Z")
MODES: Final = ("dry_run", "export")
# Reserved for the future #56 health bridge (owner decision 2026-10-02): never exported, whatever the content.
HEALTH_BRIDGE_PREFIXES: Final = ("sensor.hb_health_", "binary_sensor.hb_health_")


class SnapshotError(ValueError):
    """Constant reason codes only; never a state value, attribute value or excluded entity id."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise SnapshotError(code)


def load_policy(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    p = policy if policy is not None else json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    _require(type(p) is dict and p.get("schema_version") == "house_brain_entity_read_policy.v1", "POLICY_SCHEMA")
    for key in ("domains", "excluded_device_classes", "excluded_platforms", "owner_deny_entities",
                "owner_deny_prefixes", "token_exemptions", "default_attributes", "history_list"):
        _require(type(p.get(key)) is list and all(type(x) is str for x in p[key]), "POLICY_FIELD_INVALID")
    _require(type(p.get("attribute_allowlist")) is dict and all(
        ENTITY_ID_RE.fullmatch(k) and type(v) is list and all(type(a) is str for a in v)
        for k, v in p["attribute_allowlist"].items()), "POLICY_ATTRIBUTE_ALLOWLIST_INVALID")
    b = p.get("bounds")
    _require(type(b) is dict and all(type(b.get(k)) is int and b[k] > 0 for k in
             ("max_entities", "max_snapshot_bytes", "max_state_chars", "max_attribute_chars",
              "max_attributes_per_entity")), "POLICY_BOUNDS_INVALID")
    pre = p.get("preconditions_for_real_export")
    _require(type(pre) is dict and pre and all(type(v) is bool for v in pre.values()), "POLICY_PRECONDITIONS_INVALID")
    # The health-bridge precondition is a claim about owner_deny_prefixes: it may be true only while both hold.
    if pre.get("health_bridge_prefix_in_owner_deny_prefixes"):
        _require(set(HEALTH_BRIDGE_PREFIXES) <= set(p["owner_deny_prefixes"]), "POLICY_HEALTH_PREFIX_NOT_DENIED")
    aliases = p.get("owner_name_aliases", [])
    _require(type(aliases) is list and all(
        type(a) is dict and set(a) == {"label", "id_forms", "text_words"}
        and type(a["label"]) is str and ALIAS_LABEL_RE.fullmatch(a["label"]) is not None
        and type(a["id_forms"]) is list and a["id_forms"]
        and all(type(f) is str and ALIAS_FORM_RE.fullmatch(f) for f in a["id_forms"])
        and type(a["text_words"]) is list and a["text_words"]
        and all(type(w) is str and ALIAS_WORD_RE.fullmatch(w) for w in a["text_words"])
        for a in aliases), "POLICY_NAME_ALIASES_INVALID")
    return p


class _Aliaser:
    """Owner name aliases (owner decision 2026-10-02 "Share with neutral labels"): personal names in entity ids
    and text values are replaced by neutral labels in the exported copy only. Home Assistant is unchanged."""

    def __init__(self, aliases: list[dict[str, Any]]) -> None:
        self.id_rules = [(re.compile(r"(?<![a-z0-9])" + re.escape(f) + r"(?![a-z0-9])"), a["label"])
                         for a in aliases for f in sorted(a["id_forms"], key=len, reverse=True)]
        self.text_rules = [(re.compile(r"(?i)\b" + re.escape(w) + r"(?:'s)?\b"), a["label"].replace("_", " "))
                           for a in aliases for w in a["text_words"]]
        self.words = [re.compile(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])")
                      for a in aliases for w in a["text_words"]]

    def entity_id(self, entity_id: str) -> str:
        domain, obj = entity_id.split(".", 1)
        for rule, label in self.id_rules:
            obj = rule.sub(label, obj)
        return f"{domain}.{obj}"

    def text(self, value: Any) -> Any:
        if type(value) is not str:
            return value
        for rule, label in self.text_rules:
            value = rule.sub(label, value)
        return value

    def leaves_name(self, entity_id: str) -> bool:
        return any(w.search(entity_id) for w in self.words)


def _restricted_categories() -> dict[str, list[list[str]]]:
    cmap = json.loads(CLASS_MAP_PATH.read_text(encoding="utf-8"))
    return {name: [_tokens(t) for t in spec["name_tokens"]] for name, spec in cmap["restricted_categories"].items()}


def _blocked_domains() -> frozenset[str]:
    plane = json.loads(INSPECTION_PATH.read_text(encoding="utf-8"))
    return frozenset(plane["privacy"]["blocked_entity_domains"])


def _name_category(name: str, categories: dict[str, list[list[str]]]) -> str | None:
    tokens = _tokens(name)
    for category, compounds in sorted(categories.items()):
        if any(_contains(tokens, compound) for compound in compounds):
            return category
    return None


def _scalar(value: Any, max_chars: int) -> tuple[bool, Any]:
    if value is None or type(value) is bool:
        return True, value
    if type(value) is int:
        return abs(value) <= 2**53, value
    if type(value) is float:
        return math.isfinite(value), value
    if type(value) is str:
        return True, value[:max_chars]
    return False, None


def _exclude(counts: dict[str, int], reason: str) -> None:
    counts[reason] = counts.get(reason, 0) + 1


def _registry(registry: Any) -> dict[str, tuple[str, str | None]]:
    _require(type(registry) is dict, "REGISTRY_INVALID")
    out: dict[str, tuple[str, str | None]] = {}
    for entity_id, row in registry.items():
        _require(type(entity_id) is str and ENTITY_ID_RE.fullmatch(entity_id) is not None
                 and type(row) is dict and set(row) == {"platform", "device_id"}
                 and type(row["platform"]) is str and (row["device_id"] is None or type(row["device_id"]) is str),
                 "REGISTRY_INVALID")
        out[entity_id] = (row["platform"], row["device_id"])
    return out


MAX_HISTORY_CHANGES: Final = 200


def _history(history: Any, exported: dict[str, dict[str, Any]], history_list: list[str],
             max_chars: int, counts: dict[str, int]) -> dict[str, list[dict[str, str]]]:
    """Bounded state-change history for exported entities on the owner's history list only."""
    if history is None:
        return {}
    _require(type(history) is dict, "HISTORY_INVALID")
    out: dict[str, list[dict[str, str]]] = {}
    for entity_id in sorted(set(history_list)):
        rows = history.get(entity_id)
        if rows is None:
            continue
        _require(type(rows) is list and all(type(r) is dict and type(r.get("at")) is str
                                            and type(r.get("state")) is str for r in rows), "HISTORY_INVALID")
        if entity_id not in exported:
            _exclude(counts, "HISTORY_ENTITY_NOT_EXPORTED"); continue
        changes = [{"at": r["at"], "state": r["state"][:max_chars]} for r in rows if TS_RE.fullmatch(r["at"])]
        if scan_content(changes):
            _exclude(counts, "HISTORY_CONTENT_DETECTOR"); continue
        out[entity_id] = sorted(changes, key=lambda c: c["at"])[-MAX_HISTORY_CHANGES:]
    return out


def build(states: Any, registry: Any, *, now: str, mode: str = "dry_run",
          policy: dict[str, Any] | None = None, history: Any = None) -> dict[str, Any]:
    """Return a dry-run report or an export snapshot. Raises SnapshotError (whole refusal) on bad input."""
    _require(mode in MODES, "MODE_INVALID")
    _require(type(now) is str and TS_RE.fullmatch(now) is not None, "NOW_INVALID")
    p = load_policy(policy)
    bounds = p["bounds"]
    if mode == "export":
        _require(all(p["preconditions_for_real_export"].values()), "EXPORT_PRECONDITIONS_NOT_MET")
    _require(type(states) is list, "STATES_NOT_LIST")
    reg = _registry(registry)
    categories = _restricted_categories()
    blocked = _blocked_domains()
    # Inspector R1 rule: a device that carries any blocked-domain entity (lock, camera, alarm, person,
    # tracker, ...) exports none of its entities, e.g. a lock's battery sensor.
    blocked_devices = frozenset(dev for eid, (_, dev) in reg.items()
                                if dev is not None and eid.split(".", 1)[0] in blocked)
    domains = frozenset(p["domains"])
    deny = frozenset(p["owner_deny_entities"])
    deny_prefixes = tuple(p["owner_deny_prefixes"])
    exempt = frozenset(p["token_exemptions"])
    aliaser = _Aliaser(p.get("owner_name_aliases", []))
    original_ids = frozenset(row.get("entity_id") for row in states if type(row) is dict)
    by_original: dict[str, dict[str, Any]] = {}
    used_ids: set[str] = set()
    counts: dict[str, int] = {}
    seen: set[str] = set()
    exported: list[dict[str, Any]] = []
    for row in states:
        _require(type(row) is dict, "STATE_ROW_NOT_OBJECT")
        entity_id = row.get("entity_id")
        _require(type(entity_id) is str and ENTITY_ID_RE.fullmatch(entity_id) is not None, "ENTITY_ID_INVALID")
        _require(entity_id not in seen, "DUPLICATE_ENTITY_ID")
        seen.add(entity_id)
        state = row.get("state")
        attributes = row.get("attributes", {})
        _require(type(state) is str and type(attributes) is dict, "STATE_ROW_SHAPE")
        domain = entity_id.split(".", 1)[0]
        # Order matters only for which reason is counted; every rule excludes.
        if entity_id in deny or entity_id.startswith(deny_prefixes):
            _exclude(counts, "OWNER_DENY"); continue
        if domain in blocked:
            _exclude(counts, "BLOCKED_DOMAIN"); continue
        if domain not in domains:
            _exclude(counts, "DOMAIN_NOT_EXPORTED"); continue
        if entity_id not in reg:
            _exclude(counts, "NOT_IN_REGISTRY"); continue  # fail closed: the registry must vouch for it
        platform, device_id = reg[entity_id]
        if platform in p["excluded_platforms"]:
            _exclude(counts, "PLATFORM_EXCLUDED"); continue
        if device_id is not None and device_id in blocked_devices:
            _exclude(counts, "BLOCKED_DEVICE"); continue
        device_class = attributes.get("device_class")
        if device_class is not None and (type(device_class) is not str
                                         or device_class in p["excluded_device_classes"]):
            _exclude(counts, "DEVICE_CLASS_EXCLUDED"); continue
        if entity_id not in exempt:
            category = _name_category(entity_id, categories) or (
                _name_category(device_class, categories) if device_class else None)
            if category is not None:
                _exclude(counts, "CATEGORY_" + category); continue
        keep_names = list(p["default_attributes"]) + list(p["attribute_allowlist"].get(entity_id, []))
        kept: dict[str, Any] = {}
        bad_attribute = False
        for name in keep_names:
            if name not in attributes or name in kept:
                continue
            if _name_category(name, categories) is not None:
                continue  # an allowlisted attribute can never carry a restricted-category name
            ok, value = _scalar(attributes[name], bounds["max_attribute_chars"])
            if not ok:
                bad_attribute = True
                break
            kept[name] = value
        if bad_attribute:
            _exclude(counts, "ATTRIBUTE_NOT_SCALAR"); continue
        _require(len(kept) <= bounds["max_attributes_per_entity"], "ATTRIBUTE_COUNT_BOUND")
        value = aliaser.text(state[: bounds["max_state_chars"]])[: bounds["max_state_chars"]]
        kept = {k: (aliaser.text(v)[: bounds["max_attribute_chars"]] if type(v) is str else v) for k, v in kept.items()}
        shared_id = aliaser.entity_id(entity_id)
        if (aliaser.leaves_name(shared_id) or ENTITY_ID_RE.fullmatch(shared_id) is None
                or (shared_id != entity_id and shared_id in original_ids)):
            _exclude(counts, "NAME_ALIAS_INCOMPLETE"); continue
        if shared_id in used_ids:
            _exclude(counts, "NAME_ALIAS_COLLISION"); continue
        if scan_content({"state": value, "attributes": kept}):
            _exclude(counts, "CONTENT_DETECTOR"); continue
        used_ids.add(shared_id)
        item: dict[str, Any] = {"entity_id": shared_id, "state": value, "attributes": kept,
                                "state_truncated": len(state) > bounds["max_state_chars"],
                                "untrusted_text": True}
        for stamp in ("last_changed", "last_updated"):
            if type(row.get(stamp)) is str and TS_RE.fullmatch(row[stamp]):
                item[stamp] = row[stamp]
        exported.append(item)
        by_original[entity_id] = item
    exported.sort(key=lambda e: e["entity_id"])
    _require(len(exported) <= bounds["max_entities"], "ENTITY_COUNT_BOUND")
    kept_history = {aliaser.entity_id(eid): [{"at": c["at"], "state": aliaser.text(c["state"])[: bounds["max_state_chars"]]}
                         for c in rows]
                    for eid, rows in _history(history, by_original, p["history_list"],
                                              bounds["max_state_chars"], counts).items()}
    snapshot = {"schema": SNAPSHOT_SCHEMA, "generated_at": now, "entity_count": len(exported),
                "entities": exported, "excluded_counts": dict(sorted(counts.items())),
                "history_list": sorted(set(p["history_list"])), "history": kept_history,
                "control_authority": "NONE"}
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    export_bytes = len(encoded.encode("utf-8"))
    if mode == "dry_run":
        # The size of the export this dry run stands for, so the owner's review shows a snapshot that
        # export would refuse (SNAPSHOT_SIZE_BOUND) before any export is approved. Never a value.
        return {"schema": REPORT_SCHEMA, "generated_at": now, "input_count": len(states),
                "export_count": len(exported), "excluded_counts": dict(sorted(counts.items())),
                "would_export_entity_ids": [e["entity_id"] for e in exported],
                "history_entity_ids": sorted(kept_history),
                "export_bytes": export_bytes, "max_snapshot_bytes": bounds["max_snapshot_bytes"],
                "export_size_ok": export_bytes <= bounds["max_snapshot_bytes"],
                "values_included": False, "control_authority": "NONE"}
    _require(export_bytes <= bounds["max_snapshot_bytes"], "SNAPSHOT_SIZE_BOUND")
    return snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--states", type=Path, required=True, help="JSON list from GET /api/states")
    parser.add_argument("--registry", type=Path, required=True,
                        help='JSON object entity_id -> {"platform": ..., "device_id": ... or null}')
    parser.add_argument("--now", required=True)
    parser.add_argument("--mode", choices=MODES, default="dry_run")
    args = parser.parse_args(argv)
    try:
        result = build(json.loads(args.states.read_text(encoding="utf-8")),
                       json.loads(args.registry.read_text(encoding="utf-8")), now=args.now, mode=args.mode)
    except (SnapshotError, ValueError, OSError) as exc:
        code = str(exc) if isinstance(exc, SnapshotError) else type(exc).__name__
        print(json.dumps({"status": "REFUSED", "reason": code}))
        return 2
    print(json.dumps(result, sort_keys=True, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
