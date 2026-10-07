"""Owner-approved, read-only lookups (``house_brain_lookup_request.v1``).

An AI files a Home Assistant template in ``deploy/requests/<id>/manifest.json``.
The owner sees the exact template and approves (same channels as deploys); the
Deployer renders it through Core's read-only ``POST /api/template`` and posts
the scrubbed, size-bounded output to the tracking issue. Nothing is written:
Home Assistant templates cannot call services or change state. The Deployer
adds no service call, file write or restart for lookups.
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
    unknown = set(data) - required - {"note"}
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
    return LookupRequest(rid, sanitize_text(title, 80), data["requested_by"], issue, template,
                         sanitize_text(note, 300), hashlib.sha256(raw_bytes).hexdigest())


def scrub(output: str) -> str:
    out = net.redact(output)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[redacted]", out)
    if len(out) > MAX_OUTPUT_CHARS:
        out = out[:MAX_OUTPUT_CHARS] + "\n[truncated]"
    return out


def run_lookup(engine, lr: LookupRequest):
    """Ask the owner, then render read-only. Returns an engine Result."""
    from .engine import REJECTED, TIMED_OUT, Result

    engine.j.audit(lr.request_id, "RECEIVED", digest=lr.digest, requested_by=lr.requested_by, kind="lookup")
    preview = lr.template if len(lr.template) <= 600 else lr.template[:600] + "\n…"
    message = (f"{lr.request_id} from {lr.requested_by}\nREAD-ONLY lookup (changes nothing). Template:\n"
               f"{preview}\nAI title: \"{lr.title}\"")
    outcome = engine._ask(lr.request_id, "LOOKUP", "House Brain Deployer: approve lookup?", message,
                          engine.s.timing.approval_timeout)
    if outcome != approval.APPROVE:
        return Result(REJECTED if outcome == approval.REJECT else TIMED_OUT, [outcome])
    try:
        rendered = engine.ha.render_template(lr.template)
    except Exception as err:  # noqa: BLE001 - template errors are reported, never retried
        return Result(LOOKUP_FAILED, [f"template error: {scrub(str(err))[:300]}"])
    return Result(LOOKUP_OK, [], {"lookup_output": scrub(rendered)})
