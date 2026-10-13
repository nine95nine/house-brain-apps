#!/usr/bin/env python3
"""Bounded authenticated broker worker for the Engineering Read Gateway v0.2.2.

Only two default-role read operations are executable. The worker never invokes
Home Assistant services, Supervisor mutation endpoints, shell commands, Docker,
or physical control.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

try:
    from . import observer_supervisor as _bounded
    from .app_inventory import collect_app_info
    from .tailscale_identity import collect_tailscale_identity
except ImportError:
    import observer_supervisor as _bounded
    from app_inventory import collect_app_info
    from tailscale_identity import collect_tailscale_identity

MAX_RESULT_BYTES = 32768
MAX_RESPONSE_BYTES = 65536
MIN_IDLE_POLL_SECONDS = 1.0
MAX_IDLE_POLL_SECONDS = 8.0
HTTP_TIMEOUT_SECONDS = 10
# App 0.2.4 (GAP-91-04): total Broker response deadline, measured from the request start.
RESPONSE_DEADLINE_SECONDS = 30.0
BROKER_HOST_RE = re.compile(r"^house-brain-maintenance-broker\.[a-z0-9-]+\.workers\.dev$", re.I)
SECRET_RE = re.compile(r"^[!-~]{32,512}$")
ACTIVE_OPERATIONS = frozenset({
    "ha.app.info",
    "ha.network.tailscale.identity",
})


class ReadWorkerError(RuntimeError):
    """Fail-closed worker error whose message is safe for local diagnostics."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_OPENER = build_opener(ProxyHandler({}), _NoRedirect())


def _broker_origin(snapshot_url: str) -> str:
    if not isinstance(snapshot_url, str) or snapshot_url != snapshot_url.strip():
        raise ReadWorkerError("invalid_validated_snapshot_url")
    try:
        parsed = urlparse(snapshot_url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as err:
        raise ReadWorkerError("invalid_validated_snapshot_url") from err
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.params
        or parsed.path != "/v1/snapshot"
        or parsed.query
        or parsed.fragment
        or port not in (None, 443)
        or not BROKER_HOST_RE.fullmatch(hostname)
    ):
        raise ReadWorkerError("invalid_validated_snapshot_url")
    suffix = ":443" if port == 443 else ""
    return f"https://{hostname}{suffix}"


def _http_json(
    method: str,
    url: str,
    key: str,
    payload: dict[str, Any] | None = None,
    *,
    opener=_OPENER,
) -> dict[str, Any]:
    if not isinstance(key, str) or not SECRET_RE.fullmatch(key):
        raise ReadWorkerError("broker_write_key_missing")
    body = None if payload is None else json.dumps(
        payload, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    if body is not None and len(body) > MAX_RESULT_BYTES:
        raise ReadWorkerError("read_result_too_large")
    request = Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if body is not None else {}),
            "User-Agent": "house-brain-engineering-observer/read-v0.2.2",
        },
    )
    started = _bounded._monotonic()
    try:
        with opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            if response.status < 200 or response.status >= 300:
                raise ReadWorkerError(f"broker_http_{response.status}")
            raw = _bounded.read_bounded(response, MAX_RESPONSE_BYTES, started + RESPONSE_DEADLINE_SECONDS)
    except HTTPError as err:
        raise ReadWorkerError(f"broker_http_{err.code}") from err
    except _bounded.TRANSPORT_ERRORS as err:  # GAP-91-01/04/06: contained, timeout split out
        raise ReadWorkerError(f"broker_{_bounded.transport_error_code(err)}") from err
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ReadWorkerError("broker_response_too_large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except _bounded.JSON_DECODE_ERRORS as err:  # GAP-91-02: RecursionError contained
        raise ReadWorkerError("broker_invalid_json") from err
    if not isinstance(value, dict):
        raise ReadWorkerError("broker_invalid_shape")
    return value


def _parse_expiry(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ReadWorkerError("request_expiry_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as err:
        raise ReadWorkerError("request_expiry_invalid") from err
    if parsed.tzinfo is None:
        raise ReadWorkerError("request_expiry_invalid")
    return parsed.astimezone(timezone.utc)


def _slug_arg(args: dict[str, Any]) -> str:
    if set(args) != {"slug"} or not isinstance(args.get("slug"), str):
        raise ReadWorkerError("request_args_invalid")
    return args["slug"]


def execute_request(
    request: dict[str, Any],
    supervisor_token: str,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise ReadWorkerError("request_invalid")
    operation = request.get("operation")
    args = request.get("args", {})
    if not isinstance(operation, str) or operation not in ACTIVE_OPERATIONS or not isinstance(args, dict):
        raise ReadWorkerError("operation_not_active")
    if _parse_expiry(request.get("expires_at")) <= now():
        raise ReadWorkerError("request_expired")

    if operation == "ha.app.info":
        return collect_app_info(_slug_arg(args), supervisor_token)
    if operation == "ha.network.tailscale.identity":
        if args:
            raise ReadWorkerError("request_args_invalid")
        return collect_tailscale_identity(supervisor_token)
    raise ReadWorkerError("operation_not_active")


def worker_iteration(snapshot_url: str, write_key: str, supervisor_token: str) -> bool:
    origin = _broker_origin(snapshot_url)
    polled = _http_json("GET", origin + "/v1/read/poll", write_key)
    candidate = polled.get("request")
    if candidate is None:
        return False
    if not isinstance(candidate, dict):
        raise ReadWorkerError("broker_request_invalid")
    request_id = candidate.get("id")
    if not isinstance(request_id, str):
        raise ReadWorkerError("broker_request_invalid")

    try:
        result = execute_request(candidate, supervisor_token)
        envelope = {"id": request_id, "ok": True, "result": result, "error": None}
    except Exception as err:  # Deliberately never return exception text remotely.
        print(f"engineering_read_execution=failed type={type(err).__name__}", flush=True)
        envelope = {"id": request_id, "ok": False, "result": None, "error": "read_failed"}
    _http_json("POST", origin + "/v1/read/result", write_key, envelope)
    return True


def worker_loop(
    snapshot_url: str,
    write_key: str,
    supervisor_token: str,
    stop: threading.Event | None = None,
) -> None:
    stop = stop or threading.Event()
    idle_delay = MIN_IDLE_POLL_SECONDS
    while not stop.is_set():
        try:
            did_work = worker_iteration(snapshot_url, write_key, supervisor_token)
        except ReadWorkerError as err:
            print(f"engineering_read_poll=failed reason={err}", flush=True)
            did_work = False
        except Exception as err:  # noqa: BLE001 - GAP-91-03: the read plane must never die silently
            print(f"engineering_read_poll=failed type={type(err).__name__}", flush=True)
            did_work = False
        if did_work:
            idle_delay = MIN_IDLE_POLL_SECONDS
            stop.wait(0.2)
        else:
            stop.wait(idle_delay)
            idle_delay = min(MAX_IDLE_POLL_SECONDS, idle_delay * 2.0)
