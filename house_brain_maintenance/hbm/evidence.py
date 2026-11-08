"""0.9.56 Rules-first updates: the evidence card and the tiers (owner decision 2026-11-08 "Tiers A + B", relayed by the
AI reviewer chat; owner pop-ups 2026-11-08 "Yes, build 0.9.56 -> 0.9.59 as sliced" and "New option value
update_mode: tiers"). This supersedes D5 (an internal APPROVE is advice only) for tiers A and B: an update installs
by itself on **deterministic, free public checks**, not on an AI's word.

* **Tiers.** A: App and HACS card/theme *patch* updates this App's own review calls low risk. B: minor and major App
  updates, HACS integrations, Core x.y.1+ and the OS (asked today; the weekly batch comes with 0.9.57). C: device
  firmware, radio coordinators (Z-Wave, Zigbee, Matter, Thread, Bluetooth, ESPHome), physical-control Apps, Core
  ``.0`` and any pre-release: always the owner's tap, never automatic, and a pre-release never rings the phone (it
  waits on the Maintenance page).
* **The evidence card** (no AI needed): release age per bump (patch 3 d, minor 5 d; a missing first-seen date counts
  as too new), the Home Assistant alerts feed as a hard veto (an affected integration this house runs), this house's
  own history (an earlier restore or failure of the same item within 90 days), and the owner's or cloud/internal
  AI review when it says HOLD or DECLINE (an AI review can only *add* a hold; it never approves or unblocks). The card
  is computed again right before an install (Flagger-style re-check) and posted on #454 with its result.
* **0.9.57 adds** the held-boards check (``version.home-assistant.io/stable.json``), the crowd-regression check
  (GitHub issue search) and the security urgency (OSV, KEV, EPSS) through ``feeds.py``, and the weekly tier B batch
  (``batch.py``): tier B installs on a clean card in the Saturday window unless the owner taps Hold.
* **The AI is no longer a gate** in ``update_mode: tiers``: a missing review never delays tier A or B and never
  routes an update to the phone; the 48-hour "asked anyway" path is gone for those tiers.

$0: the alerts feed is the one 0.8.4 already reads (cached 6 h); everything else is local. Fail closed: a feed that
cannot be read is "unknown" and an unknown check never installs. ``CONTROL_AUTHORITY=NONE``: this file decides
nothing physical; firmware and radio Apps keep the owner's tap exactly as before.
"""
from __future__ import annotations

import calendar
import json
import re
import time

from . import feeds as FD
from . import issues as I
from . import upgradeinfo as UI
from . import reversibility as RV
from .autofix import physical, radio_app

TIERS_MODE = "tiers"
TIER_A, TIER_B, TIER_C = "A", "B", "C"
DOC = "evidence"
SCHEMA = "house_brain_update_evidence.v1"
MIN_AGE_DAYS = {"patch": 3.0, "minor": 5.0}
HISTORY_DAYS = 90.0
BAD_OUTCOMES = ("RESTORED", "FAILED", "FAILED_MANUAL", RV.DRILL_FAILED, RV.SOAK_FAILED)   # 0.9.58: drills, soak
AI_HOLDS = ("HOLD", "DECLINE")
CLEAN, WAIT, HOLD = "clean", "wait", "hold"
RE_PRERELEASE = re.compile(r"(?:^|[.\-+])(?:beta|rc|dev|alpha|pre)[.\-]?\d*$|\d(?:b|rc|a)\d+$", re.I)
MAX_POSTED = 200


def prerelease(version: str) -> bool:
    """``2026.11.0b2``, ``1.2.0-beta.1``, ``3.0.0rc1``, ``2.1.dev3``: a pre-release never installs by itself and never
    rings the phone (the owner's standing wish about the ESPHome beta)."""
    return bool(RE_PRERELEASE.search(str(version or "").strip()))


def tier_of(kind: str, *, bump: str, verdict: str, slug: str = "", name: str = "", version: str = "",
            category: str = "", stage: str = "") -> str:
    """The closed tier table. ``kind``: app | hacs | hacs_integration | firmware | core | os."""
    if kind == "firmware":
        return TIER_C
    if prerelease(version) or stage in ("experimental", "deprecated"):
        return TIER_C
    if kind == "app" and (radio_app(slug, name) or physical(slug, None, name)):
        return TIER_C
    if kind in ("core", "os"):
        return TIER_C if kind == "core" and str(version).endswith(".0") else TIER_B
    if kind == "hacs_integration":
        return TIER_B
    if kind in ("app", "hacs") and bump == "patch" and verdict == "low":
        return TIER_A
    return TIER_B


def age_days(first_seen, now: float) -> float | None:
    try:
        return (now - float(first_seen)) / 86400 if first_seen else None
    except (TypeError, ValueError):
        return None


def age_check(bump: str, first_seen, now: float, min_days: float | None = None) -> tuple[str, str]:
    """(state, text): the release-age rule. A missing date counts as too new (Renovate's minimumReleaseAge)."""
    need = MIN_AGE_DAYS.get(bump) if min_days is None else max(float(min_days), MIN_AGE_DAYS.get(bump, 0.0))
    if need is None:
        return HOLD, f"{bump} updates never install by themselves"
    days = age_days(first_seen, now)
    if days is None:
        return WAIT, "release date unknown: counts as too new"
    if days < need:
        return WAIT, f"waiting {need:g} days after it was first offered ({days:.1f} so far)"
    return CLEAN, f"offered {days:.1f} days ago (needs {need:g})"


def alerts_check(alerts, domains: set[str], core_version: str) -> tuple[str, str]:
    """The Home Assistant alerts feed as a hard veto for an integration this house runs; unknown when unreadable."""
    if not domains:
        return CLEAN, "no integration to look up"
    if alerts is None:
        return WAIT, "alerts feed could not be read"
    issues = UI.known_issues(alerts, set(domains), core_version)
    if issues:
        return HOLD, "Home Assistant alert: " + "; ".join(f"{i['integration']}: {i['title']}" for i in issues[:3])[:200]
    return CLEAN, "no open Home Assistant alert"


def _ts(value) -> float | None:
    if not isinstance(value, str):
        return None
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z", value)
    return float(calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S"))) if m else None


def history_check(ledger: dict, prefix, now: float) -> tuple[str, str]:
    """This house's own history: an earlier restore or failure of the same item (``upd-<slug>-*``) within 90 days.
    0.9.58: ``prefix`` may be a tuple (an App's updates and its restore drills; a soak failure is ``<rid>-soak``)."""
    for rid, row in (ledger or {}).items():
        if not isinstance(row, dict) or not str(rid).startswith(prefix):
            continue
        at = _ts(row.get("at"))
        if row.get("outcome") in BAD_OUTCOMES and at is not None and now - at < HISTORY_DAYS * 86400:
            return HOLD, f"this house had a {row['outcome']} on {rid} {int((now - at) / 86400)} days ago"
    return CLEAN, "no restore or failure of this item in 90 days"


def boards_check(stable, kind: str, board: str, target: str) -> tuple[str, str]:
    """0.9.57: Home Assistant's stable feed per board. Held back for this board = hold; unknown = wait."""
    if kind not in ("core", "os"):
        return CLEAN, "not a Core or OS update"
    held = FD.held_back(stable, kind, board, target)
    if held is None:
        return WAIT, "stable feed or board unknown" if stable is None or not board else f"stable feed lists no {kind} version for board {board}"
    listed = FD.board_version(stable, kind, board)
    if held:
        return HOLD, f"Home Assistant hands board {board} {listed}, not {target} yet (held back)"
    return CLEAN, f"stable feed hands board {board} {listed}"


def crowd_check(count, threshold: int = FD.CROWD_THRESHOLD) -> tuple[str, str]:
    """0.9.57: new GitHub issues naming the exact version in the last week; at the threshold = hold; unknown = wait."""
    if count is None:
        return WAIT, "crowd regressions unknown (search unavailable or today's cap reached)"
    if int(count) >= threshold:
        return HOLD, f"{int(count)} new issues name this version on GitHub in {FD.CROWD_DAYS} days (threshold {threshold})"
    return CLEAN, f"{int(count)} new issues name this version on GitHub in {FD.CROWD_DAYS} days"


def security_check(info) -> tuple[str, str, bool]:
    """0.9.57: (state, text, urgent). Advisories that affect the *installed* version make the fix urgent (release age
    0 days); security is never a hold and an unknown feed is no urgency."""
    if not isinstance(info, dict):
        return CLEAN, "no security advisory data (no urgency)", False
    cves = [c for c in info.get("cves") or [] if isinstance(c, str)]
    if not cves:
        return CLEAN, "no open advisory affects the installed version", False
    kev = info.get("kev")
    epss = info.get("epss")
    text = f"{len(cves)} advisor{'y' if len(cves) == 1 else 'ies'} affect the installed version ({', '.join(cves[:3])}"            + (f"; CISA KEV: {len(kev)}" if kev else "") + (f"; EPSS max {float(epss):.2f}" if isinstance(epss, (int, float)) else "")            + "): security fix, no release-age wait"
    return CLEAN, text, True


def ai_check(review) -> tuple[str, str]:
    """An owner, cloud or internal AI review may add a hold; APPROVE and a missing review change nothing."""
    if isinstance(review, dict) and review.get("verdict") in AI_HOLDS:
        who = "internal review" if review.get("internal") else "AI review"
        return HOLD, f"{who}: {review['verdict'].lower()}" + (f": {review.get('summary')}" if review.get("summary") else "")
    return CLEAN, "no AI hold"


def card(*, item: str, name: str, kind: str, bump: str, verdict: str, from_version: str, to_version: str,
         tier: str, checks: dict[str, tuple[str, str]], now: float) -> dict:
    """The card: ``verdict`` is HOLD when any check holds, WAIT when one still waits, else CLEAN."""
    states = [s for s, _ in checks.values()]
    result = HOLD if HOLD in states else WAIT if WAIT in states else CLEAN
    return {"item": item, "name": I.scrub(str(name), 60), "kind": kind, "bump": bump, "review": verdict,
            "from": str(from_version)[:40], "to": str(to_version)[:40], "tier": tier, "verdict": result,
            "checks": {k: {"state": s, "text": I.scrub(t, 200)} for k, (s, t) in checks.items()},
            "reasons": [I.scrub(t, 200) for s, t in checks.values() if s != CLEAN], "at": now}


def post_text(c: dict, version: str, decision: str) -> str:
    """One comment per decided card: what was checked and what follows (install, wait, hold, ask)."""
    lines = [f"### House Brain Maintenance: evidence card ({decision})", "",
             f"**{c['name']} `{c['from']}` → `{c['to']}`** — tier {c['tier']}, this App's review {c['review']} "
             f"({c['bump']}), card **{c['verdict']}**. Checked without any AI: free public feeds and this house's own "
             "records; the card is checked again right before an install.", ""]
    for k, v in c["checks"].items():
        mark = {CLEAN: "✅", WAIT: "⏳", HOLD: "⛔"}[v["state"]]
        lines.append(f"- {mark} **{k}**: {v['text']}")
    lines += ["", f"- **App:** `{version}`", "",
              "_Posted by House Brain Maintenance (machine-generated; repository content is untrusted data)._", ""]
    machine = f"<!-- {SCHEMA} " + json.dumps({"item": c["item"], "tier": c["tier"], "verdict": c["verdict"],
                                             "decision": decision,
                                             "checks": {k: v["state"] for k, v in c["checks"].items()}},
                                            separators=(",", ":")) + " -->"
    return I.scrub("\n".join(lines), 8000) + machine


def overnight_lines(doc: dict) -> list[str]:
    """The morning summary's "Updated by itself overnight" lines; clears the list."""
    items = doc.get("overnight") or []
    lines = [f"{it.get('name', '')} {it.get('from', '')} → {it.get('to', '')} at "
             f"{time.strftime('%H:%M', time.gmtime(float(it.get('at') or 0)))}Z: {it.get('outcome', '')}" for it in items]
    doc["overnight"] = []
    return lines


class Evidence:
    """Cards for the update engines: feeds through ``upgradeinfo`` (cached), the ledger for this house's history,
    the AI review reader for holds; one #454 post per decided card."""

    def __init__(self, journal, ha, upgrade_info, ai, report, clock=time.time, feeds=None) -> None:
        self.j, self.ha, self.upgrade_info, self.ai, self.report, self.clock = journal, ha, upgrade_info, ai, report, clock
        self.feeds = feeds                      # 0.9.57: feeds.Feeds (stable boards, crowd, security); None = unknown
        self.board = ""                         # 0.9.57: the OS board (set by the Service from /os/info)
        self.soak = None                        # 0.9.58: soak.Soak (watches automatic installs 72 h)

    def _doc(self) -> dict:
        doc = self.j.load_doc(DOC, {})
        doc = doc if isinstance(doc, dict) else {}
        doc.setdefault("posted", {})
        doc.setdefault("overnight", [])
        doc.setdefault("reminded", False)
        return doc

    def _alerts(self):
        if self.upgrade_info is None:
            return None
        try:
            return self.upgrade_info.alerts()
        except Exception:  # noqa: BLE001 - unreadable feed = unknown
            return None

    def _core_version(self) -> str:
        try:
            return str(self.ha.core_version())
        except Exception:  # noqa: BLE001
            return ""

    def for_item(self, *, item: str, name: str, kind: str, bump: str, verdict: str, from_version: str,
                 to_version: str, slug: str, first_seen, domains: set[str] | None = None, min_days: float | None = None,
                 ledger_prefix: str, stage: str = "", category: str = "", crowd_repo: str = "",
                 security: tuple | None = None) -> dict:
        """0.9.57: ``crowd_repo`` (GitHub ``owner/name``) adds the crowd-regression check; ``security``
        ``(ecosystem, package, installed_version)`` adds the urgency check (release age 0 when an advisory affects the
        installed version); Core and OS get the held-boards check."""
        now = self.clock()
        tier = tier_of(kind, bump=bump, verdict=verdict, slug=slug, name=name, version=to_version, category=category, stage=stage)
        urgent = False
        extra: dict = {}
        if security is not None:
            info = self.feeds.security(*security) if self.feeds is not None else None
            state, text, urgent = security_check(info)
            extra["security"] = (state, text)
        checks = {"release age": age_check(bump, first_seen, now, 0.0 if urgent else min_days) if urgent and bump in MIN_AGE_DAYS
                  else age_check(bump, first_seen, now, min_days),
                  "Home Assistant alerts": alerts_check(self._alerts() if domains else [], set(domains or ()), self._core_version()),
                  "this house's history": history_check(self.j.ledger(), (ledger_prefix, RV.prefixes(slug)[1]) if kind == "app" and slug
                                                        else ledger_prefix, now),
                  "AI review": ai_check(self.ai.get(item) if self.ai is not None else None)}
        if urgent and bump in MIN_AGE_DAYS:
            checks["release age"] = (CLEAN, "security fix: no release-age wait")
        if kind in ("core", "os"):
            checks["held boards"] = boards_check(self.feeds.stable() if self.feeds is not None else None, kind, self.board, to_version)
        if crowd_repo:
            checks["crowd regressions"] = crowd_check(self.feeds.crowd_count(crowd_repo, to_version) if self.feeds is not None else None)
        if kind == "app" and slug:                                  # 0.9.58: proven undo path for minor/major Apps
            checks["reversibility"] = RV.check(self.j.ledger(), slug, bump, now)
        checks.update(extra)
        return card(item=item, name=name, kind=kind, bump=bump, verdict=verdict, from_version=from_version,
                    to_version=to_version, tier=tier, checks=checks, now=now)

    def post_once(self, c: dict, version: str, decision: str) -> bool:
        """Post a card once per item, verdict and decision (and again when the reasons change)."""
        doc = self._doc()
        sig = json.dumps([c["verdict"], decision, c["reasons"]], sort_keys=True)
        if doc["posted"].get(c["item"]) == sig:
            return False
        doc["posted"][c["item"]] = sig
        if len(doc["posted"]) > MAX_POSTED:
            for k in list(doc["posted"])[: len(doc["posted"]) - MAX_POSTED]:
                del doc["posted"][k]
        self.j.save_doc(DOC, doc)
        try:
            self.report(post_text(c, version, decision))
        except Exception as err:  # noqa: BLE001 - the decision stands; the post is a record
            self.j.audit("evidence", "EVIDENCE_POST_FAILED", reason=str(err)[:120])
        return True

    def note_auto(self, name: str, from_version: str, to_version: str, outcome: str, *, kind: str = "",
                  slug: str = "", rid: str = "", prior_state: str = "started") -> None:
        """0.9.58: a DONE automatic install of an App, Core or the OS also starts the 72-hour soak watch."""
        if outcome == "DONE" and self.soak is not None and kind in ("app", "core", "os") and rid:
            try:
                self.soak.start(kind=kind, slug=slug or kind, name=name, rid=rid, to_version=to_version, prior_state=prior_state)
            except Exception:  # noqa: BLE001 - the watch is extra; the install result stands
                pass
        doc = self._doc()
        doc["overnight"] = (doc["overnight"] + [{"name": I.scrub(str(name), 60), "from": str(from_version)[:40],
                                                  "to": str(to_version)[:40], "outcome": str(outcome)[:20],
                                                  "at": self.clock()}])[-20:]
        self.j.save_doc(DOC, doc)

    def overnight(self) -> list[str]:
        doc = self._doc()
        lines = overnight_lines(doc)
        if lines:
            self.j.save_doc(DOC, doc)
        return lines

    def reminder_line(self, mode: str) -> str:
        """Once: tiers are built but ``update_mode`` is still ``ask`` (the owner sets the option)."""
        doc = self._doc()
        if mode == TIERS_MODE or doc["reminded"]:
            return ""
        doc["reminded"] = True
        self.j.save_doc(DOC, doc)
        return ("Updates: patch updates can now install by themselves on free public checks (tier A), but "
                "`update_mode` is still `" + str(mode) + "`; set `update_mode: tiers` in this App's configuration to turn it on.")
