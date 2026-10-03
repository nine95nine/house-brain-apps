#!/usr/bin/env python3
"""Deterministic global House Brain AI safety interlock R1."""
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
from typing import Any,Final
IN:Final="house_brain_ai_safety_interlock.input.v1"
OUT:Final="house_brain_ai_safety_interlock.result.v1"
MODES:Final={"ENABLED","OWNER_DISABLED","EMERGENCY_STOP","MAINTENANCE_HOLD"}
HEALTH:Final={"HEALTHY","DEGRADED","UNKNOWN"}
class InterlockError(ValueError):pass

def evaluate(d:dict[str,Any])->dict[str,Any]:
    allowed={"schema_version","mode","dependency_health","request_guard_action","owner_ack_required"}
    if set(d)!=allowed: raise InterlockError("UNSUPPORTED_FIELDS")
    if d.get("schema_version")!=IN: raise InterlockError("SCHEMA_VERSION_MISMATCH")
    mode=d.get("mode"); health=d.get("dependency_health"); guard=d.get("request_guard_action")
    if mode not in MODES: raise InterlockError("MODE_INVALID")
    if health not in HEALTH: raise InterlockError("DEPENDENCY_HEALTH_INVALID")
    if guard not in {"ALLOW","ALLOW_PROBE","THROTTLE","BLOCK"}: raise InterlockError("REQUEST_GUARD_ACTION_INVALID")
    if not isinstance(d.get("owner_ack_required"),bool): raise InterlockError("OWNER_ACK_REQUIRED_NOT_BOOLEAN")
    reasons=[]
    if mode!="ENABLED":
        decision="BLOCK"; reasons.append(f"MODE_{mode}")
    elif d["owner_ack_required"]:
        decision="BLOCK"; reasons.append("OWNER_ACK_REQUIRED")
    elif health!="HEALTHY":
        decision="BLOCK"; reasons.append(f"DEPENDENCY_{health}")
    elif guard in {"BLOCK","THROTTLE"}:
        decision="BLOCK"; reasons.append(f"REQUEST_GUARD_{guard}")
    elif guard=="ALLOW_PROBE":
        decision="ALLOW_PROBE"; reasons.append("BOUNDED_PROBE_ONLY")
    else:
        decision="ALLOW"; reasons.append("ALL_INTERLOCKS_CLEAR")
    return {"schema_version":OUT,"decision":decision,"reason_codes":reasons,
            "authority":"GLOBAL_AI_POLICY_ONLY_NO_EXECUTION","physical_actions":False}

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("packet",type=Path);a=p.parse_args(argv)
    try:
        d=json.loads(a.packet.read_text(encoding="utf-8"))
        if not isinstance(d,dict): raise InterlockError("ROOT_NOT_OBJECT")
        print(json.dumps(evaluate(d),sort_keys=True));return 0
    except (OSError,json.JSONDecodeError,InterlockError) as exc:
        print(json.dumps({"schema_version":OUT,"ok":False,"error":str(exc)},sort_keys=True));return 2
if __name__=="__main__":sys.exit(main())
