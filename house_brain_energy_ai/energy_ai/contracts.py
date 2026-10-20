"""Strict data boundaries. Provenance families prevent circular corroboration."""
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import math
import re


def timestamp(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("TIMEZONE_REQUIRED")
    return dt.timestamp()


def number(value, lo=-200000, hi=200000):
    if isinstance(value, bool):
        raise ValueError("INVALID_NUMBER")
    n = float(value)
    if not math.isfinite(n) or not lo <= n <= hi:
        raise ValueError("NUMBER_OUT_OF_RANGE")
    return n


@dataclass(frozen=True)
class Binding:
    entity: str
    role: str
    family: str
    classification: str
    max_age_s: int = 300

    def __post_init__(self):
        if not re.fullmatch(r"sensor\.[a-z0-9_]+", self.entity):
            raise ValueError("SENSOR_BINDING_REQUIRED")
        if self.classification not in {"MEASURED", "DERIVED", "ESTIMATED", "USER-CONFIRMED"}:
            raise ValueError("PROVENANCE_REQUIRED")
        if not self.family or not 60 <= self.max_age_s <= 3600:
            raise ValueError("BINDING_INVALID")


def sample(state, binding, now):
    """No interpolation. last_reported is refresh; last_changed is not freshness."""
    result = dict(entity=binding.entity, role=binding.role, family=binding.family,
                  classification=binding.classification, observed_at=now,
                  source_at=None, value=None, quality="MISSING")
    if not state or state.get("entity_id") != binding.entity:
        return result
    try:
        source_at = timestamp(state.get("last_reported") or state["last_updated"])
        result["source_at"] = source_at
        if not 0 <= now - source_at <= binding.max_age_s:
            result["quality"] = "STALE_OR_FUTURE"
            return result
        if state.get("state") in {"unknown", "unavailable", None}:
            return result
        unit = state.get("attributes", {}).get("unit_of_measurement")
        if unit not in {"W", "kW"}:
            result["quality"] = "UNIT_MISMATCH"
            return result
        value = number(state["state"]) * (1000 if unit == "kW" else 1)
        number(value, -200000 if binding.role == "grid" else 0, 200000)
        result.update(value=value, quality="GOOD")
    except (ValueError, TypeError, KeyError, OverflowError):
        result["quality"] = "INVALID"
    return result


def mark_idle(values, states):
    """A detection device sitting at 0 W is idle, not stale (R14, first live run 2026-10-20).

Home Assistant moves a state's timestamps only when the value is written, and the Sense
device sensors are written when the reading changes, so a device that was switched off hours
ago looks stale. Only when the same family's load reading is GOOD in this cycle, the device's
last reading is exactly 0 W/kW and not from the future does STALE_OR_FUTURE become IDLE.
No value is filled in: IDLE samples are never stored or used as measurements.
"""
    fresh = {v["family"] for v in values if v["role"] == "load" and v["quality"] == "GOOD"}
    for v in values:
        if (v["quality"] != "STALE_OR_FUTURE" or not v["role"].startswith("detection:")
                or v["family"] not in fresh or v["source_at"] is None or v["source_at"] > v["observed_at"]):
            continue
        state = states.get(v["entity"]) or {}
        try:
            if (state.get("attributes", {}).get("unit_of_measurement") in {"W", "kW"}
                    and number(state.get("state")) == 0):
                v["quality"] = "IDLE"
        except (ValueError, TypeError, OverflowError):
            continue
    return values


def validate_bindings(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
        raise ValueError("BINDING_LIMIT")
    bindings = [Binding(**r) for r in rows]
    if len({b.entity for b in bindings}) != len(bindings):
        raise ValueError("DUPLICATE_ENTITY")
    for role in ("load", "solar", "grid"):
        if sum(b.role == role for b in bindings) > 1:
            raise ValueError("DUPLICATE_TOTAL")
    return bindings
