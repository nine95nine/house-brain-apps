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
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from . import approval, net
from .manifest import RE_REQUEST_ID, REQUESTERS, ManifestError, sanitize_text

LOOKUP_SCHEMA = "house_brain_lookup_request.v1"
MAX_TEMPLATE_CHARS = 4000
MAX_OUTPUT_CHARS = 20000
MAX_ZWAVE_PARAMETERS = 200
RE_ENTITY_ID = re.compile(r"^[a-z_]{2,32}\.[a-z0-9_]{1,96}$")
RE_DEVICE_ID = re.compile(r"^[0-9a-f]{32}$")
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
    unknown = set(data) - required - {"note", "zwave_config_entity"}
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
    return LookupRequest(rid, sanitize_text(title, 80), data["requested_by"], issue, template,
                         sanitize_text(note, 300), hashlib.sha256(raw_bytes).hexdigest(), zwave)


def scrub(output: str) -> str:
    out = net.redact(output)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[redacted]", out)
    if len(out) > MAX_OUTPUT_CHARS:
        out = out[:MAX_OUTPUT_CHARS] + "\n[truncated]"
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


def read_zwave_config(ha, entity_id: str) -> str:
    """Read-only: device id via the ``device_id()`` template, then the parameter list. Never writes."""
    if not RE_ENTITY_ID.fullmatch(entity_id):
        raise ManifestError("PATTERN", "zwave_config_entity")
    device_id = ha.render_template("{{ device_id('" + entity_id + "') or '' }}").strip()
    if not RE_DEVICE_ID.fullmatch(device_id):
        return f"Z-Wave configuration of {entity_id}: no device found for this entity."
    lines = project_zwave_config(ha.zwave_config_parameters(device_id))
    head = f"Z-Wave configuration of the device of {entity_id} ({len(lines)} parameters, read-only):"
    return "\n".join([head, *lines]) if lines else head + "\n(none reported)"


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
    outcome = engine._ask(lr.request_id, "LOOKUP", "House Brain Deployer: approve lookup?", message,
                          engine.s.timing.approval_timeout)
    if outcome != approval.APPROVE:
        return Result(REJECTED if outcome == approval.REJECT else TIMED_OUT, [outcome])
    try:
        rendered = engine.ha.render_template(lr.template)
    except Exception as err:  # noqa: BLE001 - template errors are reported, never retried
        return Result(LOOKUP_FAILED, [f"template error: {scrub(str(err))[:300]}"])
    if lr.zwave_config_entity:
        try:
            rendered += "\n\n" + read_zwave_config(engine.ha, lr.zwave_config_entity)
        except Exception as err:  # noqa: BLE001 - reported once, never retried
            return Result(LOOKUP_FAILED, [f"zwave config read error: {scrub(str(err))[:300]}"],
                          {"lookup_output": scrub(rendered)})
    return Result(LOOKUP_OK, [], {"lookup_output": scrub(rendered)})
