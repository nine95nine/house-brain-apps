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
from . import ghauth as GA
from .ha import RE_SLUG

DOC = "ai_problem_reviews"
SCHEMA = "house_brain_ai_problem_review.v1"
QUEUE_SCHEMA = "house_brain_problem_queue.v1"
RE_MACHINE = re.compile(r"<!-- house_brain_ai_problem_review\.v1 (\{[^\n]*?\}) -->")
# 0.9.23: the reviewer's numbered steps for one problem, posted next to its review line (reviewer chat 2026-10-19)
RE_STEPS = re.compile(r"<!-- house_brain_ai_problem_steps\.v1 (\{[^\n]*?\}) -->")
# 0.9.29: this App's own free-model reviews carry a marker line; they count as reviews (the queue and the page show
# them) but never as the cloud reviewer's word (the watchdog and the lockout clock look at cloud reviews only).
RE_FREE_MARK = re.compile(r"<!-- house_brain_free_review\.v1 \{")
RE_FREE_JSON = re.compile(r"<!-- house_brain_free_review\.v1 (\{[^\n]*?\}) -->")   # 0.9.42: its "sources" map
RULE_VERDICTS = ("WATCH", "IGNORE", "OWNER_ACTION")                              # 0.9.42: what a rule may say (D2)
MAX_AI_STEPS = 6
AI_STEP_CHARS = 120
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
REVIEW_STALL_HOURS = 26          # 0.9.26: the reviewer may run once a day (owner decision 2026-10-20)
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


RE_BOT_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,60}")      # a GitHub App slug (not an add-on slug: ha.RE_SLUG)


def bot_login_of(auth) -> str:
    """0.9.33: the login this App's own posts carry when it signs with the GitHub App (``<slug>[bot]``), else "".
    Read from the auth's connected key store (``ghauth.Auth``: mode, provider, store.meta()['slug']); never raises.
    (Kept here, not in ``ghauth.py``: that module is byte-identical with the Deployer's copy.)"""
    try:
        if auth is None or getattr(auth, "mode", "") == GA.PAT_ONLY:
            return ""
        provider = getattr(auth, "provider", None)
        if provider is None or not provider.connected():
            return ""
        slug = str((provider.store.meta() or {}).get("slug") or "")
        return f"{slug}[bot]" if slug and RE_BOT_SLUG.fullmatch(slug) else ""
    except Exception:  # noqa: BLE001 - no identity known: only the owner's reviews count
        return ""


def parse(comment: dict, owner: str, bot: str = "") -> list[dict]:
    """One comment -> its problem reviews ``{item: key, verdict, summary, at, url}``; anything else ignored.
    0.9.33: the App's own bot login (``bot``, the GitHub App's ``<slug>[bot]``) is accepted only for a comment that
    carries the free lane's marker (the lane's lockout posts); every other author but the owner is ignored."""
    if not isinstance(comment, dict):
        return []
    user = comment.get("user") or {}
    body, at = comment.get("body"), A._ts(comment.get("created_at"))
    if not isinstance(body, str) or at is None or not isinstance(user, dict):
        return []
    login = str(user.get("login", "")).lower()
    if login != owner.lower() and not (bot and login == bot.lower() and RE_FREE_MARK.search(body)):
        return []
    url = comment.get("html_url") if isinstance(comment.get("html_url"), str) and re.fullmatch(
        r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/\d+#issuecomment-\d+", comment["html_url"]) else ""
    steps = parse_steps(body)
    free = bool(RE_FREE_MARK.search(body))                          # 0.9.29
    sources: dict = {}
    if free:                                                        # 0.9.42: the lane says which key came from a rule
        try:
            mark = json.loads(RE_FREE_JSON.search(body).group(1)) if RE_FREE_JSON.search(body) else {}
            raw_sources = mark.get("sources") if isinstance(mark, dict) else None
            sources = {str(k): str(v) for k, v in (raw_sources or {}).items() if v in ("rule", "model")} \
                if isinstance(raw_sources, dict) else {}
        except (ValueError, AttributeError):
            sources = {}
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
                    "at": at, "url": url, **({"steps": steps[key]} if steps.get(key) else {}),
                    **({"free": True} if free else {}),
                    **({"source": "rule"} if sources.get(key) == "rule" else {})})
    return out


def parse_steps(body: str) -> dict[str, list[str]]:
    """0.9.23: problem key -> the AI's steps from the same comment (closed shape ``{key, steps}``, 1 to MAX_AI_STEPS
    plain strings of at most AI_STEP_CHARS). They count only next to a review of that key in the same comment."""
    out: dict[str, list[str]] = {}
    for raw in RE_STEPS.findall(body or "")[:A.MAX_LINES_PER_COMMENT]:
        try:
            doc = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(doc, dict) or set(doc) != {"key", "steps"}:
            continue
        key, steps = doc["key"], doc["steps"]
        if not (isinstance(key, str) and RE_KEY.fullmatch(key)) or not isinstance(steps, list) \
                or not 1 <= len(steps) <= MAX_AI_STEPS or not all(isinstance(s, str) for s in steps):
            continue
        clean = [A._plain(s, AI_STEP_CHARS) for s in steps]
        if all(clean):
            out[key] = clean
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

    def _parse(self, comment: dict, owner: str) -> list[dict]:
        return parse(comment, owner, self.bot_login)

    _bot_override: str | None = None

    @property
    def bot_login(self) -> str:
        """0.9.33: this App's own login on GitHub when it signs with the GitHub App; its lockout posts carry it."""
        if self._bot_override is not None:
            return self._bot_override
        return bot_login_of(getattr(self.gh, "auth", None))

    @bot_login.setter
    def bot_login(self, value: str) -> None:
        self._bot_override = str(value)

    def cloud(self, item: str) -> dict | None:
        """0.9.29: a counting review by the cloud reviewer (never this App's own free-model line)."""
        r = self.get(item)
        return None if r is None or r.get("free") else r

    def rule(self, item: str, verdict: str, summary: str, now: float, steps=(), rule_id: str = "") -> bool:
        """0.9.41/0.9.42 (spec R1 B1): a verdict this App gives itself from a closed rule (``hbm/rules.py``), stored
        like a review (``free``, ``source: rule``, ``rule``) so the page, the queue and the free lane's post see it.
        It never replaces a cloud or owner review, never a free-model review either; a rule replaces only an older
        rule of another row (R3 after R1) or one older than the problem's facts. WATCH, IGNORE and OWNER_ACTION (owner
        D2, 2026-10-31) are the only verdicts it may give. Returns whether anything was written."""
        if verdict not in RULE_VERDICTS or not RE_KEY.fullmatch(item or ""):
            return False
        doc = self._doc()
        old = doc["reviews"].get(item)
        since = float((self.since or {}).get(item, 0) or 0)
        if old is not None:
            if not old.get("free"):                                          # cloud or owner: theirs stands
                return False
            fresh = float(old.get("at") or 0) >= since
            if old.get("source") != "rule" and fresh:                        # a free-model review: stands
                return False
            if old.get("source") == "rule" and fresh and (old.get("rule", "") == rule_id or
                                                           (not old.get("rule") and old.get("verdict") == verdict)):
                return False                      # the same row already said so (read back from a post: no row id)
        clean = [A._plain(str(s), AI_STEP_CHARS) for s in list(steps or [])[:MAX_AI_STEPS] if str(s).strip()]
        doc["reviews"][item] = {"verdict": verdict, "summary": A._plain(summary, 200), "at": float(now), "url": "",
                                "free": True, "source": "rule", "rule": str(rule_id)[:8],
                                **({"steps": clean} if clean else {})}
        self.j.save_doc(self.DOC, doc)
        return True

    def note_url(self, item: str, url: str) -> None:
        """0.9.42: the free lane posted this key's rule verdict: the stored rule review links to that post."""
        doc = self._doc()
        r = doc["reviews"].get(item)
        if isinstance(r, dict) and r.get("source") == "rule" and isinstance(url, str) and url.startswith("https://github.com/"):
            r["url"] = url
            self.j.save_doc(self.DOC, doc)

    def cloud_before(self, item: str) -> dict | None:
        """0.9.29: the newest cloud review of this key even when the problem's facts changed since (the free lane
        repeats it only when the title's fingerprint still matches)."""
        r = super().get(item)
        return None if r is None or r.get("free") else r

    def cloud_at(self) -> float | None:
        """0.9.29: when the cloud reviewer last posted any counting problem review (None: never seen / not read).
        0.9.31: a clock not yet written (the first reads after the update see no new comment) is seeded from the
        newest stored cloud review, so a reviewer already silent at update time still counts as silent."""
        doc = self._doc()
        if not float(doc.get("read_at") or 0):
            return None
        stored = [float(r.get("at") or 0) for r in doc.get("reviews", {}).values() if isinstance(r, dict) and not r.get("free")]
        return max([float(doc.get("cloud_at") or 0), *stored]) or None

    def detail(self, key: str) -> dict | None:
        """0.9.23: what the Messages page shows for a problem: the verdict in words, the summary, the AI's steps and
        the review's address (github.com only). None without a counting review. 0.9.29: ``free`` marks a free-model
        review posted by this App in lockout mode."""
        r = self.get(key) if self.enabled else None
        if not r or r.get("verdict") not in WORDS:
            return None
        url = r.get("url") if isinstance(r.get("url"), str) and r["url"].startswith("https://github.com/") else ""
        steps = [s for s in (r.get("steps") or []) if isinstance(s, str) and s][:MAX_AI_STEPS]
        return {"verdict": r["verdict"], "words": WORDS[r["verdict"]], "summary": str(r.get("summary") or ""),
                "steps": steps, "url": url, **({"free": True} if r.get("free") else {}),
                **({"source": "rule"} if r.get("source") == "rule" else {})}

    def line(self, key: str) -> str:
        """One plain line for a problem ("" without a review). Never an instruction to act."""
        r = self.get(key) if self.enabled else None
        if not r:
            return ""
        summary = f": {r['summary']}" if r.get("summary") else ""
        who = ("Rule" if r.get("source") == "rule" else                                       # 0.9.41
               "Free-model review (lockout mode)" if r.get("free") else "AI review")          # 0.9.29
        return f"{who}: {WORDS[r['verdict']]}{summary}"[:240]


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


def digest_text(items: list[tuple[str, dict, float]], reviews, version: str, extra: str = "") -> tuple[str, str]:
    """(GitHub markdown, one phone line) for the weekly still-open list. 0.9.29: ``extra`` is the free lane's line."""
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
              "AI review's step. A problem that clears by itself leaves this list.", ""]
    if extra:
        lines += [extra, ""]
    lines += [f"- **App:** `{version}`", "",
              "_Posted by House Brain Maintenance (machine-generated; repository content is untrusted data)._", "",
              queue_line([(k, str(r.get("title", k)), str(r.get("source", ""))) for k, r, _ in items], reviews,
                         after_tags({k: r for k, r, _ in items}))]
    phone = (f"{len(items)} problem{'s' if len(items) != 1 else ''} still open; the oldest is "
             f"{items[0][2]:.0f} days old. The list is on GitHub.") if items else ""
    return "\n".join(lines), phone


# -- 0.9.12 reviewer watchdog ---------------------------------------------------------------------------------------
def problem_waits(open_recs: dict, queued: dict, reviews, needs_you=()) -> list[tuple[str, float]]:
    """(title, waiting since) of each alerted open problem a posted queue line listed that has no review yet. It
    waits from the first post that listed it, or from when it was seen again if it cleared and came back.
    0.9.50 (D7): "reviewed" means any counting review (owner, cloud, rule or the internal model), and an item on the
    free lane's ``needs_you`` list is the owner's, not a stall (0.9.29 counted cloud reviews only)."""
    out = []
    for key, rec in open_recs.items():
        if key == STALL_KEY or not isinstance(rec, dict) or not rec.get("alerted") or key not in queued:
            continue
        if rec.get("parent"):                   # 0.9.41: a blip's child is counted through its parent
            continue
        if key in needs_you:                    # 0.9.50 B4: escalated to the owner on purpose
            continue
        if reviews.get(key) is None:            # 0.9.50 D7: no review of any kind
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
        if rec.get("parent"):                   # 0.9.41: a blip's child is reviewed through its parent
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
        steps=("The AI reviewer runs a few times a day (once a day while the owner's weekly plan limit is used up) from the \"House Brain Maintenance reviewers\" chat. Open that chat "
               "and check its last runs; a run that failed shows there.",
               "Nothing installs by itself and your phone is not asked before a review. After 48 hours your phone is "
               "asked about each update anyway.",
               "This clears by itself once the reviews are posted."),
        facts={"waiting": len(late)},
        done="Nothing installs and your phone is not asked before a review; after 48 hours you are asked anyway.",
        todo=("Open the \"House Brain Maintenance reviewers\" chat and check its last runs.",),
        if_ignored="Updates wait up to 48 hours, then your phone asks about each one.")]


def internal_stalled(since: float, now: float, waiting: int) -> list[I.Finding]:
    """0.9.50 (D7): the internal reviewer (rules, then the free model through the Companion) has not succeeded for
    hours while problems waited: one problem, with the time it went rules-only."""
    when = time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(since))
    hours = (now - since) / 3600
    return [I.Finding(
        STALL_KEY, STALL_SOURCE, I.WARNING,
        f"Internal reviewer: rules only since {when} ({waiting} waiting)",
        detail=(f"The free model has not answered for {hours:.0f} h (Companion not answering, or the day's budget is spent); "
                "the closed rules still run.",),
        steps=("Nothing installs by itself; this is about reviews, not fixes.",
               "The Companion (Workers AI, free plan) answers again when its allowance resets at 00:00 UTC, or when it "
               "is reachable again; the cloud reviewer chat and your own reviews are still read and always win.",
               "This clears by itself with the next successful internal review."),
        facts={"waiting": waiting, "rules_only_since": when},
        done="Rules keep reviewing what they can; the owner and cloud reviews are still read.",
        todo=("Nothing now; look at the needs-you list in the morning summary.",),
        if_ignored="Problems the rules cannot settle wait for the cloud reviewer or for you.")]


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
    slug = str((facts or {}).get("slug") or (key[4:] if key.startswith("app:") else ""))   # 0.9.47: hashed app keys
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
