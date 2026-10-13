#!/usr/bin/env python3
"""Deterministic metadata-only House Brain privacy data policy R1."""
from __future__ import annotations
import argparse,json,re,sys
from pathlib import Path
from typing import Any,Final
INPUT_SCHEMA:Final="house_brain_privacy_policy.input.v1"
OUTPUT_SCHEMA:Final="house_brain_privacy_policy.result.v1"
CLASSES:Final={"PUBLIC","HOUSEHOLD","PRIVATE","SENSITIVE","SECRET"}
DESTINATIONS:Final={"LOCAL_PROCESSOR","HOUSE_BRAIN_LOCAL","AI_PROVIDER","PUBLIC_EXPORT"}
PURPOSES:Final={"OPERATIONS","DIAGNOSTICS","ANALYSIS","SUPPORT","PUBLICATION"}
ID_RE=re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")
MAX_FIELDS=512
class PrivacyError(ValueError): pass

def validate(data:dict[str,Any])->None:
    if data.get("schema_version")!=INPUT_SCHEMA: raise PrivacyError("SCHEMA_VERSION_MISMATCH")
    fields=data.get("fields")
    if not isinstance(fields,list) or not 1<=len(fields)<=MAX_FIELDS: raise PrivacyError("FIELDS_INVALID")
    seen=set()
    for f in fields:
        if not isinstance(f,dict): raise PrivacyError("FIELD_INVALID")
        fid=f.get("field_id")
        if not isinstance(fid,str) or not ID_RE.fullmatch(fid): raise PrivacyError("FIELD_ID_INVALID")
        if fid in seen: raise PrivacyError("DUPLICATE_FIELD_ID")
        seen.add(fid)
        if f.get("data_class") not in CLASSES: raise PrivacyError("DATA_CLASS_INVALID")
        if f.get("destination") not in DESTINATIONS: raise PrivacyError("DESTINATION_INVALID")
        if f.get("purpose") not in PURPOSES: raise PrivacyError("PURPOSE_INVALID")
        if set(f)!={"field_id","data_class","destination","purpose"}: raise PrivacyError("UNSUPPORTED_FIELD_KEYS")

def decision(c:str,d:str,p:str)->tuple[str,str]:
    if d=="LOCAL_PROCESSOR": return "ALLOW","LOCAL_PROCESSING_ONLY"
    if d=="HOUSE_BRAIN_LOCAL":
        if c=="SECRET": return "DENY","SECRET_NOT_PROPAGATED"
        if c=="SENSITIVE": return "REDACT","SENSITIVE_MINIMIZE"
        return "ALLOW","LOCAL_HOUSE_BRAIN_ALLOWED"
    if d=="AI_PROVIDER":
        if c in {"SECRET","SENSITIVE"}: return "DENY","PROVIDER_CLASS_DENIED"
        if c=="PRIVATE": return "REDACT","PROVIDER_PRIVATE_REDACT"
        if c=="HOUSEHOLD": return ("REDACT","PUBLICATION_MINIMIZE") if p=="PUBLICATION" else ("ALLOW","BOUNDED_HOUSEHOLD_CONTEXT")
        return "ALLOW","PUBLIC_PROVIDER_ALLOWED"
    if d=="PUBLIC_EXPORT": return ("ALLOW","PUBLIC_EXPORT_ALLOWED") if c=="PUBLIC" else ("DENY","NONPUBLIC_EXPORT_DENIED")
    raise PrivacyError("DESTINATION_INVALID")

def evaluate(data:dict[str,Any])->dict[str,Any]:
    validate(data); rows=[]
    for f in data["fields"]:
        d,r=decision(f["data_class"],f["destination"],f["purpose"])
        rows.append({"field_id":f["field_id"],"data_class":f["data_class"],"destination":f["destination"],"purpose":f["purpose"],"decision":d,"reason_code":r})
    counts={x:sum(r["decision"]==x for r in rows) for x in ("ALLOW","REDACT","DENY")}
    return {"schema_version":OUTPUT_SCHEMA,"decisions":rows,"decision_counts":counts,"content_handling":"METADATA_ONLY_VALUES_NOT_ACCEPTED","authority":"PRIVACY_POLICY_ADVISORY_NO_DATA_ACCESS","physical_actions":False}

def main(argv=None)->int:
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("packet",type=Path);a=p.parse_args(argv)
    try:
        d=json.loads(a.packet.read_text(encoding="utf-8"))
        if not isinstance(d,dict): raise PrivacyError("ROOT_NOT_OBJECT")
        print(json.dumps(evaluate(d),sort_keys=True));return 0
    except (OSError,json.JSONDecodeError,PrivacyError) as exc:
        print(json.dumps({"schema_version":OUTPUT_SCHEMA,"ok":False,"error":str(exc)},sort_keys=True));return 2
if __name__=="__main__":sys.exit(main())
