"""Re-ask a request whose approval timed out (``house_brain_reask_request.v1``, 0.3.8).

Owner decision (pop-up 2026-10-08, "A: Deployer re-ask (0.3.8)"): a request whose deploy approval
expired unanswered (ledger outcome exactly ``TIMED_OUT``) may be asked ONE more time, by reference
only. An AI commits ``deploy/requests/<new id>/manifest.json`` naming the original id; the Deployer
re-reads the ORIGINAL manifest at the current branch head, requires its bytes to be the ones it
processed before (SHA-256 pinned in the ledger) and runs it through the unchanged engine path:
prepare checks, live-hash checks, a fresh owner approval with a new nonce, backup, rollback.

A re-ask never approves anything; it only asks again. Reject is final (never re-asked), at most
``MAX_REASKS`` re-asks per original, a re-ask of a re-ask is refused (point at the original), read-only
lookups are not re-asked, and nothing is ever re-asked automatically.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .manifest import RE_REQUEST_ID, REQUESTERS, ManifestError, sanitize_text

REASK_SCHEMA = "house_brain_reask_request.v1"
MAX_REASK_BYTES = 4 * 1024
MAX_REASKS = 2                       # per original request id
REASKABLE_OUTCOME = "TIMED_OUT"      # the only ledger outcome that may be asked again


@dataclass(frozen=True)
class ReaskRequest:
    request_id: str
    reask_of: str
    requested_by: str
    tracking_issue: int | None
    note: str
    digest: str


def is_reask(raw_bytes: bytes) -> bool:
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return False
    return isinstance(data, dict) and data.get("schema") == REASK_SCHEMA


def parse_reask(raw_bytes: bytes) -> ReaskRequest:
    """Closed key set, duplicate keys refused, bounded size; raises ManifestError."""
    if len(raw_bytes) > MAX_REASK_BYTES:
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
    required = {"schema", "request_id", "reask_of", "requested_by"}
    unknown = set(data) - required - {"tracking_issue", "note"}
    if required - set(data):
        raise ManifestError("MISSING_KEY", sorted(required - set(data))[0])
    if unknown:
        raise ManifestError("UNKNOWN_KEY", sorted(unknown)[0][:40])
    if data["schema"] != REASK_SCHEMA:
        raise ManifestError("SCHEMA_VERSION")
    rid = data["request_id"]
    if not isinstance(rid, str) or not RE_REQUEST_ID.fullmatch(rid):
        raise ManifestError("PATTERN", "request_id")
    of = data["reask_of"]
    if not isinstance(of, str) or not RE_REQUEST_ID.fullmatch(of):
        raise ManifestError("PATTERN", "reask_of")
    if of == rid:
        raise ManifestError("PATTERN", "reask_of equals request_id")
    if data["requested_by"] not in REQUESTERS:
        raise ManifestError("REQUESTER")
    issue = data.get("tracking_issue")
    if issue is not None and (isinstance(issue, bool) or not isinstance(issue, int)
                              or not 1 <= issue <= 10_000_000):
        raise ManifestError("RANGE", "tracking_issue")
    note = data.get("note", "")
    if not isinstance(note, str) or len(note) > 2000:
        raise ManifestError("TYPE", "note")
    return ReaskRequest(rid, of, data["requested_by"], issue, sanitize_text(note, 300),
                        hashlib.sha256(raw_bytes).hexdigest())


def check_ledger(ledger: dict, rq: ReaskRequest) -> tuple[str, str] | None:
    """Ledger-side guards. Returns (code, ended_as) when refused, None when the re-ask may proceed.

    ``ledger`` keys are exact request ids; a dry-run entry (``<id>#dry``) is never matched."""
    entry = ledger.get(rq.reask_of)
    if not isinstance(entry, dict):
        return "REASK_UNKNOWN", ""
    if entry.get("reask_of"):
        return "REASK_OF_REASK", ""
    outcome = str(entry.get("outcome") or "")
    if outcome != REASKABLE_OUTCOME:
        return "REASK_NOT_TIMED_OUT", outcome
    prior = [x for x in (entry.get("reasked_by") or []) if x != rq.request_id]
    if len(prior) >= MAX_REASKS:
        return "REASK_LIMIT", str(len(prior))
    for other in prior:
        ended = str((ledger.get(other) or {}).get("outcome") or "UNFINISHED")
        if ended != REASKABLE_OUTCOME:
            return "REASK_NOT_TIMED_OUT", ended
    return None
