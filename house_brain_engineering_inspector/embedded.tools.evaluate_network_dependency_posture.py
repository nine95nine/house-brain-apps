#!/usr/bin/env python3
"""Deterministic advisory network dependency posture evaluator."""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path
from typing import Any, Final
INPUT_SCHEMA: Final="house_brain_network_posture.input.v1"
OUTPUT_SCHEMA: Final="house_brain_network_posture.result.v1"
LAYERS: Final={"WAN","LAN","DNS","SUPERVISOR","MQTT","RELAY","MIRROR","DEVICE"}
STATUSES: Final={"HEALTHY","DEGRADED","DOWN","UNKNOWN"}
ID_RE=re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")
MAX_NODES=32
class NetworkError(ValueError): pass

def _validate(data:dict[str,Any])->None:
 if data.get("schema_version")!=INPUT_SCHEMA: raise NetworkError("SCHEMA_VERSION_MISMATCH")
 nodes=data.get("nodes")
 if not isinstance(nodes,list) or not 1<=len(nodes)<=MAX_NODES: raise NetworkError("NODES_INVALID")
 ids=set()
 for n in nodes:
  if not isinstance(n,dict): raise NetworkError("NODE_INVALID")
  nid=n.get("id")
  if not isinstance(nid,str) or not ID_RE.fullmatch(nid): raise NetworkError("NODE_ID_INVALID")
  if nid in ids: raise NetworkError("DUPLICATE_NODE_ID")
  ids.add(nid)
  if n.get("layer") not in LAYERS: raise NetworkError("LAYER_INVALID")
  if n.get("status") not in STATUSES: raise NetworkError("STATUS_INVALID")
  if not isinstance(n.get("required"),bool): raise NetworkError("REQUIRED_NOT_BOOLEAN")
  for f in ("sample_age_seconds","freshness_limit_seconds"):
   v=n.get(f)
   if isinstance(v,bool) or not isinstance(v,int) or not 0<=v<=86400: raise NetworkError(f"{f.upper()}_INVALID")
  deps=n.get("depends_on")
  if not isinstance(deps,list) or len(deps)>16 or any(not isinstance(x,str) or not ID_RE.fullmatch(x) for x in deps):
   raise NetworkError("DEPENDS_ON_INVALID")
  if len(deps)!=len(set(deps)): raise NetworkError("DUPLICATE_DEPENDENCY")
 for n in nodes:
  if n["id"] in n["depends_on"]: raise NetworkError("SELF_DEPENDENCY")
  if any(d not in ids for d in n["depends_on"]): raise NetworkError("MISSING_DEPENDENCY")
 graph={n["id"]:n["depends_on"] for n in nodes}
 visiting=set(); done=set()
 def visit(x):
  if x in visiting: raise NetworkError("DEPENDENCY_CYCLE")
  if x in done:return
  visiting.add(x)
  for d in graph[x]: visit(d)
  visiting.remove(x);done.add(x)
 for x in graph: visit(x)

def evaluate(data:dict[str,Any])->dict[str,Any]:
 _validate(data)
 nodes={n["id"]:n for n in data["nodes"]}
 effective={}
 warnings=[]
 for nid,n in nodes.items():
  stale=n["sample_age_seconds"]>n["freshness_limit_seconds"]
  effective[nid]="UNKNOWN" if stale else n["status"]
  if stale: warnings.append(f"STALE:{nid}")
 required=[nid for nid,n in nodes.items() if n["required"]]
 if any(effective[x]=="DOWN" for x in required): posture="OUTAGE"
 elif any(effective[x]=="UNKNOWN" for x in required): posture="UNKNOWN"
 elif any(effective[x]=="DEGRADED" for x in required): posture="DEGRADED"
 else: posture="HEALTHY"
 optional_issues=sorted(nid for nid,n in nodes.items() if not n["required"] and effective[nid]!="HEALTHY")
 unhealthy={nid for nid,s in effective.items() if s!="HEALTHY"}
 roots=[]
 for nid in unhealthy:
  unhealthy_deps=[d for d in nodes[nid]["depends_on"] if d in unhealthy]
  if not unhealthy_deps: roots.append(nid)
 roots.sort(key=lambda x:(nodes[x]["layer"],x))
 impacted={}
 for root in roots:
  seen=set(); changed=True
  while changed:
   changed=False
   for nid,n in nodes.items():
    if nid==root or nid in seen: continue
    if root in n["depends_on"] or any(d in seen for d in n["depends_on"]):
     seen.add(nid);changed=True
  impacted[root]=sorted(seen)
 return {
  "schema_version":OUTPUT_SCHEMA,"posture":posture,
  "effective_status":dict(sorted(effective.items())),
  "likely_root_causes":roots,"impacted_by_root":impacted,
  "optional_issues":optional_issues,"warnings":sorted(warnings),
  "authority":"ADVISORY_ONLY_NO_NETWORK_OR_CONTROL","physical_actions":False,
  "next_action":"INVESTIGATE_ROOT_CAUSES" if posture!="HEALTHY" else "NO_OWNER_ACTION",
 }
def main(argv=None):
 p=argparse.ArgumentParser(description=__doc__);p.add_argument("packet",type=Path);a=p.parse_args(argv)
 try:
  d=json.loads(a.packet.read_text(encoding="utf-8"))
  if not isinstance(d,dict): raise NetworkError("ROOT_NOT_OBJECT")
  print(json.dumps(evaluate(d),sort_keys=True));return 0
 except (OSError,json.JSONDecodeError,NetworkError) as e:
  print(json.dumps({"schema_version":OUTPUT_SCHEMA,"ok":False,"error":str(e)},sort_keys=True));return 2
if __name__=="__main__":sys.exit(main())
