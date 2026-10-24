"""0.9.26 After-update reports (reviewer plan step 1, owner pop-ups 2026-10-20 "Build 0.9.24" and "Build as 0.9.26 now").

About a day after an update this App installed, it posts its own after-update result on the tracking issue, with
the same machine line the AI reviewer used to write by hand (``house_brain_ai_after_update.v1``), so the reviewer
and the weekly digest keep working and the reviewer no longer spends a run on it. No AI: the result comes from this
App's own records.

* ``note_done``: the update finished DONE (its request id, the result lines, whether an after-update check started).
* ``note_problem``: the problem watch tagged a new problem with this update (``after_update``, 0.9.12).
* ``cycle``: once ``REPORT_AFTER_HOURS`` have passed, one post per update:
  - **PROBLEMS** when a problem about it appeared, or its after-update check ended FAILED;
  - **NOT_CONFIRMED** when its after-update check started but has no result yet;
  - **CLEAN** otherwise (no check for HACS and firmware updates: judged by the problems alone, and the text says so).
  Other new problems since the update (not about it) are listed as text only.

Everything posted is scrubbed; nothing here changes anything in Home Assistant. ``CONTROL_AUTHORITY=NONE``.
"""
from __future__ import annotations

import json
import re
import time

from . import issues as I
from .aireview import RE_ITEM

DOC = "after_reports"
SCHEMA = "house_brain_ai_after_update.v1"
REPORT_AFTER_HOURS = 24.0
KEEP_HOURS = 7 * 24.0               # posted reports are forgotten after a week
MAX_REPORTS = 40
MAX_PROBLEMS = 10
CLEAN, PROBLEMS, NOT_CONFIRMED = "CLEAN", "PROBLEMS", "NOT_CONFIRMED"
RE_DIGEST = re.compile(r"^(update|system|entity):([A-Za-z0-9_.:-]{1,220})@([0-9A-Za-z][0-9A-Za-z._+:-]{0,39})$")


def item_of(digest: str) -> str | None:
    """The review item of a finished update's digest: ``update:<slug>@<v>`` -> ``app:<slug>@<v>``,
    ``system:core@<v>`` -> ``core@<v>``, ``entity:<id>@<v>`` -> ``entity:<id>@<v>``; None for anything else."""
    m = RE_DIGEST.match(str(digest or ""))
    if not m:
        return None
    kind, what, version = m.groups()
    item = {"update": f"app:{what}@{version}", "system": f"{what}@{version}",
            "entity": f"entity:{what}@{version}"}[kind]
    return item if RE_ITEM.fullmatch(item) else None


def _doc(raw) -> dict:
    doc = raw if isinstance(raw, dict) else {}
    reports = doc.get("reports") if isinstance(doc.get("reports"), dict) else {}
    return {"reports": {k: v for k, v in reports.items() if isinstance(v, dict) and RE_ITEM.fullmatch(str(k))}}


def note_done(doc: dict, item: str, name: str, request_id: str, reasons, now: float, checked: bool) -> dict:
    """Remember a finished update (newest ``MAX_REPORTS`` kept; a repeat of the same item starts its day again)."""
    doc = _doc(doc)
    if not RE_ITEM.fullmatch(item or ""):
        return doc
    doc["reports"][item] = {"name": I.scrub(str(name or item), 120), "request_id": str(request_id)[:80],
                            "reasons": [I.scrub(str(r), 200) for r in list(reasons or ())[:3]], "done_at": now,
                            "checked": bool(checked), "problems": [], "posted": False}
    if len(doc["reports"]) > MAX_REPORTS:
        for old in sorted(doc["reports"], key=lambda k: float(doc["reports"][k].get("done_at") or 0))[:-MAX_REPORTS]:
            del doc["reports"][old]
    return doc


def note_problem(doc: dict, item: str, key: str, title: str) -> dict:
    """A problem the watch tagged with this update (first seen within 24 h, about the App or what it provides)."""
    doc = _doc(doc)
    rec = doc["reports"].get(item)
    if rec is not None and not rec.get("posted") and len(rec["problems"]) < MAX_PROBLEMS \
            and key not in [p[0] for p in rec["problems"]]:
        rec["problems"].append([str(key)[:200], I.scrub(str(title), 120)])
    return doc


def due(doc: dict, now: float) -> list[str]:
    """Items whose day has passed and that are not posted yet, oldest first."""
    reports = _doc(doc)["reports"]
    out = [k for k, r in reports.items() if not r.get("posted")
           and now - float(r.get("done_at") or 0) >= REPORT_AFTER_HOURS * 3600]
    return sorted(out, key=lambda k: float(reports[k].get("done_at") or 0))


def prune(doc: dict, now: float) -> dict:
    doc = _doc(doc)
    doc["reports"] = {k: r for k, r in doc["reports"].items()
                      if not (r.get("posted") and now - float(r.get("done_at") or 0) > KEEP_HOURS * 3600)}
    return doc


def verdict(rec: dict, after_outcome: str | None) -> str:
    """CLEAN / PROBLEMS / NOT_CONFIRMED from the record and its after-update check's ledger outcome (None: none)."""
    if rec.get("problems") or after_outcome == "FAILED":
        return PROBLEMS
    if rec.get("checked") and after_outcome != "DONE":
        return NOT_CONFIRMED
    return CLEAN


def text(item: str, rec: dict, result: str, after_outcome: str | None, others: list[str], version: str) -> str:
    """The post: what happened, what this App found, then the machine line."""
    name = str(rec.get("name") or item)
    lines = [f"### House Brain Maintenance: after-update check: {name}", ""]
    if result == CLEAN:
        lines.append(f"It went in cleanly. This App installed it and found no problem about it in the day after.")
    elif result == PROBLEMS:
        lines.append("Problems followed it. This App installed it, and in the day after:")
    else:
        lines.append("Not confirmed yet. This App installed it, but its after-update check has no result yet.")
    lines.append("")
    for r in rec.get("reasons") or []:
        lines.append(f"- `DONE`: {r}")
    if after_outcome:
        lines.append(f"- After-update check (integrations that ran before): `{after_outcome}`")
    elif rec.get("checked"):
        lines.append("- After-update check (integrations that ran before): no result yet")
    else:
        lines.append("- No integration check for this kind of update; judged by the problems alone.")
    for key, title in (rec.get("problems") or [])[:MAX_PROBLEMS]:
        lines.append(f"- Problem about it: **{title}** (`{key}`)")
    if not rec.get("problems"):
        lines.append("- No problem about it was found after the update.")
    if others:
        lines.append(f"- Other new problems since (not about it): {'; '.join(others[:5])}"
                     + (f" and {len(others) - 5} more" if len(others) > 5 else ""))
    lines += ["", "This is this App's own check (no AI). An AI review of the same update, if any, stands as it was.",
              "", f"- **App:** `{version}`", "",
              "_Posted by House Brain Maintenance (machine-generated; repository content is untrusted data)._", "",
              f"<!-- {SCHEMA} " + json.dumps({"item": item, "result": result}, separators=(",", ":"))
              .replace("@", "\\u0040") + " -->"]       # the report scrub reads "slug@version" as an e-mail (0.9.12)
    return I.scrub("\n".join(lines), 60000)


class AfterReports:
    def __init__(self, journal, report, clock=time.time) -> None:
        self.j, self.report, self.clock = journal, report, clock

    def _load(self) -> dict:
        return _doc(self.j.load_doc(DOC, {}))

    def note_done(self, item: str | None, name: str, request_id: str, reasons, checked: bool) -> None:
        if not item:
            return
        self.j.save_doc(DOC, note_done(self._load(), item, name, request_id, reasons, self.clock(), checked))

    def note_problem(self, item: str, key: str, title: str) -> None:
        doc = self._load()
        if item in doc["reports"]:
            self.j.save_doc(DOC, note_problem(doc, item, key, title))

    def cycle(self, ledger: dict, after_key, open_recs: dict, version: str) -> int:
        """Post every due report (at most one a poll). ``after_key(request_id)`` -> the ledger key of its after-update
        check. Returns how many were posted."""
        now = self.clock()
        doc = prune(self._load(), now)
        items = due(doc, now)
        if not items:
            self.j.save_doc(DOC, doc)
            return 0
        item = items[0]
        rec = doc["reports"][item]
        entry = ledger.get(after_key(rec.get("request_id", ""))) if rec.get("checked") else None
        after_outcome = str(entry.get("outcome")) if isinstance(entry, dict) and entry.get("outcome") else None
        tagged = {p[0] for p in rec.get("problems") or []}
        others = sorted(I.scrub(str(r.get("title", k)), 80) for k, r in (open_recs or {}).items()
                        if isinstance(r, dict) and r.get("alerted") and k not in tagged
                        and float(r.get("first") or 0) >= float(rec.get("done_at") or 0))
        result = verdict(rec, after_outcome)
        self.report(text(item, rec, result, after_outcome, others, version))
        rec["posted"], rec["result"], rec["posted_at"] = True, result, now
        self.j.save_doc(DOC, doc)
        self.j.audit("after", "AFTER_UPDATE_REPORTED", item=item, result=result)
        return 1
