"""0.8.2: Cloudflare control approvals on the owner's phone (owner pop-up 2026-10-11).

The Cloudflare control Worker posts each request's plan and result on GitHub issue #454, with one machine line
``<!-- house_brain_cloudflare_control_result.v1 {...} -->``, and pushes the Approve link through ntfy. The owner did
not receive those pushes, so this App reads its own issue once per poll and sends the same news as an ordinary Home
Assistant notification:

* a request waiting for the owner (``PENDING``, not past its 6 h deadline): one push with an "Open Approve page"
  button;
* the end of a request this App announced (DONE, FAILED, STALE, REJECTED, EXPIRED, REFUSED, INTERRUPTED): one push
  that replaces the first.

Trust: comment text is untrusted data. Only comments by the repository owner's account with the exact machine line
count, every field is checked to a closed shape, and the link is built here from the configured control address and
the request id; nothing in a comment can choose where the button goes. The page itself is behind Cloudflare Access
and shows the Worker's own plan; this App approves nothing and changes nothing (``CONTROL_AUTHORITY=NONE``).
Cost: one GitHub read per poll (288 a day at the default 5 minutes), $0.
"""
from __future__ import annotations

import calendar
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass

from . import net

SCHEMA = "house_brain_cloudflare_control_result.v1"
RE_MACHINE = re.compile(r"<!-- house_brain_cloudflare_control_result\.v1 (\{.*?\}) -->", re.S)
RE_ID = re.compile(r"^cf-\d{4}-\d{2}-\d{2}[a-z]$")
RE_WORKER = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
RE_CONTROL_URL = re.compile(r"^https://house-brain-cloudflare-control\.[a-z0-9-]{1,63}\.workers\.dev$")
RE_BROKER_BASE = re.compile(r"^https://house-brain-maintenance-broker\.([a-z0-9-]{1,63})\.workers\.dev$")
PENDING = "PENDING"
FINAL = ("DONE", "FAILED", "STALE", "REJECTED", "EXPIRED", "REFUSED", "INTERRUPTED")
PENDING_TTL = 6 * 3600.0          # the control Worker's own deadline (PENDING_TTL_MS)
LOOKBACK = PENDING_TTL + 3600.0   # first run: a request still waiting is announced, older news is not
KEEP = 7 * 86400.0                # remembered ids are forgotten after a week
STATE_FILE = "cfrelay.json"


def control_url_from_broker(broker_base: str) -> str:
    """The control Worker on the same Cloudflare account as the Broker (both are House Brain Workers on one
    workers.dev subdomain), or "" when the Broker address is not in the exact expected shape."""
    m = RE_BROKER_BASE.fullmatch(broker_base or "")
    return f"https://house-brain-cloudflare-control.{m.group(1)}.workers.dev" if m else ""


@dataclass
class Event:
    id: str
    state: str
    worker: str
    steps: list[str]
    created: float
    comment_at: float


def _ts(value) -> float | None:
    if not isinstance(value, str):
        return None
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?Z", value)
    if not m:
        return None
    return float(calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")))


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def parse(comment: dict, owner: str) -> Event | None:
    """One #454 comment -> a control event, or None. Closed shape; anything else is ignored."""
    if not isinstance(comment, dict):
        return None
    user = comment.get("user") or {}
    if not isinstance(user, dict) or str(user.get("login", "")).lower() != owner.lower():
        return None
    body = comment.get("body")
    if not isinstance(body, str) or not body.startswith("### Cloudflare control `"):
        return None
    found = RE_MACHINE.findall(body)
    if len(found) != 1:
        return None
    try:
        doc = json.loads(found[0])
    except ValueError:
        return None
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        return None
    rid, state, worker = doc.get("id"), doc.get("state"), doc.get("worker")
    if not (isinstance(rid, str) and RE_ID.fullmatch(rid)):
        return None
    if state != PENDING and state not in FINAL:
        return None
    if not (isinstance(worker, str) and RE_WORKER.fullmatch(worker)):
        return None
    created = _ts(doc.get("created"))
    at = _ts(comment.get("created_at"))
    if created is None or at is None:
        return None
    steps = []
    for step in doc.get("steps") or []:
        if isinstance(step, dict) and isinstance(step.get("summary"), str):
            steps.append(re.sub(r"[\x00-\x1f<>`*_\[\]]", " ", step["summary"])[:120])
    return Event(rid, state, worker, steps[:5], created, at)


class CfRelay:
    def __init__(self, gh, ha, data_dir: str, notify_service: str, issue: int, control_url: str,
                 clock=time.time) -> None:
        if not RE_CONTROL_URL.fullmatch(control_url):
            raise ValueError("cf_control_url")
        self.gh, self.ha = gh, ha
        self.path = os.path.join(data_dir, STATE_FILE)
        self.notify_service = notify_service
        self.issue = int(issue)
        self.control_url = control_url
        self.owner = gh.repo.split("/", 1)[0]
        self.clock = clock

    # ------------------------------------------------------------------ state
    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                doc = json.load(fh)
            if isinstance(doc, dict) and isinstance(doc.get("ids"), dict):
                return doc
        except (OSError, ValueError):
            pass
        return {"since": None, "ids": {}}

    def _save(self, doc: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), prefix=".cfrelay.")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ one poll
    def cycle(self) -> list[str]:
        """Read new control comments and push what the owner needs. Returns the pushes sent (for tests/logs)."""
        now = self.clock()
        doc = self._load()
        since = doc.get("since") or _iso(now - LOOKBACK)
        comments = self.gh.issue_comments(self.issue, since)
        latest: dict[str, Event] = {}
        newest = _ts(since) or now - LOOKBACK
        for c in comments if isinstance(comments, list) else []:
            at = _ts((c or {}).get("updated_at")) if isinstance(c, dict) else None
            if at is not None:
                newest = max(newest, at)
            ev = parse(c, self.owner)
            if ev is None:
                continue
            if ev.id not in latest or ev.comment_at >= latest[ev.id].comment_at:
                latest[ev.id] = ev
        sent: list[str] = []
        ids = doc["ids"]
        for ev in sorted(latest.values(), key=lambda e: e.comment_at):
            seen = ids.get(ev.id, {})
            if ev.state == PENDING:
                if seen.get("announced") or now > ev.created + PENDING_TTL:
                    continue
                self._push_pending(ev)
                ids[ev.id] = {"announced": True, "at": now}
                sent.append(f"{ev.id}:{PENDING}")
            elif seen.get("announced") and not seen.get("final"):
                self._push_final(ev)
                ids[ev.id] = {**seen, "final": ev.state, "at": now}
                sent.append(f"{ev.id}:{ev.state}")
        doc["ids"] = {k: v for k, v in ids.items() if now - float(v.get("at", now)) < KEEP}
        # GitHub's ``since`` is inclusive and second-granular: step back one second; ids already handled stay quiet.
        doc["since"] = _iso(max(newest - 1, now - LOOKBACK))
        self._save(doc)
        return sent

    def link(self, rid: str) -> str:
        return f"{self.control_url}/r/{rid}"

    def _push_pending(self, ev: Event) -> None:
        left = max(0, int((ev.created + PENDING_TTL - self.clock()) / 60))
        lines = [f"Cloudflare {ev.worker}: waiting for your Approve ({ev.id})."]
        lines += [f"{n}. {s}" for n, s in enumerate(ev.steps, 1)]
        lines.append(f"Open the page to Approve or Reject. Expires in about {left // 60} h {left % 60} min.")
        url = self.link(ev.id)
        self.ha.notify(self.notify_service, {
            "title": "Cloudflare: approve?",
            "message": "\n".join(lines)[:900],
            "data": {"tag": f"hbm-cf-{ev.id}", "url": url, "clickAction": url,
                     "push": {"interruption-level": "time-sensitive"},
                     "actions": [{"action": "URI", "title": "Open Approve page", "uri": url}]},
        })

    def _push_final(self, ev: Event) -> None:
        words = {"DONE": "done", "FAILED": "failed", "STALE": "not run (the live state changed)",
                 "REJECTED": "rejected", "EXPIRED": "expired without approval; nothing changed",
                 "REFUSED": "refused; nothing changed", "INTERRUPTED": "interrupted; a chat checks it"}
        self.ha.notify(self.notify_service, {
            "title": f"Cloudflare: {ev.state}",
            "message": f"{ev.worker} ({ev.id}): {words[ev.state]}. The full result is on GitHub #{self.issue}."[:900],
            "data": {"tag": f"hbm-cf-{ev.id}"},
        })


def safe_cycle(relay: CfRelay | None, log) -> list[str]:
    """Never lets a GitHub or notification fault stop the poll."""
    if relay is None:
        return []
    try:
        return relay.cycle()
    except Exception as err:  # noqa: BLE001 - information only; the next poll retries
        log.warning("Cloudflare approval relay waits: %s", net.redact(f"{type(err).__name__}: {err}")[:200])
        return []
