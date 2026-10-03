#!/usr/bin/env python3
"""Bounded projections of the accepted Broker snapshot, without I/O or authority.

Only exposed/decision fields and the common source envelope are validated here;
unused target and security metadata are never forwarded. This is not a replacement
for the Broker's complete ingest validator or the future provider authenticator.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import re
from types import MappingProxyType
from typing import Any, Final, NoReturn

BROKER_SCHEMA: Final = "house_brain_maintenance_snapshot.v1"
AUTHORITY: Final = "READ_ONLY_ENGINEERING"
MAX_FUTURE_SKEW_SECONDS: Final = 60
MAX_TRANSPORT_AGE_SECONDS: Final = 300
# Broker allows 300 seconds at ingestion and retains a fresh read for 300 more.
MAX_SOURCE_AGE_SECONDS: Final = 600
VERSION_RE: Final = re.compile(r"[A-Za-z0-9._+-]{1,64}")
TIME_RE: Final = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)", re.ASCII)
AUTHORITY_VALUES: Final = MappingProxyType({
    "observer_only": True, "supervisor_write_capability": False,
    "homeassistant_api_capability": False, "docker_api_capability": False,
    "shell_command_capability": False, "entity_control_capability": False,
    "physical_actions_present": False,
})


class SnapshotError(ValueError):
    """Only fixed reason codes; never interpolate source values or paths."""


def _fail(code: str) -> NoReturn:
    raise SnapshotError(code)


def _obj(value: Any, code: str, required: set[str] | None = None) -> dict[str, Any]:
    if type(value) is not dict or (required is not None and not required <= value.keys()):
        _fail(code)
    return value


def _iso(value: Any, code: str) -> datetime:
    if type(value) is not str or TIME_RE.fullmatch(value) is None:
        _fail(code)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        _fail(code)


def _now(now: datetime | None) -> datetime:
    value = datetime.now(timezone.utc) if now is None else now
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        _fail("NOW_INVALID")
    try:
        return value.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        _fail("NOW_INVALID")


def _number(value: Any, code: str, maximum: int, *, integer: bool = False,
            nullable: bool = False) -> int | float | None:
    if value is None and nullable:
        return None
    allowed = (int,) if integer else (int, float)
    if type(value) not in allowed or not 0 <= value <= maximum or not math.isfinite(value):
        _fail(code)
    return value


def _boolean(value: Any, code: str, *, nullable: bool = False) -> bool | None:
    if value is None and nullable:
        return None
    if type(value) is not bool:
        _fail(code)
    return value


def _strings(value: Any, code: str, count: int, length: int) -> list[str]:
    if type(value) is not list or len(value) > count:
        _fail(code)
    if any(type(s) is not str or not 0 < len(s) <= length or
           any(ord(c) < 32 or ord(c) == 127 for c in s) for s in value):
        _fail(code)
    if len(set(value)) != len(value):
        _fail(code)
    return value


def validate_envelope(envelope: Any, *, now: datetime | None = None) -> tuple[dict[str, Any], str]:
    root = _obj(envelope, "ENVELOPE_INVALID")
    if set(root) != {"relay", "snapshot"}:
        _fail("ENVELOPE_KEYS_INVALID")
    relay = _obj(root["relay"], "RELAY_INVALID")
    snapshot = _obj(root["snapshot"], "SNAPSHOT_MISSING")
    if set(relay) != {"version", "schema_version", "received_at", "transport_age_seconds",
                      "transport_fresh", "stale_after_seconds"}:
        _fail("RELAY_KEYS_INVALID")
    if set(snapshot) != {"schema_version", "source", "generated_at", "sequence",
                         "data_quality", "platform", "maintenance", "targets", "authority"}:
        _fail("SNAPSHOT_KEYS_INVALID")
    # 0.3.0 is the live Broker; 0.4.0 (#454, merged candidate) changes only this version
    # string on the snapshot read path. Any other version stays unqualified.
    if relay["version"] not in ("0.3.0", "0.4.0"):
        _fail("BROKER_VERSION_UNQUALIFIED")
    if relay["schema_version"] != BROKER_SCHEMA or snapshot["schema_version"] != BROKER_SCHEMA:
        _fail("SCHEMA_MISMATCH")
    source = _obj(snapshot["source"], "SOURCE_INVALID")
    if set(source) != {"system", "instance", "observer_version", "session_id"}:
        _fail("SOURCE_INVALID")
    if source["system"] != "home_assistant" or source["instance"] != "house-brain-primary":
        _fail("SOURCE_IDENTITY_INVALID")
    if type(source["observer_version"]) is not str or source["observer_version"] not in (
            "0.1.0", "0.2.0", "0.2.1", "0.2.2"):
        _fail("OBSERVER_VERSION_UNQUALIFIED")
    if type(source["session_id"]) is not str or re.fullmatch(r"[0-9a-f]{32}", source["session_id"]) is None:
        _fail("SOURCE_SESSION_INVALID")
    sequence = _number(snapshot["sequence"], "SEQUENCE_INVALID", 9007199254740991, integer=True)
    if sequence == 0:
        _fail("SEQUENCE_INVALID")
    if relay["transport_fresh"] is not True:
        _fail("TRANSPORT_STALE")
    _number(relay["transport_age_seconds"], "TRANSPORT_AGE_INVALID", MAX_TRANSPORT_AGE_SECONDS)
    # A producer must never enlarge the consumer's freshness policy.
    if type(relay["stale_after_seconds"]) not in (int, float) or relay["stale_after_seconds"] != MAX_TRANSPORT_AGE_SECONDS:
        _fail("STALE_THRESHOLD_INVALID")
    current = _now(now)
    received = _iso(relay["received_at"], "RECEIVED_AT_INVALID")
    generated = _iso(snapshot["generated_at"], "GENERATED_AT_INVALID")
    received_age = (current - received).total_seconds()
    source_age = (current - generated).total_seconds()
    if received_age < -MAX_FUTURE_SKEW_SECONDS:
        _fail("RECEIVED_AT_FUTURE")
    if source_age < -MAX_FUTURE_SKEW_SECONDS:
        _fail("GENERATED_AT_FUTURE")
    if received_age > MAX_TRANSPORT_AGE_SECONDS:
        _fail("RECEIVED_AT_STALE")
    if source_age > MAX_SOURCE_AGE_SECONDS:
        _fail("SOURCE_STALE")
    ingest_age = (received - generated).total_seconds()
    if not -MAX_FUTURE_SKEW_SECONDS <= ingest_age <= MAX_TRANSPORT_AGE_SECONDS:
        _fail("SOURCE_RECEIPT_INCONSISTENT")
    authority = _obj(snapshot["authority"], "AUTHORITY_INVARIANT_FAILED")
    if set(authority) != set(AUTHORITY_VALUES) or any(
            authority[k] is not value for k, value in AUTHORITY_VALUES.items()):
        _fail("AUTHORITY_INVARIANT_FAILED")
    return snapshot, snapshot["generated_at"]


def get_platform_versions(envelope: Any, *, now: datetime | None = None) -> dict[str, Any]:
    snapshot, observed_at = validate_envelope(envelope, now=now)
    platform = _obj(snapshot["platform"], "PLATFORM_INVALID",
                    {"core_version", "supervisor_version", "os_version", "system_supported"})
    def version(name: str) -> str | None:
        value = platform[name]
        if value is not None and (type(value) is not str or VERSION_RE.fullmatch(value) is None):
            _fail("PLATFORM_VERSION_INVALID")
        return value
    supported = _boolean(platform["system_supported"], "PLATFORM_SUPPORTED_INVALID", nullable=True)
    return {"core": version("core_version"), "supervisor": version("supervisor_version"),
            "haos": version("os_version"), "supported": supported, "observed_at": observed_at}


def get_backup_readiness(envelope: Any, *, now: datetime | None = None) -> dict[str, Any]:
    snapshot, observed_at = validate_envelope(envelope, now=now)
    maintenance = _obj(snapshot["maintenance"], "MAINTENANCE_INVALID",
                       {"backup_count", "latest_backup_age_hours", "days_until_stale", "fresh_by_supervisor_policy"})
    count = _number(maintenance["backup_count"], "BACKUP_COUNT_INVALID", 10000, integer=True)
    age = _number(maintenance["latest_backup_age_hours"], "BACKUP_AGE_INVALID", 100000, nullable=True)
    days = _number(maintenance["days_until_stale"], "BACKUP_DAYS_INVALID", 3650, integer=True, nullable=True)
    fresh = _boolean(maintenance["fresh_by_supervisor_policy"], "BACKUP_FRESH_INVALID")
    if count == 0 and (fresh is not False or age is not None):
        _fail("BACKUP_CONTRADICTION")
    if fresh is True and (age is None or days is None):
        _fail("BACKUP_EVIDENCE_INCOMPLETE")
    # Preserve Supervisor's policy flag; freshness here is not restore readiness.
    return {"count": count, "latest_age_seconds": None if age is None else int(round(age * 3600)),
            "days_until_stale": days, "fresh": fresh, "observed_at": observed_at}


def get_house_brain_status(envelope: Any, *, now: datetime | None = None) -> dict[str, Any]:
    try:
        snapshot, observed_at = validate_envelope(envelope, now=now)
    except SnapshotError as exc:
        if str(exc) != "AUTHORITY_INVARIANT_FAILED":
            raise
        # Do not echo any observation from an authority-invalid payload.
        return {"status": "BLOCKED", "reason_codes": ["AUTHORITY_INVARIANT_FAILED"],
                "observed_at": None, "source_schema": BROKER_SCHEMA, "authority": AUTHORITY}
    quality = _obj(snapshot["data_quality"], "DATA_QUALITY_INVALID")
    if set(quality) != {"complete", "failed_sources", "reason_codes"}:
        _fail("DATA_QUALITY_INVALID")
    complete = _boolean(quality["complete"], "DATA_QUALITY_INVALID")
    failed = _strings(quality["failed_sources"], "DATA_QUALITY_INVALID", 16, 64)
    raw_reasons = _strings(quality["reason_codes"], "DATA_QUALITY_INVALID", 32, 128)
    if complete is True and (failed or raw_reasons):
        _fail("DATA_QUALITY_CONTRADICTION")
    maintenance = _obj(snapshot["maintenance"], "MAINTENANCE_INVALID",
                       {"supervisor_busy", "active_job_count", "resolution"})
    busy = _boolean(maintenance["supervisor_busy"], "SUPERVISOR_BUSY_INVALID")
    jobs = _number(maintenance["active_job_count"], "JOB_COUNT_INVALID", 1000, integer=True)
    if busy is not (jobs > 0):
        _fail("SUPERVISOR_BUSY_CONTRADICTION")
    resolution = _obj(maintenance["resolution"], "RESOLUTION_INVALID",
                      {"healthy", "issue_count", "unsupported_count", "unhealthy_count"})
    healthy = _boolean(resolution["healthy"], "RESOLUTION_INVALID")
    counts = [_number(resolution[k], "RESOLUTION_INVALID", 10000, integer=True)
              for k in ("issue_count", "unsupported_count", "unhealthy_count")]
    platform = get_platform_versions(envelope, now=now)
    reasons = []
    if not complete:
        reasons.append("DATA_QUALITY_INCOMPLETE")
    if not healthy or any(counts):
        reasons.append("RESOLUTION_UNHEALTHY")
    if busy:
        reasons.append("SUPERVISOR_BUSY")
    if platform["supported"] is False:
        reasons.append("PLATFORM_UNSUPPORTED")
    if platform["supported"] is None or any(platform[k] is None for k in ("core", "supervisor", "haos")):
        reasons.append("PLATFORM_INFORMATION_INCOMPLETE")
    return {"status": "DEGRADED" if reasons else "READY", "reason_codes": reasons,
            "observed_at": observed_at, "source_schema": BROKER_SCHEMA, "authority": AUTHORITY}


HANDLERS: Final = MappingProxyType({"get_house_brain_status": get_house_brain_status,
                                   "get_platform_versions": get_platform_versions,
                                   "get_backup_readiness": get_backup_readiness})
IMPLEMENTED_TOOLS: Final = frozenset(HANDLERS)


def call_snapshot_tool(name: str, envelope: Any, arguments: Any, *, now: datetime | None = None) -> dict[str, Any]:
    if type(name) is not str or name not in HANDLERS:
        _fail("TOOL_NOT_IMPLEMENTED")
    if type(arguments) is not dict or arguments:
        _fail("ARGUMENTS_INVALID")
    return HANDLERS[name](envelope, now=now)
