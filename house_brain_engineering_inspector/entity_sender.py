"""House Brain Engineering Inspector 0.2.0: entity snapshot sender (#212 G0).

Owner decision 2026-10-01 ("Yes, build it"). Opt-in through the App option ``entity_snapshot_mode``:

* ``off`` (default, and when no options file exists): not used; the Inspector runs its 0.1.4
  one-shot inspection unchanged.
* ``dry_run``: one read of ``/api/states`` and the entity registry, passed to the embedded accepted
  builder (``embedded.tools.build_house_brain_entity_snapshot.py``, byte-identical to the repository)
  in ``dry_run`` mode. The App log gets the report: per-reason exclusion counts and the entity ids that
  would be exported, never a state or attribute value and never an excluded id. Nothing is sent.
  The owner reviews this list before any export is approved.
* ``export``: refused at start unless every ``preconditions_for_real_export`` in the embedded policy is
  true (an owner-reviewed repository change and a new Inspector version). Then, every 300 s, the export
  snapshot is POSTed to the Maintenance Broker ``/v1/entity-snapshot`` with ``entity_write_key``
  (contract ``contracts/house_brain_entity_snapshot_route.v1.json``).

0.2.5 (2026-10-10, #212 G0): each dry-run and export cycle also reads up to 24 h of state changes for exactly the
read policy's ``history_list`` ids with one GET-only ``/history/period`` request (``minimal_response``,
``no_attributes``) and passes them to the builder as ``history=``; the builder still keeps only exported ids on that
list, applies the name aliases and its bounds. History never fails the snapshot: if the size bound refuses it, fewer
of the newest changes per id are sent; if the read fails or nothing fits, the snapshot is sent without history. The
log line names which (``history_status``) with counts only, never a value.

The sender reads Home Assistant only through the Inspector's GET-only REST and allowlisted WebSocket
registry reads. It makes no service call and has no control authority. Its log lines carry metadata
only (counts, status codes, ``generated_at``), never the key, the Broker URL or the body.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Final
from urllib import parse, request
from urllib.error import HTTPError, URLError

SENDER_SCHEMA: Final = "house_brain_engineering_inspector.entity_sender.v1"
# 0.2.3 (live 2026-10-06): Cloudflare's edge answered 403 (error 1010) to Python's default "Python-urllib"
# user agent before the Broker ran (ENTITY_SNAPSHOT_BROKER_HTTP_ERROR). The POST now names the App, like the
# Maintenance App 0.5.1, the Scout and the Observer. Keep it equal to config.yaml ``version``.
APP_VERSION: Final = "0.2.11"
USER_AGENT: Final = f"house-brain-engineering-inspector/{APP_VERSION}"
OPTIONS_PATH: Final = Path("/data/options.json")
APP_DIR: Final = Path(__file__).resolve().parent
EMBED_MANIFEST: Final = "embedded.MANIFEST.json"
EMBED_LAYOUT_RE: Final = re.compile(r"\A(tools/[a-z0-9_]{1,64}\.py|contracts/[a-z0-9_.]{1,80}\.json)\Z")
MODES: Final = ("off", "dry_run", "export")
ROUTE_PATH: Final = "/v1/entity-snapshot"
PUBLISH_INTERVAL_S: Final = 300
POST_TIMEOUT_S: Final = 15
# Total wall-clock bound for one POST (DNS, connect, TLS, send, status line). Socket timeouts are per
# operation and getaddrinfo has none, so the POST runs in a worker thread joined against this deadline.
POST_DEADLINE_S: Final = 60
MAX_BODY_BYTES: Final = 524288
IDS_PER_LOG_LINE: Final = 100
KEY_RE: Final = re.compile(r"\A[\x21-\x7e]{32,512}\Z")
# 0.2.5: the one Home Assistant history read. GET only, through the Inspector's own ``ha_rest_get``; the path must match
# this pattern exactly (UTC "Z" stamps, so no "+" that a query string would turn into a space) and every id in it must
# be on the embedded read policy's history_list. One read per 5-minute cycle (288/day), REST, so no extra websocket.
HISTORY_WINDOW_S: Final = 86400
MAX_HISTORY_IDS: Final = 16
# Newest changes kept per id when only the snapshot size bound refuses the full history (builder bound is 200).
HISTORY_ROW_CAPS: Final = (100, 50, 20)
HISTORY_SIZE_CODES: Final = frozenset({"SNAPSHOT_SIZE_BOUND", "ENTITY_SENDER_BODY_TOO_LARGE"})
HISTORY_STAMP: Final = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
HISTORY_PATH_RE: Final = re.compile(
    r"\A/history/period/" + HISTORY_STAMP + r"\?filter_entity_id=([a-z0-9_.,]{1,4096})&end_time="
    + HISTORY_STAMP + r"&minimal_response&no_attributes\Z")
ENTITY_ID_RE: Final = re.compile(r"\A[a-z0-9_]{1,64}\.[a-z0-9_]{1,128}\Z")


class SenderError(RuntimeError):
    """Constant reason codes only."""


def _emit(payload: dict[str, Any], out: Callable[[str], None] = print) -> None:
    out(json.dumps({"schema": SENDER_SCHEMA, **payload, "control_authority": "NONE"},
                   sort_keys=True, separators=(",", ":")))


def read_options(path: Path = OPTIONS_PATH) -> dict[str, str]:
    """The three sender options. A missing file means ``off`` (0.1.4 behaviour); anything malformed fails closed."""
    if not path.exists():
        return {"entity_snapshot_mode": "off", "broker_entity_url": "", "entity_write_key": ""}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SenderError("ENTITY_SENDER_OPTIONS_UNREADABLE") from exc
    if type(raw) is not dict:
        raise SenderError("ENTITY_SENDER_OPTIONS_INVALID")
    opts = {
        "entity_snapshot_mode": raw.get("entity_snapshot_mode", "off"),
        "broker_entity_url": raw.get("broker_entity_url") or "",
        "entity_write_key": raw.get("entity_write_key") or "",
    }
    if any(type(v) is not str for v in opts.values()) or opts["entity_snapshot_mode"] not in MODES:
        raise SenderError("ENTITY_SENDER_OPTIONS_INVALID")
    return opts


def validate_export_target(url: str, key: str) -> None:
    # Validate exactly the string urllib will send: printable ASCII only (urlsplit silently drops tab/CR/LF
    # and surrounding spaces), and the URL must round-trip unchanged (no empty "?" or "#").
    try:
        if not re.fullmatch(r"[\x21-\x7e]{1,2048}", url):
            raise ValueError
        parts = parse.urlsplit(url)
        port = parts.port  # raises ValueError on a non-numeric port
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.path != ROUTE_PATH or parts.query or parts.fragment
                or parse.urlunsplit(parts) != url or (port is not None and not 0 < port < 65536)):
            raise ValueError
    except ValueError:
        raise SenderError("ENTITY_SENDER_BROKER_URL_INVALID") from None
    if not KEY_RE.fullmatch(key):
        raise SenderError("ENTITY_SENDER_WRITE_KEY_INVALID")


def materialize_embedded(app_dir: Path = APP_DIR, dest: Path | None = None) -> Path:
    """Verify the flat ``embedded.*`` copies against their manifest and lay them out as ``tools/`` and
    ``contracts/`` in a fresh private directory (the App updater installs flat folders only)."""
    try:
        manifest = json.loads((app_dir / EMBED_MANIFEST).read_text(encoding="utf-8"))
        rows = manifest["files"]
        if manifest.get("schema") != "house_brain_inspector_embedded_sources.v2" or type(rows) is not list or not rows:
            raise ValueError
        if dest is None:
            root = Path(tempfile.mkdtemp(prefix="hb-inspector-embedded-"))
        else:
            root = dest
            root.mkdir(mode=0o700)  # must not exist yet: never write into a pre-existing tree
        os.chmod(root, 0o700)
        for row in rows:
            flat, rel, digest = row["flat"], row["path"], row["sha256"]
            if (not EMBED_LAYOUT_RE.fullmatch(rel) or flat != "embedded." + rel.replace("/", ".", 1)
                    or "/" in flat):
                raise ValueError
            data = (app_dir / flat).read_bytes()
            if hashlib.sha256(data).hexdigest() != digest:
                raise SenderError("ENTITY_SENDER_EMBEDDED_DIGEST_MISMATCH")
            target = root / rel
            target.parent.mkdir(mode=0o700, exist_ok=True)
            with open(target, "xb") as fh:
                fh.write(data)
    except SenderError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SenderError("ENTITY_SENDER_EMBEDDED_INVALID") from exc
    return root


def load_builder(app_dir: Path = APP_DIR) -> Any:
    root = materialize_embedded(app_dir)
    sys.path.insert(0, str(root))
    import tools  # namespace package: must resolve to the verified, materialized copy only
    from tools import build_house_brain_entity_snapshot as builder  # embedded accepted builder
    if ([str(Path(p).resolve()) for p in tools.__path__] != [str((root / "tools").resolve())]
            or Path(builder.__file__).resolve().parent.parent != root.resolve()):
        raise SenderError("ENTITY_SENDER_BUILDER_NOT_EMBEDDED")
    return builder


def utc_now(clock: Callable[[], float] = time.time) -> str:
    return _dt.datetime.fromtimestamp(int(clock()), _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def registry_map(rows: Any) -> dict[str, dict[str, Any]]:
    """entity_id -> {platform, device_id}. Rows the builder could not vouch for are left out, so their
    states are excluded as NOT_IN_REGISTRY (fail closed), instead of refusing the whole snapshot."""
    out: dict[str, dict[str, Any]] = {}
    for row in rows if type(rows) is list else []:
        if type(row) is not dict:
            continue
        eid, platform, device = row.get("entity_id"), row.get("platform"), row.get("device_id")
        if (type(eid) is str and ENTITY_ID_RE.fullmatch(eid) and type(platform) is str
                and (device is None or type(device) is str) and eid not in out):
            out[eid] = {"platform": platform, "device_id": device}
    return out


def state_rows(rows: Any) -> tuple[list[dict[str, Any]], int]:
    """State rows the builder can judge one by one (well-formed unique id, string state, object attributes);
    the others are dropped (counted, never named) so one odd row cannot refuse the whole snapshot."""
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    dropped = 0
    for row in rows if type(rows) is list else []:
        eid = row.get("entity_id") if type(row) is dict else None
        if (type(eid) is not str or not ENTITY_ID_RE.fullmatch(eid) or eid in seen
                or type(row.get("state")) is not str or type(row.get("attributes", {})) is not dict):
            dropped += 1
            continue
        seen.add(eid)
        kept.append(row)
    return kept, dropped


async def collect(rest_get: Callable[[str], Any], ws_command: Callable[[str], Any], *,
                  audit_raw_registry: Callable[[Any], None] | None = None) -> tuple[list, dict, int]:
    states, dropped = state_rows(rest_get("/states"))
    # One existing allowlisted registry read. Evaluate BEFORE registry_map intentionally
    # drops unique_id. The raw rows are never exported or written to App logs.
    registry_rows = await ws_command("config/entity_registry/list")
    if audit_raw_registry is not None:
        audit_raw_registry(registry_rows)
    registry = registry_map(registry_rows)
    return states, registry, dropped


class HistoryRead:
    """Outcome of one history read: rows per policy id (``None`` when nothing usable), counts and a status code."""

    def __init__(self, rows: dict[str, list[dict[str, str]]] | None, status: str, fetched: int = 0,
                 error_code: str | None = None) -> None:
        self.rows, self.status, self.fetched, self.error_code = rows, status, fetched, error_code
        self.row_cap = 0   # newest changes per id actually sent (0: none)


def _stamp(ts: float) -> str:
    return _dt.datetime.fromtimestamp(int(ts), _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _code(exc: BaseException) -> str:
    code = str(exc) if isinstance(exc, (SenderError, ValueError)) else getattr(exc, "code", None)
    return code if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", code) else type(exc).__name__


def history_ids(policy: dict[str, Any]) -> list[str]:
    """The ids the sender may ask Home Assistant history for: the policy's history_list, nothing else."""
    ids = sorted(set(policy.get("history_list") or []))
    if len(ids) > MAX_HISTORY_IDS or not all(type(e) is str and ENTITY_ID_RE.fullmatch(e) for e in ids):
        raise SenderError("ENTITY_HISTORY_LIST_INVALID")
    return ids


def history_path(ids: list[str], end_ts: float) -> str:
    """The exact GET path for 24 h of state changes of ``ids``; refused unless it matches HISTORY_PATH_RE."""
    path = (f"/history/period/{_stamp(end_ts - HISTORY_WINDOW_S)}?filter_entity_id={','.join(ids)}"
            f"&end_time={_stamp(end_ts)}&minimal_response&no_attributes")
    m = HISTORY_PATH_RE.fullmatch(path)
    if not ids or not m or m.group(1).split(",") != ids:
        raise SenderError("ENTITY_HISTORY_PATH_NOT_ALLOWLISTED")
    return path


def history_rows(raw: Any, ids: list[str], max_rows: int, ts_re: Any) -> tuple[dict[str, list[dict[str, str]]], int]:
    """Home Assistant ``/history/period`` answer (one list per entity; with ``minimal_response`` only the first row
    names the entity, the others carry just ``state``/``last_changed``) -> ``{entity_id: [{"at", "state"}]}``, the
    builder's row shape. Lists for an id not asked for are dropped; rows without a string state or a builder-valid
    timestamp are dropped; each id keeps its newest ``max_rows`` rows (the builder's own bound)."""
    if type(raw) is not list:
        raise SenderError("ENTITY_HISTORY_RESPONSE_INVALID")
    wanted = set(ids)
    out: dict[str, list[dict[str, str]]] = {}
    fetched = 0
    for series in raw:
        if type(series) is not list or not series or type(series[0]) is not dict:
            continue
        eid = series[0].get("entity_id")
        if eid not in wanted or eid in out:
            continue
        rows = [{"at": r["last_changed"], "state": r["state"]} for r in series
                if type(r) is dict and type(r.get("last_changed")) is str and type(r.get("state")) is str
                and ts_re.fullmatch(r["last_changed"])]
        fetched += len(rows)
        out[eid] = sorted(rows, key=lambda r: r["at"])[-max_rows:]
    return out, fetched


def read_history(rest_get: Callable[[str], Any], builder: Any, policy: dict[str, Any], end_ts: float) -> HistoryRead:
    """One bounded, GET-only history read. Never raises: any failure is a status code and no history."""
    try:
        ids = history_ids(policy)
        if not ids:
            return HistoryRead(None, "ENTITY_HISTORY_LIST_EMPTY")
        raw = rest_get(history_path(ids, end_ts))
        rows, fetched = history_rows(raw, ids, int(builder.MAX_HISTORY_CHANGES), builder.TS_RE)
        return HistoryRead(rows, "ENTITY_HISTORY_INCLUDED", fetched)
    except Exception as exc:  # noqa: BLE001 - fail soft: the snapshot goes out without history
        return HistoryRead(None, "ENTITY_HISTORY_FETCH_FAILED", error_code=_code(exc))


def build_with_history(builder: Any, states: list, registry: dict, *, now: str, mode: str,
                       hist: HistoryRead) -> tuple[dict[str, Any], bytes | None]:
    """Build (and in export, encode) with history. The live snapshot is ~435 KB of the 512 KiB bound, so when only
    the size bound refuses it, fewer of the newest changes per id are tried (HISTORY_ROW_CAPS,
    ``ENTITY_HISTORY_TRIMMED``); any other refusal, or no cap fitting, builds again without history
    (``ENTITY_HISTORY_NOT_INCLUDED``). History can only ever shrink or be dropped, never fail the snapshot.
    Privacy filtering stays the builder's."""
    if hist.rows is not None:
        longest = max((len(v) for v in hist.rows.values()), default=0)
        caps = [longest] + [c for c in HISTORY_ROW_CAPS if c < longest]
        for cap in caps:
            try:
                rows = {k: v[len(v) - cap:] for k, v in hist.rows.items()}
                result = builder.build(states, registry, now=now, mode=mode, history=rows)
                if mode == "dry_run" and not result["export_size_ok"]:
                    raise SenderError("ENTITY_SENDER_BODY_TOO_LARGE")
                body = encode(result) if mode == "export" else None
                hist.row_cap = cap
                if cap < longest:
                    hist.status = "ENTITY_HISTORY_TRIMMED"
                return result, body
            except (ValueError, SenderError) as exc:
                hist.status, hist.error_code = "ENTITY_HISTORY_NOT_INCLUDED", _code(exc)
                if hist.error_code not in HISTORY_SIZE_CODES:
                    break
    result = builder.build(states, registry, now=now, mode=mode)
    return result, (encode(result) if mode == "export" else None)


def encode(snapshot: dict[str, Any]) -> bytes:
    body = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")
    if len(body) > MAX_BODY_BYTES:
        raise SenderError("ENTITY_SENDER_BODY_TOO_LARGE")
    return body


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _opener() -> request.OpenerDirector:
    return request.build_opener(request.ProxyHandler({}), _NoRedirect())


def _post_once(url: str, key: str, body: bytes, opener: Any) -> int:
    req = request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with (opener or _opener()).open(req, timeout=POST_TIMEOUT_S) as resp:
            return int(resp.status)
    except HTTPError as exc:
        status = int(exc.code)
        exc.close()
        return status
    except (URLError, OSError, ValueError, http.client.HTTPException):
        return 0


def post_snapshot(url: str, key: str, body: bytes, *, opener: Any = None,
                  deadline_s: float = POST_DEADLINE_S) -> int:
    """POST one snapshot; return the HTTP status (0 when no response in time). Never reads or logs the reply body."""
    result: list[int] = []
    worker = threading.Thread(target=lambda: result.append(_post_once(url, key, body, opener)),
                              name="entity-snapshot-post", daemon=True)
    worker.start()
    worker.join(deadline_s)
    return result[0] if result else 0


SEND_OUTCOMES: Final = {204: "ENTITY_SNAPSHOT_STORED", 409: "ENTITY_SNAPSHOT_NOT_NEWER",
                        400: "ENTITY_SNAPSHOT_REJECTED", 401: "ENTITY_SNAPSHOT_UNAUTHORIZED",
                        413: "ENTITY_SNAPSHOT_TOO_LARGE", 0: "ENTITY_SNAPSHOT_BROKER_UNREACHABLE"}


def _history_error(hist: HistoryRead) -> dict[str, str]:
    return {"history_error_code": hist.error_code} if hist.error_code else {}


def run_dry_run(rest_get: Callable, ws_command: Callable, *, builder: Any, clock: Callable[[], float] = time.time,
                out: Callable[[str], None] = print) -> int:
    import asyncio
    states, registry, dropped = asyncio.run(collect(rest_get, ws_command))
    end_ts = clock()
    hist = read_history(rest_get, builder, builder.load_policy(), end_ts)   # 0.2.5: counts only in the report
    report, _ = build_with_history(builder, states, registry, now=utc_now(lambda: end_ts), mode="dry_run", hist=hist)
    ids = report["would_export_entity_ids"]
    _emit({"status": "ENTITY_DRY_RUN_REPORT", "generated_at": report["generated_at"],
           "input_count": report["input_count"], "export_count": report["export_count"],
           "excluded_counts": report["excluded_counts"], "sender_dropped_rows": dropped,
           "history_entity_ids": report["history_entity_ids"], "history_status": hist.status,
           "history_fetched_rows": hist.fetched, **_history_error(hist), "values_included": False,
           "export_bytes": report["export_bytes"], "max_snapshot_bytes": report["max_snapshot_bytes"],
           "export_size_ok": report["export_size_ok"],
           "id_lines": (len(ids) + IDS_PER_LOG_LINE - 1) // IDS_PER_LOG_LINE}, out)
    for n in range(0, len(ids), IDS_PER_LOG_LINE):
        _emit({"status": "ENTITY_DRY_RUN_IDS", "line": n // IDS_PER_LOG_LINE + 1,
               "entity_ids": ids[n:n + IDS_PER_LOG_LINE]}, out)
    return 0


class PersistentRegistrySocket:
    """0.2.4 (owner 2026-10-06, "Fix at the source"): one long-lived, authenticated Core socket for the 5-minute
    registry read. A fresh socket per cycle made ``sensor.connected_clients`` flip every 5 minutes.

    Built from the Inspector's own ``ha_ws_command`` globals, so the URL, token, read-only command allowlist, size
    and row limits and timeouts are exactly the Inspector's. A Core "error" answer keeps the connection; any other
    failure drops it, and the next cycle (5 minutes later) reconnects once.
    """

    def __init__(self, inspector_globals: dict[str, Any]) -> None:
        self._g = inspector_globals
        self._conn: Any = None
        self._next_id = 0
        self.connects = 0

    def _connect(self) -> None:
        from websockets.sync.client import connect  # same pinned websockets==17.1 as the Inspector
        g = self._g
        conn = connect(g["WS_URL"], max_size=g["MAX_HTTP_BODY"], open_timeout=g["WS_TIMEOUT_SECONDS"],
                       close_timeout=5, legacy=True)  # long-lived by design, not a with-block (17.1 API)
        try:
            if json.loads(conn.recv(timeout=g["WS_TIMEOUT_SECONDS"])).get("type") != "auth_required":
                raise g["InspectionError"]("WS_AUTH_REQUIRED_MISSING")
            conn.send(json.dumps({"type": "auth", "access_token": g["_token"]()}))
            if json.loads(conn.recv(timeout=g["WS_TIMEOUT_SECONDS"])).get("type") != "auth_ok":
                raise g["InspectionError"]("WS_AUTH_FAILED")
        except BaseException:
            conn.close()
            raise
        self._conn, self._next_id = conn, 0
        self.connects += 1

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - closing a dead socket is best-effort
                pass

    async def __call__(self, command_type: str) -> Any:
        g = self._g
        error = g["InspectionError"]
        if command_type not in g["READ_ONLY_WS_COMMANDS"]:
            raise error("WS_COMMAND_NOT_ALLOWLISTED")
        try:
            reused = self._conn is not None
            if not reused:
                self._connect()
            try:
                self._next_id += 1
                self._conn.send(json.dumps({"id": self._next_id, "type": command_type}))
            except Exception:
                if not reused:
                    raise
                self.close()                      # a stale socket (Core restarted): reconnect once, same cycle
                self._connect()
                self._next_id += 1
                self._conn.send(json.dumps({"id": self._next_id, "type": command_type}))
            msg_id = self._next_id
            deadline = time.monotonic() + g["WS_TIMEOUT_SECONDS"]
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                response = json.loads(self._conn.recv(timeout=remaining))
                if response.get("id") == msg_id:
                    break
            if response.get("type") != "result" or response.get("success") is not True:
                raise error("WS_READ_COMMAND_FAILED")      # Core answered; the connection is fine
            result = response.get("result")
            if isinstance(result, list) and len(result) > g["MAX_REGISTRY_ROWS"]:
                raise error("WS_RESULT_TOO_LARGE")
            return result
        except error as exc:
            if str(exc) not in ("WS_READ_COMMAND_FAILED", "WS_RESULT_TOO_LARGE"):
                self.close()
            raise
        except TimeoutError as exc:
            self.close()
            raise error("WS_TIMEOUT") from exc
        except Exception as exc:
            self.close()
            raise error("WS_READ_FAILED") from exc


def persistent_ws_command(ws_command: Callable) -> Callable:
    """The production ``inspector.ha_ws_command`` becomes one long-lived socket; anything else (tests) is kept."""
    if getattr(ws_command, "__name__", "") == "ha_ws_command" and "READ_ONLY_WS_COMMANDS" in getattr(
            ws_command, "__globals__", {}):
        return PersistentRegistrySocket(ws_command.__globals__)
    return ws_command


def run_export(rest_get: Callable, ws_command: Callable, opts: dict[str, str], *, builder: Any,
               clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
               opener: Any = None, cycles: int | None = None, out: Callable[[str], None] = print) -> int:
    import asyncio
    policy = builder.load_policy()
    if not all(policy["preconditions_for_real_export"].values()):
        raise SenderError("EXPORT_PRECONDITIONS_NOT_MET")
    validate_export_target(opts["broker_entity_url"], opts["entity_write_key"])
    # Python is PID 1 (direct entrypoint): without a handler the kernel ignores SIGTERM and an App Stop
    # waits for the Supervisor's SIGKILL. Export is the only long-running mode, so only it installs one.
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    ws_command = persistent_ws_command(ws_command)   # 0.2.4: one socket for every cycle
    import registry_probe  # 0.2.9; only exact known identifiers, no extra HA reads
    done = 0
    while cycles is None or done < cycles:
        done += 1
        try:
            family_probe: dict[str, Any] = {"result": "INDETERMINATE",
                                            "entity_id_collisions": None,
                                            "unique_id_collisions": None,
                                            "registry_rows_examined": 0}
            def _audit_registry(rows: Any) -> None:
                try:
                    family_probe.update(registry_probe.summarize(rows))
                except Exception:  # any local probe defect must NOT break accepted entity export
                    family_probe["result"] = "INDETERMINATE"
                    family_probe["entity_id_collisions"] = None
                    family_probe["unique_id_collisions"] = None
                    family_probe["registry_rows_examined"] = 0

            states, registry, dropped = asyncio.run(
                collect(rest_get, ws_command, audit_raw_registry=_audit_registry))
            end_ts = clock()
            hist = read_history(rest_get, builder, policy, end_ts)   # 0.2.5: fail soft, never raises
            snapshot, body = build_with_history(builder, states, registry, now=utc_now(lambda: end_ts),
                                                mode="export", hist=hist)
            status = post_snapshot(opts["broker_entity_url"], opts["entity_write_key"], body, opener=opener)
            history = snapshot["history"]
            _emit({"status": SEND_OUTCOMES.get(status, "ENTITY_SNAPSHOT_BROKER_HTTP_ERROR"), "http_status": status,
                   "entity_count": snapshot["entity_count"], "body_bytes": len(body),
                   "generated_at": snapshot["generated_at"], "sender_dropped_rows": dropped,
                   # Sorted by _emit so these 4 bounded fields appear near the start of
                   # Supervisor log lines and survive Maintenance APP_LOG_WINDOW truncation.
                   "family_registry_result": family_probe["result"],
                   "family_registry_eid_hits": family_probe["entity_id_collisions"],
                   "family_registry_uid_hits": family_probe["unique_id_collisions"],
                   "family_registry_rows": family_probe["registry_rows_examined"],
                   "history_status": hist.status, "history_entity_count": len(history),
                   "history_change_count": sum(len(v) for v in history.values()), **_history_error(hist)}, out)
        except Exception as exc:  # one bounded line per failed cycle; the next cycle retries
            _emit({"status": "ENTITY_SNAPSHOT_CYCLE_FAILED", "error_code": _code(exc)}, out)
        if cycles is None or done < cycles:
            sleep(PUBLISH_INTERVAL_S)
    return 0


def run(opts: dict[str, str], rest_get: Callable, ws_command: Callable, *, builder: Any = None,
        clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep, opener: Any = None,
        cycles: int | None = None, out: Callable[[str], None] = print) -> int:
    """Entry point from ``inspector.main`` when ``entity_snapshot_mode`` is not ``off``. Exit 2 on refusal."""
    try:
        builder = builder or load_builder()
        if opts["entity_snapshot_mode"] == "dry_run":
            return run_dry_run(rest_get, ws_command, builder=builder, clock=clock, out=out)
        if opts["entity_snapshot_mode"] == "export":
            return run_export(rest_get, ws_command, opts, builder=builder, clock=clock, sleep=sleep,
                              opener=opener, cycles=cycles, out=out)
        raise SenderError("ENTITY_SENDER_MODE_OFF")
    except Exception as exc:
        code = str(exc) if isinstance(exc, (SenderError, ValueError)) else getattr(exc, "code", None)
        _emit({"status": "ENTITY_SENDER_REFUSED", "mode": opts["entity_snapshot_mode"],
               "error_code": code if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", code)
               else type(exc).__name__}, out)
        return 2
