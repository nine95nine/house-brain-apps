#!/usr/bin/env python3
"""House Brain AI kill-switch state contract R1 (Initiative #56) — pure core.

Versioned local kill-switch STATE records, fail-closed resolution and transition authority.
Composes *above* the accepted Global AI Safety Interlock R1 (tools/evaluate_ai_safety_interlock.py);
it never replaces or weakens it.

Pure and deterministic: no clock reads, no filesystem, no network. Every time input is an explicit
``YYYY-MM-DDTHH:MM:SSZ`` string supplied by the caller. Resolution never raises for data problems:
missing, corrupt, tampered, future-dated or otherwise invalid state resolves to ``FULL_STOP``.

States, least to most permissive::

    FULL_STOP     every AI surface blocked (projects to interlock mode EMERGENCY_STOP)
    READ_ONLY     READ and ADVISORY action classes may proceed to the interlock; MUTATION blocked
    ARMED_NORMAL  every action class may proceed to the interlock (which still decides)

Dead-man rule: ARMED_NORMAL must carry an expiry no further than ``MAX_TTL_S`` ahead; READ_ONLY may
carry one. On expiry the effective state steps down one level. FULL_STOP never expires: there is no
automatic re-enablement.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Final

RECORD_SCHEMA: Final = "house_brain_ai_kill_switch.state.v1"
VIEW_SCHEMA: Final = "house_brain_ai_kill_switch.view.v1"
POLICY_SCHEMA: Final = "house_brain_ai_kill_switch.policy.v1"

STATES: Final = ("FULL_STOP", "READ_ONLY", "ARMED_NORMAL")  # ordered least -> most permissive
RANK: Final = {s: i for i, s in enumerate(STATES)}
ACTION_CLASSES: Final = {"READ": "READ_ONLY", "ADVISORY": "READ_ONLY", "MUTATION": "ARMED_NORMAL"}
EXPIRY_STEP_DOWN: Final = {"ARMED_NORMAL": "READ_ONLY", "READ_ONLY": "FULL_STOP"}
EXPIRY_REQUIRED: Final = frozenset({"ARMED_NORMAL"})
MAX_TTL_S: Final = {"ARMED_NORMAL": 7 * 86400, "READ_ONLY": 30 * 86400}
MAX_FUTURE_SKEW_S: Final = 300
MAX_RECORD_BYTES: Final = 4096
GENESIS_PREV: Final = "0" * 64

# Actor authority. ANY = may tighten or loosen; TIGHTEN_ONLY = may only move to a strictly or equally
# restrictive state; GENESIS_ONLY = may only write generation 1 as FULL_STOP.
ACTOR_AUTHORITY: Final = {
    "OWNER_LOCAL_UI": "ANY",
    "OWNER_CLI": "ANY",
    "OWNER_PHYSICAL_BUTTON": "TIGHTEN_ONLY",
    "SAFETY_AUTOMATION": "TIGHTEN_ONLY",
    "AI_SURFACE": "TIGHTEN_ONLY",
    "DEAD_MAN_TIMER": "TIGHTEN_ONLY",
    "BOOTSTRAP": "GENESIS_ONLY",
}
OWNER_ACTORS: Final = frozenset(a for a, v in ACTOR_AUTHORITY.items() if v == "ANY")
STAGED_RECOVERY: Final = True  # FULL_STOP -> ARMED_NORMAL directly is refused; go via READ_ONLY

# Owner decisions 2026-09-29 (#56; docs/governance/OWNER_DECISIONS_2026-09-29.md): the local store
# (Option A) is the source of truth, the steady state is READ_ONLY and arming defaults to 12 h.
# These are defaults for owner-facing tooling only. They do not change the policy above: genesis
# is still FULL_STOP, ARMED_NORMAL still needs an explicit expiry in every record, and nothing
# re-arms automatically.
OWNER_SOURCE_OF_TRUTH: Final = "LOCAL_STORE_OPTION_A"
OWNER_STEADY_STATE: Final = "READ_ONLY"
OWNER_DEFAULT_ARM_TTL_S: Final = 12 * 3600
OWNER_DECISION_REF: Final = "OWNER_DECISIONS_2026-09-29#56"

REASON_CODES: Final = frozenset({
    "GENESIS_FAIL_CLOSED", "OWNER_ROUTINE", "OWNER_INVESTIGATION", "OWNER_MAINTENANCE_WINDOW",
    "OWNER_RECOVERY", "SUSPECTED_AI_MISBEHAVIOR", "SECURITY_INCIDENT", "PROVIDER_INCIDENT",
    "DEPENDENCY_INCIDENT", "PHYSICAL_PANIC_BUTTON", "DEAD_MAN_EXPIRY", "AI_SELF_STOP", "DRILL",
    "TAMPER_REINITIALIZE",
})
RECORD_KEYS: Final = frozenset({
    "schema_version", "generation", "state", "set_by", "set_at", "reason_code", "expires_at",
    "prev_record_sha256", "record_sha256",
})
ACTOR_KEYS: Final = frozenset({"actor_class", "actor_ref"})
TS_RE: Final = re.compile(r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
HEX64_RE: Final = re.compile(r"\A[0-9a-f]{64}\Z")
ACTOR_REF_RE: Final = re.compile(r"\A[a-z0-9][a-z0-9_.-]{0,47}\Z")
MAX_GENERATION: Final = 2**53 - 1


class KillSwitchError(ValueError):
    """Bounded, metadata-only error code."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise KillSwitchError(code)


def parse_ts(value: Any, code: str = "TIMESTAMP_INVALID") -> datetime:
    _require(type(value) is str and TS_RE.fullmatch(value) is not None, code)
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise KillSwitchError(code) from None


def format_ts(value: datetime) -> str:
    _require(value.tzinfo is not None, "TIMESTAMP_NOT_AWARE")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def compute_record_sha256(record: dict[str, Any]) -> str:
    body = {k: v for k, v in record.items() if k != "record_sha256"}
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise KillSwitchError("DUPLICATE_KEY")
        out[key] = value
    return out


def _reject_constant(_: str) -> Any:
    raise KillSwitchError("NONFINITE_NUMBER")


def decode_record_bytes(raw: Any) -> dict[str, Any]:
    """Strictly decode one record (bytes). Raises KillSwitchError on any problem."""
    _require(type(raw) is bytes, "RECORD_NOT_BYTES")
    _require(0 < len(raw) <= MAX_RECORD_BYTES, "RECORD_SIZE_INVALID")
    try:
        text = raw.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_strict_pairs, parse_constant=_reject_constant)
    except KillSwitchError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise KillSwitchError("RECORD_JSON_INVALID") from None
    _require(type(value) is dict, "RECORD_NOT_OBJECT")
    return value


def validate_record(record: Any) -> dict[str, Any]:
    """Validate shape, invariants and self-hash of one record. Returns it unchanged."""
    _require(type(record) is dict, "RECORD_NOT_OBJECT")
    _require(set(record) == RECORD_KEYS, "RECORD_FIELDS_INVALID")
    _require(record["schema_version"] == RECORD_SCHEMA, "RECORD_SCHEMA_MISMATCH")
    gen = record["generation"]
    _require(type(gen) is int and 1 <= gen <= MAX_GENERATION, "GENERATION_INVALID")
    _require(type(record["state"]) is str and record["state"] in STATES, "STATE_INVALID")
    actor = record["set_by"]
    _require(type(actor) is dict and set(actor) == ACTOR_KEYS, "SET_BY_INVALID")
    _require(type(actor["actor_class"]) is str and actor["actor_class"] in ACTOR_AUTHORITY, "ACTOR_CLASS_INVALID")
    _require(type(actor["actor_ref"]) is str and ACTOR_REF_RE.fullmatch(actor["actor_ref"]) is not None,
             "ACTOR_REF_INVALID")
    set_at = parse_ts(record["set_at"], "SET_AT_INVALID")
    _require(type(record["reason_code"]) is str and record["reason_code"] in REASON_CODES, "REASON_CODE_INVALID")
    expires = record["expires_at"]
    state = record["state"]
    if expires is None:
        _require(state not in EXPIRY_REQUIRED, "EXPIRY_REQUIRED")
    else:
        _require(state != "FULL_STOP", "FULL_STOP_CANNOT_EXPIRE")
        exp = parse_ts(expires, "EXPIRES_AT_INVALID")
        ttl = (exp - set_at).total_seconds()
        _require(ttl > 0, "EXPIRY_NOT_AFTER_SET_AT")
        _require(ttl <= MAX_TTL_S[state], "EXPIRY_EXCEEDS_MAX_TTL")
    prev = record["prev_record_sha256"]
    _require(type(prev) is str and HEX64_RE.fullmatch(prev) is not None, "PREV_HASH_INVALID")
    if gen == 1:
        _require(actor["actor_class"] == "BOOTSTRAP", "GENESIS_ACTOR_INVALID")
        _require(state == "FULL_STOP", "GENESIS_NOT_FULL_STOP")
    else:
        _require(actor["actor_class"] != "BOOTSTRAP", "BOOTSTRAP_AFTER_GENESIS")
        _require(prev != GENESIS_PREV, "PREV_HASH_INVALID")
    digest = record["record_sha256"]
    _require(type(digest) is str and HEX64_RE.fullmatch(digest) is not None, "RECORD_HASH_INVALID")
    _require(digest == compute_record_sha256(record), "RECORD_HASH_MISMATCH")
    return record


def effective_state(record: dict[str, Any], now: str) -> tuple[str, list[str]]:
    """State after the dead-man rule, for an already-validated record."""
    now_dt = parse_ts(now, "NOW_INVALID")
    state = record["state"]
    if record["expires_at"] is not None and now_dt >= parse_ts(record["expires_at"]):
        return EXPIRY_STEP_DOWN[state], [f"DEAD_MAN_EXPIRED_{state}"]
    return state, []


def _fail_view(reason: str, *, generation: Any = None, recorded: Any = None) -> dict[str, Any]:
    return {
        "schema_version": VIEW_SCHEMA,
        "effective_state": "FULL_STOP",
        "recorded_state": recorded,
        "generation": generation,
        "record_sha256": None,
        "expires_at": None,
        "fail_closed": True,
        "reason_codes": [f"FAIL_CLOSED_{reason}"],
        "authority": "KILL_SWITCH_VIEW_ONLY_NO_EXECUTION",
    }


def resolve(raw: Any, now: Any, *, log_head_sha256: Any = None, require_log_head: bool = False) -> dict[str, Any]:
    """Resolve raw state-file bytes into an effective view. Never raises; fails closed to FULL_STOP."""
    if type(now) is not str or TS_RE.fullmatch(now) is None:
        return _fail_view("NOW_INVALID")
    try:
        now_dt = parse_ts(now, "NOW_INVALID")
    except KillSwitchError:
        return _fail_view("NOW_INVALID")
    if raw is None:
        return _fail_view("STATE_MISSING")
    try:
        record = validate_record(decode_record_bytes(raw))
    except KillSwitchError as exc:
        return _fail_view(f"STATE_{exc}")
    except Exception:  # noqa: BLE001 - defence in depth: resolution must never raise open
        return _fail_view("STATE_UNEXPECTED_SHAPE")
    if require_log_head or log_head_sha256 is not None:
        if log_head_sha256 != record["record_sha256"]:
            return _fail_view("STATE_LOG_DIVERGED", generation=record["generation"], recorded=record["state"])
    if (parse_ts(record["set_at"]) - now_dt).total_seconds() > MAX_FUTURE_SKEW_S:
        return _fail_view("STATE_FROM_FUTURE", generation=record["generation"], recorded=record["state"])
    state, reasons = effective_state(record, now)
    expires = record["expires_at"] if not reasons else None
    return {
        "schema_version": VIEW_SCHEMA,
        "effective_state": state,
        "recorded_state": record["state"],
        "generation": record["generation"],
        "record_sha256": record["record_sha256"],
        "expires_at": expires,
        "fail_closed": False,
        "reason_codes": reasons or [f"KILL_SWITCH_{state}"],
        "authority": "KILL_SWITCH_VIEW_ONLY_NO_EXECUTION",
    }


def validate_transition(current: dict[str, Any] | None, proposed: dict[str, Any], now: str) -> None:
    """Authority rules for writing ``proposed`` on top of ``current`` (None = no chain yet).

    ``current`` must already be validated. Comparison uses current's *effective* state at ``now`` so
    that an expired ARMED_NORMAL is treated as READ_ONLY. Raises KillSwitchError on refusal.
    """
    validate_record(proposed)
    _require(proposed["set_at"] == now, "SET_AT_NOT_NOW")
    actor = proposed["set_by"]["actor_class"]
    authority = ACTOR_AUTHORITY[actor]
    if current is None:
        _require(proposed["generation"] == 1, "GENERATION_NOT_GENESIS")
        _require(authority == "GENESIS_ONLY", "GENESIS_ACTOR_INVALID")
        return
    _require(authority != "GENESIS_ONLY", "BOOTSTRAP_AFTER_GENESIS")
    _require(proposed["generation"] == current["generation"] + 1, "GENERATION_NOT_NEXT")
    _require(proposed["prev_record_sha256"] == current["record_sha256"], "PREV_HASH_NOT_HEAD")
    _require(parse_ts(proposed["set_at"]) >= parse_ts(current["set_at"]), "SET_AT_REGRESSED")
    cur_state, _ = effective_state(current, now)
    new_state = proposed["state"]
    loosening = RANK[new_state] > RANK[cur_state]
    if authority == "TIGHTEN_ONLY":
        _require(not loosening, "ACTOR_MAY_ONLY_TIGHTEN")
        # A tighten-only actor may not extend the lifetime of a still-live expiring state either.
        if new_state == cur_state and new_state != "FULL_STOP" and current["state"] == cur_state:
            cur_exp = current["expires_at"]
            if cur_exp is not None:
                _require(proposed["expires_at"] is not None
                         and parse_ts(proposed["expires_at"]) <= parse_ts(cur_exp), "ACTOR_MAY_NOT_EXTEND_EXPIRY")
    if STAGED_RECOVERY and cur_state == "FULL_STOP" and new_state == "ARMED_NORMAL":
        raise KillSwitchError("STAGED_RECOVERY_REQUIRED")
    if loosening:
        _require(actor in OWNER_ACTORS, "LOOSEN_REQUIRES_OWNER")


def build_record(*, prev: dict[str, Any] | None, state: str, actor_class: str, actor_ref: str,
                 reason_code: str, now: str, expires_at: str | None = None,
                 genesis_prev: str = GENESIS_PREV) -> dict[str, Any]:
    """Construct a hashed record following ``prev`` (None = genesis). Does not check authority."""
    record = {
        "schema_version": RECORD_SCHEMA,
        "generation": 1 if prev is None else prev["generation"] + 1,
        "state": state,
        "set_by": {"actor_class": actor_class, "actor_ref": actor_ref},
        "set_at": now,
        "reason_code": reason_code,
        "expires_at": expires_at,
        "prev_record_sha256": genesis_prev if prev is None else prev["record_sha256"],
    }
    record["record_sha256"] = compute_record_sha256(record)
    return record


def verify_chain(records: list[Any]) -> dict[str, Any]:
    """Verify a complete audit chain: hashes, links, generations and every transition's authority."""
    _require(type(records) is list and records, "CHAIN_EMPTY")
    prev: dict[str, Any] | None = None
    for record in records:
        validate_record(record)
        if prev is None:
            _require(record["generation"] == 1, "CHAIN_NOT_FROM_GENESIS")
        validate_transition(prev, record, record["set_at"])
        prev = record
    assert prev is not None
    return {"generation": prev["generation"], "head_record_sha256": prev["record_sha256"],
            "state": prev["state"], "length": len(records)}


def owner_defaults_document() -> dict[str, Any]:
    """Owner-decided defaults; must equal the contract's ``owner_decided_defaults`` block."""
    return {
        "decision_ref": OWNER_DECISION_REF,
        "decided_on": "2026-09-29",
        "source_of_truth": OWNER_SOURCE_OF_TRUTH,
        "steady_state": OWNER_STEADY_STATE,
        "armed_normal_default_ttl_s": OWNER_DEFAULT_ARM_TTL_S,
        "scope": "OWNER_TOOLING_DEFAULTS_ONLY_POLICY_GENESIS_AND_EXPIRY_RULES_UNCHANGED",
    }


def policy_document() -> dict[str, Any]:
    """The policy as data; must equal contracts/house_brain_ai_kill_switch.v1.json["policy"]."""
    return {
        "schema_version": POLICY_SCHEMA,
        "states_least_to_most_permissive": list(STATES),
        "action_class_minimum_state": dict(ACTION_CLASSES),
        "expiry_step_down": dict(EXPIRY_STEP_DOWN),
        "expiry_required_states": sorted(EXPIRY_REQUIRED),
        "max_ttl_s": dict(MAX_TTL_S),
        "full_stop_may_expire": False,
        "max_future_skew_s": MAX_FUTURE_SKEW_S,
        "max_record_bytes": MAX_RECORD_BYTES,
        "actor_authority": dict(ACTOR_AUTHORITY),
        "staged_recovery_full_stop_to_armed_normal_refused": STAGED_RECOVERY,
        "reason_codes": sorted(REASON_CODES),
    }
