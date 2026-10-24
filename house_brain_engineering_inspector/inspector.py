#!/usr/bin/env python3
"""House Brain Engineering Inspector R1.

Read-only Home Assistant inspection producer:
- GET-only Home Assistant REST reads (proxy-free, redirect-free, bounded by a total response
  deadline; failures keep their status class, v0.1.4).
- Read-only WebSocket registry list commands.
- Metadata-only package inventory; package contents are never exported.
- No service calls, shell, YAML writes, raw .storage reads, or physical control.
- v0.2.0 (#212 G0): opt-in entity snapshot sender (``entity_sender.py``, App option
  ``entity_snapshot_mode``: off by default, dry_run, export). See that module.

Runtime permissions are provided only by a separately reviewed Home Assistant
App manifest. Repository acceptance alone does not commission this component.
"""
from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import os
from pathlib import Path
import stat
import sys
import time
from typing import Any, Callable
from urllib import request
from urllib.error import HTTPError, URLError

SCHEMA = "house_brain_engineering_inspection.v1"
SOURCE = "house_brain_engineering_inspector"
REST_BASE = "http://supervisor/core/api"
WS_URL = "ws://supervisor/core/websocket"
DEFAULT_PACKAGES_ROOT = Path("/homeassistant/packages")

MAX_HTTP_BODY = 8 * 1024 * 1024
MAX_REGISTRY_ROWS = 10_000
MAX_PACKAGE_ROWS = 4_096
HTTP_TIMEOUT_SECONDS = 15
WS_TIMEOUT_SECONDS = 20
# v0.1.4 (#91 GAP-91-06, Inspector half): urllib timeouts are per socket operation, so a peer
# trickling bytes could hold a REST read open far beyond HTTP_TIMEOUT_SECONDS. Every REST body is
# read against this total deadline, measured from the request start (the Observer's 30 s value);
# worst case per read = deadline + one per-operation timeout.
REST_RESPONSE_DEADLINE_SECONDS = 30.0
REST_READ_CHUNK_BYTES = 65536
_monotonic = time.monotonic

# REST failure codes (v0.1.4). Each keeps the status class a consumer needs to tell auth expiry from
# rate limiting from an outage: HA_REST_AUTH_FAILED (401/403), HA_REST_NOT_FOUND (404),
# HA_REST_RATE_LIMITED (429), HA_REST_SERVER_ERROR (5xx), HA_REST_TIMEOUT (connect/read timeout or the
# total deadline), HA_REST_UNAVAILABLE (refused, DNS, TLS, reset, closed), HA_REST_PROTOCOL_ERROR (bad
# status line, oversized header, truncated or short body); any other HTTP status keeps the existing
# HA_HTTP_<status> family (e.g. a refused redirect). Before v0.1.4 every one of these collapsed to
# HA_REST_READ_FAILED; that code stays the fallback for an unclassified failure and is carried by every
# REST failure as ``InspectionError.legacy_code`` (the exact code v0.1.3 raised), so older consumers keep
# working.
REST_FAILURE_CODES = frozenset({
    "HA_REST_READ_FAILED", "HA_REST_AUTH_FAILED", "HA_REST_NOT_FOUND", "HA_REST_RATE_LIMITED",
    "HA_REST_SERVER_ERROR", "HA_REST_TIMEOUT", "HA_REST_UNAVAILABLE", "HA_REST_PROTOCOL_ERROR",
})
LEGACY_REST_FAILURE_CODE = "HA_REST_READ_FAILED"

BLOCKED_DOMAINS = frozenset({
    "alarm_control_panel", "camera", "device_tracker", "geo_location",
    "image", "lock", "person", "zone",
})

SAFE_STATE_ATTRIBUTES = frozenset({
    "friendly_name", "device_class", "unit_of_measurement", "state_class",
    "icon", "supported_features", "assumed_state", "restored",
})

READ_ONLY_WS_COMMANDS = frozenset({
    "config/entity_registry/list",
    "config/device_registry/list",
    "config/area_registry/list",
})


class InspectionError(RuntimeError):
    """Fail-closed inspector error.

    ``str(error)`` is the bounded error code. REST read failures (v0.1.4) also carry
    ``http_status`` (int or None) and ``legacy_code``: the code v0.1.3 raised for the same failure
    (``HA_REST_READ_FAILED`` for every class split out in v0.1.4), for backward-compatible matching.
    """

    def __init__(self, code: str, *, http_status: int | None = None, legacy_code: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status
        self.legacy_code = legacy_code if legacy_code is not None else code


class RestDeadlineExceeded(Exception):
    """A REST body was still arriving when the total response deadline passed."""


class _NoRedirect(request.HTTPRedirectHandler):
    """Refuse redirects: the Supervisor token must never follow a redirect to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


# v0.1.4: proxy-free, redirect-free opener (as the Observer and Scout use). A redirect surfaces as
# ``HA_HTTP_<3xx>`` instead of re-sending the bearer token; environment proxies are ignored.
_HA_OPENER = request.build_opener(request.ProxyHandler({}), _NoRedirect())


def read_bounded(response: Any, limit: int, deadline_at: float, *,
                 clock: Callable[[], float] | None = None) -> bytes:
    """Read at most ``limit + 1`` body bytes, failing once ``deadline_at`` (monotonic) has passed.

    Real ``http.client.HTTPResponse`` objects are read with ``read1`` (at most one socket read per
    call), so the deadline is checked between socket reads. A body that ends before its declared
    ``Content-Length`` raises ``http.client.IncompleteRead`` (the partial body is never parsed).
    Objects without ``read1`` are read with one bounded ``read`` call.
    """
    now = clock if clock is not None else _monotonic
    read1 = getattr(response, "read1", None)
    if not callable(read1):
        if now() > deadline_at:
            raise RestDeadlineExceeded()
        return response.read(limit + 1)
    chunks: list[bytes] = []
    total = 0
    while total <= limit:
        if now() > deadline_at:
            raise RestDeadlineExceeded()
        chunk = read1(min(REST_READ_CHUNK_BYTES, limit + 1 - total))
        if not chunk:
            remaining = getattr(response, "length", None)
            if isinstance(remaining, int) and not isinstance(remaining, bool) and remaining > 0:
                raise http.client.IncompleteRead(b"", remaining)
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def rest_status_code(status: int) -> str:
    """Bounded error code for an HTTP error status (v0.1.4, GAP-91-06)."""
    if status in (401, 403):
        return "HA_REST_AUTH_FAILED"
    if status == 404:
        return "HA_REST_NOT_FOUND"
    if status == 429:
        return "HA_REST_RATE_LIMITED"
    if 500 <= status <= 599:
        return "HA_REST_SERVER_ERROR"
    return f"HA_HTTP_{status}"


def rest_transport_code(err: BaseException) -> str:
    """Bounded error code for a non-status REST failure (v0.1.4, GAP-91-06)."""
    if isinstance(err, RestDeadlineExceeded):
        return "HA_REST_TIMEOUT"
    if isinstance(err, TimeoutError) or (isinstance(err, URLError) and isinstance(err.reason, TimeoutError)):
        return "HA_REST_TIMEOUT"
    if isinstance(err, (URLError, OSError)):
        return "HA_REST_UNAVAILABLE"
    if isinstance(err, http.client.HTTPException):
        return "HA_REST_PROTOCOL_ERROR"
    return "HA_REST_READ_FAILED"


def _token() -> str:
    token = os.environ.get("SUPERVISOR_TOKEN", "").strip()
    if not token:
        raise InspectionError("SUPERVISOR_TOKEN_MISSING")
    return token


def _bounded_json_loads(raw: bytes, *, limit: int = MAX_HTTP_BODY) -> Any:
    if len(raw) > limit:
        raise InspectionError("RESPONSE_TOO_LARGE")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InspectionError("INVALID_JSON") from exc
    except (ValueError, RecursionError) as exc:
        # v0.1.4: invalid UTF-8 / a nesting bomb is invalid JSON too (v0.1.3 raised HA_REST_READ_FAILED).
        raise InspectionError("INVALID_JSON", legacy_code=LEGACY_REST_FAILURE_CODE) from exc


def ha_rest_get(path: str) -> Any:
    """Perform a GET-only request through the Supervisor Core API proxy."""
    if not path.startswith("/") or ".." in path:
        raise InspectionError("INVALID_REST_PATH")
    req = request.Request(
        f"{REST_BASE}{path}",
        method="GET",
        headers={"Authorization": f"Bearer {_token()}", "Accept": "application/json"},
    )
    started = _monotonic()
    try:
        with _HA_OPENER.open(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            status = getattr(resp, "status", 200)
            if status != 200:
                raise InspectionError(f"HA_HTTP_{status}", http_status=status if isinstance(status, int) else None)
            raw = read_bounded(resp, MAX_HTTP_BODY, started + REST_RESPONSE_DEADLINE_SECONDS)
    except InspectionError:
        raise
    except HTTPError as exc:
        # HTTPError is also a URLError/OSError: classify by its status before transport errors.
        raise InspectionError(rest_status_code(exc.code), http_status=exc.code,
                              legacy_code=LEGACY_REST_FAILURE_CODE) from exc
    except Exception as exc:
        raise InspectionError(rest_transport_code(exc), legacy_code=LEGACY_REST_FAILURE_CODE) from exc
    return _bounded_json_loads(raw, limit=MAX_HTTP_BODY)


async def ha_ws_command(command_type: str) -> Any:
    """Run one exact allowlisted read-only HA WebSocket registry command."""
    if command_type not in READ_ONLY_WS_COMMANDS:
        raise InspectionError("WS_COMMAND_NOT_ALLOWLISTED")
    try:
        import websockets  # type: ignore[import-not-found]
    except ImportError as exc:
        raise InspectionError("WEBSOCKETS_DEPENDENCY_MISSING") from exc

    try:
        async with asyncio.timeout(WS_TIMEOUT_SECONDS):
            async with websockets.connect(
                WS_URL,
                max_size=MAX_HTTP_BODY,
                open_timeout=WS_TIMEOUT_SECONDS,
                close_timeout=5,
            ) as ws:
                hello = json.loads(await ws.recv())
                if hello.get("type") != "auth_required":
                    raise InspectionError("WS_AUTH_REQUIRED_MISSING")
                await ws.send(json.dumps({"type": "auth", "access_token": _token()}))
                auth = json.loads(await ws.recv())
                if auth.get("type") != "auth_ok":
                    raise InspectionError("WS_AUTH_FAILED")
                msg_id = 1
                await ws.send(json.dumps({"id": msg_id, "type": command_type}))
                while True:
                    response = json.loads(await ws.recv())
                    if response.get("id") != msg_id:
                        continue
                    if response.get("type") != "result" or response.get("success") is not True:
                        raise InspectionError("WS_READ_COMMAND_FAILED")
                    result = response.get("result")
                    if isinstance(result, list) and len(result) > MAX_REGISTRY_ROWS:
                        raise InspectionError("WS_RESULT_TOO_LARGE")
                    return result
    except InspectionError:
        raise
    except TimeoutError as exc:
        raise InspectionError("WS_TIMEOUT") from exc
    except Exception as exc:
        raise InspectionError("WS_READ_FAILED") from exc


def _domain(entity_id: str) -> str:
    return entity_id.partition(".")[0].lower()


def _safe_scalar(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return None


def sanitize_state(row: dict[str, Any]) -> dict[str, Any] | None:
    entity_id = str(row.get("entity_id", ""))
    if not entity_id or _domain(entity_id) in BLOCKED_DOMAINS:
        return None
    attributes = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
    safe_attrs: dict[str, Any] = {}
    for key in SAFE_STATE_ATTRIBUTES:
        if key in attributes:
            value = _safe_scalar(attributes[key])
            if value is not None:
                safe_attrs[key] = value
    return {
        "entity_id": entity_id,
        "state": _safe_scalar(row.get("state")),
        "last_changed": _safe_scalar(row.get("last_changed")),
        "last_updated": _safe_scalar(row.get("last_updated")),
        "attributes": safe_attrs,
    }


def sanitize_entity(row: dict[str, Any]) -> dict[str, Any] | None:
    entity_id = str(row.get("entity_id", ""))
    if not entity_id or _domain(entity_id) in BLOCKED_DOMAINS:
        return None
    keys = (
        "entity_id", "name", "original_name", "platform", "device_id", "area_id",
        "disabled_by", "hidden_by", "entity_category", "has_entity_name",
        "translation_key",
    )
    return {key: _safe_scalar(row.get(key)) for key in keys}


def sanitize_device(
    row: dict[str, Any],
    *,
    allowed_device_ids: set[str] | None = None,
    blocked_device_ids: set[str] | None = None,
) -> dict[str, Any] | None:
    """Sanitize one device, excluding devices tied to blocked entity domains."""
    device_id = str(row.get("id", ""))
    if not device_id:
        return None
    if blocked_device_ids and device_id in blocked_device_ids:
        return None
    if allowed_device_ids is not None and device_id not in allowed_device_ids:
        return None
    keys = (
        "id", "name", "name_by_user", "manufacturer", "model", "model_id",
        "sw_version", "hw_version", "area_id", "disabled_by", "entry_type",
        "config_entry_id", "config_subentry_id", "parent_device_id",
    )
    return {key: _safe_scalar(row.get(key)) for key in keys}


def sanitize_area(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "area_id": _safe_scalar(row.get("area_id") or row.get("id")),
        "name": _safe_scalar(row.get("name")),
    }


def inventory_packages(root: Path = DEFAULT_PACKAGES_ROOT) -> list[dict[str, Any]]:
    """Return metadata-only, no-follow inventory of top-level package files."""
    try:
        root = root.resolve(strict=True)
    except FileNotFoundError as exc:
        raise InspectionError("PACKAGES_ROOT_MISSING") from exc
    if not root.is_dir():
        raise InspectionError("PACKAGES_ROOT_NOT_DIRECTORY")

    rows: list[dict[str, Any]] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
        if len(rows) >= MAX_PACKAGE_ROWS:
            raise InspectionError("PACKAGE_RESULT_TOO_LARGE")
        try:
            st = entry.lstat()
        except OSError as exc:
            raise InspectionError("PACKAGE_STAT_FAILED") from exc
        if stat.S_ISLNK(st.st_mode):
            rows.append({
                "name": entry.name, "kind": "symlink_rejected", "size": st.st_size,
                "mtime_ns": st.st_mtime_ns, "sha256": None,
            })
            continue
        if not stat.S_ISREG(st.st_mode):
            continue

        lower = entry.name.casefold()
        active = lower.endswith((".yaml", ".yml"))
        retained = (
            lower.endswith((".bak", ".disabled", ".backup"))
            or ".bak." in lower or ".disabled." in lower
        )
        kind = "active_yaml" if active else ("retained_backup" if retained else "other_file")
        digest = None
        if active or retained:
            h = hashlib.sha256()
            try:
                with entry.open("rb") as fh:
                    while chunk := fh.read(1024 * 1024):
                        h.update(chunk)
            except OSError as exc:
                raise InspectionError("PACKAGE_HASH_FAILED") from exc
            digest = h.hexdigest()

        rows.append({
            "name": entry.name, "kind": kind, "size": st.st_size,
            "mtime_ns": st.st_mtime_ns, "sha256": digest,
        })
    return rows


def _bounded_list(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise InspectionError(f"{label}_NOT_LIST")
    if len(value) > MAX_REGISTRY_ROWS:
        raise InspectionError(f"{label}_TOO_LARGE")
    return [row for row in value if isinstance(row, dict)]


async def build_snapshot(packages_root: Path = DEFAULT_PACKAGES_ROOT) -> dict[str, Any]:
    config = ha_rest_get("/config")
    states_raw = _bounded_list(ha_rest_get("/states"), "STATES")
    entities_raw, devices_raw, areas_raw = await asyncio.gather(
        ha_ws_command("config/entity_registry/list"),
        ha_ws_command("config/device_registry/list"),
        ha_ws_command("config/area_registry/list"),
    )
    entity_rows = _bounded_list(entities_raw, "ENTITIES")
    blocked_device_ids = {
        str(row.get("device_id"))
        for row in entity_rows
        if row.get("device_id")
        and _domain(str(row.get("entity_id", ""))) in BLOCKED_DOMAINS
    }
    entities = [
        clean for row in entity_rows
        if (clean := sanitize_entity(row)) is not None
    ]
    allowed_device_ids = {
        str(row.get("device_id"))
        for row in entities
        if row.get("device_id")
    }
    devices = [
        clean
        for row in _bounded_list(devices_raw, "DEVICES")
        if (
            clean := sanitize_device(
                row,
                allowed_device_ids=allowed_device_ids,
                blocked_device_ids=blocked_device_ids,
            )
        ) is not None
    ]
    areas = [sanitize_area(row) for row in _bounded_list(areas_raw, "AREAS")]
    states = [clean for row in states_raw if (clean := sanitize_state(row)) is not None]

    return {
        "schema": SCHEMA,
        "source": {
            "component": SOURCE,
            "generated_at_epoch": int(time.time()),
            "mutation": False,
            "physical_control": False,
            "raw_storage_access": False,
            "service_calls": False,
            "untrusted_text": True,
        },
        "core": {
            "version": _safe_scalar(config.get("version")) if isinstance(config, dict) else None,
            "state": _safe_scalar(config.get("state")) if isinstance(config, dict) else None,
            "unit_system": (
                config.get("unit_system")
                if isinstance(config, dict) and isinstance(config.get("unit_system"), dict)
                else {}
            ),
        },
        "counts": {
            "states": len(states), "entities": len(entities),
            "devices": len(devices), "areas": len(areas),
        },
        "states": states,
        "entities": entities,
        "devices": devices,
        "areas": areas,
        "packages": inventory_packages(packages_root),
    }


def main() -> int:
    # v0.2.0 (#212 G0): the opt-in entity snapshot sender. With no options file or
    # entity_snapshot_mode "off" (the default) the 0.1.4 one-shot inspection below runs unchanged.
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import entity_sender
        options = entity_sender.read_options()
    except Exception as exc:  # unreadable options or a missing sender module: fail closed, one bounded line
        code = str(exc) if type(exc).__name__ == "SenderError" else "ENTITY_SENDER_UNAVAILABLE"
        print(json.dumps({"schema": SCHEMA, "status": "READ_ONLY_INSPECTION_FAILED", "error_code": code,
                          "legacy_error_code": None, "http_status": None,
                          "mutation": False, "physical_control": False}, sort_keys=True, separators=(",", ":")))
        return 1
    if options["entity_snapshot_mode"] != "off":
        if options["entity_snapshot_mode"] == "r18_preflight":
            import cooling_r18_site_probe
            return cooling_r18_site_probe.main()
        return entity_sender.run(options, ha_rest_get, ha_ws_command)
    try:
        snapshot = asyncio.run(build_snapshot())
    except InspectionError as exc:
        # v0.1.4: one bounded, metadata-only failure line instead of a traceback; exit status stays 1.
        print(json.dumps({
            "schema": SCHEMA,
            "status": "READ_ONLY_INSPECTION_FAILED",
            "error_code": exc.code,
            "legacy_error_code": exc.legacy_code,
            "http_status": exc.http_status,
            "mutation": False,
            "physical_control": False,
        }, sort_keys=True, separators=(",", ":")))
        return 1
    # Never dump owner-private state/entity/device data into App logs.
    print(json.dumps({
        "schema": snapshot["schema"],
        "status": "READ_ONLY_INSPECTION_PASS",
        "counts": snapshot["counts"],
        "package_count": len(snapshot["packages"]),
        "mutation": False,
        "physical_control": False,
    }, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
