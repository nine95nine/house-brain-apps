"""Deployer D5 requests: turn an owner-approved proposal into the exact request the Deployer accepts.

The Deployer's D5 lane (0.3.47) belongs to the ChatGPT lane, draft PR #1642, since the owner's transfer of
2026-11-12. This module is the Dashboard Steward side of that contract. It writes the request bytes; it
never sends them. Filing on ``deploy/requests`` is a separate owner pop-up, and the Deployer still asks the
owner on the phone.

Contract (checked against #1642 head ``092785f5``, ``hbd/dashboard_d5.py``):
* ``house_brain_dashboard_patch_request.v1`` / ``house_brain_dashboard_create_request.v1``;
* exactly the fields ``schema request_id requested_by tracking_issue dashboard proposal_id
  expect_candidate_sha256 plan owner_binding``, plus ``expect_config_sha256`` (patch) or ``title``
  (create);
* ``owner_binding`` = {proposal_id, candidate_sha256, scope: DASHBOARD_PRESENTATION_ONLY}. The Deployer
  treats it as a claim to check, never as consent;
* ``plan`` has exactly ``schema`` and ``operations``. The Steward's ``summary`` stays in the proposal.

``r1_fit`` says, before anything is filed, why #1642's R1 subset would refuse a plan. R1 admits additive
static Markdown and masonry-only creates. The Home Command Center (sections, tiles, navigation buttons)
does not fit R1. It needs the R2 port proposed in
``docs/handoff-archive/claude-d5-deployer-reference-2026-11-09/claude-d5-r2-port-on-1642-092785f5.patch``,
which adds ``allowed_control_entities`` (``build(..., allowed_control_entities=...)``).
"""
from __future__ import annotations

import json
import re

from . import lovelace as L, plan as P

PATCH_SCHEMA = "house_brain_dashboard_patch_request.v1"
CREATE_SCHEMA = "house_brain_dashboard_create_request.v1"
SCOPE = "DASHBOARD_PRESENTATION_ONLY"
CONTRACT_HEAD = "092785f533f58c6e2ad0c998eb090eb38cb47fef"   # #1642 head this contract was read from
RE_REQUEST_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")    # Deployer manifest.RE_REQUEST_ID
RE_DASHBOARD = re.compile(r"^[a-z0-9]+-[a-z0-9-]{1,60}$")   # #1642 RE_URL (a hyphen; not the default)
REQUESTERS = ("claude", "chatgpt", "owner", "grok", "gemini", "codex")
MAX_REQUEST_BYTES = 128 * 1024

# #1642 R1 admission (dashboard_d5.py at CONTRACT_HEAD)
R1_PATCH_OPS = frozenset({"add_card", "add_section", "add_view", "set_view_field"})
R1_CARD_FIELDS = frozenset({"type", "content", "title"})
R1_VIEW_FIELDS = frozenset({"title", "path", "cards", "type"})
R1_SECTION_FIELDS = frozenset({"title", "cards"})
R1_SET_FIELDS = frozenset({"title", "icon"})


class RequestRefused(ValueError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(f"{code}: {detail}")


def _card_fits(card, out: list, where: str):
    if not isinstance(card, dict) or card.get("type") != "markdown" or set(card) - R1_CARD_FIELDS:
        out.append(f"R1_CARD_NOT_STATIC_MARKDOWN {where}")
    elif any(t in str(card.get("content", "")) for t in ("{{", "{%")):
        out.append(f"R1_TEMPLATE_IN_MARKDOWN {where}")


def r1_fit(plan: dict, kind: str) -> list:
    """Reasons #1642's R1 would refuse ``plan`` ([] = it fits). Mirrors R1's documented subset."""
    out: list = []
    ops = plan.get("operations") or []
    for i, op in enumerate(ops):
        where = f"op{i}"
        name = op.get("op")
        if kind == "create":
            if name != "new_dashboard" or len(ops) != 1:
                out.append("R1_CREATE_IS_ONE_NEW_DASHBOARD")
                continue
            cfg = op.get("config") or {}
            if set(cfg) - {"views", "title"}:
                out.append("R1_CREATE_CONFIG_FIELDS")
            for vi, v in enumerate(cfg.get("views") or []):
                if not isinstance(v, dict) or set(v) - R1_VIEW_FIELDS or v.get("type", "masonry") != "masonry":
                    out.append(f"R1_CREATE_VIEW_NOT_MASONRY view{vi}")
                    continue
                for ci, c in enumerate(v.get("cards") or []):
                    _card_fits(c, out, f"view{vi}/card{ci}")
            continue
        if name not in R1_PATCH_OPS:
            out.append(f"R1_OP_REFUSED {name} {where}")
        elif op.get("position") not in (None, "end") and name != "set_view_field":
            out.append(f"R1_POSITION_NOT_END {where}")
        elif name == "add_card":
            _card_fits(op.get("card"), out, where)
        elif name == "add_section":
            sec = op.get("section") or {}
            if set(sec) - R1_SECTION_FIELDS:
                out.append(f"R1_SECTION_FIELDS {where}")
            for ci, c in enumerate(sec.get("cards") or []):
                _card_fits(c, out, f"{where}/card{ci}")
        elif name == "add_view":
            v = op.get("view_config") or {}
            if set(v) - R1_VIEW_FIELDS or v.get("type", "masonry") != "masonry":
                out.append(f"R1_VIEW_NOT_MASONRY {where}")
            for ci, c in enumerate(v.get("cards") or []):
                _card_fits(c, out, f"{where}/card{ci}")
        elif name == "set_view_field" and op.get("field") not in R1_SET_FIELDS:
            out.append(f"R1_FIELD_REFUSED {op.get('field')} {where}")
    return out


def build(proposal: dict, plan: dict, *, request_id: str, tracking_issue: int, baseline: dict | None = None,
          requested_by: str = "claude", title: str = "", allowed_control_entities=()) -> bytes:
    """Canonical request bytes for an APPROVED proposal. Refuses anything the contract would refuse."""
    if proposal.get("status") != "APPROVED":
        raise RequestRefused("PROPOSAL_NOT_APPROVED", str(proposal.get("status")))
    if not RE_REQUEST_ID.fullmatch(request_id or ""):
        raise RequestRefused("REQUEST_ID")
    if requested_by not in REQUESTERS:
        raise RequestRefused("REQUESTER")
    if type(tracking_issue) is not int or not 1 <= tracking_issue <= 10_000_000:
        raise RequestRefused("TRACKING_ISSUE")
    P.check_shape(plan)
    kind = "create" if plan["operations"][0]["op"] == "new_dashboard" else "patch"
    dashboard = plan["operations"][0]["url_path"] if kind == "create" else proposal["dashboard"]
    if not RE_DASHBOARD.fullmatch(dashboard or ""):
        raise RequestRefused("DASHBOARD_PATH", "the Deployer D5 lane needs a hyphenated dashboard url")
    candidate = proposal["candidate_sha256"]
    # the plan must still produce exactly the candidate the owner approved
    if kind == "patch" and (baseline is None or L.config_sha256(baseline) != proposal["baseline_sha256"]):
        raise RequestRefused("BASELINE_CONFIG_REQUIRED", "the exact approved baseline config")
    produced, _ = P.apply(plan, None if kind == "create" else baseline)
    if L.config_sha256(produced) != candidate:
        raise RequestRefused("PLAN_DOES_NOT_PRODUCE_CANDIDATE")
    body = {"schema": CREATE_SCHEMA if kind == "create" else PATCH_SCHEMA, "request_id": request_id,
            "requested_by": requested_by, "tracking_issue": tracking_issue, "dashboard": dashboard,
            "proposal_id": proposal["id"], "expect_candidate_sha256": candidate,
            "plan": {"schema": plan["schema"], "operations": plan["operations"]},
            "owner_binding": {"proposal_id": proposal["id"], "candidate_sha256": candidate, "scope": SCOPE}}
    if allowed_control_entities:
        # R2 port only: the controls the owner's phone message names. R1 (#1642 as of CONTRACT_HEAD) has no
        # such field and refuses unknown fields, so it is sent only when the caller names controls.
        names = sorted(set(allowed_control_entities))
        if len(names) > 16 or not all(isinstance(n, str) and "." in n for n in names):
            raise RequestRefused("ALLOWED_CONTROL_ENTITIES")
        body["allowed_control_entities"] = names
    if kind == "patch":
        body["expect_config_sha256"] = proposal["baseline_sha256"]
    else:
        body["title"] = title or plan["operations"][0].get("title") or ""
        if not 1 <= len(body["title"]) <= 80:
            raise RequestRefused("TITLE")
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw) > MAX_REQUEST_BYTES:
        raise RequestRefused("REQUEST_TOO_LARGE", str(len(raw)))
    return raw
