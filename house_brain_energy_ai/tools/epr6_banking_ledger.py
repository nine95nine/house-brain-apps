#!/usr/bin/env python3
"""#139 APS EPR-6 export-banking ledger: deterministic, advisory, repository-only.

Takes owner-supplied monthly bill records (``house_brain_epr6_bill_record.v1``, defined in
``contracts/house_brain_epr6_banking_policy.v1.json``) and keeps two separate kWh banks, one for
On-Peak and one for Off-Peak, exactly as the retained APS EPR-6 statements do:

    net = prior balance + credits earned - APS energy use          (per period, never pooled)
    net >= 0  -> carried to next month, 0 kWh billed
    net <  0  -> carried balance 0, |net| kWh billed

Policy parameters come from the contract and carry an evidence status. ``EVIDENCE_BOUND`` values
(kWh unit, TOU-period scope, netting rule, monthly carry-forward, no cross-period offset) cannot be
overridden. ``PLACEHOLDER`` values (annual true-up billing month, settlement mode, payout rate) stay
null unless the owner declares them with an evidence reference; until then the ledger runs, says
the true-up is unresolved, and never settles anything on its own. ``PENDING_SOURCE_EVIDENCE``
values (solar interconnection / PTO date, grandfathering term end) are reported as pending and are
never inferred from bill history.

Utility statements outrank this ledger. Each bill is reconciled against its own billed values
(prior balance, net balance, billed kWh, true-up settlement) and every discrepancy is flagged with
its signed size. The carried balance after a bill is the billed net balance when the statement
gives one (authority), otherwise the computed one; an independent pure-computation chain is also
kept so cumulative drift is visible.

Extras in the same module:
- a true-up forecast (same month last year, or an owner monthly profile) giving projected
  settlement kWh/value per period and the *marginal free credit*: the kWh a flexible load can add
  now without raising any billed kWh before the true-up (minimum of the projected bank path);
- adapters that let the accepted #147 TOU load-shift evaluator and the #111 pump solar-window tool
  read the bank without changing either tool's contract.

No clock read, no network, no persistence, no control authority. Errors are reason codes and never
echo a supplied value. Account identifiers are refused, never stored.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Final

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

POLICY_PATH: Final = ROOT / "contracts" / "house_brain_epr6_banking_policy.v1.json"
POLICY_SCHEMA: Final = "house_brain_epr6_banking_policy.v1"
INPUT_SCHEMA: Final = "house_brain_epr6_ledger.input.v1"
RESULT_SCHEMA: Final = "house_brain_epr6_ledger.result.v1"
BILL_SCHEMA: Final = "house_brain_epr6_bill_record.v1"
AUTHORITY: Final = "ADVISORY_LEDGER_ONLY_NO_CONTROL"
PERIODS: Final = ("on_peak", "off_peak")
TOU_NAME: Final = {"on_peak": "ON_PEAK", "off_peak": "OFF_PEAK"}
PERIOD_FIELDS: Final = ("prior_balance_kwh", "credits_earned_kwh", "energy_use_kwh", "net_balance_kwh", "billed_kwh")
TRUE_UP_FIELDS: Final = ("on_peak_settled_kwh", "off_peak_settled_kwh", "settlement_amount")
BILL_FIELDS: Final = ("billing_period", "period_start", "period_end", "on_peak", "off_peak", "true_up_billed",
                      "source_class", "evidence_ref")
TOP_FIELDS: Final = ("schema_version", "authority", "ledger_id", "opening_balance", "owner_parameters",
                     "reconciliation_tolerance_kwh", "bills", "forecast")
OWNER_PARAMS: Final = ("annual_true_up_billing_month", "true_up_settlement_mode", "true_up_payout_rate_per_kwh")
SETTLEMENT_MODES: Final = ("PAYOUT_AT_RATE", "EXPIRE_UNPAID")
FORECAST_METHODS: Final = ("SAME_MONTH_LAST_YEAR", "OWNER_MONTHLY_PROFILE")
MONTH_RE: Final = re.compile(r"^(\d{4})-(\d{2})$")
DATE_RE: Final = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
REF_RE: Final = re.compile(r"^[A-Za-z0-9_.:/#@+\-]{1,240}$")
TOKEN_RE: Final = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
LONG_DIGITS_RE: Final = re.compile(r"\d{7,}")
AMOUNT_TOLERANCE: Final = 0.01
EPS: Final = 1e-9


class Epr6Error(ValueError):
    """Fail-closed ledger error; the message is always a reason code, never a supplied value."""


def _require(ok: bool, code: str) -> None:
    if not ok:
        raise Epr6Error(code)


# ---------------------------------------------------------------------------------------------
# Policy contract
# ---------------------------------------------------------------------------------------------

def load_policy(path: Path = POLICY_PATH) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    _require(isinstance(data, dict) and data.get("schema_version") == POLICY_SCHEMA, "POLICY_SCHEMA_MISMATCH")
    params = data["parameters"]
    fixed = {
        "credit_unit": "KWH",
        "banking_scope": "TOU_PERIOD_SPECIFIC",
        "monthly_netting_rule": "NET_EQUALS_PRIOR_PLUS_EARNED_MINUS_USE_PER_PERIOD",
        "monthly_carry_forward": True,
        "cross_period_offset": False,
    }
    for key, expected in fixed.items():
        _require(params[key]["value"] == expected and params[key]["evidence_status"] == "EVIDENCE_BOUND",
                 "POLICY_EVIDENCE_BOUND_PARAMETER_DRIFT")
        _require(params[key]["owner_declarable"] is False, "POLICY_EVIDENCE_BOUND_PARAMETER_DRIFT")
    for key in ("solar_interconnection_date", "grandfathering_term_end"):
        status = params[key]["evidence_status"]
        _require(status in ("PENDING_SOURCE_EVIDENCE", "EVIDENCE_BOUND"), "POLICY_PENDING_PARAMETER_INVALID")
        _require((params[key]["value"] is None) == (status == "PENDING_SOURCE_EVIDENCE"),
                 "POLICY_PENDING_PARAMETER_INVALID")
        _require(params[key]["owner_declarable"] is False, "POLICY_PENDING_PARAMETER_INVALID")
    for key in OWNER_PARAMS:
        _require(params[key]["owner_declarable"] is True, "POLICY_PLACEHOLDER_PARAMETER_INVALID")
    return data


# ---------------------------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------------------------

def _exact(value: Any, fields: tuple[str, ...], code: str) -> dict[str, Any]:
    _require(isinstance(value, dict) and set(value) == set(fields), code)
    return value


def _num(value: Any, low: float, high: float, code: str) -> float:
    _require(isinstance(value, (int, float)) and not isinstance(value, bool), code)
    result = float(value)
    _require(math.isfinite(result) and low <= result <= high, code)
    return result


def _ref(value: Any, code: str) -> str:
    _require(isinstance(value, str) and REF_RE.fullmatch(value) is not None, code)
    # A run of 7+ digits is the shape of an account, meter or phone number; refs must not carry one.
    _require(LONG_DIGITS_RE.search(value) is None, "EVIDENCE_REF_ACCOUNT_IDENTIFIER_SHAPE")
    return value


def month_index(value: Any, code: str = "BILLING_PERIOD_INVALID") -> int:
    _require(isinstance(value, str), code)
    match = MONTH_RE.fullmatch(value)
    _require(match is not None, code)
    year, month = int(match.group(1)), int(match.group(2))
    _require(2000 <= year <= 2100 and 1 <= month <= 12, code)
    return year * 12 + (month - 1)


def month_label(index: int) -> str:
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def parse_date(value: Any, code: str) -> date:
    _require(isinstance(value, str), code)
    match = DATE_RE.fullmatch(value)
    _require(match is not None, code)
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        raise Epr6Error(code) from None


def _refuse_restricted(value: Any, restricted: frozenset[str]) -> None:
    """Refuse any object key that names a RESTRICTED account/person/payment field, at any depth."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                _require(isinstance(key, str), "INPUT_KEY_INVALID")
                _require(key.strip().lower() not in restricted, "ACCOUNT_IDENTIFIER_REFUSED")
                stack.append(child)
        elif isinstance(item, list):
            stack.extend(item)


def rnd(value: float | None, places: int = 3) -> float | None:
    if value is None:
        return None
    out = round(value, places)
    return 0.0 if out == 0 else out


# ---------------------------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------------------------

def _parse_period(raw: Any, name: str, cap: float) -> dict[str, float | None]:
    ledger = _exact(raw, PERIOD_FIELDS, f"{name.upper()}_FIELDS_INVALID")
    out: dict[str, float | None] = {}
    for key in PERIOD_FIELDS:
        value = ledger[key]
        nullable = key in ("prior_balance_kwh", "net_balance_kwh", "billed_kwh")
        if value is None:
            _require(nullable, f"{name.upper()}_{key.upper()}_REQUIRED")
            out[key] = None
        else:
            out[key] = _num(value, 0.0, cap, f"{name.upper()}_{key.upper()}_INVALID")
    return out


def parse_bill(raw: Any, policy: dict[str, Any]) -> dict[str, Any]:
    contract = policy["bill_record_contract"]
    cap = float(policy["limits"]["max_monthly_kwh_per_period"])
    _refuse_restricted(raw, frozenset(contract["restricted_fields"]))
    bill = _exact(raw, BILL_FIELDS, "BILL_FIELDS_INVALID")
    index = month_index(bill["billing_period"])
    start = parse_date(bill["period_start"], "PERIOD_START_INVALID")
    end = parse_date(bill["period_end"], "PERIOD_END_INVALID")
    _require(start < end and (end - start).days <= 62, "PERIOD_DATES_INVALID")
    _require(bill["source_class"] in contract["source_classes"], "SOURCE_CLASS_INVALID")
    true_up = bill["true_up_billed"]
    if true_up is not None:
        true_up = _exact(true_up, TRUE_UP_FIELDS, "TRUE_UP_BILLED_FIELDS_INVALID")
        true_up = {
            "on_peak_settled_kwh": _num(true_up["on_peak_settled_kwh"], 0.0, cap * 24, "TRUE_UP_SETTLED_KWH_INVALID"),
            "off_peak_settled_kwh": _num(true_up["off_peak_settled_kwh"], 0.0, cap * 24, "TRUE_UP_SETTLED_KWH_INVALID"),
            "settlement_amount": None if true_up["settlement_amount"] is None
            else _num(true_up["settlement_amount"], 0.0, 1_000_000.0, "TRUE_UP_AMOUNT_INVALID"),
        }
    return {
        "billing_period": month_label(index),
        "index": index,
        "period_start": start,
        "period_end": end,
        "on_peak": _parse_period(bill["on_peak"], "on_peak", cap),
        "off_peak": _parse_period(bill["off_peak"], "off_peak", cap),
        "true_up_billed": true_up,
        "source_class": bill["source_class"],
        "evidence_ref": _ref(bill["evidence_ref"], "BILL_EVIDENCE_REF_INVALID"),
    }


def _owner_parameters(raw: Any, policy: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    params = policy["parameters"]
    if raw is None:
        raw = {key: None for key in OWNER_PARAMS}
    _require(isinstance(raw, dict), "OWNER_PARAMETERS_INVALID")
    for key in raw:
        if key in params and not params[key]["owner_declarable"]:
            raise Epr6Error("EVIDENCE_BOUND_PARAMETER_OVERRIDE_REFUSED")
    _exact(raw, OWNER_PARAMS, "OWNER_PARAMETERS_INVALID")
    values: dict[str, Any] = {}
    refs: list[str] = []
    for key in OWNER_PARAMS:
        declared = raw[key]
        contract_value = params[key]["value"]
        if declared is None:
            values[key] = contract_value
            continue
        entry = _exact(declared, ("value", "evidence_ref"), "OWNER_PARAMETER_FIELDS_INVALID")
        refs.append(_ref(entry["evidence_ref"], "OWNER_PARAMETER_EVIDENCE_REF_INVALID"))
        value = entry["value"]
        if key == "annual_true_up_billing_month":
            _require(isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 12,
                     "TRUE_UP_MONTH_INVALID")
        elif key == "true_up_settlement_mode":
            _require(value in SETTLEMENT_MODES, "TRUE_UP_SETTLEMENT_MODE_INVALID")
        else:
            value = _num(value, 0.0, 10.0, "TRUE_UP_PAYOUT_RATE_INVALID")
        values[key] = value

    status: list[dict[str, Any]] = []
    for key in sorted(params):
        entry = params[key]
        declared = raw.get(key) is not None
        status.append({
            "parameter": key,
            "evidence_status": "OWNER_DECLARED" if declared else entry["evidence_status"],
            "contract_evidence_status": entry["evidence_status"],
            "value": values.get(key, entry["value"]),
        })
    return values, status, refs


# ---------------------------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------------------------

def _settlement_value(kwh: float, mode: str | None, rate: float | None) -> float | None:
    if mode == "EXPIRE_UNPAID":
        return 0.0
    if mode == "PAYOUT_AT_RATE" and rate is not None:
        return kwh * rate
    return None


def _next_true_up(as_of: int, month: int | None) -> int | None:
    """The first true-up billing period strictly after ``as_of`` (a true-up in ``as_of`` is past)."""
    if month is None:
        return None
    candidate = (as_of // 12) * 12 + (month - 1)
    while candidate <= as_of:
        candidate += 12
    return candidate


def evaluate(packet: Any, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    policy = policy if policy is not None else load_policy()
    contract = policy["bill_record_contract"]
    limits = policy["limits"]
    restricted = frozenset(contract["restricted_fields"])
    _require(isinstance(packet, dict), "INPUT_FIELDS_INVALID")
    _refuse_restricted(packet, restricted)
    _exact(packet, TOP_FIELDS, "INPUT_FIELDS_INVALID")
    _require(packet["schema_version"] == INPUT_SCHEMA, "SCHEMA_VERSION_MISMATCH")
    _require(packet["authority"] == AUTHORITY, "AUTHORITY_MISMATCH")
    _require(isinstance(packet["ledger_id"], str) and TOKEN_RE.fullmatch(packet["ledger_id"]) is not None,
             "LEDGER_ID_INVALID")
    _require(LONG_DIGITS_RE.search(packet["ledger_id"]) is None, "LEDGER_ID_ACCOUNT_IDENTIFIER_SHAPE")
    tolerance = packet["reconciliation_tolerance_kwh"]
    tolerance = (float(limits["default_reconciliation_tolerance_kwh"]) if tolerance is None
                 else _num(tolerance, 0.0, 1000.0, "RECONCILIATION_TOLERANCE_INVALID"))
    owner, parameter_status, owner_refs = _owner_parameters(packet["owner_parameters"], policy)
    true_up_month = owner["annual_true_up_billing_month"]
    settle_mode = owner["true_up_settlement_mode"]
    payout_rate = owner["true_up_payout_rate_per_kwh"]

    raw_bills = packet["bills"]
    _require(isinstance(raw_bills, list) and 1 <= len(raw_bills) <= int(limits["max_bills"]), "BILLS_INVALID")
    bills = sorted((parse_bill(item, policy) for item in raw_bills), key=lambda b: b["index"])
    for prev, cur in zip(bills, bills[1:]):
        _require(prev["index"] != cur["index"], "DUPLICATE_BILLING_PERIOD")

    reasons: set[str] = set()
    findings: list[dict[str, Any]] = []
    refs: set[str] = set(owner_refs)
    refs.update(ref for key in policy["parameters"] for ref in policy["parameters"][key]["evidence_refs"])

    def finding(code: str, billing_period: str, period: str | None, computed: float | None,
                billed: float | None, severity: str = "DISCREPANCY") -> None:
        findings.append({
            "code": code,
            "billing_period": billing_period,
            "period": TOU_NAME[period] if period else None,
            "computed": rnd(computed),
            "billed": rnd(billed),
            "difference_billed_minus_computed": rnd(None if computed is None or billed is None else billed - computed),
            "severity": severity,
        })

    opening = packet["opening_balance"]
    if opening is not None:
        _exact(opening, ("on_peak_kwh", "off_peak_kwh", "source_class", "evidence_ref"), "OPENING_BALANCE_FIELDS_INVALID")
        _require(opening["source_class"] in contract["source_classes"], "OPENING_BALANCE_SOURCE_CLASS_INVALID")
        refs.add(_ref(opening["evidence_ref"], "OPENING_BALANCE_EVIDENCE_REF_INVALID"))
        carry = {p: _num(opening[f"{p}_kwh"], 0.0, 24.0 * limits["max_monthly_kwh_per_period"],
                         "OPENING_BALANCE_INVALID") for p in PERIODS}
        carry_basis = "OPENING_BALANCE_" + opening["source_class"]
        if opening["source_class"] == "OWNER_ESTIMATE":
            reasons.add("OPENING_BALANCE_OWNER_ESTIMATE")
    elif all(bills[0][p]["prior_balance_kwh"] is not None for p in PERIODS):
        carry = {p: float(bills[0][p]["prior_balance_kwh"]) for p in PERIODS}
        carry_basis = "FIRST_BILL_PRIOR_BALANCE"
    else:
        carry = {p: 0.0 for p in PERIODS}
        carry_basis = "ASSUMED_ZERO"
        reasons.add("OPENING_BALANCE_UNKNOWN_ASSUMED_ZERO")
    opening_basis = carry_basis
    independent = dict(carry)
    true_up_observed_months: set[int] = set()
    rows: list[dict[str, Any]] = []
    prev_bill: dict[str, Any] | None = None

    for bill in bills:
        label = bill["billing_period"]
        refs.add(bill["evidence_ref"])
        if bill["source_class"] == "OWNER_ESTIMATE":
            reasons.add("BILL_SOURCE_OWNER_ESTIMATE")
        if prev_bill is not None:
            if bill["index"] != prev_bill["index"] + 1:
                reasons.add("LEDGER_GAP_MISSING_BILLING_PERIOD")
                finding("LEDGER_GAP_MISSING_BILLING_PERIOD", label, None, None, None, "GAP")
            if bill["period_start"] < prev_bill["period_end"]:
                reasons.add("BILL_PERIODS_OVERLAP")
                finding("BILL_PERIODS_OVERLAP", label, None, None, None, "GAP")
        policy_true_up = true_up_month is not None and (bill["index"] % 12) + 1 == true_up_month
        billed_true_up = bill["true_up_billed"]
        if billed_true_up is not None:
            true_up_observed_months.add((bill["index"] % 12) + 1)
            if true_up_month is None:
                reasons.add("TRUE_UP_OBSERVED_IN_BILL_POLICY_UNDECLARED")
            elif not policy_true_up:
                reasons.add("TRUE_UP_BILLED_OUTSIDE_DECLARED_MONTH")
                finding("TRUE_UP_BILLED_OUTSIDE_DECLARED_MONTH", label, None, None, None)
        elif policy_true_up and bill["source_class"] != "OWNER_ESTIMATE":
            reasons.add("TRUE_UP_EXPECTED_NOT_BILLED")
            finding("TRUE_UP_EXPECTED_NOT_BILLED", label, None, None, None)

        row: dict[str, Any] = {"billing_period": label, "source_class": bill["source_class"], "periods": {}}
        new_carry: dict[str, float] = {}
        settled_total = 0.0
        for p in PERIODS:
            led = bill[p]
            earned, use = float(led["credits_earned_kwh"]), float(led["energy_use_kwh"])
            computed_prior = carry[p]
            billed_prior = led["prior_balance_kwh"]
            if billed_prior is not None and abs(billed_prior - computed_prior) > tolerance:
                code = "BILLED_CHAIN_DISCONTINUITY" if carry_basis == "UTILITY_NET" else "PRIOR_BALANCE_MISMATCH"
                if carry_basis != "ASSUMED_ZERO":
                    reasons.add(code)
                    finding(code, label, p, computed_prior, billed_prior)
            base = billed_prior if billed_prior is not None else computed_prior
            net = base + earned - use
            month_carry = max(0.0, net)
            billed_kwh = max(0.0, -net)
            if billed_true_up is not None:
                settled = billed_true_up[f"{p}_settled_kwh"]
                if (policy_true_up and abs(settled - month_carry) > tolerance) or settled - month_carry > tolerance:
                    reasons.add("TRUE_UP_SETTLED_KWH_MISMATCH")
                    finding("TRUE_UP_SETTLED_KWH_MISMATCH", label, p, month_carry, settled)
                settled = min(settled, month_carry)
            elif policy_true_up:
                settled = month_carry
            else:
                settled = 0.0
            settled_total += settled
            computed_carry = month_carry - settled
            for field, computed_value in (("net_balance_kwh", computed_carry), ("billed_kwh", billed_kwh)):
                billed_value = led[field]
                if billed_value is not None and abs(billed_value - computed_value) > tolerance:
                    code = "NET_BALANCE_MISMATCH" if field == "net_balance_kwh" else "BILLED_KWH_MISMATCH"
                    reasons.add(code)
                    finding(code, label, p, computed_value, billed_value)
            carried = led["net_balance_kwh"] if led["net_balance_kwh"] is not None else computed_carry
            new_carry[p] = carried
            ind_net = independent[p] + earned - use
            ind_carry = max(0.0, ind_net)
            if policy_true_up or billed_true_up is not None:
                ind_carry = 0.0 if billed_true_up is None else max(0.0, ind_carry - billed_true_up[f"{p}_settled_kwh"])
            independent[p] = ind_carry
            row["periods"][TOU_NAME[p]] = {
                "prior_used_kwh": rnd(base),
                "prior_basis": "BILLED" if billed_prior is not None else "COMPUTED",
                "credits_earned_kwh": rnd(earned),
                "energy_use_kwh": rnd(use),
                "computed_billed_kwh": rnd(billed_kwh),
                "computed_settled_kwh": rnd(settled),
                "computed_net_balance_kwh": rnd(computed_carry),
                "carried_kwh": rnd(carried),
                "carried_basis": "BILLED" if led["net_balance_kwh"] is not None else "COMPUTED",
            }
        if billed_true_up is not None and policy_true_up and billed_true_up["settlement_amount"] is not None:
            expected = _settlement_value(settled_total, settle_mode, payout_rate)
            if expected is not None and abs(expected - billed_true_up["settlement_amount"]) > AMOUNT_TOLERANCE:
                reasons.add("TRUE_UP_AMOUNT_MISMATCH")
                finding("TRUE_UP_AMOUNT_MISMATCH", label, None, expected, billed_true_up["settlement_amount"])
        row["true_up_applied"] = bool(policy_true_up or billed_true_up is not None)
        rows.append(row)
        carry = new_carry
        carry_basis = "UTILITY_NET" if all(bill[p]["net_balance_kwh"] is not None for p in PERIODS) else "COMPUTED"
        prev_bill = bill

    last = bills[-1]
    if true_up_month is None:
        reasons.add("TRUE_UP_MONTH_UNDECLARED")
    if settle_mode is None:
        reasons.add("TRUE_UP_SETTLEMENT_MODE_UNDECLARED")
    elif settle_mode == "PAYOUT_AT_RATE" and payout_rate is None:
        reasons.add("TRUE_UP_PAYOUT_RATE_UNDECLARED")
    current = {
        "as_of_billing_period": last["billing_period"],
        "as_of_period_end": last["period_end"].isoformat(),
        "basis": "UTILITY_STATEMENT_NET" if carry_basis == "UTILITY_NET" else "COMPUTED",
        "ON_PEAK_kwh": rnd(carry["on_peak"]),
        "OFF_PEAK_kwh": rnd(carry["off_peak"]),
        "independent_computed_ON_PEAK_kwh": rnd(independent["on_peak"]),
        "independent_computed_OFF_PEAK_kwh": rnd(independent["off_peak"]),
        "drift_ON_PEAK_kwh": rnd(carry["on_peak"] - independent["on_peak"]),
        "drift_OFF_PEAK_kwh": rnd(carry["off_peak"] - independent["off_peak"]),
    }
    next_true_up = _next_true_up(last["index"], true_up_month)
    true_up = {
        "declared_month": true_up_month,
        "settlement_mode": settle_mode,
        "payout_rate_per_kwh": payout_rate,
        "next_true_up_billing_period": month_label(next_true_up) if next_true_up is not None else None,
        "months_until_next_true_up": (next_true_up - last["index"]) if next_true_up is not None else None,
        "observed_in_bills_months": sorted(true_up_observed_months),
        "month_inferred_from_bills": False,
    }
    forecast = _forecast(packet["forecast"], bills, carry, last["index"], next_true_up, settle_mode, payout_rate,
                         policy, refs)
    for code in forecast["reason_codes"]:
        reasons.add(code)

    blocking = {
        "OPENING_BALANCE_UNKNOWN_ASSUMED_ZERO", "OPENING_BALANCE_OWNER_ESTIMATE", "BILL_SOURCE_OWNER_ESTIMATE",
        "LEDGER_GAP_MISSING_BILLING_PERIOD", "BILL_PERIODS_OVERLAP", "PRIOR_BALANCE_MISMATCH",
        "BILLED_CHAIN_DISCONTINUITY", "NET_BALANCE_MISMATCH", "BILLED_KWH_MISMATCH",
        "TRUE_UP_SETTLED_KWH_MISMATCH", "TRUE_UP_AMOUNT_MISMATCH", "TRUE_UP_BILLED_OUTSIDE_DECLARED_MONTH",
        "TRUE_UP_EXPECTED_NOT_BILLED",
    }
    ledger_grade = not (reasons & blocking)
    pto = policy["parameters"]["solar_interconnection_date"]
    return {
        "schema_version": RESULT_SCHEMA,
        "authority": AUTHORITY,
        "ledger_id": packet["ledger_id"],
        "ledger_decision_grade": ledger_grade,
        "reason_codes": sorted(reasons),
        "reconciliation": {
            "tolerance_kwh": rnd(tolerance),
            "bills_checked": len(bills),
            "discrepancy_count": sum(1 for f in findings if f["severity"] == "DISCREPANCY"),
            "gap_count": sum(1 for f in findings if f["severity"] == "GAP"),
            "findings": findings,
            "utility_statement_is_authority": True,
        },
        "opening_balance_basis": opening_basis,
        "ledger": rows,
        "current_bank": current,
        "true_up": true_up,
        "forecast": forecast,
        "parameters": parameter_status,
        "solar_interconnection_date": {
            "value": pto["value"],
            "evidence_status": pto["evidence_status"],
            "inferred": False,
            "epr6_active_by_billing_period_upper_bound": policy["parameters"]["epr6_active_by_billing_period"]["value"],
        },
        "credit_unit": "KWH",
        "banking_scope": "TOU_PERIOD_SPECIFIC",
        "account_identifiers_stored": False,
        "full_bill_claimed": False,
        "billing_authority": False,
        "physical_actions": False,
        "execution_authorized": False,
        "evidence_refs": sorted(refs),
    }


# ---------------------------------------------------------------------------------------------
# True-up forecast
# ---------------------------------------------------------------------------------------------

def _forecast(raw: Any, bills: list[dict[str, Any]], carry: dict[str, float], as_of: int, next_true_up: int | None,
              mode: str | None, rate: float | None, policy: dict[str, Any], refs: set[str]) -> dict[str, Any]:
    empty = {"requested": False, "complete": False, "method": None, "reason_codes": [], "months": [],
             "projected_true_up": None, "marginal_free_credit_kwh": None}
    if raw is None:
        return empty
    _exact(raw, ("method", "monthly_profile", "evidence_ref"), "FORECAST_FIELDS_INVALID")
    _require(raw["method"] in FORECAST_METHODS, "FORECAST_METHOD_INVALID")
    refs.add(_ref(raw["evidence_ref"], "FORECAST_EVIDENCE_REF_INVALID"))
    cap = float(policy["limits"]["max_monthly_kwh_per_period"])
    profile: dict[int, dict[str, tuple[float, float]]] = {}
    if raw["method"] == "OWNER_MONTHLY_PROFILE":
        prof = _exact(raw["monthly_profile"], tuple(f"{m:02d}" for m in range(1, 13)), "FORECAST_PROFILE_INVALID")
        for key, month in prof.items():
            month = _exact(month, PERIODS, "FORECAST_PROFILE_INVALID")
            profile[int(key)] = {}
            for p in PERIODS:
                pair = _exact(month[p], ("credits_earned_kwh", "energy_use_kwh"), "FORECAST_PROFILE_INVALID")
                profile[int(key)][p] = (_num(pair["credits_earned_kwh"], 0.0, cap, "FORECAST_PROFILE_INVALID"),
                                        _num(pair["energy_use_kwh"], 0.0, cap, "FORECAST_PROFILE_INVALID"))
    else:
        _require(raw["monthly_profile"] is None, "FORECAST_PROFILE_MUST_BE_NULL")
        for bill in bills:  # later bills overwrite earlier ones: the most recent year wins
            profile[(bill["index"] % 12) + 1] = {
                p: (float(bill[p]["credits_earned_kwh"]), float(bill[p]["energy_use_kwh"])) for p in PERIODS}

    reasons: list[str] = []
    horizon_end = next_true_up if next_true_up is not None else as_of + 12
    if next_true_up is None:
        reasons.append("FORECAST_HORIZON_12_MONTHS_TRUE_UP_MONTH_UNDECLARED")
    _require(horizon_end - as_of <= int(policy["limits"]["max_forecast_months"]), "FORECAST_HORIZON_INVALID")
    bank = dict(carry)
    path_min = dict(carry)
    months: list[dict[str, Any]] = []
    complete = True
    projected_true_up = None
    for index in range(as_of + 1, horizon_end + 1):
        moy = (index % 12) + 1
        if moy not in profile:
            complete = False
            reasons.append("FORECAST_MONTH_NOT_IN_HISTORY")
            break
        entry: dict[str, Any] = {"billing_period": month_label(index)}
        settled: dict[str, float] = {}
        for p in PERIODS:
            earned, use = profile[moy][p]
            net = bank[p] + earned - use
            pre = max(0.0, net)
            path_min[p] = min(path_min[p], pre)
            settle = pre if index == next_true_up else 0.0
            settled[p] = settle
            bank[p] = pre - settle
            entry[TOU_NAME[p]] = {"projected_billed_kwh": rnd(max(0.0, -net)), "projected_balance_kwh": rnd(pre),
                                  "projected_settled_kwh": rnd(settle)}
        months.append(entry)
        if index == next_true_up:
            total = settled["on_peak"] + settled["off_peak"]
            value = _settlement_value(total, mode, rate)
            projected_true_up = {
                "billing_period": month_label(index),
                "ON_PEAK_settled_kwh": rnd(settled["on_peak"]),
                "OFF_PEAK_settled_kwh": rnd(settled["off_peak"]),
                "settlement_value": rnd(value, 4),
                "settlement_value_known": value is not None,
            }
    if next_true_up is not None and projected_true_up is None:
        complete = False
    marginal = ({TOU_NAME[p]: rnd(path_min[p]) for p in PERIODS}
                if complete and next_true_up is not None else None)
    if marginal is None:
        reasons.append("MARGINAL_FREE_CREDIT_UNRESOLVED")
    return {
        "requested": True,
        "complete": complete,
        "method": raw["method"],
        "reason_codes": sorted(set(reasons)),
        "months": months,
        "projected_true_up": projected_true_up,
        "marginal_free_credit_kwh": marginal,
        "forecast_is_estimate": True,
    }


# ---------------------------------------------------------------------------------------------
# Adapters: #147 TOU load-shift evaluator and #111 pump solar-window tool (both unchanged)
# ---------------------------------------------------------------------------------------------

def tou_credit_fields(result: dict[str, Any], source_period: str, target_period: str, as_of: str) -> dict[str, Any]:
    """Fields a caller merges into a ``house_brain_tou_load_shift.input.v1`` packet.

    Preferred basis is the forecast's marginal free credit (credit not needed by base load before
    the true-up), which is exactly the #147 meaning of "marginal credit available". Without a
    complete forecast the current bank balance is an upper bound and the quality is DEGRADED.
    ``as_of`` is an explicit ``YYYY-MM-DD`` date (no clock read) used for evidence age.
    """
    _require(isinstance(result, dict) and result.get("schema_version") == RESULT_SCHEMA, "LEDGER_RESULT_INVALID")
    _require(source_period in TOU_NAME.values() and target_period in TOU_NAME.values(), "PERIOD_INVALID")
    as_of_date = parse_date(as_of, "AS_OF_INVALID")
    period_end = parse_date(result["current_bank"]["as_of_period_end"], "LEDGER_RESULT_INVALID")
    _require(as_of_date >= period_end, "AS_OF_BEFORE_LEDGER_PERIOD_END")
    marginal = result["forecast"]["marginal_free_credit_kwh"]
    reasons: list[str] = []
    if marginal is not None:
        basis = "PROJECTED_MARGINAL_FREE_CREDIT"
        credit = {p: float(marginal[p]) for p in TOU_NAME.values()}
    else:
        basis = "CURRENT_BANK_BALANCE_UPPER_BOUND"
        credit = {p: float(result["current_bank"][f"{p}_kwh"]) for p in TOU_NAME.values()}
        reasons.append("EPR6_CREDIT_BASIS_UPPER_BOUND")
    if not result["ledger_decision_grade"]:
        reasons.append("EPR6_LEDGER_NOT_DECISION_GRADE")
    return {
        "credit_semantics": "PERIOD_SPECIFIC_KWH",
        "source_marginal_credit_available_kwh": credit[source_period],
        "target_marginal_credit_available_kwh": credit[target_period],
        "evidence_quality": "GOOD" if not reasons else "DEGRADED",
        "evidence_age_seconds": (as_of_date - period_end).days * 86400,
        "epr6_basis": basis,
        "epr6_reason_codes": sorted(reasons),
        "epr6_ledger_ref": f"epr6-ledger:{result['ledger_id']}",
    }


def build_tou_packet(result: dict[str, Any], base_packet: dict[str, Any], as_of: str) -> dict[str, Any]:
    """Return a copy of a #147 packet with its credit/evidence fields filled from the ledger."""
    _require(isinstance(base_packet, dict), "TOU_PACKET_INVALID")
    fields = tou_credit_fields(result, base_packet.get("source_period"), base_packet.get("target_period"), as_of)
    packet = dict(base_packet)
    for key in ("credit_semantics", "source_marginal_credit_available_kwh", "target_marginal_credit_available_kwh",
                "evidence_age_seconds"):
        packet[key] = fields[key]
    if fields["evidence_quality"] != "GOOD":
        packet["evidence_quality"] = "DEGRADED"
    refs = list(packet.get("evidence_refs") or [])
    if fields["epr6_ledger_ref"] not in refs:
        refs.append(fields["epr6_ledger_ref"])
    packet["evidence_refs"] = refs
    return packet


def solar_window_surplus_valuation(result: dict[str, Any], import_rates: dict[str, float]) -> dict[str, Any]:
    """A ``surplus_valuation`` block for ``house_brain_pump_solar_window.input.v1``.

    A solar-covered pump kWh displaces one exported kWh. If the forecast says that period's bank
    will still hold kWh at the true-up, the displaced export would have been settled, so it is worth
    the settlement value (payout rate, or 0 if unused credits expire). Otherwise it would have
    offset a future import and is worth the import rate: the accepted cost-neutral default.
    """
    _require(isinstance(result, dict) and result.get("schema_version") == RESULT_SCHEMA, "LEDGER_RESULT_INVALID")
    _require(isinstance(import_rates, dict) and set(import_rates) == set(TOU_NAME.values()), "IMPORT_RATES_INVALID")
    rates = {p: _num(import_rates[p], 0.0, 100.0, "IMPORT_RATES_INVALID") for p in TOU_NAME.values()}
    ref = f"epr6-ledger:{result['ledger_id']}"
    projected = result["forecast"]["projected_true_up"]
    mode = result["true_up"]["settlement_mode"]
    rate = result["true_up"]["payout_rate_per_kwh"]
    reasons: list[str] = []
    value = dict(rates)
    if projected is None or not result["forecast"]["complete"]:
        reasons.append("SURPLUS_VALUATION_DEFAULT_TRUE_UP_UNRESOLVED")
    elif mode is None or (mode == "PAYOUT_AT_RATE" and rate is None):
        reasons.append("SURPLUS_VALUATION_DEFAULT_SETTLEMENT_UNDECLARED")
    else:
        for p in TOU_NAME.values():
            if projected[f"{p}_settled_kwh"] > EPS:
                value[p] = 0.0 if mode == "EXPIRE_UNPAID" else float(rate)
                reasons.append(f"PROJECTED_{p}_SURPLUS_AT_TRUE_UP")
    if not result["ledger_decision_grade"]:
        reasons.append("EPR6_LEDGER_NOT_DECISION_GRADE")
    neutral = all(abs(value[p] - rates[p]) <= EPS for p in rates)
    valuation = {
        "mode": "KWH_CREDIT_AT_PERIOD_IMPORT_RATE" if neutral else "EXPLICIT_EXPORT_VALUE",
        "export_value_per_kwh": None if neutral else {p: value[p] for p in ("OFF_PEAK", "ON_PEAK")},
        "evidence_refs": [ref],
    }
    headroom = None
    if projected is not None:
        headroom = {p: projected[f"{p}_settled_kwh"] for p in TOU_NAME.values()}
    return {
        "surplus_valuation": valuation,
        "epr6_reason_codes": sorted(reasons),
        "valid_while_added_solar_self_consumption_within_kwh": headroom,
    }


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------

def _load(path: str | None) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else json.load(sys.stdin)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    led = sub.add_parser("ledger", help="compute the ledger, reconciliation and forecast")
    led.add_argument("input", nargs="?")
    tou = sub.add_parser("tou-packet", help="fill a #147 TOU packet's credit fields from a ledger input")
    tou.add_argument("input")
    tou.add_argument("--tou-packet", required=True)
    tou.add_argument("--as-of", required=True, help="explicit YYYY-MM-DD evaluation date")
    sol = sub.add_parser("solar-valuation", help="emit a #111 surplus_valuation block from a ledger input")
    sol.add_argument("input")
    sol.add_argument("--on-peak-rate", type=float, required=True)
    sol.add_argument("--off-peak-rate", type=float, required=True)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = evaluate(_load(args.input))
        if args.command == "tou-packet":
            result = build_tou_packet(result, _load(args.tou_packet), args.as_of)
        elif args.command == "solar-valuation":
            result = solar_window_surplus_valuation(result, {"ON_PEAK": args.on_peak_rate, "OFF_PEAK": args.off_peak_rate})
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, RecursionError, Epr6Error) as exc:
        code = str(exc) if isinstance(exc, Epr6Error) else "INPUT_UNREADABLE"
        print(json.dumps({"schema_version": RESULT_SCHEMA, "ok": False, "error": code}, sort_keys=True))
        return 2
    print(json.dumps(result, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
