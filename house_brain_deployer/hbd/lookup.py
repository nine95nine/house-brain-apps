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

0.3.36 (#215, owner pop-up 2026-10-25 "Build it"; design
``docs/architecture/SENSE_DEVICE_TAGS_0_3_36_PROPOSAL_R1.md``): the Sense list adds
Sense's expected always-on watts, its always-on flag, the first user name guess and
combined devices (by name only), and drops defaults so every device fits one lookup.

0.3.38 (#215, owner pop-up 2026-10-25 "All of the above and any other method"): an
optional ``statistics_entities`` part (up to 16 ids) with ``statistics_period``
(hour/day/month), ``statistics_days`` and ``statistics_types`` reads one Recorder
long-term statistics window through the read-only WebSocket command
``recorder/statistics_during_period``, after Approve only, and posts compact rows
(``STS1``). Numeric power sensors cannot be posted as ``HST1`` state changes; hourly
mean/min/max can. Sense's ``NameUserGuess`` turned out to be a true/false flag and is
shown as ``name-guessed``.
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
# 0.3.38: statistics part. Days per period mirror the guard in hbd/ha.py (hour 31, day 366, month 1096).
MAX_STATISTICS_DAYS = {"hour": 31, "day": 366, "month": 1096}
STATISTICS_TYPES = ("mean", "min", "max", "change")
DEFAULT_STATISTICS_TYPES = ("mean", "min", "max")
# 0.3.34: the Sense URLs the Deployer can build: the device overview and (0.3.35) the renew that Home Assistant's own
# startup makes after a 401. Never authenticate (password) or logout.
SENSE_API_BASE = "https://api.sense.com/apiservice/api/v1/"
SENSE_TIMEOUT_S = 10.0
# 0.3.36 (owner pop-up 2026-10-25 "Build it"): the projected tags. A line shows exceptions, not defaults:
# ``type=``, ``(Sense: ...)`` when Sense's default type differs, flags only when not the default, ``was`` for a changed
# original name, ``AO=`` expected always-on watts, ``c=``/``f=`` dates, merges and combined devices by name only.
SENSE_TAGS = ("DefaultUserDeviceType", "UserDeviceType", "UserDeviceTypeDisplayString", "DeviceListAllowed",
              "Revoked", "Pending", "Virtual", "TimelineAllowed", "OriginalName", "NameUseredit", "name_useredit",
              "ExpectedAOWattage", "AlwaysOn", "NameUserGuess", "CombinedDevices")
SENSE_DATE_TAGS = ("DateCreated", "DateFirstUsage")
MAX_SENSE_AO_WATTS = 5000
MAX_SENSE_COMBINED = 5
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
    statistics_entities: tuple[str, ...] = field(default_factory=tuple)
    statistics_period: str = ""
    statistics_days: int = 0
    statistics_types: tuple[str, ...] = field(default_factory=tuple)


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
                                      "history_days", "statistics_entities", "statistics_period", "statistics_days",
                                      "statistics_types"}
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
    stats = data.get("statistics_entities", [])
    if (not isinstance(stats, list) or len(stats) > MAX_HISTORY_ENTITIES or len(set(map(str, stats))) != len(stats)
            or not all(isinstance(e, str) and RE_ENTITY_ID.fullmatch(e) for e in stats)):
        raise ManifestError("PATTERN", "statistics_entities")
    period = data.get("statistics_period", "hour" if stats else "")
    if (stats and period not in MAX_STATISTICS_DAYS) or (not stats and period != ""):
        raise ManifestError("PATTERN", "statistics_period")
    sdays = data.get("statistics_days", 1 if stats else 0)
    if isinstance(sdays, bool) or not isinstance(sdays, int) or (
            stats and not 1 <= sdays <= MAX_STATISTICS_DAYS[period]) or (not stats and sdays != 0):
        raise ManifestError("RANGE", "statistics_days")
    stypes = data.get("statistics_types", list(DEFAULT_STATISTICS_TYPES) if stats else [])
    if (not isinstance(stypes, list) or len(set(map(str, stypes))) != len(stypes)
            or not all(isinstance(t, str) and t in STATISTICS_TYPES for t in stypes)
            or (stats and not stypes) or (not stats and stypes)):
        raise ManifestError("PATTERN", "statistics_types")
    return LookupRequest(rid, sanitize_text(title, 80), data["requested_by"], issue, template,
                         sanitize_text(note, 300), hashlib.sha256(raw_bytes).hexdigest(), zwave,
                         sense, tuple(hist), days, tuple(stats), period, sdays, tuple(stypes))


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


def _flag(value) -> bool | None:
    """Sense tags carry booleans as ``true``/``false`` or as strings; anything else is unknown (None)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def _watts(value) -> int | None:
    """``ExpectedAOWattage``: a number (or numeric string) within 0..MAX_SENSE_AO_WATTS, rounded to 1 W; else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if not isinstance(value, (int, float)) or not 0 <= value <= MAX_SENSE_AO_WATTS:   # also refuses NaN
        return None
    return int(round(value))


def project_sense_devices(reply) -> list[str]:
    """Sense ``devices/overview`` reply -> one compact line per device (0.3.36). Ids, account fields and unknown tag
    values drop; defaults are left out so every device fits, and only exceptions are shown."""
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
        name = _plain(d.get("name"), 60) or "(unnamed)"
        parts = [name, "icon=" + (_plain(d.get("icon"), 30) or "-")]
        scalar = {k: tags[k] for k in SENSE_TAGS if k in tags and not isinstance(tags[k], (dict, list))}
        user_type = _plain(scalar.get("UserDeviceType"), 40)
        default_type = _plain(scalar.get("DefaultUserDeviceType"), 40)
        if user_type or default_type:
            parts.append("type=" + (user_type or default_type)
                         + (f" (Sense: {default_type})" if user_type and default_type and default_type != user_type else ""))
        for key, default, label in (("DeviceListAllowed", True, "hidden"), ("Revoked", False, "revoked"),
                                    ("Pending", False, "pending"), ("Virtual", False, "virtual"),
                                    ("TimelineAllowed", True, "no-timeline"), ("AlwaysOn", False, "always-on")):
            flag = _flag(scalar.get(key))
            if flag is not None and flag != default:
                parts.append(label)
        if _flag(scalar.get("NameUseredit")) or _flag(scalar.get("name_useredit")):
            parts.append("renamed")
        original = _plain(scalar.get("OriginalName"), 40)
        if original and original != name:
            parts.append("was " + original)
        guessed = _flag(scalar.get("NameUserGuess"))   # 0.3.38: live data shows a true/false flag, not a name
        guess = "" if guessed is not None else _plain(scalar.get("NameUserGuess"), 40)
        if guessed:
            parts.append("name-guessed")
        elif guess and guess != name:
            parts.append("guess " + guess)
        watts = _watts(scalar.get("ExpectedAOWattage"))
        if watts is not None:
            parts.append(f"AO={watts}W")
        dates = " ".join(f"{tag}={_date(tags.get(key))}" for key, tag in (("DateCreated", "c"), ("DateFirstUsage", "f"))
                         if _date(tags.get(key)))
        if dates:
            parts.append(dates)
        merge = tags.get("MergeId")
        if merge:
            target = names.get(merge)
            parts.append(f"merged into {target}" if target else "merged (target not listed)")
        combined = tags.get("CombinedDevices")
        if isinstance(combined, list) and combined:
            shown = []
            for item in combined[:MAX_SENSE_COMBINED]:
                ref = item.get("id") if isinstance(item, dict) else item
                shown.append(names.get(ref) if isinstance(ref, str) and names.get(ref) else "(not listed)")
            more = len(combined) - MAX_SENSE_COMBINED
            parts.append("combines " + ", ".join(shown) + (f" +{more} more" if more > 0 else ""))
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


# ---------------------------------------------------------------------------------------------
# 0.3.38: Recorder long-term statistics (read-only window, compact STS1 rows)


def _num(value) -> str:
    """A statistic value as at most 5 significant digits; empty when missing or not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value in (
            float("inf"), float("-inf")):
        return ""
    text = f"{value:.5g}"
    return text if "e" not in text else f"{value:.0f}"


def _row_start(value) -> int | None:
    """Recorder rows carry ``start`` as epoch milliseconds (current Core) or an ISO string (older Core)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value / 1000) if value > 1e11 else int(value)
    return _epoch(value)


def project_statistics(reply, entities: tuple[str, ...], start: int, period: str, types: tuple[str, ...],
                       budget: int) -> list[str]:
    """``STS1`` lines: per entity ``<seconds since previous row, base 36>:<v1>,<v2>,...`` in ``types`` order.

    Like ``HST1``: an entity that does not fit is ``SKIPPED`` whole, the SKIPPED lines still to come and ``END`` are
    reserved up front, and nothing is ever cut silently. Values are rounded to 5 significant digits."""
    by_id = reply if isinstance(reply, dict) else {}
    lines = [f"STS1 start={start} period={period} types={','.join(types)} entities={len(entities)}"]
    used = len(lines[0])
    skips = [len(_stat_skipped(eid)) + 1 for eid in entities]
    for i, eid in enumerate(entities):
        rows = by_id.get(eid) if isinstance(by_id.get(eid), list) else []
        tokens, prev = [], start
        for row in rows:
            if not isinstance(row, dict):
                continue
            t = _row_start(row.get("start"))
            if t is None:
                continue
            tokens.append(_b36(t - prev) + ":" + ",".join(_num(row.get(k)) for k in types))
            prev = max(prev, t)
        line = f"S {eid} n={len(tokens)}\nV " + " ".join(tokens)
        if used + len(line) + 1 + sum(skips[i + 1:]) + len("\nEND") > budget:
            lines.append(_stat_skipped(eid))
            used += len(lines[-1]) + 1
            continue
        lines.append(line)
        used += len(line) + 1
    lines.append("END")
    return lines


def _stat_skipped(eid: str) -> str:
    return f"S {eid} SKIPPED budget (file again with fewer entities, fewer days or fewer types)"


def read_statistics(ha, entities: tuple[str, ...], days: int, period: str, types: tuple[str, ...],
                    now: float | None = None, budget: int = MAX_OUTPUT_CHARS) -> str:
    """One read-only statistics window; the returned text (header line included) is at most ``budget`` characters."""
    end = int(now if now is not None else time.time())
    start = end - days * 86400
    reply = ha.statistics_during_period(entities, start, end, period, types)
    head = (f"Statistics of {len(entities)} entities over {days} day(s), period {period}, read-only "
            f"(Recorder long-term statistics):")
    lines = project_statistics(reply, entities, start, period, types, budget - len(head) - 1)
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
    if lr.statistics_entities:
        message += (f"\nAlso reads {lr.statistics_days} day(s) of {lr.statistics_period}ly statistics of "
                    f"{len(lr.statistics_entities)} entities: " + ", ".join(lr.statistics_entities[:6])
                    + (" ..." if len(lr.statistics_entities) > 6 else ""))
    outcome = engine._ask(lr.request_id, "LOOKUP", "House Brain Deployer: approve lookup?", message,
                          engine.s.timing.approval_timeout)
    if outcome != approval.APPROVE:
        return Result(REJECTED if outcome == approval.REJECT else TIMED_OUT, [outcome])
    try:
        rendered = engine.ha.render_template(lr.template)
    except Exception as err:  # noqa: BLE001 - template errors are reported, never retried
        return Result(LOOKUP_FAILED, [f"template error: {scrub(str(err))[:300]}"])
    parts = sum(map(bool, (lr.zwave_config_entity, lr.history_entities, lr.statistics_entities, lr.sense_device_tags)))
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
    if lr.statistics_entities:
        try:
            rendered += "\n\n" + read_statistics(engine.ha, lr.statistics_entities, lr.statistics_days,
                                                 lr.statistics_period, lr.statistics_types, budget=budget())
        except Exception as err:  # noqa: BLE001 - reported once, never retried
            return Result(LOOKUP_FAILED, [f"statistics read error: {scrub(str(err))[:300]}"],
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
