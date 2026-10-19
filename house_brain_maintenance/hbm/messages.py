"""0.9.18 Maintenance messages (owner 2026-10-18: "I want all the messages that I get somehow available for me to view
within Home Assistant once I click them on my pop-up ... with the solution that the AI took, or with the solution that
I need to take"; owner pop-ups 2026-10-18 "Messages list on Maintenance page", "Home Assistant bell", "'Handled' button
on the push", "Quiet hours", "Repeat unhandled critical", "Sunday line: open messages"; Work Board #942 claim
6251789151).

* Every push this App sends is first kept as a message: what happened, what was already done, what the owner needs to
  do (or nothing) and what happens if nothing is done. At most ``MAX_MESSAGES`` for ``KEEP_DAYS``, scrubbed.
* Each push has its own tag (``hbm-msg-<id>``), so a new push never replaces an older one on the phone, a thread
  ``group`` per area, and a tap opens the Maintenance page, whose Messages list shows it.
* An open problem is also one Home Assistant notification (the bell, ``hbm_msg_<id>``), dismissed when it clears or
  is marked handled; at most ``BELL_MAX`` at a time.
* "Handled" (the push button or the page button) only marks the message. It never switches anything.
* Quiet hours hold warning pushes until the morning (one "while you slept" push); CRITICAL always comes through.
* An unhandled CRITICAL problem is pushed again after ``REPEAT_HOURS``, at most ``MAX_REPEATS`` times.

Only the poll loop writes the message file; the page and the push listener queue their "handled" requests.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time

from . import issues as I

DOC = "messages"
KEEP_DAYS = 90
MAX_MESSAGES = 500
BELL_MAX = 20
REPEAT_HOURS = 4
MAX_REPEATS = 3
PUSH_CHARS = 600
RE_ID = re.compile(r"[a-f0-9]{12}")
RE_HANDLED_ACTION = re.compile(r"HBM_MSG_HANDLED_([a-f0-9]{12})")
BELL_PREFIX = "hbm_msg_"
OPEN, HANDLED, RESOLVED, INFO = "open", "handled", "resolved", "info"
GROUPS = {"problem": "maintenance-problems", "ai": "maintenance-ai", "summary": "maintenance-summary",
          "result": "maintenance-results", "update": "maintenance-updates"}
SEVERITIES = ("critical", "warning", "info")
MAX_TEXT = 600
MAX_REFS = 12                       # 0.9.23: problems a summary message links to on the page
RE_REF = re.compile(r"[a-z_]{1,24}:[A-Za-z0-9_.:@/+-]{1,200}")


def _clean(text, limit: int = MAX_TEXT) -> str:
    return I.scrub_message(str(text or ""), limit).strip()


def parse_quiet(spec: str) -> tuple[int, int] | None:
    """``"22-7"`` -> (22, 7); "" or "off" -> None (no quiet hours). Anything else is refused by the options check."""
    spec = (spec or "").strip().lower()
    if spec in ("", "off"):
        return None
    m = re.fullmatch(r"([01]?\d|2[0-3])-([01]?\d|2[0-3])", spec)
    if not m or int(m.group(1)) == int(m.group(2)):
        raise ValueError("quiet_hours")
    return int(m.group(1)), int(m.group(2))


def in_quiet(quiet: tuple[int, int] | None, hour: int) -> bool:
    if quiet is None:
        return False
    start, end = quiet
    return start <= hour < end if start < end else (hour >= start or hour < end)


class Messages:
    """The kept messages. ``add``/``resolve``/``apply_handled`` run on the poll loop only (one writer)."""

    def __init__(self, journal, clock=time.time) -> None:
        self.j = journal
        self.clock = clock
        self._lock = threading.Lock()
        self._requests: list[str] = []          # "handled" requests from the page / push listener threads
        self.nonce = secrets.token_hex(12)      # the page button's single-use code
        self.owner = None                       # the owner's user id lookup (set by the Service)
        self._view: list[dict] = list(self._doc()["items"])

    # -- storage ------------------------------------------------------------------------------------------
    def _doc(self) -> dict:
        doc = self.j.load_doc(DOC, {})
        doc = doc if isinstance(doc, dict) else {}
        items = doc.get("items") if isinstance(doc.get("items"), list) else []
        bell = doc.get("bell") if isinstance(doc.get("bell"), list) else []
        return {"items": [m for m in items if isinstance(m, dict) and RE_ID.fullmatch(str(m.get("id", "")))],
                "bell": [b for b in bell if isinstance(b, str) and RE_ID.fullmatch(b)]}

    def _save(self, doc: dict) -> None:
        now = self.clock()
        items = [m for m in doc["items"] if now - float(m.get("at") or 0) < KEEP_DAYS * 86400]
        if len(items) > MAX_MESSAGES:            # drop the oldest closed ones first, open ones only if still too many
            closed = [m for m in items if m.get("state") != OPEN]
            drop = {id(m) for m in sorted(closed, key=lambda m: float(m.get("at") or 0))[:len(items) - MAX_MESSAGES]}
            items = [m for m in items if id(m) not in drop][-MAX_MESSAGES:]
        doc["items"] = items
        self.j.save_doc(DOC, doc)
        with self._lock:
            self._view = [dict(m) for m in items]

    # -- writes (poll loop) ---------------------------------------------------------------------------------
    def add(self, kind: str, title: str, *, happened: str = "", done: str = "", todo=(), if_ignored: str = "",
            severity: str = "info", key: str = "", link: str = "", detail=(), refs=()) -> dict:
        """Keep one message. A problem (``key``) that is still open is updated, never kept twice."""
        now = self.clock()
        doc = self._doc()
        if key:
            for m in reversed(doc["items"]):
                if m.get("key") == key and m.get("state") == OPEN:
                    m.update(title=_clean(title, 160), done=_clean(done) or m.get("done", ""),
                             todo=[_clean(s) for s in todo][:6], if_ignored=_clean(if_ignored),
                             detail=[_clean(d, 300) for d in detail][:3], seen_at=now)
                    if _https(link):
                        m["link"] = link
                    self._save(doc)
                    return dict(m)
        seed = f"{key or kind}|{title}|{now}|{secrets.token_hex(4)}"
        msg = {"id": hashlib.sha256(seed.encode()).hexdigest()[:12], "at": now, "kind": kind if kind in GROUPS
               else "result", "severity": severity if severity in SEVERITIES else "info", "key": key[:120],
               "title": _clean(title, 160), "happened": _clean(happened), "done": _clean(done),
               "todo": [_clean(s) for s in todo][:6], "if_ignored": _clean(if_ignored),
               "detail": [_clean(d, 300) for d in detail][:3],
               "link": link if _https(link) else "",
               "refs": [r for r in refs if isinstance(r, str) and RE_REF.fullmatch(r)][:MAX_REFS],
               "state": OPEN if key else INFO, "pushes": 0, "pushed_at": 0, "held": False}
        doc["items"].append(msg)
        self._save(doc)
        return dict(msg)

    def update(self, msg_id: str, **fields) -> None:
        doc = self._doc()
        for m in doc["items"]:
            if m.get("id") == msg_id:
                m.update(fields)
        self._save(doc)

    def note_fix(self, key: str, text: str) -> None:
        """0.9.23: the result of a fix run for problem ``key`` ("Fixed: …" / "The fix did not help: …") on its newest
        message, so the page shows what happened where the owner tapped "Fix it"."""
        doc = self._doc()
        for m in reversed(doc["items"]):
            if m.get("key") == key:
                m["fix_result"] = _clean(text, 300)
                self._save(doc)
                return

    def resolve(self, key: str) -> list[dict]:
        """The problem ``key`` cleared: its open message is marked resolved (no new push)."""
        doc, now, out = self._doc(), self.clock(), []
        for m in doc["items"]:
            if m.get("key") == key and m.get("state") == OPEN:
                m.update(state=RESOLVED, resolved_at=now, held=False)
                out.append(dict(m))
        if out:
            self._save(doc)
        return out

    def apply_handled(self) -> list[str]:
        """Apply the queued "handled" requests (owner already checked). Returns the ids marked."""
        with self._lock:
            ids, self._requests = self._requests, []
        if not ids:
            return []
        doc, now, done = self._doc(), self.clock(), []
        for m in doc["items"]:
            if m.get("id") in ids and m.get("state") in (OPEN, INFO):
                m.update(state=HANDLED, handled_at=now, held=False)
                done.append(m["id"])
        if done:
            self._save(doc)
        return done

    # -- requests (page / listener threads) ---------------------------------------------------------------
    def queue_handled(self, msg_id: str) -> bool:
        if not RE_ID.fullmatch(msg_id or ""):
            return False
        with self._lock:
            if msg_id not in self._requests:
                self._requests.append(msg_id)
        return True

    def request_handled(self, nonce: str, user_id: str, owner: str | None, msg_id: str) -> int:
        """Page button: 200 queued; 403 not the owner; 409 stale code; 404 unknown message."""
        if not owner or not user_id or not secrets.compare_digest(user_id, owner):
            return 403
        with self._lock:
            if not nonce or not secrets.compare_digest(nonce, self.nonce):
                return 409
            if not RE_ID.fullmatch(msg_id or "") or not any(m.get("id") == msg_id for m in self._view):
                return 404
            self.nonce = secrets.token_hex(12)      # single use
            if msg_id not in self._requests:
                self._requests.append(msg_id)
        return 200

    # -- reads --------------------------------------------------------------------------------------------
    def items(self) -> list[dict]:
        """Newest first (what the page shows)."""
        with self._lock:
            return [dict(m) for m in reversed(self._view)]

    def open_items(self) -> list[dict]:
        return [m for m in self.items() if m.get("state") == OPEN]

    def held(self) -> list[dict]:
        return [m for m in self.items() if m.get("held")]


def _https(link) -> bool:
    return isinstance(link, str) and link.startswith("https://")


def push_message(msg: dict) -> str:
    """The phone text: what happened, what was done, what you do (first step), what doing nothing means."""
    lines = []
    if msg.get("happened"):
        lines.append(msg["happened"])
    if msg.get("done"):
        lines.append(f"Done: {msg['done']}")
    todo = msg.get("todo") or []
    if msg.get("key") or msg.get("done") or todo:
        lines.append(f"You: {' '.join(todo[:2]) if todo else I.NOTHING_TO_DO}")   # the first two steps, as before
    if msg.get("if_ignored"):
        lines.append(f"If you do nothing: {msg['if_ignored']}")
    if msg.get("link"):
        lines.append(msg["link"])
    return "\n".join(lines)[:PUSH_CHARS]


class Messenger:
    """Keeps, pushes, holds (quiet hours), repeats (unhandled CRITICAL) and mirrors open problems to the bell."""

    def __init__(self, ha, store: Messages, notify_service: str, open_url=None, quiet: tuple[int, int] | None = None,
                 clock=time.time, localtime=time.localtime) -> None:
        self.ha, self.store, self.notify_service = ha, store, notify_service
        self.open_url = open_url
        self.quiet = quiet
        self.clock, self.localtime = clock, localtime

    def quiet_now(self) -> bool:
        return in_quiet(self.quiet, self.localtime(self.clock()).tm_hour)

    def _url(self) -> str | None:
        try:
            return self.open_url() if callable(self.open_url) else self.open_url
        except Exception:  # noqa: BLE001 - the push still works without the tap link
            return None

    def payload(self, msg: dict, repeat: int = 0) -> dict:
        data: dict = {"tag": f"hbm-msg-{msg['id']}", "group": GROUPS.get(msg.get("kind"), GROUPS["result"])}
        url = self._url()
        if url:
            data.update(url=url, clickAction=url)            # a tap opens the Messages list (never decides)
        if msg.get("key"):
            data["actions"] = [{"action": f"HBM_MSG_HANDLED_{msg['id']}", "title": "Handled"}]
        if msg.get("severity") == "critical":
            data["push"] = {"interruption-level": "time-sensitive"}
        title = msg["title"] if not repeat else f"Still open: {msg['title']}"
        body = push_message(msg) or "Open the Maintenance page for the full message."
        return {"title": title[:120], "message": (body + "\nTap to open your messages.")[:900], "data": data}

    def send(self, msg: dict, *, critical: bool = False) -> bool:
        """Push one kept message (held instead during quiet hours unless CRITICAL). True when pushed."""
        critical = critical or msg.get("severity") == "critical"
        if self.quiet_now() and not critical:
            self.store.update(msg["id"], held=True)
            return False
        return self._push(msg)

    def _push(self, msg: dict, repeat: int = 0) -> bool:
        try:
            self.ha.notify(self.notify_service, self.payload(msg, repeat))
        except Exception:  # noqa: BLE001 - the message is kept; the page and the bell still show it
            return False
        self.store.update(msg["id"], pushes=int(msg.get("pushes") or 0) + 1, pushed_at=self.clock(), held=False,
                          repeats=repeat or int(msg.get("repeats") or 0))
        return True

    def flush_held(self) -> int:
        """After quiet hours: one push for everything held overnight (each one stays on the page)."""
        held = [m for m in self.store.held() if m.get("state") in (OPEN, INFO)]
        if not held or self.quiet_now():
            return 0
        titles = "; ".join(m["title"][:50] for m in held[:3]) + (f" and {len(held) - 3} more" if len(held) > 3 else "")
        url = self._url()
        data: dict = {"tag": f"hbm-msg-held-{int(self.clock())}", "group": GROUPS["summary"]}
        if url:
            data.update(url=url, clickAction=url)
        try:
            self.ha.notify(self.notify_service, {"title": f"Maintenance: {len(held)} message(s) while you slept"[:120],
                                                 "message": f"{titles}\nTap to open your messages."[:400],
                                                 "data": data})
        except Exception:  # noqa: BLE001 - kept held; tried again on the next check
            return 0
        for m in held:
            self.store.update(m["id"], held=False, pushes=int(m.get("pushes") or 0) + 1, pushed_at=self.clock())
        return len(held)

    def repeat_critical(self) -> int:
        """An unhandled CRITICAL problem is pushed again after REPEAT_HOURS, at most MAX_REPEATS times."""
        now, sent = self.clock(), 0
        for m in self.store.open_items():
            if m.get("severity") != "critical" or not m.get("pushes") or int(m.get("repeats") or 0) >= MAX_REPEATS:
                continue
            if now - float(m.get("pushed_at") or 0) >= REPEAT_HOURS * 3600:
                sent += self._push(m, repeat=int(m.get("repeats") or 0) + 1)
        return sent

    def sync_bell(self) -> None:
        """Open problems (newest BELL_MAX) are Home Assistant notifications; the rest are dismissed."""
        doc = self.store._doc()
        want = [m for m in reversed(doc["items"]) if m.get("state") == OPEN][:BELL_MAX]
        want_ids = {m["id"] for m in want}
        shown = list(doc["bell"])
        changed = False
        for msg_id in [b for b in shown if b not in want_ids]:
            try:
                self.ha.persistent_dismiss(BELL_PREFIX + msg_id)
            except Exception:  # noqa: BLE001 - tried again on the next check
                continue
            shown.remove(msg_id)
            changed = True
        for m in want:
            if m["id"] in shown:
                continue
            text = [push_message(m) or m["title"]]
            if m.get("detail"):
                text.append(" ".join(m["detail"]))
            text.append("Your full message list is on the House Brain Maintenance page.")
            try:
                self.ha.persistent_note(BELL_PREFIX + m["id"], m["title"], "\n\n".join(text))
            except Exception:  # noqa: BLE001 - tried again on the next check
                continue
            shown.append(m["id"])
            changed = True
        if changed:
            doc = self.store._doc()
            doc["bell"] = shown
            self.store._save(doc)


class HandledListener:
    """Listens for the push button "Handled" (``HBM_MSG_HANDLED_<id>``) from the owner only and queues it. It never
    switches anything; the poll loop marks the message."""

    def __init__(self, ha, store: Messages, owner, should_stop=lambda: False, retry: float = 30.0) -> None:
        self.ha, self.store, self.owner, self.should_stop, self.retry = ha, store, owner, should_stop, retry
        self.thread = threading.Thread(target=self.run, daemon=True, name="hbm-handled")

    def start(self) -> None:
        self.thread.start()

    def handle(self, event: dict) -> bool:
        data = event.get("data") if isinstance(event, dict) else None
        action = data.get("action") if isinstance(data, dict) else None
        m = RE_HANDLED_ACTION.fullmatch(action) if isinstance(action, str) else None
        if not m:
            return False
        try:
            owner = self.owner() if callable(self.owner) else self.owner
        except Exception:  # noqa: BLE001 - unknown owner: nobody may mark
            owner = None
        user = (event.get("context") or {}).get("user_id")
        if not owner or user != owner:
            return False
        return self.store.queue_handled(m.group(1))

    def run(self) -> None:
        from .ha import APPROVAL_EVENT
        while not self.should_stop():
            sock = None
            try:
                sock = self.ha.ws()
                sock.command({"type": "subscribe_events", "event_type": APPROVAL_EVENT})
                while not self.should_stop():
                    event = sock.next_event(1.0)
                    if event:
                        self.handle(event)
            except Exception:  # noqa: BLE001 - reconnect after a pause (Core restart, lost socket)
                pass
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:  # noqa: BLE001
                        pass
            end = time.monotonic() + self.retry
            while not self.should_stop() and time.monotonic() < end:
                time.sleep(0.5)
