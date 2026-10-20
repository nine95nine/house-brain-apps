"""Bounded private input contracts; reuse accepted evaluators without changing them."""
import json
from pathlib import Path
from tools.epr6_banking_ledger import evaluate as evaluate_bill, load_policy
from tools.evaluate_sense_load_identity_evidence import evaluate as evaluate_identity
from .analytics import fit_thermal, correlate, virtual_meter


def load_packet(root, name, limit=2_000_000):
    from .installation import read_private, decode
    path=Path(root)/name
    try:
        raw=read_private(path)
    except FileNotFoundError:
        return None
    if len(raw)>limit:
        raise ValueError("PRIVATE_PACKET_REFUSED")
    return decode(raw)


def evaluate_private_inputs(root, policy=None, register=None, retained_bill=None):
    results={}
    for name,fn in (("bill_packet.json",lambda p:evaluate_bill(p,policy)),
                    ("sense_packet.json",lambda p:evaluate_identity(p,register))):
        try:
            packet=load_packet(root,name)
            if packet is None and name=='bill_packet.json':
                packet=retained_bill
            results[name]=qualitative(fn(packet)) if packet is not None else {"status":"EVIDENCE_NOT_AVAILABLE"}
            if name=='bill_packet.json' and packet is not None:
                results[name]['time_scope']='HISTORICAL_STATEMENT_LEDGER; live native estimator remains current-bank authority'
        except (ValueError,KeyError,TypeError,OverflowError,OSError,RecursionError):
            results[name]={"status":"PACKET_REFUSED"}
    return results


def qualitative(value):
    """Legacy classifier percentages are rule scores, not calibrated probabilities."""
    if isinstance(value,dict):
        result={k:qualitative(v) for k,v in value.items() if k!="confidence_pct"}
        if "confidence_pct" in value:
            result["probability"]=None
            result["confidence_basis"]="qualitative rule classification; no calibrated probability"
        return result
    if isinstance(value,list):
        return [qualitative(v) for v in value]
    return value
