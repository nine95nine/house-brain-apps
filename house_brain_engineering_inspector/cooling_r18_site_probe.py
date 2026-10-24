"""One-shot, read-only R18 site probe for the existing Engineering Inspector runtime.

Run in the Inspector container after separately reviewed installation. It uses
the Inspector's existing allowlisted WebSocket read and package inventory.
No raw registry rows, tokens, arbitrary filenames or file contents are emitted.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import inspector

SCHEMA = "cooling_r18_site_probe.v1"
HELPERS = frozenset({
    "input_boolean.cooling_comfort_hold_active",
    "input_boolean.cooling_comfort_policy_enabled",
    "input_datetime.cooling_comfort_hold_placed",
    "input_datetime.cooling_comfort_release_requested",
    "input_number.cooling_comfort_last_setpoint",
    "input_number.cooling_comfort_release_attempts",
    "input_text.cooling_comfort_selected_profile",
})
NEW_TEMPLATE_ENTITIES = frozenset({
    "sensor.cooling_comfort_coordination",
    "sensor.cooling_diagnostics_version",
    "sensor.cooling_diagnostics_source_alignment",
    "sensor.cooling_diagnostics_operation",
    "sensor.cooling_diagnostics_fan_entry",
    "sensor.cooling_diagnostics_temperature_advantage",
    "sensor.cooling_diagnostics_outside_threshold",
    "sensor.cooling_diagnostics_additional_cooling",
    "sensor.cooling_diagnostics_outdoor_dp_headroom",
    "sensor.cooling_diagnostics_indoor_dp_headroom",
    "sensor.cooling_diagnostics_indoor_rh_headroom",
    "sensor.cooling_diagnostics_indoor_dp_entry_headroom",
    "sensor.cooling_diagnostics_indoor_rh_entry_headroom",
    "sensor.cooling_diagnostics_humidity_budget",
    "sensor.cooling_diagnostics_data_status",
    "sensor.cooling_diagnostics_last_target_change",
    "sensor.cooling_diagnostics_last_phase_change",
})
TEMPLATES = frozenset({
    "cooling_comfort_coordination_r18",
    "house_brain_cooling_diagnostics_version_v1",
    "house_brain_cooling_diagnostics_source_alignment_v1",
    "house_brain_cooling_diagnostics_operation_v1",
    "house_brain_cooling_diagnostics_fan_entry_v1",
    "house_brain_cooling_diagnostics_temperature_advantage_v1",
    "house_brain_cooling_diagnostics_outside_threshold_v1",
    "house_brain_cooling_diagnostics_additional_cooling_v1",
    "house_brain_cooling_diagnostics_outdoor_dp_headroom_v1",
    "house_brain_cooling_diagnostics_indoor_dp_headroom_v1",
    "house_brain_cooling_diagnostics_indoor_rh_headroom_v1",
    "house_brain_cooling_diagnostics_indoor_dp_entry_headroom_v1",
    "house_brain_cooling_diagnostics_indoor_rh_entry_headroom_v1",
    "house_brain_cooling_diagnostics_humidity_budget_v1",
    "house_brain_cooling_diagnostics_data_status_v1",
    "house_brain_cooling_diagnostics_last_target_change_v1",
    "house_brain_cooling_diagnostics_last_phase_change_v1",
})
EXPECTED = {
    "house_brain_cooling_arbiter_v2_1_r17.yaml": "8825a008885f1bb73dc40b7e225c097d5e73ed7cd1cd69e1e28da7d884d6ffa5",
}
NEW_ACTIVE = frozenset({
    "house_brain_cooling_arbiter_v2_1_r18.yaml",
    "house_brain_cooling_diagnostics_v1_0_r2.yaml",
})


def summarize(rows: Any, packages: Any) -> dict[str, Any]:
    result = {"schema": SCHEMA, "status": "INDETERMINATE", "reason": "INVALID_INPUT",
              "registry_rows": None, "helper_collisions": None, "unique_id_collisions": None,
              "r17_hash_match": None, "new_package_count": None,
              "raw_registry_exported": False, "mutation": False, "physical_control": False}
    if (type(rows) is not list or not rows or len(rows) >= inspector.MAX_REGISTRY_ROWS
            or type(packages) is not list or not packages):
        return result
    seen = set()
    helpers = set()
    uids = set()
    for row in rows:
        if type(row) is not dict:
            return result
        eid, uid = row.get("entity_id"), row.get("unique_id")
        if (type(eid) is not str or not 3 <= len(eid) <= 160 or "." not in eid
                or type(uid) is not str or not 1 <= len(uid) <= 512 or eid in seen):
            return result
        seen.add(eid)
        if eid in HELPERS or eid in NEW_TEMPLATE_ENTITIES:
            helpers.add(eid)
        if uid in TEMPLATES:
            uids.add(uid)
    if any(type(p) is not dict or type(p.get("name")) is not str for p in packages):
        return result
    names = [p["name"] for p in packages]
    if len(names) != len(set(names)) or any(p.get("kind") == "symlink_rejected" for p in packages):
        return result
    active = [p for p in packages if p.get("kind") == "active_yaml"]
    r17 = [p for p in active if p.get("name") in EXPECTED]
    new = [p for p in active if p.get("name") in NEW_ACTIVE]
    legacy_diag = [p for p in active if p["name"].startswith("house_brain_cooling_diagnostics_")]
    result.update(registry_rows=len(rows), helper_collisions=len(helpers), unique_id_collisions=len(uids),
                  r17_hash_match=len(r17) == 1 and r17[0].get("sha256") == EXPECTED[r17[0]["name"]],
                  new_package_count=len(new))
    result["status"] = "CLEAR" if not helpers and not uids and result["r17_hash_match"] and not new and not legacy_diag else "BLOCK"
    result["reason"] = "NONE" if result["status"] == "CLEAR" else "COLLISION_OR_PACKAGE_MISMATCH"
    return result


def main() -> int:
    try:
        rows = asyncio.run(inspector.ha_ws_command("config/entity_registry/list"))
        packages = inspector.inventory_packages()
        report = summarize(rows, packages)
    except Exception:
        report = {"schema": SCHEMA, "status": "INDETERMINATE", "reason": "READ_FAILED",
                  "raw_registry_exported": False, "mutation": False, "physical_control": False}
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report["status"] == "CLEAR" else 1


if __name__ == "__main__":
    raise SystemExit(main())
