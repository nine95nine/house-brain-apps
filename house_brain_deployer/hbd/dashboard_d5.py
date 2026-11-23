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

0.3.52 (deep dive HOME_CENTER_MAIN_DASHBOARD_DEEP_DIVE_2026-11-22, findings A2-A4):
- ``house_brain_dashboard_replace_request.v1``: replace the WHOLE config of a
  dashboard that D5 itself created. A separate schema, not a create option,
  so create keeps its absolute "target must not exist" rule. The request
  names the D5 request (``provenance_request_id``) whose recorded result hash
  must equal the live hash (the Deployer proves this from its own ledger and
  the request file, see dashboard_d5_owned); an owner edit in the UI changes
  the hash and the request is refused. The new config passes the same closed
  R2 guard as a create.
- ``house_brain_dashboard_transplant_request.v1``: copy whole sections/cards
  byte-identically from a source dashboard (bound to its live hash) to the
  end of a D5-created target. Owner decision 2026-11-22 (pop-up "A: copy
  as-is"): the copied subtrees keep templates/custom cards exactly as they
  are; they are not generated content. Every house-changing entity in the
  copy must be declared in ``expect_control_entities`` and is listed on the
  owner's phone.
- A4: an ``entities``-card row whose entity can change the house must say
  ``type: simple-entity`` or ``attribute`` (Home Assistant frontend
  create-row-element.ts DOMAIN_TO_ELEMENT_TYPE renders a typeless row as a
  live toggle/slider), and such a card must set ``show_header_toggle: false``
  (hui-entities-card.ts computeShowHeaderToggle shows a toggle-all switch
  when a titled card holds two toggleable entities, whatever the row type).
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
REPLACE_SCHEMA = "house_brain_dashboard_replace_request.v1"        # 0.3.52
TRANSPLANT_SCHEMA = "house_brain_dashboard_transplant_request.v1"  # 0.3.52
PROMOTE_SCHEMA = "house_brain_dashboard_promote_request.v1"        # 0.3.53
ADOPT_SCHEMA = "house_brain_dashboard_adopt_request.v1"            # 0.3.53
SCHEMA_KINDS = {PATCH_SCHEMA: "patch", CREATE_SCHEMA: "create",
                REPLACE_SCHEMA: "replace", TRANSPLANT_SCHEMA: "transplant",
                PROMOTE_SCHEMA: "promote", ADOPT_SCHEMA: "adopt"}
# Kinds that rewrite an existing storage dashboard (before/after hash, Undo copy).
REWRITE_KINDS = frozenset({"patch", "replace", "transplant", "promote"})
# Kinds allowed only on a dashboard D5 itself created (provenance-bound). Adopt writes nothing.
OWNED_KINDS = frozenset({"replace", "transplant", "promote", "adopt"})
PLAN_SCHEMA = "dashboard_steward_change_plan.v1"
BINDING_SCOPE = "DASHBOARD_PRESENTATION_ONLY"
MAX_REQUEST_BYTES = 128 * 1024
MAX_OPERATIONS = 40
MAX_CONFIG_BYTES = 3 * 1024 * 1024
RE_SHA = re.compile(r"^[0-9a-f]{64}$")
RE_URL = re.compile(r"^[a-z0-9]+-[a-z0-9-]{1,60}$")
RE_PROPOSAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,100}$")
RE_VIEW_PATH = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
RE_SOURCE_URL = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")   # any storage/YAML dashboard address (not the default)
MAX_TRANSPLANT_CONTROLS = 200
# Domains read when listing the entities a transplant copies (strings, templates included).
READ_DOMAINS = frozenset({
    "sensor", "binary_sensor", "weather", "person", "sun", "zone", "device_tracker", "camera", "image",
    "calendar", "todo", "event", "air_quality", "geo_location", "alert", "conversation", "tts", "stt",
    "image_processing", "notify", "wake_word", "ai_task", "assist_satellite", "plant", "proximity"})
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
    provenance_request_id: str | None = None        # 0.3.52 replace/transplant
    source_dashboard: str | None = None              # 0.3.52 transplant
    expect_source_config_sha256: str | None = None   # 0.3.52 transplant
    expect_controls: tuple = ()                      # 0.3.52 transplant
    source_provenance_request_id: str | None = None  # 0.3.53 promote
    expect_added_controls: tuple = ()                # 0.3.53 promote
    expect_removed_controls: tuple = ()              # 0.3.53 promote
    rebase_navigation: bool = False                  # 0.3.53 promote: /<source>/ -> /<target>/ in nav paths


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
    return isinstance(data, dict) and data.get("schema") in SCHEMA_KINDS


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
    _refuse_if(schema not in SCHEMA_KINDS, "SCHEMA_REFUSED")
    kind = SCHEMA_KINDS[schema]
    fields = {"schema", "request_id", "requested_by", "tracking_issue",
              "dashboard", "proposal_id", "expect_candidate_sha256",
              "plan", "owner_binding"}
    if kind in REWRITE_KINDS or kind == "adopt":
        fields.add("expect_config_sha256")
    if kind == "create":
        fields.add("title")
    if kind in OWNED_KINDS:
        fields.add("provenance_request_id")
    if kind == "promote":
        fields |= {"source_dashboard", "source_provenance_request_id", "expect_source_config_sha256",
                   "expect_added_controls", "expect_removed_controls"}
    if kind == "transplant":
        fields |= {"source_dashboard", "expect_source_config_sha256", "expect_control_entities"}
    if kind in ("transplant", "promote", "adopt"):
        # Copied/adopted controls are declared and counted, never allowed by a separate list.
        _refuse_if(set(data) - ({"rebase_navigation"} if kind == "promote" else set()) != fields,
                   "REQUEST_FIELDS")
    else:
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
    if kind in REWRITE_KINDS or kind == "adopt":
        _refuse_if(not isinstance(baseline, str) or not RE_SHA.fullmatch(baseline),
                   "BASELINE_SHA")
    provenance = data.get("provenance_request_id")
    if kind in OWNED_KINDS:
        _refuse_if(not isinstance(provenance, str) or not RE_REQUEST_ID.fullmatch(provenance)
                   or provenance == rid, "PROVENANCE_REQUEST_ID")
    source = source_sha = None
    controls = ()
    if kind == "transplant":
        source = data["source_dashboard"]
        _refuse_if(not isinstance(source, str) or not RE_SOURCE_URL.fullmatch(source)
                   or source == dashboard, "SOURCE_DASHBOARD")
        source_sha = data["expect_source_config_sha256"]
        _refuse_if(not isinstance(source_sha, str) or not RE_SHA.fullmatch(source_sha), "SOURCE_SHA")
        declared = data["expect_control_entities"]
        _refuse_if(not isinstance(declared, list) or len(declared) > MAX_TRANSPLANT_CONTROLS
                   or len(set(map(str, declared))) != len(declared)
                   or not all(isinstance(e, str) and RE_ENTITY.fullmatch(e)
                              and e.split(".", 1)[0] in ACTUATOR_DOMAINS for e in declared),
                   "EXPECT_CONTROL_ENTITIES")
        controls = tuple(sorted(declared))
    source_prov = None
    added = removed = ()
    if kind == "promote":
        source = data["source_dashboard"]
        _refuse_if(not isinstance(source, str) or not RE_URL.fullmatch(source)
                   or source == dashboard, "SOURCE_DASHBOARD")
        source_prov = data["source_provenance_request_id"]
        _refuse_if(not isinstance(source_prov, str) or not RE_REQUEST_ID.fullmatch(source_prov)
                   or source_prov in (rid, provenance), "SOURCE_PROVENANCE_REQUEST_ID")
        source_sha = data["expect_source_config_sha256"]
        _refuse_if(not isinstance(source_sha, str) or not RE_SHA.fullmatch(source_sha), "SOURCE_SHA")
        rebase = data.get("rebase_navigation")
        if rebase is not None:
            # 0.3.53 (R3 worker finding): the test dashboard's own navigation points at itself. The ONLY
            # allowed rewrite maps "/<source>/" to "/<target>/" in navigation_path/back_path values.
            _refuse_if(rebase != {"from": f"/{source}/", "to": f"/{dashboard}/"}, "REBASE_NAVIGATION")
        else:
            # The result IS the source, byte for byte.
            _refuse_if(source_sha != expected, "PROMOTE_CANDIDATE_IS_SOURCE")
        lists = []
        for key in ("expect_added_controls", "expect_removed_controls"):
            got = data[key]
            _refuse_if(not isinstance(got, list) or len(got) > MAX_TRANSPLANT_CONTROLS
                       or len(set(map(str, got))) != len(got)
                       or not all(isinstance(e, str) and RE_ENTITY.fullmatch(e)
                                  and e.split(".", 1)[0] in ACTUATOR_DOMAINS for e in got),
                       "EXPECT_CONTROL_DIFF")
            lists.append(tuple(sorted(got)))
        added, removed = lists
    if kind == "adopt":
        # Adopt writes nothing: the "candidate" is the live config being adopted.
        _refuse_if(expected != baseline, "ADOPT_CANDIDATE_IS_LIVE")
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
    kinds = {"patch": {"add_card", "add_section", "add_view", "set_view_field"},
             "create": {"new_dashboard"}, "replace": {"replace_config"},
             "transplant": {"copy_section", "copy_card"},
             "promote": {"promote"}, "adopt": {"adopt"}}[kind]
    for op in ops:
        _refuse_if(not isinstance(op, dict) or op.get("op") not in kinds,
                   "PLAN_KIND_REFUSED")
    _refuse_if(kind == "create" and len(ops) != 1, "CREATE_ONE_OPERATION")
    _refuse_if(kind == "replace" and len(ops) != 1, "REPLACE_ONE_OPERATION")
    _refuse_if(kind in ("promote", "adopt") and (len(ops) != 1 or set(ops[0]) != {"op"}),
               "ONE_BARE_OPERATION")
    return D5Request(kind, rid, issue, who, dashboard, title, proposal,
                     baseline, expected, copy.deepcopy(plan), hashlib.sha256(raw).hexdigest(),
                     tuple(sorted(allowed)), provenance, source, source_sha, controls,
                     source_prov, added, removed, kind == "promote" and data.get("rebase_navigation") is not None)


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
        rows = card.get("entities")
        _refuse_if(rows is not None and not isinstance(rows, list), "R2_ENTITIES_ROW")
        control_rows = False
        for row in rows or []:
            if isinstance(row, dict):
                rtype = row.get("type", "simple-entity")
                _refuse_if(bool(set(row) - {"entity", "name", "type", "icon", "attribute"})
                           or rtype not in ("simple-entity", "attribute")
                           or ("attribute" in row) != (rtype == "attribute")
                           or ("attribute" in row and (not isinstance(row["attribute"], str)
                                                       or not 1 <= len(row["attribute"]) <= 64)),
                           "R2_ENTITIES_ROW")
                ent = row.get("entity")
                if isinstance(ent, str) and ent.split(".", 1)[0] in ACTUATOR_DOMAINS:
                    control_rows = True
                    # 0.3.52 A4: no type = the domain's default row (toggle/slider/run button).
                    _refuse_if(row.get("type") not in ("simple-entity", "attribute"), "R2_CONTROL_ROW_TYPE")
            elif isinstance(row, str) and row.split(".", 1)[0] in ACTUATOR_DOMAINS:
                raise D5Refused("R2_INLINE_CONTROL_ROW")   # a plain row renders a toggle
            elif not isinstance(row, str):
                raise D5Refused("R2_ENTITIES_ROW")
        # 0.3.52: the header toggle-all switch acts on every row's entity, whatever the row type.
        # Frontend computeShowHeaderToggle: shown when show_header_toggle is true, or when the card
        # has a title and show_header_toggle is unset (and two rows can toggle). Fail closed on both.
        _refuse_if("show_header_toggle" in card and type(card["show_header_toggle"]) is not bool, "R2_HEADER_TOGGLE")
        _refuse_if(control_rows and (card.get("show_header_toggle") is True
                                     or ("title" in card and card.get("show_header_toggle") is not False)),
                   "R2_HEADER_TOGGLE")


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


def _validated_full_config(result, req: D5Request, *, title: str | None) -> None:
    """A whole new config (create or replace) passes the closed R2 guard."""
    _refuse_if(not isinstance(result, dict) or set(result) - {"views", "title"},
               "CREATE_CONFIG_FIELDS")
    _refuse_if(not isinstance(result.get("views"), list) or not result["views"],
               "CREATE_VIEWS")
    if title is not None:
        _refuse_if("title" in result and result["title"] != title, "CREATE_TITLE_MISMATCH")
    elif "title" in result:
        _refuse_if(not isinstance(result["title"], str) or not 1 <= len(result["title"]) <= 80
                   or any(ord(x) < 32 for x in result["title"]), "TITLE")
    _r2_create_views(result["views"], frozenset(req.allowed_controls))


# ---------------------------------------------------------------- 0.3.52 transplant (verbatim copy)
def _source_view(config: dict, ident):
    """Read-only view lookup (never setdefault on the source)."""
    _refuse_if(not isinstance(ident, str) or not 1 <= len(ident) <= 64, "TRANSPLANT_POINTER")
    views = config.get("views")
    _refuse_if(not isinstance(views, list), "SOURCE_VIEWS_SHAPE")
    for i, row in enumerate(views if isinstance(views, list) else []):
        if isinstance(row, dict) and (row.get("path") == ident or str(i) == ident):
            return i, row
    raise D5Refused("SOURCE_VIEW_NOT_FOUND")


def _index(value, items, code: str) -> int:
    _refuse_if(type(value) is not int or not isinstance(items, list) or not 0 <= value < len(items), code)
    return value


def _transplant(plan: dict, before, source):
    """Append verbatim copies; return the candidate and [(destination pointer, source subtree)]."""
    cfg = copy.deepcopy(before)
    copied: list = []
    seen: set = set()
    key: tuple
    pointer: tuple
    for op in plan["operations"]:
        kind = op["op"]
        if kind == "copy_section":
            _refuse_if(set(op) != {"op", "source_view", "source_section", "target_view"}, "OP_FIELDS")
            _, sview = _source_view(source, op["source_view"])
            secs = sview.get("sections")
            si = _index(op["source_section"], secs, "SOURCE_SECTION_NOT_FOUND")
            key = (kind, op["source_view"], si)
            subtree = secs[si]
            _refuse_if(not isinstance(subtree, dict), "SOURCE_SECTION_NOT_FOUND")
            ti, tview = _source_view(cfg, op["target_view"])
            tsecs = tview.get("sections")
            _refuse_if(not isinstance(tsecs, list) or "cards" in tview, "NOT_A_SECTION_VIEW")
            tsecs.append(copy.deepcopy(subtree))
            pointer = ("views", ti, "sections", len(tsecs) - 1)
        elif kind == "copy_card":
            _refuse_if(set(op) - {"source_section", "target_section"}
                   != {"op", "source_view", "source_card", "target_view"}, "OP_FIELDS")
            _, sview = _source_view(source, op["source_view"])
            if op.get("source_section") is None:
                _refuse_if("source_section" in op or "sections" in sview, "SOURCE_SECTION_REQUIRED")
                cards = sview.get("cards")
            else:
                secs = sview.get("sections")
                si = _index(op["source_section"], secs, "SOURCE_SECTION_NOT_FOUND")
                _refuse_if(not isinstance(secs[si], dict), "SOURCE_SECTION_NOT_FOUND")
                cards = secs[si].get("cards")
            ci = _index(op["source_card"], cards, "SOURCE_CARD_NOT_FOUND")
            key = (kind, op["source_view"], op.get("source_section"), ci)
            subtree = cards[ci]
            _refuse_if(not isinstance(subtree, dict), "SOURCE_CARD_NOT_FOUND")
            ti, tview = _source_view(cfg, op["target_view"])
            tsec = op.get("target_section")
            _refuse_if("target_section" in op and tsec is None, "OP_FIELDS")
            target_cards = _cards(tview, tsec)
            target_cards.append(copy.deepcopy(subtree))
            pointer = (("views", ti, "cards", len(target_cards) - 1) if tsec is None
                       else ("views", ti, "sections", tsec, "cards", len(target_cards) - 1))
        else:   # parse admits only the two kinds above
            raise D5Refused("PLAN_KIND_REFUSED")
        _refuse_if(key in seen, "TRANSPLANT_DUPLICATE")
        seen.add(key)
        copied.append((pointer, copy.deepcopy(subtree)))
    return cfg, copied


def at_pointer(config, pointer):
    node = config
    for step in pointer:
        if isinstance(step, int):
            _refuse_if(not isinstance(node, list) or not 0 <= step < len(node), "POINTER_MISSING")
        else:
            _refuse_if(not isinstance(node, dict) or step not in node, "POINTER_MISSING")
        node = node[step]
    return node


RE_ENTITY_IN_TEXT = re.compile(r"(?<![a-z0-9_.])([a-z_]{2,32})\.([a-z0-9_]{1,96})(?![a-z0-9_])")


def inventory(subtrees) -> dict:
    """What a verbatim copy carries: entity ids, house-changing ids, actions, templates/custom cards.

    Conservative: every string is scanned (templates included), so an entity
    named only inside a template is still listed.
    """
    entities, actions = set(), set()
    flags = {"templates": False, "custom_cards": False}
    known = ACTUATOR_DOMAINS | READ_DOMAINS

    def text(value: str) -> None:
        if "{{" in value or "{%" in value:
            flags["templates"] = True
        for m in RE_ENTITY_IN_TEXT.finditer(value):
            if m.group(1) in known:
                entities.add(m.group(1) + "." + m.group(2))

    def walk(node, depth=0):
        _refuse_if(depth > 48, "DEPTH")
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str):
                    text(k)
                if k == "type" and isinstance(v, str) and v.startswith("custom:"):
                    flags["custom_cards"] = True
                if k in ("service", "perform_action", "action") and isinstance(v, str):
                    if "." in v:
                        actions.add(v[:96])   # an action name, not an entity id
                    if "{{" in v or "{%" in v:
                        flags["templates"] = True
                    continue
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)
        elif isinstance(node, str):
            text(node)
    for tree in subtrees:
        walk(tree)
    controls = {e for e in entities if e.split(".", 1)[0] in ACTUATOR_DOMAINS}
    return {"entities": sorted(entities), "controls": sorted(controls), "actions": sorted(actions),
            "templates": flags["templates"], "custom_cards": flags["custom_cards"]}


NAV_KEYS = ("navigation_path", "back_path")


def rebase_navigation(config, old: str, new: str):
    """0.3.53: rewrite navigation_path/back_path values under ``old`` ("/<source>/", or exactly "/<source>")
    to ``new``. Returns (config, rewritten count, other mentions of the old address left unchanged)."""
    count = [0, 0]
    bare_old, bare_new = old.rstrip("/"), new.rstrip("/")

    def fix(node, depth=0):
        _refuse_if(depth > 48, "DEPTH")
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k in NAV_KEYS and isinstance(v, str) and (v.startswith(old) or v == bare_old):
                    count[0] += 1
                    out[k] = new + v[len(old):] if v.startswith(old) else bare_new
                else:
                    out[k] = fix(v, depth + 1)
            return out
        if isinstance(node, list):
            return [fix(v, depth + 1) for v in node]
        if isinstance(node, str) and bare_old in node:
            count[1] += 1
        return node
    return fix(config), count[0], count[1]


def promote_result(req: D5Request, source: dict) -> tuple[dict, int, int]:
    """The exact config promote writes: the source, with the declared navigation rebase only."""
    if not req.rebase_navigation:
        return copy.deepcopy(source), 0, 0
    old, new = f"/{req.source_dashboard}/", f"/{req.dashboard}/"
    result, n, other = rebase_navigation(source, old, new)
    # Reversible and exact: mapping back must give the source byte for byte (refuses a source that
    # already pointed at the target, where the rewrite would not be one-to-one).
    back, _n, _o = rebase_navigation(result, new, old)
    _refuse_if(back != source, "REBASE_AMBIGUOUS")
    _refuse_if(n == 0, "REBASE_NOTHING")
    return result, n, other


def control_diff(before, after) -> dict:
    """0.3.53 promote: house-changing entities and actions the new config adds or removes."""
    old, new = inventory([before]), inventory([after])
    return {"added": sorted(set(new["controls"]) - set(old["controls"])),
            "removed": sorted(set(old["controls"]) - set(new["controls"])),
            "actions_added": sorted(set(new["actions"]) - set(old["actions"])),
            "templates": new["templates"], "custom_cards": new["custom_cards"]}


def preview(req: D5Request, baseline: dict | None, source: dict | None = None) -> dict:
    """Locally recompute the exact candidate; refuses a stale or unreviewed hash."""
    if req.kind == "promote":
        # 0.3.53: the target becomes the D5-owned source byte for byte. Every byte of the source was
        # either R2-guarded (create/replace) or owner-tapped (transplant/adopt), so nothing new is admitted;
        # the tap shows the control difference (dashboard_d5_owned proves both ownerships).
        _refuse_if(not isinstance(baseline, dict), "BASELINE_REQUIRED")
        _refuse_if(dashboards.config_sha256(baseline) != req.expect_config_sha256,
                   "D5_OWNED_CHANGED_SINCE_LAST_WRITE")
        _refuse_if(not isinstance(source, dict), "SOURCE_REQUIRED")
        _refuse_if(dashboards.config_sha256(source) != req.expect_source_config_sha256, "SOURCE_CHANGED")
        result = promote_result(req, source)[0] if isinstance(source, dict) else {}
        _refuse_if(result == baseline, "NO_CHANGE")
        diff = control_diff(baseline, result)
        _refuse_if(tuple(diff["added"]) != req.expect_added_controls
                   or tuple(diff["removed"]) != req.expect_removed_controls, "PROMOTE_CONTROLS_MISMATCH")
    elif req.kind == "adopt":
        # 0.3.53: no write. Binds the live config (an owner edit) as D5's own after the owner's tap.
        _refuse_if(not isinstance(baseline, dict), "BASELINE_REQUIRED")
        _refuse_if(dashboards.config_sha256(baseline) != req.expect_config_sha256, "ADOPT_LIVE_CHANGED")
        result = copy.deepcopy(baseline) if isinstance(baseline, dict) else {}
    elif req.kind in ("patch", "transplant"):
        _refuse_if(not isinstance(baseline, dict), "BASELINE_REQUIRED")
        _refuse_if(dashboards.config_sha256(baseline) != req.expect_config_sha256,
                   "STALE_BASELINE" if req.kind == "patch" else "D5_OWNED_CHANGED_SINCE_LAST_WRITE")
        if req.kind == "patch":
            result = _additive_patch(req.plan, baseline, frozenset(req.allowed_controls))
        else:
            _refuse_if(not isinstance(source, dict), "SOURCE_REQUIRED")
            _refuse_if(dashboards.config_sha256(source) != req.expect_source_config_sha256,
                       "SOURCE_CHANGED")
            result, copied = _transplant(req.plan, baseline, source)
            inv = inventory([tree for _p, tree in copied])
            _refuse_if(tuple(inv["controls"]) != req.expect_controls, "TRANSPLANT_CONTROLS_MISMATCH")
        try:
            dashboard_d5_no_loss.prove_additive(baseline, result)
        except dashboard_d5_no_loss.NoLossViolation as err:
            raise D5Refused(str(err)) from err
        _refuse_if(result == baseline, "NO_CHANGE")
    elif req.kind == "replace":
        _refuse_if(not isinstance(baseline, dict), "BASELINE_REQUIRED")
        _refuse_if(dashboards.config_sha256(baseline) != req.expect_config_sha256,
                   "D5_OWNED_CHANGED_SINCE_LAST_WRITE")
        op = req.plan["operations"][0]
        _refuse_if(set(op) != {"op", "config"}, "OP_FIELDS")
        result = copy.deepcopy(op["config"])
        _validated_full_config(result, req, title=None)
        _refuse_if(result == baseline, "NO_CHANGE")
    else:
        _refuse_if(baseline is not None, "CREATE_TARGET_EXISTS")
        op = req.plan["operations"][0]
        _refuse_if(set(op) != {"op", "url_path", "title", "config"}
                   or op["url_path"] != req.dashboard or op["title"] != req.title,
                   "CREATE_PLAN_MISMATCH")
        result = copy.deepcopy(op["config"])
        _validated_full_config(result, req, title=req.title)
    _refuse_if(len(_canonical(result)) > MAX_CONFIG_BYTES, "CONFIG_TOO_LARGE")
    _refuse_if(dashboards.config_sha256(result) != req.expect_candidate_sha256,
               "CANDIDATE_SHA_MISMATCH")
    return result


def transplant_copies(req: D5Request, baseline, source):
    """The (pointer, subtree) list for read-back; only after preview admitted the request."""
    return _transplant(req.plan, baseline, source)[1]
