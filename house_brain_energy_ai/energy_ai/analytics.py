"""Evidence-based diagnostics; no appliance diagnosis from power alone."""
from datetime import datetime
import hashlib
import math
import statistics
from .forecast import TZ, solve


def robust_anomaly(history, value, minimum=30):
    if len(history)<minimum:
        return {"status":"LEARNING","samples":len(history)}
    center=statistics.median(history)
    mad=statistics.median(abs(v-center) for v in history)
    band=max(100.,mad*1.4826*4)
    return dict(status="REVIEW" if abs(value-center)>band else "NORMAL",
                baseline=center, deviation=value-center, band=band,
                cause="not diagnosed", classification="ESTIMATED")


def baseload(hourly):
    nights=[v for t,v in hourly if 1<=datetime.fromtimestamp(t,TZ).hour<5]
    if len(nights)<12:
        return {"status":"INSUFFICIENT_NIGHTS"}
    return {"status":"ESTIMATED","overnight_median_w":statistics.median(nights),
            "samples":len(nights),"occupancy_inferred":False,
            "note":"includes necessary always-on services; not all avoidable"}


def cycles(rows, threshold_w=50, max_gap=180):
    """Minute-scale observations; incomplete/overlapping cycles stay uncertain."""
    result=[]
    start=None
    powers=[]
    previous=None
    for t,w in rows:
        if previous is not None and (t<=previous or t-previous>max_gap):
            start=None
            powers=[]
        if w>=threshold_w:
            if start is None:
                # Only count a complete rising edge after an observed off sample.
                if previous is not None and last_w<threshold_w and t-previous<=max_gap:
                    start=t
                    powers=[]
            if start is not None:
                powers.append(w)
        elif start is not None:
            result.append(dict(start=start,end=t,watts=statistics.median(powers),
                               duration_s=t-start,precision_s=max_gap))
            start=None
            powers=[]
        previous,last_w=t,w
    return result


def clusters(events):
    """Learned empirical centers; clustering alone never identifies an appliance."""
    groups=[]
    for e in events:
        found=next((g for g in groups if abs(g["watts"]-e["watts"])<=max(50,g["watts"]*.2)
                    and abs(g["duration_s"]-e["duration_s"])<=max(180,g["duration_s"]*.4)),None)
        if found is None:
            if len(groups)>=64:
                continue  # Retain raw events; report uncovered counts in runtime diagnostics.
            groups.append({"watts":e["watts"],"duration_s":e["duration_s"],"count":1})
        else:
            count=found["count"]
            for key in ("watts","duration_s"):
                found[key]=(found[key]*count+e[key])/(count+1)
            found["count"]+=1
    return [dict(g,identity="UNKNOWN",classification="ESTIMATED") for g in groups[:64]]


def correlate(events, independent_events, family, independent_family, tolerance_s=120):
    if family==independent_family:
        return {"status":"CIRCULAR_EVIDENCE_REFUSED"}
    matches=set()
    matched=0
    for e in events:
        candidates=[(abs(e["start"]-v["start"])+abs(e["end"]-v["end"]),i)
                    for i,v in enumerate(independent_events) if i not in matches
                    and abs(e["start"]-v["start"])<=tolerance_s and abs(e["end"]-v["end"])<=tolerance_s]
        if candidates:
            _,i=min(candidates)
            matches.add(i)
            matched+=1
    return dict(status="CANDIDATE_ONLY",matched=matched,false_matches=len(events)-matched,
                missed=len(independent_events)-matched,
                precision=matched/len(events) if events else None,
                recall=matched/len(independent_events) if independent_events else None,
                probability=None,physical_actions=False)


def virtual_meter(seconds, profile_w, error_fraction, calibrated, evidence_ref):
    if not calibrated or not evidence_ref or not 0<=error_fraction<=1 or seconds<0 or profile_w<0:
        return {"status":"CALIBRATION_REQUIRED"}
    energy=seconds*profile_w/3600000
    return dict(status="ESTIMATED",kwh=energy,range_kwh=[energy*(1-error_fraction),energy*(1+error_fraction)],
                evidence_ref=evidence_ref,independent_measurement=False,sense_bridge_eligible=False)


def fit_thermal(rows):
    """Rows: dt_s, indoor C, outdoor C, cooling state, observed next indoor C.

    Identifies temperature response only, never COP/capacity. No humidity relaxation.
    """
    if len(rows)<100:
        return {"status":"INSUFFICIENT_THERMAL_DATA"}
    matrix=[[1. if i==j else 0. for j in range(3)] for i in range(3)]
    vector=[0.,0.,0.]
    for dt,indoor,outdoor,cooling,next_indoor in rows:
        if not 60<=dt<=1800 or cooling not in (0,1):
            raise ValueError("THERMAL_INTERVAL")
        x=[1.,outdoor-indoor,float(cooling)]
        y=(next_indoor-indoor)*3600/dt
        for i in range(3):
            vector[i]+=x[i]*y
            for j in range(3):
                matrix[i][j]+=x[i]*x[j]
    return {"status":"EXPERIMENTAL_FIT","coefficients":solve(matrix,vector),
            "unit":"C/hour", "promoted":False,"physical_authority":False}


def compare_outcome(actual_kwh, baseline_kwh, baseline_error_kwh, context_matched):
    if not context_matched:
        return {"status":"ATTRIBUTION_UNRESOLVED","savings_kwh":None}
    delta=baseline_kwh-actual_kwh
    return {"status":"PROVISIONAL","savings_kwh":delta,
            "range_kwh":[delta-baseline_error_kwh,delta+baseline_error_kwh],
            "additive_with_other_claims":False}


def bridge_eligibility(binding, fresh, dedicated, capacity_w):
    return bool(binding.classification=="MEASURED" and binding.family!="sense" and fresh
                and dedicated and capacity_w>0)


class IdentityRegistry:
    def __init__(self, store, accepted):
        self.store=store
        self.accepted=accepted

    def review(self, stable_id, identity, decision, evidence_ref, at):
        if decision not in {"CONFIRM","REJECT","NOT_SURE"} or not evidence_ref:
            raise ValueError("REVIEW_EVIDENCE_REQUIRED")
        if not stable_id or len(stable_id)>128 or not identity or len(identity)>128:
            raise ValueError("IDENTITY_INVALID")
        # Stable ids must have an independently reviewed registry binding.
        bindings=self.store.get("identity_bindings",{})
        if stable_id not in bindings or bindings[stable_id].get("verified") is not True:
            raise ValueError("STABLE_BINDING_REQUIRED")
        if any(v in identity.upper() for v in ("TESLA","BOLT","CHEVY")):
            raise ValueError("SHARED_EV_NOT_VEHICLE_IDENTITY")
        record=dict(identity=identity,decision=decision,evidence_ref=evidence_ref,at=at,
                    classification="USER-CONFIRMED",physical_authority=False)
        key="identity:"+stable_id
        old=self.store.get(key,{"history":[]})
        if len(old["history"])>=100:
            raise ValueError("REVIEW_HISTORY_EXPORT_REQUIRED")
        record["history"]=old["history"]+[ {k:v for k,v in record.items() if k!="history"} ]
        self.store.put(key,record)
        return record

    def queue(self):
        rows=self.store.db.execute("SELECT key,value FROM kv WHERE key LIKE 'identity:%'")
        import json
        bindings=self.store.get("identity_bindings",{})
        return [{"stable_id":k[9:],"identity":json.loads(v)["identity"],
                 "remaining_step":"review native Sense app; supported rename/merge API unverified"}
                for k,v in rows if json.loads(v)["decision"]=="CONFIRM"
                and bindings.get(k[9:],{}).get("verified") is True]
