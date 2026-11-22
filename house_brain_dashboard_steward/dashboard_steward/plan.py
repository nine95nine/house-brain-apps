"""Typed change plan: the closed set of edits Dashboard Steward may propose, applied as the smallest patch.

The model writes a plan (YAML) whose operations carry real Lovelace YAML for the cards, sections and views
it composes. ``apply`` turns it into a full candidate config against the exact baseline, and ``diff`` lists
every changed path. A plan never says "replace the dashboard"; a brand-new dashboard is its own operation
(``new_dashboard``) that cannot touch an existing one.

Operations (closed set; anything else is refused):
  add_card        {view, section?, position?, card}
  add_section     {view, position?, section}
  add_view        {view_config, position?}
  replace_card    {view, section?, card_index, card}
  remove_card     {view, section?, card_index}          (NDEC still requires an approved removal)
  repoint_entity  {old, new}                            (entity-reference repair; whole-name only)
  set_view_field  {view, field, value}                  (title, icon, subview, back_path, visible)
  new_dashboard   {url_path, title, config}             (only when the plan targets no existing dashboard)
"""
from __future__ import annotations

import copy
import re

from . import lovelace as L

PLAN_SCHEMA = "dashboard_steward_change_plan.v1"
OPS = frozenset({"add_card", "add_section", "add_view", "replace_card", "remove_card", "repoint_entity",
                 "set_view_field", "new_dashboard"})
VIEW_FIELDS = frozenset({"title", "icon", "subview", "back_path", "visible"})
MAX_OPS = 40
RE_DASH_URL = re.compile(r"^[a-z0-9]+-[a-z0-9-]{1,60}$")   # HA requires a hyphen in a dashboard url


class PlanRefused(ValueError):
    def __init__(self, code: str, detail: str = ""):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}")


def _view(config, ident):
    for i, v in enumerate(config.get("views") or []):
        if isinstance(v, dict) and (v.get("path") == ident or str(i) == str(ident)):
            return v
    raise PlanRefused("PLAN_VIEW_NOT_FOUND", str(ident))


def _container(view, section):
    if section is None:
        if "sections" in view and "cards" not in view:
            raise PlanRefused("PLAN_SECTION_REQUIRED", "sections view: name a section")
        return view.setdefault("cards", [])
    secs = view.get("sections")
    if not isinstance(secs, list) or not isinstance(section, int) or not 0 <= section < len(secs):
        raise PlanRefused("PLAN_SECTION_NOT_FOUND", str(section))
    if "strategy" in secs[section]:
        raise PlanRefused("PLAN_STRATEGY_SECTION_IMMUTABLE", str(section))
    return secs[section].setdefault("cards", [])


def _pos(lst, position):
    if position in (None, "end"):
        return len(lst)
    if isinstance(position, int) and 0 <= position <= len(lst):
        return position
    raise PlanRefused("PLAN_POSITION_INVALID", str(position))


def _repoint(o, old, new):
    rx = re.compile(r"(?<![a-z0-9_.])" + re.escape(old) + r"(?![a-z0-9_])")
    count = 0

    def rec(x):
        nonlocal count
        if isinstance(x, dict):
            return {k: rec(v) for k, v in x.items()}
        if isinstance(x, list):
            return [rec(v) for v in x]
        if isinstance(x, str):
            y, n = rx.subn(new, x)
            count += n
            return y
        return x
    return rec(o), count


def check_shape(plan) -> None:
    if not isinstance(plan, dict) or plan.get("schema") != PLAN_SCHEMA:
        raise PlanRefused("PLAN_SCHEMA_INVALID", "schema must be " + PLAN_SCHEMA)
    ops = plan.get("operations")
    if not isinstance(ops, list) or not ops:
        raise PlanRefused("PLAN_EMPTY")
    if len(ops) > MAX_OPS:
        raise PlanRefused("PLAN_TOO_MANY_OPERATIONS", str(len(ops)))
    for op in ops:
        if not isinstance(op, dict) or op.get("op") not in OPS:
            raise PlanRefused("PLAN_OPERATION_UNKNOWN", str(op.get("op") if isinstance(op, dict) else op)[:40])
    if any(op["op"] == "new_dashboard" for op in ops) and len(ops) != 1:
        raise PlanRefused("PLAN_NEW_DASHBOARD_ALONE", "a new dashboard is its own plan")


def apply(plan: dict, baseline):
    """Return (candidate_config, repoint_counts). The baseline is never modified."""
    check_shape(plan)
    ops = plan["operations"]
    if ops[0]["op"] == "new_dashboard":
        op = ops[0]
        if baseline is not None:
            raise PlanRefused("PLAN_NEW_DASHBOARD_HAS_BASELINE", "new_dashboard cannot target an existing dashboard")
        if not isinstance(op.get("url_path"), str) or not RE_DASH_URL.match(op["url_path"]):
            raise PlanRefused("PLAN_DASHBOARD_URL_INVALID", str(op.get("url_path"))[:40])
        if not isinstance(op.get("config"), dict):
            raise PlanRefused("PLAN_CONFIG_INVALID")
        return copy.deepcopy(op["config"]), {}
    if not isinstance(baseline, dict):
        raise PlanRefused("PLAN_BASELINE_REQUIRED")
    cfg = copy.deepcopy(baseline)
    counts = {}
    for op in ops:
        kind = op["op"]
        if kind in ("add_card", "replace_card", "remove_card"):
            lst = _container(_view(cfg, op.get("view")), op.get("section"))
            if kind == "add_card":
                if not isinstance(op.get("card"), dict):
                    raise PlanRefused("PLAN_CARD_INVALID")
                lst.insert(_pos(lst, op.get("position")), copy.deepcopy(op["card"]))
            else:
                i = op.get("card_index")
                if not isinstance(i, int) or not 0 <= i < len(lst):
                    raise PlanRefused("PLAN_CARD_NOT_FOUND", str(i))
                if kind == "replace_card":
                    if not isinstance(op.get("card"), dict):
                        raise PlanRefused("PLAN_CARD_INVALID")
                    lst[i] = copy.deepcopy(op["card"])
                else:
                    del lst[i]
        elif kind == "add_section":
            v = _view(cfg, op.get("view"))
            if not isinstance(op.get("section"), dict):
                raise PlanRefused("PLAN_SECTION_INVALID")
            if "cards" in v and "sections" not in v:
                raise PlanRefused("PLAN_NOT_A_SECTIONS_VIEW", str(op.get("view")))
            secs = v.setdefault("sections", [])
            secs.insert(_pos(secs, op.get("position")), copy.deepcopy(op["section"]))
        elif kind == "add_view":
            vc = op.get("view_config")
            if not isinstance(vc, dict):
                raise PlanRefused("PLAN_VIEW_INVALID")
            views = cfg.setdefault("views", [])
            views.insert(_pos(views, op.get("position")), copy.deepcopy(vc))
        elif kind == "set_view_field":
            if op.get("field") not in VIEW_FIELDS:
                raise PlanRefused("PLAN_VIEW_FIELD_REFUSED", str(op.get("field"))[:40])
            _view(cfg, op.get("view"))[op["field"]] = copy.deepcopy(op.get("value"))
        elif kind == "repoint_entity":
            old, new = op.get("old"), op.get("new")
            if not all(isinstance(x, str) and L.RE_ENTITY_ID.match(x) for x in (old, new)) or old == new:
                raise PlanRefused("PLAN_REPOINT_INVALID", f"{old}->{new}")
            if L.domain(old) != L.domain(new):
                raise PlanRefused("PLAN_REPOINT_DOMAIN_CHANGE", f"{old}->{new}")
            cfg, n = _repoint(cfg, old, new)
            if n == 0:
                raise PlanRefused("PLAN_REPOINT_NO_OCCURRENCE", old)
            counts[old] = n
        else:  # new_dashboard mixed with others is refused by check_shape
            raise PlanRefused("PLAN_OPERATION_UNKNOWN", kind)
    return cfg, counts


def diff(a, b, path="") -> list:
    """Structural diff: [(change, path)] where change is added | removed | changed. Lists compare by index."""
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in a:
            if k not in b:
                out.append(("removed", f"{path}/{k}"))
            else:
                out += diff(a[k], b[k], f"{path}/{k}")
        for k in b:
            if k not in a:
                out.append(("added", f"{path}/{k}"))
    elif isinstance(a, list) and isinstance(b, list):
        for i in range(max(len(a), len(b))):
            if i >= len(a):
                out.append(("added", f"{path}/{i}"))
            elif i >= len(b):
                out.append(("removed", f"{path}/{i}"))
            else:
                out += diff(a[i], b[i], f"{path}/{i}")
    elif a != b or type(a) is not type(b):
        out.append(("changed", path))
    return out
