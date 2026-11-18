"""0.9.61 Never lose a result (deep-dive review R1 slice S1, owner pop-ups 2026-11-16; record
``docs/validation/HB454_MAINTENANCE_DEEP_DIVE_R1_2026-11-16.md`` A1, A2).

Until 0.9.60 every post on the tracking issue was sent once: a failed post was written to the audit file on the Pi
and never sent again, so a result could be in the ledger with nothing on GitHub (the ESPHome switch of 2026-11-09 and
the OS 18.5 download of 2026-11-15 left no result post). Now every post goes through this outbox:

* the post is tried at once, exactly as before; when GitHub accepts it nothing else happens;
* when it fails, for any reason, the finished text is kept in ``/data/outbox.json`` ("owed") and sent again on later
  polls: at most ``MAX_PER_POLL`` a poll, oldest first, waiting ``BACKOFF`` between tries (the last wait repeats). It
  never gives up and never throws a post away; it is removed from the outbox only once GitHub has it;
* every post carries a hidden ``<!-- hbm:post:<id> -->`` at the end of its heading line: line 1 still starts with the
  heading, line 2 stays the machine status line, nothing follows the last machine block, and GitHub's length limit
  never cuts it. When a failure was unclear
  (no answer, or a GitHub server error: the post may have arrived), the next try first reads the issue's comments
  since the post was written and does not send it again when its line is there. At most ``CHECKS_PER_DAY`` such reads
  a day; past that the post is sent without the check (a duplicate is better than a lost result);
* a post sent late says so in one plain line at its end (no ``-->`` in it), with the time it was written.

$0: no new post in normal running (a retry replaces a post that failed), at most ``CHECKS_PER_DAY`` reads a day.
"""
from __future__ import annotations

import hashlib
import re
import time

from . import net

DOC = "outbox"
MARKER = "<!-- hbm:post:{} -->"
RE_MARKER = re.compile(r"<!-- hbm:post:([0-9A-Za-z-]{1,40}) -->")
BACKOFF = (300.0, 1800.0, 7200.0, 21600.0)     # 5 min, 30 min, 2 h, then every 6 h
MAX_PER_POLL = 3
CHECKS_PER_DAY = 10
CHECK_PAGES = 3                                # 100 comments a page; a post is looked for in the first 300 since it
ALERT_AFTER = 6 * 3600.0                       # the owner hears once a day when a post is owed this long
RE_COMMENT_URL = re.compile(r"https://github\.com/[^\s#]+/issues/\d+#issuecomment-\d+")


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(max(0.0, epoch)))


def _day(epoch: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(epoch))


def unclear(err: BaseException) -> bool:
    """True when the post may have reached GitHub although the call failed (no answer, or a server error)."""
    status = getattr(err, "status", None)
    return not isinstance(status, int) or status == 0 or status >= 500


def mark(body: str, pid: str, made: float | None = None) -> str:
    """The post as sent: the hidden id at the end of the heading line, and a "sent late" line at the end when
    ``made`` is given."""
    head, sep, rest = body.partition("\n")
    late = f"\n\n> Sent late: written {_iso(made)} UTC; the first try to post it failed." if made is not None else ""
    return f"{head} {MARKER.format(pid)}{sep}{rest}{late}"


class Outbox:
    def __init__(self, journal, gh, clock=time.time, on_delivered=None) -> None:
        self.j, self.gh, self.clock = journal, gh, clock
        self.on_delivered = on_delivered          # (source, url) once an owed post is on GitHub
        self.kept = False                         # True when the last post() failed and was kept

    # -- state -------------------------------------------------------------------------------------------------
    def _doc(self) -> dict:
        doc = self.j.load_doc(DOC, {})
        doc = doc if isinstance(doc, dict) else {}
        items = doc.get("items") if isinstance(doc.get("items"), list) else []
        doc["items"] = [i for i in items if isinstance(i, dict) and i.get("id") and i.get("body")]
        doc.setdefault("delivered", 0)
        doc.setdefault("checks", {"day": "", "n": 0})
        doc.setdefault("alerted", "")
        return doc

    def _save(self, doc: dict) -> None:
        self.j.save_doc(DOC, doc)

    def _new_id(self, body: str, now: float) -> str:
        digest = hashlib.sha256(f"{now!r}\n{body}".encode("utf-8", "replace")).hexdigest()[:10]
        return f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime(now))}-{digest}"

    # -- posting -----------------------------------------------------------------------------------------------
    def post(self, issue: int, body: str, source: str = "") -> str:
        """Post now; on any failure keep it owed. Returns the comment's address, "" when unknown or owed."""
        now = self.clock()
        pid = self._new_id(body, now)
        self.kept = False
        try:
            url = self.gh.comment(int(issue), mark(body, pid)) or ""
        except Exception as err:  # noqa: BLE001 - whatever failed, the post is kept and sent again
            doc = self._doc()
            doc["items"].append({"id": pid, "issue": int(issue), "body": body, "source": str(source)[:200],
                                 "made": now, "tries": 1, "next": now + BACKOFF[0], "unclear": unclear(err),
                                 "error": net.redact(f"{type(err).__name__}: {err}")[:200]})
            self._save(doc)
            self.kept = True
            self.j.audit(str(source) or "outbox", "POST_OWED", post=pid, issue=int(issue),
                         error=net.redact(str(err))[:200])
            return ""
        return url if isinstance(url, str) else ""

    def _found(self, item: dict, doc: dict) -> str | None:
        """The address of the comment that already carries this post's id line, "" when it is not there, None when
        it could not be checked (no reads left today, or the read failed)."""
        now = self.clock()
        checks = doc["checks"]
        if checks.get("day") != _day(now):
            checks.update(day=_day(now), n=0)
        for page in range(1, CHECK_PAGES + 1):
            if int(checks.get("n", 0)) >= CHECKS_PER_DAY:
                return None
            checks["n"] = int(checks.get("n", 0)) + 1
            try:
                comments = self.gh.issue_comments(int(item["issue"]), _iso(float(item["made"]) - 120.0), page)
            except Exception:  # noqa: BLE001 - not checked; sent again, a duplicate is better than a loss
                return None
            for c in comments:
                body = c.get("body") if isinstance(c, dict) else None
                if isinstance(body, str) and MARKER.format(item["id"]) in body:
                    url = c.get("html_url")
                    return url if isinstance(url, str) and RE_COMMENT_URL.fullmatch(url) else "found"
            if len(comments) < 100:
                return ""
        return ""

    def drain(self) -> int:
        """Send owed posts that are due. Returns how many reached GitHub."""
        doc = self._doc()
        now = self.clock()
        due = sorted((i for i in doc["items"] if float(i.get("next", 0)) <= now), key=lambda i: float(i.get("made", 0)))
        sent = 0
        for item in due[:MAX_PER_POLL]:
            url = None
            if item.get("unclear"):
                found = self._found(item, doc)
                if found:
                    url = found
            if url is None:
                try:
                    url = self.gh.comment(int(item["issue"]), mark(item["body"], item["id"], float(item["made"]))) or ""
                except Exception as err:  # noqa: BLE001 - kept; tried again after the next wait
                    tries = int(item.get("tries", 1)) + 1
                    item.update(tries=tries, next=now + BACKOFF[min(tries - 1, len(BACKOFF) - 1)],
                                unclear=unclear(err), error=net.redact(f"{type(err).__name__}: {err}")[:200])
                    self.j.audit(item.get("source") or "outbox", "POST_STILL_OWED", post=item["id"], tries=tries,
                                 error=net.redact(str(err))[:200])
                    break                                   # GitHub is failing: the rest wait for the next poll
            doc["items"] = [i for i in doc["items"] if i["id"] != item["id"]]
            doc["delivered"] = int(doc.get("delivered", 0)) + 1
            sent += 1
            link = url if isinstance(url, str) and RE_COMMENT_URL.fullmatch(url) else ""
            self.j.audit(item.get("source") or "outbox", "POST_DELIVERED", post=item["id"], tries=int(item.get("tries", 1)),
                         late_minutes=round((now - float(item.get("made", now))) / 60.0, 1), url=link)
            if self.on_delivered is not None:
                try:
                    self.on_delivered(str(item.get("source") or ""), link)
                except Exception:  # noqa: BLE001 - the post is on GitHub; only the ledger link waits
                    pass
        self._save(doc)
        return sent

    # -- what the owner and the AIs see -------------------------------------------------------------------------
    def status(self) -> dict:
        doc = self._doc()
        now = self.clock()
        oldest = min((float(i.get("made", now)) for i in doc["items"]), default=None)
        return {"owed": len(doc["items"]), "oldest_hours": round((now - oldest) / 3600.0, 1) if oldest else 0.0,
                "delivered_late": int(doc.get("delivered", 0))}

    def alert_due(self) -> bool:
        """True once a day while a post has been owed longer than ``ALERT_AFTER`` (GitHub posting is broken)."""
        doc = self._doc()
        now = self.clock()
        oldest = min((float(i.get("made", now)) for i in doc["items"]), default=None)
        if oldest is None or now - oldest < ALERT_AFTER or doc.get("alerted") == _day(now):
            return False
        doc["alerted"] = _day(now)
        self._save(doc)
        return True
