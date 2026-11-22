"""Lovelace structure model: walk a dashboard config into elements, entity references, actions and navigation.

Sources for the shapes used here (verified 2026-11-08 against home-assistant/frontend ``dev``):
``src/data/lovelace/config/{view,section,action}.ts``, ``src/panels/lovelace/cards/types.ts``,
``hui-tile-card.ts``, ``hui-button-card.ts``, ``src/common/const.ts`` (``DOMAINS_TOGGLE``),
``strategies/get-strategy.ts`` (section strategies ``common-controls``, ``device``, ``entities``).

Defaults are never trusted: the button card's default tap action is ``toggle`` for ``DOMAINS_TOGGLE``
entities, and the tile card's icon default has changed between frontend releases. So a card that shows an
actuator entity without explicit actions is reported as ``implicit`` control, whatever this frontend does.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

# --- domains -----------------------------------------------------------------------------------------------
# Entities whose more-info dialog or card can change something in the house.
ACTUATOR_DOMAINS = frozenset({
    "alarm_control_panel", "automation", "button", "climate", "cover", "date", "datetime", "fan", "group",
    "humidifier", "input_boolean", "input_button", "input_datetime", "input_number", "input_select", "input_text",
    "lawn_mower", "light", "lock", "media_player", "number", "remote", "scene", "script", "select", "siren",
    "switch", "text", "time", "timer", "update", "vacuum", "valve", "water_heater", "counter",
})
# Physical equipment, safety or access: a new surface for these needs the separate physical-control gate.
SENSITIVE_DOMAINS = frozenset({
    "alarm_control_panel", "climate", "cover", "fan", "humidifier", "lawn_mower", "light", "lock", "number",
    "select", "siren", "switch", "vacuum", "valve", "water_heater", "button", "script", "scene", "automation",
    "update", "remote", "media_player",
})
# Frontend DOMAINS_TOGGLE (src/common/const.ts): button-card default tap is toggle for these.
DOMAINS_TOGGLE = frozenset({"fan", "input_boolean", "light", "switch", "group", "automation", "humidifier", "valve"})
READ_DOMAINS = frozenset({"sensor", "binary_sensor", "weather", "sun", "person", "device_tracker", "zone", "event",
                          "image", "camera", "calendar", "todo", "conversation", "air_quality", "geo_location",
                          "image_processing", "stt", "tts", "wake_word", "assist_satellite", "ai_task", "notify",
                          "tag"})
KNOWN_DOMAINS = ACTUATOR_DOMAINS | READ_DOMAINS

RE_ENTITY_ID = re.compile(r"^[a-z][a-z0-9_]*\.[a-z0-9_]+$")
_RE_TEMPLATE_ENTITY = re.compile(r"(?<![a-z0-9_.])([a-z][a-z0-9_]*\.[a-z0-9_]*[a-z0-9])(?![a-z0-9_(])")

# --- cards -------------------------------------------------------------------------------------------------
# Native card types (frontend dev, 2026-11). Anything else that is not ``custom:`` is unknown.
NATIVE_CARDS = frozenset({
    "alarm-panel", "area", "button", "calendar", "clock", "conditional", "distribution", "entities", "entity",
    "entity-filter", "gauge", "glance", "grid", "heading", "history-graph", "horizontal-stack", "humidifier",
    "iframe", "light", "logbook", "map", "markdown", "media-control", "picture", "picture-elements",
    "picture-entity", "picture-glance", "plant-status", "sensor", "shopping-list", "statistic", "statistics-graph",
    "thermostat", "tile", "todo-list", "vertical-stack", "weather-forecast", "energy-date-selection",
    "energy-usage-graph", "energy-solar-graph", "energy-gas-graph", "energy-water-graph", "energy-distribution",
    "energy-sources-table", "energy-grid-neutrality-gauge", "energy-solar-consumed-gauge",
    "energy-self-sufficiency-gauge", "energy-carbon-consumed-gauge", "energy-devices-graph",
    "energy-devices-detail-graph", "alert", "shortcut", "home-summary", "toggle-group", "empty-state",
    "divider", "spacer", "section", "error",
})
# Row-only "special" rows inside an entities card.
ROW_TYPES = frozenset({"attribute", "button", "buttons", "call-service", "perform-action", "cast", "conditional",
                       "divider", "section", "simple-entity", "text", "weblink", "custom"})
VIEW_TYPES = frozenset({"masonry", "sections", "panel", "sidebar"})
SECTION_STRATEGIES = frozenset({"common-controls", "device", "entities"})
# Cards that render native controls for their entity without any action key.
CONTROL_CARDS = frozenset({"thermostat", "humidifier", "light", "alarm-panel", "media-control", "toggle-group",
                           "area", "picture-glance", "picture-elements"})
ACTION_KEYS = ("tap_action", "hold_action", "double_tap_action",
               "icon_tap_action", "icon_hold_action", "icon_double_tap_action")
ACTUATING_ACTIONS = frozenset({"toggle", "perform-action", "call-service", "fire-dom-event", "assist"})
PASSIVE_ACTIONS = frozenset({"more-info", "none", "navigate", "url"})
ALL_ACTIONS = ACTUATING_ACTIONS | PASSIVE_ACTIONS
# Cards whose default tap (no action key) can actuate for some domains.
IMPLICIT_ACTION_CARDS = frozenset({"button", "tile", "entity", "glance", "picture-entity", "light"})
# Frontend panels that are valid navigation targets outside dashboards.
# Built-in dashboards (lovelace, home, light, security, climate, maintenance) are dashboards, resolved from
# the inventory like any other, never waved through here.
SYSTEM_PANELS = frozenset({"config", "history", "logbook", "energy", "map", "developer-tools", "hassio", "profile",
                           "media-browser", "todo", "calendar"})


def config_sha256(config) -> str:
    """Semantic digest: canonical JSON (sorted keys, no whitespace). Key order and formatting do not change it."""
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def bytes_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


@dataclass
class EntityRef:
    entity_id: str
    path: str
    kind: str          # "structured" | "template"


@dataclass
class ActionUse:
    path: str
    key: str
    action: str
    entity: str        # the entity the action applies to ("" if none)
    target: tuple      # perform-action target entity ids
    service: str       # perform_action / service
    navigation_path: str
    url_path: str
    confirmation: bool


@dataclass
class Element:
    path: str          # JSON-pointer-like path: /views/0/sections/1/cards/2
    level: str         # "view" | "section" | "card" | "row" | "badge"
    type: str
    view_index: int
    entities: list = field(default_factory=list)


@dataclass
class ViewInfo:
    index: int
    path: str          # view path or "" (identity falls back to the index)
    title: str
    subview: bool
    visible: object
    type: str
    strategy: bool

    @property
    def ident(self) -> str:
        return self.path or str(self.index)


@dataclass
class Model:
    views: list
    elements: list
    refs: list
    actions: list
    navigation: list   # (path, navigation_path)
    custom_cards: list
    unknown_cards: list
    controls: list     # (path, entity_id, how) how in explicit|implicit|native
    strategies: list   # (path, level, strategy type)

    def entity_ids(self, kinds=("structured", "template")) -> set:
        return {r.entity_id for r in self.refs if r.kind in kinds}


def _as_entity(v) -> str:
    return v if isinstance(v, str) and RE_ENTITY_ID.match(v) and domain(v) in KNOWN_DOMAINS else ""


def walk(config) -> Model:
    """Walk a dashboard config. Never raises on odd shapes; odd shapes are left for the validators."""
    m = Model([], [], [], [], [], [], [], [], [])
    if not isinstance(config, dict):
        return m
    views = config.get("views")
    if not isinstance(views, list):
        return m
    for vi, view in enumerate(views):
        if not isinstance(view, dict):
            continue
        vp = f"/views/{vi}"
        m.views.append(ViewInfo(vi, view.get("path") if isinstance(view.get("path"), str) else "",
                                str(view.get("title") or ""), view.get("subview") is True, view.get("visible", True),
                                str(view.get("type") or ("sections" if "sections" in view else "masonry")),
                                "strategy" in view))
        m.elements.append(Element(vp, "view", m.views[-1].type, vi))
        if "strategy" in view:
            m.strategies.append((vp, "view", _strategy_type(view["strategy"])))
        _walk_generic(view, vp, vi, m, skip=("cards", "sections", "badges", "header", "footer", "sidebar"))
        for key in ("cards",):
            for ci, card in enumerate(view.get(key) or [] if isinstance(view.get(key), list) else []):
                _walk_card(card, f"{vp}/{key}/{ci}", vi, m)
        for si, sec in enumerate(view.get("sections") or [] if isinstance(view.get("sections"), list) else []):
            _walk_section(sec, f"{vp}/sections/{si}", vi, m)
        for bi, badge in enumerate(view.get("badges") or [] if isinstance(view.get("badges"), list) else []):
            _walk_badge(badge, f"{vp}/badges/{bi}", vi, m)
        for k in ("header", "footer"):
            part = view.get(k)
            if isinstance(part, dict) and isinstance(part.get("card"), dict):
                _walk_card(part["card"], f"{vp}/{k}/card", vi, m)
        side = view.get("sidebar")
        if isinstance(side, dict):
            for si, sec in enumerate(side.get("sections") or [] if isinstance(side.get("sections"), list) else []):
                _walk_section(sec, f"{vp}/sidebar/sections/{si}", vi, m)
    return m


def _strategy_type(s) -> str:
    return str(s.get("type")) if isinstance(s, dict) else ""


def _walk_section(sec, path, vi, m: Model):
    if not isinstance(sec, dict):
        return
    m.elements.append(Element(path, "section", str(sec.get("type") or ""), vi))
    if "strategy" in sec:
        m.strategies.append((path, "section", _strategy_type(sec["strategy"])))
        _scan_strategy_targets(sec["strategy"], path + "/strategy", m)
    _walk_generic(sec, path, vi, m, skip=("cards", "sections", "badges", "strategy"))
    for ci, card in enumerate(sec.get("cards") or [] if isinstance(sec.get("cards"), list) else []):
        _walk_card(card, f"{path}/cards/{ci}", vi, m)
    for si, sub in enumerate(sec.get("sections") or [] if isinstance(sec.get("sections"), list) else []):
        _walk_section(sub, f"{path}/sections/{si}", vi, m)
    for bi, badge in enumerate(sec.get("badges") or [] if isinstance(sec.get("badges"), list) else []):
        _walk_badge(badge, f"{path}/badges/{bi}", vi, m)


def _scan_strategy_targets(strategy, path, m: Model):
    if not isinstance(strategy, dict):
        return
    tgt = strategy.get("target")
    if isinstance(tgt, dict):
        ids = tgt.get("entity_id")
        for e in ([ids] if isinstance(ids, str) else ids if isinstance(ids, list) else []):
            if _as_entity(e):
                m.refs.append(EntityRef(e, path + "/target/entity_id", "structured"))


def _walk_badge(badge, path, vi, m: Model):
    if isinstance(badge, str):
        if _as_entity(badge):
            m.refs.append(EntityRef(badge, path, "structured"))
            m.elements.append(Element(path, "badge", "entity", vi, [badge]))
        return
    if not isinstance(badge, dict):
        return
    ent = _as_entity(badge.get("entity"))
    m.elements.append(Element(path, "badge", str(badge.get("type") or "entity"), vi, [ent] if ent else []))
    _walk_generic(badge, path, vi, m, skip=(), owner_entity=ent, card_type="badge")


def _walk_card(card, path, vi, m: Model, level="card"):
    if not isinstance(card, dict):
        return
    ctype = card.get("type")
    ctype = ctype if isinstance(ctype, str) else ""
    ents = []
    own = _as_entity(card.get("entity"))
    if own:
        ents.append(own)
    if level == "card":
        if ctype.startswith("custom:"):
            m.custom_cards.append((path, ctype))
        elif ctype not in NATIVE_CARDS:
            m.unknown_cards.append((path, ctype))
    el = Element(path, level, ctype, vi, ents)
    m.elements.append(el)
    # nested cards
    if isinstance(card.get("cards"), list):
        for ci, sub in enumerate(card["cards"]):
            _walk_card(sub, f"{path}/cards/{ci}", vi, m)
    if isinstance(card.get("card"), dict):
        _walk_card(card["card"], f"{path}/card", vi, m)
    # entities rows
    if isinstance(card.get("entities"), list):
        for ri, row in enumerate(card["entities"]):
            rp = f"{path}/entities/{ri}"
            if isinstance(row, str):
                if _as_entity(row):
                    m.refs.append(EntityRef(row, rp, "structured"))
                    el.entities.append(row)
                    _note_control(rp, row, ctype, {}, m, row_of=ctype)
            elif isinstance(row, dict):
                re_ = _as_entity(row.get("entity"))
                m.elements.append(Element(rp, "row", str(row.get("type") or ""), vi, [re_] if re_ else []))
                if re_:
                    el.entities.append(re_)
                _walk_generic(row, rp, vi, m, skip=("entities",), owner_entity=re_, card_type="row:" + ctype)
                if re_ and row.get("type") not in ("simple-entity", "attribute"):
                    # simple-entity / attribute rows show state only (no inline toggle)
                    _note_control(rp, re_, ctype, row, m, row_of=ctype)
    if isinstance(card.get("badges"), list):
        for bi, b in enumerate(card["badges"]):
            _walk_badge(b, f"{path}/badges/{bi}", vi, m)
    _walk_generic(card, path, vi, m, skip=("cards", "card", "entities", "badges"), owner_entity=own, card_type=ctype)
    if own:
        _note_control(path, own, ctype, card, m)


def _note_control(path, ent, ctype, cfg, m: Model, row_of=""):
    d = domain(ent)
    if d not in ACTUATOR_DOMAINS:
        return
    if row_of:
        # entities-card rows render toggles/inputs inline for actuator domains
        if row_of in ("entities", ""):
            m.controls.append((path, ent, "native"))
        return
    if ctype in CONTROL_CARDS or (ctype == "tile" and cfg.get("features")):
        m.controls.append((path, ent, "native"))
    if ctype in IMPLICIT_ACTION_CARDS:
        keys = ("tap_action", "hold_action", "double_tap_action")
        if ctype == "tile":
            keys += ("icon_tap_action",)
        if any(not isinstance(cfg.get(k), dict) for k in keys):
            m.controls.append((path, ent, "implicit"))


def _walk_generic(obj, path, vi, m: Model, skip=(), owner_entity="", card_type=""):
    """Collect entity refs, actions, navigation and conditions anywhere in ``obj`` except ``skip`` keys."""
    for k, v in obj.items():
        if k in skip:
            continue
        p = f"{path}/{k}"
        if k in ACTION_KEYS and isinstance(v, dict):
            _action(v, p, k, owner_entity, m)
        elif k in ("entity", "camera_image", "image_entity", "entity_id") and isinstance(v, str):
            if _as_entity(v):
                m.refs.append(EntityRef(v, p, "structured"))
            elif "{" in v:
                _template_refs(v, p, m)
        elif k == "entity_id" and isinstance(v, list):
            for i, e in enumerate(v):
                if _as_entity(e):
                    m.refs.append(EntityRef(e, f"{p}/{i}", "structured"))
        elif k in ("conditions", "visibility") and isinstance(v, list):
            for i, c in enumerate(v):
                if isinstance(c, dict):
                    _walk_generic(c, f"{p}/{i}", vi, m)
        elif k == "target" and isinstance(v, dict):
            _walk_generic(v, p, vi, m)
        elif isinstance(v, str):
            if "{{" in v or "{%" in v or "states(" in v:
                _template_refs(v, p, m)
        elif isinstance(v, dict):
            _walk_generic(v, p, vi, m, skip=(), owner_entity="")
        elif isinstance(v, list):
            for i, x in enumerate(v):
                if isinstance(x, dict):
                    _walk_generic(x, f"{p}/{i}", vi, m)
                elif isinstance(x, str) and ("{{" in x or "{%" in x):
                    _template_refs(x, f"{p}/{i}", m)


def _template_refs(text: str, path: str, m: Model):
    seen = set()
    for e in _RE_TEMPLATE_ENTITY.findall(text):
        if e not in seen and domain(e) in KNOWN_DOMAINS and RE_ENTITY_ID.match(e):
            seen.add(e)
            m.refs.append(EntityRef(e, path, "template"))


def _action(cfg: dict, path: str, key: str, owner_entity: str, m: Model):
    act = cfg.get("action")
    act = act if isinstance(act, str) else ""
    tgt = []
    t = cfg.get("target")
    if isinstance(t, dict):
        ids = t.get("entity_id")
        tgt = [ids] if isinstance(ids, str) else [x for x in ids if isinstance(x, str)] if isinstance(ids, list) else []
    for e in tgt:
        if _as_entity(e):
            m.refs.append(EntityRef(e, path + "/target/entity_id", "structured"))
    ent = _as_entity(cfg.get("entity")) or owner_entity
    if _as_entity(cfg.get("entity")):
        m.refs.append(EntityRef(cfg["entity"], path + "/entity", "structured"))
    nav = cfg.get("navigation_path") if isinstance(cfg.get("navigation_path"), str) else ""
    m.actions.append(ActionUse(path, key, act, ent, tuple(tgt),
                               str(cfg.get("perform_action") or cfg.get("service") or ""), nav,
                               cfg.get("url_path") if isinstance(cfg.get("url_path"), str) else "",
                               "confirmation" in cfg and cfg.get("confirmation") is not False))
    if act == "navigate":
        m.navigation.append((path, nav))
    data = cfg.get("data") if isinstance(cfg.get("data"), dict) else cfg.get("service_data")
    if isinstance(data, dict):
        ids = data.get("entity_id")
        for e in ([ids] if isinstance(ids, str) else ids if isinstance(ids, list) else []):
            if _as_entity(e):
                m.refs.append(EntityRef(e, path + "/data/entity_id", "structured"))


def resolve_navigation(nav: str, dashboard_url: str, views: list, known_dashboards: dict | None):
    """Return (status, detail). status: OK | DEAD | UNKNOWN | INVALID.

    ``known_dashboards`` maps url_path -> list of view idents (path or index string), or None when the
    dashboard inventory is not available (then other-dashboard targets are UNKNOWN, never OK).
    """
    if not nav or not nav.startswith("/") or "//" in nav or any(c in nav for c in " \t\n\"'<>`"):
        return "INVALID", nav
    parts = nav.split("?", 1)[0].split("#", 1)[0].strip("/").split("/")
    first = parts[0]
    if first == dashboard_url:
        idents = {v.ident for v in views} | {str(v.index) for v in views}
        if len(parts) == 1:
            return ("OK", nav) if views else ("DEAD", nav)
        return ("OK", nav) if parts[1] in idents else ("DEAD", nav)
    if known_dashboards is not None and first in known_dashboards:
        if len(parts) == 1:
            return "OK", nav
        return ("OK", nav) if parts[1] in set(known_dashboards[first]) else ("DEAD", nav)
    if first in SYSTEM_PANELS:
        return "OK", nav
    return "UNKNOWN", nav
