"""0.9.58 reversibility score (owner decision 2026-11-08 "Tiers A + B", rules-first updates part 3).

How much this house has *proven* that an App's update can be undone, from its own ledger, no AI:

* ``+1`` for every update of the App that finished DONE (its backup -> update -> health watch path worked),
* ``+1`` for every restore drill that brought the App back (``DRILL_OK``, 0.9.58 ``hbm/drill.py``),
* ``-3`` for every failed restore (``FAILED_MANUAL``: the backup did not bring the App back), failed drill
  (``DRILL_FAILED``) or soak failure (``SOAK_FAILED``: the App stopped running within 72 hours of installing by itself),
* only the last ``WINDOW_DAYS`` (90) count: the score decays by itself.

A ``RESTORED`` update (the update failed, the restore worked) is neither: the restore path proved itself, the version
is quarantined by the 90-day history check already.

The evidence card uses it for **minor and major App updates** (tier B): they install by themselves only when the
App's score is at least ``MINOR_AUTO_SCORE`` (3). A patch update (tier A) is not gated (its track record is the
house-wide ``successes`` count). Below the score the card says *wait*, never hold: the owner can still approve the
update on the Maintenance page, and every approved update that works adds a point.
"""
from __future__ import annotations

import calendar
import re
import time

WINDOW_DAYS = 90.0
MINOR_AUTO_SCORE = 3
POINTS = {"DONE": 1, "DRILL_OK": 1, "FAILED_MANUAL": -3, "DRILL_FAILED": -3, "SOAK_FAILED": -3}
DRILL_OK, DRILL_FAILED, SOAK_FAILED = "DRILL_OK", "DRILL_FAILED", "SOAK_FAILED"


def dashed(slug: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", slug[:36].lower()).strip("-")


def prefixes(slug: str) -> tuple[str, str]:
    """The ledger prefixes that belong to one App: its updates and its restore drills."""
    return f"upd-{dashed(slug)}-", f"drill-{dashed(slug)}-"


def _ts(value) -> float | None:
    if not isinstance(value, str):
        return None
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z", value)
    return float(calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S"))) if m else None


def score(ledger: dict, slug: str, now: float) -> tuple[int, dict[str, int]]:
    """(points, counts per outcome) from the ledger rows of this App within the window."""
    points, counts = 0, {}
    pre = prefixes(slug)
    for rid, row in (ledger or {}).items():
        if not isinstance(row, dict) or not str(rid).startswith(pre):
            continue
        at = _ts(row.get("at"))
        outcome = str(row.get("outcome", ""))
        if at is None or now - at >= WINDOW_DAYS * 86400 or outcome not in POINTS:
            continue
        points += POINTS[outcome]
        counts[outcome] = counts.get(outcome, 0) + 1
    return points, counts


def check(ledger: dict, slug: str, bump: str, now: float) -> tuple[str, str]:
    """The evidence-card check: (state, text). ``CLEAN`` for a patch; else CLEAN at the score, WAIT below it."""
    from .evidence import (  # evidence imports this module: a late import avoids the cycle
        CLEAN,
        WAIT,
    )

    if bump == "patch":
        return CLEAN, "not needed for a patch update"
    points, counts = score(ledger, slug, now)
    good = counts.get("DONE", 0) + counts.get(DRILL_OK, 0)
    bad = sum(v for k, v in counts.items() if POINTS[k] < 0)
    detail = f"{good} proven update{'s' if good != 1 else ''}/drill{'s' if good != 1 else ''}, {bad} failure{'s' if bad != 1 else ''} in {int(WINDOW_DAYS)} days"
    if points >= MINOR_AUTO_SCORE:
        return CLEAN, f"reversibility score {points} ({detail})"
    return WAIT, f"reversibility score {points} of {MINOR_AUTO_SCORE} needed for a {bump} update ({detail}); approve it on the Maintenance page or let a restore drill prove the undo path"
