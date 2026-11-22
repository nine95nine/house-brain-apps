"""No-Loss Dashboard Evolution Contract (NDEC) and the Capability Access Ledger.

The ledger is derived from the accepted/current baseline config (never from the candidate): every entity the
dashboard shows, every action it offers, every view, and the pinned anchors the household has learned.
``check`` then proves a candidate keeps all of it, or names exactly what it loses. YAML parsing success
proves none of this.

NDEC items (directive §7) and where they are enforced:
 1 capabilities reachable        CAPABILITY_LOST, CAPABILITY_UNREACHABLE (here)
 2 required actions present      ACTION_LOST (here)
 3 navigation resolves           validate.DEAD_NAVIGATION / UNREACHABLE_SUBVIEW
 4 entity references valid       validate.HALLUCINATED_ENTITY / MISSING_ENTITY / ENTITIES_UNVERIFIED
 5 control semantics unchanged   CONFIRMATION_REMOVED, ACTION_RETARGETED (here)
 6 pinned favorites stable       PIN_MOVED, PIN_CHANGED (here)
 7 hidden views reachable        VIEW_LOST (here), validate.UNREACHABLE_SUBVIEW
 8 critical uncertainty shown    CRITICAL_STATUS_HIDDEN (here)
 9/10 fingerprints / live drift  approval.bind / approval.recheck
 11 recovery and undo            deploy plan (D5) requires an undo snapshot
 12 no incidental physical ctrl  validate.NEW_CONTROL_SURFACE / NEW_ACTUATING_ACTION
"""
from __future__ import annotations

from dataclasses import dataclass

from . import lovelace as L
from .validate import BLOCK, INFO, WARN, Finding

LEDGER_VERSION = "dashboard_steward_capability_ledger.v1"


def _views_reachable(m: L.Model) -> set:
    """View indexes reachable by a person: every non-subview view, plus subviews something navigates to."""
    reach = {v.index for v in m.views if not v.subview}
    targets = set()
    for _, nav in m.navigation:
        parts = nav.split("?", 1)[0].strip("/").split("/")
        if len(parts) >= 2:
            targets.add(parts[1])
    reach |= {v.index for v in m.views if v.subview and (v.ident in targets or str(v.index) in targets)}
    return reach


def _view_of(path: str) -> int:
    try:
        return int(path.split("/")[2])
    except (IndexError, ValueError):
        return -1


def _gated(config, path: str) -> bool:
    """True when ``path`` sits inside a conditional card or a section/card with visibility conditions."""
    node = config
    parts = [p for p in path.split("/") if p]
    for p in parts:
        if isinstance(node, dict):
            if node.get("type") == "conditional" or node.get("visibility"):
                return True
            node = node.get(p)
        elif isinstance(node, list):
            try:
                node = node[int(p)]
            except (ValueError, IndexError):
                return False
        else:
            return False
    return isinstance(node, dict) and bool(node.get("visibility"))


def card_at(config, view_ident: str, section: int | None, card: int):
    for i, v in enumerate(config.get("views") or []):
        if isinstance(v, dict) and ((v.get("path") or str(i)) == view_ident or str(i) == view_ident):
            try:
                return (v["sections"][section]["cards"][card] if section is not None else v["cards"][card])
            except (KeyError, IndexError, TypeError):
                return None
    return None


def build_ledger(baseline, *, dashboard_url: str, critical_entities=(), pins=()) -> dict:
    """Ledger from the baseline. ``pins``: [{"view": ident, "section": int|None, "card": int}] anchors."""
    m = L.walk(baseline)
    reach = _views_reachable(m)
    caps = []
    for eid in sorted({r.entity_id for r in m.refs}):
        locs = sorted({r.path for r in m.refs if r.entity_id == eid})
        reachable = any(_view_of(p) in reach for p in locs)
        caps.append({"id": f"entity:{eid}", "kind": "entity", "entity": eid,
                     "authority": "control" if L.domain(eid) in L.ACTUATOR_DOMAINS else "read",
                     "reachable": reachable, "locations": locs[:20],
                     "critical": eid in set(critical_entities)})
    for a in m.actions:
        if a.action in L.ACTUATING_ACTIONS:
            caps.append({"id": f"action:{a.action}:{a.service}:{a.entity}:{','.join(sorted(a.target))}",
                         "kind": "action", "action": a.action, "service": a.service, "entity": a.entity,
                         "target": sorted(a.target), "confirmation": a.confirmation, "location": a.path,
                         "authority": "sensitive_control" if any(
                             L.domain(t) in L.SENSITIVE_DOMAINS for t in set(a.target) | {a.entity} if t)
                         else "control"})
    for v in m.views:
        caps.append({"id": f"view:{v.ident}", "kind": "view", "view": v.ident, "title": v.title,
                     "subview": v.subview, "reachable": v.index in reach})
    pin_rows = []
    for p in pins:
        c = card_at(baseline, p["view"], p.get("section"), p["card"])
        pin_rows.append({**p, "fingerprint": L.config_sha256(c) if c is not None else None})
    # de-duplicate action capabilities (same action offered in two places is one capability)
    seen, uniq = set(), []
    for c in caps:
        if c["id"] not in seen:
            seen.add(c["id"])
            uniq.append(c)
    return {"schema": LEDGER_VERSION, "dashboard": dashboard_url, "baseline_sha256": L.config_sha256(baseline),
            "capabilities": uniq, "pins": pin_rows}


@dataclass
class NdecResult:
    findings: list

    @property
    def verdict(self) -> str:
        return "FAIL" if any(f.severity == BLOCK for f in self.findings) else "PASS"

    def codes(self, severity=None) -> set:
        return {f.code for f in self.findings if severity is None or f.severity == severity}


def check(ledger: dict, candidate, *, approved_removals=frozenset()) -> NdecResult:
    """Prove ``candidate`` keeps every ledger capability. ``approved_removals``: capability ids the owner
    explicitly approved removing (an owner decision bound to this exact candidate)."""
    out: list[Finding] = []
    m = L.walk(candidate)
    reach = _views_reachable(m)
    by_entity: dict = {}
    for r in m.refs:
        by_entity.setdefault(r.entity_id, []).append(r.path)
    cand_actions = [a for a in m.actions if a.action in L.ACTUATING_ACTIONS]
    view_idents = {v.ident for v in m.views}
    for cap in ledger.get("capabilities", []):
        cid = cap["id"]
        if cid in approved_removals:
            out.append(Finding("CAPABILITY_REMOVAL_APPROVED", INFO, "", cid))
            continue
        if cap["kind"] == "entity":
            paths = by_entity.get(cap["entity"], [])
            if not paths:
                out.append(Finding("CAPABILITY_LOST", BLOCK, "", cap["entity"]))
            elif cap.get("reachable") and not any(_view_of(p) in reach for p in paths):
                out.append(Finding("CAPABILITY_UNREACHABLE", BLOCK, paths[0], cap["entity"]))
            elif cap.get("critical") and all(_gated(candidate, p) for p in paths):
                out.append(Finding("CRITICAL_STATUS_HIDDEN", BLOCK, paths[0],
                                   f"{cap['entity']} is only shown under a condition"))
        elif cap["kind"] == "action":
            same = [a for a in cand_actions if a.action == cap["action"] and a.service == cap["service"]
                    and a.entity == cap["entity"] and sorted(a.target) == cap["target"]]
            if not same:
                retarget = [a for a in cand_actions if a.action == cap["action"] and a.service == cap["service"]]
                out.append(Finding("ACTION_RETARGETED" if retarget else "ACTION_LOST", BLOCK, cap["location"], cid))
            elif cap.get("confirmation") and not any(a.confirmation for a in same):
                out.append(Finding("CONFIRMATION_REMOVED", BLOCK, same[0].path, cid))
        elif cap["kind"] == "view":
            if cap["view"] not in view_idents:
                out.append(Finding("VIEW_LOST", BLOCK, "", cap["view"]))
            elif cap.get("reachable"):
                idx = next(v.index for v in m.views if v.ident == cap["view"])
                if idx not in reach:
                    out.append(Finding("VIEW_UNREACHABLE", BLOCK, f"/views/{idx}", cap["view"]))
    for p in ledger.get("pins", []):
        c = card_at(candidate, p["view"], p.get("section"), p["card"])
        if c is None:
            out.append(Finding("PIN_MOVED", BLOCK, "", f"{p['view']}/{p.get('section')}/{p['card']}"))
        elif p.get("fingerprint") and L.config_sha256(c) != p["fingerprint"]:
            out.append(Finding("PIN_CHANGED", BLOCK, "", f"{p['view']}/{p.get('section')}/{p['card']}"))
    if candidate is not None and L.config_sha256(candidate) == ledger.get("baseline_sha256"):
        out.append(Finding("NO_CHANGE", WARN, "", "candidate equals the baseline"))
    return NdecResult(out)
