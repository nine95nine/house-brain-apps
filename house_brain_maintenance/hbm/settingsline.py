"""0.9.61 The App says which settings it runs with (deep-dive R1 slice S1, A5 and A13).

The records did not know whether ``update_mode`` was ``tiers``, and the weekly batch's "Saturday 03:00" is the
container's local time, which no post named. Both now ride the posts that go out anyway (the morning summary, each
weekly batch post) and the status entity. No new post, no network call.
"""
from __future__ import annotations

import os
import time

HOME_ZONE = "America/Phoenix"


def zone(now: float | None = None) -> tuple[str, str, bool]:
    """(zone name, UTC offset like "-0700", True when it is Arizona time)."""
    name = (os.environ.get("TZ") or "").strip() or (time.tzname[0] if time.tzname else "") or "unknown"
    offset = time.strftime("%z", time.localtime(time.time() if now is None else now))
    ok = name == HOME_ZONE or (offset == "-0700" and not time.daylight)
    return name, offset, ok


def zone_text(now: float | None = None) -> str:
    name, offset, ok = zone(now)
    text = f"{name} (UTC{offset})"
    return text if ok else text + "; this is not Arizona time, so the weekly batch's 03:00 is not 03:00 in Arizona"


def line(update_mode: str, owed: dict | None, now: float | None = None) -> str:
    """One summary line: update mode, time zone, and posts still owed to GitHub."""
    owed = owed or {}
    posts = (f"{owed.get('owed', 0)} post(s) waiting to reach GitHub, oldest {owed.get('oldest_hours', 0)} h"
             if owed.get("owed") else "no post waiting to reach GitHub")
    return f"This App runs with `update_mode: {update_mode}`, time zone {zone_text(now)}; {posts}."


def attributes(update_mode: str, owed: dict | None, now: float | None = None) -> dict:
    name, offset, ok = zone(now)
    owed = owed or {}
    return {"update_mode": update_mode, "time_zone": f"{name} (UTC{offset})", "time_zone_ok": ok,
            "posts_owed": int(owed.get("owed", 0)), "posts_owed_oldest_hours": float(owed.get("oldest_hours", 0.0))}
