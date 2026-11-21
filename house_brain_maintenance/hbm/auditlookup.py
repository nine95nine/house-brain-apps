"""0.9.63 AUDIT_LOOKUP: read this App's own audit file for named events in a UTC window (read-only, no approval).

Why (Maintenance chat 2026-11-18): two open questions can only be answered from ``/data/audit.jsonl`` on the Pi, which
no assistant can read: what the ESPHome stable switch did around 2026-11-09 (no ledger row, no result post), and why
the morning summary posts about 12:00 Arizona instead of 08:00 (``PROBLEM_SUMMARY`` times). Until now the only way in
was ``APP_LOG_WINDOW``, which cannot read this App itself and keeps 6 hours of one boot.

Rules (all enforced in ``manifest._audit_lookup`` and here):

* one to five event names, plain ``UPPER_CASE``; a name may end in ``*`` (prefix, at least 3 characters before it);
* a UTC window of at most 7 days (``since`` inclusive, ``until`` exclusive);
* at most ``MAX_SHOWN`` (200) lines, each scrubbed (``issues.scrub`` plus bare token shapes, ``RE_TOKEN_SHAPES``) and
  cut to ``MAX_LINE_CHARS``; the block at most ``MAX_BLOCK_CHARS``; counts per event name are always complete;
* the file is read line by line and never written; a line that is not a JSON object is counted, never shown.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from .issues import RE_TOKEN_SHAPES, scrub

MAX_WINDOW_SECONDS = 7 * 86400
MAX_EVENTS = 5
MAX_SHOWN = 200
MAX_LINE_CHARS = 300
MAX_BLOCK_CHARS = 40000
MAX_COUNTED_NAMES = 30
RE_EVENT = re.compile(r"^[A-Z][A-Z0-9_]{1,59}$")
RE_PREFIX = re.compile(r"^[A-Z][A-Z0-9_]{2,58}\*$")
RE_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def parse_events(text: object) -> tuple[str, ...]:
    """``"A|B_*"`` -> ``("A", "B_*")``. Raises ValueError on anything else (empty, too many, bad shape, repeats)."""
    if not isinstance(text, str) or not text:
        raise ValueError("events")
    names = tuple(n.strip() for n in text.split("|"))
    if len(names) > MAX_EVENTS or len(set(names)) != len(names) \
            or any(not (RE_EVENT.fullmatch(n) or RE_PREFIX.fullmatch(n)) for n in names):
        raise ValueError("events")
    return names


def matches(event: str, names: tuple[str, ...]) -> bool:
    for name in names:
        if name.endswith("*"):
            if event.startswith(name[:-1]):
                return True
        elif event == name:
            return True
    return False


def _ts(text: object) -> float | None:
    if not isinstance(text, str) or not RE_TS.fullmatch(text):
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    except ValueError:
        return None


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def lookup(lines, since: float, until: float, names: tuple[str, ...]) -> tuple[list[str], dict]:
    """(scrubbed matching lines, facts) for audit records with ``since <= ts < until`` and a matching event."""
    first = last = None
    read = unreadable = in_window = matched = 0
    counts: dict[str, int] = {}
    shown: list[str] = []
    size = 0
    for raw in lines:
        read += 1
        try:
            rec = json.loads(raw)
        except ValueError:
            unreadable += 1
            continue
        when = _ts(rec.get("ts")) if isinstance(rec, dict) else None
        if when is None:
            unreadable += 1
            continue
        first = when if first is None else min(first, when)
        last = when if last is None else max(last, when)
        if not since <= when < until:
            continue
        in_window += 1
        event = rec.get("event")
        if not isinstance(event, str) or not matches(event, names):
            continue
        matched += 1
        if event in counts or len(counts) < MAX_COUNTED_NAMES:
            counts[event] = counts.get(event, 0) + 1
        if len(shown) < MAX_SHOWN:
            detail = {k: v for k, v in rec.items() if k not in ("ts", "event", "request_id")}
            text = f"{rec['ts']} {event} {rec.get('request_id', '')} " \
                   f"{json.dumps(detail, sort_keys=True, default=str) if detail else ''}"
            line = scrub(RE_TOKEN_SHAPES.sub("[redacted]", text.rstrip()), MAX_LINE_CHARS).replace("```", "'''")
            if size + len(line) + 1 > MAX_BLOCK_CHARS:
                continue
            shown.append(line)
            size += len(line) + 1
    facts = {
        "lines_read": read,
        "unreadable": unreadable,
        "audit_from": iso(first) if first is not None else None,
        "audit_to": iso(last) if last is not None else None,
        "reaches_window_start": first is not None and first <= since,
        "records_in_window": in_window,
        "matched": matched,
        "shown": len(shown),
        "counts": dict(sorted(counts.items())),
    }
    return shown, facts


def run(path: str, m):
    """The job: read ``path`` (this App's own audit file) for the manifest's window and names. Never writes."""
    from .jobs import DONE, FAILED, Result
    facts = {"events": list(m.audit_events), "window_utc": [iso(m.since), iso(m.until)]}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            shown, found = lookup(fh, m.since, m.until, m.audit_events)
    except FileNotFoundError:
        return Result(FAILED, ["this App has no audit file yet; nothing was changed"], facts)
    facts.update(found)
    reasons = [f"{found['matched']} matching audit record(s) in the window; nothing was changed"]
    if not found["reaches_window_start"]:
        reasons.append(f"the audit file starts after the window start (first record {found['audit_from']})")
    if found["matched"] > found["shown"]:
        reasons.append(f"only the first {found['shown']} records are shown; the counts cover all of them")
    if found["unreadable"]:
        reasons.append(f"{found['unreadable']} line(s) of the file could not be read and are not shown")
    return Result(DONE, reasons, facts, shown)
