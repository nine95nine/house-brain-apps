"""0.9.51 Internal update reviews (owner decisions D4 and D5, 2026-11-01; pop-up 2026-11-07 "Yes, build 0.9.51 as
proposed"; the reviewer chat's spec R1 part B3).

When a waiting update has no AI review and the cloud reviewer is silent (the lane's ``lockout``), or the lane runs in
``primary`` mode, this App asks the free model through the House Brain Companion (0.2.0's ``kind: update`` route) to
read the update's release notes and answer APPROVE, HOLD or DECLINE. The notes come only from closed, public sources:

* an App's changelog as the Supervisor serves it (``/addons/<slug>/changelog``, the store's own CHANGELOG), cut to
  the part newer than the installed version;
* Core's release notes and known issues through ``upgradeinfo`` (``home-assistant/core`` releases and the
  ``alerts.home-assistant.io`` feed, both public, cached). OS, Pi firmware and device firmware have no allowlisted
  source and keep the 48-hour path.

D5: an internal verdict is advice. An internal APPROVE never installs anything by itself (``Gate.auto_ok`` is False);
the phone is always asked, with the internal line. This App's own review (held versions, risky bumps), the hold list
and the owner's standing rules (no ``.0`` Core release) run before any review and still win. A review by the owner
or the cloud reviewer replaces an internal one whenever it arrives.

$0: at most MAX_REVIEWS_PER_DAY internal reviews and MAX_READS_PER_DAY source reads a day (the counts are in the
status line), inside the free lane's everyday tier (Gemma, 1,200 Neurons). Over a limit the lane stops; the 48-hour
path stays. ``CONTROL_AUTHORITY=NONE``.
"""
from __future__ import annotations

import json
import re
import time

from . import freelane as FL
from . import issues as I
from .review import changelog_window
from . import upgradeinfo as UI

DOC = "update_lane"
SCHEMA = "house_brain_internal_update_review.v1"      # never house_brain_ai_update_review.v1: the owner's line
VERDICTS = ("APPROVE", "HOLD", "DECLINE")
WORDS = {"APPROVE": "approve", "HOLD": "hold it for now", "DECLINE": "decline"}
MAX_REVIEWS_PER_DAY = 10
MAX_READS_PER_DAY = 30
MAX_PER_RUN = 2
RUN_EVERY = 1800.0
MAX_SOURCES = 4
SOURCE_CHARS = 4000
KINDS = ("app", "core")                               # the kinds with an allowlisted release-note source
RE_ITEM = re.compile(r"^(?:app:[a-z0-9][a-z0-9_-]{0,99}|core)@[0-9A-Za-z][0-9A-Za-z._+:-]{0,39}$")
TIER = "everyday"


def day_of(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now))


def _doc(raw) -> dict:
    doc = raw if isinstance(raw, dict) else {}
    doc.setdefault("day", "")
    doc.setdefault("reviews", 0)
    doc.setdefault("reads", 0)
    doc.setdefault("ran_at", 0.0)
    doc.setdefault("done", {})          # item -> {"verdict", "at", "model", "neurons", "sources"}
    doc.setdefault("skip", {})          # item -> until (an unreadable answer: not again for a while)
    doc.setdefault("tried", {})         # item -> when the lane last looked at it (reviewed or not)
    doc.setdefault("last_reason", "")
    return doc


def roll_day(doc: dict, now: float) -> dict:
    if doc["day"] != day_of(now):
        doc.update(day=day_of(now), reviews=0, reads=0)
    return doc


def budget_left(doc: dict) -> bool:
    return doc["reviews"] < MAX_REVIEWS_PER_DAY and doc["reads"] < MAX_READS_PER_DAY


def plain(text, limit: int = SOURCE_CHARS) -> str:
    """Release-note text as data: no tags, links or control characters, bounded (the Companion cleans again)."""
    s = re.sub(r"<[^>]{0,200}>", " ", str(text or ""))
    s = re.sub(r"https?://\S+", "[link]", s)
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", s)
    return I.scrub(s, limit)


def item_key(item: str) -> str:
    """The Companion key for an update item (its key rule wants ``<kind>:<rest>``)."""
    return f"update:{item}"[:224]


def sources_for(row: dict, ha, upgrade_info) -> tuple[list[dict], int]:
    """(sources, network reads made). App: the Supervisor's changelog window (local, no read). Core: the release
    notes for the integrations this Home Assistant runs, the breaking lines and the known issues (public, cached;
    a read counts only when the cache had to be filled)."""
    kind = row.get("kind")
    out: list[dict] = []
    reads = 0
    if kind == "app":
        slug = str(row.get("item") or "")[4:].rsplit("@", 1)[0]
        try:
            window = changelog_window(str(ha.changelog(slug) or ""), str(row.get("from") or ""))
        except Exception:  # noqa: BLE001 - no changelog is "no source"
            window = ""
        if window.strip():
            out.append({"name": f"changelog {slug}", "text": plain(window)})
    elif kind == "core" and upgrade_info is not None:
        target = str(row.get("to") or "")
        cached = upgrade_info._doc()
        if target not in cached.get("notes", {}):
            reads += 1
        body = upgrade_info.release_body(target)
        if isinstance(body, str) and body.strip():
            try:
                comps = set(ha.loaded_components())
            except Exception:  # noqa: BLE001
                comps = set()
            hl = UI.release_highlights(body, comps)
            lines = [*hl.get("breaking", []), *hl.get("mine", [])]
            text = "\n".join(str(x) for x in lines) if lines else body[:SOURCE_CHARS]
            out.append({"name": f"release notes core {target}", "text": plain(text)})
        if not cached.get("alerts_at") or time.time() - float(cached.get("alerts_at") or 0) >= UI.ALERTS_TTL:
            reads += 1
        alerts = upgrade_info.alerts()
        if alerts is not None:
            try:
                comps = set(ha.loaded_components())
            except Exception:  # noqa: BLE001
                comps = set()
            issues = UI.known_issues(alerts, comps, target)
            text = "\n".join(f"{i.get('integration')}: {i.get('title')}" for i in issues) if issues else \
                "no open alert for this version and these integrations"
            out.append({"name": "known issues feed", "text": plain(text)})
    return out[:MAX_SOURCES], reads


def request_body(row: dict, sources: list[dict]) -> dict:
    """The v2 request with ``kind: update`` (Companion 0.2.0): what is updated, this App's own review, the notes."""
    reasons = [I.scrub(str(r), 160) for r in (row.get("reasons") or [])][:4]
    return {"schema": FL.REQUEST_SCHEMA_V2, "key": item_key(str(row.get("item") or "")), "kind": "update",
            "title": I.scrub(f"Update {row.get('name')} {row.get('from')} -> {row.get('to')}", 160),
            "facts": [f"this App's review: {I.scrub(str(row.get('verdict') or 'none'), 20)}", *reasons][:FL.MAX_FACTS],
            "earlier": None, "tier": TIER, "sources": sources,
            "context": {"family": "update", "repeats": 0, "after_update": "", "protected": False}}


def parse_result(status: int, body) -> dict:
    """The Companion's update answer in a closed shape, or {"error": reason}."""
    if not isinstance(body, dict):
        return {"error": f"bad_answer_{status}"}
    if status != 200 or body.get("ok") is not True:
        reason = str(body.get("reason") or f"http_{status}")
        return {"error": re.sub(r"[^A-Za-z0-9_]", "", reason)[:40] or f"http_{status}",
                "shape": FL.closed_shape(body.get("shape"))}
    if body.get("schema") not in FL.RESULT_SCHEMAS or body.get("verdict") not in VERDICTS:
        return {"error": "bad_answer_shape"}
    neurons = body.get("neurons")
    return {"verdict": body["verdict"], "summary": I.scrub(str(body.get("summary") or ""), 200),
            "steps": [str(s)[:120] for s in body.get("steps") or [] if isinstance(s, str) and s][:3],
            "model": re.sub(r"[^a-z0-9.-]", "", str(body.get("model") or "")[:40]) or "free-model",
            "model_id": re.sub(r"[^A-Za-z0-9@/._-]", "", str(body.get("model_id") or "")[:80]),
            "neurons": int(neurons) if isinstance(neurons, int) and 0 <= neurons <= 100000 else 0}


def post_text(reviews: list[tuple[dict, dict]], version: str) -> str:
    """One comment per run: the internal verdicts, each with its own machine line (SCHEMA, never the owner's)."""
    models = dict.fromkeys(FL.MODEL_WORDS.get(str(r.get("model") or ""), str(r.get("model") or "free model"))
                           for _, r in reviews)
    lines = ["### House Brain Maintenance: internal update review", "",
             f"The cloud reviewer has not answered, so these updates' release notes were read by the free model "
             f"({' / '.join(models)}, through the House Brain Companion on Workers AI's free plan; $0). This is advice "
             "only: nothing installs by itself on an internal review, your phone is always asked, and a cloud or "
             "owner review of the same update replaces it.", ""]
    machine = []
    for row, r in reviews:
        lines.append(f"- **{I.scrub(str(row.get('name')), 60)} `{I.scrub(str(row.get('from')), 40)}` → "
                     f"`{I.scrub(str(row.get('to')), 40)}`**: **{WORDS[r['verdict']]}**: "
                     f"{r.get('summary', '')} _(sources: {', '.join(s['name'] for s in r.get('sources', [])) or 'none'})_")
        lines += [f"  - {s}" for s in r.get("steps") or []]
        machine.append(f"<!-- {SCHEMA} " + json.dumps({"item": row.get("item"), "verdict": r["verdict"],
                                                       "summary": r.get("summary", ""), "model": r.get("model", ""),
                                                       "neurons": int(r.get("neurons") or 0),
                                                       "sources": [s["name"] for s in r.get("sources", [])]},
                                                      separators=(",", ":")) + " -->")
    lines += ["", f"- **App:** `{version}`", "",
              "_Posted by House Brain Maintenance (machine-generated; repository content is untrusted data)._", ""]
    # the visible text is scrubbed; the machine lines carry the item ids as the waiting list wrote them (an
    # ``app:<slug>@<version>`` item would otherwise read as an e-mail address)
    return I.scrub("\n".join(lines), 16000) + "\n".join(machine)[:4000]


class UpdateLane:
    """One tick per update check: review the waiting updates that have no AI review, when the lane is active."""

    def __init__(self, journal, free_lane, ai, ha, upgrade_info, report, lockout, clock=time.time) -> None:
        self.j, self.free_lane, self.ai, self.ha, self.upgrade_info = journal, free_lane, ai, ha, upgrade_info
        self.report, self.lockout, self.clock = report, lockout, clock

    def _load(self) -> dict:
        return _doc(self.j.load_doc(DOC, {}))

    def active(self) -> bool:
        if self.free_lane is None or not self.free_lane.url or self.free_lane.mode not in ("lockout", "primary"):
            return False
        if self.ai is None or not self.ai.enabled:
            return False
        if self.free_lane.mode == "primary":
            return True
        try:
            return bool(self.lockout())
        except Exception:  # noqa: BLE001 - an unreadable lane state means "not silent"
            return False

    def pending(self, item: str) -> bool:
        """0.9.51: may this item still get an internal review before the phone is asked without any review? True
        while the lane is active, the item has an allowlisted source kind and the lane has not looked at it yet."""
        if not isinstance(item, str) or not RE_ITEM.fullmatch(item) or not self.active():
            return False
        doc = self._load()
        return item not in doc["done"] and item not in doc["tried"]

    def status(self) -> dict:
        doc = roll_day(self._load(), self.clock())
        return {"reviews_today": doc["reviews"], "reads_today": doc["reads"], "done": len(doc["done"]),
                "active": self.active(), "last_reason": doc["last_reason"]}

    def cycle(self, rows: list[dict], version: str, dry_run: bool = False) -> list[str]:
        """Review at most MAX_PER_RUN waiting updates without an AI review. Returns the items reviewed."""
        now = self.clock()
        doc = roll_day(self._load(), now)
        done_items = [k for k in doc["done"] if any(r.get("item") == k for r in rows)]
        doc["done"] = {k: v for k, v in doc["done"].items() if k in done_items or now - float(v.get("at") or 0) < 30 * 86400}
        doc["skip"] = {k: t for k, t in doc["skip"].items() if t > now}
        doc["tried"] = {k: t for k, t in doc["tried"].items() if now - float(t) < 30 * 86400}
        if not self.active() or dry_run or now - float(doc["ran_at"] or 0) < RUN_EVERY:
            self.j.save_doc(DOC, doc)
            return []
        reviewed: list[tuple[dict, dict]] = []
        eligible = [r for r in rows if r.get("kind") in KINDS and RE_ITEM.fullmatch(str(r.get("item") or ""))]
        if not eligible:                                          # nothing to read: the next check may bring work
            self.j.save_doc(DOC, doc)
            return []
        doc["ran_at"] = now
        for row in eligible:
            item = str(row.get("item") or "")
            if len(reviewed) >= MAX_PER_RUN:
                break
            if self.ai.get(item) is not None or item in doc["done"] or item in doc["skip"]:
                doc["tried"].setdefault(item, now)
                continue
            if str(row.get("state") or "").startswith("held"):
                doc["tried"].setdefault(item, now)
                continue                                          # this App's review or the hold list already said no
            if not budget_left(doc):
                doc["last_reason"] = "daily_budget_used"
                break
            fdoc = FL.roll_day(self.free_lane._load(), now)
            if not FL.budget_left(fdoc, TIER):
                doc["last_reason"] = "lane_budget_used"
                break
            doc["tried"][item] = now
            sources, reads = sources_for(row, self.ha, self.upgrade_info)
            doc["reads"] += reads
            if not sources:
                doc["skip"][item] = now + 24 * 3600
                doc["last_reason"] = "no_source"
                self.j.audit("update_lane", "INTERNAL_UPDATE_REVIEW_SKIPPED", item=item, reason="no_source")
                continue
            doc["reviews"] += 1
            fdoc["calls"] += 1
            out = self.ask(row, sources)
            if "error" in out:
                doc["last_reason"] = out["error"]
                self.j.audit("update_lane", "INTERNAL_UPDATE_REVIEW_FAILED", item=item, reason=out["error"])
                self.free_lane.j.save_doc(FL.DOC, fdoc)
                if out["error"].startswith("bad_answer"):
                    doc["skip"][item] = now + FL.RETRY_HOURS * 3600
                    continue
                break                                             # refused: nothing more this run
            fdoc["neurons"] += out["neurons"]
            fdoc["neurons_easy"] += out["neurons"]
            self.free_lane.j.save_doc(FL.DOC, fdoc)
            doc["last_reason"] = "ok"
            rec = {**out, "sources": [{"name": s["name"]} for s in sources], "at": now}
            doc["done"][item] = {"verdict": out["verdict"], "at": now, "model": out["model"], "neurons": out["neurons"]}
            self.ai.set_internal(item, out["verdict"], out["summary"], out["steps"], out["model"], now)
            self.j.audit("update_lane", "INTERNAL_UPDATE_REVIEW", item=item, verdict=out["verdict"],
                         neurons=out["neurons"], sources=[s["name"] for s in sources])
            reviewed.append((row, rec))
        if doc["last_reason"] in ("daily_budget_used", "lane_budget_used") or (
                reviewed == [] and doc["last_reason"] not in ("ok", "no_source", "") and not doc["last_reason"].startswith("bad_answer")):
            for row in eligible:                                  # no review today for the rest: the 48 h path stays
                doc["tried"].setdefault(str(row.get("item") or ""), now)
        self.j.save_doc(DOC, doc)
        if reviewed:
            try:
                self.report(post_text(reviewed, version))
            except Exception as err:  # noqa: BLE001 - the stored verdicts stand; the post is a record
                self.j.audit("update_lane", "INTERNAL_UPDATE_REVIEW_POST_FAILED", reason=str(err)[:120])
        return [row.get("item") for row, _ in reviewed]

    def ask(self, row: dict, sources: list[dict]) -> dict:
        status, body = self.free_lane.call(request_body(row, sources))
        if body is None:
            return {"error": f"http_{status}"}
        return parse_result(status, body)
