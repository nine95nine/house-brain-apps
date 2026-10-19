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

0.9.12 (owner pop-up 2026-10-14, "Build both"). Live finding: two own-store App updates waited in the AI review queue
and left it without any AI review while the reviewer routines were failing silently. Now:

* **Reviewer watchdog.** A waiting update (counted from when it was first offered) or a problem a posted queue line
  listed (counted from that first post, ``WATCH_DOC``) that waits over ``REVIEW_STALL_HOURS`` without an AI review
  raises one problem, ``STALL_KEY``. Like the credential steps it is pushed once and named in the next morning
  summary; it is never an approval ask and never itself put in the queue line. It clears by itself when nothing
  waits too long. AI review off: nothing counts. A review read that failed: nothing new is said and an open one stays.
* **After-update tag.** Each installed App update is remembered ``AFTER_UPDATE_HOURS`` (``INSTALLS_DOC``, at most
  ``MAX_INSTALLS``): the App, the Apps that need its services and the integrations it provides. A problem first seen
  in that window about one of them carries ``"after_update": "<item>"`` in its queue entry (the newest such install).

0.9.19 (owner pop-up 2026-10-18 "Build both as 0.9.19"; reviewer chat audit of #454). Live findings:

* **Stale review reuse.** A review was matched by the problem key only, so the 2026-10-13 review of
  ``backup:incomplete`` (the local Observer, stopped on purpose) was shown on 2026-10-18 next to the same key about a
  different App (ESPHome Device Builder). Now each problem has a fingerprint of what it says (its title with numbers
  masked: "ends in 12 days" and "ends in 11 days" are the same problem, "misses Observer" and "misses ESPHome" are
  not). When it changes, a review posted before the change no longer counts and the problem is queued again.
* **Late queueing.** Problems first alerted before the queue line existed, or cut from a long morning summary,
  reached the queue only on Sunday (Assist Microphone after 12 days). Now every alerted open problem without a
  counting review that no posted queue line has named since its facts last changed is named in a short "problems
  waiting for AI review" post (at most every ``QUEUE_POST_HOURS``).

``CONTROL_AUTHORITY=NONE``. Cost: the review read shares the update reviews' paged read cadence; $0.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from . import aireview as A, issues as I
from .ha import RE_SLUG

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
STALL_KEY = "reviewer:stalled"     # 0.9.12 reviewer watchdog
STALL_SOURCE = "reviewer"
WATCH_DOC = "review_watch"         # {"queued": {problem key: when a posted queue line first listed it}}
REVIEW_STALL_HOURS = 6
MAX_STALL_NAMED = 5
MAX_QUEUED_KEPT = 200
INSTALLS_DOC = "recent_installs"   # 0.9.12 after-update tag: {"installs": [{"at", "item", "apps", "domains"}]}
AFTER_UPDATE_HOURS = 24
MAX_INSTALLS = 20
RE_DOMAIN = re.compile(r"^[a-z0-9_]{1,64}$")
RE_AFTER = re.compile(r'"after_update":"[^"\\]*"')
FP_DOC_KEY = "fps"                 # 0.9.19: watch state {problem key: [fingerprint, since]} (kept after a clear)
MAX_FPS = 500
QUEUE_POST_HOURS = 1.0


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


def fingerprint(title: str) -> str:
    """0.9.19: what a problem says, numbers masked (counts and days change daily; names and kinds do not)."""
    norm = re.sub(r"\d+", "#", " ".join(str(title or "").lower().split()))
    return hashlib.sha256(norm.encode()).hexdigest()[:12]


def note_fingerprint(fps: dict, key: str, title: str, now: float, existed: bool) -> float:
    """Record ``key``'s fingerprint; returns since when its facts are as they are now. A key first fingerprinted while
    it was already open (before 0.9.19) counts as changed now, so an old review is checked once more."""
    fp = fingerprint(title)
    prev = fps.get(key)
    if isinstance(prev, list) and len(prev) == 2 and prev[0] == fp:
        return float(prev[1])
    since = now if (prev is not None or existed) else 0.0
    fps[key] = [fp, since]
    if len(fps) > MAX_FPS:
        for old in sorted(fps, key=lambda k: float(fps[k][1]) if isinstance(fps[k], list) else 0.0)[:len(fps) - MAX_FPS]:
            fps.pop(old, None)
    return since


class ProblemReviews(A.AiReviews):
    DOC = DOC
    since: dict = {}                    # 0.9.19: problem key -> when its facts last changed (set by the watcher)

    def get(self, item: str) -> dict | None:
        """0.9.19: a review counts only when it was posted after the problem's facts last changed."""
        r = super().get(item)
        if r is not None and float(r.get("at") or 0) < float((self.since or {}).get(item, 0) or 0):
            return None
        return r

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


def queue_entries(keys_titles: list[tuple[str, str, str]], reviews, after: dict | None = None) -> list[dict]:
    """The problems the queue line names: [(key, title, source)] without an AI review. 0.9.12: never the reviewer
    watchdog's own problem; ``after`` (key -> update item) adds ``after_update``."""
    after = after or {}
    return [{"key": k, "title": t[:120], "source": s, **({"after_update": after[k]} if after.get(k) else {})}
            for k, t, s in keys_titles if RE_KEY.fullmatch(k) and k != STALL_KEY
            and (reviews is None or reviews.get(k) is None)][:MAX_QUEUE]


def queue_line(keys_titles: list[tuple[str, str, str]], reviews, after: dict | None = None) -> str:
    """The machine line naming problems without an AI review: [(key, title, source)]."""
    need = queue_entries(keys_titles, reviews, after)
    text = json.dumps({"needs_review": need}, separators=(",", ":"))
    # 0.9.12: the report scrub reads "slug@version" as an e-mail address; "\u0040" is the same JSON string
    text = RE_AFTER.sub(lambda m: m.group(0).replace("@", "\\u0040"), text)
    return f"<!-- {QUEUE_SCHEMA} " + text[:6000] + " -->"


def after_tags(open_recs: dict) -> dict:
    """0.9.12: problem key -> the update item it was first seen after (open problems only)."""
    return {k: r["after_update"] for k, r in open_recs.items() if isinstance(r, dict) and r.get("after_update")}


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
              queue_line([(k, str(r.get("title", k)), str(r.get("source", ""))) for k, r, _ in items], reviews,
                         after_tags({k: r for k, r, _ in items}))]
    phone = (f"{len(items)} problem{'s' if len(items) != 1 else ''} still open; the oldest is "
             f"{items[0][2]:.0f} days old. The list is on GitHub.") if items else ""
    return "\n".join(lines), phone


# -- 0.9.12 reviewer watchdog ---------------------------------------------------------------------------------------
def problem_waits(open_recs: dict, queued: dict, reviews) -> list[tuple[str, float]]:
    """(title, waiting since) of each alerted open problem a posted queue line listed that has no AI review yet. It
    waits from the first post that listed it, or from when it was seen again if it cleared and came back."""
    out = []
    for key, rec in open_recs.items():
        if key == STALL_KEY or not isinstance(rec, dict) or not rec.get("alerted") or key not in queued:
            continue
        if reviews.get(key) is None:
            out.append((str(rec.get("title", key)), max(float(queued[key]), float(rec.get("first") or 0))))
    return out


def note_queued(doc: dict, keys: list[str], open_keys: set[str], now: float, since: dict | None = None) -> dict:
    """The watch doc with ``keys`` (just posted in a queue line) added; problems no longer open are dropped. 0.9.19:
    a key queued before its facts last changed (``since``) is queued anew (the watchdog counts from now)."""
    queued = {k: t for k, t in ((doc or {}).get("queued") or {}).items() if k in open_keys}
    since = since or {}
    for key in keys:
        if key not in queued or float(queued[key]) < float(since.get(key, 0) or 0):
            queued[key] = now
    return {"queued": dict(sorted(queued.items(), key=lambda kv: kv[1])[-MAX_QUEUED_KEPT:])}


DETAILS_HOURS = 24.0                # 0.9.21: at most one "problem details" post a day
MAX_DETAIL_STEPS = 4
MAX_DETAILS = 15


def detail_steps(f) -> list[str]:
    """0.9.21: a problem's "what you can do" lines as posted on the tracking issue (scrubbed, bounded)."""
    return [I.scrub(str(s), 500) for s in tuple(f.steps or ())[:MAX_DETAIL_STEPS]]


def details_due(open_recs: dict, posted: dict) -> list[tuple[str, dict, str]]:
    """0.9.21: (key, record, steps fingerprint) of alerted open problems whose lines were never posted or changed
    since (numbers masked), oldest first; never the reviewer watchdog itself."""
    out = []
    for key, rec in sorted(open_recs.items(), key=lambda kv: float((kv[1] or {}).get("first") or 0)):
        if key == STALL_KEY or not isinstance(rec, dict) or not rec.get("alerted") or not rec.get("steps"):
            continue
        fp = fingerprint(" | ".join(rec["steps"]))
        if posted.get(key) != fp:
            out.append((key, rec, fp))
    return out[:MAX_DETAILS]


def need_queue(open_recs: dict, queued: dict, reviews, since: dict | None = None) -> list[tuple[str, str, str]]:
    """0.9.19: (key, title, source) of each alerted open problem without a counting review that no posted queue line
    has named since its facts last changed (never the reviewer watchdog itself)."""
    since = since or {}
    out = []
    for key, rec in sorted(open_recs.items(), key=lambda kv: float((kv[1] or {}).get("first") or 0)):
        if key == STALL_KEY or not isinstance(rec, dict) or not rec.get("alerted") or not RE_KEY.fullmatch(key):
            continue
        if reviews.get(key) is not None:
            continue
        if key in queued and float(queued[key]) >= float(since.get(key, 0) or 0):
            continue
        out.append((key, str(rec.get("title", key)), str(rec.get("source", ""))))
    return out[:MAX_QUEUE]


def stalled(waits: list[tuple[str, float]], now: float) -> list[I.Finding]:
    """One problem when anything waited over REVIEW_STALL_HOURS for its AI review, else none."""
    late = sorted(((name, since) for name, since in waits if now - since > REVIEW_STALL_HOURS * 3600),
                  key=lambda w: (w[1], w[0]))
    if not late:
        return []
    detail = tuple(f"{name[:100]}: waiting {(now - since) / 3600:.0f} h" for name, since in late[:MAX_STALL_NAMED])
    if len(late) > MAX_STALL_NAMED:
        detail += (f"and {len(late) - MAX_STALL_NAMED} more",)
    return [I.Finding(
        STALL_KEY, STALL_SOURCE, I.WARNING,
        f"The AI reviewer has not answered for over {REVIEW_STALL_HOURS} hours ({len(late)} waiting)",
        detail=detail,
        steps=("The AI reviewer runs every hour from the \"House Brain Maintenance reviewers\" chat. Open that chat "
               "and check its last runs; a run that failed shows there.",
               "Nothing installs by itself and your phone is not asked before a review. After 48 hours your phone is "
               "asked about each update anyway.",
               "This clears by itself once the reviews are posted."),
        facts={"waiting": len(late)},
        done="Nothing installs and your phone is not asked before a review; after 48 hours you are asked anyway.",
        todo=("Open the \"House Brain Maintenance reviewers\" chat and check its last runs.",),
        if_ignored="Updates wait up to 48 hours, then your phone asks about each one.")]


# -- 0.9.12 after-update tag ----------------------------------------------------------------------------------------
def note_install(installs: list, item: str, apps, domains, now: float) -> list[dict]:
    """The installs of the last AFTER_UPDATE_HOURS with this one added (newest last, at most MAX_INSTALLS)."""
    keep = [r for r in installs if isinstance(r, dict) and now - float(r.get("at") or 0) < AFTER_UPDATE_HOURS * 3600]
    if A.RE_ITEM.fullmatch(item):
        keep.append({"at": now, "item": item,
                     "apps": sorted({a for a in apps if isinstance(a, str) and RE_SLUG.fullmatch(a)})[:20],
                     "domains": sorted({d for d in domains if isinstance(d, str) and RE_DOMAIN.fullmatch(d)})[:20]})
    return keep[-MAX_INSTALLS:]


def problem_domain(key: str, facts: dict) -> str:
    """The integration a problem is about ("" for none): repairs and log errors by their key, integration entries
    (``entry:``/``reauth:``, keyed by entry id) by their domain."""
    kind, _, rest = key.partition(":")
    if kind == "repair":
        return rest.split(":", 1)[0]
    if kind == "log":
        parts = rest.split(":", 1)[0].split(".")
        if parts[:2] == ["homeassistant", "components"] and len(parts) > 2:
            return parts[2]
        return parts[1] if parts[0] == "custom_components" and len(parts) > 1 else ""
    if kind in ("entry", "reauth"):
        return str((facts or {}).get("domain") or "")
    return ""


def after_update(installs: list, key: str, facts: dict, first: float) -> str | None:
    """The newest install at most AFTER_UPDATE_HOURS before ``first`` that the problem is about: the App itself or an
    App that needs it (``app:<slug>``), or an integration it provides."""
    slug = key[4:] if key.startswith("app:") else ""
    domain = problem_domain(key, facts)
    best: tuple[float, str] | None = None
    for r in installs:
        at = float(r.get("at") or 0) if isinstance(r, dict) else -1.0
        if not 0 <= first - at < AFTER_UPDATE_HOURS * 3600:
            continue
        if (slug and slug in r.get("apps", ())) or (domain and domain in r.get("domains", ())):
            if best is None or at >= best[0]:
                best = (at, str(r.get("item")))
    return best[1] if best else None
