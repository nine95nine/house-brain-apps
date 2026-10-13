#!/usr/bin/env python3
"""Sanitized default-role App info read for the House Brain Engineering Observer.

All-App enumeration and App stats require Supervisor manager scope and are
intentionally owned outside this default-role Observer.
"""
from __future__ import annotations

import re
from typing import Any, Callable

try:
    from .observer_supervisor import bool_or_none, str_or_none, supervisor_get
except ImportError:
    from observer_supervisor import bool_or_none, str_or_none, supervisor_get

SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
SAFE_STATES = frozenset({"startup", "started", "stopped", "unknown", "error"})
SAFE_STAGES = frozenset({"stable", "experimental", "deprecated"})
SAFE_BOOT = frozenset({"auto", "manual"})
SAFE_BOOT_CONFIG = frozenset({"auto", "manual", "manual_only"})
SAFE_HASSIO_ROLES = frozenset({"default", "homeassistant", "backup", "manager", "admin"})


class AppInventoryError(RuntimeError):
    """Fail-closed error for bounded App reads."""


def _safe_enum(value: Any, allowed: frozenset[str], max_len: int = 32) -> str | None:
    value = str_or_none(value, max_len)
    return value if value in allowed else None


def _safe_version(value: Any) -> str | None:
    value = str_or_none(value, 64)
    if value is None or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+:-]{0,63}", value):
        return None
    return value


def _safe_slug(value: Any) -> str:
    if not isinstance(value, str) or not SLUG_RE.fullmatch(value):
        raise AppInventoryError("invalid_app_slug")
    return value


def collect_app_info(
    slug: str,
    token: str,
    *,
    getter: Callable[[str, str], tuple[dict[str, Any] | None, str | None]] = supervisor_get,
) -> dict[str, Any]:
    safe_slug = _safe_slug(slug)
    detail, error = getter(f"/addons/{safe_slug}/info", token)
    if error == "http_404":
        raise AppInventoryError("app_not_installed")
    if error is not None:
        raise AppInventoryError(f"app_info_{error}")
    if not isinstance(detail, dict):
        raise AppInventoryError("app_info_schema_invalid")

    devices = detail.get("devices")
    privileged = detail.get("privileged")
    usb = detail.get("usb")
    return {
        "operation": "ha.app.info",
        "data": {
            "slug": safe_slug,
            "version": _safe_version(detail.get("version")),
            "version_latest": _safe_version(detail.get("version_latest")),
            "state": _safe_enum(detail.get("state"), SAFE_STATES),
            "stage": _safe_enum(detail.get("stage"), SAFE_STAGES),
            "update_available": bool_or_none(detail.get("update_available")),
            "boot": _safe_enum(detail.get("boot"), SAFE_BOOT),
            "boot_config": _safe_enum(detail.get("boot_config"), SAFE_BOOT_CONFIG),
            "watchdog": bool_or_none(detail.get("watchdog")),
            "auto_update": bool_or_none(detail.get("auto_update")),
            "system_managed": bool_or_none(detail.get("system_managed")),
            "protected": bool_or_none(detail.get("protected")),
            "signed": bool_or_none(detail.get("signed")),
            "apparmor": str_or_none(detail.get("apparmor"), 128),
            "permissions": {
                "hassio_api": bool_or_none(detail.get("hassio_api")),
                "hassio_role": _safe_enum(detail.get("hassio_role"), SAFE_HASSIO_ROLES),
                "homeassistant_api": bool_or_none(detail.get("homeassistant_api")),
                "docker_api": bool_or_none(detail.get("docker_api")),
                "auth_api": bool_or_none(detail.get("auth_api")),
                "full_access": bool_or_none(detail.get("full_access")),
                "host_network": bool_or_none(detail.get("host_network")),
                "host_pid": bool_or_none(detail.get("host_pid")),
                "host_ipc": bool_or_none(detail.get("host_ipc")),
                "host_dbus": bool_or_none(detail.get("host_dbus")),
                "host_uts": bool_or_none(detail.get("host_uts")),
                "stdin": bool_or_none(detail.get("stdin")),
                "udev": bool_or_none(detail.get("udev")),
                "uart": bool_or_none(detail.get("uart")),
                "gpio": bool_or_none(detail.get("gpio")),
                "devices_present": len(devices) > 0 if isinstance(devices, list) else None,
                "privileged_present": len(privileged) > 0 if isinstance(privileged, list) else None,
                "usb_present": len(usb) > 0 if isinstance(usb, list) else None,
            },
            "raw_options_exposed": False,
            "raw_logs_exposed": False,
            "free_text_exposed": False,
            "control_authority": "none",
        },
    }
