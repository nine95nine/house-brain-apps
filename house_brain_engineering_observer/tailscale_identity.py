#!/usr/bin/env python3
"""Strict, default-role Tailscale identity projection for House Brain.

The collector uses only Supervisor `.../info` endpoints permitted to the permanent
Observer's default role. It never requests App logs, never shells into the
Tailscale App, never returns raw App options/logs, and never guesses a tailnet
suffix. Exact MagicDNS/FQDN identity therefore remains external accepted evidence
unless a future separately privileged producer supplies a bounded corroborating
source.
"""
from __future__ import annotations

import ipaddress
import json
import re
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

try:
    from . import observer_supervisor as _bounded
except ImportError:
    import observer_supervisor as _bounded

SUPERVISOR_BASE = "http://supervisor"
TAILSCALE_SLUG = "a0d7b954_tailscale"
ADDON_INFO_PATH = f"/addons/{TAILSCALE_SLUG}/info"
HOST_INFO_PATH = "/host/info"
NETWORK_INFO_PATH = "/network/info"
JSON_PATHS = frozenset({ADDON_INFO_PATH, HOST_INFO_PATH, NETWORK_INFO_PATH})
HTTP_TIMEOUT_SECONDS = 10
MAX_JSON_BYTES = 262144
SECRET_RE = re.compile(r"^[!-~]{32,512}$")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_SUPERVISOR_OPENER = build_opener(ProxyHandler({}), _NoRedirect())


def _supervisor_open(request: Request, timeout: int):
    return _SUPERVISOR_OPENER.open(request, timeout=timeout)


TAILNET_IPV4 = ipaddress.ip_network("100.64.0.0/10")
DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.I)
FQDN_RE = re.compile(
    r"(?i)\b([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\.ts\.net)\b"
)
FQDN_CONTEXT = (
    "available within your tailnet",
    "dnsname",
    "certificate",
    "cert domain",
    "tailscale serve",
    "tailscale funnel",
)


def _request_bytes(
    path: str,
    token: str,
    *,
    accept: str,
    max_bytes: int,
    allowed_paths: frozenset[str],
    opener: Callable[..., Any] = _supervisor_open,
) -> tuple[bytes | None, str | None]:
    if path not in allowed_paths:
        return None, "path_not_allowlisted"
    if not isinstance(token, str) or not SECRET_RE.fullmatch(token):
        return None, "supervisor_token_missing"
    req = Request(
        SUPERVISOR_BASE + path,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": accept,
            "User-Agent": "house-brain-engineering-observer/tailscale-identity-r2",
        },
    )
    if req.method != "GET":
        return None, "non_get_method_prohibited"
    started = _bounded._monotonic()
    try:
        with opener(req, timeout=HTTP_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                return None, f"http_{response.status}"
            raw = _bounded.read_bounded(response, max_bytes, started + _bounded.RESPONSE_DEADLINE_SECONDS)
    except HTTPError as err:
        return None, f"http_{err.code}"
    except _bounded.TRANSPORT_ERRORS as err:  # GAP-91-01/04/06: contained, timeout split out
        return None, _bounded.transport_error_code(err)
    if len(raw) > max_bytes:
        return None, "response_too_large"
    return raw, None


def _json_get(
    path: str,
    token: str,
    *,
    opener: Callable[..., Any] = _supervisor_open,
) -> tuple[dict[str, Any] | None, str | None]:
    raw, error = _request_bytes(
        path,
        token,
        accept="application/json",
        max_bytes=MAX_JSON_BYTES,
        allowed_paths=JSON_PATHS,
        opener=opener,
    )
    if error or raw is None:
        return None, error
    try:
        body = json.loads(raw.decode("utf-8"))
    except _bounded.JSON_DECODE_ERRORS:  # GAP-91-02: deep nesting (RecursionError) is contained
        return None, "malformed_json"
    if not isinstance(body, dict) or body.get("result") != "ok" or not isinstance(body.get("data"), dict):
        return None, "unexpected_envelope"
    return body["data"], None


def _safe_label(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().rstrip(".").lower()
    if not DNS_LABEL_RE.fullmatch(candidate):
        return None
    return candidate


def _safe_tailnet_ipv4(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.split("/", 1)[0].strip()
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv4Address) and address in TAILNET_IPV4:
        return str(address)
    return None


def _host_hostname(host_info: dict[str, Any] | None) -> str | None:
    if not isinstance(host_info, dict):
        return None
    return _safe_label(host_info.get("hostname"))


def _iter_ipv4_values(interface: dict[str, Any]) -> list[str]:
    ipv4 = interface.get("ipv4")
    if not isinstance(ipv4, dict):
        return []
    values: list[str] = []
    single = ipv4.get("ip_address")
    if isinstance(single, str):
        values.append(single)
    for key in ("address", "addresses"):
        raw = ipv4.get(key)
        if isinstance(raw, str):
            values.append(raw)
        elif isinstance(raw, list):
            values.extend(item for item in raw if isinstance(item, str))
    return values


def _network_tailscale_ips(network_info: dict[str, Any] | None) -> set[str]:
    found: set[str] = set()
    if not isinstance(network_info, dict):
        return found
    interfaces = network_info.get("interfaces")
    if not isinstance(interfaces, list):
        return found
    for interface in interfaces:
        if not isinstance(interface, dict):
            continue
        if str(interface.get("interface", "")).lower() != "tailscale0":
            continue
        for raw in _iter_ipv4_values(interface):
            value = _safe_tailnet_ipv4(raw)
            if value:
                found.add(value)
    return found


def collect_tailscale_identity(
    token: str,
    *,
    json_getter: Callable[[str, str], tuple[dict[str, Any] | None, str | None]] = _json_get,
) -> dict[str, Any]:
    """Return a bounded default-role identity projection."""
    host_info, host_error = json_getter(HOST_INFO_PATH, token)
    addon_info, addon_error = json_getter(ADDON_INFO_PATH, token)
    network_info, network_error = json_getter(NETWORK_INFO_PATH, token)

    requested_hostname = _host_hostname(host_info)
    tailscale_ips = _network_tailscale_ips(network_info)
    tailscale_ipv4 = next(iter(tailscale_ips)) if len(tailscale_ips) == 1 else None

    installed = isinstance(addon_info, dict)
    state = addon_info.get("state") if installed and isinstance(addon_info.get("state"), str) else None
    version = addon_info.get("version") if installed and isinstance(addon_info.get("version"), str) else None

    status = (
        "PARTIAL"
        if requested_hostname or tailscale_ipv4
        else "PENDING_SOURCE_EVIDENCE"
    )

    return {
        "operation": "ha.network.tailscale.identity",
        "data": {
            "installed": installed,
            "state": state,
            "version": version,
            "requested_node_name": requested_hostname,
            "magicdns_hostname": None,
            "tailscale_ipv4": tailscale_ipv4,
            "identity_status": status,
            "fqdn_candidate_count": 0,
            "ipv4_candidate_count": len(tailscale_ips),
            "source_evidence": {
                "haos_host_info": host_error is None,
                "tailscale_addon_info": addon_error is None,
                "haos_network_info": network_error is None,
                "tailscale_latest_startup_logs": False,
                "hostname_contract": "tailscale_up_uses_haos_hostname",
            },
            "errors": {
                "haos_host_info": host_error,
                "tailscale_addon_info": addon_error,
                "haos_network_info": network_error,
                "tailscale_latest_startup_logs": "not_collected_default_role",
            },
            "credentials_exposed": False,
            "raw_logs_exposed": False,
            "control_authority": "none",
        },
    }
