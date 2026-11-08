#!/usr/bin/env python3
"""Validate WAN/LAN/DNS/MQTT/DEVICE producer observations and compose Network Posture.

Repository/offline, deterministic and advisory only (Initiative #73). It performs
no network probe, reads no wall clock, and grants no network/device/physical
authority. The accepted Network Dependency Posture evaluator is imported and
called unchanged; this tool only builds its input.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.evaluate_network_dependency_posture import (  # noqa: E402
    ID_RE,
    MAX_NODES,
    NetworkError,
    evaluate as evaluate_posture,
)

INPUT_SCHEMA: Final = "house_brain_network_producer_evaluation_input.v1"
OUTPUT_SCHEMA: Final = "house_brain_network_producer_evaluation_result.v1"
OBSERVATION_SCHEMA: Final = "house_brain_network_producer_observation.v1"
POSTURE_SCHEMA: Final = "house_brain_network_posture.input.v1"
AUTHORITY_IN: Final = "READ_ONLY_OBSERVATION_NO_CONTROL"
AUTHORITY_OUT: Final = "PRODUCER_CONTRACT_EVALUATOR_ADVISORY_NO_NETWORK_OR_CONTROL"
PRIVACY_CLASS: Final = "HOUSEHOLD"
MAX_INPUT_BYTES: Final = 262144

PRODUCER_LAYERS: Final = ("DNS", "DEVICE", "LAN", "MQTT", "WAN")
ADAPTER_LAYERS: Final = frozenset({"SUPERVISOR", "RELAY", "MIRROR"})
ALL_LAYERS: Final = ("DEVICE", "DNS", "LAN", "MIRROR", "MQTT", "RELAY", "SUPERVISOR", "WAN")
STATES: Final = frozenset({"UP", "DEGRADED", "DOWN", "UNKNOWN", "STALE", "UNAVAILABLE"})
QUALITIES: Final = frozenset({"GOOD", "SUSPECT", "UNTRUSTED"})
POSTURE_STATUSES: Final = frozenset({"HEALTHY", "DEGRADED", "DOWN", "UNKNOWN"})
PRODUCER_KINDS: Final = frozenset({
    "HA_INTEGRATION_STATE", "ROUTER_READONLY_API", "BROKER_SYS_TOPICS",
    "PASSIVE_LISTENER", "BOUNDED_SYNTHETIC_PROBE", "OWNER_MANUAL_ENTRY", "REPLAY_FIXTURE",
})
COLLECTION_MODES: Final = frozenset({"PASSIVE", "BOUNDED_ACTIVE_PROBE", "MANUAL", "REPLAY"})

TOP_KEYS: Final = frozenset({"schema_version", "evaluated_at", "observations", "adapter_nodes", "topology"})
OBS_KEYS: Final = frozenset({
    "schema_version", "layer", "node_id", "required", "provenance", "observed_at",
    "freshness_limit_seconds", "quality", "state", "reason_codes", "privacy_class",
    "authority", "metrics",
})
PROVENANCE_KEYS: Final = frozenset({"producer_id", "producer_kind", "source_revision", "collection_mode"})
ADAPTER_NODE_KEYS: Final = frozenset({
    "id", "layer", "status", "required", "sample_age_seconds", "freshness_limit_seconds", "depends_on",
})

# Metric specification per producer: name -> ("bool",) | ("num", lo, hi) | ("int", lo, hi) | ("enum", {...})
METRICS: Final[dict[str, dict[str, tuple[Any, ...]]]] = {
    "WAN": {
        "internet_reachable": ("bool",),
        "latency_ms": ("num", 0, 60000),
        "packet_loss_percent": ("num", 0, 100),
        "link_class": ("enum", frozenset({"PRIMARY", "FAILOVER", "UNKNOWN"})),
        "public_address_changed": ("bool",),
    },
    "LAN": {
        "gateway_reachable": ("bool",),
        "gateway_latency_ms": ("num", 0, 60000),
        "mesh_backhaul_class": ("enum", frozenset({"WIRED", "WIRELESS", "MIXED", "UNKNOWN"})),
        "satellites_expected": ("int", 0, 16),
        "satellites_online": ("int", 0, 16),
    },
    "DNS": {
        "resolver_role": ("enum", frozenset({"LOCAL", "UPSTREAM", "UNKNOWN"})),
        "resolution_ok": ("bool",),
        "resolution_latency_ms": ("num", 0, 60000),
        "query_name_class": ("enum", frozenset({"LOCAL_NAME", "PUBLIC_NAME", "CLOUD_INTEGRATION_NAME", "NONE"})),
        "answers_consistent": ("bool",),
    },
    "MQTT": {
        "broker_reachable": ("bool",),
        "session_connected": ("bool",),
        "last_message_age_seconds": ("int", 0, 86400),
        "retained_topic_count": ("int", 0, 100000),
        "connected_client_count": ("int", 0, 1000),
    },
    "DEVICE": {
        "device_class": ("enum", frozenset({
            "ROUTER", "MESH_SATELLITE", "SWITCH", "IOT_BRIDGE", "SERIAL_BRIDGE", "SENSOR", "CONTROLLER", "OTHER",
        })),
        "radio": ("enum", frozenset({
            "ETHERNET", "WIFI", "ZIGBEE", "ZWAVE", "THREAD", "BLUETOOTH", "OTHER", "UNKNOWN",
        })),
        "reachable": ("bool",),
        "rssi_dbm": ("num", -120, 0),
        "link_quality_percent": ("num", 0, 100),
        "battery_percent": ("num", 0, 100),
    },
}
# device_class/radio are classification, not measurement: they are required non-null.
NON_NULL_METRICS: Final = frozenset({("DEVICE", "device_class"), ("DEVICE", "radio")})
PRIMARY_REACHABILITY: Final = {
    "WAN": "internet_reachable",
    "LAN": "gateway_reachable",
    "DNS": "resolution_ok",
    "MQTT": "broker_reachable",
    "DEVICE": "reachable",
}
POSTURE_OF: Final = {
    "UP": "HEALTHY", "DEGRADED": "DEGRADED", "DOWN": "DOWN",
    "UNKNOWN": "UNKNOWN", "STALE": "UNKNOWN", "UNAVAILABLE": "UNKNOWN",
}

TS_RE: Final = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
REASON_RE: Final = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
REVISION_RE: Final = re.compile(r"^[A-Za-z0-9_.+-]{1,64}$")
# Privacy screen: raw network identifiers are never accepted anywhere in a packet.
PRIVACY_PATTERNS: Final = (
    re.compile(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f])"),  # MAC, separated
    re.compile(r"(?i)(?<![0-9a-f])[0-9a-f]{12}(?![0-9a-f])"),  # MAC, bare 12 hex
    re.compile(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])"),  # IPv4
    re.compile(r"(?i)(?<![0-9a-z])(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}(?![0-9a-z])"),  # IPv6 full
    re.compile(r"::"),  # IPv6 compressed
    re.compile(r"@"),  # e-mail / userinfo
)


class ProducerContractError(ValueError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ProducerContractError(code)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _parse_ts(value: Any, code: str) -> datetime:
    require(isinstance(value, str) and TS_RE.fullmatch(value) is not None, code)
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ProducerContractError(code) from exc


def _id(value: Any, code: str) -> str:
    require(isinstance(value, str) and ID_RE.fullmatch(value) is not None, code)
    return value


def _string_leaves(value: Any) -> list[str]:
    out: list[str] = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return out


def privacy_screen(value: Any) -> None:
    """Reject MAC/IP/e-mail-shaped strings anywhere. Never echoes the value."""
    for text in _string_leaves(value):
        for pattern in PRIVACY_PATTERNS:
            require(pattern.search(text) is None, "PRIVACY_IDENTIFIER_REJECTED")


def _validate_metrics(layer: str, metrics: Any) -> dict[str, Any]:
    spec = METRICS[layer]
    require(isinstance(metrics, dict) and set(metrics) == set(spec), "METRICS_FIELDS_INVALID")
    for name, rule in spec.items():
        value = metrics[name]
        code = f"METRIC_{name.upper()}_INVALID"
        if value is None:
            require((layer, name) not in NON_NULL_METRICS, code)
            continue
        kind = rule[0]
        if kind == "bool":
            require(isinstance(value, bool), code)
        elif kind == "num":
            require(_is_num(value) and rule[1] <= value <= rule[2], code)
        elif kind == "int":
            require(_is_int(value) and rule[1] <= value <= rule[2], code)
        else:
            require(isinstance(value, str) and value in rule[1], code)
    if layer == "LAN":
        expected, online = metrics["satellites_expected"], metrics["satellites_online"]
        if expected is not None and online is not None:
            require(online <= expected, "METRIC_SATELLITES_ONLINE_EXCEEDS_EXPECTED")
    return metrics


def _validate_observation(obs: Any) -> dict[str, Any]:
    require(isinstance(obs, dict) and set(obs) == OBS_KEYS, "OBSERVATION_FIELDS_INVALID")
    require(obs["schema_version"] == OBSERVATION_SCHEMA, "OBSERVATION_SCHEMA_MISMATCH")
    require(obs["layer"] in PRODUCER_LAYERS, "LAYER_INVALID")
    _id(obs["node_id"], "NODE_ID_INVALID")
    require(isinstance(obs["required"], bool), "REQUIRED_NOT_BOOLEAN")
    prov = obs["provenance"]
    require(isinstance(prov, dict) and set(prov) == PROVENANCE_KEYS, "PROVENANCE_INVALID")
    _id(prov["producer_id"], "PROVENANCE_INVALID")
    require(prov["producer_kind"] in PRODUCER_KINDS, "PROVENANCE_INVALID")
    require(isinstance(prov["source_revision"], str)
            and REVISION_RE.fullmatch(prov["source_revision"]) is not None, "PROVENANCE_INVALID")
    require(prov["collection_mode"] in COLLECTION_MODES, "PROVENANCE_INVALID")
    _parse_ts(obs["observed_at"], "OBSERVED_AT_INVALID")
    limit = obs["freshness_limit_seconds"]
    require(_is_int(limit) and 1 <= limit <= 86400, "FRESHNESS_LIMIT_INVALID")
    require(obs["quality"] in QUALITIES, "QUALITY_INVALID")
    require(obs["state"] in STATES, "STATE_INVALID")
    reasons = obs["reason_codes"]
    require(isinstance(reasons, list) and len(reasons) <= 16
            and all(isinstance(r, str) and REASON_RE.fullmatch(r) for r in reasons)
            and len(reasons) == len(set(reasons)), "REASON_CODES_INVALID")
    require(obs["privacy_class"] == PRIVACY_CLASS, "PRIVACY_CLASS_INVALID")
    require(obs["authority"] == AUTHORITY_IN, "AUTHORITY_INVALID")
    _validate_metrics(obs["layer"], obs["metrics"])
    return obs


def _validate_adapter_node(node: Any) -> dict[str, Any]:
    require(isinstance(node, dict) and set(node) == ADAPTER_NODE_KEYS, "ADAPTER_NODE_INVALID")
    _id(node["id"], "ADAPTER_NODE_INVALID")
    require(node["layer"] in ADAPTER_LAYERS, "ADAPTER_LAYER_NOT_PERMITTED")
    require(node["status"] in POSTURE_STATUSES, "ADAPTER_NODE_INVALID")
    require(isinstance(node["required"], bool), "ADAPTER_NODE_INVALID")
    for key in ("sample_age_seconds", "freshness_limit_seconds"):
        require(_is_int(node[key]) and 0 <= node[key] <= 86400, "ADAPTER_NODE_INVALID")
    deps = node["depends_on"]
    require(isinstance(deps, list) and len(deps) <= 16
            and all(isinstance(d, str) and ID_RE.fullmatch(d) for d in deps), "ADAPTER_NODE_INVALID")
    return node


def classify(obs: dict[str, Any], evaluated_at: datetime) -> tuple[int, str, list[str]]:
    """Return (age_seconds, effective_state, evaluator reason codes) for one observation."""
    reasons: list[str] = []
    observed_at = _parse_ts(obs["observed_at"], "OBSERVED_AT_INVALID")
    age = int((evaluated_at - observed_at).total_seconds())
    state = obs["state"]
    quality = obs["quality"]
    primary = obs["metrics"][PRIMARY_REACHABILITY[obs["layer"]]]

    if age < 0:
        return age, "UNKNOWN", ["FUTURE_OBSERVATION_TIMESTAMP"]
    if age > obs["freshness_limit_seconds"]:
        return age, "STALE", ["STALE_EVIDENCE"]
    if state == "STALE":
        return age, "STALE", ["PRODUCER_REPORTED_STALE"]
    if state == "UNAVAILABLE":
        return age, "UNAVAILABLE", ["PRODUCER_SOURCE_UNAVAILABLE"]
    if state == "UNKNOWN":
        return age, "UNKNOWN", ["PRODUCER_REPORTED_UNKNOWN"]
    if quality == "UNTRUSTED":
        return age, "UNKNOWN", ["QUALITY_UNTRUSTED"]
    if (state in ("UP", "DEGRADED") and primary is False) or (state == "DOWN" and primary is True):
        return age, "UNKNOWN", ["METRIC_STATE_CONTRADICTION"]
    effective = state
    if quality == "SUSPECT":
        if state == "DOWN":
            effective = "UNKNOWN"
            reasons.append("QUALITY_SUSPECT_OUTAGE_NOT_ASSERTED")
        elif state == "UP":
            effective = "DEGRADED"
            reasons.append("QUALITY_SUSPECT_HEALTH_CAPPED")
    return age, effective, reasons


def _project(evaluated: list[dict[str, Any]], layer: str, metric: str) -> bool | None:
    values = [
        row["_metrics"][metric] for row in evaluated
        if row["layer"] == layer and row["quality"] == "GOOD"
        and row["effective_state"] in ("UP", "DEGRADED", "DOWN")
        and row["_metrics"][metric] is not None
    ]
    if not values:
        return None
    return False if False in values else True


def evaluate(payload: Any) -> dict[str, Any]:
    require(isinstance(payload, dict), "ROOT_NOT_OBJECT")
    require(set(payload) == TOP_KEYS, "UNSUPPORTED_FIELDS")
    require(payload["schema_version"] == INPUT_SCHEMA, "SCHEMA_MISMATCH")
    # Privacy screen runs before any other check so an identifier leak is always
    # reported as such (and never echoed), regardless of other defects.
    privacy_screen(payload)
    evaluated_at = _parse_ts(payload["evaluated_at"], "EVALUATED_AT_INVALID")

    observations = payload["observations"]
    require(isinstance(observations, list) and 1 <= len(observations) <= MAX_NODES, "OBSERVATIONS_INVALID")
    adapter_nodes = payload["adapter_nodes"]
    require(isinstance(adapter_nodes, list) and len(adapter_nodes) <= 16, "ADAPTER_NODES_INVALID")
    require(len(observations) + len(adapter_nodes) <= MAX_NODES, "TOO_MANY_NODES")
    for obs in observations:
        _validate_observation(obs)
    for node in adapter_nodes:
        _validate_adapter_node(node)

    ids = [o["node_id"] for o in observations] + [n["id"] for n in adapter_nodes]
    require(len(ids) == len(set(ids)), "DUPLICATE_NODE_ID")
    producer_ids = {o["node_id"] for o in observations}

    topology = payload["topology"]
    require(isinstance(topology, dict) and len(topology) <= MAX_NODES, "TOPOLOGY_INVALID")
    for key, deps in topology.items():
        _id(key, "TOPOLOGY_INVALID")
        require(key in producer_ids, "TOPOLOGY_TARGET_NOT_PRODUCER")
        require(isinstance(deps, list) and len(deps) <= 16
                and all(isinstance(d, str) and ID_RE.fullmatch(d) for d in deps), "TOPOLOGY_INVALID")

    rows: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = []
    for obs in sorted(observations, key=lambda o: o["node_id"]):
        age, effective, reasons = classify(obs, evaluated_at)
        posture_status = POSTURE_OF[effective]
        rows.append({
            "node_id": obs["node_id"],
            "layer": obs["layer"],
            "producer_id": obs["provenance"]["producer_id"],
            "reported_state": obs["state"],
            "quality": obs["quality"],
            "age_seconds": age,
            "effective_state": effective,
            "posture_status": posture_status,
            "reason_codes": sorted(set(reasons) | set(obs["reason_codes"])),
            "_metrics": obs["metrics"],
        })
        nodes.append({
            "id": obs["node_id"],
            "layer": obs["layer"],
            "status": posture_status,
            "required": obs["required"],
            "sample_age_seconds": min(max(age, 0), 86400),
            "freshness_limit_seconds": obs["freshness_limit_seconds"],
            "depends_on": list(topology.get(obs["node_id"], [])),
        })
    for node in sorted(adapter_nodes, key=lambda n: n["id"]):
        nodes.append({key: (list(node[key]) if key == "depends_on" else node[key]) for key in sorted(node)})

    posture_input = {"schema_version": POSTURE_SCHEMA, "nodes": nodes}
    try:
        posture = evaluate_posture(posture_input)
    except NetworkError as exc:
        raise ProducerContractError(f"POSTURE_{exc}") from exc

    populated = sorted({n["layer"] for n in nodes})
    wan_down = any(r["layer"] == "WAN" and r["effective_state"] == "DOWN" for r in rows)
    lan_down = any(r["layer"] == "LAN" and r["effective_state"] == "DOWN" for r in rows)
    projection = {
        "lan_gateway_reachable": _project(rows, "LAN", "gateway_reachable"),
        "dns_ok": _project(rows, "DNS", "resolution_ok"),
        "mqtt_reachable": _project(rows, "MQTT", "broker_reachable"),
    }
    public_rows = [{k: v for k, v in row.items() if k != "_metrics"} for row in rows]
    not_healthy = sorted(
        f"{r['layer']}:{r['node_id']}:{r['effective_state']}" for r in public_rows if r["effective_state"] != "UP"
    ) + sorted(
        f"{n['layer']}:{n['id']}:{posture['effective_status'][n['id']]}"
        for n in nodes if n["layer"] in ADAPTER_LAYERS and posture["effective_status"][n["id"]] != "HEALTHY"
    )
    return {
        "schema_version": OUTPUT_SCHEMA,
        "evaluated_at": payload["evaluated_at"],
        "observations": public_rows,
        "network_posture_input": posture_input,
        "posture": posture,
        "layers_populated": populated,
        "layers_not_populated": [layer for layer in ALL_LAYERS if layer not in populated],
        "reliability_signal_projection": projection,
        "network_weather": {
            "headline": posture["posture"],
            "not_healthy": not_healthy,
            "local_first_fallback_relevant": wan_down and not lan_down,
        },
        "privacy_screen": "PASS_NO_RAW_NETWORK_IDENTIFIERS",
        "network_probes_performed": False,
        "mutation_authorized": False,
        "physical_actions": False,
        "authority": AUTHORITY_OUT,
    }


def _reject_constant(_: str) -> Any:
    raise ProducerContractError("NON_FINITE_NUMBER")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packet", type=Path)
    args = parser.parse_args(argv)
    try:
        raw = args.packet.read_bytes()
        require(len(raw) <= MAX_INPUT_BYTES, "INPUT_TOO_LARGE")
        payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        print(json.dumps(evaluate(payload), sort_keys=True))
        return 0
    except RecursionError:
        error = "INPUT_NESTING_TOO_DEEP"
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        error = type(exc).__name__.upper()
    except ProducerContractError as exc:
        error = str(exc)
    print(json.dumps({"schema_version": OUTPUT_SCHEMA, "ok": False, "error": error}, sort_keys=True))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
