"""0.9.39 re-ask back-off (owner 2026-10-31, after the unattended week of 2026-10-27..31).

Live on 0.9.37 two unanswered updates (OS 18.4 and an ESPHome beta) were asked on the phone every 6 hours for four
days: about 8 pushes a day, nothing answered, and every one of them counted against the daily ask limit that the
chats' requests and the fixes share. An update ask that gets no answer now waits longer each time (6 h, 12 h, 24 h),
and after ``MAX_PHONE_TIMEOUTS`` unanswered asks the phone is left alone: the item stays on the Maintenance page
("Needs your decision") and in the morning summary, and the phone asks again only once the owner has decided
something on the page (a sign they are back). Phone asks for updates also respect the daily ask limit now.
"""
from __future__ import annotations

import time

BACKOFF = (1, 2, 4)             # x reask_hours after the 1st, 2nd, 3rd unanswered ask: 6 h, 12 h, 24 h (6 h default)
MAX_PHONE_TIMEOUTS = 3          # after this many unanswered asks the phone is left alone
PAGE_ONLY_NOTE = ("asked on your phone 3 times without an answer; it waits on the Maintenance page (Needs your "
                  "decision) and the phone asks again once you decide something there")
LIMIT_NOTE = "today's phone-ask limit is used up; asks again tomorrow (or decide it on the Maintenance page now)"


def wait_seconds(reask_hours: float, timeouts: int) -> float:
    return float(reask_hours) * 3600.0 * BACKOFF[min(max(int(timeouts) - 1, 0), len(BACKOFF) - 1)]


def phone_allowed(timeouts: int) -> bool:
    return int(timeouts) < MAX_PHONE_TIMEOUTS


def next_ask_note(asked: float, reask_hours: float, timeouts: int) -> str:
    at = float(asked) + wait_seconds(reask_hours, timeouts)
    return "asked on your phone; asks again at " + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(at))


def budget(settings) -> tuple[bool, str]:
    """May a phone ask be sent now? ``settings.ask_allowed`` (set by main) is the daily-limit check."""
    fn = getattr(settings, "ask_allowed", None)
    if fn is None:
        return True, ""
    try:
        return (True, "") if fn() else (False, LIMIT_NOTE)
    except Exception:  # noqa: BLE001 - a broken budget read never blocks an ask
        return True, ""
