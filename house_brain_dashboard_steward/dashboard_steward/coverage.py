"""Live coverage ledger, data health, usage view and dashboard export (0.1.1). Pure computation, no I/O.

* ``ledger``       the deletion-readiness ledger of ``tools/dashboard_steward/coverage_ledger.py`` (same rules,
                   same output keys), run against the dashboards the Steward already read live (M2).
* ``liveness``     which entity ids are live now: in the entity registry or in states, and not unavailable or
                   unknown for longer than ``dead_after_s`` (default 24 h).
* ``data_health``  entities the main dashboard references that are unavailable, unknown or missing, with how
                   long (from ``last_changed`` in states; the recorder database is never opened) (M1).
* ``usage_view``   the ``usage_prediction/common_control`` lists per time of day next to the main dashboard's
                   first view (its pins); display only, nothing is proposed from it (M3).
* ``export``       a dashboard's full config for the owner to download (owner pop-up 2026-11-22 "Both"):
                   secret-looking values replaced by ``<set>`` (DATA_RETENTION_STANDARD AI-copy rule), with the
                   live canonical hash so the repository copy can be checked against Deployer scans.

A dashboard is ``ready`` only when nothing live is missing. Readiness is necessary, not sufficient: deleting a
dashboard still needs the owner's pop-up and an archive copy first.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from . import lovelace as L
from .contracts import timestamp

LEDGER_SCHEMA = "dashboard_steward_coverage_ledger.v1"
DATA_HEALTH_SCHEMA = "dashboard_steward_data_health.v1"
USAGE_SCHEMA = "dashboard_steward_usage.v1"
EXPORT_SCHEMA = "dashboard_steward_export.v1"
BAD_STATES = ("unavailable", "unknown")
DEAD_AFTER_S = 24 * 3600
TIME_CATEGORIES = ("morning", "afternoon", "evening", "night")
RE_URL_PATH = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_SECRET_KEY = re.compile(r"(token|password|passwd|secret|api_?key|access_?key|auth|credential|bearer)", re.I)
_SECRET_QUERY = re.compile(r"((?:access_token|token|api_key|apikey|key|auth|password|signature|sig)=)[^&#\s\"']+",
                           re.I)


def ledger(candidate: dict, old: dict, live: set | None = None, retired: frozenset = frozenset()) -> dict:
    """Byte-for-byte the rules of tools/dashboard_steward/coverage_ledger.ledger (a test proves parity)."""
    have = set(L.walk(candidate).entity_ids())
    out, total_missing = {}, set()
    for name, cfg in old.items():
        refs = set(L.walk(cfg).entity_ids())
        dead = refs - live if live is not None else set()
        alive = refs - dead
        missing = alive - have - retired
        out[name] = {
            "refs": len(refs), "dead": len(dead), "covered": len(alive & have), "retired": len(alive & retired),
            "missing": sorted(missing),
            "missing_controls": sorted(e for e in missing if L.domain(e) in L.ACTUATOR_DOMAINS),
            "ready": not missing,
        }
        total_missing |= missing
    return {"schema": LEDGER_SCHEMA, "dashboards": out,
            "total_missing": len(total_missing),
            "total_missing_controls": sum(1 for e in total_missing if L.domain(e) in L.ACTUATOR_DOMAINS),
            "live_list_used": live is not None}


def _since(row: dict):
    for k in ("last_changed", "last_updated"):
        v = row.get(k)
        if isinstance(v, str):
            try:
                return timestamp(v)
            except ValueError:
                continue
    return None


def _states(raw_states) -> dict:
    return {r["entity_id"]: r for r in raw_states or [] if isinstance(r, dict) and isinstance(r.get("entity_id"), str)}


def liveness(registry_ids, raw_states, now: float, dead_after_s: float = DEAD_AFTER_S) -> set:
    states = _states(raw_states)
    live = set(registry_ids or ()) | set(states)
    for eid, row in states.items():
        if str(row.get("state")) in BAD_STATES:
            since = _since(row)
            if since is not None and now - since > dead_after_s:
                live.discard(eid)
    return live


def data_health(main_cfg: dict, registry_ids, raw_states, now: float, dashboard: str = "") -> dict:
    states = _states(raw_states)
    registry = set(registry_ids or ())
    refs = sorted(set(L.walk(main_cfg).entity_ids())) if main_cfg is not None else []
    rows = []
    for eid in refs:
        row = states.get(eid)
        if row is None:
            rows.append({"entity_id": eid, "state": "missing" if eid not in registry else "no_state",
                         "since": None, "for_s": None, "in_registry": eid in registry})
            continue
        st = str(row.get("state"))
        if st in BAD_STATES:
            since = _since(row)
            rows.append({"entity_id": eid, "state": st, "since": since,
                         "for_s": None if since is None else max(0, round(now - since)), "in_registry": eid in registry})
    rows.sort(key=lambda r: (-(r["for_s"] if r["for_s"] is not None else 10 ** 12), r["entity_id"]))
    return {"schema": DATA_HEALTH_SCHEMA, "dashboard": dashboard, "at": now, "refs": len(refs),
            "bad": len(rows), "over_24h": sum(1 for r in rows if r["for_s"] is None or r["for_s"] > DEAD_AFTER_S),
            "entities": rows}


def time_category(hour: int) -> str:
    """Home Assistant 2026.11.2 usage_prediction/common_control.py time_category (read from source)."""
    if 6 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 22:
        return "evening"
    return "night"


def local_category(now: float, time_zone: str | None) -> str:
    """The bucket Home Assistant used for an answer at ``now``; ``unknown`` when the zone cannot be resolved."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(time_zone) if isinstance(time_zone, str) and time_zone else None
    except Exception:                    # no tz database in the image, or a bad name: say so, never guess
        tz = None
    if tz is None:
        return "unknown"
    return time_category(datetime.fromtimestamp(now, timezone.utc).astimezone(tz).hour)


def usage_view(usage: dict, main_cfg: dict | None) -> dict:
    """Per category: each predicted entity and where it is on the main dashboard (first view = pins)."""
    first, anywhere = set(), set()
    if main_cfg is not None:
        m = L.walk(main_cfg)
        anywhere = set(m.entity_ids())
        if isinstance(main_cfg.get("views"), list) and main_cfg["views"]:
            first = set(L.walk({"views": main_cfg["views"][:1]}).entity_ids())
    cats = {}
    for cat, entry in (usage.get("by_category") or {}).items():
        cats[cat] = {"at": entry.get("at"),
                     "entities": [{"entity_id": e, "placement": "pinned" if e in first else
                                   ("on_main" if e in anywhere else "not_on_main")}
                                  for e in entry.get("entities", [])]}
    return {"schema": USAGE_SCHEMA, "status": usage.get("status", "NOT_READ"), "reason": usage.get("reason", ""),
            "user_scope": "SUPERVISOR_TOKEN_USER",
            "user_scope_note": ("Home Assistant ranks the calling user's own service calls (30 days). The Steward "
                                "reads with the App's Supervisor token, so these lists are that identity's, "
                                "not the owner's phone or browser. Display only."),
            "pins": sorted(first), "by_category": cats}


def _redact(v, count: list):
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            if isinstance(k, str) and _SECRET_KEY.search(k) and isinstance(x, (str, int, float)) and x != "":
                out[k] = "<set>"
                count[0] += 1
            else:
                out[k] = _redact(x, count)
        return out
    if isinstance(v, list):
        return [_redact(x, count) for x in v]
    if isinstance(v, str):
        new, n = _SECRET_QUERY.subn(lambda mt: mt.group(1) + "<set>", v)
        count[0] += n
        return new
    return v


def export(url_path: str, config, captured_at: float, version: str) -> dict:
    count = [0]
    redacted = _redact(config, count)
    return {"schema": EXPORT_SCHEMA, "url_path": url_path, "steward_version": version,
            "captured_at": datetime.fromtimestamp(captured_at, timezone.utc).isoformat(),
            "live_config_sha256": L.config_sha256(config), "exported_config_sha256": L.config_sha256(redacted),
            "redactions": count[0],
            "hash_note": ("exported == live: the file is the exact live config" if count[0] == 0 else
                          "values were replaced by <set>; compare live_config_sha256 with the Deployer scan"),
            "views": len(config.get("views", [])) if isinstance(config, dict) and isinstance(config.get("views"), list) else 0,
            "config": redacted}


def export_json(doc: dict) -> bytes:
    return json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False).encode("utf-8")


def export_yaml(doc: dict) -> bytes:
    """The config as YAML with a provenance header; refuses (ValueError) what the strict emitter cannot write."""
    from . import yamlio
    head = "".join(f"# {k}: {doc[k]}\n" for k in ("schema", "url_path", "steward_version", "captured_at",
                                                    "live_config_sha256", "exported_config_sha256", "redactions",
                                                    "hash_note", "views"))
    return (head + yamlio.dumps(doc["config"])).encode("utf-8")
