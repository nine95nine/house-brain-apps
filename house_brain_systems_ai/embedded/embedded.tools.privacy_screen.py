#!/usr/bin/env python3
"""Reusable House Brain privacy-screen library (Initiative #60, machine enforcement R2).

Composes, without modifying, three accepted surfaces:

* Privacy Data Policy R1 (``tools/evaluate_privacy_data_policy.py``) — the five data classes and
  the class/destination/purpose -> ALLOW/REDACT/DENY decision. This library never re-implements
  that decision; every decision below is the R1 ``decision()`` function.
* the #73 network privacy screen (``tools/evaluate_network_producer_observations.privacy_screen``)
  — its MAC/IPv4/IPv6/e-mail patterns are imported and reused as the NETWORK_IDENTIFIER /
  PERSON_IDENTIFIER content detectors, and ``network_privacy_screen`` is the accepted function
  itself, so evaluators that already call it keep byte-identical behaviour.
* the repository secret-boundary rules (``tools/validate_repository_secret_boundaries.RULES``) —
  reused as CREDENTIAL content detectors.

What this library adds:

1. a central field-class map (``contracts/house_brain_privacy_data_classes.v1.json``) plus a
   JSON-schema indexer that turns every contract field into a stable field id and a data class;
2. content detectors extended with credential, coordinate, phone and restricted-HA-entity shapes;
3. redaction helpers (token masking, keyed pseudonymisation, coordinate/IPv4 generalisation);
4. evaluator adapters: ``build_policy_packet`` (payload -> R1 input packet), ``minimize_for_destination``
   (drop DENY, redact REDACT, fail closed on unclassified paths, mask residual free text) and
   ``screen_outbound`` (metadata-only attestation an evaluator can attach to its result).

Everything is deterministic and offline. No clock read, no network, no persistence, no control
authority. Findings never echo the offending value.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import ipaddress
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Iterable, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.evaluate_network_producer_observations import (  # noqa: E402
    PRIVACY_PATTERNS as NETWORK_PRIVACY_PATTERNS,
    ProducerContractError,
    privacy_screen as network_privacy_screen,
)
from tools.evaluate_privacy_data_policy import (  # noqa: E402
    CLASSES as POLICY_CLASSES,
    DESTINATIONS as POLICY_DESTINATIONS,
    PURPOSES as POLICY_PURPOSES,
    ID_RE as POLICY_FIELD_ID_RE,
    INPUT_SCHEMA as POLICY_INPUT_SCHEMA,
    decision as policy_decision,
    evaluate as policy_evaluate,
)
from tools.validate_repository_secret_boundaries import RULES as SECRET_BOUNDARY_RULES  # noqa: E402

__all__ = [
    "CLASS_ORDER", "CLASS_RANK", "CLASS_MAP_PATH", "CONTRACTS_DIR", "PrivacyScreenError",
    "ProducerContractError", "network_privacy_screen", "scan_content", "screen_content",
    "mask_content", "redaction_token", "pseudonymize", "generalize_coordinate", "generalize_ipv4",
    "load_class_map", "SchemaIndex", "FieldInfo", "field_categories", "max_class",
    "retention_hint", "fabric_to_policy_class", "classify_payload", "build_policy_packet",
    "minimize_for_destination", "screen_outbound",
]

CLASS_MAP_SCHEMA: Final = "house_brain_privacy_data_classes.v1"
ATTESTATION_SCHEMA: Final = "house_brain_privacy_screen_attestation.v1"
CLASS_MAP_PATH: Final = ROOT / "contracts" / "house_brain_privacy_data_classes.v1.json"
CONTRACTS_DIR: Final = ROOT / "contracts"

# Ordered least -> most restrictive. Must equal the accepted R1 class set exactly.
CLASS_ORDER: Final = ("PUBLIC", "HOUSEHOLD", "PRIVATE", "SENSITIVE", "SECRET")
CLASS_RANK: Final = {name: rank for rank, name in enumerate(CLASS_ORDER)}
if frozenset(CLASS_ORDER) != frozenset(POLICY_CLASSES):  # pragma: no cover - import-time guard
    raise RuntimeError("privacy class order drifted from Privacy Data Policy R1")

LOCAL_DESTINATIONS: Final = frozenset({"LOCAL_PROCESSOR", "HOUSE_BRAIN_LOCAL"})
EGRESS_DESTINATIONS: Final = frozenset({"AI_PROVIDER", "PUBLIC_EXPORT"})
CATEGORIES: Final = (
    "CREDENTIAL", "NETWORK_IDENTIFIER", "PERSON_IDENTIFIER", "LOCATION", "PRECISE_LOCATION",
    "PRESENCE", "HEALTH", "CAMERA_AUDIO",
)
KINDS: Final = frozenset({
    "STRING", "TIMESTAMP", "ENUM", "CONST", "NUMBER", "BOOLEAN", "NULL", "OBJECT", "ARRAY",
    "OPAQUE", "REF",
})
ARRAY_SEGMENT: Final = "-"
MAP_SEGMENT: Final = ":"
DEFS_PREFIX: Final = "defs:"
MAX_DEPTH: Final = 64
MAX_NODES: Final = 200_000
SAFE_KEY_RE: Final = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
TIMESTAMP_PATTERN_HINTS: Final = ("[0-9]{4}-[0-9]{2}-[0-9]{2}", r"\d{4}-\d{2}-\d{2}")
TIMESTAMP_FORMATS: Final = frozenset({"date-time", "date", "time"})


class PrivacyScreenError(ValueError):
    """Machine-safe error; the message is always a reason code, never a value."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise PrivacyScreenError(code)


# ---------------------------------------------------------------------------------------------
# Content detectors (reuse #73 + secret-boundary rules, then extend)
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Detector:
    detector_id: str
    category: str
    pattern: re.Pattern[str]


def _network_detectors() -> tuple[Detector, ...]:
    # Same order and patterns as the accepted #73 screen; only labelled here.
    labels = ("NET_MAC_SEPARATED", "NET_MAC_BARE", "NET_IPV4", "NET_IPV6_FULL", "NET_IPV6_COMPRESSED",
              "NET_AT_SIGN")
    categories = ("NETWORK_IDENTIFIER",) * 5 + ("PERSON_IDENTIFIER",)
    if len(NETWORK_PRIVACY_PATTERNS) != len(labels):  # pragma: no cover - drift guard
        raise RuntimeError("#73 privacy pattern set changed; relabel detectors")
    return tuple(Detector(i, c, p) for i, c, p in zip(labels, categories, NETWORK_PRIVACY_PATTERNS))


def _credential_detectors() -> tuple[Detector, ...]:
    reused = tuple(Detector("CRED_" + rule.rule_id, "CREDENTIAL", rule.pattern) for rule in SECRET_BOUNDARY_RULES)
    extra = (
        # JSON Web Token: three base64url segments, the first a JSON header ("eyJ").
        Detector("CRED_JWT", "CREDENTIAL",
                 re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
        Detector("CRED_BEARER", "CREDENTIAL", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
        Detector("CRED_KEY_VALUE", "CREDENTIAL",
                 re.compile(r"(?i)\b(?:password|passwd|passphrase|secret|token|api[_-]?key|pin)\s*[:=]\s*\S{4,}")),
    )
    return reused + extra


def _extended_detectors() -> tuple[Detector, ...]:
    return (
        Detector("LOC_COORDINATE_PAIR", "PRECISE_LOCATION",
                 re.compile(r"(?<![0-9.])[-+]?[0-9]{1,2}\.[0-9]{4,}\s*,\s*[-+]?[0-9]{1,3}\.[0-9]{4,}(?![0-9])")),
        Detector("PERSON_PHONE_NANP", "PERSON_IDENTIFIER",
                 re.compile(r"(?<![0-9])(?:\+?1[\s.-]?)?\(?[2-9][0-9]{2}\)?[\s.-][0-9]{3}[\s.-][0-9]{4}(?![0-9])")),
        Detector("PERSON_PHONE_E164", "PERSON_IDENTIFIER", re.compile(r"(?<![0-9A-Za-z])\+[1-9][0-9]{7,14}(?![0-9])")),
        Detector("HA_PRESENCE_ENTITY", "PRESENCE", re.compile(r"\b(?:person|device_tracker)\.[a-z0-9_]{1,64}\b")),
        Detector("HA_LOCATION_ENTITY", "LOCATION", re.compile(r"\b(?:zone|geo_location)\.[a-z0-9_]{1,64}\b")),
        Detector("HA_CAMERA_ENTITY", "CAMERA_AUDIO", re.compile(r"\b(?:camera|image)\.[a-z0-9_]{1,64}\b")),
    )


DETECTORS: Final = _network_detectors() + _credential_detectors() + _extended_detectors()


def _iter_leaves(value: Any) -> Iterator[tuple[tuple[str, ...], str, bool]]:
    """Yield (path, text, is_key) for every string leaf and dict key, iteratively (no recursion)."""
    stack: list[tuple[tuple[str, ...], Any, int]] = [((), value, 0)]
    seen = 0
    while stack:
        path, item, depth = stack.pop()
        seen += 1
        _require(seen <= MAX_NODES and depth <= MAX_DEPTH, "PRIVACY_SCREEN_INPUT_TOO_LARGE")
        if isinstance(item, str):
            yield path, item, False
        elif isinstance(item, dict):
            for index, (key, child) in enumerate(item.items()):
                key_text = key if isinstance(key, str) else str(key)
                yield path, key_text, True
                segment = key_text if SAFE_KEY_RE.fullmatch(key_text) and not _detect(key_text) else f"<key:{index}>"
                stack.append((path + (segment,), child, depth + 1))
        elif isinstance(item, (list, tuple)):
            for index, child in enumerate(item):
                stack.append((path + (str(index),), child, depth + 1))


def _detect(text: str) -> list[Detector]:
    return [d for d in DETECTORS if d.pattern.search(text) is not None]


def scan_content(value: Any) -> list[dict[str, str]]:
    """Return metadata-only findings: JSON-pointer-like path, category, detector. Never the value."""
    findings: list[dict[str, str]] = []
    for path, text, is_key in _iter_leaves(value):
        for det in _detect(text):
            findings.append({
                "path": "/" + "/".join(path),
                "location": "KEY" if is_key else "VALUE",
                "category": det.category,
                "detector": det.detector_id,
            })
    findings.sort(key=lambda f: (f["path"], f["location"], f["category"], f["detector"]))
    return findings


def screen_content(value: Any, allowed_categories: Iterable[str] = ()) -> None:
    """Raise PrivacyScreenError('PRIVACY_CONTENT_REJECTED:<CATEGORY>') on the first disallowed hit."""
    allowed = frozenset(allowed_categories)
    _require(allowed <= frozenset(CATEGORIES), "PRIVACY_CATEGORY_INVALID")
    for finding in scan_content(value):
        if finding["category"] not in allowed:
            raise PrivacyScreenError("PRIVACY_CONTENT_REJECTED:" + finding["category"])


# ---------------------------------------------------------------------------------------------
# Redaction helpers
# ---------------------------------------------------------------------------------------------

def redaction_token(label: str) -> str:
    _require(label in CLASS_RANK or label in CATEGORIES, "REDACTION_LABEL_INVALID")
    return f"[REDACTED:{label}]"


def mask_content(text: str) -> tuple[str, int]:
    """Replace every detector match in free text with a category token. Returns (masked, count)."""
    _require(isinstance(text, str), "MASK_INPUT_NOT_STRING")
    count = 0
    for det in DETECTORS:
        text, n = det.pattern.subn(redaction_token(det.category), text)
        count += n
    return text, count


def pseudonymize(value: str, key: bytes, *, prefix: str = "pn") -> str:
    """Keyed, deterministic, non-reversible pseudonym (HMAC-SHA-256, 64-bit output).

    The key is caller-supplied at runtime (never a repository literal) and must be >= 16 bytes.
    Without the key the pseudonym cannot be recomputed from a guessed value.
    """
    _require(isinstance(value, str) and value != "", "PSEUDONYM_INPUT_INVALID")
    _require(isinstance(key, (bytes, bytearray)) and len(key) >= 16, "PSEUDONYM_KEY_TOO_SHORT")
    _require(re.fullmatch(r"[a-z]{1,8}", prefix) is not None, "PSEUDONYM_PREFIX_INVALID")
    digest = hmac.new(bytes(key), value.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{prefix}_{digest[:16]}"


def generalize_coordinate(value: float, *, decimals: int = 1, latitude: bool = True) -> float:
    """Round a coordinate to ``decimals`` places (1 decimal ~ 11 km). decimals must be 0..2."""
    _require(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value),
             "COORDINATE_INVALID")
    limit = 90 if latitude else 180
    _require(-limit <= value <= limit, "COORDINATE_OUT_OF_RANGE")
    _require(isinstance(decimals, int) and not isinstance(decimals, bool) and 0 <= decimals <= 2,
             "COORDINATE_PRECISION_NOT_ALLOWED")
    return round(float(value), decimals)


def generalize_ipv4(text: str, *, prefix: int = 24) -> str:
    """Return the enclosing network (e.g. 192.0.2.0/24) of an IPv4 address; prefix 8..24 only."""
    _require(isinstance(prefix, int) and not isinstance(prefix, bool) and 8 <= prefix <= 24, "IPV4_PREFIX_NOT_ALLOWED")
    try:
        address = ipaddress.IPv4Address(text)
    except (ipaddress.AddressValueError, TypeError, ValueError) as exc:
        raise PrivacyScreenError("IPV4_INVALID") from exc
    return str(ipaddress.IPv4Network(f"{address}/{prefix}", strict=False))


# ---------------------------------------------------------------------------------------------
# Class map
# ---------------------------------------------------------------------------------------------

def _is_class(value: Any) -> bool:
    return isinstance(value, str) and value in CLASS_RANK


def load_class_map(path: Path | None = None, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load and structurally validate the central class map. Raises PrivacyScreenError."""
    if data is None:
        try:
            data = json.loads((path or CLASS_MAP_PATH).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PrivacyScreenError("CLASS_MAP_UNREADABLE") from exc
    _require(isinstance(data, dict), "CLASS_MAP_NOT_OBJECT")
    _require(data.get("schema_version") == CLASS_MAP_SCHEMA, "CLASS_MAP_SCHEMA_MISMATCH")
    classes = data.get("classes")
    _require(isinstance(classes, list) and [c.get("id") for c in classes if isinstance(c, dict)] == list(CLASS_ORDER),
             "CLASS_MAP_CLASSES_INVALID")
    for entry in classes:
        _require(isinstance(entry.get("retention"), dict), "CLASS_MAP_RETENTION_MISSING")
        _require(isinstance(entry.get("redaction"), str), "CLASS_MAP_REDACTION_MISSING")
    crosswalk = data.get("ai_fabric_crosswalk")
    _require(isinstance(crosswalk, dict) and crosswalk, "CLASS_MAP_CROSSWALK_INVALID")
    for label, target in crosswalk.items():
        _require(isinstance(label, str) and _is_class(target), "CLASS_MAP_CROSSWALK_INVALID")
    cats = data.get("restricted_categories")
    _require(isinstance(cats, dict) and sorted(cats) == sorted(CATEGORIES), "CLASS_MAP_CATEGORIES_INVALID")
    for name, cat in cats.items():
        _require(isinstance(cat, dict), "CLASS_MAP_CATEGORY_INVALID")
        _require(_is_class(cat.get("minimum_class")), "CLASS_MAP_CATEGORY_MINIMUM_INVALID")
        names = cat.get("name_tokens")
        _require(isinstance(names, list) and names and all(isinstance(n, str) and re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", n)
                                                           for n in names), "CLASS_MAP_CATEGORY_TOKENS_INVALID")
        carriers = cat.get("carrier_kinds")
        _require(isinstance(carriers, list) and carriers and set(carriers) <= KINDS, "CLASS_MAP_CATEGORY_CARRIERS_INVALID")
        _require(isinstance(cat.get("match_parent_name", False), bool), "CLASS_MAP_CATEGORY_INVALID")
    generic = data.get("generic_leaf_names")
    _require(isinstance(generic, list) and all(isinstance(g, str) for g in generic), "CLASS_MAP_GENERIC_INVALID")
    contracts = data.get("contracts")
    _require(isinstance(contracts, dict), "CLASS_MAP_CONTRACTS_INVALID")
    for ref, entry in contracts.items():
        _require(isinstance(ref, str) and isinstance(entry, dict), "CLASS_MAP_CONTRACT_INVALID")
        _require(entry.get("role") in {"INPUT", "OUTPUT", "RECORD", "CONFIG"}, "CLASS_MAP_ROLE_INVALID")
        _require(entry.get("destination") in POLICY_DESTINATIONS, "CLASS_MAP_DESTINATION_INVALID")
        _require(entry.get("purpose") in POLICY_PURPOSES, "CLASS_MAP_PURPOSE_INVALID")
        _require(_is_class(entry.get("default_class")), "CLASS_MAP_DEFAULT_CLASS_INVALID")
        fields = entry.get("fields", {})
        _require(isinstance(fields, dict), "CLASS_MAP_FIELDS_INVALID")
        for fid, spec in fields.items():
            _require(isinstance(fid, str) and POLICY_FIELD_ID_RE.fullmatch(fid) is not None, "CLASS_MAP_FIELD_ID_INVALID")
            _require(isinstance(spec, dict) and _is_class(spec.get("class")), "CLASS_MAP_FIELD_CLASS_INVALID")
            _require(isinstance(spec.get("rationale"), str) and spec["rationale"].strip(), "CLASS_MAP_FIELD_RATIONALE_MISSING")
        dismissals = entry.get("dismissals", {})
        _require(isinstance(dismissals, dict), "CLASS_MAP_DISMISSALS_INVALID")
        for fid, spec in dismissals.items():
            _require(isinstance(spec, dict) and isinstance(spec.get("categories"), list)
                     and spec["categories"] and set(spec["categories"]) <= set(CATEGORIES), "CLASS_MAP_DISMISSAL_INVALID")
            _require(isinstance(spec.get("rationale"), str) and spec["rationale"].strip(), "CLASS_MAP_DISMISSAL_RATIONALE_MISSING")
    domains = data.get("restricted_ha_domains")
    _require(isinstance(domains, dict) and all(isinstance(domains.get(k), list) and all(isinstance(d, str) for d in domains[k])
                                               for k in ("required", "advisory")), "CLASS_MAP_HA_DOMAINS_INVALID")
    surfaces = data.get("ai_surfaces")
    _require(isinstance(surfaces, list), "CLASS_MAP_SURFACES_INVALID")
    for surface in surfaces:
        _require(isinstance(surface, dict) and all(isinstance(surface.get(k), str) and surface[k]
                                                   for k in ("contract", "kind", "allowed_pointer", "prohibited_pointer")),
                 "CLASS_MAP_SURFACE_INVALID")
        _require(surface.get("destination") in POLICY_DESTINATIONS and surface.get("purpose") in POLICY_PURPOSES,
                 "CLASS_MAP_SURFACE_INVALID")
    egress = data.get("declared_egress_paths")
    _require(isinstance(egress, list), "CLASS_MAP_EGRESS_INVALID")
    for path in egress:
        _require(isinstance(path, dict) and all(isinstance(path.get(k), str) and path[k] for k in ("id", "kind", "status", "purpose"))
                 and path.get("policy_destination") in POLICY_DESTINATIONS | {"OUT_OF_R1_SCOPE"}
                 and isinstance(path.get("evidence"), list) and isinstance(path.get("carries_classes"), list),
                 "CLASS_MAP_EGRESS_INVALID")
    _require(isinstance(data.get("exceptions"), list), "CLASS_MAP_EXCEPTIONS_INVALID")
    return data


def retention_hint(data_class: str, class_map: dict[str, Any] | None = None) -> dict[str, Any]:
    _require(_is_class(data_class), "DATA_CLASS_INVALID")
    cmap = class_map or load_class_map()
    entry = next(c for c in cmap["classes"] if c["id"] == data_class)
    return {"data_class": data_class, **copy.deepcopy(entry["retention"]), "redaction": entry["redaction"]}


def fabric_to_policy_class(label: str, class_map: dict[str, Any] | None = None) -> str:
    cmap = class_map or load_class_map()
    _require(label in cmap["ai_fabric_crosswalk"], "AI_FABRIC_LABEL_UNKNOWN")
    return cmap["ai_fabric_crosswalk"][label]


def max_class(classes: Iterable[str]) -> str | None:
    ranked = [CLASS_RANK[c] for c in classes]
    return CLASS_ORDER[max(ranked)] if ranked else None


# ---------------------------------------------------------------------------------------------
# JSON-schema indexer
# ---------------------------------------------------------------------------------------------

@dataclass
class FieldInfo:
    field_id: str
    name: str
    parent_name: str
    kinds: set[str] = field(default_factory=set)
    ref_targets: set[str] = field(default_factory=set)
    free_text: bool = False  # a string with no enum/const/pattern/format: can carry arbitrary text


def _node_kinds(node: dict[str, Any]) -> set[str]:
    kinds: set[str] = set()
    if "const" in node:
        return {"CONST"}
    types = node.get("type")
    type_list = [types] if isinstance(types, str) else list(types) if isinstance(types, list) else []
    if "enum" in node:
        values = [v for v in node["enum"] if v is not None]
        kinds.add("ENUM" if len(values) != 1 else "CONST")
        if None in node["enum"]:
            kinds.add("NULL")
        return kinds
    for t in type_list:
        if t == "string":
            pattern = node.get("pattern", "")
            if node.get("format") in TIMESTAMP_FORMATS or any(h in pattern for h in TIMESTAMP_PATTERN_HINTS):
                kinds.add("TIMESTAMP")
            else:
                kinds.add("STRING")
        elif t in ("integer", "number"):
            kinds.add("NUMBER")
        elif t == "boolean":
            kinds.add("BOOLEAN")
        elif t == "null":
            kinds.add("NULL")
        elif t == "object":
            closed = node.get("additionalProperties") is False or node.get("unevaluatedProperties") is False
            has_shape = bool(node.get("properties") or node.get("patternProperties")) or isinstance(node.get("additionalProperties"), dict)
            kinds.add("OBJECT" if (closed or has_shape) else "OPAQUE")
        elif t == "array":
            kinds.add("ARRAY" if isinstance(node.get("items"), (dict, list)) or node.get("prefixItems") else "OPAQUE")
    if not type_list and "$ref" in node:
        kinds.add("REF")
    if not kinds:
        structural = any(k in node for k in ("properties", "items", "oneOf", "anyOf", "allOf", "not", "patternProperties"))
        kinds.add("OBJECT" if structural else "OPAQUE")
    return kinds


class SchemaIndex:
    """Index every field of every JSON-schema contract (top-level and embedded)."""

    def __init__(self, contracts_dir: Path | None = None) -> None:
        self.contracts_dir = contracts_dir or CONTRACTS_DIR
        self.documents: dict[str, Any] = {}
        for path in sorted(self.contracts_dir.glob("*.json")):
            try:
                self.documents[path.name] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise PrivacyScreenError("CONTRACT_UNREADABLE:" + path.name) from exc
        self.schemas: dict[str, dict[str, Any]] = {}
        self.embedded_parents: dict[str, dict[str, Any]] = {}
        for name, doc in self.documents.items():
            if name.endswith(".schema.json") and isinstance(doc, dict):
                self.schemas[name] = doc
            for pointer, embedded, parent in _embedded_schemas(doc):
                self.schemas[f"{name}#{pointer}"] = embedded
                self.embedded_parents[f"{name}#{pointer}"] = parent
        self._fields: dict[str, dict[str, FieldInfo]] = {}

    def fields(self, ref: str) -> dict[str, FieldInfo]:
        if ref not in self._fields:
            _require(ref in self.schemas, "CONTRACT_REF_UNKNOWN")
            out: dict[str, FieldInfo] = {}
            self._walk(ref, self.schemas[ref], (), out, "", "", frozenset())
            self._fields[ref] = out
        return self._fields[ref]

    def _resolve_local(self, ref_owner: str, pointer: str) -> dict[str, Any] | None:
        node: Any = self.schemas.get(ref_owner)
        for part in [p for p in pointer.split("/") if p]:
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node if isinstance(node, dict) else None

    def _walk(self, ref: str, node: Any, path: tuple[str, ...], out: dict[str, FieldInfo], name: str,
              parent_name: str, active_refs: frozenset[str]) -> None:
        _require(len(path) <= MAX_DEPTH, "SCHEMA_TOO_DEEP")
        if not isinstance(node, dict):
            return
        if path:
            fid = "/".join(path)
            info = out.setdefault(fid, FieldInfo(fid, name, parent_name))
            node_kinds = _node_kinds(node)
            info.kinds |= node_kinds
            if "STRING" in node_kinds and "pattern" not in node and "format" not in node:
                info.free_text = True
            if "OPAQUE" in node_kinds:
                info.free_text = True
        target = node.get("$ref")
        if isinstance(target, str):
            if target.startswith("#"):
                key = f"{ref}{target}"
                resolved = self._resolve_local(ref, target[1:])
                if resolved is not None and key not in active_refs:
                    if path:
                        out["/".join(path)].kinds.discard("REF")
                    self._walk(ref, resolved, path, out, name, parent_name, active_refs | {key})
            elif path:
                out["/".join(path)].ref_targets.add(target)
        for key, child in (node.get("properties") or {}).items():
            self._walk(ref, child, path + (key,), out, key, name or parent_name, active_refs)
        for child in (node.get("patternProperties") or {}).values():
            self._walk(ref, child, path + (MAP_SEGMENT,), out, name, parent_name, active_refs)
        if isinstance(node.get("additionalProperties"), dict):
            self._walk(ref, node["additionalProperties"], path + (MAP_SEGMENT,), out, name, parent_name, active_refs)
        items = node.get("items")
        if isinstance(items, dict):
            self._walk(ref, items, path + (ARRAY_SEGMENT,), out, name, parent_name, active_refs)
        elif isinstance(items, list):
            for child in items:
                self._walk(ref, child, path + (ARRAY_SEGMENT,), out, name, parent_name, active_refs)
        for child in node.get("prefixItems") or []:
            self._walk(ref, child, path + (ARRAY_SEGMENT,), out, name, parent_name, active_refs)
        for key in ("oneOf", "anyOf", "allOf"):
            for child in node.get(key) or []:
                self._walk(ref, child, path, out, name, parent_name, active_refs)
        for key in ("if", "then", "else", "not"):
            if isinstance(node.get(key), dict) and key != "not":
                self._walk(ref, node[key], path, out, name, parent_name, active_refs)


def _embedded_schemas(doc: Any) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """Find input_schema/output_schema objects inside non-schema contracts (e.g. AI tool catalogs).

    Pointers use the sibling ``name``/``id`` of list elements when present so they stay stable
    when a catalog is reordered.
    """
    found: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    stack: list[tuple[tuple[str, ...], Any]] = [((), doc)]
    while stack:
        path, node = stack.pop()
        if isinstance(node, dict):
            for key, child in node.items():
                if key in ("input_schema", "output_schema") and isinstance(child, dict):
                    found.append(("/".join(path + (key,)), child, node))
                else:
                    stack.append((path + (str(key),), child))
        elif isinstance(node, list):
            for index, child in enumerate(node):
                label = str(index)
                if isinstance(child, dict):
                    for id_key in ("name", "id"):
                        if isinstance(child.get(id_key), str) and SAFE_KEY_RE.fullmatch(child[id_key]):
                            label = child[id_key]
                            break
                stack.append((path + (label,), child))
    found.sort(key=lambda item: item[0])
    return found


def _tokens(name: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return [t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t]


def _token_eq(token: str, wanted: str) -> bool:
    # Simple plural tolerance: recipients ~ recipient, addresses ~ address.
    return token == wanted or token == wanted + "s" or token == wanted + "es"


def _contains(tokens: list[str], compound: list[str]) -> bool:
    n = len(compound)
    return any(all(_token_eq(tokens[i + j], compound[j]) for j in range(n)) for i in range(len(tokens) - n + 1))


def field_categories(info: FieldInfo, class_map: dict[str, Any]) -> list[str]:
    """Restricted categories this field could carry, by name tokens AND carrier kind."""
    tokens = _tokens(info.name)
    if info.name.lower() in set(class_map["generic_leaf_names"]):
        tokens = _tokens(info.parent_name) + tokens
    hits = []
    for cat in CATEGORIES:
        spec = class_map["restricted_categories"][cat]
        if not (info.kinds & set(spec["carrier_kinds"])):
            continue
        candidates = tokens
        if spec.get("match_parent_name") and info.parent_name:
            # e.g. occupancy/{owner_phone: HOME|AWAY}: the container names the category.
            candidates = _tokens(info.parent_name) + ["|"] + tokens
        if any(_contains(candidates, token.split("_")) for token in spec["name_tokens"]):
            hits.append(cat)
    return hits


def resolve_class(entry: dict[str, Any], field_id: str) -> tuple[str, str]:
    """Explicit field entry > nearest mapped ancestor > contract default. Returns (class, source)."""
    fields = entry.get("fields", {})
    if field_id in fields:
        return fields[field_id]["class"], "FIELD"
    parts = field_id.split("/")
    for cut in range(len(parts) - 1, 0, -1):
        ancestor = "/".join(parts[:cut])
        if ancestor in fields:
            return fields[ancestor]["class"], "ANCESTOR"
    return entry["default_class"], "CONTRACT_DEFAULT"


# ---------------------------------------------------------------------------------------------
# Evaluator adapters
# ---------------------------------------------------------------------------------------------

def _payload_leaves(payload: Any) -> Iterator[tuple[tuple[str, ...], tuple[Any, ...], Any]]:
    """Yield (schema_path, concrete_path, value) for every leaf of a payload (iterative)."""
    stack: list[tuple[tuple[str, ...], tuple[Any, ...], Any]] = [((), (), payload)]
    seen = 0
    while stack:
        spath, cpath, node = stack.pop()
        seen += 1
        _require(seen <= MAX_NODES and len(cpath) <= MAX_DEPTH, "PAYLOAD_TOO_LARGE")
        if isinstance(node, dict) and node:
            for key in sorted(node, key=str, reverse=True):
                _require(isinstance(key, str), "PAYLOAD_KEY_NOT_STRING")
                stack.append((spath + (key,), cpath + (key,), node[key]))
        elif isinstance(node, list) and node:
            for index in range(len(node) - 1, -1, -1):
                stack.append((spath + (ARRAY_SEGMENT,), cpath + (index,), node[index]))
        else:
            yield spath, cpath, node


def _schema_field_for(fields: dict[str, FieldInfo], spath: tuple[str, ...]) -> str | None:
    """Map a concrete payload path to its schema field id, one segment at a time (linear).

    An object key matches a named property first, then a map (':') segment. Below an opaque/open
    container or an unresolved cross-contract $ref the schema has no children, so the leaf
    inherits that container's field id (and therefore its class). Otherwise None (unclassified).
    """
    prefix: list[str] = []
    for segment in spath:
        chosen = None
        options = (segment,) if segment == ARRAY_SEGMENT else (segment, MAP_SEGMENT)
        for option in options:
            if "/".join(prefix + [option]) in fields:
                chosen = option
                break
        if chosen is None:
            info = fields.get("/".join(prefix)) if prefix else None
            if info is not None and info.kinds & {"OPAQUE", "REF"}:
                return "/".join(prefix)
            return None
        prefix.append(chosen)
    return "/".join(prefix) if prefix else None

def classify_payload(payload: Any, contract_ref: str, *, class_map: dict[str, Any] | None = None,
                     index: SchemaIndex | None = None) -> list[dict[str, Any]]:
    """Classify every payload leaf. Paths absent from the contract schema fail closed as SECRET."""
    cmap = class_map or load_class_map()
    idx = index or SchemaIndex()
    _require(contract_ref in cmap["contracts"], "CONTRACT_NOT_CLASSIFIED")
    entry = cmap["contracts"][contract_ref]
    fields = idx.fields(contract_ref)
    rows = []
    for spath, cpath, _value in _payload_leaves(payload):
        if not spath:
            continue
        fid = _schema_field_for(fields, spath)
        if fid is None:
            rows.append({"path": cpath, "field_id": None, "data_class": "SECRET", "source": "UNCLASSIFIED_FAIL_CLOSED"})
            continue
        cls, source = resolve_class(entry, fid)
        rows.append({"path": cpath, "field_id": fid, "data_class": cls, "source": source})
    return rows


def _policy_field_id(field_id: str | None) -> str:
    raw = field_id if field_id is not None else "unclassified"
    safe = re.sub(r"[^A-Za-z0-9_.:/-]", "_", raw)[:128]
    return safe or "root"


def build_policy_packet(payload: Any, contract_ref: str, destination: str, purpose: str, **kw: Any) -> dict[str, Any]:
    """Build a Privacy Data Policy R1 input packet (metadata only) for a concrete payload."""
    _require(destination in POLICY_DESTINATIONS, "DESTINATION_INVALID")
    _require(purpose in POLICY_PURPOSES, "PURPOSE_INVALID")
    rows = classify_payload(payload, contract_ref, **kw)
    by_id: dict[str, str] = {}
    for row in rows:
        fid = _policy_field_id(row["field_id"])
        prior = by_id.get(fid)
        by_id[fid] = row["data_class"] if prior is None else max_class([prior, row["data_class"]])  # type: ignore[assignment]
    _require(by_id, "PAYLOAD_EMPTY")
    return {
        "schema_version": POLICY_INPUT_SCHEMA,
        "fields": [{"field_id": fid, "data_class": by_id[fid], "destination": destination, "purpose": purpose}
                   for fid in sorted(by_id)],
    }


def _set_path(root: Any, cpath: tuple[Any, ...], value: Any) -> None:
    node = root
    for part in cpath[:-1]:
        node = node[part]
    node[cpath[-1]] = value


def _drop_paths(root: Any, cpaths: list[tuple[Any, ...]]) -> None:
    # Delete deepest/last first so list indices stay valid.
    for cpath in sorted(cpaths, key=lambda p: (len(p), [str(x) for x in p]), reverse=True):
        node = root
        for part in cpath[:-1]:
            node = node[part]
        if isinstance(node, dict):
            node.pop(cpath[-1], None)
        elif isinstance(node, list) and isinstance(cpath[-1], int) and cpath[-1] < len(node):
            node[cpath[-1]] = None  # keep list shape; element becomes null


def minimize_for_destination(payload: Any, contract_ref: str, destination: str, purpose: str, *,
                             class_map: dict[str, Any] | None = None,
                             index: SchemaIndex | None = None) -> tuple[Any, dict[str, Any]]:
    """Apply R1 decisions to a concrete payload copy. DENY drops, REDACT replaces with a class token.

    For egress destinations (AI_PROVIDER / PUBLIC_EXPORT) every surviving string is also passed
    through ``mask_content`` so a restricted value in an allowed field is still masked.
    Returns (minimized_payload, metadata_report). The input payload is never mutated.
    """
    _require(destination in POLICY_DESTINATIONS, "DESTINATION_INVALID")
    _require(purpose in POLICY_PURPOSES, "PURPOSE_INVALID")
    rows = classify_payload(payload, contract_ref, class_map=class_map, index=index)
    out = copy.deepcopy(payload)
    counts = {"ALLOW": 0, "REDACT": 0, "DENY": 0}
    dropped: list[tuple[Any, ...]] = []
    unclassified = 0
    for row in rows:
        verdict, _reason = policy_decision(row["data_class"], destination, purpose)
        counts[verdict] += 1
        if row["source"] == "UNCLASSIFIED_FAIL_CLOSED":
            unclassified += 1
        if verdict == "DENY":
            dropped.append(row["path"])
        elif verdict == "REDACT":
            current = _get_path(out, row["path"])
            _set_path(out, row["path"], redaction_token(row["data_class"]) if isinstance(current, str) else None)
    _drop_paths(out, dropped)
    masked = 0
    if destination in EGRESS_DESTINATIONS:
        for _spath, cpath, value in list(_payload_leaves(out)):
            if isinstance(value, str) and cpath:
                new, n = mask_content(value)
                if n:
                    _set_path(out, cpath, new)
                    masked += n
    report = {
        "schema_version": ATTESTATION_SCHEMA,
        "contract_ref": contract_ref,
        "destination": destination,
        "purpose": purpose,
        "leaf_count": len(rows),
        "decision_counts": counts,
        "unclassified_leaf_count": unclassified,
        "content_masked_count": masked,
        "max_class_in": max_class([r["data_class"] for r in rows]),
        "authority": "PRIVACY_SCREEN_ADVISORY_NO_DATA_ACCESS",
        "physical_actions": False,
    }
    return out, report


def _get_path(root: Any, cpath: tuple[Any, ...]) -> Any:
    node = root
    for part in cpath:
        node = node[part]
    return node


def screen_outbound(payload: Any, contract_ref: str, destination: str, purpose: str, **kw: Any) -> dict[str, Any]:
    """Metadata-only attestation an evaluator can attach before a payload crosses a boundary.

    verdict PASS: every leaf is ALLOW for the destination and no content detector fires on an
    egress destination. BLOCK otherwise (the evaluator should call minimize_for_destination or
    not send). The payload value is never copied into the attestation.
    """
    packet = build_policy_packet(payload, contract_ref, destination, purpose, **kw)
    result = policy_evaluate(packet)
    findings = scan_content(payload) if destination in EGRESS_DESTINATIONS else []
    blocked = result["decision_counts"]["DENY"] or result["decision_counts"]["REDACT"] or findings
    return {
        "schema_version": ATTESTATION_SCHEMA,
        "contract_ref": contract_ref,
        "destination": destination,
        "purpose": purpose,
        "policy_decision_counts": result["decision_counts"],
        "blocking_field_ids": [d["field_id"] for d in result["decisions"] if d["decision"] != "ALLOW"],
        "content_finding_categories": sorted({f["category"] for f in findings}),
        "verdict": "BLOCK" if blocked else "PASS",
        "content_handling": "METADATA_ONLY_VALUES_NOT_ECHOED",
        "authority": "PRIVACY_SCREEN_ADVISORY_NO_DATA_ACCESS",
        "physical_actions": False,
    }
