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


# 0.9.63 (Maintenance chat 2026-11-18): the morning summary has posted about 12:00 Arizona every day since 2026-10-20
# instead of 08:00. From outside nobody can tell whether the hour option says 12 or the problem check did not run in
# the morning. The summary now says both: the hour it was due (and that the hour is the option ``digest_hour``), when
# it was built, and how long before it the previous problem check ran. No new post, no network call.
DEFAULT_DIGEST_HOUR = 8


def _span(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 120:
        return f"{int(seconds)} s"
    if seconds < 7200:
        return f"{int(seconds // 60)} min"
    return f"{seconds / 3600:.1f} h"


def timing_line(now: float, digest_hour: int, prev_check: float | None) -> str:
    """One summary line: due hour (option ``digest_hour``), built time, minutes late, gap since the previous check."""
    lt = time.localtime(now)
    name, _, _ = zone(now)
    late = max(0, (lt.tm_hour - int(digest_hour)) * 60 + lt.tm_min)
    hour = f"{int(digest_hour):02d}:00 (option `digest_hour`: {int(digest_hour)}"
    hour += ")" if int(digest_hour) == DEFAULT_DIGEST_HOUR else f"; the default is {DEFAULT_DIGEST_HOUR})"
    gap = ("this is the first problem check since the App started" if prev_check is None
           else f"the problem check before this one ran {_span(now - prev_check)} earlier")
    return (f"Timing: this summary was due at {hour} and was built at {time.strftime('%H:%M', lt)} {name}, "
            f"{late} min after it was due; {gap}.")
