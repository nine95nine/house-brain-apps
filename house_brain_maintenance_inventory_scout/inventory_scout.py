#!/usr/bin/env python3
"""One-shot, privilege-isolated all-App inventory producer for House Brain."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

SCHEMA = "house_brain_app_inventory_snapshot.v1"
PRODUCER = "maintenance_inventory_scout"
PRODUCER_VERSION = "0.1.0"
PRIVILEGE_BOUNDARY = "supervisor_manager_get_addons_only"
SOURCE_INSTANCE = "house-brain-primary"
SUPERVISOR_URL = "http://supervisor/addons"
OPTIONS_PATH = Path("/data/options.json")
BROKER_HOST_RE = re.compile(r"^house-brain-maintenance-broker\.[a-z0-9-]+\.workers\.dev$", re.I)
SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:-]{0,63}$")
SAFE_STATES = frozenset({"startup", "started", "stopped", "unknown", "error"})
SAFE_STAGES = frozenset({"stable", "experimental", "deprecated"})
MAX_APPS = 128
MAX_SUPERVISOR_BYTES = 262144
MAX_BROKER_BYTES = 65536
MAX_POST_BYTES = 32768
HTTP_TIMEOUT_SECONDS = 10
SECRET_RE = re.compile(r"^[!-~]{32,512}$")


class InventoryScoutError(RuntimeError):
    """Bounded local diagnostic error."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


# Do not inherit HTTP(S)_PROXY or other ambient proxy configuration: this
# process carries a Supervisor manager-role bearer and a dedicated Broker
# capability. Both destinations are explicitly allowlisted and must be reached
# directly.
_NO_REDIRECT = build_opener(ProxyHandler({}), _NoRedirect())


def _supervisor_open(request: Request, timeout: int):
    """Open a Supervisor request without following redirects.

    The Scout carries a manager-role token for only this one-shot process. Never
    allow an unexpected redirect to forward that bearer capability elsewhere.
    """
    return _NO_REDIRECT.open(request, timeout=timeout)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise InventoryScoutError("options_invalid") from err
    if not isinstance(value, dict):
        raise InventoryScoutError("options_invalid")
    return value


def validate_broker_url(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise InventoryScoutError("broker_url_invalid")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise InventoryScoutError("broker_url_invalid")
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as err:
        raise InventoryScoutError("broker_url_invalid") from err
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise InventoryScoutError("broker_url_invalid")
    if parsed.path != "/v1/inventory":
        raise InventoryScoutError("broker_url_path_invalid")
    if not hostname or not BROKER_HOST_RE.fullmatch(hostname):
        raise InventoryScoutError("broker_url_host_not_allowlisted")
    if port not in (None, 443):
        raise InventoryScoutError("broker_url_port_not_allowlisted")
    return value


def load_options(path: Path = OPTIONS_PATH) -> tuple[str, str]:
    value = _load_json(path)
    if set(value) != {"broker_url", "broker_inventory_write_key"}:
        raise InventoryScoutError("options_shape_invalid")
    broker_url = validate_broker_url(value.get("broker_url"))
    key = value.get("broker_inventory_write_key")
    if not isinstance(key, str) or not SECRET_RE.fullmatch(key):
        raise InventoryScoutError("inventory_write_key_invalid")
    return broker_url, key


def _nullable_version(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not VERSION_RE.fullmatch(value):
        raise InventoryScoutError(f"addons_inventory_{field}_invalid")
    return value


def _nullable_enum(value: Any, allowed: frozenset[str], field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in allowed:
        raise InventoryScoutError(f"addons_inventory_{field}_invalid")
    return value


def _nullable_bool(value: Any, field: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise InventoryScoutError(f"addons_inventory_{field}_invalid")
    return value


def _overview_row(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        raise InventoryScoutError("addons_inventory_row_invalid")

    # Supervisor GET /addons lists installed Apps only (sys_apps.installed) and
    # carries no "installed" key (verified Supervisor 2024.06.0 .. 2026.09.3; the
    # live 2026.09.3 rejected 0.1.1, which required it). An absent key therefore
    # means installed. An explicit boolean is still honoured (False = catalog
    # row, not a maintenance target); any other value fails closed.
    installed = item.get("installed", True)
    if not isinstance(installed, bool):
        raise InventoryScoutError("addons_inventory_installed_invalid")
    if installed is False:
        return None

    slug = item.get("slug")
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        raise InventoryScoutError("addons_inventory_slug_invalid")

    row = {
        "slug": slug,
        "version": _nullable_version(item.get("version"), "version"),
        "version_latest": _nullable_version(item.get("version_latest"), "version_latest"),
        "state": _nullable_enum(item.get("state"), SAFE_STATES, "state"),
        "stage": _nullable_enum(item.get("stage"), SAFE_STAGES, "stage"),
        "update_available": _nullable_bool(item.get("update_available"), "update_available"),
        "system_managed": _nullable_bool(item.get("system_managed"), "system_managed"),
    }
    if row["version"] is None and row["state"] is None:
        raise InventoryScoutError("addons_inventory_installed_row_incomplete")
    return row


def sanitize_inventory(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("addons")
    if not isinstance(raw, list):
        raise InventoryScoutError("addons_inventory_schema_invalid")
    rows = []
    seen: set[str] = set()
    for item in raw:
        row = _overview_row(item)
        if row is None:
            continue
        if row["slug"] in seen:
            raise InventoryScoutError("addons_inventory_duplicate_slug")
        seen.add(row["slug"])
        rows.append(row)
    rows.sort(key=lambda row: row["slug"])
    if len(rows) > MAX_APPS:
        raise InventoryScoutError("addons_inventory_too_many_installed")
    return {
        "count": len(rows),
        "truncated": False,
        "apps": rows,
        "raw_options_exposed": False,
        "raw_logs_exposed": False,
        "free_text_exposed": False,
        "control_authority": "none",
    }


def supervisor_inventory(
    token: str,
    *,
    opener: Callable[..., Any] = _supervisor_open,
) -> dict[str, Any]:
    if not isinstance(token, str) or not SECRET_RE.fullmatch(token):
        raise InventoryScoutError("supervisor_token_missing")
    request = Request(
        SUPERVISOR_URL,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": f"house-brain-maintenance-inventory-scout/{PRODUCER_VERSION}",
        },
    )
    try:
        with opener(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise InventoryScoutError(f"supervisor_http_{response.status}")
            raw = response.read(MAX_SUPERVISOR_BYTES + 1)
    except HTTPError as err:
        raise InventoryScoutError(f"supervisor_http_{err.code}") from err
    except (URLError, TimeoutError, OSError) as err:
        raise InventoryScoutError("supervisor_unavailable") from err
    if len(raw) > MAX_SUPERVISOR_BYTES:
        raise InventoryScoutError("supervisor_response_too_large")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise InventoryScoutError("supervisor_malformed_json") from err
    if not isinstance(body, dict) or body.get("result") != "ok" or not isinstance(body.get("data"), dict):
        raise InventoryScoutError("supervisor_unexpected_envelope")
    return body["data"]


def build_payload(data: dict[str, Any], *, now: datetime | None = None, event_id: str | None = None) -> dict[str, Any]:
    now = now or utc_now()
    if now.tzinfo is None:
        raise InventoryScoutError("clock_not_timezone_aware")
    event_id = event_id or str(uuid.uuid4())
    return {
        "schema_version": SCHEMA,
        "source": {
            "system": "home_assistant",
            "instance": SOURCE_INSTANCE,
            "producer": PRODUCER,
            "producer_version": PRODUCER_VERSION,
            "privilege_boundary": PRIVILEGE_BOUNDARY,
        },
        "generated_at": iso_z(now),
        "id": event_id,
        "operation": "ha.apps.inventory",
        "data": sanitize_inventory(data),
    }


def publish_inventory(
    broker_url: str,
    key: str,
    payload: dict[str, Any],
    *,
    opener=_NO_REDIRECT,
) -> dict[str, Any]:
    if not SECRET_RE.fullmatch(key):
        raise InventoryScoutError("inventory_write_key_invalid")
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(body) > MAX_POST_BYTES:
        raise InventoryScoutError("inventory_payload_too_large")
    request = Request(
        broker_url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"house-brain-maintenance-inventory-scout/{PRODUCER_VERSION}",
        },
    )
    try:
        with opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            if response.status != 202:
                raise InventoryScoutError(f"broker_http_{response.status}")
            raw = response.read(MAX_BROKER_BYTES + 1)
    except HTTPError as err:
        raise InventoryScoutError(f"broker_http_{err.code}") from err
    except (URLError, TimeoutError, OSError) as err:
        raise InventoryScoutError("broker_unavailable") from err
    if len(raw) > MAX_BROKER_BYTES:
        raise InventoryScoutError("broker_response_too_large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise InventoryScoutError("broker_malformed_json") from err
    if (
        not isinstance(value, dict)
        or value.get("ok") is not True
        or value.get("control_authority") != "none"
        or value.get("disposition") not in {"accept", "accept_idempotent"}
    ):
        raise InventoryScoutError("broker_rejected_inventory")
    return value


def main() -> int:
    try:
        broker_url, key = load_options()
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        payload = build_payload(supervisor_inventory(token))
        result = publish_inventory(broker_url, key, payload)
    except InventoryScoutError as err:
        print(f"maintenance_inventory_scout=failed reason={err}", flush=True)
        return 1
    except Exception as err:  # Never print arbitrary exception detail.
        print(f"maintenance_inventory_scout=failed reason=unexpected_{type(err).__name__}", flush=True)
        return 1

    data = payload["data"]
    print(
        "maintenance_inventory_scout=pass "
        f"count={data['count']} truncated={str(data['truncated']).lower()} "
        f"disposition={result['disposition']} control_authority=none",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
