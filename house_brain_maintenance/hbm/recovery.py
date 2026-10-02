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
from .ha import RECOVERY_NOTIFICATION_ID, RECOVERY_READ_ENTITIES, ForbiddenCall, HAError, HomeAssistant

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

# -- pinned inputs (exact entity ids; nothing else is read) --------------------------------------
FORENSICS = "sensor.house_brain_connection_forensics_last_restart"
NET_CLASS = "sensor.house_brain_network_outage_class"
NET_SUMMARY = "sensor.house_brain_network_outage_summary"
NET_WAN = "sensor.house_brain_network_wan_status"
UPS_STATUS = "sensor.ups_status_data"
UPS_CHARGE = "sensor.ups_battery_charge"
OUTAGE_CLASSES = frozenset({"ISP_OUTAGE", "UTILITY_POWER_OUTAGE", "LOCAL_LAN_FAULT", "PARTIAL"})

P0, P1, P2 = "P0", "P1", "P2"
RECOVERED, PENDING, ATTENTION, NOT_OBSERVED = "RECOVERED", "PENDING", "ATTENTION", "NOT_OBSERVED"


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
READ_ENTITIES = frozenset({FORENSICS, NET_CLASS, NET_SUMMARY, NET_WAN, UPS_STATUS, UPS_CHARGE}
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


def notify_worthy(rule: str, duration: float) -> bool:
    """Planned restarts shorter than QUIET_PLANNED_SECONDS stay on the page only."""
    return not (rule in ("R1", "R2", "R3") and duration < QUIET_PLANNED_SECONDS)


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


CHECK_LABELS = {c.check_id: c.label for c in (*CHECKS, UPS_CHECK)}


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
            alive = self.ha.core_alive()
            if not alive:
                self._down_count += 1
                if self.st["core_down_since"] is None:
                    self.st["core_down_since"] = now
                    self.j.audit("recovery", "CORE_DOWN_SEEN")
            else:
                if self._down_count == 1 and self.st["core_down_since"] is not None \
                        and not self.st.get("startup_pending"):
                    # One missed check is a slow answer, not an outage.
                    self.st["core_down_since"] = None
                    self.j.audit("recovery", "CORE_BLIP_IGNORED")
                self._down_count = 0
                self._core_up(now)
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
        if pending:
            forensics = self._forensics()
            rule = classify_host_return(ups_on_battery=pending.get("ups_on_battery"), forensics=forensics,
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
            evidence.append(self._forensics_line(forensics))
            logs = self._logs(previous_boot=True) if rule in ("R5", "R4") else []
            self._new_incident("host_down", pending["start"], now, rule, evidence, logs,
                               ups_check=bool(pending.get("ups_on_battery")))
            self.st["startup_pending"] = None
            self.st["core_down_since"] = None
        elif down is not None:
            before = self.st.get("last_core_version")
            forensics = self._forensics()
            rule = classify_core_return(version_before=before, version_after=version, forensics=forensics)
            evidence = ["Home Assistant Core stopped answering while the host kept running."]
            if rule == "R1":
                evidence.append(f"Home Assistant {before} → {version}.")
            evidence.append(self._forensics_line(forensics))
            logs = self._logs(previous_boot=False, before=down) if rule == "R6" else []
            self._new_incident("ha_down", down, now, rule, evidence, logs, ups_check=False)
            self.st["core_down_since"] = None
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
                "FIRST_RUN": "Connection Forensics was just installed; no verdict yet."}.get(
            verdict or "", "Connection Forensics is not installed, so planned and unplanned look the same.")

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
                      logs: list[str], *, ups_check: bool) -> None:
        inc = {"id": time.strftime("%Y%m%d-%H%M%S", time.gmtime(end)), "kind": kind,
               "start": float(start), "end": float(end), "rule": rule,
               "cause": RULE_TEXT[rule], "confidence": CONFIDENCE[rule],
               "evidence": [I.scrub(e, 300) for e in evidence if e][:8],
               "steps": list(RULE_STEPS[rule]), "log_lines": logs[:MAX_LOG_LINES],
               "checks": {}, "ups_check": ups_check, "notify": notify_worthy(rule, end - start),
               "pushed": False, "push_failed_at": None, "first_push_try": None, "p0_pushed": False,
               "acked": False, "local_note_done": False}
        self.st["incidents"] = ([inc] + self.st["incidents"])[:MAX_INCIDENTS]
        self.j.audit("recovery", "INCIDENT", kind=kind, rule=rule, duration=int(end - start),
                     notify=inc["notify"])

    def _checks_for(self, inc: dict) -> tuple[Check, ...]:
        return (*CHECKS, UPS_CHECK) if inc.get("ups_check") else CHECKS

    def _advance(self, now: float) -> None:
        for inc in self.st["incidents"][:3]:
            since = now - inc["end"]
            if since > CHECK_WINDOW * self.s.scale and inc["checks"]:
                continue
            checks = self._checks_for(inc)
            states = self._states({e for c in checks for e in c.entities})
            inc["checks"] = {c.check_id: eval_check(c, states, since / self.s.scale) for c in checks}

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
