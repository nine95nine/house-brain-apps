"""Inspector 0.2.9: strictly bounded, no-PII full-registry collision result.

Runs on the ALREADY retrieved full Home Assistant config/entity_registry/list WebSocket
response, before entity_sender.registry_map() discards unique_id. No extra HA request,
permissions, secrets, cloud activity, or state/control writes. No registry row export.
"""
from __future__ import annotations

from typing import Any

SCHEMA = "family_preparedness_registry_summary.v1"
EXPECTED_ENTITIES = frozenset((
    "input_select.house_brain_family_household_review",
    "input_select.house_brain_family_water_review",
    "input_select.house_brain_family_food_review",
    "input_select.house_brain_family_infant_review",
    "input_select.house_brain_family_medical_review",
    "input_select.house_brain_family_go_bags_review",
    "input_select.house_brain_family_network_review",
    "input_select.house_brain_family_communications_review",
    "input_select.house_brain_family_evacuation_review",
    "input_datetime.house_brain_family_household_verified_on",
    "input_datetime.house_brain_family_water_verified_on",
    "input_datetime.house_brain_family_food_verified_on",
    "input_datetime.house_brain_family_infant_verified_on",
    "input_datetime.house_brain_family_medical_verified_on",
    "input_datetime.house_brain_family_go_bags_verified_on",
    "input_datetime.house_brain_family_network_verified_on",
    "input_datetime.house_brain_family_communications_verified_on",
    "input_datetime.house_brain_family_evacuation_verified_on",
    "input_number.house_brain_family_people",
    "input_number.house_brain_family_target_days",
    "input_number.house_brain_family_water_daily_gallons",
    "input_number.house_brain_family_water_gallons",
    "input_number.house_brain_family_food_days",
    "sensor.house_brain_family_preparedness_software",
    "sensor.house_brain_family_household_check",
    "sensor.house_brain_family_water_check",
    "sensor.house_brain_family_food_check",
    "sensor.house_brain_family_infant_check",
    "sensor.house_brain_family_medical_check",
    "sensor.house_brain_family_go_bags_check",
    "sensor.house_brain_family_network_check",
    "sensor.house_brain_family_communications_check",
    "sensor.house_brain_family_evacuation_check",
    "sensor.house_brain_family_water_target_gallons",
    "sensor.house_brain_family_water_shortfall_gallons",
    "sensor.house_brain_family_verified_checks",
    "sensor.house_brain_family_preparedness_status",
))
EXPECTED_SENSOR_UNIQUE_IDS = frozenset((
    "house_brain_family_preparedness_software_v1",
    "house_brain_family_household_check_v1",
    "house_brain_family_water_check_v1",
    "house_brain_family_food_check_v1",
    "house_brain_family_infant_check_v1",
    "house_brain_family_medical_check_v1",
    "house_brain_family_go_bags_check_v1",
    "house_brain_family_network_check_v1",
    "house_brain_family_communications_check_v1",
    "house_brain_family_evacuation_check_v1",
    "house_brain_family_water_target_gallons_v1",
    "house_brain_family_water_shortfall_gallons_v1",
    "house_brain_family_verified_checks_v1",
    "house_brain_family_preparedness_status_v1",
))
MAX_REGISTRY_ROWS = 10_000
MAX_ENTITY_ID_LEN = 160
MAX_UNIQUE_ID_LEN = 512

if len(EXPECTED_ENTITIES) != 37 or len(EXPECTED_SENSOR_UNIQUE_IDS) != 14:
    raise RuntimeError("REGISTRY_PROBE_SOURCE_CONTRACT_MISMATCH")


def _blocked(reason: str, rows: int = 0) -> dict[str, Any]:
    """Unknown is NOT clear. Emit only allowlisted fixed error codes and counts."""
    return {
        "schema": SCHEMA, "result": "INDETERMINATE", "reason": reason,
        "registry_rows_examined": rows, "proposed_entity_count": 37,
        "proposed_unique_id_count": 14,
        "entity_id_collisions": None, "unique_id_collisions": None,
        "full_registry_path": "config/entity_registry/list",
    }


def summarize(rows: Any) -> dict[str, Any]:
    """Fail-closed private match on exact IDs and unique IDs including disabled rows.

    rows is only a trusted *complete* successful WebSocket response returned by
    Inspector.ha_ws_command. The caller must NOT supply filtered state/list_for_display
    rows; completeness cannot be inferred from an arbitrary list alone.
    """
    if type(rows) is not list or not rows or len(rows) >= MAX_REGISTRY_ROWS:
        return _blocked("REGISTRY_RESPONSE_INVALID_OR_EMPTY")
    seen: set[str] = set()
    ids: set[str] = set()
    uids: set[str] = set()
    for entry in rows:
        if type(entry) is not dict:
            return _blocked("REGISTRY_ROW_INVALID", len(seen))
        eid, uid = entry.get("entity_id"), entry.get("unique_id")
        if (type(eid) is not str or not (3 <= len(eid) <= MAX_ENTITY_ID_LEN)
                or "." not in eid or type(uid) is not str
                or not (1 <= len(uid) <= MAX_UNIQUE_ID_LEN)):
            return _blocked("REGISTRY_IDENTIFIERS_MISSING_OR_INVALID", len(seen))
        if eid in seen:
            return _blocked("REGISTRY_ENTITY_ID_DUPLICATE", len(seen))
        seen.add(eid)
        if eid in EXPECTED_ENTITIES:
            ids.add(eid)
        # Conservatively refuse identical proposed sensor unique_id in ANY domain;
        # another entity/platform can never be interpreted as safe reuse.
        if uid in EXPECTED_SENSOR_UNIQUE_IDS:
            uids.add(uid)
    return {
        "schema": SCHEMA,
        "result": "COLLISION" if ids or uids else "CLEAR",
        "reason": "MATCHED_PROPOSED_IDENTIFIER" if ids or uids else "NONE",
        "registry_rows_examined": len(rows),
        "proposed_entity_count": 37,
        "proposed_unique_id_count": 14,
        "entity_id_collisions": len(ids),
        "unique_id_collisions": len(uids),
        "full_registry_path": "config/entity_registry/list",
    }
