"""Re-ask a request whose approval timed out (``house_brain_reask_request.v1``, 0.3.8).

Owner decision (pop-up 2026-10-08, "A: Deployer re-ask (0.3.8)"): a request whose deploy approval
expired unanswered (ledger outcome exactly ``TIMED_OUT``; from 0.3.11 also a restart approval that expired
unanswered, see ``reaskable``) may be asked again, by reference
only. An AI commits ``deploy/requests/<new id>/manifest.json`` naming the original id; the Deployer
re-reads the ORIGINAL manifest at the current branch head, requires its bytes to be the ones it
processed before (SHA-256 pinned in the ledger) and runs it through the unchanged engine path:
prepare checks, live-hash checks, a fresh owner approval with a new nonce, backup, rollback.

A re-ask never approves anything; it only asks again. Reject is final (never re-asked), at most
``MAX_REASKS`` re-asks per original, a re-ask of a re-ask is refused (point at the original), read-only
lookups are not re-asked, and nothing is ever re-asked automatically.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import time
from dataclasses import dataclass

from .manifest import RE_REQUEST_ID, REQUESTERS, ManifestError, sanitize_text

REASK_SCHEMA = "house_brain_reask_request.v1"
MAX_REASK_BYTES = 4 * 1024
MAX_REASKS = 2                       # per original request id
REASKABLE_OUTCOME = "TIMED_OUT"      # the deploy approval expired unanswered
RESTART_TIMEOUT_OUTCOME = "ROLLED_BACK"  # 0.3.11: only with ``restart_timed_out`` on the ledger entry


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


def reaskable(entry: object) -> bool:
    """May this ledger entry be asked again? ``TIMED_OUT`` (the deploy approval expired unanswered), or, from
    0.3.11 (owner pop-up 2026-10-10), ``ROLLED_BACK`` marked ``restart_timed_out``: the files were installed, the
    restart approval expired unanswered and the old files were put back without a restart. A Reject (deploy or
    restart), a failed config or health check and every other outcome stay final."""
    if not isinstance(entry, dict):
        return False
    outcome = entry.get("outcome")
    return outcome == REASKABLE_OUTCOME or (outcome == RESTART_TIMEOUT_OUTCOME
                                            and entry.get("restart_timed_out") is True)


def check_ledger(ledger: dict, rq: ReaskRequest) -> tuple[str, str] | None:
    """Ledger-side guards. Returns (code, ended_as) when refused, None when the re-ask may proceed.

    ``ledger`` keys are exact request ids; a dry-run entry (``<id>#dry``) is never matched."""
    entry = ledger.get(rq.reask_of)
    if not isinstance(entry, dict):
        return "REASK_UNKNOWN", ""
    if entry.get("reask_of"):
        return "REASK_OF_REASK", ""
    if not reaskable(entry):
        return "REASK_NOT_TIMED_OUT", str(entry.get("outcome") or "")
    prior = [x for x in (entry.get("reasked_by") or []) if x != rq.request_id]
    if len(prior) >= MAX_REASKS:
        return "REASK_LIMIT", str(len(prior))
    for other in prior:
        if not reaskable(ledger.get(other)):
            return "REASK_NOT_TIMED_OUT", str((ledger.get(other) or {}).get("outcome") or "UNFINISHED")
    return None


# ---------------------------------------------------------------------------------------------- 0.3.40 page re-ask
# Owner pop-ups 2026-10-26 "Also restart wait to 60 min" and 2026-10-31 "Mine becomes 0.3.40": an approval the owner
# missed (deploy approval or restart approval expired unanswered) stays on the Deployer page for PAGE_WINDOW_S with an
# owner-only "Try again". The tap asks again exactly like a filed re-ask: same original manifest (pinned to its
# ledger digest at the branch head), the same engine path, fresh approvals, the same limits (MAX_REASKS, the daily
# install limit, Reject final, lookups refused). No new ledger field: the startup history check accepts a closed set.
PAGE_WINDOW_S = 24 * 3600
PAGE_SUFFIX = "-again"
PAGE_NOTE = "Try again tapped on the Deployer page"


def _at(entry: object) -> float | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("at"), str):
        return None
    try:
        return calendar.timegm(time.strptime(entry["at"], "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


def page_id(original: str, ledger: dict) -> str | None:
    """The id a page re-ask of ``original`` gets: ``<original>-again<N>``, N one more than the re-asks so far.
    None when it would not be a valid request id (the original is too long) or is already in the ledger."""
    entry = ledger.get(original)
    n = len([x for x in ((entry or {}).get("reasked_by") or []) if isinstance(x, str)]) + 1
    rid = f"{original}{PAGE_SUFFIX}{n}"
    if not RE_REQUEST_ID.fullmatch(rid) or rid in ledger:
        return None
    return rid


def page_raw(request_id: str, original: str) -> bytes:
    """The re-ask a page tap stands for, as bytes. Deterministic from the two ids, so recovery can rebuild it."""
    return json.dumps({"schema": REASK_SCHEMA, "request_id": request_id, "reask_of": original,
                       "requested_by": "owner", "note": PAGE_NOTE}, sort_keys=True).encode("utf-8")


def page_original(request_id: str, ledger: dict) -> str | None:
    """Recovery: the original a page re-ask id belongs to (its ``reasked_by`` names it; linked before the run)."""
    if PAGE_SUFFIX not in request_id:
        return None
    for key, entry in ledger.items():
        if isinstance(entry, dict) and request_id in (entry.get("reasked_by") or []) \
                and request_id.startswith(key + PAGE_SUFFIX):
            return key
    return None


def page_offers(ledger: dict, now: float) -> list[dict]:
    """Missed approvals the page offers again: an original (not a re-ask, not practice mode) that check_ledger would
    let through, whose last ask ended in the last PAGE_WINDOW_S, and that no page or filed re-ask was refused for.
    Newest first; ``kind`` is "restart" when the install happened and the restart approval expired."""
    refused = {e.get("reask_of") for e in ledger.values() if isinstance(e, dict) and e.get("outcome") == "REFUSED"}
    out = []
    for key, entry in ledger.items():
        if "#" in key or not isinstance(entry, dict) or entry.get("reask_of") or key in refused:
            continue
        rid = page_id(key, ledger)
        if rid is None or check_ledger(ledger, ReaskRequest(rid, key, "owner", None, "", "")) is not None:
            continue
        times = [_at(entry)] + [_at(ledger.get(x)) for x in (entry.get("reasked_by") or [])]
        last = max((t for t in times if t is not None), default=None)
        if last is None or not 0 <= now - last <= PAGE_WINDOW_S:
            continue
        latest = ledger.get((entry.get("reasked_by") or [key])[-1]) or entry
        kind = "restart" if latest.get("restart_timed_out") is True else "install"
        out.append({"request_id": key, "at": last, "kind": kind, "again_id": rid})
    return sorted(out, key=lambda o: -o["at"])
