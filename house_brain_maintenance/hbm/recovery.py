"""Recovery Report (0.3.0): what happened while Home Assistant was down, and what to do.

Owner decisions D1-D6 (2026-10-01; ``docs/architecture/HA_RECOVERY_REPORT_R1_APP_AND_WORKER_DESIGN.md``).
Read-only: it never restarts, reloads, starts or changes anything. It only reads, records and tells.

A background thread runs ``tick()`` every ``HEARTBEAT_SECONDS``:

* writes ``/data/recovery_heartbeat.json`` (last alive time, host boot, Core/OS version, UPS on battery);
* watches Home Assistant Core (``/core/api/``) and the #73 network/UPS sensors while it runs;
* when Core comes back, a host comes back after a reboot, or a network outage ends, it builds one
  incident: duration, likely cause (rules R0-R8) with confidence, evidence, owner steps, and for a
  crash or unexpected reboot the last error lines of the log from before it;
* runs a recovery checklist for 30 minutes (smoke/CO first);
* shows the latest incidents on the Maintenance page and as a persistent notification, and pushes
  once when the internet is back. Short planned restarts are recorded but not pushed. A P0 check
  that stays in ATTENTION pushes to every ``safety_notify_services`` phone, once;
* optionally sends a signed "still alive" ping to the owner's liveness Worker (off by default).

0.5.2 (owner "All of the above", 2026-10-05), still read-only:

* the Connection Forensics verdict is trusted only when it was written for *this* start (its ``last_start``
  is not older than the outage start); until then the report waits (at most ``FORENSICS_WAIT``) and then
  says "verdict not yet available" instead of using the previous restart's verdict. The install restart
  itself (``FIRST_RUN``) is recorded quietly (no push);
* a CLEAN Core restart is tagged PLANNED (with the Deployer request id) or UNPLANNED_CLEAN from the House
  Brain Deployer's status entity (``DEPLOYING`` seen just before the outage, or a restart outcome published
  after it); UNCLEAN stays UNCLEAN;
* a rolling 90-day restart ledger (counts by class, mean time between unplanned failures, a feed text for
  the #152/#163 stability ledger);
* after a Core restart, how long ZHA / Z-Wave JS / Matter took until their devices were back (P2 checks).

Only exact, pinned entity ids are read (``READ_ENTITIES``). Log text is scrubbed before it is kept.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import issues as I, net
from .ha import (DEPLOYER_STATUS_ENTITY, FORENSICS_ENTITY, RECOVERY_NOTIFICATION_ID, RECOVERY_READ_ENTITIES,
                 ForbiddenCall, HAError, HomeAssistant)

LOG = logging.getLogger("hbm")

DOC_HEART = "recovery_heartbeat"
DOC_STATE = "recovery"
NOTIFICATION_ID = RECOVERY_NOTIFICATION_ID
HEARTBEAT_SECONDS = 60.0
APP_RESTART_GAP = 180.0          # an App restart/update shorter than this is not an outage
NET_MIN_SECONDS = 180.0          # a network problem shorter than this is not reported
QUIET_PLANNED_SECONDS = 600.0    # planned restarts shorter than this are page-only (no push)
CHECK_WINDOW = 1800.0            # checklist runs for 30 minutes after recovery
RETRY_FOR = 86400.0              # an undelivered push is retried for 24 h
RETRY_EVERY = 300.0
MAX_INCIDENTS = 10
MAX_LOG_LINES = 20
# 0.5.2
FORENSICS_WAIT = 300.0           # wait this long after Core answers for this start's forensics verdict
FORENSICS_TOLERANCE = 5.0        # last_start may be this much older than the first missed check
DEPLOYER_LOOKBACK = 180.0        # DEPLOYING must have been read this close before the outage started
DEPLOYER_JOIN_WINDOW = 1800.0    # after Core is back, a Deployer restart outcome is joined this long
RESTART_OUTCOMES = frozenset({"SUCCEEDED", "ROLLED_BACK_RESTARTED", "FAILED_MANUAL"})  # hbd/engine.py
MESH_WINDOW = 600.0              # mesh rejoin is measured for 10 minutes after Core answers again
MESH_BASELINE_EVERY = 21600.0    # "unavailable before the restart" baseline, refreshed every 6 h
MESH_RETRY = 3600.0
LEDGER_DAYS = 90
LEDGER_MAX = 400

# -- pinned inputs (exact entity ids; nothing else is read) --------------------------------------
FORENSICS = FORENSICS_ENTITY
DEPLOYER = DEPLOYER_STATUS_ENTITY
NET_CLASS = "sensor.house_brain_network_outage_class"
NET_SUMMARY = "sensor.house_brain_network_outage_summary"
NET_WAN = "sensor.house_brain_network_wan_status"
UPS_STATUS = "sensor.ups_status_data"
UPS_CHARGE = "sensor.ups_battery_charge"
OUTAGE_CLASSES = frozenset({"ISP_OUTAGE", "UTILITY_POWER_OUTAGE", "LOCAL_LAN_FAULT", "PARTIAL"})

P0, P1, P2 = "P0", "P1", "P2"
RECOVERED, PENDING, ATTENTION, NOT_OBSERVED = "RECOVERED", "PENDING", "ATTENTION", "NOT_OBSERVED"
LIVENESS_DOC = "liveness"           # read by the problem watch (0.5.1)


@dataclass(frozen=True)
class Check:
    check_id: str
    priority: str
    entities: tuple[str, ...]
    deadline: float               # seconds after recovery before PENDING becomes ATTENTION
    label: str
    step: str


CHECKS: tuple[Check, ...] = (
    Check("SMOKE_CO_SENSORS", P0, ("binary_sensor.any_smoke_detected", "binary_sensor.any_co_detected"), 900,
          "Smoke/CO sensors reporting",
          "Smoke/CO sensors are not reporting: open the Ring app and check the alarm base station is online."),
    Check("THERMOSTAT", P1, ("climate.ecobee_thermostat",), 900, "Thermostat online",
          "Thermostat offline: check its Wi-Fi. It keeps running its own schedule meanwhile."),
    Check("SENSE", P2, ("sensor.sense_51446_l1_voltage",), 900, "Sense energy monitor",
          "Sense still offline: usually recovers by itself; if not, check the Sense monitor's Wi-Fi."),
    Check("SOLAR_ENVOY", P2, ("sensor.enphase_solar_power_now",), 900, "Solar (Envoy)",
          "Solar data still missing: the Envoy can take 15-30 min to come back after a power cut."),
)
UPS_CHECK = Check("UPS_RECHARGED", P1, (UPS_STATUS, UPS_CHARGE), 0, "UPS back on mains and recharged",
                  "UPS not back on mains or not recharging: check the UPS display and its wall outlet.")
# 0.5.2: mesh rejoin after a Core restart (read from get_states + the entity registry; absent meshes skipped).
MESH_CHECKS: tuple[Check, ...] = (
    Check("MESH_ZHA", P2, (), MESH_WINDOW, "Zigbee (ZHA) devices back",
          "Zigbee devices still unavailable after 10 min: check the Zigbee coordinator, then the devices in "
          "Settings › Devices & services › Zigbee."),
    Check("MESH_ZWAVE", P2, (), MESH_WINDOW, "Z-Wave devices back",
          "Z-Wave devices still unavailable after 10 min: check the Z-Wave JS App is running, then the devices."),
    Check("MESH_MATTER", P2, (), MESH_WINDOW, "Matter devices back",
          "Matter devices still unavailable after 10 min: check the Matter Server App and the Thread border router."),
)
MESH_PLATFORM = {"MESH_ZHA": "zha", "MESH_ZWAVE": "zwave_js", "MESH_MATTER": "matter"}
READ_ENTITIES = frozenset({FORENSICS, DEPLOYER, NET_CLASS, NET_SUMMARY, NET_WAN, UPS_STATUS, UPS_CHARGE}
                          | {e for c in CHECKS for e in c.entities})
assert READ_ENTITIES == RECOVERY_READ_ENTITIES  # noqa: S101 - the allowlist in ha.py is the single source

RULE_TEXT = {
    "R1": "Planned update",
    "R2": "Planned restart (a clean shutdown ran first)",
    "R3": "Home Assistant host was rebooted cleanly",
    "R4": "Power cut: the UPS ran out of battery",
    "R5": "The Home Assistant host rebooted unexpectedly",
    "R6": "Home Assistant crashed or froze",
    "R7": "Network outage (Home Assistant kept running)",
    "R8": "Home Assistant restarted; the reason could not be told",
}
RULE_STEPS = {
    "R1": ("Nothing to do unless something below needs attention.",),
    "R2": ("Usually nothing to do. A Deployer install, an update or a restart from Settings causes this.",),
    "R3": ("Usually nothing to do (an OS update or a reboot from Settings).",),
    "R4": ("Check the UPS is back on mains and recharging.",
           "The router and modem are not on the UPS, so a power cut also cuts the internet (open decision)."),
    "R5": ("If this repeats, check the SD card / SSD health in Settings › System › Hardware.",
           "The last error lines from before the reboot are shown below, if any."),
    "R6": ("Open Settings › System › Logs. The error lines from just before the crash are below.",
           "If an integration is named there, check it in Settings › Devices & services."),
    "R7": ("Nothing to fix in Home Assistant. If the internet is still slow, power-cycle the modem and router.",),
    "R8": ("Install the Connection Forensics package (queued) to tell planned restarts from crashes.",
           "Generic steps: docs/runbooks/HA_OUTAGE_RECOVERY.md."),
}
CONFIDENCE = {"R1": "high", "R2": "high", "R3": "medium", "R4": "high", "R5": "medium", "R6": "medium",
              "R7": "from the network sensors", "R8": "low"}

ANSI = re.compile(r"\x1b\[[0-9;]*m")
LOG_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:\.\d+)? (ERROR|CRITICAL)\b")


# -- pure helpers (unit-tested) ---------------------------------------------------------------------
def local_hm(ts: float) -> str:
    lt = time.localtime(ts)
    return f"{lt.tm_hour % 12 or 12}:{lt.tm_min:02d} {'am' if lt.tm_hour < 12 else 'pm'}"


def duration_text(seconds: float) -> str:
    minutes = max(1, int(round(seconds / 60)))
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


def classify_core_return(*, version_before: str | None, version_after: str | None,
                         forensics: str | None) -> str:
    """Core was seen down and is back while this App kept running (host did not reboot)."""
    if version_before and version_after and version_before != version_after:
        return "R1"
    if forensics == "CLEAN":
        return "R2"
    if forensics == "UNCLEAN":
        return "R6"
    return "R8"


def classify_host_return(*, ups_on_battery: bool | None, forensics: str | None,
                         os_before: str | None, os_after: str | None) -> str:
    """The App itself restarted because the host rebooted (boot time changed)."""
    if ups_on_battery:
        return "R4"
    if os_before and os_after and os_before != os_after:
        return "R1"
    if forensics == "CLEAN":
        return "R3"
    return "R5"


def notify_worthy(rule: str, duration: float, verdict: str | None = None) -> bool:
    """Planned restarts shorter than QUIET_PLANNED_SECONDS stay on the page only. The restart that installs
    Connection Forensics (verdict FIRST_RUN, 0.5.2) is page-only too, unless the UPS ran out (R4)."""
    if verdict == "FIRST_RUN" and rule != "R4":
        return False
    return not (rule in ("R1", "R2", "R3") and duration < QUIET_PLANNED_SECONDS)


def fresh_verdict(row: dict | None, outage_start: float) -> str | None:
    """The Connection Forensics verdict, only when it was written for THIS start (0.5.2 race fix).

    Core answers ``/core/api/`` before its start-marker automation runs, so right after a restart the
    sensor still shows the previous restart's verdict. Its ``last_start`` tells: it must not be older than
    the outage start (less ``FORENSICS_TOLERANCE``). A missing ``last_start`` is never trusted."""
    if not row or row.get("state") not in ("CLEAN", "UNCLEAN", "FIRST_RUN"):
        return None
    last_start = (row.get("attrs") or {}).get("last_start")
    if not isinstance(last_start, int) or isinstance(last_start, bool) \
            or last_start < outage_start - FORENSICS_TOLERANCE:
        return None
    return row["state"]


def deployer_signal(row: dict | None) -> dict:
    """What the Deployer status entity says (0.5.2): a request it is installing (``DEPLOYING``, its marker
    state), a restart outcome it just published, and its ``last_result`` (request id + outcome)."""
    state = (row or {}).get("state")
    attrs = (row or {}).get("attrs") or {}
    rid = attrs.get("request_id")
    live = not attrs.get("dry_run")
    last = (f"{attrs['last_rid']}: {attrs['last_outcome']}"
            if attrs.get("last_rid") and attrs.get("last_outcome") else None)
    return {"deploying": rid if state == "DEPLOYING" and rid else None,
            "done": rid if state in RESTART_OUTCOMES and rid and live else None,
            "last": last,
            "last_rid": attrs.get("last_rid") if attrs.get("last_outcome") in RESTART_OUTCOMES and live else None}


def restart_class(rule: str, verdict: str | None, tag: str | None) -> str | None:
    """Ledger class of a Core restart (None for a network outage, which is not a restart)."""
    if rule == "R7":
        return None
    if rule == "R4":
        return "POWER_CUT"
    if rule == "R1":
        return "UPDATE"
    if verdict == "FIRST_RUN":
        return "INSTALL"
    if rule == "R2":
        return "PLANNED" if tag == "PLANNED" else "UNPLANNED_CLEAN"
    return {"R3": "HOST_REBOOT", "R5": "HOST_UNEXPECTED", "R6": "UNCLEAN"}.get(rule, "UNKNOWN")


LEDGER_CLASSES = ("PLANNED", "UPDATE", "INSTALL", "UNPLANNED_CLEAN", "UNCLEAN", "HOST_REBOOT", "HOST_UNEXPECTED",
                  "POWER_CUT", "UNKNOWN")
MTBF_CLASSES = frozenset({"UNCLEAN", "HOST_UNEXPECTED", "POWER_CUT"})


def prune_ledger(ledger: list[dict], now: float) -> list[dict]:
    """Keep 90 days, at most LEDGER_MAX rows, oldest first."""
    keep = [e for e in ledger if isinstance(e, dict) and float(e.get("t") or 0) >= now - LEDGER_DAYS * 86400]
    return sorted(keep, key=lambda e: float(e["t"]))[-LEDGER_MAX:]


def ledger_stats(ledger: list[dict], now: float, since: float | None) -> dict:
    """30/90-day counts by class and the mean time between unplanned failures (observed time / failures).

    Unplanned failures are UNCLEAN, HOST_UNEXPECTED and POWER_CUT. UNPLANNED_CLEAN (a clean restart the
    Deployer did not cause, e.g. from Settings) and UNKNOWN are counted but are not failures."""
    out: dict[str, Any] = {}
    for days in (30, 90):
        rows = [e for e in ledger if float(e.get("t") or 0) >= now - days * 86400]
        out[f"restarts_{days}d"] = {c: n for c in LEDGER_CLASSES
                                    if (n := sum(1 for e in rows if e.get("c") == c))}
    failures = [e for e in ledger if e.get("c") in MTBF_CLASSES
                and float(e.get("t") or 0) >= now - LEDGER_DAYS * 86400]
    observed = max(0.0, min(LEDGER_DAYS * 86400.0, now - (since if since is not None else now)))
    out["observed_days"] = round(observed / 86400, 1)
    out["unplanned_failures_90d"] = len(failures)
    out["mtbf_unplanned_days"] = round(observed / 86400 / len(failures), 1) if failures else None
    out["last_restart_class"] = ledger[-1].get("c") if ledger else None
    return out


def ledger_feed(ledger: list[dict], now: float, since: float | None) -> str:
    """Plain text for the #152 / #163 stability ledger (the owner or an AI session pastes it; nothing is posted)."""
    st = ledger_stats(ledger, now, since)

    def counts(d: dict) -> str:
        return ", ".join(f"{k} {v}" for k, v in d.items()) or "none"
    mtbf = (f"{st['mtbf_unplanned_days']} days" if st["mtbf_unplanned_days"] is not None
            else f"no unplanned failure in {st['observed_days']} days observed")
    lines = [f"House Brain Maintenance restart ledger ({time.strftime('%Y-%m-%d', time.gmtime(now))} UTC; "
             f"observed {st['observed_days']} of {LEDGER_DAYS} days)",
             f"30 d: {counts(st['restarts_30d'])}", f"90 d: {counts(st['restarts_90d'])}",
             f"Unplanned failures (UNCLEAN, HOST_UNEXPECTED, POWER_CUT) 90 d: {st['unplanned_failures_90d']}; "
             f"mean time between them: {mtbf}"]
    for e in ledger[-10:][::-1]:
        mesh = ", ".join(f"{k} {v if v is not None else '>600'} s" for k, v in (e.get("m") or {}).items())
        lines.append(f"- {time.strftime('%Y-%m-%d %H:%M', time.gmtime(float(e['t'])))}Z {e.get('c')} "
                     f"{e.get('r')} verdict {e.get('v') or '-'} down {int(e.get('d') or 0)} s"
                     + (f" request {e['q']}" if e.get("q") else "") + (f" mesh: {mesh}" if mesh else ""))
    return "\n".join(lines)


def error_lines(text: str, *, before: float | None = None, limit: int = MAX_LOG_LINES) -> list[str]:
    """Last ERROR/CRITICAL lines (scrubbed), optionally only those logged before ``before`` + 60 s."""
    out = []
    for raw in text.splitlines()[-4000:]:
        line = ANSI.sub("", raw).strip()
        m = LOG_LINE.match(line)
        if not m:
            continue
        if before is not None:
            try:
                ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                continue
            if ts > before + 60:
                continue
        out.append(I.scrub(line, 300))
    return out[-limit:]


def eval_check(check: Check, states: dict[str, dict | None], since: float) -> str:
    """One checklist item. ``since`` = seconds since recovery."""
    if check.check_id == UPS_CHECK.check_id:
        status, charge = states.get(UPS_STATUS), states.get(UPS_CHARGE)
        if status is None:
            return NOT_OBSERVED
        flags = str(status.get("state") or "").split()
        if "OB" in flags:
            return ATTENTION
        try:
            pct = float(str((charge or {}).get("state")))
        except (TypeError, ValueError):
            return NOT_OBSERVED
        return RECOVERED if "OL" in flags and pct >= 95.0 else PENDING
    rows = [states.get(e) for e in check.entities]
    if all(r is None for r in rows):
        return NOT_OBSERVED
    if all(r is not None and r.get("state") not in ("unavailable", "unknown", None) for r in rows):
        return RECOVERED
    return ATTENTION if since > check.deadline else PENDING


def push_text(inc: dict) -> tuple[str, str]:
    if inc["kind"] == "network":
        title = "Home network outage ended"
        head = f"{local_hm(inc['start'])} → {local_hm(inc['end'])} ({duration_text(inc['end'] - inc['start'])})."
    else:
        title = "Home Assistant is back"
        head = f"Down {local_hm(inc['start'])} → {local_hm(inc['end'])} ({duration_text(inc['end'] - inc['start'])})."
    cause = f"Likely: {inc['cause']} ({inc['confidence']} confidence)."
    need = [CHECK_LABELS.get(k, k) for k, v in inc.get("checks", {}).items() if v == ATTENTION]
    tail = f" {len(need)} check(s) need you: {', '.join(need)}." if need else ""
    return title, f"{head} {cause}{tail} Open Maintenance for details."[:900]


CHECK_LABELS = {c.check_id: c.label for c in (*CHECKS, UPS_CHECK, *MESH_CHECKS)}


def sign(key: str, body: bytes) -> str:
    return hmac.new(key.encode("utf-8"), body, hashlib.sha256).hexdigest()


# -- settings ----------------------------------------------------------------------------------------
@dataclass
class RecoverySettings:
    notify_service: str
    safety_notify_services: tuple[str, ...] = ()
    liveness_url: str = ""
    liveness_key: str = ""
    liveness_interval: float = 120.0
    open_url: str | None = None
    owner_user_id: Callable[[], str | None] = field(default=lambda: None)
    scale: float = 1.0           # test mode time scale


# -- the monitor --------------------------------------------------------------------------------------
class Recovery:
    def __init__(self, ha: HomeAssistant, journal, settings: RecoverySettings,
                 clock: Callable[[], float] = time.time) -> None:
        self.ha = ha
        self.j = journal
        self.s = settings
        self.clock = clock
        self.lock = threading.Lock()
        self.st = self._load()
        self._last_ping = 0.0
        self._ping_seq = int(self.st.get("ping_seq", 0))
        self._ping_failed_logged = 0.0
        self.ack_nonce = secrets.token_hex(12)
        self._ack_requested = False
        self._ack_lock = threading.Lock()      # short lock for the page thread (never held during I/O)
        self._view: list[dict] = json.loads(json.dumps(self.st["incidents"]))
        self._saved = json.dumps(self.st, sort_keys=True)
        self._detected = False
        self._boot: str | None = None
        self._os_now: str | None = None
        self._down_count = 0
        self._streak_opened = True
        self._dep_mem: dict | None = None      # 0.5.2: last Deployer status read while Core was up
        self._mesh_base_try = 0.0
        self._ledger_view: tuple[list[dict], float | None] = (json.loads(json.dumps(self.st["ledger"])),
                                                              self.st.get("ledger_since"))

    # -- persistence -------------------------------------------------------------
    def _load(self) -> dict:
        st = self.j.load_doc(DOC_STATE, {})
        st.setdefault("incidents", [])
        st.setdefault("core_down_since", None)
        st.setdefault("net_bad_since", None)
        st.setdefault("net_classes", [])
        st.setdefault("net_ups_ob", False)
        st.setdefault("startup_pending", None)
        st.setdefault("last_core_version", None)
        st.setdefault("last_os_version", None)
        st.setdefault("ups_ob_since", None)
        # 0.5.2
        st.setdefault("return_seen_at", None)   # first time Core answered after the outage (verdict wait)
        st.setdefault("dep_at_down", None)      # Deployer status as last read before the outage
        st.setdefault("mesh_run", None)         # mesh rejoin measurement of the latest Core return
        st.setdefault("mesh_baseline", None)    # mesh entities unavailable before the restart
        st.setdefault("ledger", [])
        st.setdefault("ledger_since", None)
        return st

    def _save(self) -> None:
        """Write the state only when it changed (the heartbeat is the only per-minute write)."""
        self.st["ping_seq"] = self._ping_seq
        text = json.dumps(self.st, sort_keys=True)
        if text != self._saved:
            self.j.save_doc(DOC_STATE, self.st)
            self._saved = text
        with self._ack_lock:
            self._view = json.loads(json.dumps(self.st["incidents"]))
            self._ledger_view = (json.loads(json.dumps(self.st["ledger"])), self.st.get("ledger_since"))

    def heartbeat(self) -> dict:
        return self.j.load_doc(DOC_HEART, {})

    def _write_heartbeat(self, now: float, boot: str | None, *, clean_stop: bool = False) -> None:
        self.j.save_doc(DOC_HEART, {"v": 1, "ts": now, "boot": boot,
                                    "core": self.st.get("last_core_version"),
                                    "os": self.st.get("last_os_version"),
                                    "ups_on_battery": self.st.get("ups_ob_since") is not None,
                                    "ups_ob_since": self.st.get("ups_ob_since"),
                                    "clean_stop": clean_stop})

    # -- reads (all bounded, all optional) ----------------------------------------
    def _host(self) -> tuple[str | None, str | None]:
        try:
            info = self.ha.host_boot_info()
        except (HAError, net.NetError, ForbiddenCall):
            return None, None
        return info

    def _states(self, entities) -> dict[str, dict | None]:
        out: dict[str, dict | None] = {}
        for e in entities:
            try:
                out[e] = self.ha.entity_state(e)
            except (HAError, net.NetError, ForbiddenCall):
                out[e] = None
        return out

    # -- lifecycle -----------------------------------------------------------------
    def start(self) -> None:
        """At App start: if the host rebooted while we were away, remember to report it."""
        self._detect(self.clock())

    def _detect(self, now: float) -> None:
        """Compare the last heartbeat with this boot. Retried every tick until the host boot is known;
        until then the old heartbeat is kept (it is the only record of when the house went quiet)."""
        if self._detected:
            return
        boot, os_version = self._host()
        if not boot:
            return
        self._detected = True
        hb = self.heartbeat()
        if hb and hb.get("boot") and hb.get("boot") != boot and not self.st.get("startup_pending"):
            self.st["startup_pending"] = {"start": float(hb.get("ts") or now), "ups_on_battery": bool(
                hb.get("ups_on_battery")), "ups_ob_since": hb.get("ups_ob_since"),
                "os_before": hb.get("os"), "boot_before": hb.get("boot"),
                "net_before": list(self.st.get("net_classes") or []) if self.st.get("net_bad_since") else []}
            # A network/UPS outage that was still open when the host went down belongs to this report.
            self.st["net_bad_since"], self.st["net_classes"], self.st["net_ups_ob"] = None, [], False
            self.st["ups_ob_since"] = None
            self.j.audit("recovery", "HOST_REBOOT_SEEN", gap=int(now - float(hb.get("ts") or now)))
        elif hb and now - float(hb.get("ts") or now) >= APP_RESTART_GAP:
            self.j.audit("recovery", "APP_WAS_STOPPED", gap=int(now - float(hb.get("ts") or now)))
        self._boot = boot
        self._os_now = os_version
        if os_version:
            self.st["last_os_version"] = os_version     # the next reboot compares with this OS version
        self._save()
        self._write_heartbeat(now, boot)

    def stop(self) -> None:
        with self.lock:
            if self._detected:
                self._write_heartbeat(self.clock(), self._boot, clean_stop=True)

    # -- one tick ----------------------------------------------------------------------
    def tick(self) -> None:
        with self.lock:
            now = self.clock()
            self._detect(now)
            if self.st.get("ledger_since") is None:
                self.st["ledger_since"] = now           # 0.5.2: MTBF counts observed time from here
            alive = self.ha.core_alive()
            if not alive:
                self._down_count += 1
                if self._down_count == 1:
                    # Only a streak that opened the outage may be forgiven as a one-check blip.
                    self._streak_opened = self.st["core_down_since"] is None
                if self.st["core_down_since"] is None:
                    self.st["core_down_since"] = now
                    self.st["dep_at_down"] = self._dep_before(now)
                    self.j.audit("recovery", "CORE_DOWN_SEEN")
                # Core went away again while the report waited for this start's verdict: wait anew.
                self.st["return_seen_at"] = None
            else:
                if self._down_count == 1 and self.st["core_down_since"] is not None \
                        and not self.st.get("startup_pending") and self._streak_opened:
                    # One missed check is a slow answer, not an outage.
                    self.st["core_down_since"] = None
                    self.j.audit("recovery", "CORE_BLIP_IGNORED")
                self._down_count = 0
                self._core_up(now)
                self._mesh_tick(now)
                self._deployer_tick(now)
                self._network(now)
            self._advance(now)
            self._deliver(now)
            if self._ack_requested:
                for inc in self.st["incidents"]:
                    inc["acked"] = True
                self._apply_ack()
            self._save()
            if self._detected:
                self._write_heartbeat(now, self._boot)
            self._ping(now)

    def _core_up(self, now: float) -> None:
        try:
            version = self.ha.core_version()
        except (HAError, net.NetError, ForbiddenCall):
            version = None
        pending = self.st.get("startup_pending")
        down = self.st.get("core_down_since")
        if not pending and down is None:
            if version:
                self.st["last_core_version"] = version
            return
        # 0.5.2 race fix: Core answers /core/api/ before its start-marker automation has run, so the sensor
        # may still hold the previous restart's verdict. Use it only once it was written for this start;
        # wait at most FORENSICS_WAIT, then say the verdict is not available (never a stale one).
        start = float(pending["start"]) if pending else float(down)
        first = self.st.get("return_seen_at")
        if first is None:
            first = self.st["return_seen_at"] = now
            self._mesh_begin(first)
            self.j.audit("recovery", "CORE_BACK_SEEN")
        row = self._states([FORENSICS]).get(FORENSICS)
        verdict = fresh_verdict(row, start)
        if verdict is None and now - first < FORENSICS_WAIT * self.s.scale:
            return                                   # not trusted yet: look again next tick
        shown = verdict if verdict is not None else ("LATE" if row is not None else None)
        ready = (row or {}).get("attrs", {}).get("stop_to_ready_seconds") if verdict else None
        if pending:
            rule = classify_host_return(ups_on_battery=pending.get("ups_on_battery"), forensics=verdict,
                                        os_before=pending.get("os_before"), os_after=self._os_now)
            evidence = ["The Home Assistant host restarted (its boot time changed)."]
            if pending.get("ups_on_battery"):
                since = pending.get("ups_ob_since")
                if since:
                    evidence.append(f"The UPS was on battery from {local_hm(since)} until the host went down "
                                    f"(about {duration_text(pending['start'] - since)}).")
                else:
                    evidence.append("The UPS was on battery when the host went down.")
            if pending.get("net_before"):
                evidence.append("Before it went down the outage sensors saw: "
                                + ", ".join(str(c) for c in pending["net_before"]) + ".")
            if rule == "R1":
                evidence.append(f"Operating system {pending.get('os_before')} → {self._os_now}.")
            evidence.append(self._forensics_line(shown))
            if isinstance(ready, int):
                evidence.append(f"Home Assistant was ready {ready} s after it stopped (Connection Forensics).")
            logs = self._logs(previous_boot=True) if rule in ("R5", "R4") else []
            self._new_incident("host_down", pending["start"], first, rule, evidence, logs,
                               ups_check=bool(pending.get("ups_on_battery")), verdict=verdict)
            self.st["startup_pending"] = None
            self.st["core_down_since"] = None
        else:
            before = self.st.get("last_core_version")
            rule = classify_core_return(version_before=before, version_after=version, forensics=verdict)
            evidence = ["Home Assistant Core stopped answering while the host kept running."]
            if rule == "R1":
                evidence.append(f"Home Assistant {before} → {version}.")
            evidence.append(self._forensics_line(shown))
            if isinstance(ready, int):
                evidence.append(f"Home Assistant was ready {ready} s after it stopped (Connection Forensics).")
            logs = self._logs(previous_boot=False, before=down) if rule == "R6" else []
            self._new_incident("ha_down", down, first, rule, evidence, logs, ups_check=False, verdict=verdict)
            self.st["core_down_since"] = None
        self.st["return_seen_at"] = None
        self.st["dep_at_down"] = None
        if version:
            self.st["last_core_version"] = version

    def _forensics(self) -> str | None:
        row = self._states([FORENSICS]).get(FORENSICS)
        state = (row or {}).get("state")
        return state if state in ("CLEAN", "UNCLEAN", "FIRST_RUN") else None

    @staticmethod
    def _forensics_line(verdict: str | None) -> str:
        return {"CLEAN": "Connection Forensics: Home Assistant shut down cleanly first.",
                "UNCLEAN": "Connection Forensics: no clean shutdown ran first.",
                "FIRST_RUN": "Connection Forensics was just installed: this restart was its install, so there "
                             "is no verdict for it.",
                "LATE": "Connection Forensics verdict not yet available for this restart (it had not written "
                        "one 5 minutes after Home Assistant answered), so planned and unplanned look the same."}.get(
            verdict or "", "Connection Forensics is not installed, so planned and unplanned look the same.")

    # -- 0.5.2: Deployer join (PLANNED vs UNPLANNED_CLEAN) -----------------------------------------
    def _dep_before(self, now: float) -> dict:
        """The Deployer status as last read just before the outage (only a read this recent counts)."""
        mem = self._dep_mem
        if not mem or now - float(mem.get("at") or 0) > DEPLOYER_LOOKBACK * self.s.scale:
            return {"seen": False, "deploying": None, "last": None}
        return {"seen": True, "deploying": mem.get("deploying"), "last": mem.get("last")}

    def _deployer_tick(self, now: float) -> None:
        """One read of the Deployer status per tick: remembered while no outage is open, and joined to a
        CLEAN restart for DEPLOYER_JOIN_WINDOW after Core is back."""
        open_outage = self.st.get("core_down_since") is not None or bool(self.st.get("startup_pending"))
        joins = [i for i in self.st["incidents"][:3] if i.get("join_until") and not i.get("tag_final")]
        if open_outage and not joins:
            return
        sig = deployer_signal(self._states([DEPLOYER]).get(DEPLOYER))
        if not open_outage:
            self._dep_mem = {**sig, "at": now}
        for inc in joins:
            if now > float(inc["join_until"]):
                inc["tag_final"] = True
                self.j.audit("recovery", "DEPLOYER_JOIN_CLOSED", incident=inc["id"], tag=inc.get("tag"))
                continue
            before = inc.get("dep_before") or {}
            rid = sig["done"]
            if rid is None and sig["last_rid"] and before.get("seen") and sig["last"] != before.get("last"):
                rid = sig["last_rid"]                  # a new restart outcome appeared across the outage
            if rid is None:
                continue
            if inc.get("tag") == "PLANNED" and inc.get("request_id") not in (None, rid):
                continue                               # another request's result; keep waiting for ours
            if inc.get("tag") != "PLANNED":
                inc["evidence"] = (inc["evidence"] + [I.scrub(
                    f"House Brain Deployer request {rid} restarted Home Assistant (planned).", 300)])[:8]
            else:
                inc["evidence"] = (inc["evidence"] + [I.scrub(
                    f"House Brain Deployer request {rid} finished after the restart.", 300)])[:8]
            inc.update(tag="PLANNED", request_id=rid, tag_final=True)
            self._ledger_put(inc)
            self.j.audit("recovery", "DEPLOYER_JOINED", incident=inc["id"], request=rid)

    # -- 0.5.2: mesh rejoin ------------------------------------------------------------------------
    def _mesh_begin(self, first: float) -> None:
        self.st["mesh_run"] = {"since": first, "res": {}, "done": False, "read": False}

    def _mesh_read(self) -> dict | None:
        try:
            counts = self.ha.mesh_counts()
        except Exception:  # noqa: BLE001 - Core still starting or the read failed: try again next tick
            return None
        return counts if isinstance(counts, dict) else None

    def _mesh_tick(self, now: float) -> None:
        run = self.st.get("mesh_run")
        open_outage = self.st.get("core_down_since") is not None or bool(self.st.get("startup_pending"))
        if run and not run["done"]:
            elapsed = now - float(run["since"])
            counts = self._mesh_read()
            if counts is not None:
                run["read"] = True
                base = ((self.st.get("mesh_baseline") or {}).get("counts") or {})
                for plat in sorted(counts):
                    c = counts[plat]
                    r = run["res"].setdefault(plat, {"seconds": None, "baseline": int(
                        (base.get(plat) or {}).get("unavailable") or 0)})
                    r.update(total=int(c.get("total") or 0), unavailable=int(c.get("unavailable") or 0))
                    if r["seconds"] is None and r["unavailable"] <= r["baseline"]:
                        r["seconds"] = int(round(elapsed / self.s.scale))
            all_back = run["read"] and all(r["seconds"] is not None for r in run["res"].values())
            if all_back or elapsed >= MESH_WINDOW * self.s.scale:
                run["done"] = True
                self.j.audit("recovery", "MESH_REJOIN", **{p: r["seconds"] for p, r in run["res"].items()})
            return
        if open_outage or now - self._mesh_base_try < MESH_RETRY * self.s.scale:
            return
        base = self.st.get("mesh_baseline") or {}
        if now - float(base.get("at") or 0) < MESH_BASELINE_EVERY * self.s.scale:
            return
        self._mesh_base_try = now
        counts = self._mesh_read()
        if counts is not None:
            self.st["mesh_baseline"] = {"at": now, "counts": {
                p: {"unavailable": int(c.get("unavailable") or 0), "total": int(c.get("total") or 0)}
                for p, c in counts.items()}}

    def _mesh_checks(self, inc: dict) -> dict[str, str]:
        mesh = inc.get("mesh") or {}
        out = {}
        for c in MESH_CHECKS:
            r = (mesh.get("res") or {}).get(MESH_PLATFORM[c.check_id])
            if r is None:
                continue                                 # this mesh is not used here
            out[c.check_id] = (RECOVERED if r.get("seconds") is not None
                               else ATTENTION if mesh.get("done") else PENDING)
        return out

    # -- 0.5.2: restart ledger ---------------------------------------------------------------------
    def _ledger_put(self, inc: dict) -> None:
        klass = restart_class(inc["rule"], inc.get("verdict"), inc.get("tag"))
        if klass is None:
            return
        row = {"id": inc["id"], "t": int(inc["end"]), "d": int(inc["end"] - inc["start"]), "c": klass,
               "r": inc["rule"], "v": inc.get("verdict"), "q": inc.get("request_id")}
        mesh = inc.get("mesh") or {}
        if mesh.get("done"):
            row["m"] = {p: r.get("seconds") for p, r in (mesh.get("res") or {}).items()}
        ledger = [e for e in self.st["ledger"] if e.get("id") != inc["id"]] + [row]
        self.st["ledger"] = prune_ledger(ledger, float(inc["end"]))

    def ledger_view(self) -> dict:
        """Page / status entity view (never waits for a running tick)."""
        with self._ack_lock:
            ledger, since = json.loads(json.dumps(self._ledger_view[0])), self._ledger_view[1]
        now = self.clock()
        return {**ledger_stats(ledger, now, since), "feed": ledger_feed(ledger, now, since)}

    def status_attrs(self) -> dict:
        """Attributes added to the App's status entity (counts only; the feed text stays on the page)."""
        view = self.ledger_view()
        return {k: view[k] for k in ("restarts_30d", "restarts_90d", "unplanned_failures_90d",
                                     "mtbf_unplanned_days", "last_restart_class")}

    def _logs(self, *, previous_boot: bool, before: float | None = None) -> list[str]:
        try:
            text = self.ha.core_log_tail(previous_boot=previous_boot)
        except (HAError, net.NetError, ForbiddenCall):
            return ["(The log from before could not be read.)"]
        return error_lines(text, before=before)

    def _network(self, now: float) -> None:
        rows = self._states([NET_CLASS, NET_WAN, UPS_STATUS])
        klass = (rows.get(NET_CLASS) or {}).get("state")
        wan = (rows.get(NET_WAN) or {}).get("state")
        flags = str((rows.get(UPS_STATUS) or {}).get("state") or "").split()
        on_battery = "OB" in flags
        if on_battery and self.st.get("ups_ob_since") is None:
            self.st["ups_ob_since"] = now
        elif not on_battery and rows.get(UPS_STATUS) is not None:
            self.st["ups_ob_since"] = None
        bad = klass in OUTAGE_CLASSES or wan == "DOWN" or on_battery
        if bad:
            if self.st["net_bad_since"] is None:
                self.st["net_bad_since"] = now
                self.st["net_classes"] = []
                self.st["net_ups_ob"] = False
            if klass in OUTAGE_CLASSES and klass not in self.st["net_classes"]:
                self.st["net_classes"].append(klass)
            if wan == "DOWN" and "WAN_DOWN" not in self.st["net_classes"]:
                self.st["net_classes"].append("WAN_DOWN")
            self.st["net_ups_ob"] = self.st["net_ups_ob"] or on_battery
            return
        since = self.st.get("net_bad_since")
        if since is None:
            return
        self.st["net_bad_since"] = None
        if now - since < NET_MIN_SECONDS:
            return
        seen = self.st["net_classes"] or (["UPS_ON_BATTERY"] if self.st["net_ups_ob"] else [])
        nice = {"ISP_OUTAGE": "internet (ISP) outage", "UTILITY_POWER_OUTAGE": "power cut (on UPS)",
                "LOCAL_LAN_FAULT": "home network (router) fault", "PARTIAL": "partial outage",
                "WAN_DOWN": "internet down", "UPS_ON_BATTERY": "UPS on battery (power cut)"}
        evidence = ["Home Assistant kept running; the outage sensors saw: "
                    + ", ".join(nice.get(c, c) for c in seen) + "."]
        summary = (self._states([NET_SUMMARY]).get(NET_SUMMARY) or {}).get("state")
        if summary and summary not in ("unknown", "unavailable"):
            evidence.append(f"Last outage summary: {I.scrub(summary, 200)}")
        self._new_incident("network", since, now, "R7", evidence, [], ups_check=self.st["net_ups_ob"])

    # -- incidents ---------------------------------------------------------------------
    def _new_incident(self, kind: str, start: float, end: float, rule: str, evidence: list[str],
                      logs: list[str], *, ups_check: bool, verdict: str | None = None) -> None:
        inc = {"id": time.strftime("%Y%m%d-%H%M%S", time.gmtime(end)), "kind": kind,
               "start": float(start), "end": float(end), "rule": rule,
               "cause": RULE_TEXT[rule], "confidence": CONFIDENCE[rule],
               "evidence": [I.scrub(e, 300) for e in evidence if e][:8],
               "steps": list(RULE_STEPS[rule]), "log_lines": logs[:MAX_LOG_LINES],
               "checks": {}, "ups_check": ups_check, "notify": notify_worthy(rule, end - start, verdict),
               "pushed": False, "push_failed_at": None, "first_push_try": None, "p0_pushed": False,
               "acked": False, "local_note_done": False, "verdict": verdict, "tag": None, "request_id": None}
        if kind != "network":
            # 0.5.2: PLANNED vs UNPLANNED_CLEAN for a clean Core restart; UNCLEAN stays UNCLEAN.
            if rule == "R2":
                before = self.st.get("dep_at_down") or {"seen": False}
                inc.update(dep_before=before, tag_final=False,
                           join_until=float(end) + DEPLOYER_JOIN_WINDOW * self.s.scale)
                if before.get("deploying"):
                    inc.update(tag="PLANNED", request_id=before["deploying"])
                    inc["evidence"] = (inc["evidence"] + [I.scrub(
                        f"House Brain Deployer was installing request {before['deploying']} when Home Assistant "
                        "went down (planned restart).", 300)])[:8]
                else:
                    inc["tag"] = "UNPLANNED_CLEAN"
            elif rule == "R6":
                inc["tag"] = "UNCLEAN"
            run = self.st.get("mesh_run") or {}
            if run.get("since") == float(end):
                inc["mesh"] = json.loads(json.dumps(run))
            self._ledger_put(inc)
        self.st["incidents"] = ([inc] + self.st["incidents"])[:MAX_INCIDENTS]
        self.j.audit("recovery", "INCIDENT", kind=kind, rule=rule, duration=int(end - start),
                     notify=inc["notify"], verdict=verdict, tag=inc["tag"])

    def _checks_for(self, inc: dict) -> tuple[Check, ...]:
        return (*CHECKS, UPS_CHECK) if inc.get("ups_check") else CHECKS

    def _advance(self, now: float) -> None:
        run = self.st.get("mesh_run") or {}
        for inc in self.st["incidents"][:3]:
            if inc.get("mesh") and not inc["mesh"].get("done") and run.get("since") == inc["mesh"].get("since"):
                inc["mesh"] = json.loads(json.dumps(run))            # 0.5.2: latest mesh rejoin reading
                if run.get("done"):
                    self._ledger_put(inc)
            since = now - inc["end"]
            if since > CHECK_WINDOW * self.s.scale and inc["checks"]:
                continue
            checks = self._checks_for(inc)
            states = self._states({e for c in checks for e in c.entities})
            inc["checks"] = {c.check_id: eval_check(c, states, since / self.s.scale) for c in checks}
            inc["checks"].update(self._mesh_checks(inc))

    # -- delivery ----------------------------------------------------------------------
    def _wan_down(self) -> bool:
        return (self._states([NET_WAN]).get(NET_WAN) or {}).get("state") == "DOWN"

    def _send(self, service: str, title: str, message: str, *, urgent: bool) -> bool:
        data: dict[str, Any] = {"tag": "hbm-recovery"}
        if self.s.open_url:
            data["url"] = self.s.open_url
        if urgent:
            data["push"] = {"interruption-level": "time-sensitive"}
        try:
            self.ha.notify(service, {"title": title[:120], "message": message[:900], "data": data})
            return True
        except (HAError, net.NetError, ForbiddenCall) as err:
            self.j.audit("recovery", "PUSH_FAILED", error=str(err)[:120])
            return False

    def _deliver(self, now: float) -> None:
        for inc in self.st["incidents"][:3]:
            if not inc["local_note_done"]:
                title, body = push_text(inc)
                try:
                    self.ha.persistent_note(NOTIFICATION_ID, title, body)
                    inc["local_note_done"] = True
                except (HAError, net.NetError, ForbiddenCall):
                    pass
            p0_attention = inc["checks"].get("SMOKE_CO_SENSORS") == ATTENTION
            if p0_attention and not inc["p0_pushed"] and not self._wan_down():
                ok = True
                for svc in self.s.safety_notify_services or (self.s.notify_service,):
                    ok = self._send(svc, "Smoke/CO sensors not back",
                                    "After the outage, the smoke/CO sensors are still not reporting. "
                                    "Check the Ring alarm base station now.", urgent=True) and ok
                inc["p0_pushed"] = ok
                self.j.audit("recovery", "P0_ESCALATED", delivered=ok)
            if not inc["notify"] or inc["pushed"]:
                continue
            first = inc["first_push_try"] or now
            inc["first_push_try"] = first
            if now - first > RETRY_FOR * self.s.scale:
                continue
            last = inc["push_failed_at"]
            if last is not None and now - last < RETRY_EVERY * self.s.scale:
                continue
            if self._wan_down():
                continue
            title, body = push_text(inc)
            if self._send(self.s.notify_service, title, body, urgent=False):
                inc["pushed"] = True
                self.j.audit("recovery", "PUSHED", incident=inc["id"])
            else:
                inc["push_failed_at"] = now

    # -- owner "Got it" ------------------------------------------------------------------
    def request_ack(self, nonce: str, user_id: str) -> int:
        """Called from the page thread. 200 accepted; 403 not the owner; 409 stale code."""
        owner = self.s.owner_user_id()
        if not owner or user_id != owner:
            return 403
        with self._ack_lock:
            if not nonce or not hmac.compare_digest(nonce, self.ack_nonce):
                return 409
            self.ack_nonce = secrets.token_hex(12)   # single use
            self._ack_requested = True               # applied (and saved) by the next tick
            for inc in self._view:
                inc["acked"] = True
        return 200

    def _apply_ack(self) -> None:
        try:
            self.ha.persistent_dismiss(NOTIFICATION_ID)
            self._ack_requested = False
        except (HAError, net.NetError, ForbiddenCall):
            pass

    def snapshot(self) -> tuple[list[dict], str]:
        """What the page shows: the state as of the last tick (never waits for a running tick)."""
        with self._ack_lock:
            return json.loads(json.dumps(self._view)), self.ack_nonce

    # -- off-site ping -----------------------------------------------------------------
    def _ping(self, now: float) -> None:
        if not self.s.liveness_url or not self.s.liveness_key:
            return
        if self._last_ping and now - self._last_ping < self.s.liveness_interval:
            return
        self._last_ping = now
        self._ping_seq += 1
        body = json.dumps({"v": 1, "seq": self._ping_seq, "ts": int(now)}, separators=(",", ":")).encode()
        try:
            net.request("POST", self.s.liveness_url, {"Content-Type": "application/json",
                                                       "X-Signature": sign(self.s.liveness_key, body)},
                        raw_body=body, timeout=15)
        except net.NetError as err:
            if now - self._ping_failed_logged > 3600:
                self._ping_failed_logged = now
                LOG.info("liveness ping failed (logged at most hourly): %s", str(err)[:120])
            # 0.5.1: the problem watch says so when the ping keeps failing (written on change and hourly).
            doc = self.j.load_doc(LIVENESS_DOC, {})
            if doc.get("fail_since") is None or doc.get("status") != err.status \
                    or now - float(doc.get("written") or 0) >= 3600:
                self.j.save_doc(LIVENESS_DOC, {"fail_since": doc.get("fail_since") or now, "status": err.status,
                                               "detail": net.redact(str(err))[:120], "written": now})
            return
        if self.j.load_doc(LIVENESS_DOC, {}).get("fail_since") is not None:
            self.j.save_doc(LIVENESS_DOC, {"fail_since": None, "ok_at": now, "written": now})


def run_forever(rec: Recovery, should_stop: Callable[[], bool], period: float) -> None:
    """Background loop: one tick per period; a failing tick never stops the loop."""
    while not should_stop():
        try:
            rec.tick()
        except Exception as err:  # noqa: BLE001 - visibility only; next tick tries again
            LOG.warning("recovery tick failed: %s", net.redact(f"{type(err).__name__}: {err}")[:200])
        deadline = time.monotonic() + period
        while not should_stop() and time.monotonic() < deadline:
            time.sleep(min(1.0, period))
    rec.stop()
