"""0.8.4 Safer upgrades: what is known about a Core version before the owner approves it (owner pop-up 2026-10-11,
"All of the above based on your priority").

* **Known issues.** Home Assistant's own alerts feed (``https://alerts.home-assistant.io/alerts.json``, the same feed
  Core's ``homeassistant_alerts`` integration reads) lists problems per integration with ``affected_from_version``
  and ``resolved_in_version``. Here it is evaluated against the **target** version and the integrations loaded on
  this Home Assistant: "your ZHA is affected in 2026.10.0" before the update, not after it.
* **Release notes for your integrations.** The target version's GitHub release notes
  (``home-assistant/core`` releases, public, no token) are reduced to the lines that link an integration this Home
  Assistant runs, and the ones marked breaking.

Read-only and best effort: a feed that cannot be read says so in the review and never blocks or approves anything.
Both reads are cached (alerts 6 h, release notes per version), so a pending update costs a few requests a day.
Everything shown is plain, bounded text; nothing from the feeds is followed or executed.
"""
from __future__ import annotations

import re
import time

from . import net

ALERTS_URL = "https://alerts.home-assistant.io/alerts.json"
RELEASE_URL = "https://api.github.com/repos/home-assistant/core/releases/tags/{version}"
NOTES_URL = "https://github.com/home-assistant/core/releases/tag/{version}"
DOC = "upgrade_info"
ALERTS_TTL = 6 * 3600
RE_DOMAIN = re.compile(r"^[a-z0-9_]{1,64}$")
RE_VERSION = re.compile(r"^\d{4}\.\d{1,2}\.\d{1,3}(?:b\d{1,3}|\.dev\d{1,14})?$")
RE_DOCS_LINK = re.compile(r"home-assistant\.io/integrations/([a-z0-9_]{1,64})/?")
RE_BREAKING = re.compile(r"breaking[- ]change|backward[- ]incompatible", re.I)
MAX_ITEMS = 12


def version_key(v: str) -> tuple | None:
    """CalVer ``YYYY.M.P`` (+ ``bN`` / ``.devN``) as a sortable tuple; None when not a Core version."""
    if not isinstance(v, str) or not RE_VERSION.fullmatch(v):
        return None
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:b(\d+)|\.dev(\d+))?$", v)
    major, minor, patch, beta, dev = m.groups()
    pre = (0, int(dev)) if dev else (1, int(beta)) if beta else (2, 0)    # dev < beta < release
    return int(major), int(minor), int(patch), pre


def _plain(text, limit: int = 140) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f<>`*_\[\]()|#]", " ", str(text)))[:limit].strip()


def known_issues(alerts, components: set[str], target: str) -> list[dict]:
    """Alerts that affect ``target`` for an integration in ``components`` (Core's own rule, applied to the target)."""
    tk = version_key(target)
    if tk is None or not isinstance(alerts, list):
        return []
    out: list[dict] = []
    for alert in alerts:
        if not isinstance(alert, dict) or not isinstance(alert.get("integrations"), list):
            continue
        ha = alert.get("homeassistant") if isinstance(alert.get("homeassistant"), dict) else {}
        lo, hi = version_key(str(ha.get("affected_from_version", ""))), version_key(str(ha.get("resolved_in_version", "")))
        if "affected_from_version" in ha and (lo is None or tk < lo):
            continue
        if "resolved_in_version" in ha and (hi is None or tk >= hi):
            continue
        filename = str(alert.get("filename") or "")
        if not re.fullmatch(r"[a-z0-9_.-]{1,80}", filename):
            continue
        for row in alert["integrations"]:
            domain = row.get("package") if isinstance(row, dict) else None
            if isinstance(domain, str) and RE_DOMAIN.fullmatch(domain) and domain in components:
                out.append({"integration": domain, "title": _plain(alert.get("title") or filename.rsplit(".", 1)[0]),
                            "url": f"https://alerts.home-assistant.io/#{filename}"})
                break
        if len(out) >= MAX_ITEMS:
            break
    return out


def release_highlights(body, components: set[str]) -> dict:
    """Lines of a Core release body that link an integration this Home Assistant runs; breaking ones first."""
    if not isinstance(body, str):
        return {"read": False, "mine": [], "breaking": []}
    mine, breaking = [], []
    for line in body.splitlines()[:3000]:
        domains = [d for d in RE_DOCS_LINK.findall(line) if d in components]
        if not domains:
            continue
        text = _plain(re.sub(r"\(\[@[^)]*\)|\[#\d+\]|\[[a-z0-9_ ]+docs\]", "", line), 120)
        row = {"integration": domains[0], "text": text}
        (breaking if RE_BREAKING.search(line) else mine).append(row)
    return {"read": True, "mine": mine[:MAX_ITEMS], "breaking": breaking[:MAX_ITEMS],
            "mine_total": len(mine), "breaking_total": len(breaking)}


class UpgradeInfo:
    def __init__(self, journal, clock=time.time, request=None) -> None:
        self.j = journal
        self.clock = clock
        self.request = request or net.request

    def _doc(self) -> dict:
        doc = self.j.load_doc(DOC, {})
        doc = doc if isinstance(doc, dict) else {}
        doc.setdefault("notes", {})
        return doc

    def alerts(self):
        """The alerts feed, cached for 6 h; None when it could not be read (and no fresh copy)."""
        doc = self._doc()
        if doc.get("alerts_at") and self.clock() - float(doc["alerts_at"]) < ALERTS_TTL:
            return doc.get("alerts")
        try:
            _, data = self.request("GET", ALERTS_URL, {"Accept": "application/json"}, timeout=20)
        except net.NetError:
            return doc.get("alerts") if doc.get("alerts_at") and self.clock() - float(doc["alerts_at"]) < 86400 \
                else None
        if not isinstance(data, list):
            return None
        doc.update(alerts=data[:2000], alerts_at=self.clock())
        self.j.save_doc(DOC, doc)
        return doc["alerts"]

    def release_body(self, version: str):
        if version_key(version) is None:
            return None
        doc = self._doc()
        if version in doc["notes"]:
            return doc["notes"][version]
        try:
            _, data = self.request("GET", RELEASE_URL.format(version=version),
                                   {"Accept": "application/vnd.github+json"}, timeout=20)
        except net.NetError:
            return None
        body = data.get("body") if isinstance(data, dict) else None
        if isinstance(body, str):
            doc["notes"] = {**{k: v for k, v in list(doc["notes"].items())[-4:]}, version: body[:200000]}
            self.j.save_doc(DOC, doc)
        return body if isinstance(body, str) else None

    def for_core(self, target: str, components: set[str]) -> dict:
        """Facts for the Core review and the phone ask (best effort; never raises)."""
        try:
            alerts = self.alerts()
            issues = known_issues(alerts, components, target) if alerts is not None else None
            notes = release_highlights(self.release_body(target), components)
        except Exception:  # noqa: BLE001 - extra information only
            issues, notes = None, {"read": False, "mine": [], "breaking": []}
        return {"known_issues": issues, "notes": notes, "notes_url": NOTES_URL.format(version=target),
                "components": len(components)}


def review_lines(info: dict) -> list[str]:
    """Markdown bullet lines for the GitHub review."""
    lines = []
    issues = info.get("known_issues")
    if issues is None:
        lines.append("Known issues (Home Assistant alerts feed): could not be read; check https://alerts.home-assistant.io")
    elif not issues:
        lines.append("Known issues (Home Assistant alerts feed): none for your integrations in this version")
    else:
        lines.append(f"**Known issues for your integrations in this version: {len(issues)}**")
        lines += [f"  - {i['integration']}: {i['title']} ({i['url']})" for i in issues]
    notes = info.get("notes") or {}
    if not notes.get("read"):
        lines.append(f"Release notes: could not be read; see {info.get('notes_url')}")
    else:
        lines.append(f"Release notes for your integrations: {notes.get('mine_total', 0)} changes, "
                     f"{notes.get('breaking_total', 0)} marked breaking ({info.get('notes_url')})")
        lines += [f"  - breaking, {r['integration']}: {r['text']}" for r in notes.get("breaking", [])]
        lines += [f"  - {r['integration']}: {r['text']}" for r in notes.get("mine", [])[:6]]
    return lines


def phone_line(info: dict) -> str:
    """One short line for the phone ask."""
    issues = info.get("known_issues")
    notes = info.get("notes") or {}
    parts = []
    if issues is None:
        parts.append("known issues: not checked")
    elif issues:
        parts.append(f"KNOWN ISSUES for your integrations: {len(issues)} ({', '.join(i['integration'] for i in issues[:3])})")
    else:
        parts.append("known issues: none for your integrations")
    if notes.get("read"):
        parts.append(f"{notes.get('breaking_total', 0)} breaking change(s) for your integrations")
    return "; ".join(parts)[:240]
