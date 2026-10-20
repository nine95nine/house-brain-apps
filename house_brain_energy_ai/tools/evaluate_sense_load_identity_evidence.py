"""Generic extraction of accepted evidence scoring. Household pins are required private input.

The root evaluator remains the accepted household oracle. Scoring and classifier
precedence are preserved; deployment profiles carry immutable negative rulings.
No equipment or native naming authority is granted.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Final

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.evaluate_load_identity_confidence import (  # noqa: E402
    INPUT_SCHEMA as CLASSIFIER_INPUT_SCHEMA,
    evaluate as classify_325,
)

INPUT_SCHEMA: Final = "house_brain_sense_load_identity_evidence.input.v1"
RESULT_SCHEMA: Final = "house_brain_sense_load_identity_evidence.result.v1"
WORKSHEET_SCHEMA: Final = "house_brain_sense_load_identity_worksheet.v1"
REGISTER_SCHEMA: Final = "house_brain_sense_load_identity_register.v1"
CHECK_SCHEMA: Final = "house_brain_sense_load_identity_register_check.v1"
AUTHORITY: Final = "IDENTITY_EVIDENCE_ONLY_NO_CONTROL"

TOKEN_RE: Final = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
REF_RE: Final = re.compile(r"^[A-Za-z0-9_.:/#@+-]{1,160}$")
NAME_RE: Final = re.compile(r"^[A-Za-z0-9 ._/()&+-]{1,64}$")
MAX_WATTS: Final = 200_000.0
MAX_EPISODES: Final = 5_000
MAX_DEVICES: Final = 128
MAX_CORROBORATORS: Final = 4
MAX_ATTESTATIONS: Final = 32
MAX_AGE: Final = 100_000_000

EV_CLAIM: Final = "EV_CHARGING_SHARED"
# Any claim naming a specific vehicle is refused outright: EV load is shared (DO_NOT_REGRESS).
VEHICLE_CLAIM_RE: Final = re.compile(r"(TESLA|BOLT|CHEVY|CHEVROLET|MODEL_[0-9SXY3]|VEHICLE_[0-9A-Z])")

CORROBORATOR_KINDS: Final = frozenset({
    "SWITCH_STATE", "NATIVE_DEVICE_STATE", "CONTACT_SENSOR", "DEDICATED_CIRCUIT_METER", "SMART_PLUG_POWER",
})
ATTESTATION_KINDS: Final = frozenset({
    "CONTROLLED_TEST_MATCH", "CONTROLLED_TEST_MISMATCH", "OWNER_IDENTITY_ATTESTATION", "OWNER_LABEL_DISPUTE",
})
POSITIVE_ATTESTATIONS: Final = frozenset({"CONTROLLED_TEST_MATCH", "OWNER_IDENTITY_ATTESTATION"})
NEGATIVE_ATTESTATIONS: Final = frozenset({"CONTROLLED_TEST_MISMATCH", "OWNER_LABEL_DISPUTE"})
TEMPLATE_PLACEHOLDER: Final = "FILL_ME"

POLICY_FIELDS: Final = (
    "id", "evidence_refs", "stale_after_seconds", "edge_tolerance_s", "min_signature_events",
    "max_step_dispersion_ratio", "min_in_profile_fraction", "max_in_profile_fraction_for_mismatch",
    "reference_tolerance_ratio", "min_corroborated_episodes", "min_precision", "min_recall",
    "contradiction_fraction_min", "phantom_min_events",
)
DEVICE_FIELDS: Final = ("device_id",)
DEVICE_OPTIONAL: Final = (
    "sense_name", "identity_claim", "expected_signature", "episodes", "corroborators", "attestations",
    "kind", "shared_load_class",
)

# Score weights — ORDERING ONLY (worksheet priority). The class never reads the score.
SCORE_WEIGHTS: Final = {"name": 5, "signature": 25, "corroboration": 35, "attestation": 35}

CLASS_ORDER: Final = (
    "REVIEW_REQUIRED_RULING_CONTRADICTED", "REVIEW_REQUIRED_RULING_CHALLENGED",
    "UNVERIFIED_CORROBORATION_PENDING", "UNVERIFIED_MORE_REPEATS_NEEDED", "UNVERIFIED_SIGNATURE_PENDING",
    "UNVERIFIED_STALE", "UNVERIFIED_NO_CLAIM", "UNVERIFIED_NAME_ONLY", "REJECTED", "CONFIRMED",
    "CATEGORY_ONLY",
)

UPGRADE_CATALOG: Final = {
    "STATE_IDENTITY_CLAIM": "Say which physical appliance this Sense device is being tested as (a claim token).",
    "SUPPLY_EXPECTED_SIGNATURE": "Give the expected on-step watts band (nameplate, clamp reading or a controlled test).",
    "COLLECT_SENSE_EPISODES": "Export more Sense on-episodes for this device from Home Assistant history.",
    "IMPROVE_SIGNATURE_CONSISTENCY": "Episodes vary too much or fall outside the band; re-check the band or look for a merged load.",
    "ADD_INDEPENDENT_CORROBORATOR": "Pair the device with an independent Home Assistant entity (switch, native state, contact sensor or circuit meter).",
    "COLLECT_CORROBORATED_EPISODES": "Let ordinary use continue until enough Sense episodes line up with the independent entity.",
    "OWNER_CONTROLLED_TEST": "During ordinary use, run the appliance at least three times and note whether this Sense device responds.",
    "REFRESH_EVIDENCE": "Evidence is older than the freshness limit; add a newer history export or attestation.",
    "RESOLVE_CONTRADICTION": "Independent evidence disagrees with the claim; review before any reuse.",
    "OWNER_REVIEW_OF_ACCEPTED_RULING": "New evidence disagrees with an accepted ruling; the owner decides whether the ruling changes.",
    "NEW_CONTROLLED_EVIDENCE_TO_REOPEN": "Rejected claim; only new controlled evidence plus owner review can reopen it.",
    "VEHICLE_IDENTITY_NOT_DERIVABLE": "Shared EV load: combined EV energy only. Per-vehicle identity is never inferred from Sense.",
    "NONE_AGGREGATE_BUCKET": "Aggregate bucket, not an appliance identity. There is no upgrade path.",
    "NONE_CONFIRMED": "Confirmed. Keep it confirmed through ordinary use; no action needed.",
}


class EvidenceError(ValueError):
    """Fail-closed contract error (the message is a reason code, optionally with context)."""


def _require(ok: bool, code: str) -> None:
    if not ok:
        raise EvidenceError(code)


# ---------------------------------------------------------------------------------------------
# primitive validators


def _obj(v: Any, code: str) -> dict[str, Any]:
    _require(isinstance(v, dict), code)
    return v


def _list(v: Any, code: str, cap: int) -> list[Any]:
    _require(isinstance(v, list) and len(v) <= cap, code)
    return v


def _fields(d: dict[str, Any], required: tuple[str, ...], optional: tuple[str, ...], code: str) -> None:
    keys = set(d)
    _require(set(required) <= keys and keys <= set(required) | set(optional), code)


def _num(v: Any, code: str, lo: float, hi: float) -> float:
    _require(isinstance(v, (int, float)) and not isinstance(v, bool), code)
    _require(math.isfinite(float(v)) and lo <= float(v) <= hi, code)
    return float(v)


def _int(v: Any, code: str, lo: int, hi: int) -> int:
    _require(isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi, code)
    return v


def _token(v: Any, code: str) -> str:
    _require(isinstance(v, str) and TOKEN_RE.fullmatch(v) is not None, code)
    return v


def _ref(v: Any, code: str) -> str:
    _require(isinstance(v, str) and REF_RE.fullmatch(v) is not None, code)
    return v


def _ts(v: Any, code: str) -> datetime:
    _require(isinstance(v, str) and 19 <= len(v) <= 40, code)
    try:
        at = datetime.fromisoformat(v[:-1] + "+00:00" if v.endswith("Z") else v)
    except ValueError as exc:
        raise EvidenceError(code) from exc
    _require(at.tzinfo is not None and at.utcoffset() is not None, code)
    return at


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _rnd(v: float | None, places: int = 3) -> float | None:
    return None if v is None else round(v, places)


def _claim(v: Any, code: str) -> str:
    claim = _token(v, code)
    _require(VEHICLE_CLAIM_RE.search(claim) is None, "VEHICLE_IDENTITY_INFERENCE_PROHIBITED")
    return claim


# ---------------------------------------------------------------------------------------------
# register


def load_register(path: Path, pins: dict[str, Any]) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("REGISTER_UNREADABLE") from exc
    return validate_register(raw, pins)


def validate_pins(raw: Any) -> dict[str, Any]:
    pins = _obj(raw, "PRIVATE_PINS_REQUIRED")
    _fields(pins, ("schema_version", "rejections", "shared_ev", "pool_claim", "pool_source",
                   "pool_authority_key", "pool_evaluator_refuses_named_device"), (), "PRIVATE_PIN_FIELDS")
    _require(type(pins["schema_version"]) is int and pins["schema_version"] == 1, "PRIVATE_PIN_SCHEMA")
    seen = set()
    for pin in _list(pins["rejections"], "PRIVATE_REJECTIONS", MAX_DEVICES):
        _fields(_obj(pin, "PRIVATE_REJECTION"), ("device_id", "claim", "reason"), (), "PRIVATE_REJECTION")
        for key in pin:
            _token(pin[key], "PRIVATE_REJECTION_TOKEN")
        pair = (pin["device_id"], pin["claim"])
        _require(pair not in seen, "PRIVATE_REJECTION_DUPLICATE")
        seen.add(pair)
    shared = _list(pins["shared_ev"], "PRIVATE_SHARED_EV", MAX_DEVICES)
    for did in shared:
        _token(did, "PRIVATE_SHARED_EV_TOKEN")
    _require(len(shared) == len(set(shared)), "PRIVATE_SHARED_EV_DUPLICATE")
    for field in ("pool_claim", "pool_source"):
        _token(pins[field], "PRIVATE_CONSUMER_TOKEN")
    _require(isinstance(pins["pool_authority_key"], str) and re.fullmatch(r"[a-z_]{1,64}", pins["pool_authority_key"]),
             "PRIVATE_CONSUMER_KEY")
    _require(type(pins["pool_evaluator_refuses_named_device"]) is bool, "PRIVATE_CONSUMER_BOOLEAN")
    return pins


def validate_register(raw: Any, pins: dict[str, Any]) -> dict[str, Any]:
    pins = validate_pins(pins)
    reg = _obj(raw, "REGISTER_NOT_OBJECT")
    _require(reg.get("schema_version") == REGISTER_SCHEMA, "REGISTER_SCHEMA_MISMATCH")
    _require(reg.get("authority") == AUTHORITY, "REGISTER_AUTHORITY_INVALID")
    _require(isinstance(reg.get("revision"), str) and REF_RE.fullmatch(reg["revision"]) is not None,
         "REGISTER_REVISION_INVALID")
    devices: dict[str, dict[str, Any]] = {}
    for d in _list(reg.get("devices"), "REGISTER_DEVICES_INVALID", MAX_DEVICES):
        d = _obj(d, "REGISTER_DEVICE_INVALID")
        _fields(d, ("device_id", "r1_row_label", "r1_disposition", "kind", "identity_scope", "shared_load_class",
                    "primary_claim", "reference_step_w", "rulings", "aggregate_contains"), (),
                "REGISTER_DEVICE_FIELDS_INVALID")
        did = _token(d["device_id"], "REGISTER_DEVICE_ID_INVALID")
        _require(did not in devices, "REGISTER_DEVICE_DUPLICATE")
        _require(d["kind"] in {"DETECTED_DEVICE", "AGGREGATE_BUCKET"}, "REGISTER_KIND_INVALID")
        _require(d["identity_scope"] in {"DEVICE", "SHARED_LOAD_CLASS"}, "REGISTER_SCOPE_INVALID")
        _require(d["shared_load_class"] in {None, "EV"}, "REGISTER_SHARED_CLASS_INVALID")
        _require((d["shared_load_class"] is None) == (d["identity_scope"] == "DEVICE"), "REGISTER_SHARED_CLASS_INVALID")
        if d["primary_claim"] is not None:
            _claim(d["primary_claim"], "REGISTER_CLAIM_INVALID")
        if d["shared_load_class"] == "EV":
            _require(d["primary_claim"] == EV_CLAIM, "REGISTER_EV_CLAIM_INVALID")
        if d["reference_step_w"] is not None:
            _num(d["reference_step_w"], "REGISTER_REFERENCE_INVALID", 1, MAX_WATTS)
        rulings: dict[str, dict[str, Any]] = {}
        for r in _list(d["rulings"], "REGISTER_RULINGS_INVALID", 8):
            r = _obj(r, "REGISTER_RULING_INVALID")
            _fields(r, ("claim", "ruling", "basis"), (), "REGISTER_RULING_INVALID")
            claim = _claim(r["claim"], "REGISTER_RULING_INVALID")
            _require(r["ruling"] in {"CONFIRMED", "REJECTED"} and claim not in rulings, "REGISTER_RULING_INVALID")
            _token(r["basis"], "REGISTER_RULING_INVALID")
            rulings[claim] = dict(r)
        if d["kind"] == "AGGREGATE_BUCKET":
            _require(not rulings and d["primary_claim"] is None, "REGISTER_AGGREGATE_HAS_IDENTITY")
        for c in _list(d["aggregate_contains"], "REGISTER_AGGREGATE_INVALID", 16):
            _claim(c, "REGISTER_AGGREGATE_INVALID")
        _require(not d["aggregate_contains"] or d["kind"] == "AGGREGATE_BUCKET", "REGISTER_AGGREGATE_INVALID")
        devices[did] = {**d, "rulings": rulings}
    for pin in pins["rejections"]:
        did, claim = pin["device_id"], pin["claim"]
        _require(devices.get(did, {}).get("rulings", {}).get(claim, {}).get("ruling") == "REJECTED",
             "REGISTER_PINNED_REJECTION_MISSING")
    for did in pins["shared_ev"]:
        _require(devices.get(did, {}).get("shared_load_class") == "EV", "REGISTER_PINNED_SHARED_EV_MISSING")
    return {"revision": reg["revision"], "devices": devices, "pins": pins,
            "physical_signatures": _list(reg.get("physical_signatures"), "REGISTER_SIGNATURES_INVALID", 16)}


# ---------------------------------------------------------------------------------------------
# packet


def _policy(raw: Any) -> dict[str, Any]:
    p = _obj(raw, "POLICY_INVALID")
    _fields(p, POLICY_FIELDS, (), "POLICY_FIELDS_INVALID")
    out = {
        "id": _token(p["id"], "POLICY_ID_INVALID"),
        "evidence_refs": [_ref(r, "POLICY_EVIDENCE_REFS_INVALID")
                          for r in _list(p["evidence_refs"], "POLICY_EVIDENCE_REFS_INVALID", 16)],
        "stale_after_seconds": _int(p["stale_after_seconds"], "POLICY_STALE_INVALID", 1, MAX_AGE),
        "edge_tolerance_s": _num(p["edge_tolerance_s"], "POLICY_EDGE_TOLERANCE_INVALID", 1, 3600),
        "min_signature_events": _int(p["min_signature_events"], "POLICY_MIN_EVENTS_INVALID", 3, 10_000),
        "max_step_dispersion_ratio": _num(p["max_step_dispersion_ratio"], "POLICY_DISPERSION_INVALID", 0.01, 1),
        "min_in_profile_fraction": _num(p["min_in_profile_fraction"], "POLICY_IN_PROFILE_INVALID", 0.5, 1),
        "max_in_profile_fraction_for_mismatch": _num(p["max_in_profile_fraction_for_mismatch"],
                                                     "POLICY_MISMATCH_FRACTION_INVALID", 0, 0.5),
        "reference_tolerance_ratio": _num(p["reference_tolerance_ratio"], "POLICY_REFERENCE_TOLERANCE_INVALID",
                                          0.01, 0.5),
        "min_corroborated_episodes": _int(p["min_corroborated_episodes"], "POLICY_MIN_CORROBORATED_INVALID",
                                          3, 10_000),
        "min_precision": _num(p["min_precision"], "POLICY_PRECISION_INVALID", 0.5, 1),
        "min_recall": _num(p["min_recall"], "POLICY_RECALL_INVALID", 0.5, 1),
        "contradiction_fraction_min": _num(p["contradiction_fraction_min"], "POLICY_CONTRADICTION_INVALID", 0.1, 1),
        "phantom_min_events": _int(p["phantom_min_events"], "POLICY_PHANTOM_INVALID", 1, 10_000),
    }
    _require(out["max_in_profile_fraction_for_mismatch"] < out["min_in_profile_fraction"], "POLICY_FRACTIONS_INCONSISTENT")
    return out


def _intervals(raw: Any, code: str, lower: datetime, upper: datetime, with_step: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    prior: datetime | None = None
    for row in _list(raw, code, MAX_EPISODES):
        row = _obj(row, code)
        _fields(row, ("start", "end", "step_w") if with_step else ("start", "end"), (), code)
        a, b = _ts(row["start"], code), _ts(row["end"], code)
        _require(a < b and lower <= a and b <= upper, "INTERVAL_OUTSIDE_OBSERVATION_WINDOW" if a < b else code)
        _require(prior is None or a > prior, "INTERVALS_NOT_STRICTLY_INCREASING")
        prior = a
        item: dict[str, Any] = {"start": a.timestamp(), "end": b.timestamp()}
        if with_step:
            item["step_w"] = _num(row["step_w"], "EPISODE_STEP_W_INVALID", 0.001, MAX_WATTS)
        out.append(item)
    return out


def _device(raw: Any, reg: dict[str, Any], lower: datetime, upper: datetime, evaluated_at: datetime) -> dict[str, Any]:
    d = _obj(raw, "DEVICE_INVALID")
    _fields(d, DEVICE_FIELDS, DEVICE_OPTIONAL, "DEVICE_FIELDS_INVALID")
    did = _token(d["device_id"], "DEVICE_ID_INVALID")
    base = reg["devices"].get(did)
    kind = d.get("kind", base["kind"] if base else "DETECTED_DEVICE")
    _require(kind in {"DETECTED_DEVICE", "AGGREGATE_BUCKET"}, "DEVICE_KIND_INVALID")
    _require(base is None or kind == base["kind"], "DEVICE_KIND_CONFLICTS_WITH_REGISTER")
    shared = d.get("shared_load_class", base["shared_load_class"] if base else None)
    _require(shared in {None, "EV"}, "DEVICE_SHARED_CLASS_INVALID")
    _require(base is None or base["shared_load_class"] is None or shared == "EV", "SHARED_LOAD_CLASS_CANNOT_BE_REMOVED")
    if did in reg["pins"]["shared_ev"]:
        shared = "EV"
    name = d.get("sense_name")
    if name is not None:
        _require(isinstance(name, str) and NAME_RE.fullmatch(name) is not None, "SENSE_NAME_INVALID")
    claim = d.get("identity_claim", base["primary_claim"] if base else None)
    if claim is not None:
        claim = _claim(claim, "IDENTITY_CLAIM_INVALID")
    if shared == "EV":
        _require(claim in {None, EV_CLAIM}, "VEHICLE_IDENTITY_INFERENCE_PROHIBITED")
        claim = EV_CLAIM
    expected = d.get("expected_signature")
    if expected is not None:
        e = _obj(expected, "EXPECTED_SIGNATURE_INVALID")
        _fields(e, ("step_min_w", "step_max_w"), ("duration_min_s", "duration_max_s"), "EXPECTED_SIGNATURE_INVALID")
        lo = _num(e["step_min_w"], "EXPECTED_SIGNATURE_INVALID", 0.001, MAX_WATTS)
        hi = _num(e["step_max_w"], "EXPECTED_SIGNATURE_INVALID", lo, MAX_WATTS)
        dlo = _num(e.get("duration_min_s", 0), "EXPECTED_SIGNATURE_INVALID", 0, 31_536_000)
        dhi = _num(e.get("duration_max_s", 31_536_000), "EXPECTED_SIGNATURE_INVALID", dlo, 31_536_000)
        expected = {"step_min_w": lo, "step_max_w": hi, "duration_min_s": dlo, "duration_max_s": dhi,
                    "source": "OWNER_PACKET"}
    episodes = _intervals(d.get("episodes", []), "EPISODES_INVALID", lower, upper, True)
    corroborators = []
    refs: set[str] = set()
    for c in _list(d.get("corroborators", []), "CORROBORATORS_INVALID", MAX_CORROBORATORS):
        c = _obj(c, "CORROBORATOR_INVALID")
        _fields(c, ("ref", "source_kind", "evidences_claim", "coverage_start", "coverage_end", "episodes"), (),
                "CORROBORATOR_FIELDS_INVALID")
        ref = _ref(c["ref"], "CORROBORATOR_REF_INVALID")
        _require(ref not in refs, "CORROBORATOR_DUPLICATE")
        refs.add(ref)
        kind_c = c["source_kind"]
        _require(isinstance(kind_c, str), "CORROBORATOR_KIND_INVALID")
        _require(not kind_c.upper().startswith("SENSE") and "sense" not in ref.lower(), "SENSE_NOT_INDEPENDENT_CORROBORATOR")
        _require(kind_c in CORROBORATOR_KINDS, "CORROBORATOR_KIND_INVALID")
        ev_claim = _claim(c["evidences_claim"], "CORROBORATOR_CLAIM_INVALID")
        cs, ce = _ts(c["coverage_start"], "CORROBORATOR_COVERAGE_INVALID"), _ts(c["coverage_end"], "CORROBORATOR_COVERAGE_INVALID")
        _require(lower <= cs < ce <= upper, "CORROBORATOR_COVERAGE_INVALID")
        corroborators.append({"ref": ref, "source_kind": kind_c, "evidences_claim": ev_claim,
                              "coverage": (cs.timestamp(), ce.timestamp()),
                              "episodes": _intervals(c["episodes"], "CORROBORATOR_EPISODES_INVALID", cs, ce, False)})
    attestations = []
    for a in _list(d.get("attestations", []), "ATTESTATIONS_INVALID", MAX_ATTESTATIONS):
        a = _obj(a, "ATTESTATION_INVALID")
        _fields(a, ("kind", "identity_claim", "observed_at", "repeats", "evidence_ref"), (), "ATTESTATION_FIELDS_INVALID")
        _require(TEMPLATE_PLACEHOLDER not in (a["kind"], a["identity_claim"]) and None not in a.values(),
             "ATTESTATION_TEMPLATE_UNFILLED")
        _require(a["kind"] in ATTESTATION_KINDS, "ATTESTATION_KIND_INVALID")
        a_claim = _claim(a["identity_claim"], "ATTESTATION_CLAIM_INVALID")
        if shared == "EV":
            _require(a_claim == EV_CLAIM, "VEHICLE_IDENTITY_INFERENCE_PROHIBITED")
        at = _ts(a["observed_at"], "ATTESTATION_TIME_INVALID")
        _require(at <= evaluated_at, "ATTESTATION_AFTER_EVALUATED_AT")
        attestations.append({"kind": a["kind"], "claim": a_claim, "at": at.timestamp(),
                             "repeats": _int(a["repeats"], "ATTESTATION_REPEATS_INVALID", 1, 1000),
                             "evidence_ref": _ref(a["evidence_ref"], "ATTESTATION_REF_INVALID")})
    return {"device_id": did, "kind": kind, "shared_load_class": shared, "sense_name": name, "claim": claim,
            "expected": expected, "episodes": episodes, "corroborators": corroborators,
            "attestations": attestations, "in_packet": True}


# ---------------------------------------------------------------------------------------------
# evidence analysis


def _name_hint(name: str | None, claim: str | None) -> dict[str, Any]:
    if name is None:
        return {"strength": "NONE", "verdict": "SENSE_NAME_NOT_SUPPLIED", "points": 0}
    if claim is None:
        return {"strength": "WEAK", "verdict": "SENSE_NAME_NO_CLAIM_TO_COMPARE", "points": 0}
    name_tokens = {t for t in re.split(r"[^A-Z0-9]+", name.upper()) if len(t) >= 3}
    claim_tokens = {t for t in claim.split("_") if len(t) >= 3}
    if name_tokens & claim_tokens:
        return {"strength": "WEAK", "verdict": "SENSE_NAME_SUGGESTS_CLAIM_WEAK", "points": SCORE_WEIGHTS["name"]}
    return {"strength": "WEAK", "verdict": "SENSE_NAME_UNRELATED_TO_CLAIM", "points": 0}


def _signature(dev: dict[str, Any], base: dict[str, Any] | None, pol: dict[str, Any]) -> dict[str, Any]:
    eps = dev["episodes"]
    band = dev["expected"]
    if band is None and base is not None and base["reference_step_w"] is not None and dev["claim"] == base["primary_claim"]:
        ref = float(base["reference_step_w"])
        tol = pol["reference_tolerance_ratio"]
        band = {"step_min_w": ref * (1 - tol), "step_max_w": ref * (1 + tol), "duration_min_s": 0.0,
                "duration_max_s": 31_536_000.0, "source": "REGISTER_REFERENCE"}
    out: dict[str, Any] = {"episodes": len(eps), "band": None if band is None else {
        k: _rnd(band[k], 1) for k in ("step_min_w", "step_max_w", "duration_min_s", "duration_max_s")} | {
        "source": band["source"]}, "median_step_w": None, "step_dispersion_ratio": None,
        "median_duration_s": None, "duration_dispersion_ratio": None, "episodes_per_day": None,
        "in_profile": 0, "in_profile_fraction": None, "match": "UNKNOWN", "reasons": [], "points": 0}
    if not eps:
        out["reasons"].append("NO_SENSE_EPISODES")
        return out
    steps = [e["step_w"] for e in eps]
    durations = [e["end"] - e["start"] for e in eps]
    med = _median(steps)
    disp = _median([abs(s - med) for s in steps]) / med
    dmed = _median(durations)
    ddisp = _median([abs(x - dmed) for x in durations]) / dmed if dmed > 0 else 0.0
    out.update({"median_step_w": _rnd(med, 1), "step_dispersion_ratio": _rnd(disp, 4),
                "median_duration_s": _rnd(dmed, 1), "duration_dispersion_ratio": _rnd(ddisp, 4)})
    span = eps[-1]["end"] - eps[0]["start"]
    if span >= 86_400:
        out["episodes_per_day"] = _rnd(len(eps) / (span / 86_400.0), 3)
    consistent = disp <= pol["max_step_dispersion_ratio"]
    if not consistent:
        out["reasons"].append("SIGNATURE_STEP_INCONSISTENT")
    if band is None:
        out["reasons"].append("SIGNATURE_PROFILE_MISSING")
        out["points"] = 5 if consistent and len(eps) >= pol["min_signature_events"] else 0
        return out
    inside = sum(1 for s, t in zip(steps, durations)
                 if band["step_min_w"] <= s <= band["step_max_w"] and band["duration_min_s"] <= t <= band["duration_max_s"])
    frac = inside / len(eps)
    out["in_profile"] = inside
    out["in_profile_fraction"] = _rnd(frac, 4)
    if len(eps) < pol["min_signature_events"]:
        out["reasons"].append("SIGNATURE_TOO_FEW_EPISODES")
    elif frac <= pol["max_in_profile_fraction_for_mismatch"]:
        out["match"] = "MISMATCH"
        out["reasons"].append("SIGNATURE_OUTSIDE_EXPECTED_BAND")
    elif frac >= pol["min_in_profile_fraction"] and consistent:
        out["match"] = "MATCH"
        out["reasons"].append("SIGNATURE_CONSISTENT_WITH_BAND")
    else:
        out["reasons"].append("SIGNATURE_PARTIAL_MATCH")
    out["points"] = {"MATCH": SCORE_WEIGHTS["signature"], "MISMATCH": 0}.get(out["match"], 10 if frac >= 0.5 else 0)
    return out


def _corroborate(dev: dict[str, Any], corr: dict[str, Any], index: int, pol: dict[str, Any]) -> dict[str, Any]:
    lo, hi = corr["coverage"]
    tol = pol["edge_tolerance_s"]
    sense = [e for e in dev["episodes"] if lo <= e["start"] and e["end"] <= hi]
    other = corr["episodes"]
    used = [False] * len(other)
    matched = 0
    contradicted = 0
    for e in sense:
        hit = False
        for j, o in enumerate(other):
            if not used[j] and abs(o["start"] - e["start"]) <= tol:
                used[j] = True
                hit = True
                break
        if hit:
            matched += 1
        elif not any(o["start"] - tol < e["end"] and e["start"] < o["end"] + tol for o in other):
            contradicted += 1  # Sense says ON while the independent entity is OFF throughout
    out: dict[str, Any] = {
        "corroborator_index": index, "source_kind": corr["source_kind"], "evidences_claim": corr["evidences_claim"],
        "sense_episodes_in_coverage": len(sense), "independent_episodes": len(other), "matched": matched,
        "contradicted": contradicted,
        "precision": _rnd(matched / len(sense), 4) if sense else None,
        "recall": _rnd(matched / len(other), 4) if other else None,
        "verdict": "INSUFFICIENT_DATA",
    }
    if corr["evidences_claim"] != dev["claim"]:
        out["verdict"] = "IGNORED_EVIDENCES_OTHER_CLAIM"
        return out
    if len(sense) >= pol["min_signature_events"] and contradicted / len(sense) >= pol["contradiction_fraction_min"]:
        out["verdict"] = "CONTRADICTS"
    elif (matched >= pol["min_corroborated_episodes"] and out["precision"] is not None and out["recall"] is not None
          and matched / len(sense) >= pol["min_precision"] and matched / len(other) >= pol["min_recall"]):
        out["verdict"] = "CONFIRMS"
    elif len(sense) >= pol["phantom_min_events"] or len(other) >= pol["min_corroborated_episodes"]:
        out["verdict"] = "DOES_NOT_CONFIRM"
    return out


# ---------------------------------------------------------------------------------------------
# per-device evaluation


def _evaluate_device(dev: dict[str, Any], base: dict[str, Any] | None, pol: dict[str, Any], at: datetime, pins: dict[str, Any]) -> dict[str, Any]:
    did = dev["device_id"]
    claim = dev["claim"]
    reasons: set[str] = set()
    rulings = dict(base["rulings"]) if base else {}
    for pin in pins["rejections"]:
        if pin["device_id"] == did:
            pclaim = pin["claim"]
            rulings[pclaim] = {"claim": pclaim, "ruling": "REJECTED", "basis": "PINNED_KNOWN_FACT"}
    scope = "SHARED_LOAD_CLASS" if dev["shared_load_class"] == "EV" else "DEVICE"
    common = {"device_id": did, "identity_claim": claim, "identity_scope": scope, "kind": dev["kind"],
              "usable_for_control": False, "physical_actions": False,
              "vehicle_identity": "PROHIBITED" if scope == "SHARED_LOAD_CLASS" else "NOT_APPLICABLE",
              "register_disposition": base["r1_disposition"] if base else None,
              "in_packet": dev["in_packet"]}

    if dev["kind"] == "AGGREGATE_BUCKET":
        if dev["in_packet"] and (dev["claim"] is not None or dev["attestations"] or dev["corroborators"]):
            reasons.add("AGGREGATE_BUCKET_CANNOT_CARRY_IDENTITY")
        reasons.add("CATEGORY_ONLY_NOT_AN_APPLIANCE")
        return {**common, "identity_claim": None, "confidence_class": "CATEGORY_ONLY", "usable_for_analytics": False,
                "accepted_ruling": None, "rejected_claims": [], "classifier_325": None, "evidence": None,
                "evidence_score": 0, "reason_codes": sorted(reasons), "phantom_status": "NOT_APPLICABLE",
                "contains_verified_signatures": sorted(base["aggregate_contains"]) if base else [],
                "upgrade_path": _upgrades(["NONE_AGGREGATE_BUCKET"])}

    name = _name_hint(dev["sense_name"], claim)
    reasons.add(name["verdict"])
    sig = _signature(dev, base, pol)
    reasons.update(sig["reasons"])
    corr = [_corroborate(dev, c, i, pol) for i, c in enumerate(dev["corroborators"])]
    atts_for = [a for a in dev["attestations"] if a["claim"] == claim]
    other_neg = sorted({a["claim"] for a in dev["attestations"]
                        if a["claim"] != claim and a["kind"] in NEGATIVE_ATTESTATIONS})
    if len(atts_for) != len(dev["attestations"]):
        reasons.add("ATTESTATION_FOR_OTHER_CLAIM_RECORDED")
    pos = [a for a in atts_for if a["kind"] in POSITIVE_ATTESTATIONS]
    neg = [a for a in atts_for if a["kind"] in NEGATIVE_ATTESTATIONS]
    controlled_match = [a for a in pos if a["kind"] == "CONTROLLED_TEST_MATCH"]

    # --- map evidence onto the accepted #325 input (never re-implementing its precedence) ---
    if sig["match"] == "MISMATCH" or (neg and any(a["kind"] == "CONTROLLED_TEST_MISMATCH" for a in neg)):
        signature_match = "MISMATCH"
    elif sig["match"] == "MATCH" or controlled_match:
        signature_match = "MATCH"
    else:
        signature_match = "UNKNOWN"
    if sig["match"] == "MISMATCH" and controlled_match:
        reasons.add("EVIDENCE_CONFLICT_SIGNATURE_VS_CONTROLLED_TEST")
    confirms = [c for c in corr if c["verdict"] == "CONFIRMS"]
    contradicts = [c for c in corr if c["verdict"] == "CONTRADICTS"]
    judged = [c for c in corr if c["verdict"] in {"CONFIRMS", "CONTRADICTS", "DOES_NOT_CONFIRM"}]
    if claim is None:
        independent = "UNKNOWN"
        reasons.add("NO_IDENTITY_CLAIM_UNDER_TEST")
    elif confirms or pos:
        independent = "CONFIRMED"
    elif judged:
        independent = "ABSENT"
    else:
        independent = "UNKNOWN"
    if confirms:
        reasons.add("INDEPENDENT_ENTITY_CORROBORATES")
    if contradicts:
        reasons.add("INDEPENDENT_ENTITY_CONTRADICTS")
    if pos:
        reasons.add("OWNER_ATTESTATION_SUPPORTS_CLAIM")
    if neg:
        reasons.add("OWNER_ATTESTATION_DISPUTES_CLAIM")
    contradiction_count = len(neg) + len(contradicts)
    repeats = min(100_000, sig["in_profile"] + sum(a["repeats"] for a in controlled_match))
    times = [e["end"] for e in dev["episodes"]] + [a["at"] for a in atts_for] + [c["coverage"][1] for c in dev["corroborators"]]
    has_evidence = bool(dev["episodes"] or atts_for or dev["corroborators"])
    if times:
        age = int(max(0.0, at.timestamp() - max(times)))
    else:
        age = pol["stale_after_seconds"] + 1
        reasons.add("NO_EVIDENCE_TREATED_AS_STALE")
    classifier_input = {
        "schema_version": CLASSIFIER_INPUT_SCHEMA, "candidate_id": did, "signature_match": signature_match,
        "repeat_observations": repeats, "independent_corroboration": independent,
        "contradiction_count": min(100_000, contradiction_count),
        "observation_age_seconds": min(MAX_AGE, age), "stale_after_seconds": pol["stale_after_seconds"],
        "reason_codes": [],
    }
    classified = classify_325(classifier_input)
    c325 = classified["classification"]

    # --- accepted-ruling overlay (preserve; flag, never flip) ---
    ruling = rulings.get(claim) if claim is not None else None
    rejected_claims = sorted({k for k, r in rulings.items() if r["ruling"] == "REJECTED"} | set(other_neg))
    positive_new = c325 == "CONFIRMED" or bool(controlled_match)
    if ruling is not None and ruling["ruling"] == "CONFIRMED":
        if c325 == "REJECTED":
            cls = "REVIEW_REQUIRED_RULING_CONTRADICTED"
            reasons.add("ACCEPTED_RULING_CONTRADICTED_BY_NEW_EVIDENCE")
        else:
            cls = "CONFIRMED"
            reasons.add("ACCEPTED_RULING_PRESERVED")
    elif ruling is not None:
        if positive_new:
            cls = "REVIEW_REQUIRED_RULING_CHALLENGED"
            reasons.add("ACCEPTED_REJECTION_CHALLENGED_BY_NEW_EVIDENCE")
        else:
            cls = "REJECTED"
            reasons.add("ACCEPTED_RULING_PRESERVED")
        for pin in pins["rejections"]:
            if (did, claim) == (pin["device_id"], pin["claim"]):
                reasons.add(pin["reason"])
    elif c325 == "CONFIRMED":
        cls = "CONFIRMED"
    elif c325 == "REJECTED":
        cls = "REJECTED"
    elif claim is None:
        cls = "UNVERIFIED_NO_CLAIM" if has_evidence else "UNVERIFIED_NAME_ONLY"
    elif not has_evidence:
        cls = "UNVERIFIED_NAME_ONLY"
    elif "EVIDENCE_STALE" in classified["reason_codes"]:
        cls = "UNVERIFIED_STALE"
    elif "SIGNATURE_NOT_CONFIRMED" in classified["reason_codes"]:
        cls = "UNVERIFIED_SIGNATURE_PENDING"
    elif "INSUFFICIENT_REPEAT_OBSERVATIONS" in classified["reason_codes"]:
        cls = "UNVERIFIED_MORE_REPEATS_NEEDED"
    else:
        cls = "UNVERIFIED_CORROBORATION_PENDING"
    if scope == "SHARED_LOAD_CLASS":
        reasons.add("EV_LOAD_SHARED_COMBINED_ONLY")

    usable = cls == "CONFIRMED"
    corr_points = SCORE_WEIGHTS["corroboration"] if confirms else (10 if any(c["matched"] for c in corr) else 0)
    att_points = SCORE_WEIGHTS["attestation"] if controlled_match else (25 if pos else 0)
    score = 0 if (contradiction_count or signature_match == "MISMATCH") else min(
        100, name["points"] + sig["points"] + corr_points + att_points)

    phantom = _phantom(dev, corr, pos, ruling, pol)
    if phantom in {"PHANTOM_SUSPECT", "NEVER_CORROBORATED"}:
        reasons.add(phantom)
    upgrades = _upgrade_codes(cls, dev, sig, corr, pos, claim, scope, pol, classified)
    return {**common, "confidence_class": cls, "usable_for_analytics": usable,
            "accepted_ruling": None if ruling is None else {"claim": ruling["claim"], "ruling": ruling["ruling"],
                                                             "basis": ruling["basis"]},
            "rejected_claims": rejected_claims,
            "classifier_325": {"input": classifier_input, "result": classified},
            "evidence": {"name": {k: name[k] for k in ("strength", "verdict")},
                         "signature": {k: v for k, v in sig.items() if k not in {"reasons", "points"}},
                         "corroboration": corr,
                         "attestations": {"supporting": len(pos), "disputing": len(neg),
                                          "controlled_repeats": sum(a["repeats"] for a in controlled_match),
                                          "evidence_refs": sorted({a["evidence_ref"] for a in atts_for})}},
            "evidence_score": score, "reason_codes": sorted(reasons), "phantom_status": phantom,
            "contains_verified_signatures": [], "upgrade_path": _upgrades(upgrades)}


def _phantom(dev: dict[str, Any], corr: list[dict[str, Any]], pos: list[dict[str, Any]],
             ruling: dict[str, Any] | None, pol: dict[str, Any]) -> str:
    if not dev["in_packet"]:
        return "NO_PACKET_EVIDENCE"
    n = len(dev["episodes"])
    if n == 0:
        return "DORMANT"
    if pos or (ruling is not None and ruling["ruling"] == "CONFIRMED") or any(c["matched"] for c in corr):
        return "NOT_PHANTOM"
    if any(c["sense_episodes_in_coverage"] >= pol["phantom_min_events"] for c in corr):
        return "PHANTOM_SUSPECT"
    if n >= pol["phantom_min_events"]:
        return "NEVER_CORROBORATED"
    return "INSUFFICIENT_ACTIVITY"


def _upgrade_codes(cls: str, dev: dict[str, Any], sig: dict[str, Any], corr: list[dict[str, Any]],
                   pos: list[dict[str, Any]], claim: str | None, scope: str, pol: dict[str, Any],
                   classified: dict[str, Any]) -> list[str]:
    codes: list[str] = []
    if cls == "CONFIRMED":
        codes.append("NONE_CONFIRMED")
    elif cls in {"REVIEW_REQUIRED_RULING_CONTRADICTED", "REVIEW_REQUIRED_RULING_CHALLENGED"}:
        codes.append("OWNER_REVIEW_OF_ACCEPTED_RULING")
    elif cls == "REJECTED":
        codes.append("NEW_CONTROLLED_EVIDENCE_TO_REOPEN")
        if classified["classification"] == "REJECTED" and "CONTRADICTION_PRESENT" in classified["reason_codes"]:
            codes.append("RESOLVE_CONTRADICTION")
    else:
        if claim is None:
            codes.append("STATE_IDENTITY_CLAIM")
        if sig["band"] is None:
            codes.append("SUPPLY_EXPECTED_SIGNATURE")
        if sig["episodes"] < pol["min_signature_events"]:
            codes.append("COLLECT_SENSE_EPISODES")
        elif sig["band"] is not None and sig["match"] != "MATCH":
            codes.append("IMPROVE_SIGNATURE_CONSISTENCY")
        if not corr:
            codes.append("ADD_INDEPENDENT_CORROBORATOR")
        elif not any(c["verdict"] == "CONFIRMS" for c in corr):
            codes.append("COLLECT_CORROBORATED_EPISODES")
        if not pos:
            codes.append("OWNER_CONTROLLED_TEST")
        if cls == "UNVERIFIED_STALE":
            codes.append("REFRESH_EVIDENCE")
    if scope == "SHARED_LOAD_CLASS":
        codes.append("VEHICLE_IDENTITY_NOT_DERIVABLE")
    return codes


def _upgrades(codes: list[str]) -> list[dict[str, str]]:
    return [{"code": c, "text": UPGRADE_CATALOG[c]} for c in dict.fromkeys(codes)]


# ---------------------------------------------------------------------------------------------
# top-level evaluation + consumer feeds


def _placeholder(did: str, base: dict[str, Any], pins: dict[str, Any]) -> dict[str, Any]:
    pinned_ev = did in pins["shared_ev"] or base["shared_load_class"] == "EV"
    return {"device_id": did, "kind": base["kind"], "shared_load_class": "EV" if pinned_ev else None, "sense_name": None,
            "claim": EV_CLAIM if pinned_ev else base["primary_claim"], "expected": None, "episodes": [], "corroborators": [],
            "attestations": [], "in_packet": False}


def evaluate(payload: Any, register: dict[str, Any] | None = None) -> dict[str, Any]:
    _require(register is not None and "pins" in register, "EXPLICIT_PRIVATE_REGISTER_REQUIRED")
    reg = register
    p = _obj(payload, "ROOT_NOT_OBJECT")
    _fields(p, ("schema_version", "authority", "evaluated_at", "observation_window", "policy", "devices"), (),
            "UNSUPPORTED_FIELDS")
    _require(p["schema_version"] == INPUT_SCHEMA, "SCHEMA_VERSION_MISMATCH")
    _require(p["authority"] == AUTHORITY, "AUTHORITY_ESCALATION_REFUSED")
    at = _ts(p["evaluated_at"], "EVALUATED_AT_INVALID")
    w = _obj(p["observation_window"], "OBSERVATION_WINDOW_INVALID")
    _fields(w, ("start", "end"), (), "OBSERVATION_WINDOW_INVALID")
    lower, upper = _ts(w["start"], "OBSERVATION_WINDOW_INVALID"), _ts(w["end"], "OBSERVATION_WINDOW_INVALID")
    _require(lower < upper <= at, "OBSERVATION_WINDOW_INVALID")
    pol = _policy(p["policy"])
    devices: dict[str, dict[str, Any]] = {}
    for raw in _list(p["devices"], "DEVICES_INVALID", MAX_DEVICES):
        dev = _device(raw, reg, lower, upper, at)
        _require(dev["device_id"] not in devices, "DEVICE_DUPLICATE")
        devices[dev["device_id"]] = dev
    for did, base in reg["devices"].items():
        devices.setdefault(did, _placeholder(did, base, reg["pins"]))
    results = [_evaluate_device(devices[did], reg["devices"].get(did), pol, at, reg["pins"]) for did in sorted(devices)]
    counts: dict[str, int] = {c: 0 for c in CLASS_ORDER}
    for r in results:
        counts[r["confidence_class"]] += 1
    phantoms = [{"device_id": r["device_id"], "phantom_status": r["phantom_status"]} for r in results
                if r["phantom_status"] in {"PHANTOM_SUSPECT", "NEVER_CORROBORATED", "DORMANT"}]
    return {
        "schema_version": RESULT_SCHEMA, "authority": AUTHORITY, "evaluated_at": at.isoformat(),
        "physical_actions": False, "usable_for_control": False, "sense_name_can_raise_class": False,
        "classifier_authority": "tools/evaluate_load_identity_confidence.py",
        "register_revision": reg["revision"],
        "policy": {"id": pol["id"], "evidence_refs": pol["evidence_refs"]},
        "summary": {"devices": len(results), "by_class": {k: v for k, v in counts.items() if v},
                    "usable_for_analytics": sum(1 for r in results if r["usable_for_analytics"]),
                    "review_required": sum(1 for r in results if r["confidence_class"].startswith("REVIEW_REQUIRED"))},
        "devices": results,
        "phantom_report": phantoms,
        "consumer_feeds": consumer_feeds(results, reg["pins"]),
    }


def consumer_feeds(results: list[dict[str, Any]], pins: dict[str, Any]) -> dict[str, Any]:
    """Read-only projections for downstream consumers. They grant nothing; consumers keep their authority."""
    refused = sorted(r["device_id"] for r in results if pins["pool_claim"] in r["rejected_claims"]
                     or (r["identity_claim"] == pins["pool_claim"] and not r["usable_for_analytics"]))
    confirmed_named = sorted(r["device_id"] for r in results
                             if r["identity_claim"] == pins["pool_claim"] and r["usable_for_analytics"])
    ev = [{"device_id": r["device_id"], "confidence_class": r["confidence_class"],
           "usable_as": "COMBINED_EV_POWER_CONTEXT" if r["usable_for_analytics"] else "NOT_USABLE_UNVERIFIED"}
          for r in results if r["identity_scope"] == "SHARED_LOAD_CLASS"]
    eligible = [{"device_id": r["device_id"], "identity_claim": r["identity_claim"]} for r in results
                if r["usable_for_analytics"] and r["identity_scope"] == "DEVICE"]
    return {
        "feed_authority": "READ_ONLY_INPUT_NO_CONTROL",
        "pool_pump_111": {
            "accepted_power_source": pins["pool_source"],
            pins["pool_authority_key"]: True,
            "refused_named_sense_devices": refused,
            "confirmed_named_sense_devices_not_enabled": confirmed_named,
            "named_device_use_requires_111_contract_change": True,
            "evaluator_refuses_named_device": pins["pool_evaluator_refuses_named_device"],
        },
        "ev_headroom_127": {
            "ev_shared_loads": ev,
            "vehicle_identity": "PROHIBITED",
            "usable_for_overload_protection": False,
            "usable_for_control": False,
            "note_code": "EV_LOAD_SHARED_COMBINED_ONLY",
        },
        "predictive_maintenance_81": {
            "eligible_device_identities": eligible,
            "excluded_devices": sum(1 for r in results if r["kind"] == "DETECTED_DEVICE") - len(eligible),
        },
    }


# ---------------------------------------------------------------------------------------------
# extras: owner labeling worksheet, register drift check

WORKSHEET_SKIP: Final = frozenset({"CONFIRMED", "CATEGORY_ONLY", "REJECTED"})


def worksheet(result: dict[str, Any]) -> dict[str, Any]:
    """Owner labeling worksheet: devices that need evidence, most-upgradable first, plus a fill-in template."""
    rows = [r for r in result["devices"] if r["confidence_class"] not in WORKSHEET_SKIP]
    rank = {c: i for i, c in enumerate(CLASS_ORDER)}
    rows.sort(key=lambda r: (rank[r["confidence_class"]], -r["evidence_score"], r["device_id"]))
    items = [{"priority": i + 1, "device_id": r["device_id"], "confidence_class": r["confidence_class"],
              "identity_claim": r["identity_claim"], "evidence_score": r["evidence_score"],
              "phantom_status": r["phantom_status"], "next_evidence": [u["code"] for u in r["upgrade_path"]]}
             for i, r in enumerate(rows)]
    template = [{"device_id": r["device_id"], "attestations": [{
        "kind": TEMPLATE_PLACEHOLDER, "identity_claim": r["identity_claim"] or TEMPLATE_PLACEHOLDER,
        "observed_at": None, "repeats": None, "evidence_ref": None}]}
        for r in rows if r["identity_scope"] == "DEVICE" or r["identity_claim"] == EV_CLAIM]
    return {"schema_version": WORKSHEET_SCHEMA, "authority": AUTHORITY, "evaluated_at": result["evaluated_at"],
            "register_revision": result["register_revision"], "items": items,
            "attestation_template": template,
            "instructions": ("Fill kind (CONTROLLED_TEST_MATCH, CONTROLLED_TEST_MISMATCH, OWNER_IDENTITY_ATTESTATION "
                             "or OWNER_LABEL_DISPUTE), observed_at, repeats and evidence_ref. Unfilled rows are "
                             "refused. Use ordinary appliance use; do not stage risky conditions. EV rows never "
                             "take a vehicle name.")}


def render_worksheet_markdown(ws: dict[str, Any]) -> str:
    lines = [
        "# Sense device labeling worksheet",
        "",
        f"Evaluated at `{ws['evaluated_at']}` against register `{ws['register_revision']}`. "
        "Identity evidence only: nothing here controls a device.",
        "",
        "| # | Device | Current class | Claim under test | Next evidence | Owner result | Date | Evidence ref |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for it in ws["items"]:
        claim = it["identity_claim"] or "(none yet)"
        nxt = "; ".join(UPGRADE_CATALOG[c] for c in it["next_evidence"])
        lines.append(f"| {it['priority']} | `{it['device_id']}` | {it['confidence_class']} | {claim} | {nxt} |  |  |  |")
    lines += ["", ws["instructions"], ""]
    return "\n".join(lines)


