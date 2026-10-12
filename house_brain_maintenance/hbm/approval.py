"""Owner approval via a Companion-app push: tap-to-open page or press-and-hold buttons.

Two equivalent channels, both bound to the same single-use nonce and owner:
* tapping the push opens the ingress approval page (``web.py``) where the
  owner taps Approve/Reject (Supervisor supplies the logged-in user id);
* press-and-hold shows Approve/Reject actions; the resulting
  ``mobile_app_notification_action`` event must carry the owner's user id.
Anything else (another user, an automation with no user, a stale or foreign
nonce, a timeout, a lost socket) is not approval. Timeout is rejection.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from .ha import APPROVAL_EVENT, HAError, HomeAssistant
from .web import ApprovalBoard, Pending

APPROVE = "APPROVE"
REJECT = "REJECT"
TIMEOUT = "TIMEOUT"
UNAVAILABLE = "UNAVAILABLE"
INBOX_CHANNEL = "inbox"   # 0.9.0: answered by the owner's decision on the Maintenance page (no push was sent)

# 0.9.0 Approve from the page: the App's one decision inbox (inbox.Inbox), set by main.py; None = no inbox.
INBOX = None


def decided(stage: str, rid: str) -> bool:
    """0.9.0: the owner decided this exact item on the page; its job should ask (and so get the answer) now."""
    return INBOX is not None and bool(rid) and INBOX.decided(f"{stage}:{rid}|")


def _from_inbox(key: str) -> str | None:
    if INBOX is None:
        return None
    choice = INBOX.take(key)
    return {"approve": APPROVE, "reject": REJECT}.get(choice) if choice else None


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
        raise HAError("OWNER_NOT_FOUND", f"owner_username matches {len(matches)} active users "
                      f"(of {len(known)}); check the spelling on the Configuration tab")
    return matches[0]["id"]


def ask(ha: HomeAssistant, notify_service: str, owner_user_id: str, *, stage: str,
        title: str, message: str, timeout: float, require_auth: bool = True,
        board: ApprovalBoard | None = None, open_url: str | None = None,
        should_stop=None) -> Decision:
    from .inbox import key_of
    key = key_of(stage, message, title)
    early = _from_inbox(key)
    if early is not None:                       # 0.9.0: decided on the page already: no push
        return Decision(early, channel=INBOX_CHANNEL)
    decision = _ask(ha, notify_service, owner_user_id, stage=stage, title=title, message=message, timeout=timeout,
                    require_auth=require_auth, board=board, open_url=open_url, should_stop=should_stop, key=key)
    if INBOX is not None:
        if decision.outcome in (APPROVE, REJECT):
            INBOX.answered(key)
        elif decision.detail != "shutting down":
            INBOX.missed(key, stage, title, message)   # listed on the page with Approve / Decline
            if decision.outcome == TIMEOUT and stage not in ("UPDATE", "SYSTEM", "PIFW", "JOB", "STAGE"):
                # (updates send their own missed note; one-shot chat requests are re-filed by the chat)
                inform(ha, notify_service, "Maintenance: missed ask",
                       f"Missed: {title[:90]}. Open the Maintenance page to approve or decline it.",
                       tag="hbm-missed", url=open_url)
    return decision


def _ask(ha: HomeAssistant, notify_service: str, owner_user_id: str, *, stage: str,
         title: str, message: str, timeout: float, require_auth: bool = True,
         board: ApprovalBoard | None = None, open_url: str | None = None,
         should_stop=None, key: str = "") -> Decision:
    nonce = secrets.token_hex(12)
    approve_id = f"HBM_{stage}_APPROVE_{nonce}"
    reject_id = f"HBM_{stage}_REJECT_{nonce}"
    body = message[:900]
    if board is not None:
        body = "Tap to open the approval page.\n" + body
    payload: dict = {
        "title": title[:120],
        "message": body[:950],
        "data": {
            "tag": f"hbm-{stage.lower()}",
            "push": {"interruption-level": "time-sensitive"},
            "actions": [
                {"action": approve_id, "title": "Approve", "authenticationRequired": require_auth},
                {"action": reject_id, "title": "Reject", "destructive": True},
            ],
        },
    }
    if board is not None and open_url:
        payload["data"]["url"] = open_url  # tap = open the page; it never approves by itself
    try:
        sock = ha.ws()
    except Exception as err:  # noqa: BLE001 - any socket failure means no approval
        return Decision(UNAVAILABLE, detail=type(err).__name__)
    ignored = 0
    deadline = time.monotonic() + timeout
    if board is not None:
        board.open(Pending(stage, nonce, title[:120], message[:900], owner_user_id, deadline))
    try:
        sock.command({"type": "subscribe_events", "event_type": APPROVAL_EVENT})
        ha.notify(notify_service, payload)
        while True:
            if board is not None:
                choice = board.decision(nonce)
                if choice is not None:
                    return Decision(APPROVE if choice == "approve" else REJECT, ignored, channel="page")
            early = _from_inbox(key)            # 0.9.0: decided in the page's list while this ask is open
            if early is not None:
                return Decision(early, ignored, channel=INBOX_CHANNEL)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return Decision(TIMEOUT, ignored)
            if should_stop is not None and should_stop():
                return Decision(UNAVAILABLE, ignored, "shutting down")
            event = sock.next_event(min(remaining, 1.0))
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
            return Decision(APPROVE if action == approve_id else REJECT, ignored, channel="push")
    except Exception as err:  # noqa: BLE001 - lost socket / notify failure: fail closed
        return Decision(UNAVAILABLE, ignored, type(err).__name__)
    finally:
        if board is not None:
            board.close()
        sock.close()


def inform(ha: HomeAssistant, notify_service: str, title: str, message: str, tag: str = "hbm-result",
           url: str | None = None) -> None:
    data: dict = {"tag": tag}
    if url:                                      # 0.9.0: tap opens the Maintenance page (it decides nothing itself)
        data.update(url=url, clickAction=url)
    try:
        ha.notify(notify_service, {"title": title[:120], "message": message[:900], "data": data})
    except Exception:  # noqa: BLE001, S110 - information only
        pass
