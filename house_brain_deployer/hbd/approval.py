"""Owner approval via a Companion-app push: tap-to-open page or press-and-hold buttons.

Two equivalent channels, both bound to the same single-use nonce and owner:
* tapping the push opens the ingress approval page (``web.py``) where the
  owner taps Approve/Reject (Supervisor supplies the logged-in user id);
* press-and-hold shows Approve/Reject actions; the resulting
  ``mobile_app_notification_action`` event must carry the owner's user id.
Anything else (another user, an automation with no user, a stale or foreign
nonce, a timeout, a lost socket) is not approval. Timeout is rejection.

0.3.4: a long wait (hours) must survive a Core restart. Once the push is sent,
a lost socket no longer ends the wait while the page is open: the page keeps
deciding and the push buttons are re-subscribed every ``RESUBSCRIBE_SECONDS``.
Without a page, a lost socket still fails closed.
"""
from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import net
from .ha import APPROVAL_EVENT, CoreSocket, HAError, HomeAssistant
from .web import ApprovalBoard, Pending

APPROVE = "APPROVE"
REJECT = "REJECT"
TIMEOUT = "TIMEOUT"
UNAVAILABLE = "UNAVAILABLE"
STOPPED = "STOPPED"   # 0.3.5: the App was asked to stop while waiting (never an approval)
RESUBSCRIBE_SECONDS = 30.0


@dataclass
class Decision:
    outcome: str
    ignored_events: int = 0
    detail: str = ""
    channel: str = ""


def resolve_owner(ha: HomeAssistant, username: str) -> str:
    sock = ha.ws()
    try:
        users = sock.command({"type": "config/auth/list"})
    finally:
        sock.close()
    matches = [
        u for u in (users or [])
        if isinstance(u, dict) and u.get("username") == username
        and u.get("is_active") and not u.get("system_generated")
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("id"), str):
        known = sorted(str(u.get("username")) for u in (users or [])
                       if isinstance(u, dict) and u.get("username") and not u.get("system_generated"))
        raise HAError("OWNER_NOT_FOUND", f"owner_username must be one of: {', '.join(known)[:200]}")
    return matches[0]["id"]


def ask(ha: HomeAssistant, notify_service: str, owner_user_id: str, *, stage: str,
        title: str, message: str, timeout: float, require_auth: bool = True,
        board: ApprovalBoard | None = None, open_url: str | None = None,
        should_stop: Callable[[], bool] | None = None) -> Decision:
    nonce = secrets.token_hex(12)
    approve_id = f"HBD_{stage}_APPROVE_{nonce}"
    reject_id = f"HBD_{stage}_REJECT_{nonce}"
    body = message[:900]
    if board is not None:
        body = "Tap to open the approval page.\n" + body
    payload: dict = {
        "title": title[:120],
        "message": body[:950],
        "data": {
            "tag": f"hbd-{stage.lower()}",
            "push": {"interruption-level": "time-sensitive"},
            "actions": [
                {"action": approve_id, "title": "Approve", "authenticationRequired": require_auth},
                {"action": reject_id, "title": "Reject", "destructive": True},
            ],
        },
    }
    if board is not None and open_url:
        payload["data"]["url"] = open_url  # tap = open the page; it never approves by itself
    sock: CoreSocket | None
    try:
        sock = ha.ws()
    except Exception as err:  # noqa: BLE001 - any socket failure means no approval
        return Decision(UNAVAILABLE, detail=net.redact(f"{type(err).__name__}: {err}")[:200])
    ignored = 0
    deadline = time.monotonic() + timeout
    if board is not None:
        board.open(Pending(stage, nonce, title[:120], message[:900], owner_user_id, deadline))
    lost = ""
    retry_at = 0.0
    try:
        sock.command({"type": "subscribe_events", "event_type": APPROVAL_EVENT})
        ha.notify(notify_service, payload)
        while True:
            if should_stop is not None and should_stop():
                return Decision(STOPPED, ignored, lost)   # 0.3.5 (P3): the App is stopping; nothing decided
            if board is not None:
                choice = board.decision(nonce)
                if choice is not None:
                    return Decision(APPROVE if choice == "approve" else REJECT, ignored, lost, channel="page")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return Decision(TIMEOUT, ignored, lost)
            if sock is None:
                if time.monotonic() >= retry_at:
                    sock = _resubscribe(ha)
                    retry_at = time.monotonic() + RESUBSCRIBE_SECONDS
                if sock is None:
                    time.sleep(min(remaining, 1.0))
                    continue
            try:
                event = sock.next_event(min(remaining, 1.0))
            except Exception as err:  # noqa: BLE001 - socket lost after the push was sent
                if board is None:
                    raise
                lost = net.redact(f"push socket lost (page stayed open): {type(err).__name__}: {err}")[:200]
                sock.close()
                sock = None
                retry_at = time.monotonic() + RESUBSCRIBE_SECONDS
                continue
            if not event:
                continue
            data = event.get("data") or {}
            action = data.get("action") if isinstance(data, dict) else None
            if action not in (approve_id, reject_id):
                ignored += 1
                continue
            user = (event.get("context") or {}).get("user_id")
            if user != owner_user_id:
                ignored += 1
                continue
            return Decision(APPROVE if action == approve_id else REJECT, ignored, lost, channel="push")
    except Exception as err:  # noqa: BLE001 - lost socket / notify failure: fail closed
        where = "notify " if getattr(err, "what", "") == "notify" else ""
        return Decision(UNAVAILABLE, ignored, net.redact(f"{where}{type(err).__name__}: {err}")[:200])
    finally:
        if board is not None:
            board.close()
        if sock is not None:
            sock.close()


def _resubscribe(ha: HomeAssistant) -> CoreSocket | None:
    """A fresh socket listening for push-button taps, or None (the page still decides)."""
    try:
        sock = ha.ws()
    except Exception:  # noqa: BLE001 - Core still down; try again later
        return None
    try:
        sock.command({"type": "subscribe_events", "event_type": APPROVAL_EVENT})
    except Exception:  # noqa: BLE001
        sock.close()
        return None
    return sock


def inform(ha: HomeAssistant, notify_service: str, title: str, message: str) -> None:
    try:
        ha.notify(notify_service, {"title": title[:120], "message": message[:900],
                                   "data": {"tag": "hbd-result"}})
    except Exception:  # noqa: BLE001, S110 - information only
        pass
