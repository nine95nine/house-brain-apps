"""A6.0 repair record contract, ``house_brain_onm_repair_record.v1`` (design ONM_A6_INVESTIGATOR_DESIGN_R1 §5).

It fills the slot Systems AI creates on every case (``cases.repair_slot()``, design C1) and is the reference for
the checks Systems AI applies on ``POST /link/v1/repair`` (A6.1). Every claim carries evidence ids. A record is data:
it holds no authority and every request in it stays the owner's tap.
"""
from __future__ import annotations

import json
import re

SCHEMA = "house_brain_onm_repair_record.v1"
STATES = ("NOT_STARTED", "INVESTIGATING", "CAUSE_PROPOSED", "REPAIR_PROPOSED", "AWAITING_OWNER", "VERIFYING",
          "CLOSED_VERIFIED", "CLOSED_UNRESOLVED", "NEEDS_ENGINEER")
OBLIGATIONS = ("service_restored", "cause_corrected", "side_effects_reconciled", "prevention_proven")
ONM_MEASURES = ("cause_corrected", "side_effects_reconciled")       # the two Systems AI reads NOT_MEASURED
SYSTEMS_AI_MEASURES = ("service_restored", "prevention_proven")
CHECK_STATUSES = ("NOT_MEASURED", "UNVERIFIED", "VERIFIED", "FAILED")
# A6.1 follow-up (2026-11-15): SPECIALIST, the answer of a specialist App that Systems AI 0.3.13 asked (A4).
HYPOTHESIS_KINDS = ("PLAYBOOK", "SPECIALIST", "RESEARCH", "SIMILAR_CASE", "HYPOTHESIS", "DEPENDENCY")
REQUEST_SOURCES = ("playbook", "suggestion")
PREVENTION_STATUSES = ("EXISTING_PLAYBOOK", "CANDIDATE", "NONE_YET")
TIERS = ("T0", "T1")
MAX_BYTES = 64 * 1024
MAX_ITEMS = 20
MAX_TEXT = 600
MAX_EVIDENCE = 20
ID = re.compile(r"[A-Za-z0-9_.:@/+-]{1,160}\Z")
CASE_ID = re.compile(r"[0-9a-f]{12}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
TOP_KEYS = frozenset(("schema", "case", "rev", "ticket", "seq", "owner", "state", "cause", "obligations",
                      "prevention", "requests", "basis", "authority"))


class RepairHold(ValueError):
    """The record is refused as a whole; nothing in it is stored."""


def _text(value, limit=MAX_TEXT) -> bool:
    return type(value) is str and 0 < len(value) <= limit and "\x00" not in value


def _evidence(value, *, required: bool) -> bool:
    if type(value) is not list or len(value) > MAX_EVIDENCE or (required and not value):
        return False
    return all(type(e) is str and ID.fullmatch(e) for e in value) and len(set(value)) == len(value)


def _int(value, low, high) -> bool:
    return type(value) is int and low <= value <= high


def validate(doc) -> None:
    """Raise RepairHold with one reason code, or return None. Strict: unknown keys, wrong types, a claim without
    evidence, or a verified close that is not fully measured are all refused."""
    try:
        size = len(json.dumps(doc, sort_keys=True, ensure_ascii=False).encode())
    except (TypeError, ValueError) as exc:
        raise RepairHold("REPAIR_CONTRACT") from exc
    if size > MAX_BYTES:
        raise RepairHold("REPAIR_TOO_LARGE")
    if type(doc) is not dict or set(doc) != TOP_KEYS or doc["schema"] != SCHEMA:
        raise RepairHold("REPAIR_CONTRACT")
    if (type(doc["case"]) is not str or not CASE_ID.fullmatch(doc["case"]) or not _int(doc["rev"], 0, 2**62)
            or type(doc["ticket"]) is not str or not HEX64.fullmatch(doc["ticket"])
            or not _int(doc["seq"], 1, 2**63 - 1) or doc["owner"] != "O&M" or doc["state"] not in STATES
            or doc["authority"] != "NONE"):
        raise RepairHold("REPAIR_CONTRACT")

    cause = doc["cause"]
    if (type(cause) is not dict or set(cause) != {"hypotheses", "upstream", "downstream"}
            or type(cause["hypotheses"]) is not list or len(cause["hypotheses"]) > MAX_ITEMS):
        raise RepairHold("REPAIR_CAUSE_CONTRACT")
    for h in cause["hypotheses"]:
        if (type(h) is not dict or set(h) != {"kind", "text", "evidence"} or h["kind"] not in HYPOTHESIS_KINDS
                or not _text(h["text"])):
            raise RepairHold("REPAIR_CAUSE_CONTRACT")
        if not _evidence(h["evidence"], required=True):
            raise RepairHold("REPAIR_EVIDENCE_MISSING")
    for side in ("upstream", "downstream"):
        if (type(cause[side]) is not list or len(cause[side]) > MAX_ITEMS
                or not all(type(x) is str and ID.fullmatch(x) for x in cause[side])):
            raise RepairHold("REPAIR_CAUSE_CONTRACT")

    obligations = doc["obligations"]
    if type(obligations) is not dict or set(obligations) != set(OBLIGATIONS):
        raise RepairHold("REPAIR_OBLIGATION_CONTRACT")
    for name, item in obligations.items():
        if (type(item) is not dict or set(item) != {"status", "evidence", "measured_by"}
                or item["status"] not in CHECK_STATUSES):
            raise RepairHold("REPAIR_OBLIGATION_CONTRACT")
        owner = "onm" if name in ONM_MEASURES else "systems_ai"
        if item["measured_by"] != owner:
            raise RepairHold("REPAIR_OBLIGATION_OWNER")
        if not _evidence(item["evidence"], required=item["status"] in ("VERIFIED", "FAILED")):
            raise RepairHold("REPAIR_EVIDENCE_MISSING")
        if name in ONM_MEASURES and item["status"] == "NOT_MEASURED":
            raise RepairHold("REPAIR_OBLIGATION_CONTRACT")   # O&M's own checks are never left unmeasured
    if doc["state"] == "CLOSED_VERIFIED" and any(o["status"] != "VERIFIED" for o in obligations.values()):
        raise RepairHold("REPAIR_CLOSE_NOT_VERIFIED")

    prevention = doc["prevention"]
    if (type(prevention) is not dict or set(prevention) != {"status", "playbook", "candidate"}
            or prevention["status"] not in PREVENTION_STATUSES):
        raise RepairHold("REPAIR_PREVENTION_CONTRACT")
    if prevention["status"] == "EXISTING_PLAYBOOK":
        ok = type(prevention["playbook"]) is str and ID.fullmatch(prevention["playbook"]) and prevention["candidate"] is None
    elif prevention["status"] == "CANDIDATE":
        c = prevention["candidate"]
        ok = (prevention["playbook"] is None and type(c) is dict
              and set(c) == {"signature", "checks", "verify", "not_when", "regression"}
              and type(c["signature"]) is list and 0 < len(c["signature"]) <= MAX_ITEMS
              and all(type(x) is str and ID.fullmatch(x) for x in c["signature"])
              and type(c["checks"]) is list and len(c["checks"]) <= MAX_ITEMS and all(_text(x) for x in c["checks"])
              and all(_text(c[k]) for k in ("verify", "not_when", "regression")))
    else:
        ok = prevention["playbook"] is None and prevention["candidate"] is None
    if not ok:
        raise RepairHold("REPAIR_PREVENTION_CONTRACT")

    requests = doc["requests"]
    if type(requests) is not list or len(requests) > 5:
        raise RepairHold("REPAIR_REQUEST_CONTRACT")
    for r in requests:
        if (type(r) is not dict or set(r) != {"source", "ref", "text"} or r["source"] not in REQUEST_SOURCES
                or type(r["ref"]) is not str or not ID.fullmatch(r["ref"]) or not _text(r["text"])):
            raise RepairHold("REPAIR_REQUEST_CONTRACT")

    basis = doc["basis"]
    if (type(basis) is not dict or set(basis) != {"tier", "library", "rules"} or basis["tier"] not in TIERS
            or not _text(basis["library"], 80) or type(basis["rules"]) is not list or len(basis["rules"]) > MAX_ITEMS
            or not all(type(x) is str and ID.fullmatch(x) for x in basis["rules"])):
        raise RepairHold("REPAIR_BASIS_CONTRACT")
