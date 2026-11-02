"""House Brain Engineering Inspector 0.2.24: read-only Bluetooth and storage diagnostics (#212 / #1455 R6).

Owner request (Ondilo #1455, R6 capability gap, PR #1469): let ChatGPT/Claude check the Home Assistant Bluetooth
adapters, active-connection support, ESP32 proxy health and storage without asking the owner for screenshots.
Owner pop-ups 2026-11-02: "Claim 0.2.24/0.4.12 now" and storage from "HA system health (Recommended)".

What it reads (export mode only, through the Inspector's existing Core WebSocket and its existing token; no new
privilege, ``hassio_api`` stays false):

* ``bluetooth/subscribe_scanner_details``, ``bluetooth/subscribe_scanner_state`` and
  ``bluetooth/subscribe_connection_allocations``: Core's own in-memory Bluetooth manager. Each is subscribed, the
  events Core sends straight after its result are kept, and the subscription is closed again. Every 5-minute cycle.
* ``system_health/info``: only the ``hassio`` section of the ``initial`` event (the same data as Settings > System >
  Repairs > System information). At most once an hour; unsubscribing right after ``initial`` cancels Core's pending
  reachability checks.

What leaves the App (the ``diagnostics`` member of the entity snapshot), and nothing else:

* per scanner: an ordinal label (``scanner_1`` ...), the scanner name with every MAC-like and hex-serial part removed,
  its type (usb / uart / remote / unknown), ``connectable``, current and requested scan mode, connection slots,
  free slots and the NUMBER of connected devices. Never a MAC address, a device address, an advertisement or a UUID.
* storage: OS name, board, disk total and used (GB), eMMC life used (when the host reports it), Supervisor healthy and
  supported, NTP synchronized. Never nameservers, the App list, URLs or any other system-health section.

Everything here is pure (no socket, no clock): ``entity_sender`` passes in the events it read and the time. Any
failure becomes a constant reason code in that section; a diagnostics problem never stops the entity snapshot.
"""
from __future__ import annotations

import math
import re
import unicodedata
from typing import Any, Final

DIAGNOSTICS_SCHEMA: Final = "house_brain_diagnostics.v1"
SCANNER_DETAILS: Final = "bluetooth/subscribe_scanner_details"
SCANNER_STATE: Final = "bluetooth/subscribe_scanner_state"
CONNECTION_ALLOCATIONS: Final = "bluetooth/subscribe_connection_allocations"
SYSTEM_HEALTH: Final = "system_health/info"
SUBSCRIPTIONS: Final = (SCANNER_DETAILS, SCANNER_STATE, CONNECTION_ALLOCATIONS, SYSTEM_HEALTH)
STORAGE_INTERVAL_S: Final = 3600
MAX_SCANNERS: Final = 16
MAX_NAME: Final = 48
MAX_TEXT: Final = 64
MAX_SLOTS: Final = 64
SCANNER_TYPES: Final = frozenset({"usb", "uart", "remote", "unknown"})
SCAN_MODES: Final = frozenset({"passive", "active", "auto"})
# Constant reason codes this module (and the sender for socket failures) may put in a section.
REASONS: Final = frozenset({
    "OK", "BLUETOOTH_NOT_LOADED", "BLUETOOTH_RESULT_INVALID", "TOO_MANY_SCANNERS",
    "SYSTEM_HEALTH_NOT_AVAILABLE", "SYSTEM_HEALTH_RESULT_INVALID", "HASSIO_INFO_MISSING",
    "WS_READ_COMMAND_FAILED", "WS_UNKNOWN_COMMAND", "WS_TIMEOUT", "WS_READ_FAILED", "WS_RESULT_TOO_LARGE", "WS_COMMAND_NOT_ALLOWLISTED",
    "DIAGNOSTICS_INTERNAL_ERROR",
})

_SPLIT_RE: Final = re.compile(r"[^A-Za-z0-9]+")
_HEX_RE: Final = re.compile(r"\A[0-9A-Fa-f]+\Z")
_ADAPTER_RE: Final = re.compile(r"\Ahci[0-9]{1,2}\Z")
_GB_RE: Final = re.compile(r"\A([0-9]{1,7}(?:\.[0-9]{1,3})?) GB\Z")
_PERCENT_RE: Final = re.compile(r"\A([0-9]{1,3}) %\Z")
_TEXT_RE: Final = re.compile(r"\A[A-Za-z0-9 ._()+-]{1,64}\Z")


def _hex(token: str) -> bool:
    return _HEX_RE.fullmatch(token) is not None


def _drop(tokens: list[str]) -> list[bool]:
    """Which tokens are address or serial material: any run of 3+ short hex tokens (a MAC or MAC tail in any separator
    style, an IPv4 address, a dotted MAC), any hex token of 6+ characters, any hex token of 4+ with a digit."""
    drop = [_hex(t) and (len(t) >= 6 or (len(t) >= 4 and any(c.isdigit() for c in t))) for t in tokens]
    i = 0
    while i < len(tokens):
        j = i
        while j < len(tokens) and _hex(tokens[j]) and len(tokens[j]) <= 4:
            j += 1
        if j - i >= 3:
            for k in range(i, j):
                drop[k] = True
        i = max(j, i + 1)
    return drop


def address_like(text: str) -> bool:
    """True when the text still carries address-shaped material (the perimeter applies the same rule)."""
    tokens = [t for t in _SPLIT_RE.split(text) if t]
    return any(_drop(tokens))


class DiagnosticsError(ValueError):
    """Constant reason codes only."""


def clean_name(value: Any) -> str | None:
    """Scanner name as ASCII letter/digit words joined by "-", without any address or serial material."""
    if type(value) is not str:
        return None
    text = unicodedata.normalize("NFKC", value)          # fullwidth ":" and digits become ASCII first
    tokens = [t for t in _SPLIT_RE.split(text) if t]
    kept = [t for t, gone in zip(tokens, _drop(tokens)) if not gone]
    out = "-".join(kept)[:MAX_NAME].strip("-")
    return out if out and not address_like(out) else None


def _text(value: Any) -> str | None:
    return value if type(value) is str and _TEXT_RE.fullmatch(value) and not address_like(value) else None


def _small_int(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= MAX_SLOTS else None


def _events(rows: Any) -> list[Any]:
    if type(rows) is not list:
        raise DiagnosticsError("BLUETOOTH_RESULT_INVALID")
    return rows


def bluetooth_section(details: Any, states: Any, allocations: Any, *, observed_at: str) -> dict[str, Any]:
    """Join the three Bluetooth subscriptions by their (never exported) source into sanitized scanner rows."""
    scanners: dict[str, dict[str, Any]] = {}
    for event in _events(details):
        if type(event) is not dict:
            raise DiagnosticsError("BLUETOOTH_RESULT_INVALID")
        for item in event.get("add", []) if type(event.get("add", [])) is list else []:
            if type(item) is not dict or type(item.get("source")) is not str or not item["source"]:
                raise DiagnosticsError("BLUETOOTH_RESULT_INVALID")
            scanners[item["source"]] = {
                "name": clean_name(item.get("name")),
                "adapter": item["adapter"] if type(item.get("adapter")) is str and _ADAPTER_RE.fullmatch(
                    item["adapter"]) else None,
                "scanner_type": item["scanner_type"] if item.get("scanner_type") in SCANNER_TYPES else "unknown",
                "connectable": item.get("connectable") is True,
                "current_mode": None, "requested_mode": None,
                "slots": None, "free_slots": None, "connected_devices": None,
            }
        for item in event.get("remove", []) if type(event.get("remove", [])) is list else []:
            if type(item) is dict and type(item.get("source")) is str:
                scanners.pop(item["source"], None)
    if len(scanners) > MAX_SCANNERS:
        raise DiagnosticsError("TOO_MANY_SCANNERS")
    for event in _events(states):
        if type(event) is dict and event.get("source") in scanners:
            row = scanners[event["source"]]
            row["current_mode"] = event.get("current_mode") if event.get("current_mode") in SCAN_MODES else None
            row["requested_mode"] = event.get("requested_mode") if event.get("requested_mode") in SCAN_MODES else None
    for event in _events(allocations):
        for item in event if type(event) is list else []:
            if type(item) is dict and item.get("source") in scanners:
                row = scanners[item["source"]]
                slots, free = _small_int(item.get("slots")), _small_int(item.get("free"))
                allocated = item.get("allocated")
                row["slots"], row["free_slots"] = slots, free
                row["connected_devices"] = len(allocated) if type(allocated) is list and len(
                    allocated) <= MAX_SLOTS else None
    # Ordinal labels in a stable order that does not depend on the hidden source address.
    ordered = sorted(scanners.items(), key=lambda kv: (kv[1]["scanner_type"], kv[1]["name"] or "", kv[1]["adapter"] or "",
                                                       kv[0]))
    rows = [{"scanner": f"scanner_{i}", **row} for i, (_, row) in enumerate(ordered, start=1)]
    return {"observed_at": observed_at, "status": "OK", "reason": "OK", "scanners": rows}


def _gb(value: Any) -> float | None:
    match = _GB_RE.fullmatch(value) if type(value) is str else None
    if match is None:
        return None
    number = float(match.group(1))
    return number if math.isfinite(number) else None


def _flag(value: Any) -> bool | None:
    if value is True:
        return True
    if type(value) is dict and value.get("type") == "failed":
        return False
    if value is False:
        return False
    return None


def storage_section(events: Any, *, observed_at: str) -> dict[str, Any]:
    """Only the ``hassio`` section of system health's ``initial`` event, field by field."""
    if type(events) is not list:
        raise DiagnosticsError("SYSTEM_HEALTH_RESULT_INVALID")
    initial = next((e for e in events if type(e) is dict and e.get("type") == "initial"), None)
    if initial is None or type(initial.get("data")) is not dict:
        raise DiagnosticsError("SYSTEM_HEALTH_RESULT_INVALID")
    hassio = initial["data"].get("hassio")
    info = hassio.get("info") if type(hassio) is dict else None
    if type(info) is not dict:
        raise DiagnosticsError("HASSIO_INFO_MISSING")
    life = _PERCENT_RE.fullmatch(info["disk_life_time"]) if type(info.get("disk_life_time")) is str else None
    total, used = _gb(info.get("disk_total")), _gb(info.get("disk_used"))
    if total is not None and used is not None and (total <= 0 or used > total):
        total = used = None
    return {
        "observed_at": observed_at, "status": "OK", "reason": "OK",
        "host_os": _text(info.get("host_os")),
        "board": _text(info.get("board")),
        "disk_total_gb": total,
        "disk_used_gb": used,
        "disk_life_time_percent": int(life.group(1)) if life and int(life.group(1)) <= 100 else None,
        "supervisor_healthy": _flag(info.get("healthy")),
        "supervisor_supported": _flag(info.get("supported")),
        "ntp_synchronized": info.get("ntp_synchronized") if type(info.get("ntp_synchronized")) is bool else None,
    }


def error_section(reason: str, *, observed_at: str) -> dict[str, Any]:
    return {"observed_at": observed_at, "status": "ERROR",
            "reason": reason if reason in REASONS else "DIAGNOSTICS_INTERNAL_ERROR"}


def _reason(exc: BaseException, unknown: str) -> str:
    code = str(exc)
    if code == "WS_UNKNOWN_COMMAND":   # Core has no such command: the integration is not loaded
        return unknown
    return code if code in REASONS else "DIAGNOSTICS_INTERNAL_ERROR"


class DiagnosticsCollector:
    """Keeps the last storage section between its hourly reads; Bluetooth is read every cycle."""

    def __init__(self, storage_interval_s: int = STORAGE_INTERVAL_S) -> None:
        self.storage_interval_s = storage_interval_s
        self._storage: dict[str, Any] | None = None
        self._storage_ts: float | None = None

    async def collect(self, subscribe: Any, *, now_ts: float, observed_at: str) -> dict[str, Any]:
        """``subscribe(command_type, until=None)`` returns the event payloads of one bounded subscription."""
        try:
            details = await subscribe(SCANNER_DETAILS)
            states = await subscribe(SCANNER_STATE)
            allocations = await subscribe(CONNECTION_ALLOCATIONS)
            bluetooth = bluetooth_section(details, states, allocations, observed_at=observed_at)
        except Exception as exc:  # noqa: BLE001 - constant reason code; the entity snapshot is unaffected
            bluetooth = error_section(_reason(exc, "BLUETOOTH_NOT_LOADED"), observed_at=observed_at)
        if self._storage_ts is None or now_ts - self._storage_ts >= self.storage_interval_s or now_ts < self._storage_ts:
            self._storage_ts = now_ts   # an error is also kept for the hour: Core is never asked more often
            try:
                events = await subscribe(SYSTEM_HEALTH, until=lambda e: type(e) is dict and e.get("type") == "initial")
                self._storage = storage_section(events, observed_at=observed_at)
            except Exception as exc:  # noqa: BLE001
                self._storage = error_section(_reason(exc, "SYSTEM_HEALTH_NOT_AVAILABLE"), observed_at=observed_at)
        return {"schema": DIAGNOSTICS_SCHEMA, "bluetooth": bluetooth, "storage": self._storage}
