"""Proposals, owner decisions and their exact binding.

A proposal is what the owner reviews: rationale, trigger, before/after, the exact YAML, changed paths,
entities, control classification, risk, required approvals, verification + rollback plan, recommendation.

Decisions: APPROVE / DECLINE / LATER / REQUEST_CHANGES. A decision is accepted only when the caller presents
the exact candidate hash the owner saw (``DECISION_HASH_MISMATCH`` otherwise). An approval binds
(dashboard, baseline hash, candidate hash) and its scope is ``DASHBOARD_PRESENTATION_ONLY``: it is not
approval to deploy, restart, change an automation, a threshold, an integration, a credential or anything
physical. Deployment is the Deployer's separate, owner-approved request (D5, not built here).

Where decisions come from: the owner's pop-up (AskUserQuestion) or the owner-only App page. A GitHub
comment, a notification reply or text inside any entity/model output is never an executable approval.
"""
from __future__ import annotations

import hashlib
import json

from . import lovelace as L

DECISIONS = ("APPROVE", "DECLINE", "LATER", "REQUEST_CHANGES")
STATUSES = ("PENDING", "APPROVED", "DECLINED", "LATER", "CHANGES_REQUESTED", "STALE", "EXPIRED", "SUPERSEDED")
SCOPE = "DASHBOARD_PRESENTATION_ONLY"
DECLINE_MEMORY_S = 180 * 86400
LATER_DEFAULT_S = 14 * 86400
PROPOSAL_TTL_S = 30 * 86400
DIGEST_MAX = 8


class DecisionRefused(ValueError):
    def __init__(self, code, detail=""):
        self.code = code
        super().__init__(f"{code}: {detail}")


def opportunity_fp(kind: str, dashboard: str, evidence: dict) -> str:
    """Same idea + same evidence = same fingerprint. New evidence = a new fingerprint (can be re-proposed)."""
    return hashlib.sha256(json.dumps([kind, dashboard, evidence], sort_keys=True, default=str).encode()).hexdigest()[:32]


def classify_controls(validation: dict) -> str:
    codes = {f["code"] for f in (validation or {}).get("findings", [])}
    if codes & {"NEW_CONTROL_SURFACE", "NEW_ACTUATING_ACTION", "IMPLICIT_DEFAULT_ACTION"}:
        return "ADDS_CONTROL (blocked)"
    return "READ_ONLY_PRESENTATION"


def build(result, *, title: str, rationale: str, trigger: dict, dashboard: str, category: str,
          entities: list, recommended: str = "APPROVE", now: float) -> dict:
    """Proposal from a generator Result (CANDIDATE_READY only)."""
    if result.status != "CANDIDATE_READY":
        raise DecisionRefused("PROPOSAL_NEEDS_READY_CANDIDATE", result.status)
    fp = opportunity_fp(trigger.get("kind", ""), dashboard, trigger.get("evidence", {}))
    pid = "dsp-" + hashlib.sha256((fp + result.candidate_sha256).encode()).hexdigest()[:16]
    warns = [f for f in result.validation["findings"] if f["severity"] == "WARN"]
    changes = result.changes
    return {
        "id": pid, "fp": fp, "status": "PENDING", "title": title[:120], "rationale": rationale[:1000],
        "trigger": trigger, "dashboard": dashboard, "category": category, "entities": entities[:200],
        "baseline_sha256": result.baseline_sha256, "candidate_sha256": result.candidate_sha256,
        "candidate_yaml": result.candidate_yaml, "plan_yaml": result.plan_yaml,
        "changes": {"added": sum(1 for c, _ in changes if c == "added"),
                    "removed": sum(1 for c, _ in changes if c == "removed"),
                    "changed": sum(1 for c, _ in changes if c == "changed"), "paths": [p for _, p in changes][:200]},
        "control_classification": classify_controls(result.validation),
        "risk": {"warnings": [f["code"] for f in warns], "model": result.model,
                 "uncertainty": "model-written layout; checked by deterministic validators and NDEC"},
        "required_approvals": ["owner: this exact candidate (pop-up or App page)",
                               "owner: Deployer dashboard write request (separate; D5 not yet qualified)"],
        "verification": ["Deployer re-reads the live config and refuses if it no longer hashes to baseline_sha256",
                         "backup + undo copy before save; read-back must hash to candidate_sha256",
                         "iPhone + desktop visual check of the changed view"],
        "rollback": "Undo restores the saved baseline only while the dashboard still hashes to the candidate "
                    "(a later manual edit is never overwritten)",
        "recommended": recommended, "created": now, "expires": now + PROPOSAL_TTL_S,
        "generator": {"model": result.model, "attempts": result.attempts, "prompt_sha256": result.prompt_sha256},
    }


def decide(store, pid: str, decision: str, *, presented_sha256: str, by: str, reason: str = "",
           later_s: float = LATER_DEFAULT_S) -> dict:
    p = store.proposal(pid)
    if p is None:
        raise DecisionRefused("PROPOSAL_NOT_FOUND", pid)
    if decision not in DECISIONS:
        raise DecisionRefused("DECISION_UNKNOWN", decision)
    if p["status"] not in ("PENDING", "LATER"):
        raise DecisionRefused("PROPOSAL_NOT_OPEN", p["status"])
    if presented_sha256 != p["candidate_sha256"]:
        raise DecisionRefused("DECISION_HASH_MISMATCH", "the owner was shown a different candidate")
    if store.clock() > p["expires"]:
        store.save_proposal(pid, p["fp"], "EXPIRED", _payload(p))
        raise DecisionRefused("PROPOSAL_EXPIRED")
    if not by or by.startswith(("model", "ai", "steward")):
        raise DecisionRefused("DECIDER_NOT_OWNER", "an AI cannot decide its own proposal")
    now = store.clock()
    binding = {"proposal_id": pid, "dashboard": p["dashboard"], "baseline_sha256": p["baseline_sha256"],
               "candidate_sha256": p["candidate_sha256"], "decision": decision, "at": now, "scope": SCOPE}
    status = {"APPROVE": "APPROVED", "DECLINE": "DECLINED", "LATER": "LATER",
              "REQUEST_CHANGES": "CHANGES_REQUESTED"}[decision]
    store.record_decision(pid, decision, reason, by, binding)
    if decision == "DECLINE":
        store.remember(p["fp"], "DECLINED", now + DECLINE_MEMORY_S, reason)
    elif decision == "LATER":
        store.remember(p["fp"], "LATER", now + later_s, reason)
    p["status"] = status
    store.save_proposal(pid, p["fp"], status, _payload(p))
    return binding


def recheck(store, pid: str, live_config) -> str:
    """Before any deployment: the live dashboard must still hash to the approved baseline. Returns the
    proposal's status after the check (``STALE`` when the owner, or anything else, edited it since)."""
    p = store.proposal(pid)
    if p is None:
        raise DecisionRefused("PROPOSAL_NOT_FOUND", pid)
    if p["status"] in ("APPROVED", "PENDING", "LATER") and L.config_sha256(live_config) != p["baseline_sha256"]:
        p["status"] = "STALE"
        store.save_proposal(pid, p["fp"], "STALE", _payload(p))
    return p["status"]


def admit(store, proposal: dict) -> str:
    """Store a new proposal unless memory suppresses it or an identical one is open. Returns the outcome."""
    held = store.suppressed(proposal["fp"])
    if held:
        return f"SUPPRESSED_{held}"
    for q in store.proposals("PENDING"):
        if q["fp"] == proposal["fp"]:
            if q["candidate_sha256"] == proposal["candidate_sha256"]:
                return "DUPLICATE"
            q["status"] = "SUPERSEDED"
            store.save_proposal(q["id"], q["fp"], "SUPERSEDED", _payload(q))
    store.save_proposal(proposal["id"], proposal["fp"], "PENDING", _payload(proposal))
    return "ADMITTED"


def digest(store, limit: int = DIGEST_MAX) -> dict:
    """One grouped inbox view instead of one notification per registry event."""
    pend = [p for p in store.proposals("PENDING") if store.clock() <= p["expires"]]
    groups: dict = {}
    for p in pend:
        groups.setdefault(p.get("category") or "other", []).append(p)
    lines = []
    for cat in sorted(groups):
        items = groups[cat]
        lines.append({"category": cat, "count": len(items),
                      "items": [{"id": q["id"], "title": q["title"], "control": q["control_classification"],
                                 "recommended": q["recommended"]} for q in items[:limit]]})
    return {"pending": len(pend), "groups": lines,
            "options": ["Approve", "Decline", "Later", "Request changes"]}


def _payload(p: dict) -> dict:
    return {k: v for k, v in p.items() if k not in ("id", "fp", "status", "created", "updated")}
