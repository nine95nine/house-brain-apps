"Read-only Supervisor info client and sanitizers for Engineering Observer v0.2."

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import http.client
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

OBSERVER_VERSION = "0.2.2"
SUPERVISOR_BASE = "http://supervisor"
HTTP_TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 262144
# App 0.2.4 (GAP-91-04): urllib timeouts are per socket operation, so a peer trickling bytes could
# hold a read open indefinitely. Every response body is now read against this total deadline
# (measured from the request start); worst case = deadline + one per-operation timeout.
RESPONSE_DEADLINE_SECONDS = 30.0
READ_CHUNK_BYTES = 65536
SECRET_RE = re.compile(r"^[!-~]{32,512}$")
# C0 controls and DEL: the Maintenance Broker (F2 control) rejects a whole snapshot carrying any.
CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_monotonic = time.monotonic


class ResponseDeadlineExceeded(Exception):
    """A response body was still arriving when the total response deadline passed."""


def read_bounded(response: Any, limit: int, deadline_at: float, *, clock: Callable[[], float] | None = None) -> bytes:
    """Read at most ``limit + 1`` body bytes, failing once ``deadline_at`` (monotonic) has passed.

    Real ``http.client.HTTPResponse`` objects are read with ``read1`` (at most one socket read per
    call), so the deadline is checked between socket reads. A body that ends before its declared
    ``Content-Length`` raises ``http.client.IncompleteRead`` (never carrying the partial body).
    Objects without ``read1`` are read with one bounded ``read`` call.
    """
    now = clock if clock is not None else _monotonic
    read1 = getattr(response, "read1", None)
    if not callable(read1):
        if now() > deadline_at:
            raise ResponseDeadlineExceeded()
        return response.read(limit + 1)
    chunks: list[bytes] = []
    total = 0
    while total <= limit:
        if now() > deadline_at:
            raise ResponseDeadlineExceeded()
        chunk = read1(min(READ_CHUNK_BYTES, limit + 1 - total))
        if not chunk:
            remaining = getattr(response, "length", None)
            if isinstance(remaining, int) and not isinstance(remaining, bool) and remaining > 0:
                raise http.client.IncompleteRead(b"", remaining)
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def transport_error_code(err: BaseException) -> str:
    """Bounded code for a non-HTTP-status transport failure (GAP-91-01/04/06).

    ``timeout`` is split from ``unavailable`` (refused, DNS, TLS, reset); ``http.client`` protocol
    errors that are not ``OSError`` (IncompleteRead, BadStatusLine, LineTooLong) are contained as
    ``protocol_error`` instead of escaping the caller.
    """
    if isinstance(err, ResponseDeadlineExceeded):
        return "deadline_timeout"
    if isinstance(err, TimeoutError) or (isinstance(err, URLError) and isinstance(err.reason, TimeoutError)):
        return "timeout"
    if isinstance(err, (URLError, OSError)):
        return "unavailable"
    return "protocol_error"


TRANSPORT_ERRORS = (ResponseDeadlineExceeded, URLError, OSError, http.client.HTTPException)
JSON_DECODE_ERRORS = (ValueError, RecursionError)  # ValueError covers JSONDecodeError/UnicodeDecodeError


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_SUPERVISOR_OPENER = build_opener(ProxyHandler({}), _NoRedirect())


def _supervisor_open(request: Request, timeout: int):
    return _SUPERVISOR_OPENER.open(request, timeout=timeout)


STATIC_INFO_PATHS = frozenset({
    "/info",
    "/supervisor/info",
    "/core/info",
    "/os/info",
    "/jobs/info",
    "/backups/info",
    "/resolution/info",
    "/security/info",
    "/addons/self/info",
})
ADDON_INFO_RE = re.compile(r"^/addons/[A-Za-z0-9_.-]{1,128}/info$")


def path_is_allowlisted(path: str) -> bool:
    """Return true only for explicit read-only Supervisor JSON endpoints."""
    return (
        path in STATIC_INFO_PATHS
        or bool(ADDON_INFO_RE.fullmatch(path))
    )


def supervisor_get(
    path: str,
    token: str,
    *,
    opener=_supervisor_open,
) -> tuple[dict[str, Any] | None, str | None]:
    """GET an explicitly allowlisted Supervisor JSON endpoint."""
    if not path_is_allowlisted(path):
        return None, "path_not_allowlisted"
    if not isinstance(token, str) or not SECRET_RE.fullmatch(token):
        return None, "supervisor_token_missing"
    request = Request(
        SUPERVISOR_BASE + path,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": f"house-brain-engineering-observer/{OBSERVER_VERSION}",
        },
    )
    started = _monotonic()
    try:
        with opener(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                return None, f"http_{response.status}"
            raw = read_bounded(response, MAX_RESPONSE_BYTES, started + RESPONSE_DEADLINE_SECONDS)
    except HTTPError as err:
        return None, f"http_{err.code}"
    except TRANSPORT_ERRORS as err:
        return None, transport_error_code(err)
    if len(raw) > MAX_RESPONSE_BYTES:
        return None, "response_too_large"
    try:
        body = json.loads(raw)
    except JSON_DECODE_ERRORS:
        return None, "malformed_json"
    if not isinstance(body, dict) or body.get("result") != "ok" or not isinstance(body.get("data"), dict):
        return None, "unexpected_envelope"
    return body["data"], None


def pick(data: dict[str, Any] | None, key: str, expected: type | tuple[type, ...] | None = None) -> Any:
    if not isinstance(data, dict) or key not in data:
        return None
    value = data[key]
    if expected is not None and not isinstance(value, expected):
        return None
    return value


def bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


_CONTROL_AUDIT: ContextVar[list[int] | None] = ContextVar("observer_control_char_audit", default=None)


@contextmanager
def control_char_audit() -> Iterator[list[int]]:
    """Count strings rejected by ``str_or_none`` for control characters (per thread/context)."""
    hits: list[int] = []
    token = _CONTROL_AUDIT.set(hits)
    try:
        yield hits
    finally:
        _CONTROL_AUDIT.reset(token)


def str_or_none(value: Any, max_len: int = 128) -> str | None:
    if not isinstance(value, str) or not 0 < len(value) <= max_len:
        return None
    if CONTROL_CHAR_RE.search(value):
        # GAP-91-07: never forward C0/DEL (the Broker would reject the whole snapshot); the caller
        # records the affected section in data_quality through control_char_audit().
        audit = _CONTROL_AUDIT.get()
        if audit is not None:
            audit.append(1)
        return None
    return value


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def derive_backup_readiness(backups: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    candidates: list[datetime] = []
    entries = backups.get("backups") if isinstance(backups, dict) else None
    if isinstance(entries, list):
        for item in entries:
            if not isinstance(item, dict):
                continue
            parsed = parse_timestamp(item.get("date"))
            if parsed:
                candidates.append(parsed)
    latest = max(candidates) if candidates else None
    age_hours = round(max(0.0, (now - latest).total_seconds()) / 3600.0, 2) if latest else None
    days_until_stale = int_or_none(backups.get("days_until_stale")) if isinstance(backups, dict) else None
    fresh_by_policy = (
        age_hours is not None
        and days_until_stale is not None
        and age_hours <= days_until_stale * 24.0
    )
    return {
        "backup_count": len(candidates),
        "latest_backup_age_hours": age_hours,
        "days_until_stale": days_until_stale,
        "fresh_by_supervisor_policy": fresh_by_policy,
    }


def derive_jobs(jobs: dict[str, Any] | None) -> dict[str, Any]:
    roots = jobs.get("jobs") if isinstance(jobs, dict) else None
    queue = list(roots) if isinstance(roots, list) else []
    active = 0
    while queue:
        item = queue.pop(0)
        if not isinstance(item, dict):
            continue
        if item.get("done") is False:
            active += 1
        children = item.get("child_jobs")
        if isinstance(children, list):
            queue.extend(children)
    return {"active_job_count": active, "supervisor_busy": active > 0}


def derive_resolution(data: dict[str, Any] | None) -> dict[str, Any]:
    issues = pick(data, "issues", list) or []
    unsupported = pick(data, "unsupported", list) or []
    unhealthy = pick(data, "unhealthy", list) or []
    types: set[str] = set()
    for issue in issues:
        if isinstance(issue, dict):
            for key in ("type", "category"):
                value = str_or_none(issue.get(key), 64)
                if value:
                    types.add(value)
                    break
    return {
        "issue_count": len(issues),
        "issue_types": sorted(types)[:16],
        "unsupported_count": len(unsupported),
        "unhealthy_count": len(unhealthy),
        # GAP-91-09: health is asserted only from an observed resolution object. The Broker schema
        # requires a boolean, so an unobserved resolution reports False (never a default True) and
        # the snapshot names it in data_quality (RESOLUTION_FACTS_UNOBSERVED).
        "healthy": isinstance(data, dict) and len(issues) == 0 and len(unsupported) == 0 and len(unhealthy) == 0,
    }


def derive_security(data: dict[str, Any] | None) -> dict[str, Any]:
    """Return factual Supervisor security-policy flags; do not invent a health state."""
    return {
        "pwned_check_enabled": bool_or_none(data.get("pwned")) if isinstance(data, dict) else None,
        "force_security_enabled": bool_or_none(data.get("force_security")) if isinstance(data, dict) else None,
    }


def addon_info_by_slugs(
    slugs: tuple[str, ...],
    token: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Read one explicitly allowlisted target by known slug."""
    for slug in slugs:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", slug):
            return None, "unsafe_slug"
        value, error = supervisor_get(f"/addons/{slug}/info", token)
        if error is None:
            return value, None
        if error != "http_404":
            return None, error
    return None, None


def sanitize_addon(detail: dict[str, Any] | None) -> dict[str, Any]:
    """Sanitize only stable App info fields required for engineering readiness."""
    source = detail if isinstance(detail, dict) else {}

    def first(key: str) -> Any:
        return source.get(key)

    return {
        "installed": isinstance(detail, dict),
        "installed_version": str_or_none(first("version"), 64),
        "state": str_or_none(first("state"), 32),
        "stage": str_or_none(first("stage"), 32),
        "update_available": bool_or_none(first("update_available")),
        "boot": str_or_none(first("boot"), 32),
        "boot_config": str_or_none(first("boot_config"), 32),
        "watchdog": bool_or_none(first("watchdog")),
        "auto_update": bool_or_none(first("auto_update")),
        "apparmor": str_or_none(first("apparmor"), 128),
        "hassio_api": bool_or_none(first("hassio_api")),
        "hassio_role": str_or_none(first("hassio_role"), 32),
        "homeassistant_api": bool_or_none(first("homeassistant_api")),
        "docker_api": bool_or_none(first("docker_api")),
        "auth_api": bool_or_none(first("auth_api")),
        "host_network": bool_or_none(first("host_network")),
        "full_access": bool_or_none(first("full_access")),
        "protected": bool_or_none(first("protected")),
        "devices_present": (len(first("devices")) > 0) if isinstance(first("devices"), list) else None,
        "privileged_present": (len(first("privileged")) > 0) if isinstance(first("privileged"), list) else None,
        "uart": bool_or_none(first("uart")),
        "usb_present": (len(first("usb")) > 0) if isinstance(first("usb"), list) else None,
        "gpio": bool_or_none(first("gpio")),
        "udev": bool_or_none(first("udev")),
        "host_pid": bool_or_none(first("host_pid")),
        "host_ipc": bool_or_none(first("host_ipc")),
        "host_dbus": bool_or_none(first("host_dbus")),
        "host_uts": bool_or_none(first("host_uts")),
        "stdin": bool_or_none(first("stdin")),
        "signed": bool_or_none(first("signed")),
    }
