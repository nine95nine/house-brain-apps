#!/usr/bin/env python3
"""Deterministic load/device identity confidence classifier R1."""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path
from typing import Any, Final

INPUT_SCHEMA: Final = "house_brain_load_identity.input.v1"
OUTPUT_SCHEMA: Final = "house_brain_load_identity.result.v1"
TOKEN = re.compile(r"^[A-Z0-9_.:-]{1,128}$")
ID = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")
MATCHES = {"MATCH","MISMATCH","UNKNOWN"}
CORROBORATION = {"CONFIRMED","ABSENT","UNKNOWN"}

class IdentityError(ValueError): pass
def req(ok, code):
    if not ok: raise IdentityError(code)
def evaluate(d: dict[str, Any]) -> dict[str, Any]:
    allowed={"schema_version","candidate_id","signature_match","repeat_observations","independent_corroboration","contradiction_count","observation_age_seconds","stale_after_seconds","reason_codes"}
    req(set(d)==allowed,"UNSUPPORTED_FIELDS")
    req(d.get("schema_version")==INPUT_SCHEMA,"SCHEMA_VERSION_MISMATCH")
    req(isinstance(d["candidate_id"],str) and ID.fullmatch(d["candidate_id"]) is not None,"CANDIDATE_ID_INVALID")
    req(d["signature_match"] in MATCHES,"SIGNATURE_MATCH_INVALID")
    req(d["independent_corroboration"] in CORROBORATION,"CORROBORATION_INVALID")
    for k,lo,hi in (("repeat_observations",0,100000),("contradiction_count",0,100000),("observation_age_seconds",0,100000000),("stale_after_seconds",1,100000000)):
        v=d[k]; req(isinstance(v,int) and not isinstance(v,bool) and lo<=v<=hi,f"{k.upper()}_INVALID")
    rs=d["reason_codes"]; req(isinstance(rs,list) and len(rs)<=16 and len(rs)==len(set(rs)),"REASON_CODES_INVALID")
    for r in rs: req(isinstance(r,str) and TOKEN.fullmatch(r) is not None,"REASON_CODES_INVALID")
    stale=d["observation_age_seconds"]>d["stale_after_seconds"]
    reasons=list(rs)
    if d["contradiction_count"]>0:
        classification="REJECTED"; reasons.append("CONTRADICTION_PRESENT")
    elif d["signature_match"]=="MISMATCH":
        classification="REJECTED"; reasons.append("SIGNATURE_MISMATCH")
    elif stale:
        classification="UNVERIFIED"; reasons.append("EVIDENCE_STALE")
    elif d["signature_match"]!="MATCH":
        classification="UNVERIFIED"; reasons.append("SIGNATURE_NOT_CONFIRMED")
    elif d["repeat_observations"]<3:
        classification="UNVERIFIED"; reasons.append("INSUFFICIENT_REPEAT_OBSERVATIONS")
    elif d["independent_corroboration"]!="CONFIRMED":
        classification="UNVERIFIED"; reasons.append("INDEPENDENT_CORROBORATION_MISSING")
    else:
        classification="CONFIRMED"; reasons.append("MULTI_EVIDENCE_IDENTITY_CONFIRMED")
    confidence={"CONFIRMED":95,"UNVERIFIED":50,"REJECTED":0}[classification]
    return {"schema_version":OUTPUT_SCHEMA,"candidate_id":d["candidate_id"],"classification":classification,
            "confidence_pct":confidence,"fresh":not stale,"reason_codes":sorted(set(reasons)),
            "usable_for_analytics":classification=="CONFIRMED",
            "usable_for_control":False,
            "authority":"IDENTITY_EVIDENCE_ONLY_NO_CONTROL","physical_actions":False}
def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("packet",type=Path);a=p.parse_args(argv)
    try:
        d=json.loads(a.packet.read_text(encoding="utf-8")); req(isinstance(d,dict),"ROOT_NOT_OBJECT")
        print(json.dumps(evaluate(d),sort_keys=True));return 0
    except (OSError,json.JSONDecodeError,IdentityError) as exc:
        print(json.dumps({"schema_version":OUTPUT_SCHEMA,"ok":False,"error":str(exc)},sort_keys=True));return 2
if __name__=="__main__": sys.exit(main())
