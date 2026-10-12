"""0.9.0 Approve from the page: one decision inbox for everything this App asks the owner (owner pop-ups 2026-10-12:
"Yes, build it", scope "All updates", and "It should let me update everything that goes through the maintenance app
that's not approved automatically").

Before 0.9.0 a missed phone ask could only be asked again: "Ask me again" queued it for the next check (the App
sleeps ``poll_seconds``, 5 min by default, and waits behind any other open ask), it counted against
``max_approval_requests_per_day`` and needed a second push. Now:

* every ask that ends without an answer (timeout, App restart) and can be resumed is listed on the Maintenance page
  ("Needs your decision"), together with every waiting update this App offers but has not asked yet;
* the owner taps Approve or Decline there, confirms once, and the decision is stored here;
* ``approval.ask`` answers the next ask for that exact item from this inbox at once (no push, not counted against
  the daily ask limit), and the engines skip their re-ask wait for an item the owner decided, so the job runs on the
  next poll, which the page wakes at once;
* an open phone ask for the same item is answered by the page decision too.

The item is the stage plus the request id (``Request: <id>`` in every ask; the id names the exact version), so a
decision can never approve a different version. A decision lapses after ``DECISION_TTL`` if the item stops being
asked (for example the update disappeared). One-shot requests filed by a chat (stage ``JOB``/``STAGE``) cannot be
resumed after a timeout; the page lists them without buttons.

Trust: the page rules are unchanged (Supervisor ingress peer, the Supervisor-inserted owner user id, POST only,
single-use codes, a confirm step); ``CONTROL_AUTHORITY=NONE``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time

RE_REQUEST = re.compile(r"(?m)^Request: ([A-Za-z0-9][A-Za-z0-9._:@/-]{0,119})\s*$")   # fix ids: fix-app:<slug>
RESUMABLE = frozenset({"UPDATE", "SYSTEM", "OSBOOT", "OSBACK", "PIFW", "FIX", "AFTER", "MIGRATE", "MIGRATE_REMOVE",
                       "KEYS", "SCOUTMOVE"})
DECISION_TTL = 24 * 3600.0
MISSED_TTL = 7 * 86400.0
CONFIRM_TTL = 600.0
MAX_ITEMS = 30
STATE_FILE = "inbox.json"
CHOICES = ("approve", "reject")


def key_of(stage: str, message: str, title: str = "") -> str:
    """``<stage>:<request id>|<question>``: the request id names the exact version; the question part keeps two
    different asks of one request apart (the after-update check asks "reload?" and then "undo?")."""
    m = RE_REQUEST.search(message or "")
    question = hashlib.sha256((title or "").encode()).hexdigest()[:8]
    return f"{stage}:{m.group(1) if m else ''}|{question}"


class Inbox:
    def __init__(self, data_dir: str | None, owner, wake=None, clock=time.time) -> None:
        self.path = os.path.join(data_dir, STATE_FILE) if data_dir else None
        self._owner = owner                     # () -> the owner's Home Assistant user id ("" while unknown)
        self._wake = wake                       # () -> None: wake the App's poll loop
        self.clock = clock
        self._lock = threading.Lock()
        self._missed: dict[str, dict] = {}
        self._decisions: dict[str, dict] = {}
        self._offered: dict[str, dict] = {}
        self._confirm: dict[str, dict] = {}
        self.nonce = secrets.token_hex(12)
        self._load()

    # ------------------------------------------------------------------ persistence (decisions survive a restart)
    def _load(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            return
        if isinstance(doc, dict):
            self._missed = {k: v for k, v in (doc.get("missed") or {}).items() if isinstance(v, dict)}
            self._decisions = {k: v for k, v in (doc.get("decisions") or {}).items() if isinstance(v, dict)}

    def _save(self) -> None:
        if not self.path:
            return
        try:
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), prefix=".inbox.")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"missed": self._missed, "decisions": self._decisions}, fh)
            os.replace(tmp, self.path)
        except OSError:                                  # the inbox stays in memory; never break an ask
            pass

    def _expire(self) -> None:
        now = self.clock()
        self._missed = {k: v for k, v in self._missed.items() if now - float(v.get("at") or 0) < MISSED_TTL}
        self._decisions = {k: v for k, v in self._decisions.items() if now - float(v.get("at") or 0) < DECISION_TTL}
        self._confirm = {k: v for k, v in self._confirm.items() if now < v["expires"]}

    # ------------------------------------------------------------------ the ask side (approval.ask)
    def take(self, key: str) -> str | None:
        """The owner's page decision for this item ("approve"/"reject"), consumed; None when there is none."""
        with self._lock:
            self._expire()
            d = self._decisions.pop(key, None)
            if d is None:                               # an update decided before it was ever asked
                d = self._decisions.pop(key.split("|", 1)[0] + "|*", None)
            if d is None:
                return None
            self._missed.pop(key, None)
            self._save()
            return d["choice"]

    def has_decisions(self) -> bool:
        with self._lock:
            self._expire()
            return bool(self._decisions)

    def decided(self, prefix: str) -> bool:
        """True when the owner decided an item whose key starts with ``<stage>:<request id>|``."""
        with self._lock:
            self._expire()
            return any(k.startswith(prefix) for k in self._decisions)

    def missed(self, key: str, stage: str, title: str, message: str) -> None:
        with self._lock:
            self._missed[key] = {"stage": stage, "title": title[:120], "message": message[:900], "at": self.clock(),
                                 "resumable": stage in RESUMABLE}
            for k in sorted(self._missed, key=lambda k: self._missed[k]["at"])[:-MAX_ITEMS]:
                del self._missed[k]
            self._save()

    def answered(self, key: str) -> None:
        with self._lock:
            if self._missed.pop(key, None) is not None:
                self._save()

    # ------------------------------------------------------------------ what the page shows
    def set_offered(self, rows: list[dict]) -> None:
        """Waiting updates this App offers (from the engines' queues): {key, stage, title, detail, ai, ai_url}.
        Their key is ``<stage>:<request id>|*`` (any question about that exact request)."""
        with self._lock:
            self._offered = {r["key"]: dict(r) for r in rows[:MAX_ITEMS] if r.get("key")}

    def items(self) -> list[dict]:
        """Everything that needs the owner: missed asks first (newest first), then waiting updates."""
        with self._lock:
            self._expire()
            out = []
            by_request = {k.split("|", 1)[0]: r for k, r in self._offered.items()}
            for k, m in sorted(self._missed.items(), key=lambda kv: -float(kv[1]["at"])):
                offered = by_request.get(k.split("|", 1)[0], {})
                out.append({"key": k, "stage": m["stage"], "title": m["title"], "detail": m["message"], "missed": True,
                            "resumable": m["resumable"], "ai": offered.get("ai", ""), "ai_url": offered.get("ai_url", ""),
                            "ai_verdict": offered.get("ai_verdict", "")})
            missed_requests = {k.split("|", 1)[0] for k in self._missed}
            for k, r in self._offered.items():
                if k.split("|", 1)[0] not in missed_requests:     # a missed ask of it is listed already
                    out.append({**r, "missed": False, "resumable": True})
            for row in out:
                d = self._decisions.get(row["key"])
                row["decision"] = d["choice"] if d else ""
            return out[:MAX_ITEMS]

    # ------------------------------------------------------------------ the page side (two steps)
    def _owner_ok(self, user_id: str) -> bool:
        try:
            owner = self._owner() or ""
        except Exception:  # noqa: BLE001 - unknown owner: nobody may decide
            owner = ""
        return bool(owner and user_id and secrets.compare_digest(user_id, owner))

    def confirm(self, nonce: str, user_id: str, key: str, choice: str) -> tuple[int, dict | None, str]:
        """Step 1: (status, the item, a single-use confirm code). 403 not the owner; 409 stale page code;
        404 not an item that needs the owner (or not resumable); 400 unknown choice."""
        if not self._owner_ok(user_id):
            return 403, None, ""
        if choice not in CHOICES:
            return 400, None, ""
        with self._lock:
            if not nonce or not secrets.compare_digest(nonce, self.nonce):
                return 409, None, ""
        item = next((i for i in self.items() if i["key"] == key and i["resumable"]), None)
        if item is None:
            return 404, None, ""
        code = secrets.token_hex(12)
        with self._lock:
            self._confirm[code] = {"key": key, "choice": choice, "expires": self.clock() + CONFIRM_TTL}
        return 200, item, code

    def decide(self, code: str, user_id: str) -> tuple[int, str, str]:
        """Step 2: (status, key, choice). 403 not the owner; 409 unknown/expired/used confirm code."""
        if not self._owner_ok(user_id):
            return 403, "", ""
        with self._lock:
            self._expire()
            c = self._confirm.pop(code, None) if code else None
            if c is None:
                return 409, "", ""
            self._decisions[c["key"]] = {"choice": c["choice"], "at": self.clock(), "by": "page"}
            self.nonce = secrets.token_hex(12)           # the list's buttons are single use too
            self._save()
        if self._wake is not None:
            self._wake()
        return 200, c["key"], c["choice"]
