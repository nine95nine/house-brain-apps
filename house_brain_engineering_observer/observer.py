#!/usr/bin/env python3
"""House Brain Engineering Observer v0.2.2 (App package 0.2.4).

Read-only by architecture:
- Supervisor API role: default/info only.
- /addons mapping: read-only, further narrowed by AppArmor.
- No Home Assistant API, Docker API, host network, devices, or shell-command API.
- Publishes only a strict sanitized engineering snapshot to the maintenance broker.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from observer_source import TARGET_ID, inspect_target
from observer_supervisor import (
    JSON_DECODE_ERRORS,
    TRANSPORT_ERRORS,
    addon_info_by_slugs,
    bool_or_none,
    control_char_audit,
    derive_backup_readiness,
    derive_jobs,
    derive_resolution,
    derive_security,
    int_or_none,
    pick,
    sanitize_addon,
    str_or_none,
    supervisor_get,
    transport_error_code,
)

SCHEMA = "house_brain_maintenance_snapshot.v1"
OBSERVER_VERSION = "0.2.2"
SOURCE_INSTANCE = "house-brain-primary"
TARGET_SLUGS = ("local_house_brain_njspc_aquarite", "house_brain_njspc_aquarite")
STATE_PATH = Path("/data/observer-state.json")
OPTIONS_PATH = Path("/data/options.json")
HTTP_TIMEOUT_SECONDS = 10
MAX_PUBLISH_BYTES = 32768
BROKER_PATH = "/v1/snapshot"
BROKER_HOST_RE = re.compile(r"^house-brain-maintenance-broker\.[a-z0-9-]+\.workers\.dev$", re.I)
SECRET_RE = re.compile(r"^[!-~]{32,512}$")
# data_quality reason codes added by App 0.2.4 (wire-compatible: bounded strings in existing arrays).
# GAP-91-07: a section whose Supervisor strings carried C0/DEL controls (those strings are nulled).
CONTROL_CHARACTER_REASON_CODES = {
    "platform": "PLATFORM_CONTROL_CHARACTERS_REJECTED",
    "maintenance": "MAINTENANCE_CONTROL_CHARACTERS_REJECTED",
    "observer_app": "OBSERVER_APP_CONTROL_CHARACTERS_REJECTED",
    "target_app": "TARGET_APP_CONTROL_CHARACTERS_REJECTED",
}
# GAP-91-09: derived facts whose source was not observed carry schema-required defaults
# (supervisor_busy=false, active_job_count=0; resolution.healthy is now false). The Broker schema
# does not allow null there, so the snapshot names the defaulted facts explicitly instead.
UNOBSERVED_FACT_REASON_CODES = {
    "jobs": "JOBS_FACTS_UNOBSERVED",
    "resolution": "RESOLUTION_FACTS_UNOBSERVED",
}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


# Never inherit ambient proxy configuration while carrying Broker capability
# material. The destination is strictly allowlisted and must be contacted
# directly.
_BROKER_OPENER = build_opener(ProxyHandler({}), _NoRedirect())


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, *JSON_DECODE_ERRORS) as err:  # GAP-91-05: non-UTF-8 / deep nesting contained
        raise RuntimeError(f"invalid_json:{path.name}:{type(err).__name__}") from err
    if not isinstance(data, dict):
        raise RuntimeError(f"invalid_json_object:{path.name}")
    return data


def validate_broker_url(broker_url: Any) -> str:
    """Validate the single approved outbound maintenance-broker destination."""
    if not isinstance(broker_url, str) or not broker_url:
        raise RuntimeError("broker_url_missing")
    if broker_url != broker_url.strip() or any(ord(char) < 0x20 or ord(char) == 0x7F for char in broker_url):
        raise RuntimeError("broker_url_invalid")
    try:
        parsed = urlparse(broker_url)
        username = parsed.username
        password = parsed.password
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as err:
        raise RuntimeError("broker_url_invalid") from err
    if parsed.scheme != "https" or username or password or parsed.params or parsed.query or parsed.fragment:
        raise RuntimeError("broker_url_invalid")
    if parsed.path != BROKER_PATH or not hostname:
        raise RuntimeError("broker_url_path_invalid")
    if not BROKER_HOST_RE.fullmatch(hostname):
        raise RuntimeError("broker_url_host_not_allowlisted")
    if port not in (None, 443):
        raise RuntimeError("broker_url_port_not_allowlisted")
    return broker_url


def load_options() -> tuple[str, str, int]:
    options = load_json(OPTIONS_PATH)
    if set(options) != {"broker_url", "broker_write_key", "publish_interval_seconds"}:
        raise RuntimeError("options_shape_invalid")
    broker_url = validate_broker_url(options.get("broker_url"))
    write_key = options.get("broker_write_key")
    interval = options.get("publish_interval_seconds")
    if not isinstance(write_key, str) or not SECRET_RE.fullmatch(write_key):
        raise RuntimeError("broker_write_key_invalid")
    if not isinstance(interval, int) or isinstance(interval, bool) or not 60 <= interval <= 300:
        raise RuntimeError("publish_interval_invalid")
    return broker_url, write_key, interval


def collect_snapshot(
    token: str,
    sequence: int,
    session_id: str,
    *,
    extra_errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Collect one sanitized snapshot.

    ``extra_errors`` (App 0.2.4) adds local health facts, such as an unpersisted sequence state or a
    stopped read worker, to ``data_quality`` exactly like a failed Supervisor source.
    """
    now = utc_now()
    endpoints = {
        "system": "/info",
        "supervisor": "/supervisor/info",
        "core": "/core/info",
        "os": "/os/info",
        "jobs": "/jobs/info",
        "backups": "/backups/info",
        "resolution": "/resolution/info",
        "security": "/security/info",
    }
    data: dict[str, dict[str, Any] | None] = {}
    errors: dict[str, str] = {}
    for name, path in endpoints.items():
        value, error = supervisor_get(path, token)
        data[name] = value
        if error:
            errors[name] = error

    target_detail, target_error = addon_info_by_slugs(TARGET_SLUGS, token)
    if target_error:
        errors["target_app"] = target_error

    observer_detail, observer_error = supervisor_get("/addons/self/info", token)
    if observer_error:
        errors["observer_app"] = observer_error
    for key, value in (extra_errors or {}).items():
        errors[key] = value

    control_sections: set[str] = set()

    def audited(section: str, build: Callable[[], Any]) -> Any:
        with control_char_audit() as hits:
            value = build()
        if hits:
            control_sections.add(section)
        return value

    platform = audited("platform", lambda: {
        "supervisor_version": str_or_none(pick(data.get("supervisor"), "version"), 64),
        "supervisor_latest": str_or_none(pick(data.get("supervisor"), "version_latest"), 64),
        "supervisor_update_available": bool_or_none(pick(data.get("supervisor"), "update_available")),
        "supervisor_state": str_or_none(pick(data.get("system"), "state"), 32),
        "system_supported": bool_or_none(pick(data.get("system"), "supported")),
        "core_version": str_or_none(pick(data.get("core"), "version"), 64),
        "core_latest": str_or_none(pick(data.get("core"), "version_latest"), 64),
        "core_update_available": bool_or_none(pick(data.get("core"), "update_available")),
        "os_version": str_or_none(pick(data.get("os"), "version"), 64),
        "os_latest": str_or_none(pick(data.get("os"), "version_latest"), 64),
        "os_update_available": bool_or_none(pick(data.get("os"), "update_available")),
        "arch": str_or_none(pick(data.get("system"), "arch"), 32)
            or str_or_none(pick(data.get("supervisor"), "arch"), 32)
            or str_or_none(pick(data.get("core"), "arch"), 32),
    })
    maintenance = audited("maintenance", lambda: {
        **derive_jobs(data.get("jobs")),
        **derive_backup_readiness(data.get("backups"), now),
        "resolution": derive_resolution(data.get("resolution")),
        "security": derive_security(data.get("security")),
    })
    observer_runtime = audited("observer_app", lambda: sanitize_addon(observer_detail))
    target_runtime = audited("target_app", lambda: sanitize_addon(target_detail))

    reason_codes = {f"{key.upper()}_{value.upper()}" for key, value in errors.items()}
    reason_codes.update(CONTROL_CHARACTER_REASON_CODES[section] for section in control_sections)
    reason_codes.update(code for source, code in UNOBSERVED_FACT_REASON_CODES.items() if source in errors)

    snapshot = {
        "schema_version": SCHEMA,
        "source": {
            "system": "home_assistant",
            "instance": SOURCE_INSTANCE,
            "observer_version": OBSERVER_VERSION,
            "session_id": session_id,
        },
        "generated_at": iso_z(now),
        "sequence": sequence,
        "data_quality": {
            "complete": len(errors) == 0 and not reason_codes,
            "failed_sources": sorted(errors.keys()),
            "reason_codes": sorted(reason_codes),
        },
        "platform": platform,
        "maintenance": maintenance,
        "targets": {
            "engineering_observer": {
                "runtime": observer_runtime,
            },
            TARGET_ID: {
                "runtime": target_runtime,
                "source": inspect_target(),
            },
        },
        "authority": {
            "observer_only": True,
            "supervisor_write_capability": False,
            "homeassistant_api_capability": False,
            "docker_api_capability": False,
            "shell_command_capability": False,
            "entity_control_capability": False,
            "physical_actions_present": False,
        },
    }
    return snapshot


def read_state() -> dict[str, Any]:
    try:
        return load_json(STATE_PATH)
    except RuntimeError:
        return {}


def write_state(sequence: int, session_id: str) -> None:
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"sequence": sequence, "session_id": session_id}, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, STATE_PATH)


def next_sequence_checked() -> tuple[int, str, bool]:
    """Return ``(sequence, session_id, persisted)``; never raises for local state faults (GAP-91-05).

    An unreadable/corrupt state file restarts the session (as invalid JSON always did). When the
    new state cannot be written, a fresh session starting at 1 is returned instead, so a read-only
    volume holding an old state can never make the Broker see a repeated sequence; the caller flags
    ``observer_state`` in ``data_quality``.
    """
    state_data = read_state()
    session_id = state_data.get("session_id")
    if not isinstance(session_id, str) or not re.fullmatch(r"[0-9a-f]{32}", session_id):
        session_id = uuid.uuid4().hex
        current = 0
    else:
        value = state_data.get("sequence")
        current = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
    sequence = current + 1
    try:
        write_state(sequence, session_id)
    except OSError:
        return 1, uuid.uuid4().hex, False
    return sequence, session_id, True


def next_sequence() -> tuple[int, str]:
    sequence, session_id, _persisted = next_sequence_checked()
    return sequence, session_id


def publish(url: str, key: str, payload: dict[str, Any], *, opener=_BROKER_OPENER) -> None:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(raw) > MAX_PUBLISH_BYTES:
        raise RuntimeError("snapshot_too_large")
    request = Request(
        url,
        data=raw,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"house-brain-engineering-observer/{OBSERVER_VERSION}",
        },
    )
    try:
        with opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            if response.status not in (200, 202, 204):
                raise RuntimeError(f"broker_http_{response.status}")
    except HTTPError as err:
        raise RuntimeError(f"broker_http_{err.code}") from err
    except TRANSPORT_ERRORS as err:  # GAP-91-01/06: protocol errors contained, timeout split out
        raise RuntimeError(f"broker_{transport_error_code(err)}") from err


def run_iteration(
    token: str,
    broker_url: str,
    write_key: str,
    read_worker_alive: Callable[[], bool] | None = None,
) -> None:
    """One collect + publish cycle. Local health faults become data_quality facts."""
    sequence, session_id, persisted = next_sequence_checked()
    extra_errors: dict[str, str] = {}
    if not persisted:
        extra_errors["observer_state"] = "not_persisted"
    if read_worker_alive is not None and not read_worker_alive():
        extra_errors["read_worker"] = "stopped"  # GAP-91-03 liveness fact
    snapshot = collect_snapshot(token, sequence, session_id, extra_errors=extra_errors)
    try:
        publish(broker_url, write_key, snapshot)
        print(
            f"maintenance_publish=accepted sequence={sequence} "
            f"complete={str(snapshot['data_quality']['complete']).lower()}",
            flush=True,
        )
    except RuntimeError as err:
        print(f"maintenance_publish=failed sequence={sequence} reason={err}", flush=True)


def main(read_worker_alive: Callable[[], bool] | None = None) -> int:
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not SECRET_RE.fullmatch(token):
        print("observer_startup_error=supervisor_token_missing", flush=True)
        return 2
    try:
        broker_url, write_key, interval = load_options()
    except RuntimeError as err:
        print(f"observer_startup_error={err}", flush=True)
        return 2

    print(
        f"observer_started version={OBSERVER_VERSION} schema={SCHEMA} "
        "authority=read_only supervisor_role=default",
        flush=True,
    )
    while True:
        started = time.monotonic()
        try:
            run_iteration(token, broker_url, write_key, read_worker_alive)
        except Exception as err:  # noqa: BLE001 - GAP-91-01: last-resort guard, type name only
            print(f"maintenance_iteration=failed type={type(err).__name__}", flush=True)
        elapsed = time.monotonic() - started
        time.sleep(max(1.0, interval - elapsed))


if __name__ == "__main__":
    raise SystemExit(main())
