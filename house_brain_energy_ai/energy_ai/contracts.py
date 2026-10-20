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
