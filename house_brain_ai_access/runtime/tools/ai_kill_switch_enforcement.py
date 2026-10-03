#!/usr/bin/env python3
"""Single pre-action enforcement point for every House Brain AI surface (Initiative #56, R1).

``enforce`` composes the local kill-switch view with the accepted Global AI Safety Interlock R1
(``tools/evaluate_ai_safety_interlock.evaluate``), which is called unchanged. The kill switch can
only ever *remove* permission: where it permits, the result is exactly the interlock's result.

Equivalence theorem (proved exhaustively by tests/test_ai_kill_switch_enforcement.py)::

    enforce(view, action_class, P).decision
        == interlock.evaluate(P with mode := project_interlock_mode(view, action_class, P.mode)).decision

where ``project_interlock_mode`` maps a blocking kill-switch state to the interlock mode that has the
same effect (FULL_STOP -> EMERGENCY_STOP, READ_ONLY vs MUTATION -> OWNER_DISABLED) and otherwise
leaves the caller's mode untouched.

``enforce`` never raises for data problems and never executes anything; its ``decision`` value is a
drop-in for the ``safety_interlock`` / ``safety_interlock_action`` fields that the accepted HA AI Task
preflight, Maintenance Window, Broker admission and One-Shot Executor envelope already consume.
No clock reads: ``guard`` requires an explicit ``now``.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any, Final

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import ai_kill_switch_state as ks  # noqa: E402
from tools.evaluate_ai_safety_interlock import InterlockError, evaluate as interlock_evaluate  # noqa: E402

RESULT_SCHEMA: Final = "house_brain_ai_kill_switch.enforcement.v1"
SURFACE_RE: Final = re.compile(r"\A[a-z0-9][a-z0-9_.:-]{0,63}\Z")
DECISIONS: Final = ("ALLOW", "ALLOW_PROBE", "BLOCK")
AUTHORITY: Final = "GLOBAL_AI_POLICY_ONLY_NO_EXECUTION"


def permits(effective_state: str, action_class: str) -> bool:
    return ks.RANK[effective_state] >= ks.RANK[ks.ACTION_CLASSES[action_class]]


def project_interlock_mode(effective_state: str, action_class: str, caller_mode: str) -> str:
    if permits(effective_state, action_class):
        return caller_mode
    return "EMERGENCY_STOP" if effective_state == "FULL_STOP" else "OWNER_DISABLED"


def _view_state(view: Any) -> tuple[str, list[str], Any]:
    if type(view) is not dict or view.get("schema_version") != ks.VIEW_SCHEMA:
        return "FULL_STOP", ["KILL_SWITCH_VIEW_INVALID"], None
    state = view.get("effective_state")
    if type(state) is not str or state not in ks.STATES or type(view.get("fail_closed")) is not bool:
        return "FULL_STOP", ["KILL_SWITCH_VIEW_INVALID"], None
    if view["fail_closed"] and state != "FULL_STOP":
        return "FULL_STOP", ["KILL_SWITCH_VIEW_INCONSISTENT"], None
    gen = view.get("generation")
    return state, [], gen if type(gen) is int else None


def enforce(*, view: Any, surface_id: Any, action_class: Any, interlock_packet: Any) -> dict[str, Any]:
    state, reasons, generation = _view_state(view)
    surface = surface_id if type(surface_id) is str and SURFACE_RE.fullmatch(surface_id) else None
    if surface is None:
        reasons.append("SURFACE_ID_INVALID")
    ac = action_class if type(action_class) is str and action_class in ks.ACTION_CLASSES else None
    if ac is None:
        reasons.append("ACTION_CLASS_INVALID")
    interlock_result: dict[str, Any] | None
    try:
        if type(interlock_packet) is not dict:
            raise InterlockError("ROOT_NOT_OBJECT")
        interlock_result = interlock_evaluate(dict(interlock_packet))
    except InterlockError as exc:
        interlock_result = None
        reasons.append(f"INTERLOCK_PACKET_INVALID_{exc}")
    if surface is None or ac is None or interlock_result is None or reasons:
        decision = "BLOCK"
    elif not permits(state, ac):
        decision = "BLOCK"
        reasons.append(f"KILL_SWITCH_{state}_BLOCKS_{ac}")
    else:
        decision = interlock_result["decision"]
    if interlock_result is not None:
        reasons.extend(f"INTERLOCK_{code}" for code in interlock_result["reason_codes"])
    return {
        "schema_version": RESULT_SCHEMA,
        "decision": decision,
        "surface_id": surface,
        "action_class": ac,
        "kill_switch_state": state,
        "kill_switch_generation": generation,
        "interlock_decision": None if interlock_result is None else interlock_result["decision"],
        "reason_codes": reasons,
        "authority": AUTHORITY,
        "physical_actions": False,
    }


def guard(*, store: Any, now: str, surface_id: str, action_class: str, interlock_packet: Any) -> dict[str, Any]:
    """Read the local store (fail-closed) and enforce. The store is only ever read here."""
    try:
        view = store.read_view(now)
    except Exception:  # noqa: BLE001 - any store failure must fail closed, never open
        view = ks._fail_view("STORE_READ_EXCEPTION")
    return enforce(view=view, surface_id=surface_id, action_class=action_class, interlock_packet=interlock_packet)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("packet", type=Path, help='{"view":..., "surface_id":..., "action_class":..., "interlock_packet":...}')
    a = p.parse_args(argv)
    try:
        d = json.loads(a.packet.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = None
    if type(d) is not dict or set(d) != {"view", "surface_id", "action_class", "interlock_packet"}:
        print(json.dumps({"schema_version": RESULT_SCHEMA, "ok": False, "error": "PACKET_INVALID"}, sort_keys=True))
        return 2
    out = enforce(**d)
    print(json.dumps(out, sort_keys=True))
    return 0 if out["decision"] != "BLOCK" else 1


if __name__ == "__main__":
    sys.exit(main())
