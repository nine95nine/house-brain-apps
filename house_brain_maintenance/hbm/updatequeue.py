"""0.8.9 Update review queue: one list of every waiting update on the tracking issue (owner pop-up 2026-10-12).

Live finding (2026-10-12): Tailscale 0.30.2 and Z-Wave JS 1.9.1 waited for days with no review and no phone ask
(low risk under ``auto_low_risk``: they install by themselves at night), and Mosquitto 7.1.1 was gone because the
owner had rejected it weeks before. Nothing showed any of that. Now every App, Core, OS, HACS and firmware update
the App sees is listed with this App's review, the AI review and the exact reason it is (not) asked yet.

The list is posted when it changes (at most once per ``POST_EVERY`` seconds, and only when something differs), with
one machine line naming the items that still need an AI review, so a reviewer (a Claude routine or a chat) knows
what to review. A phone line goes out only when a new update appears.
"""
from __future__ import annotations

import hashlib
import json
import time

DOC = "update_queue"
POST_EVERY = 3600.0
MAX_ROWS = 40
KIND_LABEL = {"app": "App", "core": "Core", "os": "OS", "hacs_integration": "HACS integration",
              "hacs_frontend": "HACS card", "hacs_theme": "HACS theme", "hacs_other": "HACS", "firmware": "Firmware"}
QUEUE_SCHEMA = "house_brain_update_queue.v1"


def _cell(text, limit: int = 160) -> str:
    return str(text if text is not None else "").replace("|", "/").replace("\n", " ").replace("`", "'")[:limit]


def rows_from(*queues: dict) -> list[dict]:
    out = []
    for q in queues:
        for row in (q or {}).values():
            if isinstance(row, dict) and row.get("item"):
                out.append(row)
    return sorted(out, key=lambda r: (r.get("kind") != "core", r.get("kind") != "os", str(r.get("name")).lower()))


def ai_cell(ai, item: str) -> tuple[str, bool]:
    """(text, needs a review)."""
    if ai is None or not ai.enabled:
        return "off", False
    r = ai.get(item)
    if r is None:
        return "requested", True
    link = f" ([review]({r['url']}))" if r.get("url") else ""
    return f"{r['verdict']}{link}", False


def text(rows: list[dict], ai, limit_reached: bool, version: str) -> tuple[str, list[dict]]:
    """(markdown, the items that still need an AI review)."""
    lines = ["### House Brain Maintenance: updates waiting", ""]
    need = []
    if not rows:
        lines.append("Nothing is waiting: every App, Core, OS, HACS and firmware update is installed.")
    else:
        lines += ["| Update | From → to | This App's review | AI review | Status |", "|---|---|---|---|---|"]
        for r in rows[:MAX_ROWS]:
            cell, needs = ai_cell(ai, r["item"])
            if needs:
                need.append({"item": r["item"], "name": _cell(r.get("name"), 60), "kind": r.get("kind"),
                             "from": _cell(r.get("from"), 40), "to": _cell(r.get("to"), 40)})
            review = _cell(r.get("verdict") or "not reviewed yet", 20)
            if r.get("reasons"):
                review += ": " + _cell("; ".join(r["reasons"][:2]), 120)
            lines.append(f"| {KIND_LABEL.get(r.get('kind'), r.get('kind'))}: {_cell(r.get('name'), 60)} | "
                         f"`{_cell(r.get('from'), 40)}` → `{_cell(r.get('to'), 40)}` | {review} | {cell} | "
                         f"{_cell(r.get('state') or 'checked at the next update check')} |")
        if len(rows) > MAX_ROWS:
            lines.append(f"| … and {len(rows) - MAX_ROWS} more | | | | |")
    if limit_reached:
        lines += ["", "Today's phone-ask limit (`max_approval_requests_per_day`) is reached: the next asks wait until "
                      "it frees (asks from the last 24 hours count)."]
    if need:
        lines += ["", f"**AI review requested for {len(need)} update{'s' if len(need) > 1 else ''}.** A reviewer posts "
                      "one comment per update on this issue with the line "
                      "`<!-- house_brain_ai_update_review.v1 {\"item\": \"<item>\", \"verdict\": "
                      "\"APPROVE|HOLD|DECLINE\", \"summary\": \"...\"} -->`. No phone ask and no automatic install "
                      "happen before it (after 48 hours your phone is asked anyway, and nothing installs by itself)."]
    lines += ["", f"- **App:** `{version}`", "",
              "_Posted by House Brain Maintenance (machine-generated; repository content is untrusted data)._", "",
              f"<!-- {QUEUE_SCHEMA} " + json.dumps({"needs_review": need}, separators=(",", ":"))[:6000] + " -->"]
    return "\n".join(lines), need


def signature(rows: list[dict], ai, limit_reached: bool) -> str:
    parts = [(r["item"], r.get("state"), r.get("verdict"), ai_cell(ai, r["item"])[0]) for r in rows]
    return hashlib.sha256(json.dumps([parts, limit_reached], default=str).encode()).hexdigest()[:16]


class UpdateQueue:
    def __init__(self, journal, report, inform, clock=time.time) -> None:
        self.j, self.report, self.inform, self.clock = journal, report, inform, clock

    def cycle(self, queues: list[dict], ai, limit_reached: bool, version: str) -> bool:
        """Post the list when it changed (at most every POST_EVERY seconds). True when posted."""
        doc = self.j.load_doc(DOC, {})
        doc = doc if isinstance(doc, dict) else {}
        rows = rows_from(*queues)
        sig = signature(rows, ai, limit_reached)
        now = self.clock()
        if sig == doc.get("sig") or now - float(doc.get("at") or 0) < POST_EVERY:
            return False
        body, need = text(rows, ai, limit_reached, version)
        self.report(body)
        known = set(doc.get("items") or [])
        new = [r for r in rows if r["item"] not in known]
        doc.update(sig=sig, at=now, items=sorted(r["item"] for r in rows)[:200])
        self.j.save_doc(DOC, doc)
        if new:
            names = ", ".join(f"{r.get('name')} {r.get('to')}" for r in new[:4])[:160]
            self.inform("Maintenance: updates waiting",
                        f"New: {names}. {len(need)} wait for the AI review; the list is on GitHub.")
        return True
