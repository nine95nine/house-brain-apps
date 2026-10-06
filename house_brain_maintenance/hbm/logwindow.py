"""App log window (0.5.4, owner decision 2026-10-06): filter one App's verbose log to a UTC window.

Supervisor's verbose format (``?verbose``) starts every journal entry with a UTC timestamp:
``YYYY-MM-DD HH:MM:SS.mmm <hostname> <identifier>[<pid>]: <message>``. Lines without a timestamp are
continuations of the entry above and share its time. The hostname is dropped, every kept line is scrubbed
(``issues.scrub``) and the output is capped. Keywords are plain text (case-insensitive), never patterns.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime

from .issues import scrub

MAX_WINDOW_SECONDS = 6 * 3600
MAX_KEYWORDS = 5
MAX_SHOWN = 200
MAX_LINE_CHARS = 300
MAX_BLOCK_CHARS = 40000
RE_KEYWORD = re.compile(r"^[\x20-\x7e]{1,60}$")
RE_ENTRY = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d{3} \S* (.*)$")


def parse_keywords(text: str | None) -> list[str]:
    """``"a|b"`` -> ``["a", "b"]``; empty -> no filter. Raises ValueError on anything else."""
    if text in (None, ""):
        return []
    if not isinstance(text, str):
        raise ValueError("keywords")
    words = [w.strip() for w in text.split("|")]
    if len(words) > MAX_KEYWORDS or any(not w or not RE_KEYWORD.fullmatch(w) for w in words):
        raise ValueError("keywords")
    return words


def _ts(text: str) -> float | None:
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp()
    except ValueError:
        return None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def window(log: str, since: float, until: float, keywords: list[str]) -> tuple[list[str], dict]:
    """(scrubbed matching lines, facts) for entries with ``since <= time < until``."""
    lowered = [k.lower() for k in keywords]
    first = last = None
    read = in_window = matched = 0
    shown: list[str] = []
    size = 0
    current: float | None = None
    for raw in log.splitlines():
        read += 1
        entry = RE_ENTRY.match(raw)
        if entry:
            current = _ts(entry.group(1))
            body = entry.group(2)
            stamp = entry.group(1)
            if current is not None:
                first = current if first is None else min(first, current)
                last = current if last is None else max(last, current)
        else:
            body, stamp = raw.strip(), "  ..."
        if current is None or not since <= current < until or not body:
            continue
        in_window += 1
        if lowered and not any(k in body.lower() for k in lowered):
            continue
        matched += 1
        if len(shown) < MAX_SHOWN:
            line = scrub(f"{stamp}Z {body}" if entry else f"{stamp} {body}", MAX_LINE_CHARS).replace("```", "'''")
            if size + len(line) + 1 > MAX_BLOCK_CHARS:
                continue
            shown.append(line)
            size += len(line) + 1
    facts = {
        "lines_read": read,
        "log_from": _iso(first) if first is not None else None,
        "log_to": _iso(last) if last is not None else None,
        "reaches_window_start": first is not None and first <= since,
        "lines_in_window": in_window,
        "matched": matched,
        "shown": len(shown),
    }
    return shown, facts
