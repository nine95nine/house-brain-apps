"""Dashboards (0.3.43): a read-only scan and an owner-approved entity rename in storage-mode dashboards.

Owner pop-ups 2026-11-01: "I need you to figure out a way to change dashboards and anything else that entails that
old sensor" and "Build it (Recommended)" (#158: the replacement Ondilo probe reports on ``sensor.tonto_ln_pool_*_2``;
the old names stay reserved for the spa, so dashboards must be re-pointed, not the entities renamed).

``house_brain_dashboard_scan_request.v1`` (read-only, lookup allowance, asks first like every lookup)
    Lists the dashboards (``lovelace/dashboards/list`` plus the default one), reads each config
    (``lovelace/config``) and counts whole-name occurrences of each requested entity id in its strings. It also
    asks ``search/related`` which automations, scripts, scenes and groups use each entity. Output (``DBS1``):
    per dashboard its url path, mode, config SHA-256 and counts; per entity the related ids. Never dashboard
    content: no titles of cards, no other entity ids, no text.

``house_brain_dashboard_rename_request.v1`` (changes Home Assistant; asks first, DEPLOY stage)
    For one storage-mode dashboard: the config must still hash to ``expect_config_sha256`` (else nothing is
    changed); every whole-name occurrence of each ``renames`` key is replaced by its value, and the count must
    equal ``expect_replacements``. After Approve: a partial Home Assistant backup, the old config saved in this
    App's ``/data`` (the Undo copy), ``lovelace/config/save``, then a read-back that must hash to the new config.
    If the read-back differs, the old config is saved back. No file under ``/config/packages`` changes and nothing
    restarts.

``house_brain_dashboard_undo_request.v1`` (asks first, DEPLOY stage)
    Puts back the config saved by an earlier rename, only while the dashboard still hashes to what that rename
    wrote (so a later owner edit is never overwritten).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass

from . import approval, net
from .manifest import RE_REQUEST_ID, REQUESTERS, ManifestError

SCAN_SCHEMA = "house_brain_dashboard_scan_request.v1"
RENAME_SCHEMA = "house_brain_dashboard_rename_request.v1"
UNDO_SCHEMA = "house_brain_dashboard_undo_request.v1"
KINDS = {SCAN_SCHEMA: "scan", RENAME_SCHEMA: "rename", UNDO_SCHEMA: "undo"}

SCAN_OK = "DASHBOARD_SCAN_OK"
SCAN_FAILED = "DASHBOARD_SCAN_FAILED"
RENAMED = "DASHBOARD_RENAMED"
RENAME_FAILED = "DASHBOARD_RENAME_FAILED"
UNDONE = "DASHBOARD_UNDONE"
UNDO_FAILED = "DASHBOARD_UNDO_FAILED"

MAX_ENTITIES = 16
MAX_REPLACEMENTS = 5000
MAX_CONFIG_BYTES = 3 * 1024 * 1024
MAX_DASHBOARDS = 64
MAX_RELATED_IDS = 40
RE_ENTITY = re.compile(r"^[a-z_]{2,32}\.[a-z0-9_]{1,96}$")
# Home Assistant dashboard url paths contain a hyphen; "" is the default dashboard (url_path null on the wire).
RE_URL_PATH = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
RE_SHA256 = re.compile(r"^[0-9a-f]{64}$")
RELATED_KINDS = ("automation", "script", "scene", "group")
UNDO_DIR = "dashboard_undo"


@dataclass(frozen=True)
class DashboardRequest:
    kind: str
    request_id: str
    requested_by: str
    tracking_issue: int
    digest: str
    entities: tuple[str, ...] = ()
    dashboard: str = ""
    expect_config_sha256: str = ""
    renames: tuple[tuple[str, str], ...] = ()
    expect_replacements: int = 0
    undo_of: str = ""


def kind(raw_bytes: bytes) -> str | None:
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return KINDS.get(data.get("schema")) if isinstance(data, dict) else None


def _load(raw_bytes: bytes) -> dict:
    if len(raw_bytes) > 16 * 1024:
        raise ManifestError("TOO_LARGE")

    def _no_dupes(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ManifestError("DUPLICATE_KEY", str(key)[:40])
            out[key] = value
        return out

    def _no_constants(name):
        raise ManifestError("JSON_CONSTANT", name)

    try:
        data = json.loads(raw_bytes.decode("utf-8", errors="strict"), object_pairs_hook=_no_dupes,
                          parse_constant=_no_constants)
    except ManifestError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as err:
        raise ManifestError("JSON") from err
    if not isinstance(data, dict):
        raise ManifestError("TYPE")
    return data


def parse(raw_bytes: bytes) -> DashboardRequest:
    data = _load(raw_bytes)
    which = KINDS.get(data.get("schema"))
    if which is None:
        raise ManifestError("SCHEMA_VERSION")
    required = {"schema", "request_id", "requested_by", "tracking_issue"}
    required |= {"scan": {"entities"}, "rename": {"dashboard", "expect_config_sha256", "renames",
                                                   "expect_replacements"}, "undo": {"undo_of"}}[which]
    if required - set(data):
        raise ManifestError("MISSING_KEY", sorted(required - set(data))[0])
    if set(data) - required - {"note", "title"}:
        raise ManifestError("UNKNOWN_KEY", sorted(set(data) - required - {"note", "title"})[0][:40])
    rid = data["request_id"]
    if not isinstance(rid, str) or not RE_REQUEST_ID.fullmatch(rid):
        raise ManifestError("PATTERN", "request_id")
    if data["requested_by"] not in REQUESTERS:
        raise ManifestError("REQUESTER")
    issue = data["tracking_issue"]
    if isinstance(issue, bool) or not isinstance(issue, int) or not 1 <= issue <= 10_000_000:
        raise ManifestError("RANGE", "tracking_issue")
    for text_key in ("note", "title"):
        if not isinstance(data.get(text_key, ""), str) or len(data.get(text_key, "")) > 2000:
            raise ManifestError("TYPE", text_key)
    digest = hashlib.sha256(raw_bytes).hexdigest()
    if which == "scan":
        ents = data["entities"]
        if (not isinstance(ents, list) or not ents or len(ents) > MAX_ENTITIES or len(set(map(str, ents))) != len(ents)
                or not all(isinstance(e, str) and RE_ENTITY.fullmatch(e) for e in ents)):
            raise ManifestError("PATTERN", "entities")
        return DashboardRequest("scan", rid, data["requested_by"], issue, digest, entities=tuple(ents))
    if which == "undo":
        undo_of = data["undo_of"]
        if not isinstance(undo_of, str) or not RE_REQUEST_ID.fullmatch(undo_of) or undo_of == rid:
            raise ManifestError("PATTERN", "undo_of")
        return DashboardRequest("undo", rid, data["requested_by"], issue, digest, undo_of=undo_of)
    dash = data["dashboard"]
    if not isinstance(dash, str) or (dash and not RE_URL_PATH.fullmatch(dash)):
        raise ManifestError("PATTERN", "dashboard")
    sha = data["expect_config_sha256"]
    if not isinstance(sha, str) or not RE_SHA256.fullmatch(sha):
        raise ManifestError("PATTERN", "expect_config_sha256")
    ren = data["renames"]
    if not isinstance(ren, dict) or not ren or len(ren) > MAX_ENTITIES:
        raise ManifestError("PATTERN", "renames")
    pairs = []
    for old, new in ren.items():
        if (not RE_ENTITY.fullmatch(old) or not isinstance(new, str) or not RE_ENTITY.fullmatch(new) or old == new
                or old.split(".", 1)[0] != new.split(".", 1)[0]):
            raise ManifestError("PATTERN", "renames")
        pairs.append((old, new))
    olds = {o for o, _ in pairs}
    if any(n in olds for _, n in pairs):
        raise ManifestError("PATTERN", "renames chain")   # a -> b and b -> c would be order-dependent
    count = data["expect_replacements"]
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_REPLACEMENTS:
        raise ManifestError("RANGE", "expect_replacements")
    return DashboardRequest("rename", rid, data["requested_by"], issue, digest, dashboard=dash,
                            expect_config_sha256=sha, renames=tuple(sorted(pairs)), expect_replacements=count)


# ---------------------------------------------------------------- pure helpers (tested directly)
def config_sha256(config) -> str:
    """Canonical JSON (sorted keys, no spaces, UTF-8): the same dashboard always hashes the same."""
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def _token(entity: str) -> re.Pattern:
    # A whole entity id: not preceded or followed by a name character. A dot before it is allowed, so the template
    # form ``states.sensor.x.state`` is found too (pool_chemistry uses ``states.sensor.tonto_ln_pool_ph.last_updated``).
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(entity) + r"(?![A-Za-z0-9_])")


def count_refs(config, entities) -> dict[str, int]:
    pats = {e: _token(e) for e in entities}
    counts = dict.fromkeys(entities, 0)

    def walk(node):
        if isinstance(node, str):
            for e, p in pats.items():
                counts[e] += len(p.findall(node))
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(k)
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(config)
    return counts


def apply_renames(config, renames) -> tuple[object, int]:
    """Return a new config with every whole-name ``old`` replaced by ``new`` in every string (keys and values)."""
    pats = [(_token(o), n) for o, n in renames]
    total = 0

    def fix(text: str) -> str:
        nonlocal total
        for pat, new in pats:
            text, n = pat.subn(new, text)
            total += n
        return text

    def walk(node):
        if isinstance(node, str):
            return fix(node)
        if isinstance(node, dict):
            return {fix(k) if isinstance(k, str) else k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node
    return walk(config), total


def _url(dashboard: str):
    return dashboard or None


# ---------------------------------------------------------------- Home Assistant reads/writes (guarded in ha.py)
def read_config(ha, dashboard: str):
    cfg = ha.ws_once({"type": "lovelace/config", "url_path": _url(dashboard), "force": False}, timeout=60)
    if not isinstance(cfg, dict):
        raise DashboardError("CONFIG_SHAPE")
    if len(json.dumps(cfg, ensure_ascii=False).encode("utf-8")) > MAX_CONFIG_BYTES:
        raise DashboardError("CONFIG_TOO_LARGE")
    return cfg


def save_config(ha, dashboard: str, config) -> None:
    ha.ws_once({"type": "lovelace/config/save", "url_path": _url(dashboard), "config": config}, timeout=60)


def list_dashboards(ha) -> list[dict]:
    listed = ha.ws_once({"type": "lovelace/dashboards/list"}, timeout=30) or []
    out = [{"url_path": "", "mode": "default"}]
    for item in listed[:MAX_DASHBOARDS] if isinstance(listed, list) else []:
        if isinstance(item, dict) and isinstance(item.get("url_path"), str) and RE_URL_PATH.fullmatch(item["url_path"]):
            out.append({"url_path": item["url_path"], "mode": str(item.get("mode") or "?")[:12]})
    return out


class DashboardError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def scan_lines(ha, entities) -> list[str]:
    dashboards = list_dashboards(ha)
    lines = [f"DBS1 dashboards={len(dashboards)} entities={len(entities)}"]
    for d in dashboards:
        name = d["url_path"] or "(default)"
        try:
            cfg = read_config(ha, d["url_path"])
        except Exception as err:  # noqa: BLE001 - a dashboard that cannot be read is reported, never guessed
            code = getattr(err, "code", type(err).__name__)
            lines.append(f"D {name} mode={d['mode']} unreadable={code}")
            continue
        counts = count_refs(cfg, entities)
        total = sum(counts.values())
        views = len(cfg.get("views", [])) if isinstance(cfg.get("views"), list) else 0
        found = " ".join(f"{e}={n}" for e, n in counts.items() if n)
        lines.append(f"D {name} mode={d['mode']} sha={config_sha256(cfg)} views={views} refs={total}"
                     + (f" {found}" if found else ""))
    for e in entities:
        try:
            rel = ha.ws_once({"type": "search/related", "item_type": "entity", "item_id": e}, timeout=30) or {}
        except Exception as err:  # noqa: BLE001
            lines.append(f"R {e} unreadable={getattr(err, 'code', type(err).__name__)}")
            continue
        parts = []
        for k in RELATED_KINDS:
            ids = [i for i in (rel.get(k) or []) if isinstance(i, str) and RE_ENTITY.fullmatch(i)]
            if ids:
                parts.append(f"{k}={','.join(sorted(ids)[:MAX_RELATED_IDS])}")
        lines.append(f"R {e} " + (" ".join(parts) if parts else "none"))
    lines.append("END")
    return lines


def _undo_path(data_dir: str, request_id: str) -> str:
    return os.path.join(data_dir, UNDO_DIR, f"{request_id}.json")


def _write_undo(data_dir: str, request_id: str, payload: dict) -> None:
    folder = os.path.join(data_dir, UNDO_DIR)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    path = _undo_path(data_dir, request_id)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------- runners (called from main after parsing)
def run_scan(engine, req: DashboardRequest):
    from .engine import REJECTED, TIMED_OUT, Result

    engine.j.audit(req.request_id, "RECEIVED", digest=req.digest, requested_by=req.requested_by, kind="dashboard_scan")
    message = (f"{req.request_id} from {req.requested_by}\nRead-only: count where these names appear in your "
               f"dashboards, and which automations/scripts use them:\n" + "\n".join(req.entities)
               + "\nOnly counts and names leave the house. Nothing is changed.")
    outcome = engine._ask(req.request_id, "LOOKUP", "House Brain: scan dashboards?", message,
                          engine.s.timing.approval_timeout)
    if outcome != approval.APPROVE:
        return Result(REJECTED if outcome == approval.REJECT else TIMED_OUT, [outcome])
    try:
        lines = scan_lines(engine.ha, req.entities)
    except Exception as err:  # noqa: BLE001
        return Result(SCAN_FAILED, [f"scan stopped: {net.redact(str(err))[:200]}"])
    return Result(SCAN_OK, [], {"lookup_output": "\n".join(lines)[:20000]})


def run_rename(engine, req: DashboardRequest, data_dir: str, dry_run: bool):
    from .engine import REJECTED, TIMED_OUT, Result

    engine.j.audit(req.request_id, "RECEIVED", digest=req.digest, requested_by=req.requested_by,
                   kind="dashboard_rename")
    name = req.dashboard or "(default)"
    try:
        before = read_config(engine.ha, req.dashboard)
    except Exception as err:  # noqa: BLE001
        return Result(RENAME_FAILED, [f"dashboard {name} could not be read: "
                                      f"{getattr(err, 'code', type(err).__name__)}; nothing changed"])
    sha_before = config_sha256(before)
    if sha_before != req.expect_config_sha256:
        return Result(RENAME_FAILED, [f"dashboard {name} changed since the scan (sha {sha_before[:12]}); "
                                      "nothing changed. Scan again."], {"config_sha256": sha_before})
    after, n = apply_renames(before, req.renames)
    if n != req.expect_replacements:
        return Result(RENAME_FAILED, [f"{n} replacements, expected {req.expect_replacements}; nothing changed"],
                      {"replacements": n})
    sha_after = config_sha256(after)
    message = (f"{req.request_id} from {req.requested_by}\nDashboard {name}: replace {n} references\n"
               + "\n".join(f"{o} -> {nw}" for o, nw in req.renames)
               + "\nA Home Assistant backup is taken first and the old dashboard is kept for Undo. No restart.")
    outcome = engine._ask(req.request_id, "DEPLOY", "House Brain: update a dashboard?", message,
                          engine.s.timing.approval_timeout)
    if outcome != approval.APPROVE:
        return Result(REJECTED if outcome == approval.REJECT else TIMED_OUT, [outcome])
    facts = {"dashboard": name, "replacements": n, "config_sha256_before": sha_before, "config_sha256_after": sha_after}
    if dry_run:
        return Result("DRY_RUN_OK", ["Dry-run mode: the dashboard was not changed."], facts)
    try:
        engine.ha.backup_partial(f"house-brain-dashboard-{req.request_id}"[:60])
    except Exception as err:  # noqa: BLE001 - no backup, no change
        return Result(RENAME_FAILED, [f"backup failed ({getattr(err, 'code', type(err).__name__)}); nothing changed"],
                      facts)
    _write_undo(data_dir, req.request_id, {"dashboard": req.dashboard, "config": before,
                                           "sha_before": sha_before, "sha_after": sha_after})
    try:
        save_config(engine.ha, req.dashboard, after)
        readback = config_sha256(read_config(engine.ha, req.dashboard))
    except Exception as err:  # noqa: BLE001
        readback = f"error:{getattr(err, 'code', type(err).__name__)}"
    if readback == sha_after:
        return Result(RENAMED, [f"Dashboard {name}: {n} references renamed. Undo: file a dashboard undo request "
                                f"with undo_of {req.request_id}."], facts)
    reasons = [f"read-back did not match ({readback[:24]})"]
    try:
        save_config(engine.ha, req.dashboard, before)
        reasons.append("the old dashboard was saved back" if config_sha256(read_config(engine.ha, req.dashboard))
                       == sha_before else "UNDO CHECK FAILED: compare the dashboard with the backup")
    except Exception as err:  # noqa: BLE001
        reasons.append(f"UNDO FAILED ({getattr(err, 'code', type(err).__name__)}): restore from the backup")
    return Result(RENAME_FAILED, reasons, facts)


def run_undo(engine, req: DashboardRequest, data_dir: str, dry_run: bool):
    from .engine import REJECTED, TIMED_OUT, Result

    engine.j.audit(req.request_id, "RECEIVED", digest=req.digest, requested_by=req.requested_by, kind="dashboard_undo")
    try:
        with open(_undo_path(data_dir, req.undo_of), encoding="utf-8") as fh:
            saved = json.load(fh)
        dash, old, sha_before, sha_after = saved["dashboard"], saved["config"], saved["sha_before"], saved["sha_after"]
    except Exception:  # noqa: BLE001
        return Result(UNDO_FAILED, [f"no Undo copy for {req.undo_of}; nothing changed"])
    name = dash or "(default)"
    try:
        current = config_sha256(read_config(engine.ha, dash))
    except Exception as err:  # noqa: BLE001
        return Result(UNDO_FAILED, [f"dashboard {name} could not be read ({getattr(err, 'code', type(err).__name__)})"])
    if current != sha_after:
        return Result(UNDO_FAILED, [f"dashboard {name} was edited after {req.undo_of}; nothing changed"])
    outcome = engine._ask(req.request_id, "DEPLOY", "House Brain: undo a dashboard change?",
                          f"{req.request_id} from {req.requested_by}\nPut dashboard {name} back as it was before "
                          f"{req.undo_of}. No restart.", engine.s.timing.approval_timeout)
    if outcome != approval.APPROVE:
        return Result(REJECTED if outcome == approval.REJECT else TIMED_OUT, [outcome])
    if dry_run:
        return Result("DRY_RUN_OK", ["Dry-run mode: nothing was put back."])
    try:
        save_config(engine.ha, dash, old)
        ok = config_sha256(read_config(engine.ha, dash)) == sha_before
    except Exception as err:  # noqa: BLE001
        return Result(UNDO_FAILED, [f"save failed ({getattr(err, 'code', type(err).__name__)}): restore from backup"])
    return (Result(UNDONE, [f"Dashboard {name} is back as before {req.undo_of}."]) if ok
            else Result(UNDO_FAILED, ["read-back did not match: compare the dashboard with the backup"]))
