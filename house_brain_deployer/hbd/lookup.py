"""Owner-approved, read-only lookups (``house_brain_lookup_request.v1``).

An AI files a Home Assistant template in ``deploy/requests/<id>/manifest.json``.
The owner sees the exact template and approves (same channels as deploys); the
Deployer renders it through Core's read-only ``POST /api/template`` and posts
the scrubbed, size-bounded output to the tracking issue. Nothing is written:
Home Assistant templates cannot call services or change state. The Deployer
adds no service call, file write or restart for lookups.

0.3.29 (#912, owner pop-up 2026-10-18 "This chat builds it"): an optional
``zwave_config_entity`` also reads that entity's Z-Wave device configuration
parameters through ``zwave_js/get_config_parameters``, a read-only WebSocket
command that answers from the values Z-Wave JS already holds (no radio
traffic, no write). The device id comes from the read-only template
``device_id()``. Output is projected to parameter number, label, value, unit and
default; nothing else leaves the house. The owner's approval message names the
entity.

0.3.34 (#215, owner pop-ups 2026-10-24 "This chat builds 0.3.34", "Approve §4 as
written"; design ``docs/architecture/SENSE_DEVICE_TAGS_LOOKUP_DEPLOYER_PROPOSAL_R1.md``):
two more optional read-only parts, each named in the approval message and run only
after the owner's Approve:

* ``sense_device_tags: true`` reads Home Assistant's own Sense login from
  ``.storage/core.config_entries`` (the only ``.storage`` file AppArmor lets the
  Deployer read), keeps only that one entry's token, device id and monitor id in
  memory, and makes one GET to Sense's ``devices/overview``. It never
  authenticates, refreshes or logs out, so Home Assistant's Sense login is not
  touched. Output: device names, icons and allowlisted tags only.
* ``history_entities`` (up to 16 ids) with ``history_days`` (1-7) reads one
  ``/api/history/period`` window (``minimal_response``, ``no_attributes``) and
  posts compact state changes.

0.3.35 (#215, owner pop-ups 2026-10-24 "yes, fix the bug ... dive deep" and "Approve
renew-like-HA"):

* Every part has an output budget, so a lookup never loses its end. The template part
  is capped when other parts follow; the Z-Wave, history and Sense parts share what is
  left; history counts its header, SKIPPED lines and ``END``; Sense keeps the tag-key
  line first and says how many devices did not fit. ``scrub()`` cuts on a line.
* Home Assistant renews its Sense token in memory and never writes it back, so the
  stored token goes stale. Like Home Assistant at every start, the Deployer then
  renews once (only after a 401) with the stored refresh token, in memory, and reads.
  It never authenticates with a password and never logs out.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field

from . import approval, net
from .manifest import RE_REQUEST_ID, REQUESTERS, ManifestError, sanitize_text

LOOKUP_SCHEMA = "house_brain_lookup_request.v1"
MAX_TEMPLATE_CHARS = 4000
MAX_OUTPUT_CHARS = 20000
MAX_ZWAVE_PARAMETERS = 200
RE_ENTITY_ID = re.compile(r"^[a-z_]{2,32}\.[a-z0-9_]{1,96}$")
RE_DEVICE_ID = re.compile(r"^[0-9a-f]{32}$")
MAX_HISTORY_ENTITIES = 16
MAX_HISTORY_DAYS = 7
MAX_HISTORY_STATES = 32          # distinct states per entity in the legend; more are reported, not guessed
# 0.3.34: the Sense URLs the Deployer can build: the device overview and (0.3.35) the renew that Home Assistant's own
# startup makes after a 401. Never authenticate (password) or logout.
SENSE_API_BASE = "https://api.sense.com/apiservice/api/v1/"
SENSE_TIMEOUT_S = 10.0
SENSE_TAGS = ("DefaultUserDeviceType", "UserDeviceType", "UserDeviceTypeDisplayString", "DeviceListAllowed",
              "Revoked", "Pending", "Virtual", "TimelineAllowed", "OriginalName", "NameUseredit")
SENSE_DATE_TAGS = ("DateCreated", "DateFirstUsage")
SENSE_HINT_TAG = "PeerNames"
MAX_SENSE_DEVICES = 300
RE_SENSE_MONITOR = re.compile(r"^[0-9]{1,12}$")
RE_SENSE_DEVICE_HEADER = re.compile(r"^[A-Za-z0-9-]{1,64}$")
RE_SENSE_TOKEN = re.compile(r"^[\x21-\x7e]{16,4096}$")
RE_SENSE_USER = re.compile(r"^[A-Za-z0-9-]{1,64}$")
# 0.3.35: output budget. With other parts, the template part keeps at most this many characters; the parts share the rest.
TEMPLATE_CHARS_WITH_PARTS = 4000
MAX_TAG_KEYS_CHARS = 1000
CONFIG_ENTRIES = os.path.join(".storage", "core.config_entries")
LOOKUP_OK = "LOOKUP_OK"
LOOKUP_FAILED = "LOOKUP_FAILED"

# Secret-shaped strings are scrubbed from lookup output before it leaves the house.
_SECRET_PATTERNS = (
    re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),  # JWT / HA tokens
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)\b(?:password|passwd|api[_-]?key|secret|token)\s*[:=]\s*\S{6,}"),
)


@dataclass(frozen=True)
class LookupRequest:
    request_id: str
    title: str
    requested_by: str
    tracking_issue: int
    template: str
    note: str
    digest: str
    zwave_config_entity: str = ""
    sense_device_tags: bool = False
    history_entities: tuple[str, ...] = field(default_factory=tuple)
    history_days: int = 0


def is_lookup(raw_bytes: bytes) -> bool:
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return isinstance(data, dict) and data.get("schema") == LOOKUP_SCHEMA


def parse_lookup(raw_bytes: bytes) -> LookupRequest:
    if len(raw_bytes) > 16 * 1024:
        raise ManifestError("TOO_LARGE")
    try:
        text = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as err:
        raise ManifestError("ENCODING") from err

    def _no_dupes(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ManifestError("DUPLICATE_KEY", str(key)[:40])
            out[key] = value
        return out

    def _no_constants(name):
        raise ManifestError("JSON_CONSTANT", name)

    try:
        data = json.loads(text, object_pairs_hook=_no_dupes, parse_constant=_no_constants)
    except ManifestError:
        raise
    except (ValueError, RecursionError) as err:
        raise ManifestError("JSON") from err
    if not isinstance(data, dict):
        raise ManifestError("TYPE")
    required = {"schema", "request_id", "title", "requested_by", "tracking_issue", "template"}
    unknown = set(data) - required - {"note", "zwave_config_entity", "sense_device_tags", "history_entities",
                                      "history_days"}
    if required - set(data):
        raise ManifestError("MISSING_KEY", sorted(required - set(data))[0])
    if unknown:
        raise ManifestError("UNKNOWN_KEY", sorted(unknown)[0][:40])
    if data["schema"] != LOOKUP_SCHEMA:
        raise ManifestError("SCHEMA_VERSION")
    rid = data["request_id"]
    if not isinstance(rid, str) or not RE_REQUEST_ID.fullmatch(rid):
        raise ManifestError("PATTERN", "request_id")
    if data["requested_by"] not in REQUESTERS:
        raise ManifestError("REQUESTER")
    issue = data["tracking_issue"]
    if isinstance(issue, bool) or not isinstance(issue, int) or not 1 <= issue <= 10_000_000:
        raise ManifestError("RANGE", "tracking_issue")
    title = data["title"]
    if not isinstance(title, str) or len(title) > 200 or not sanitize_text(title, 80):
        raise ManifestError("PATTERN", "title")
    template = data["template"]
    if not isinstance(template, str) or not template.strip():
        raise ManifestError("EMPTY", "template")
    if len(template) > MAX_TEMPLATE_CHARS:
        raise ManifestError("TOO_LONG", "template")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in template) or "\x7f" in template:
        raise ManifestError("CONTROL_CHARS", "template")
    note = data.get("note", "")
    if not isinstance(note, str) or len(note) > 2000:
        raise ManifestError("TYPE", "note")
    zwave = data.get("zwave_config_entity", "")
    if not isinstance(zwave, str) or (zwave and not RE_ENTITY_ID.fullmatch(zwave)):
        raise ManifestError("PATTERN", "zwave_config_entity")
    sense = data.get("sense_device_tags", False)
    if not isinstance(sense, bool):
        raise ManifestError("TYPE", "sense_device_tags")
    hist = data.get("history_entities", [])
    if (not isinstance(hist, list) or len(hist) > MAX_HISTORY_ENTITIES or len(set(map(str, hist))) != len(hist)
            or not all(isinstance(e, str) and RE_ENTITY_ID.fullmatch(e) for e in hist)):
        raise ManifestError("PATTERN", "history_entities")
    days = data.get("history_days", 1 if hist else 0)
    if isinstance(days, bool) or not isinstance(days, int) or (
            hist and not 1 <= days <= MAX_HISTORY_DAYS) or (not hist and days != 0):
        raise ManifestError("RANGE", "history_days")
    return LookupRequest(rid, sanitize_text(title, 80), data["requested_by"], issue, template,
                         sanitize_text(note, 300), hashlib.sha256(raw_bytes).hexdigest(), zwave,
                         sense, tuple(hist), days)


def scrub(output: str) -> str:
    out = net.redact(output)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[redacted]", out)
    if len(out) > MAX_OUTPUT_CHARS:
        cut = out.rfind("\n", 0, MAX_OUTPUT_CHARS + 1)   # 0.3.35: never mid-line (a half line reads as data)
        out = out[:cut if cut > MAX_OUTPUT_CHARS - 2000 else MAX_OUTPUT_CHARS] + "\n[truncated]"
    return out


def fit_lines(head: str, rows: list[str], budget: int, more: str) -> list[str]:
    """0.3.35: ``head`` plus as many whole rows as fit; then ``more`` (``{n}`` = rows left out) is said, never cut.

    The joined result (``"\n"``) is at most ``budget`` characters whenever ``head`` and the ``more`` line fit.
    """
    out, used = [head], len(head)
    for i, row in enumerate(rows):
        reserve = len(more.format(n=len(rows) - i - 1)) + 1 if i < len(rows) - 1 else 0
        if used + 1 + len(row) + reserve > budget:
            out.append(more.format(n=len(rows) - i))
            return out
        out.append(row)
        used += 1 + len(row)
    return out


def _plain(value, limit: int) -> str:
    """One line of plain text: no control characters, no backticks, bounded."""
    text = "" if value is None else str(value)
    text = "".join(ch if 32 <= ord(ch) < 127 or ord(ch) > 159 else " " for ch in text).replace("`", "'")
    return text.strip()[:limit]


def project_zwave_config(result: dict) -> list[str]:
    """``zwave_js/get_config_parameters`` result -> sorted lines ``P<n>[/<key>] <label> = <value> ...``.

    Only the parameter number, its sub-key, label, current value, unit and default are kept. Descriptions,
    value ids, metadata states and everything else are dropped. Unreadable entries are skipped, not guessed.
    """
    rows = []
    for item in result.values():
        if not isinstance(item, dict):
            continue
        prop = item.get("property")
        if isinstance(prop, bool) or not isinstance(prop, int) or not 0 <= prop <= 65535:
            continue
        key = item.get("property_key")
        key = key if isinstance(key, int) and not isinstance(key, bool) else None
        raw_meta = item.get("metadata")
        meta: dict = raw_meta if isinstance(raw_meta, dict) else {}
        value = item.get("value")
        if isinstance(value, bool) or not (value is None or isinstance(value, (int, float, str))):
            value = None
        rows.append((prop, -1 if key is None else key, key, _plain(meta.get("label"), 60), value,
                     _plain(meta.get("unit"), 12), meta.get("default")))
    rows.sort(key=lambda r: (r[0], r[1]))
    lines = []
    for prop, _, key, label, value, unit, default in rows[:MAX_ZWAVE_PARAMETERS]:
        name = f"P{prop}/{key}" if key is not None else f"P{prop}"
        shown = "not read" if value is None else _plain(value, 40)
        extra = (f" {unit}" if unit else "") + (
            f" (default {_plain(default, 20)})" if isinstance(default, (int, float, str))
            and not isinstance(default, bool) else "")
        lines.append(f"{name} {label} = {shown}{extra}")
    if len(rows) > MAX_ZWAVE_PARAMETERS:
        lines.append(f"[{len(rows) - MAX_ZWAVE_PARAMETERS} more parameters not shown]")
    return lines


def read_zwave_config(ha, entity_id: str, budget: int = MAX_OUTPUT_CHARS) -> str:
    """Read-only: device id via the ``device_id()`` template, then the parameter list. Never writes."""
    if not RE_ENTITY_ID.fullmatch(entity_id):
        raise ManifestError("PATTERN", "zwave_config_entity")
    device_id = ha.render_template("{{ device_id('" + entity_id + "') or '' }}").strip()
    if not RE_DEVICE_ID.fullmatch(device_id):
        return f"Z-Wave configuration of {entity_id}: no device found for this entity."
    lines = project_zwave_config(ha.zwave_config_parameters(device_id))
    head = f"Z-Wave configuration of the device of {entity_id} ({len(lines)} parameters, read-only):"
    if not lines:
        return head + "\n(none reported)"
    return "\n".join(fit_lines(head, lines, budget, "[{n} more lines not shown (output bound)]"))


# ---------------------------------------------------------------------------------------------
# 0.3.34: Sense device tags (read-only; Home Assistant's own login). 0.3.35: renewed once after a 401, as HA does.


class SenseLookupError(RuntimeError):
    """A reason code; the message never contains a token."""


def read_sense_login(config_root: str) -> tuple[str, str, str, str, str]:
    """``(access_token, device_id, monitor_id, user_id, refresh_token)`` of the single loaded Sense entry.

    The file holds every integration's credentials; this function is the only reader and keeps five values of one
    entry (0.3.35 adds the two that Home Assistant's own startup renew sends). Everything else goes out of scope
    before the function returns.
    """
    path = os.path.join(config_root, CONFIG_ENTRIES)
    try:
        with open(path, "rb") as fh:
            raw = fh.read(4 * 1024 * 1024 + 1)
    except OSError:
        raise SenseLookupError("SENSE_CONFIG_UNREADABLE") from None
    if len(raw) > 4 * 1024 * 1024:
        raise SenseLookupError("SENSE_CONFIG_UNREADABLE")
    try:
        entries = json.loads(raw.decode("utf-8"))["data"]["entries"]
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        raise SenseLookupError("SENSE_CONFIG_UNREADABLE") from None
    finally:
        raw = b""
    if not isinstance(entries, list):
        raise SenseLookupError("SENSE_CONFIG_UNREADABLE")
    sense = [e for e in entries if isinstance(e, dict) and e.get("domain") == "sense" and e.get("disabled_by") is None]
    if len(sense) != 1 or not isinstance(sense[0].get("data"), dict):
        raise SenseLookupError("SENSE_ENTRY_NOT_UNIQUE")
    data = sense[0]["data"]
    token, device, monitor = data.get("access_token"), data.get("device_id"), str(data.get("monitor_id", ""))
    user, refresh = data.get("user_id"), data.get("refresh_token")
    user = str(user) if isinstance(user, int) and not isinstance(user, bool) else user
    del entries, sense, data
    if not (isinstance(token, str) and RE_SENSE_TOKEN.fullmatch(token) and isinstance(device, str)
            and RE_SENSE_DEVICE_HEADER.fullmatch(device) and RE_SENSE_MONITOR.fullmatch(monitor)
            and isinstance(user, str) and RE_SENSE_USER.fullmatch(user)
            and isinstance(refresh, str) and RE_SENSE_TOKEN.fullmatch(refresh)):
        raise SenseLookupError("SENSE_ENTRY_SHAPE")
    net.register_secret(token)
    net.register_secret(refresh)
    return token, device, monitor, user, refresh


def sense_overview_url(monitor_id: str) -> str:
    """The Sense read URL. With ``sense_renew_url`` the only Sense paths; authenticate and logout cannot be built."""
    if not RE_SENSE_MONITOR.fullmatch(monitor_id):
        raise SenseLookupError("SENSE_ENTRY_SHAPE")
    return f"{SENSE_API_BASE}app/monitors/{monitor_id}/devices/overview"


def sense_renew_url() -> str:
    """0.3.35: the renew Home Assistant's Sense library makes after a 401 (form ``user_id`` + ``refresh_token``)."""
    return SENSE_API_BASE + "renew"


def _date(value) -> str:
    text = _plain(value, 40)
    return text[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", text) else ""


def project_sense_devices(reply) -> list[str]:
    """Sense ``devices/overview`` reply -> one line per device. Ids, account fields and unknown tag values drop."""
    devices = reply.get("devices") if isinstance(reply, dict) else None
    if not isinstance(devices, list):
        raise SenseLookupError("SENSE_REPLY_SHAPE")
    names = {d.get("id"): _plain(d.get("name"), 60) for d in devices if isinstance(d, dict)}
    rows, unknown = [], set()
    for d in devices[:MAX_SENSE_DEVICES]:
        if not isinstance(d, dict):
            continue
        raw_tags = d.get("tags")
        tags: dict = raw_tags if isinstance(raw_tags, dict) else {}
        parts = [_plain(d.get("name"), 60) or "(unnamed)", "icon=" + (_plain(d.get("icon"), 30) or "-")]
        for key in SENSE_TAGS:
            if key in tags and not isinstance(tags[key], (dict, list)):
                parts.append(f"{key}={_plain(tags[key], 40)}")
        for key in SENSE_DATE_TAGS:
            if _date(tags.get(key)):
                parts.append(f"{key}={_date(tags.get(key))}")
        merge = tags.get("MergeId")
        if merge:
            target = names.get(merge)
            parts.append(f"merged into {target}" if target else "merged (target not listed)")
        hints = tags.get(SENSE_HINT_TAG)
        if isinstance(hints, list):
            scored = []
            for h in hints:
                if isinstance(h, dict) and _plain(h.get("Name"), 40):
                    pct = h.get("Percent")
                    pct = float(pct) if isinstance(pct, (int, float)) and not isinstance(pct, bool) else -1.0
                    scored.append((pct, _plain(h.get("Name"), 40)))
            scored.sort(key=lambda x: -x[0])
            if scored:
                parts.append("hints: " + ", ".join(n + (f" {p:.0f}%" if p >= 0 else "") for p, n in scored[:3]))
        unknown |= {_plain(k, 40) for k in tags if k not in SENSE_TAGS + SENSE_DATE_TAGS + ("MergeId", SENSE_HINT_TAG)}
        rows.append(" | ".join(parts))
    rows.sort(key=str.lower)
    if len(devices) > MAX_SENSE_DEVICES:
        rows.append(f"[{len(devices) - MAX_SENSE_DEVICES} more devices not shown]")
    if unknown:   # 0.3.35: first, so an output bound can never drop it
        keys = "Other tag keys (values not shown): " + ", ".join(sorted(k for k in unknown if k))
        rows.insert(0, keys if len(keys) <= MAX_TAG_KEYS_CHARS else keys[:MAX_TAG_KEYS_CHARS - 4] + " ...")
    return rows


def read_sense_device_tags(config_root: str, request=None, budget: int = MAX_OUTPUT_CHARS) -> str:
    """Read-only: one GET with Home Assistant's own Sense login. Never authenticates (password) or logs out.

    0.3.35: Home Assistant renews its token in memory and never writes it back, so the stored one goes stale. Only on
    a 401, and exactly once, this renews the way Home Assistant's own startup does (stored ``user_id`` + refresh
    token, in memory only) and reads once more. A 403, a refused renew or a second 401 ends the read; nothing retries.
    """
    request = request or net.request
    token, device, monitor, user, refresh = read_sense_login(config_root)
    note = ""
    try:
        try:
            _, reply = request("GET", sense_overview_url(monitor),
                               {"x-sense-device-id": device, "Authorization": f"bearer {token}"},
                               timeout=SENSE_TIMEOUT_S)
        except net.NetError as err:
            if err.status != 401:
                raise
            try:
                _, renewed = request("POST", sense_renew_url(),
                                     {"x-sense-device-id": device, "Authorization": f"bearer {token}"},
                                     form={"user_id": user, "refresh_token": refresh}, timeout=SENSE_TIMEOUT_S)
            except net.NetError:
                raise SenseLookupError("SENSE_RENEW_REFUSED") from None
            new = renewed.get("access_token") if isinstance(renewed, dict) else None
            if not (isinstance(new, str) and RE_SENSE_TOKEN.fullmatch(new)):
                raise SenseLookupError("SENSE_RENEW_REFUSED") from None
            net.register_secret(new)
            rotated = renewed.get("refresh_token")
            if isinstance(rotated, str):
                net.register_secret(rotated)
            note = ("Sense session: the stored token had expired, so it was renewed once the way Home Assistant does at "
                    "startup (refresh token unchanged: " + ("yes" if rotated == refresh else "NO") + ").")
            token = new
            renewed = new = rotated = None
            _, reply = request("GET", sense_overview_url(monitor),
                               {"x-sense-device-id": device, "Authorization": f"bearer {token}"},
                               timeout=SENSE_TIMEOUT_S)
    except net.NetError as err:
        raise SenseLookupError("SENSE_TOKEN_REFUSED" if err.status in (401, 403) else "SENSE_READ_FAILED") from None
    finally:
        token = refresh = user = ""
    lines = project_sense_devices(reply)
    head = (f"Sense devices ({len(lines)} lines, read-only with Home Assistant's own Sense login; tags allowlisted; "
            "a Sense name or type is a hint, never an identity):") + ("\n" + note if note else "")
    return "\n".join(fit_lines(head, lines, budget,
                                "[{n} more lines not shown (output bound); file the Sense read on its own]"))


# ---------------------------------------------------------------------------------------------
# 0.3.34: history (read-only /api/history/period window)


def _b36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    n = max(0, int(n))
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if not n:
            return out


def _epoch(value) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        from datetime import datetime
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(t.timestamp()) if t.tzinfo else None


def project_history(reply, entities: tuple[str, ...], start: int, budget: int) -> list[str]:
    """``HST1`` lines: per entity a state legend and ``<seconds since previous, base 36><state letter>`` changes.

    An entity whose line does not fit the remaining budget is listed as SKIPPED (re-file with fewer entities); no
    change is ever dropped silently from an emitted line.
    """
    by_id: dict[str, list] = {}
    if isinstance(reply, list):
        for series in reply:
            if isinstance(series, list) and series and isinstance(series[0], dict):
                eid = series[0].get("entity_id")
                if eid in entities:
                    by_id[eid] = series
    lines = [f"HST1 start={start} entities={len(entities)}"]
    used = len(lines[0])
    letters = "abcdefghijklmnopqrstuvwxyzABCDEF"
    # 0.3.35: the SKIPPED lines still to come and END are reserved up front, so the block always ends with END.
    skips = [len(_skipped(eid)) + 1 for eid in entities]
    for i, eid in enumerate(entities):
        series = by_id.get(eid, [])
        legend: dict[str, str] = {}
        changes, prev, overflow = [], start, False
        for row in series:
            if not isinstance(row, dict):
                continue
            t = _epoch(row.get("last_changed") or row.get("lu"))
            state = _plain(row.get("state"), 32) or "?"
            if t is None:
                continue
            if state not in legend:
                if len(legend) >= MAX_HISTORY_STATES:
                    overflow = True
                    continue
                legend[state] = letters[len(legend)]
            changes.append(_b36(t - prev) + legend[state])
            prev = max(prev, t)
        line = (f"E {eid} n={len(changes)} legend=" + ",".join(f"{v}:{k}" for k, v in legend.items())
                + (" STATES_OVERFLOW" if overflow else "") + "\nC " + " ".join(changes))
        if used + len(line) + 1 + sum(skips[i + 1:]) + len("\nEND") > budget:
            lines.append(_skipped(eid))
            used += len(lines[-1]) + 1
            continue
        lines.append(line)
        used += len(line) + 1
    lines.append("END")
    return lines


def _skipped(eid: str) -> str:
    return f"E {eid} SKIPPED budget (file again with fewer entities or days)"


def read_history(ha, entities: tuple[str, ...], days: int, now: float | None = None,
                 budget: int = MAX_OUTPUT_CHARS) -> str:
    """One read-only window; the returned text (header line included) is at most ``budget`` characters."""
    end = int(now if now is not None else time.time())
    start = end - days * 86400
    reply = ha.history_period(entities, start, end)
    head = f"History of {len(entities)} entities over {days} day(s), read-only (state changes only):"
    lines = project_history(reply, entities, start, budget - len(head) - 1)
    return "\n".join([head, *lines])


def run_lookup(engine, lr: LookupRequest):
    """Ask the owner, then render read-only. Returns an engine Result."""
    from .engine import REJECTED, TIMED_OUT, Result

    engine.j.audit(lr.request_id, "RECEIVED", digest=lr.digest, requested_by=lr.requested_by, kind="lookup")
    preview = lr.template if len(lr.template) <= 600 else lr.template[:600] + "\n…"
    message = (f"{lr.request_id} from {lr.requested_by}\nREAD-ONLY lookup (changes nothing). Template:\n"
               f"{preview}\nAI title: \"{lr.title}\"")
    if lr.zwave_config_entity:
        message += (f"\nAlso reads the Z-Wave settings of the device of {lr.zwave_config_entity} "
                    "(read-only, nothing is written to the device).")
    if lr.sense_device_tags:
        message += ("\nAlso reads Sense's device list, read-only, using Home Assistant's own Sense login "
                    "(no new login, nothing changed in Sense or Home Assistant; if that login's stored token has "
                    "expired it is renewed once, as Home Assistant does at startup).")
    if lr.history_entities:
        message += (f"\nAlso reads {lr.history_days} day(s) of history of {len(lr.history_entities)} entities: "
                    + ", ".join(lr.history_entities[:6]) + (" ..." if len(lr.history_entities) > 6 else ""))
    outcome = engine._ask(lr.request_id, "LOOKUP", "House Brain Deployer: approve lookup?", message,
                          engine.s.timing.approval_timeout)
    if outcome != approval.APPROVE:
        return Result(REJECTED if outcome == approval.REJECT else TIMED_OUT, [outcome])
    try:
        rendered = engine.ha.render_template(lr.template)
    except Exception as err:  # noqa: BLE001 - template errors are reported, never retried
        return Result(LOOKUP_FAILED, [f"template error: {scrub(str(err))[:300]}"])
    parts = sum(map(bool, (lr.zwave_config_entity, lr.history_entities, lr.sense_device_tags)))
    if parts and len(rendered) > TEMPLATE_CHARS_WITH_PARTS:   # 0.3.35: leave the parts their room
        cut = rendered.rfind("\n", 0, TEMPLATE_CHARS_WITH_PARTS - 100)
        rendered = (rendered[:cut if cut > 0 else TEMPLATE_CHARS_WITH_PARTS - 100]
                    + f"\n[template output cut at {TEMPLATE_CHARS_WITH_PARTS} characters to leave room for the other parts]")

    def budget() -> int:
        """0.3.35: the parts still to run share what is left equally (2 characters each for the blank line)."""
        nonlocal parts
        share = (MAX_OUTPUT_CHARS - len(rendered) - 2 * parts) // parts
        parts -= 1
        return share
    if lr.zwave_config_entity:
        try:
            rendered += "\n\n" + read_zwave_config(engine.ha, lr.zwave_config_entity, budget=budget())
        except Exception as err:  # noqa: BLE001 - reported once, never retried
            return Result(LOOKUP_FAILED, [f"zwave config read error: {scrub(str(err))[:300]}"],
                          {"lookup_output": scrub(rendered)})
    if lr.history_entities:
        try:
            rendered += "\n\n" + read_history(engine.ha, lr.history_entities, lr.history_days, budget=budget())
        except Exception as err:  # noqa: BLE001 - reported once, never retried
            return Result(LOOKUP_FAILED, [f"history read error: {scrub(str(err))[:300]}"],
                          {"lookup_output": scrub(rendered)})
    if lr.sense_device_tags:
        try:
            rendered += "\n\n" + read_sense_device_tags(os.path.dirname(engine.pk.root.rstrip("/")), budget=budget())
        except SenseLookupError as err:
            return Result(LOOKUP_FAILED, [f"sense read error: {err}"], {"lookup_output": scrub(rendered)})
        except Exception as err:  # noqa: BLE001 - never echoes the exception text (it could hold a token)
            return Result(LOOKUP_FAILED, [f"sense read error: SENSE_READ_FAILED {type(err).__name__}"],
                          {"lookup_output": scrub(rendered)})
    return Result(LOOKUP_OK, [], {"lookup_output": scrub(rendered)})
