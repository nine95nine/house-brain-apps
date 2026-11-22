"""Deterministic validators for a dashboard config. Independent of the model: the model never grades itself.

Every finding has a stable reason code and a severity:
* ``BLOCK``  the candidate cannot be offered for approval;
* ``WARN``   offered, with the warning shown to the owner;
* ``INFO``   recorded only.

``origin="generated"`` applies the strict rules (no new control surface, no hallucinated entity, no custom
card that the baseline does not already use). ``origin="baseline"`` audits what is live today: the same
problems become warnings (dashboard debt), never silent fixes.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from . import lovelace as L

BLOCK, WARN, INFO = "BLOCK", "WARN", "INFO"

MAX_CONFIG_BYTES = 3 * 1024 * 1024      # same cap as the Deployer's dashboard operations
MAX_VIEWS = 40
MAX_CARDS = 800
MAX_STRING = 16_000
MAX_DEPTH = 24
VIEW_CARD_BUDGET = 80                   # complexity warning per view
RE_VIEW_PATH = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
RE_SERVICE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")
RE_ICON = re.compile(r"^(mdi|hass|si):[a-z0-9-]+$")
ENTITY_REQUIRED = frozenset({"tile", "entity", "thermostat", "gauge", "sensor", "light", "humidifier",
                             "alarm-panel", "media-control", "picture-entity", "alert", "plant-status",
                             "weather-forecast"})
# Section strategies that auto-populate controls are not allowed in generated candidates: they would put
# whatever matches a filter (including future actuators) on screen with no review.
CONTROL_STRATEGIES = frozenset({"common-controls", "device"})
_RE_BAD_TEXT = re.compile(r"(?i)<\s*script|javascript:|vbscript:|data:text/html|on(?:load|error|click)\s*=")
_RE_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    path: str
    detail: str = ""


@dataclass
class Inventory:
    """What the house has, from the registries. ``None`` fields mean unknown, never 'fine'."""
    entities: dict | None = None          # entity_id -> {"disabled": bool, "available": bool|None, ...}
    dashboards: dict | None = None        # url_path -> [view idents]

    def knows(self) -> bool:
        return self.entities is not None


@dataclass
class Report:
    findings: list
    model: L.Model
    sha256: str
    size: int

    @property
    def verdict(self) -> str:
        return "FAIL" if any(f.severity == BLOCK for f in self.findings) else "PASS"

    def codes(self, severity: str | None = None) -> set:
        return {f.code for f in self.findings if severity is None or f.severity == severity}

    def as_dict(self) -> dict:
        return {"verdict": self.verdict, "config_sha256": self.sha256, "size": self.size,
                "findings": [asdict(f) for f in self.findings]}


def _control_keys(m: L.Model) -> set:
    return {(e, how) for _, e, how in m.controls}


def _action_sig(a: L.ActionUse) -> tuple:
    return (a.action, a.service, a.entity, tuple(sorted(a.target)))


def validate(config, *, dashboard_url: str, inventory: Inventory | None = None, origin: str = "generated",
             baseline=None, allowed_new_controls: frozenset = frozenset(),
             inbound_navigation: frozenset = frozenset(), renames: dict | None = None) -> Report:
    """Validate ``config``. ``baseline`` (a config) is required to judge new control surfaces when generated.

    ``renames`` maps old -> new entity ids whose identity is proven (same platform + unique id); the
    baseline's existing controls are translated through it, so a proven rename is not a new control."""
    rn = renames or {}
    inv = inventory or Inventory()
    strict = origin == "generated"
    sev = BLOCK if strict else WARN
    out: list[Finding] = []
    add = out.append
    try:
        raw = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as e:
        return Report([Finding("CONFIG_NOT_JSON_COMPATIBLE", BLOCK, "", str(e)[:120])], L.walk({}), "", 0)
    size = len(raw.encode("utf-8"))
    if size > MAX_CONFIG_BYTES:
        add(Finding("CONFIG_TOO_LARGE", BLOCK, "", str(size)))
    m = L.walk(config)
    report = Report(out, m, L.config_sha256(config), size)

    # ---- structure
    if not isinstance(config, dict):
        add(Finding("CONFIG_NOT_MAPPING", BLOCK, ""))
        return report
    if "strategy" in config and "views" not in config:
        add(Finding("DASHBOARD_STRATEGY_OPAQUE", sev, "/strategy", "auto-generated dashboard; contents not reviewable"))
        return report
    views = config.get("views")
    if not isinstance(views, list) or not views:
        add(Finding("NO_VIEWS", BLOCK, "/views"))
        return report
    if len(views) > MAX_VIEWS:
        add(Finding("TOO_MANY_VIEWS", BLOCK, "/views", str(len(views))))
    _depth_check(config, "", 0, out)
    seen_paths = {}
    for i, v in enumerate(views):
        vp = f"/views/{i}"
        if not isinstance(v, dict):
            add(Finding("VIEW_NOT_MAPPING", BLOCK, vp))
            continue
        p = v.get("path")
        if p is not None:
            if not isinstance(p, str) or not RE_VIEW_PATH.match(p) or p.isdigit():
                add(Finding("VIEW_PATH_INVALID", BLOCK, vp + "/path", str(p)[:40]))
            elif p in seen_paths:
                add(Finding("VIEW_PATH_DUPLICATE", BLOCK, vp + "/path", p))
            else:
                seen_paths[p] = i
        vt = v.get("type", "sections" if "sections" in v else "masonry")
        if isinstance(vt, str) and vt.startswith("custom:"):
            add(Finding("CUSTOM_VIEW_TYPE", sev, vp + "/type", vt))
        elif vt not in L.VIEW_TYPES:
            add(Finding("UNKNOWN_VIEW_TYPE", BLOCK, vp + "/type", str(vt)[:40]))
        if vt == "sections" and isinstance(v.get("cards"), list) and v["cards"]:
            add(Finding("SECTIONS_VIEW_HAS_CARDS", WARN, vp + "/cards", "cards on a sections view are not shown"))
        if "icon" in v and not (isinstance(v["icon"], str) and RE_ICON.match(v["icon"])):
            add(Finding("ICON_INVALID", WARN, vp + "/icon", str(v["icon"])[:40]))
        cards_here = sum(1 for e in m.elements if e.level == "card" and e.view_index == i)
        if cards_here > VIEW_CARD_BUDGET:
            add(Finding("VIEW_OVER_COMPLEXITY_BUDGET", WARN, vp, str(cards_here)))
    if sum(1 for e in m.elements if e.level == "card") > MAX_CARDS:
        add(Finding("TOO_MANY_CARDS", BLOCK, "/views"))
    for path, level, st in m.strategies:
        if level == "section" and st not in L.SECTION_STRATEGIES:
            add(Finding("UNKNOWN_SECTION_STRATEGY", BLOCK, path, st[:40]))
        elif level == "section" and st in CONTROL_STRATEGIES and strict:
            add(Finding("AUTO_CONTROL_SECTION_REFUSED", BLOCK, path, st))
        elif level == "view" and strict:
            add(Finding("VIEW_STRATEGY_OPAQUE", WARN, path, st[:40]))
    base_model = L.walk(baseline) if baseline is not None else None
    base_custom = {t for _, t in base_model.custom_cards} if base_model else set()
    for path, t in m.custom_cards:
        if strict and t not in base_custom:
            add(Finding("CUSTOM_CARD_NOT_IN_BASELINE", BLOCK, path, t[:60]))
        else:
            add(Finding("CUSTOM_CARD_PRESERVED", INFO, path, t[:60]))
    for path, t in m.unknown_cards:
        add(Finding("UNKNOWN_CARD_TYPE", sev, path, (t or "<missing type>")[:60]))
    for e in m.elements:
        if e.level == "card" and e.type in ENTITY_REQUIRED and not e.entities:
            add(Finding("CARD_MISSING_ENTITY", sev, e.path, e.type))

    # ---- actions
    for a in m.actions:
        if a.action not in L.ALL_ACTIONS:
            add(Finding("UNKNOWN_ACTION", BLOCK, a.path, a.action[:40]))
        elif a.action == "navigate" and not a.navigation_path:
            add(Finding("NAVIGATE_WITHOUT_PATH", BLOCK, a.path))
        elif a.action == "url":
            if not re.match(r"^https?://[^\s\"'<>`]+$", a.url_path or ""):
                add(Finding("UNSAFE_URL", BLOCK, a.path, (a.url_path or "")[:60]))
        elif a.action in ("perform-action", "call-service") and not RE_SERVICE.match(a.service or ""):
            add(Finding("ACTION_SERVICE_INVALID", BLOCK, a.path, (a.service or "")[:60]))
        if a.action == "call-service" and strict:
            add(Finding("DEPRECATED_CALL_SERVICE", WARN, a.path, "use perform-action"))

    # ---- control surfaces: a generated candidate may not add a way to change the house
    if strict:
        if base_model is None:
            add(Finding("BASELINE_REQUIRED", BLOCK, "", "control-surface check needs the baseline"))
        else:
            base_ctl = {(rn.get(e, e), how) for e, how in _control_keys(base_model)}
            base_ctl_entities = {e for e, _ in base_ctl}
            for path, ent, how in m.controls:
                if ent in allowed_new_controls:
                    continue
                if how == "implicit" and (ent, "implicit") not in base_ctl:
                    add(Finding("IMPLICIT_DEFAULT_ACTION", BLOCK, path,
                                f"{ent}: set tap/hold/double-tap (and icon) actions explicitly"))
                elif ent not in base_ctl_entities:
                    add(Finding("NEW_CONTROL_SURFACE", BLOCK, path, ent))
            base_sigs = {(a.action, a.service, rn.get(a.entity, a.entity), tuple(sorted(rn.get(t, t) for t in a.target)))
                         for a in base_model.actions if a.action in L.ACTUATING_ACTIONS}
            for a in m.actions:
                if a.action in L.ACTUATING_ACTIONS and _action_sig(a) not in base_sigs:
                    targets = set(a.target) | ({a.entity} if a.entity else set())
                    if not targets or not targets <= allowed_new_controls:
                        add(Finding("NEW_ACTUATING_ACTION", BLOCK, a.path, f"{a.action} {a.service}".strip()))
                    elif not a.confirmation and any(L.domain(t) in L.SENSITIVE_DOMAINS for t in targets):
                        add(Finding("UNCONFIRMED_SENSITIVE_ACTION", BLOCK, a.path, a.service))
    else:
        for path, ent, how in m.controls:
            if how == "implicit":
                add(Finding("IMPLICIT_DEFAULT_ACTION", WARN, path, ent))

    # ---- entities
    if inv.entities is None:
        if m.refs:
            add(Finding("ENTITIES_UNVERIFIED", WARN, "", "registry inventory unavailable; references not checked"))
    else:
        seen = set()
        for r in m.refs:
            key = (r.entity_id, r.kind)
            if key in seen:
                continue
            seen.add(key)
            info = inv.entities.get(r.entity_id)
            if info is None:
                if r.kind == "structured":
                    add(Finding("HALLUCINATED_ENTITY" if strict else "MISSING_ENTITY", sev, r.path, r.entity_id))
                else:
                    add(Finding("TEMPLATE_ENTITY_UNKNOWN", WARN, r.path, r.entity_id))
            elif info.get("disabled"):
                add(Finding("ENTITY_DISABLED", sev if r.kind == "structured" else WARN, r.path, r.entity_id))
            elif info.get("available") is False:
                add(Finding("ENTITY_UNAVAILABLE", INFO, r.path, r.entity_id))

    # ---- navigation graph
    inbound = set()
    for path, nav in m.navigation:
        status, _ = L.resolve_navigation(nav, dashboard_url, m.views, inv.dashboards)
        if status == "DEAD":
            add(Finding("DEAD_NAVIGATION", sev, path, nav[:80]))
        elif status == "INVALID":
            add(Finding("NAVIGATION_INVALID", BLOCK, path, nav[:80]))
        elif status == "UNKNOWN":
            add(Finding("NAVIGATION_UNVERIFIED", WARN, path, nav[:80]))
        parts = nav.split("?", 1)[0].strip("/").split("/")
        if len(parts) >= 2 and parts[0] == dashboard_url:
            inbound.add(parts[1])
    for v in m.views:
        bp = (config["views"][v.index] or {}).get("back_path") if isinstance(config["views"][v.index], dict) else None
        if isinstance(bp, str):
            st, _ = L.resolve_navigation(bp, dashboard_url, m.views, inv.dashboards)
            if st in ("DEAD", "INVALID"):
                add(Finding("DEAD_BACK_PATH", sev, f"/views/{v.index}/back_path", bp[:80]))
        if v.subview and v.ident not in inbound and str(v.index) not in inbound and v.ident not in inbound_navigation:
            add(Finding("UNREACHABLE_SUBVIEW", sev, f"/views/{v.index}", v.ident))

    # ---- text safety (strings come from a model or a live config; both untrusted)
    _text_check(config, "", out, strict)
    return report


def _depth_check(o, path, d, out):
    if d > MAX_DEPTH:
        out.append(Finding("CONFIG_TOO_DEEP", BLOCK, path))
        return True
    if isinstance(o, dict):
        return any(_depth_check(v, f"{path}/{k}", d + 1, out) for k, v in o.items())
    if isinstance(o, list):
        return any(_depth_check(v, f"{path}/{i}", d + 1, out) for i, v in enumerate(o))
    return False


def _text_check(o, path, out, strict):
    if isinstance(o, dict):
        for k, v in o.items():
            if not isinstance(k, str):
                out.append(Finding("NON_STRING_KEY", BLOCK, path))
                continue
            if strict and k == "type" and v == "iframe":
                out.append(Finding("IFRAME_IN_GENERATED", BLOCK, path, "external content in a generated card"))
            _text_check(v, f"{path}/{k}", out, strict)
    elif isinstance(o, list):
        for i, v in enumerate(o):
            _text_check(v, f"{path}/{i}", out, strict)
    elif isinstance(o, str):
        if len(o) > MAX_STRING:
            out.append(Finding("STRING_TOO_LONG", BLOCK if strict else WARN, path, str(len(o))))
        if _RE_BAD_TEXT.search(o):
            out.append(Finding("ACTIVE_CONTENT_REFUSED", BLOCK, path, _RE_BAD_TEXT.search(o).group(0)))
        if _RE_CTRL.search(o):
            out.append(Finding("CONTROL_CHARACTER", BLOCK if strict else WARN, path))
        if strict and ("{{" in o or "{%" in o):
            out.append(Finding("GENERATED_TEMPLATE", WARN, path, "template text: rendered read-only, review it"))
