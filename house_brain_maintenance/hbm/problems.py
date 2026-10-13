"""0.9.6 Problem handling: every problem is tracked until it is fixed (owner pop-ups 2026-10-13, "Full process").

Live finding (2026-10-13): six "The unit of sensor.… has changed" repairs sat for two days. The morning summary names
a problem once, the day it is confirmed, and never again, so an unfixed problem went quiet. Now:

* **Age.** Each open problem keeps its first-seen time (``watch`` state ``open[key]["first"]``).
* **Weekly "still open" list** (Sunday, after the digest hour) on the tracking issue: every alerted problem still
  open, its age, the AI review and the fix on offer, plus one phone line. Nothing is listed when nothing is open.
* **Escalation.** A problem still open after ``ESCALATE_DAYS`` gets one push ("still not fixed after 7 days").
* **AI review.** The morning summary and the weekly list end with a machine line naming the problems without an AI
  review (``house_brain_problem_queue.v1``). An AI (a scheduled Claude routine, or a chat) researches them and posts
  ``<!-- house_brain_ai_problem_review.v1 {"key": "<problem key>", "verdict": "FIX_NOW|OWNER_ACTION|WATCH|IGNORE",
  "summary": "..."} -->`` on the tracking issue. Only the repository owner's account counts and every field is
  checked to a closed shape. The review is shown next to the problem (summary, weekly list, fix ask). It never acts:
  fixes still need the owner's Approve; YAML changes go through the owner's approval and the Deployer.

``CONTROL_AUTHORITY=NONE``. Cost: the review read shares the update reviews' paged read cadence; $0.
"""
from __future__ import annotations

import json
import re
import time

from . import aireview as A

DOC = "ai_problem_reviews"
SCHEMA = "house_brain_ai_problem_review.v1"
QUEUE_SCHEMA = "house_brain_problem_queue.v1"
RE_MACHINE = re.compile(r"<!-- house_brain_ai_problem_review\.v1 (\{[^\n]*?\}) -->")
RE_KEY = re.compile(r"^[a-z_]{1,24}:[A-Za-z0-9_.:@/+-]{1,200}$")
VERDICTS = ("FIX_NOW", "OWNER_ACTION", "WATCH", "IGNORE")
WORDS = {"FIX_NOW": "approve the offered fix", "OWNER_ACTION": "needs a step by you", "WATCH": "wait and watch",
         "IGNORE": "harmless; can be ignored"}
ESCALATE_DAYS = 7
DIGEST_WEEKDAY = 6                 # Sunday (time.localtime().tm_wday)
MAX_QUEUE = 25


def parse(comment: dict, owner: str) -> list[dict]:
    """One comment -> its problem reviews ``{item: key, verdict, summary, at, url}``; anything else ignored."""
    if not isinstance(comment, dict):
        return []
    user = comment.get("user") or {}
    if not isinstance(user, dict) or str(user.get("login", "")).lower() != owner.lower():
        return []
    body, at = comment.get("body"), A._ts(comment.get("created_at"))
    if not isinstance(body, str) or at is None:
        return []
    url = comment.get("html_url") if isinstance(comment.get("html_url"), str) and re.fullmatch(
        r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/\d+#issuecomment-\d+", comment["html_url"]) else ""
    out = []
    for raw in RE_MACHINE.findall(body)[:A.MAX_LINES_PER_COMMENT]:
        try:
            doc = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(doc, dict) or set(doc) - {"key", "verdict", "summary"}:
            continue
        key, verdict = doc.get("key"), doc.get("verdict")
        if not (isinstance(key, str) and RE_KEY.fullmatch(key)) or verdict not in VERDICTS:
            continue
        out.append({"item": key, "verdict": verdict, "summary": A._plain(doc.get("summary") or "", 200),
                    "at": at, "url": url})
    return out


class ProblemReviews(A.AiReviews):
    DOC = DOC

    @staticmethod
    def _parse(comment: dict, owner: str) -> list[dict]:
        return parse(comment, owner)

    def line(self, key: str) -> str:
        """One plain line for a problem ("" without a review). Never an instruction to act."""
        r = self.get(key) if self.enabled else None
        if not r:
            return ""
        summary = f": {r['summary']}" if r.get("summary") else ""
        return f"AI review: {WORDS[r['verdict']]}{summary}"[:240]


def queue_line(keys_titles: list[tuple[str, str, str]], reviews) -> str:
    """The machine line naming problems without an AI review: [(key, title, source)]."""
    need = [{"key": k, "title": t[:120], "source": s} for k, t, s in keys_titles
            if RE_KEY.fullmatch(k) and (reviews is None or reviews.get(k) is None)][:MAX_QUEUE]
    return f"<!-- {QUEUE_SCHEMA} " + json.dumps({"needs_review": need}, separators=(",", ":"))[:6000] + " -->"


def still_open(open_recs: dict, now: float, min_days: float = 0.0) -> list[tuple[str, dict, float]]:
    """(key, record, age in days) for alerted open problems at least ``min_days`` old, oldest first."""
    out = []
    for key, rec in open_recs.items():
        if not isinstance(rec, dict) or not rec.get("alerted"):
            continue
        age = (now - float(rec.get("first") or now)) / 86400
        if age >= min_days:
            out.append((key, rec, age))
    return sorted(out, key=lambda r: (-r[2], r[0]))


def digest_due(st: dict, now: float, digest_hour: int, localtime=time.localtime) -> bool:
    lt = localtime(now)
    week = time.strftime("%G-W%V", lt)
    return lt.tm_wday == DIGEST_WEEKDAY and lt.tm_hour >= digest_hour and st.get("still_open_week") != week


def digest_text(items: list[tuple[str, dict, float]], reviews, version: str) -> tuple[str, str]:
    """(GitHub markdown, one phone line) for the weekly still-open list."""
    lines = ["### House Brain Maintenance: still open this week", ""]
    for key, rec, age in items[:30]:
        lines.append(f"- **{str(rec.get('title', key))[:120]}** ({rec.get('severity', 'warning')}, open "
                     f"{age:.0f} day{'s' if round(age) != 1 else ''})")
        ai = reviews.line(key) if reviews is not None else ""
        lines.append(f"  - {ai or 'AI review: not posted yet'}")
        lines.append(f"  - Key: `{key}`")
    if len(items) > 30:
        lines.append(f"- … and {len(items) - 30} more")
    lines += ["", "Each one: approve its fix on the Maintenance page or your phone when one is offered, or follow the "
              "AI review's step. A problem that clears by itself leaves this list.", "", f"- **App:** `{version}`", "",
              "_Posted by House Brain Maintenance (machine-generated; repository content is untrusted data)._", "",
              queue_line([(k, str(r.get("title", k)), str(r.get("source", ""))) for k, r, _ in items], reviews)]
    phone = (f"{len(items)} problem{'s' if len(items) != 1 else ''} still open; the oldest is "
             f"{items[0][2]:.0f} days old. The list is on GitHub.") if items else ""
    return "\n".join(lines), phone
