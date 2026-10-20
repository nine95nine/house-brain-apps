"""Execution contract and durable at-most-once handoff to an existing controller.

Production v0.1 has no controller sender installed. No HA service API exists here.
Modes are prepared without silently enabling global House Brain auto actions.
"""
from dataclasses import dataclass, asdict
import hashlib
import math
from .store import encode


@dataclass(frozen=True)
class Proposal:
    id: str
    domain: str
    target: str
    operation: str
    reason: str
    model_version: str
    policy_version: str
    evidence_at: float
    valid_from: float
    expires: float
    expected_benefit: float
    constraints: dict
    required_authority: str


def refuse(proposal, context, now):
    if context.get("mode") not in {"SUPERVISED","BOUNDED AUTO"}:
        return "SHADOW_NO_AUTHORITY"
    if context.get("paused",True):
        return "PAUSED"
    envelope=context.get("envelopes",{}).get(proposal.domain,{})
    checks=("commissioned","approved","current_feedback","native_schedule_reconciled",
            "dwell_satisfied","safety_clear","no_conflict")
    if not all(envelope.get(k) is True for k in checks):
        return "DOMAIN_NOT_READY"
    if envelope.get("authority") != proposal.required_authority:
        return "AUTHORITY_MISMATCH"
    if (proposal.target,proposal.operation) not in envelope.get("allowed_operations",[]):
        return "OUTSIDE_ENVELOPE"
    if any(not math.isfinite(x) for x in (proposal.valid_from,proposal.expires,proposal.evidence_at)):
        return "INVALID_TIME"
    if not proposal.valid_from <= now < proposal.expires or proposal.expires-proposal.valid_from > 1800:
        return "EXPIRED_OR_FUTURE"
    if not 0 <= now-proposal.evidence_at <= 120:
        return "STALE_EVIDENCE"
    if envelope.get("expires",0) <= now or envelope.get("override_until",0)>now:
        return "EXPIRED_ENVELOPE_OR_OVERRIDE"
    if proposal.constraints != envelope.get("constraints"):
        return "CONSTRAINTS_CHANGED"
    if context["mode"]=="SUPERVISED" and proposal.id not in context.get("approved_proposals",[]):
        return "INDIVIDUAL_APPROVAL_REQUIRED"
    return None


def dispatch(store, proposal, context, now, sender=None):
    reason=refuse(proposal,context,now)
    if reason:
        return {"state":"REFUSED","reason":reason}
    if sender is None:
        return {"state":"REFUSED","reason":"NO_COMMISSIONED_ADAPTER"}
    digest=hashlib.sha256(encode(asdict(proposal)).encode()).hexdigest()
    # Durable intent before effect; crash/ambiguous timeout never replays the operation.
    if store.get("spent:"+proposal.id) is not None:
        return {"state":"REFUSED","reason":"DUPLICATE_OR_AMBIGUOUS_PRIOR_ATTEMPT"}
    # This tombstone is not removed by transient event pruning. Bounded authority
    # enrollment must provision a separate execution journal before live use.
    count=store.db.execute("SELECT COUNT(*) FROM kv WHERE key LIKE 'spent:%'").fetchone()[0]
    if count>=10000:
        return {"state":"REFUSED","reason":"EXECUTION_JOURNAL_CAPACITY"}
    store.put("spent:"+proposal.id,{"digest":digest,"expires":proposal.expires})
    store.event("action:"+proposal.id,now,"ACTION_INTENT",{"digest":digest})
    try:
        ack=sender(proposal)
        result={"state":"ACKNOWLEDGED" if ack is True else "SENT_UNCONFIRMED",
                "physically_observed":False}
    except Exception:
        result={"state":"UNKNOWN_OUTCOME","physically_observed":False}
    store.put("action-result:"+proposal.id,result)
    return result
