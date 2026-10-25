"""0.9.30 Systems AI relay (AI family plan step 4; owner pop-ups 2026-10-20 "Maintenance relays it" and
"Maintenance pulls /proposals").

Systems AI is the read-only overseer: it has no hands. This App reads its two internal routes and:

* relays ``/digest`` into the one family status sensor ``sensor.house_brain_family_status`` for Companion (state =
  the family word, attributes = the line and one short line per App), at most every 30 minutes unless the word
  changes, so the recorder gains about 50 rows a day;
* shows each ``/proposals`` row under "Needs your decision" as a normal problem with this App's existing Fix
  button. Nothing runs without the owner's tap, and ``fix_target`` checks the target again then (never this App,
  the Scout or a manual-start App).

One owner per problem: no proposal is shown for an App this App already reports, and Systems AI itself never
raises what this App reports. Every field is checked here, whatever Systems AI sent: unknown kinds, targets
outside the House Brain App names, expired rows and an unhealthy source are dropped. Unknown never reads OK.
"""
from __future__ import annotations

import re

from . import issues as I

DIGEST_SCHEMA = "house_brain_systems_ai_digest.v1"
PROPOSALS_SCHEMA = "house_brain_systems_ai_proposals.v1"
DIGEST_STATES = ("OK", "WATCH", "PROBLEMS", "HELD", "DEGRADED", "FAILED", "UNKNOWN")
KINDS = {"restart_app": "restart", "start_app": "start"}
RE_TARGET = re.compile(r"^(?:local|[0-9a-f]{8})_house_brain_[a-z0-9_]{1,48}$")
RE_REASON = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
RE_ID = re.compile(r"^[A-Za-z0-9_:.\-]{1,160}$")
RE_UNSAFE = re.compile(r"[^\w .,:;()%/+\-–…'°]")
MAX_PROPOSALS = 10
MAX_APP_LINES = 12
TEXT_CHARS = 255
LINE_CHARS = 80
FAMILY_EVERY = 1800
SOURCE = "systems_ai"
UNREACHABLE_TEXT = "House Brain systems UNKNOWN: Systems AI is not reachable."


def _clean(text, limit: int) -> str:
    return RE_UNSAFE.sub("", text)[:limit] if isinstance(text, str) else ""


def _num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value


def project_digest(doc) -> dict | None:
    """The digest typed field by field; None = unusable (then the family sensor says UNKNOWN)."""
    if not isinstance(doc, dict) or doc.get("schema_version") != DIGEST_SCHEMA or doc.get("physical_actions") is not False \
            or doc.get("state") not in DIGEST_STATES or not isinstance(doc.get("text"), str) or not _num(doc.get("at")):
        return None
    apps = doc.get("apps") if isinstance(doc.get("apps"), dict) else {}
    lines = {_clean(k, 40): _clean(v, LINE_CHARS) for k, v in sorted(apps.items())[:MAX_APP_LINES]
             if isinstance(k, str) and isinstance(v, str)}
    return {"state": doc["state"], "text": _clean(doc["text"], TEXT_CHARS), "apps": lines, "at": int(doc["at"])}


def project_proposals(doc, now: float) -> list[dict] | None:
    """Usable, unexpired proposal rows; None = the reply is unusable. A FAILED source offers nothing."""
    if not isinstance(doc, dict) or doc.get("schema_version") != PROPOSALS_SCHEMA \
            or doc.get("executed_by_systems_ai") is not False or not isinstance(doc.get("proposals"), list):
        return None
    if doc.get("source_health") not in ("OK", "DEGRADED"):
        return []
    out = []
    for p in doc["proposals"][:MAX_PROPOSALS]:
        if not isinstance(p, dict) or p.get("kind") not in KINDS or not isinstance(p.get("target"), str) \
                or not RE_TARGET.fullmatch(p["target"]) or not isinstance(p.get("id"), str) \
                or not RE_ID.fullmatch(p["id"]) or not isinstance(p.get("reason"), str) \
                or not RE_REASON.fullmatch(p["reason"]) or not _num(p.get("expires_at")) or p["expires_at"] <= now \
                or not _num(p.get("opened_at")):
            continue
        out.append({"id": p["id"], "kind": p["kind"], "target": p["target"], "reason": p["reason"],
                    "opened_at": float(p["opened_at"])})
    return out


def from_proposals(rows: list[dict], names: dict[str, str], taken: set[str], blocked: set[str]) -> list[I.Finding]:
    """``names``: installed House Brain Apps (slug -> name) read this time; ``taken``: targets this App already
    reports or fixes (one owner per problem); ``blocked``: targets never offered (this App, the Scout)."""
    out, seen = [], set()
    for p in rows:
        target, kind = p["target"], p["kind"]
        if target not in names or target in taken or target in blocked or (target, kind) in seen:
            continue
        seen.add((target, kind))
        name = _clean(names[target], 60) or target
        why = p["reason"].lower().replace("_", " ")
        label = f"{KINDS[kind]} {name}"
        out.append(I.Finding(f"systems_ai:{kind}:{target}", SOURCE, I.WARNING, f"Systems AI suggests: {label}",
                             detail=(f"Why: {why}.",), steps=(f"Tap Fix it to {label}, or Reject.",),
                             link=I.APPS_LINK, action=I.Action(kind, target, label), announce_clear=False,
                             facts={"slug": target, "reason": p["reason"], "proposal": p["id"]},
                             done=f"Nothing yet. Systems AI noticed it; I can {label} for you: I ask you first.",
                             if_ignored="Nothing is done by itself; the suggestion expires when the problem goes."))
    return out


def family_state(digest: dict | None) -> tuple[str, dict]:
    """(state, attributes) of the family sensor. Unknown never reads OK."""
    if digest is None:
        return "UNKNOWN", {"text": UNREACHABLE_TEXT, "apps": {}, "source": "systems_ai"}
    return digest["state"], {"text": digest["text"], "apps": digest["apps"], "digest_at": digest["at"],
                             "source": "systems_ai"}


def due(last: tuple | None, state: str, now: float) -> bool:
    """Publish when the family word changes, else at most every FAMILY_EVERY seconds (about 48 rows a day)."""
    return last is None or last[0] != state or now - last[1] >= FAMILY_EVERY
