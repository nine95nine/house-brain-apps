"""0.8.9 Update review queue: the AI review step (owner pop-up 2026-10-12, "Yes, build it"; "Keep auto for low-risk").

Before 0.8.9 the owner waited for an AI review of each update that nothing ever asked for: Core/OS reviews said
"Release-note check: by the AI in chat", App updates carried only this App's own rule-based review, and low-risk
App updates installed by themselves without any review. Now:

* This App lists every waiting update on the tracking issue (``updatequeue.py``), with a machine line naming the
  items that still need an AI review.
* An AI (a Claude routine, or a chat) reads that list, checks the real release notes and posts one review comment
  per update with a machine line
  ``<!-- house_brain_ai_update_review.v1 {"item": "app:<slug>@<version>", "verdict": "APPROVE", "summary": "..."} -->``
  (verdict ``APPROVE``, ``HOLD`` or ``DECLINE``; several lines may share one comment).
* The gate: no phone ask and no automatic install before that review exists. An ``APPROVE`` keeps everything as
  before (a low-risk update may still install by itself at night). ``HOLD`` or ``DECLINE`` never installs by itself;
  the owner is asked, with the AI's recommendation on the phone, and decides. If no review arrives within
  ``AI_WAIT_HOURS`` the owner is asked anyway ("AI review: none") and nothing installs by itself, so a missing
  reviewer can never freeze updates.

Trust: comment text is untrusted data. Only comments by the repository owner's account count, every field is checked
to a closed shape, and a review can only make this App ask or wait; it can never approve anything (the owner's
Approve tap stays the only approval). ``CONTROL_AUTHORITY=NONE``.
Cost: one GitHub read every ``READ_EVERY`` seconds, $0.
"""
from __future__ import annotations

import calendar
import json
import re
import time
from dataclasses import dataclass

SCHEMA = "house_brain_ai_update_review.v1"
RE_MACHINE = re.compile(r"<!-- house_brain_ai_update_review\.v1 (\{[^\n]*?\}) -->")
RE_ITEM = re.compile(r"^(?:app:[a-z0-9][a-z0-9_]{0,99}|core|os|pifw|entity:[a-z0-9_]{1,40}\.[a-z0-9_]{1,200})"
                     r"@[0-9A-Za-z][0-9A-Za-z._+:-]{0,39}$")
VERDICTS = ("APPROVE", "HOLD", "DECLINE")
DOC = "ai_reviews"
AI_WAIT_HOURS = 48.0
READ_EVERY = 600.0
LOOKBACK = 30 * 86400.0           # first read: reviews from the last 30 days
KEEP = 60 * 86400.0
MAX_LINES_PER_COMMENT = 20
PAGE = 100                        # GitHub's page size for issue comments
MAX_PAGES = 10                    # 0.9.0: one read takes every page (up to 1,000 comments), not only the oldest 100


def item_app(slug: str, version: str) -> str:
    return f"app:{slug}@{version}"


def item_entity(entity_id: str, version: str) -> str:
    return f"entity:{entity_id}@{version}"


def _ts(value) -> float | None:
    if not isinstance(value, str):
        return None
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?Z", value)
    return float(calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S"))) if m else None


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _plain(text, limit: int) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f<>`*_\[\]|]", " ", str(text))).strip()[:limit]


def parse(comment: dict, owner: str) -> list[dict]:
    """One comment -> its reviews ``{item, verdict, summary, at, url}``; anything not in the closed shape is ignored."""
    if not isinstance(comment, dict):
        return []
    user = comment.get("user") or {}
    if not isinstance(user, dict) or str(user.get("login", "")).lower() != owner.lower():
        return []
    body, at = comment.get("body"), _ts(comment.get("created_at"))
    if not isinstance(body, str) or at is None:
        return []
    url = comment.get("html_url") if isinstance(comment.get("html_url"), str) and \
        re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/\d+#issuecomment-\d+",
                     comment["html_url"]) else ""
    out = []
    for raw in RE_MACHINE.findall(body)[:MAX_LINES_PER_COMMENT]:
        try:
            doc = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(doc, dict) or set(doc) - {"item", "verdict", "summary"}:
            continue
        item, verdict = doc.get("item"), doc.get("verdict")
        if not (isinstance(item, str) and RE_ITEM.fullmatch(item)) or verdict not in VERDICTS:
            continue
        out.append({"item": item, "verdict": verdict, "summary": _plain(doc.get("summary") or "", 160),
                    "at": at, "url": url})
    return out


@dataclass(frozen=True)
class Gate:
    state: str            # off | ok | wait | missing
    verdict: str | None
    line: str             # one phone/review line ("" when the gate is off)
    auto_ok: bool         # may this update install by itself (when everything else allows it)?

    @property
    def proceed(self) -> bool:
        return self.state != "wait"


OFF = Gate("off", None, "", True)


class AiReviews:
    def __init__(self, gh, journal, issue: int, enabled: bool = True, clock=time.time) -> None:
        self.gh, self.j, self.issue, self.enabled, self.clock = gh, journal, int(issue), enabled, clock
        self.owner = gh.repo.split("/", 1)[0] if gh is not None else ""

    def _doc(self) -> dict:
        doc = self.j.load_doc(DOC, {})
        doc = doc if isinstance(doc, dict) else {}
        doc.setdefault("since", None)
        doc.setdefault("reviews", {})
        doc.setdefault("read_at", 0)
        return doc

    def refresh(self, force: bool = False) -> None:
        """Read new review comments (at most every READ_EVERY seconds). Never raises past a network fault."""
        if not self.enabled or not self.issue:
            return
        now = self.clock()
        doc = self._doc()
        if not force and now - float(doc["read_at"] or 0) < READ_EVERY:
            return
        since = doc["since"] or _iso(now - LOOKBACK)
        comments = []
        for page in range(1, MAX_PAGES + 1):    # 0.9.0 fix: the first read saw only the oldest 100 of #454's comments
            chunk = self.gh.issue_comments(self.issue, since, page)   # and found a review hours late (live 2026-10-12)
            chunk = chunk if isinstance(chunk, list) else []
            comments += chunk
            if len(chunk) < PAGE:
                break
        newest = _ts(since) or now - LOOKBACK
        reviews = doc["reviews"]
        for c in comments if isinstance(comments, list) else []:
            at = _ts((c or {}).get("updated_at")) if isinstance(c, dict) else None
            if at is not None:
                newest = max(newest, at)
            for r in parse(c, self.owner):
                old = reviews.get(r["item"])
                if old is None or r["at"] >= float(old.get("at") or 0):     # the newest review of an item wins
                    reviews[r["item"]] = {k: r[k] for k in ("verdict", "summary", "at", "url")}
        doc["reviews"] = {k: v for k, v in reviews.items() if now - float(v.get("at") or 0) < KEEP}
        doc["since"] = _iso(max(newest - 1, now - LOOKBACK))
        doc["read_at"] = now
        self.j.save_doc(DOC, doc)

    def get(self, item: str) -> dict | None:
        return self._doc()["reviews"].get(item)

    def gate(self, item: str, first_seen: float | None) -> Gate:
        """May this update be asked (and may it install by itself)? ``first_seen``: when it was first offered."""
        if not self.enabled:
            return OFF
        r = self.get(item)
        if r is not None:
            words = {"APPROVE": "approve", "HOLD": "hold it for now", "DECLINE": "decline"}[r["verdict"]]
            summary = f": {r['summary']}" if r.get("summary") else ""
            return Gate("ok", r["verdict"], f"AI review: {words}{summary}"[:200], r["verdict"] == "APPROVE")
        waited = (self.clock() - float(first_seen)) / 3600 if first_seen else 0.0
        if waited < AI_WAIT_HOURS:
            return Gate("wait", None, "AI review: requested on the tracking issue", False)
        return Gate("missing", None, f"AI review: none after {AI_WAIT_HOURS:.0f} h (asked anyway; never installs "
                                     "by itself)", False)

    def ask_anyway_at(self, first_seen: float | None) -> str:
        """When the phone is asked even without a review (a fixed time, so the waiting list stays stable)."""
        start = float(first_seen) if first_seen else self.clock()
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(start + AI_WAIT_HOURS * 3600))

    def wait_left_hours(self, first_seen: float | None) -> float:
        waited = (self.clock() - float(first_seen)) / 3600 if first_seen else 0.0
        return max(0.0, AI_WAIT_HOURS - waited)
