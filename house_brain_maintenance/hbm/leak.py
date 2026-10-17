"""0.9.13 "Suspected leak?" page (Credential Autopilot follow-up, owner pop-up 2026-10-07 "Both").

One owner tap queues a change of every Broker key this App changes by itself (``ROTATE_KEYS_NOW`` for all roles,
the same path an AI request uses; nothing is asked, each role keeps its own check and undo). The rest of the
house's credentials cannot be changed from here: the page lists them with their ``leak_response`` from the
repository's credential list (``contracts/house_brain_credential_inventory.v1.json``), read once a day by the poll
loop through this App's own GitHub sign-in. No secret, value or fingerprint is ever shown.

The page thread only records the request; the poll loop takes it (like "Ask me again"), so the key file is never
written from two threads.
"""
from __future__ import annotations

import html
import json
import re
import secrets
import threading
import time

INVENTORY_PATH = "contracts/house_brain_credential_inventory.v1.json"
INVENTORY_REFRESH = 86400.0          # read the list once a day (and at the first poll)
INVENTORY_RETRY = 3600.0             # after a failed read
REQUEST_TTL = 3600.0                 # a tap the poll loop has not taken within an hour lapses
MAX_ITEMS = 80
BUTTON_MARK = "ROTATE_KEYS_NOW"      # an inventory leak_response naming this is done by the button
NOTHING = ("none", "none needed", "not a long-lived credential")
RE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# Only these hosts become links; everything else stays text.
RE_LINK = re.compile(r"\b((?:github\.com|dash\.cloudflare\.com|claude\.ai)(?:/[A-Za-z0-9._~/-]*)?)")


def parse_inventory(raw: bytes) -> list[dict]:
    """The credential list as plain rows (id, kind, where, response, button). Raises ValueError when unusable."""
    doc = json.loads(raw.decode("utf-8"))
    creds = doc.get("credentials") if isinstance(doc, dict) else None
    if not isinstance(creds, list) or not creds:
        raise ValueError("no credentials list")
    rows = []
    for c in creds[:MAX_ITEMS]:
        if not isinstance(c, dict) or not isinstance(c.get("id"), str) or not RE_ID.fullmatch(c["id"]):
            continue
        response = str(c.get("leak_response") or "").strip()[:400]
        if not response or response.lower().rstrip(".") in NOTHING:
            continue
        where = str(c.get("provider") or c.get("consumer") or c.get("kind") or "")[:160]
        rows.append({"id": c["id"], "kind": str(c.get("kind") or "")[:40], "where": where, "response": response,
                     "button": BUTTON_MARK in response})
    if not rows:
        raise ValueError("no leak responses")
    return rows


def _linked(text: str) -> str:
    """Escape ``text``; known hosts (GitHub, Cloudflare dashboard, claude.ai) become https links."""
    out, last = [], 0
    for m in RE_LINK.finditer(text):
        out.append(html.escape(text[last:m.start()]))
        target = m.group(1).rstrip(".")
        out.append(f'<a href="https://{html.escape(target)}" target="_blank" rel="noopener">{html.escape(target)}</a>')
        out.append(html.escape(m.group(1)[len(target):]))
        last = m.end()
    out.append(html.escape(text[last:]))
    return "".join(out)


class LeakResponse:
    """``owner`` returns the owner's Home Assistant user id (None/"" while unknown: nobody may start it)."""

    def __init__(self, owner, roles: tuple[str, ...], role_names: dict[str, str], repo: str, issue: int | None = None,
                 clock=time.time) -> None:
        self._owner = owner
        self.issue = issue
        self.roles = tuple(roles)
        self.role_names = dict(role_names)
        self.repo = repo
        self._clock = clock
        self._lock = threading.Lock()
        self._rows: list[dict] | None = None
        self._read_at: float | None = None       # last successful read
        self._next_read = 0.0
        self._requested: dict | None = None
        self.last_started: float | None = None   # when the poll loop last queued the keys (shown on the page)
        self.keys_summary = lambda: None          # set by the service: the key rotator's summary (or None)
        self.nonce = secrets.token_hex(12)

    # -- poll loop ------------------------------------------------------------------------------------------
    def refresh_due(self) -> bool:
        return self._clock() >= self._next_read

    def refresh(self, read) -> bool:
        """``read()`` returns the inventory file's bytes. True when the list was updated."""
        now = self._clock()
        try:
            rows = parse_inventory(read())
        except Exception:  # noqa: BLE001 - keep the last good list; try again in an hour
            self._next_read = now + INVENTORY_RETRY
            return False
        with self._lock:
            self._rows, self._read_at = rows, now
        self._next_read = now + INVENTORY_REFRESH
        return True

    def take(self) -> tuple[dict | None, bool]:
        """(the owner's request or None, whether one lapsed unused)."""
        with self._lock:
            item, self._requested = self._requested, None
        if item is None:
            return None, False
        if self._clock() >= item["expires"]:
            return None, True
        return item, False

    def started(self) -> None:
        self.last_started = self._clock()

    # -- page thread ----------------------------------------------------------------------------------------
    def request(self, nonce: str, user_id: str, choice: str) -> int:
        """200 recorded; 403 not the owner; 409 stale form; 429 already waiting; 400 unknown choice."""
        if choice != "rotate":
            return 400
        try:
            owner = self._owner() or ""
        except Exception:  # noqa: BLE001 - unknown owner: nobody may start it
            owner = ""
        if not owner or not user_id or not secrets.compare_digest(user_id, owner):
            return 403
        with self._lock:
            if not nonce or not secrets.compare_digest(nonce, self.nonce):
                return 409
            now = self._clock()
            if self._requested is not None and now < self._requested["expires"]:
                return 429
            self.nonce = secrets.token_hex(12)    # single use
            self._requested = {"at": now, "expires": now + REQUEST_TTL}
            return 200

    def render(self, action: str, note: str = "") -> str:
        """The page body (the key rotator's summary decides whether the button is offered)."""
        e = html.escape
        try:
            keys = self.keys_summary()
        except Exception:  # noqa: BLE001 - visibility only: no summary = no button
            keys = None
        with self._lock:
            rows = list(self._rows) if self._rows else None
            read_at, nonce, waiting = self._read_at, self.nonce, self._requested is not None
        out = ["<h1>Suspected leak?</h1>"]
        if note:
            out.append(f'<p class="w">{e(note)}</p>')
        out.append("<p>If you think a key, token or password of the house was seen by someone else (a screenshot, a "
                   "paste, a lost phone), do this page from top to bottom. Nothing here shows a secret.</p>")
        out.append("<h2>1. Change the house's own keys (one tap)</h2>")
        enabled = bool(keys and keys.get("keys_enabled"))
        if not enabled:
            out.append('<p class="w">Automatic key changes are turned off (option <code>key_rotation</code>), so '
                       "this App cannot change them. Turn the option on, then come back.</p>")
        else:
            names = ", ".join(e(self.role_names.get(r, r)) for r in self.roles)
            out.append(f"<p>Changes the Broker keys of: {names}. One App at a time, each checked and undone by "
                       "itself if it fails; the old key stops working within 24 hours. Nothing else changes and "
                       "nothing is asked on your phone.</p>")
            if not (keys or {}).get("keys_enrolled"):
                out.append('<p class="w">Automatic key changes are not set up yet; the tap is kept and runs once '
                           "they are.</p>")
            if waiting:
                out.append('<p class="m">Started: it begins on the next check.</p>')
            else:
                out.append(f'<form method="post" action="{e(action)}"><input type="hidden" name="nonce" '
                           f'value="{e(nonce)}"><button class="r" name="choice" value="rotate">'
                           "Change the house keys now</button></form>")
            if self.last_started:
                when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(self.last_started))
                where = f"issue #{int(self.issue)}" if self.issue else "the report issue"
                out.append(f'<p class="m">Last started: {e(when)}. Results are posted on {e(where)}.</p>')
        out.append("<h2>2. Change the rest by hand</h2>")
        inventory = f"https://github.com/{self.repo}/blob/main/{INVENTORY_PATH}"
        if rows is None:
            out.append('<p class="w">The list could not be read from GitHub yet. Open it on GitHub: '
                       f'<a href="{e(inventory)}" target="_blank" rel="noopener">credential list</a> '
                       "(each entry's <code>leak_response</code> says what to do).</p>")
        else:
            manual = [r for r in rows if not r["button"]]
            out.append(f'<p class="m">From the credential list ({len(manual)} items; read '
                       f'{e(time.strftime("%Y-%m-%d", time.gmtime(read_at)))}). Start with the one you think leaked. '
                       "A GitHub App key: Disconnect it on its GitHub connection page, delete that App on GitHub, "
                       "then Connect again (3 taps).</p><ol>")
            for r in manual:
                out.append(f"<li><b>{e(r['id'])}</b> <span class=\"m\">({e(r['where'])})</span><br>"
                           f"{_linked(r['response'])}</li>")
            out.append("</ol>")
            done = [r["id"] for r in rows if r["button"]]
            if done:
                out.append(f'<p class="m">Done by the button above: {e(", ".join(done))}.</p>')
        out.append('<p class="m">Tell the AI which one you think leaked: it can file the changes it is allowed to '
                   "make and tell you the exact taps for the rest.</p>")
        return "".join(out)
