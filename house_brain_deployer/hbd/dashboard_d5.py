"""Dashboard Steward D5 release R1: closed, non-actuating plan interpreter.

Source-only takeover of the Deployer 0.3.50 D5 lane.  This deliberately
does NOT write Home Assistant, authenticate a claimed model decision, mint
an owner receipt, or weaken existing dashboard operations.  It provides
a small, reviewable, deterministic admission kernel for the later
authenticated engine and independent fake-HA runner.

R1 accepts additive display-only patches.  Destructive changes, renames,
new control surfaces, hidden views, removals and arbitrary Lovelace
actions fail closed until separately engineered and qualified.  Model-
supplied owner_binding is a hash *claim*, not trusted identity or consent.

R2 (proposed by the Dashboard Steward lane, 2026-11-14; reference bundle
docs/handoff-archive/claude-d5-deployer-reference-2026-11-09/): native display
cards (heading, tile, entities, glance, button, markdown, conditional, grid
and stacks ...) instead of static Markdown only, and sections dashboards on
create, under one closed card guard. Actions may only open more-info, do
nothing, or navigate to an absolute path. An entity whose domain can change the
house is refused unless the request names it in ``allowed_control_entities``
(the owner's phone shows those names), and then every tap/hold (and tile icon)
action on its card must be explicit. Service calls, targets, url actions,
custom cards, iframes, strategies and templates stay refused. A created
subview must be reachable by navigation from a top-level view of the same new
dashboard. Repoint, removal, replacement, reordering and subview changes on an
existing dashboard stay refused, as in R1.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import re

from . import dashboards, dashboard_d5_no_loss
from .manifest import RE_REQUEST_ID, REQUESTERS

PATCH_SCHEMA = "house_brain_dashboard_patch_request.v1"
CREATE_SCHEMA = "house_brain_dashboard_create_request.v1"
PLAN_SCHEMA = "dashboard_steward_change_plan.v1"
BINDING_SCOPE = "DASHBOARD_PRESENTATION_ONLY"
MAX_REQUEST_BYTES = 128 * 1024
MAX_OPERATIONS = 40
MAX_CONFIG_BYTES = 3 * 1024 * 1024
RE_SHA = re.compile(r"^[0-9a-f]{64}$")
RE_URL = re.compile(r"^[a-z0-9]+-[a-z0-9-]{1,60}$")
RE_PROPOSAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,100}$")
RE_VIEW_PATH = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
# Until independent registry/NDEC qualification exists, the only safe
# generated card is static Markdown. Graph/gauge cards can introduce
# unverified entities and implicit controls, so they fail closed.
DISPLAY_CARD_TYPES = frozenset({"markdown"})
# R2: native display cards; containers hold cards; actions are display-only.
R2_CARD_TYPES = frozenset({"markdown", "heading", "tile", "entities", "glance", "button", "sensor", "gauge",
                           "history-graph", "weather-forecast", "conditional", "grid", "vertical-stack",
                           "horizontal-stack"})
R2_SAFE_ACTIONS = frozenset({"more-info", "none", "navigate"})
R2_ACTION_KEYS = frozenset({"tap_action", "hold_action", "double_tap_action", "icon_tap_action",
                            "icon_hold_action", "icon_double_tap_action"})
R2_FORBIDDEN_KEYS = frozenset({"service", "perform_action", "service_data", "target", "url", "url_path",
                               "strategy", "data", "confirmation", "card_mod", "style"})
IMPLICIT_ACTION_TYPES = frozenset({"button", "tile", "entity", "glance", "light"})
ACTUATOR_DOMAINS = frozenset({
    "alarm_control_panel", "automation", "button", "climate", "cover", "date", "datetime", "fan", "group",
    "humidifier", "input_boolean", "input_button", "input_datetime", "input_number", "input_select", "input_text",
    "lawn_mower", "light", "lock", "media_player", "number", "remote", "scene", "script", "select", "siren",
    "switch", "text", "time", "timer", "update", "vacuum", "valve", "water_heater", "counter"})
RE_ENTITY = re.compile(r"^[a-z_]{2,32}\.[a-z0-9_]{1,96}$")
RE_NAV = re.compile(r"^/[a-z0-9][a-z0-9_/-]{0,120}$")
MAX_ALLOWED_CONTROLS = 16
R2_VIEW_FIELDS = frozenset({"title", "path", "icon", "type", "cards", "sections", "subview", "back_path",
                            "max_columns"})
STATIC_MARKDOWN_FIELDS = frozenset({"type", "content", "title"})
FORBIDDEN_ACTION_KEYS = frozenset({
    "tap_action", "hold_action", "double_tap_action", "icon_tap_action",
    "icon_hold_action", "action", "service", "perform_action", "service_data",
    "target", "confirmation", "type:custom", "javascript", "url",
})


class D5Refused(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class D5Request:
    kind: str
    request_id: str
    tracking_issue: int
    requested_by: str
    dashboard: str
    title: str
    proposal_id: str
    expect_config_sha256: str | None
    expect_candidate_sha256: str
    plan: dict
    digest: str
    allowed_controls: tuple = ()


def _refuse_if(condition: bool, reason: str):
    if condition:
        raise D5Refused(reason)


def _canonical(value) -> bytes:
    try:
        data = json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as err:
        raise D5Refused("NON_CANONICAL_JSON") from err
    return data


def _unique(pairs):
    result = {}
    for k, v in pairs:
        _refuse_if(k in result, "DUPLICATE_JSON_KEY")
        result[k] = v
    return result


def _constant(_value):
    raise D5Refused("JSON_NONFINITE")


def is_d5(raw: bytes) -> bool:
    """Classification only, not validation (the parsed request is still checked)."""
    if not isinstance(raw, bytes) or len(raw) > MAX_REQUEST_BYTES:
        return False
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return False
    return isinstance(data, dict) and data.get("schema") in {PATCH_SCHEMA, CREATE_SCHEMA}


def parse(raw: bytes) -> D5Request:
    _refuse_if(not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_REQUEST_BYTES,
               "REQUEST_SIZE")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique,
                          parse_constant=_constant)
    except D5Refused:
        raise  # preserve security-specific refusal codes, including duplicate keys
    except (ValueError, UnicodeDecodeError, RecursionError) as err:
        raise D5Refused("REQUEST_JSON") from err
    _refuse_if(not isinstance(data, dict), "REQUEST_SHAPE")
    schema = data.get("schema")
    _refuse_if(schema not in {PATCH_SCHEMA, CREATE_SCHEMA}, "SCHEMA_REFUSED")
    kind = "patch" if schema == PATCH_SCHEMA else "create"
    fields = {"schema", "request_id", "requested_by", "tracking_issue",
              "dashboard", "proposal_id", "expect_candidate_sha256",
              "plan", "owner_binding"}
    if kind == "patch":
        fields.add("expect_config_sha256")
    else:
        fields.add("title")
    _refuse_if(set(data) - {"allowed_control_entities"} != fields, "REQUEST_FIELDS")
    allowed = data.get("allowed_control_entities", [])
    _refuse_if(not isinstance(allowed, list) or len(allowed) > MAX_ALLOWED_CONTROLS
               or len(set(map(str, allowed))) != len(allowed)
               or not all(isinstance(e, str) and RE_ENTITY.fullmatch(e)
                          and e.split(".", 1)[0] in ACTUATOR_DOMAINS for e in allowed),
               "ALLOWED_CONTROL_ENTITIES")
    rid = data["request_id"]
    _refuse_if(not isinstance(rid, str) or not RE_REQUEST_ID.fullmatch(rid),
               "REQUEST_ID")
    who = data["requested_by"]
    _refuse_if(not isinstance(who, str) or who not in REQUESTERS, "REQUESTER")
    issue = data["tracking_issue"]
    _refuse_if(type(issue) is not int or not 1 <= issue <= 10000000, "TRACKING_ISSUE")
    dashboard = data["dashboard"]
    _refuse_if(not isinstance(dashboard, str) or not RE_URL.fullmatch(dashboard),
               "DASHBOARD_PATH")
    proposal = data["proposal_id"]
    _refuse_if(not isinstance(proposal, str) or not RE_PROPOSAL.fullmatch(proposal),
               "PROPOSAL_ID")
    expected = data["expect_candidate_sha256"]
    _refuse_if(not isinstance(expected, str) or not RE_SHA.fullmatch(expected),
               "CANDIDATE_SHA")
    baseline = data.get("expect_config_sha256")
    if kind == "patch":
        _refuse_if(not isinstance(baseline, str) or not RE_SHA.fullmatch(baseline),
                   "BASELINE_SHA")
    title = data.get("title", "")
    _refuse_if(kind == "create" and (not isinstance(title, str)
               or not 1 <= len(title) <= 80 or any(ord(x) < 32 for x in title)),
               "TITLE")
    binding = data["owner_binding"]
    # A claim supplied inside the request is not a trusted user identity.
    _refuse_if(not isinstance(binding, dict) or set(binding) != {
        "proposal_id", "candidate_sha256", "scope"
    }, "BINDING_FIELDS")
    _refuse_if(binding != {
        "proposal_id": proposal, "candidate_sha256": expected,
        "scope": BINDING_SCOPE
    }, "BINDING_MISMATCH")
    plan = data["plan"]
    _refuse_if(not isinstance(plan, dict) or set(plan) != {"schema", "operations"}
               or plan["schema"] != PLAN_SCHEMA, "PLAN_SCHEMA")
    ops = plan["operations"]
    _refuse_if(not isinstance(ops, list) or not 1 <= len(ops) <= MAX_OPERATIONS,
               "PLAN_OPERATIONS")
    kinds = ({"add_card", "add_section", "add_view", "set_view_field"}
             if kind == "patch" else {"new_dashboard"})
    for op in ops:
        _refuse_if(not isinstance(op, dict) or op.get("op") not in kinds,
                   "PLAN_KIND_REFUSED")
    _refuse_if(kind == "create" and len(ops) != 1, "CREATE_ONE_OPERATION")
    return D5Request(kind, rid, issue, who, dashboard, title, proposal,
                     baseline, expected, copy.deepcopy(plan), hashlib.sha256(raw).hexdigest(),
                     tuple(sorted(allowed)))


def _display_only(node, *, root=False, depth=0) -> None:
    _refuse_if(depth > 24, "DEPTH")
    if isinstance(node, dict):
        _refuse_if(any(not isinstance(k, str) or k in FORBIDDEN_ACTION_KEYS
                       for k in node), "CONTROL_OR_NONSTRING_KEY")
        if root:
            _refuse_if(node.get("type") not in DISPLAY_CARD_TYPES,
                       "NEW_CARD_TYPE_REFUSED")
            _refuse_if(set(node) - STATIC_MARKDOWN_FIELDS, "NEW_CARD_UNREVIEWED_FIELDS")
            _refuse_if(not isinstance(node.get("content"), str) or
                       not 1 <= len(node["content"]) <= 4000, "MARKDOWN_CONTENT")
            if "title" in node:
                _refuse_if(not isinstance(node["title"], str) or
                           not 1 <= len(node["title"]) <= 80, "MARKDOWN_TITLE")
        for v in node.values():
            _display_only(v, depth=depth + 1)
    elif isinstance(node, list):
        for v in node:
            _display_only(v, depth=depth + 1)
    elif isinstance(node, str):
        _refuse_if(len(node) > 16000 or
                   any(s in node.lower() for s in ("<script", "javascript:", "data:text/html",
                                                   "{%", "{{", "onerror=", "onload=")),
                   "ACTIVE_CONTENT")
    elif type(node) not in (int, float, bool, type(None)):
        raise D5Refused("NON_JSON_VALUE")


def _r2_actions(card: dict, entities: set, allowed: frozenset) -> None:
    for key in R2_ACTION_KEYS & set(card):
        act = card[key]
        _refuse_if(not isinstance(act, dict) or set(act) - {"action", "navigation_path"}
                   or act.get("action") not in R2_SAFE_ACTIONS, "R2_ACTION_NOT_DISPLAY_ONLY")
        if act["action"] == "navigate":
            nav = act.get("navigation_path")
            _refuse_if(not isinstance(nav, str) or not RE_NAV.fullmatch(nav) or "//" in nav, "R2_NAVIGATION_PATH")
        else:
            _refuse_if("navigation_path" in act, "R2_ACTION_FIELDS")
    controls = {e for e in entities if e.split(".", 1)[0] in ACTUATOR_DOMAINS}
    _refuse_if(bool(controls - allowed), "R2_UNNAMED_CONTROL_ENTITY")
    if controls and card.get("type") in IMPLICIT_ACTION_TYPES:
        need = {"tap_action", "hold_action", "double_tap_action"}
        if card.get("type") == "tile":
            need.add("icon_tap_action")
        _refuse_if(bool(need - set(card)), "R2_IMPLICIT_DEFAULT_ACTION")


def _r2_entities(node, out: set, depth=0) -> None:
    _refuse_if(depth > 24, "DEPTH")
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "entity" and isinstance(v, str):
                _refuse_if(not RE_ENTITY.fullmatch(v), "R2_ENTITY_ID")
                out.add(v)
            elif k == "entities" and isinstance(v, list):
                for row in v:
                    if isinstance(row, str):
                        _refuse_if(not RE_ENTITY.fullmatch(row), "R2_ENTITY_ID")
                        out.add(row)
                    else:
                        _r2_entities(row, out, depth + 1)
            elif k not in ("card", "cards"):
                _r2_entities(v, out, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _r2_entities(v, out, depth + 1)


def _r2_card(card, allowed: frozenset, depth=0) -> None:
    """R2 closed card guard (replaces static-Markdown-only for generated cards)."""
    _refuse_if(depth > 12 or not isinstance(card, dict), "CARD_SHAPE")
    ctype = card.get("type")
    _refuse_if(not isinstance(ctype, str) or ctype not in R2_CARD_TYPES, "NEW_CARD_TYPE_REFUSED")
    _refuse_if(bool(R2_FORBIDDEN_KEYS & set(card)), "R2_FORBIDDEN_KEY")
    if ctype == "markdown":
        _refuse_if(set(card) - STATIC_MARKDOWN_FIELDS, "NEW_CARD_UNREVIEWED_FIELDS")
        _refuse_if(not isinstance(card.get("content"), str) or not 1 <= len(card["content"]) <= 4000,
                   "MARKDOWN_CONTENT")
    # strings: no templates or active content anywhere (R1 rule kept)
    _display_text(card)
    if ctype == "conditional":
        conds = card.get("conditions")
        _refuse_if(not isinstance(conds, list) or not conds or set(card) - {"type", "conditions", "card"},
                   "R2_CONDITIONAL_SHAPE")
        for c in conds:
            _refuse_if(not isinstance(c, dict) or set(c) - {"condition", "entity", "state", "state_not"},
                       "R2_CONDITION_SHAPE")
        _r2_card(card.get("card"), allowed, depth + 1)
        ents = set()
        _r2_entities({"x": conds}, ents)
        _refuse_if(bool({e for e in ents if e.split(".", 1)[0] in ACTUATOR_DOMAINS} - allowed),
                   "R2_UNNAMED_CONTROL_ENTITY")
        return
    if ctype in ("grid", "vertical-stack", "horizontal-stack"):
        _refuse_if(set(card) - {"type", "cards", "columns", "square", "title"} or not isinstance(card.get("cards"), list),
                   "R2_STACK_SHAPE")
        for sub in card["cards"]:
            _r2_card(sub, allowed, depth + 1)
        return
    ents = set()
    _r2_entities(card, ents)
    _r2_actions(card, ents, allowed)
    if ctype == "entities":
        for row in card.get("entities") or []:
            if isinstance(row, dict):
                _refuse_if(set(row) - {"entity", "name", "type", "icon"}
                           or row.get("type", "simple-entity") not in ("simple-entity", "attribute"),
                           "R2_ENTITIES_ROW")
            elif isinstance(row, str) and row.split(".", 1)[0] in ACTUATOR_DOMAINS:
                raise D5Refused("R2_INLINE_CONTROL_ROW")   # a plain row renders a toggle


def _display_text(node, depth=0) -> None:
    _refuse_if(depth > 24, "DEPTH")
    if isinstance(node, dict):
        _refuse_if(any(not isinstance(k, str) for k in node), "CONTROL_OR_NONSTRING_KEY")
        for v in node.values():
            _display_text(v, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _display_text(v, depth + 1)
    elif isinstance(node, str):
        _refuse_if(len(node) > 16000 or node.startswith("custom:") or
                   any(t in node.lower() for t in ("<script", "javascript:", "data:text/html",
                                                   "{%", "{{", "onerror=", "onload=", "iframe")),
                   "ACTIVE_CONTENT")
    elif type(node) not in (int, float, bool, type(None)):
        raise D5Refused("NON_JSON_VALUE")


def _r2_section(section, allowed: frozenset) -> None:
    _refuse_if(not isinstance(section, dict), "SECTION_SHAPE")
    if "type" in section:   # a native sections-view grid section
        _refuse_if(section.get("type") != "grid" or set(section) - {"type", "cards", "column_span"},
                   "SECTION_SHAPE")
    else:
        _refuse_if(set(section) - {"title", "cards"}, "SECTION_SHAPE")
        _refuse_if(not isinstance(section.get("title", ""), str) or len(section.get("title", "")) > 80,
                   "SECTION_TITLE")
    _refuse_if(not isinstance(section.get("cards"), list), "SECTION_CARDS")
    for card in section["cards"]:
        _r2_card(card, allowed)


def _r2_create_views(views, allowed: frozenset) -> None:
    seen, navs, subviews = set(), set(), set()
    for view in views:
        _refuse_if(not isinstance(view, dict) or set(view) - R2_VIEW_FIELDS, "CREATE_VIEW_SHAPE")
        vtype = view.get("type", "masonry")
        _refuse_if(vtype not in ("masonry", "sections"), "CREATE_VIEW_TYPE")
        path = view.get("path")
        _refuse_if(not isinstance(path, str) or not RE_VIEW_PATH.fullmatch(path) or path in seen, "CREATE_VIEW_PATH")
        seen.add(path)
        for key in ("title", "icon", "back_path"):
            _refuse_if(key in view and (not isinstance(view[key], str) or len(view[key]) > 120), "CREATE_VIEW_FIELD")
        _refuse_if("subview" in view and type(view["subview"]) is not bool, "CREATE_VIEW_FIELD")
        _refuse_if("max_columns" in view and (type(view["max_columns"]) is not int
                                              or not 1 <= view["max_columns"] <= 6), "CREATE_VIEW_FIELD")
        if view.get("subview"):
            subviews.add(path)
        if vtype == "sections":
            _refuse_if("cards" in view or not isinstance(view.get("sections"), list), "CREATE_VIEW_TYPE")
            for sec in view["sections"]:
                _r2_section(sec, allowed)
            cards = [c for sec in view["sections"] for c in sec["cards"]]
        else:
            _refuse_if("sections" in view or not isinstance(view.get("cards"), list), "CREATE_VIEW_TYPE")
            for card in view["cards"]:
                _r2_card(card, allowed)
            cards = view["cards"]
        if not view.get("subview"):
            _collect_navs(cards, navs)
    for sub in subviews:   # a created subview must be reachable from a top-level view
        _refuse_if(not any(n.rstrip("/").endswith("/" + sub) for n in navs), "CREATE_SUBVIEW_UNREACHABLE")


def _collect_navs(node, out: set) -> None:
    if isinstance(node, dict):
        act = node.get("action")
        if act == "navigate" and isinstance(node.get("navigation_path"), str):
            out.add(node["navigation_path"])
        for v in node.values():
            _collect_navs(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_navs(v, out)


def _view(config: dict, ident):
    views = config.get("views")
    _refuse_if(not isinstance(views, list), "VIEWS_SHAPE")
    for i, row in enumerate(views):
        if isinstance(row, dict) and (row.get("path") == ident or str(i) == str(ident)):
            return row
    raise D5Refused("VIEW_NOT_FOUND")


def _cards(view, section):
    if section is None:
        _refuse_if("sections" in view, "SECTION_REQUIRED")
        cards = view.setdefault("cards", [])
    else:
        secs = view.get("sections")
        _refuse_if(type(section) is not int or not isinstance(secs, list)
                   or not 0 <= section < len(secs), "SECTION_NOT_FOUND")
        _refuse_if(not isinstance(secs[section], dict)
                   or "strategy" in secs[section], "SECTION_IMMUTABLE")
        cards = secs[section].setdefault("cards", [])
    _refuse_if(not isinstance(cards, list), "CARDS_SHAPE")
    return cards


def _additive_patch(plan: dict, before: dict, allowed: frozenset = frozenset()) -> dict:
    cfg = copy.deepcopy(before)
    for op in plan["operations"]:
        kind = op["op"]
        if kind == "add_card":
            _refuse_if(set(op) - {"op", "view", "section", "card", "position"},
                       "OP_FIELDS")
            _refuse_if(op.get("position") not in (None, "end"), "PIN_REORDER_REFUSED")
            card = op.get("card")
            _refuse_if(not isinstance(card, dict), "CARD_SHAPE")
            _r2_card(card, allowed)
            _cards(_view(cfg, op.get("view")), op.get("section")).append(copy.deepcopy(card))
        elif kind == "add_section":
            _refuse_if(set(op) - {"op", "view", "section", "position"}, "OP_FIELDS")
            _refuse_if(op.get("position") not in (None, "end"), "PIN_REORDER_REFUSED")
            section = op.get("section")
            _r2_section(section, allowed)
            view = _view(cfg, op.get("view"))
            secs = view.get("sections")
            _refuse_if(not isinstance(secs, list) or "cards" in view,
                       "NOT_A_SECTION_VIEW")
            secs.append(copy.deepcopy(section))
        elif kind == "add_view":
            _refuse_if(set(op) - {"op", "view_config", "position"}, "OP_FIELDS")
            _refuse_if(op.get("position") not in (None, "end"), "PIN_REORDER_REFUSED")
            view = op.get("view_config")
            _refuse_if(not isinstance(view, dict)
                       or set(view) - {"title", "path", "cards", "type"}, "VIEW_SHAPE")
            _refuse_if(view.get("type", "masonry") != "masonry", "VIEW_TYPE")
            _refuse_if(not isinstance(view.get("path"), str)
                       or not RE_VIEW_PATH.fullmatch(view["path"]), "VIEW_PATH")
            _refuse_if(not isinstance(view.get("title", ""), str)
                       or len(view.get("title", "")) > 80, "VIEW_TITLE")
            _refuse_if(not isinstance(view.get("cards"), list), "VIEW_CARDS")
            for card in view["cards"]:
                _refuse_if(not isinstance(card, dict), "CARD_SHAPE")
                _r2_card(card, allowed)
            views = cfg.get("views")
            _refuse_if(not isinstance(views, list) or any(
                isinstance(v, dict) and v.get("path") == view["path"] for v in views
            ), "VIEW_DUPLICATE")
            views.append(copy.deepcopy(view))
        else:
            _refuse_if(set(op) != {"op", "view", "field", "value"},
                       "OP_FIELDS")
            _refuse_if(op["field"] not in {"title", "icon"}, "PRESENTATION_FIELD_ONLY")
            _refuse_if(not isinstance(op["value"], str) or
                       not 1 <= len(op["value"]) <= 80, "FIELD_VALUE")
            _view(cfg, op["view"])[op["field"]] = op["value"]
    return cfg


def preview(req: D5Request, baseline: dict | None) -> dict:
    """Locally recompute the exact candidate; refuses a stale or unreviewed hash."""
    if req.kind == "patch":
        _refuse_if(not isinstance(baseline, dict), "BASELINE_REQUIRED")
        _refuse_if(dashboards.config_sha256(baseline) != req.expect_config_sha256,
                   "STALE_BASELINE")
        result = _additive_patch(req.plan, baseline, frozenset(req.allowed_controls))
        try:
            dashboard_d5_no_loss.prove_additive(baseline, result)
        except dashboard_d5_no_loss.NoLossViolation as err:
            raise D5Refused(str(err)) from err
        _refuse_if(result == baseline, "NO_CHANGE")
    else:
        _refuse_if(baseline is not None, "CREATE_TARGET_EXISTS")
        op = req.plan["operations"][0]
        _refuse_if(set(op) != {"op", "url_path", "title", "config"}
                   or op["url_path"] != req.dashboard or op["title"] != req.title,
                   "CREATE_PLAN_MISMATCH")
        result = copy.deepcopy(op["config"])
        _refuse_if(not isinstance(result, dict) or set(result) - {"views", "title"},
                   "CREATE_CONFIG_FIELDS")
        _refuse_if(not isinstance(result.get("views"), list) or not result["views"],
                   "CREATE_VIEWS")
        _refuse_if("title" in result and result["title"] != req.title,
                   "CREATE_TITLE_MISMATCH")
        _r2_create_views(result["views"], frozenset(req.allowed_controls))
    _refuse_if(len(_canonical(result)) > MAX_CONFIG_BYTES, "CONFIG_TOO_LARGE")
    _refuse_if(dashboards.config_sha256(result) != req.expect_candidate_sha256,
               "CANDIDATE_SHA_MISMATCH")
    return result
