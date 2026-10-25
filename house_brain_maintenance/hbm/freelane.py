"""0.9.29 Free reviewer lane (reviewer session message 2026-10-24 14:30Z; owner pop-ups 2026-10-24 "Go straight to
lockout mode", "Companion review route", "Its Broker key"; design ``docs/architecture/HOUSE_BRAIN_FREE_REVIEWER_LANE_R1.md``).

Live finding: from 2026-10-20 to 2026-10-24 the owner's weekly Claude limit was used up and no scheduled reviewer run
reviewed anything; 29 problems and 6 updates piled up. A cloud-only reviewer cannot "run itself". This lane keeps the
low-risk problems reviewed for free:

* **Drafts.** For each alerted open problem of a low-risk kind without a counting cloud review, this App asks the
  House Brain Companion's review route (Gemma 4 26B on Workers AI's free plan; the App's own Broker rotation key is
  its identity) for one verdict: WATCH, IGNORE or ESCALATE. The draft is stored, never pushed to the phone.
* **Shadow.** When the cloud review of the same problem arrives, the two verdicts are compared and the agreement is
  recorded per kind (the weekly list shows it).
* **Lockout mode.** When no cloud review has appeared for ``LOCKOUT_HOURS``, the drafts for low-risk problems are
  posted on the tracking issue as ordinary review lines (WATCH and IGNORE only; ESCALATE is never posted), marked
  "Reviewer: free model", so the queue, the Messages page and the weekly list show them. A later cloud review of the
  same problem wins (the newest review of an item counts). An unchanged problem that clears and comes back keeps its
  cloud review by the 0.9.19 fingerprint rule, so it costs nothing here; only changed or new problems are drafted.
* **Never:** FIX_NOW, OWNER_ACTION, an update review (APPROVE/HOLD/DECLINE), anything CRITICAL, a problem with a
  built-in fix, or a logger about firmware, Core, the OS, backups, safety or a radio network (Zigbee, Z-Wave, Thread,
  Matter, Bluetooth, ESPHome). Posting stops by itself when the lane disagrees with the cloud reviewer too often.

Cost: at most ``DAILY_NEURONS`` Neurons (the Companion enforces 3,000 too) and ``DAILY_CALLS`` calls a day; at most
one lockout post every ``POST_EVERY_HOURS``. $0: Workers Free fails closed past its allowance and cannot bill.
``CONTROL_AUTHORITY=NONE``: a verdict is text; fixes still need the owner's Approve.
"""
from __future__ import annotations

import json
import re
import time

from . import issues as I, net, problems as PR

DOC = "free_reviews"
SCHEMA = "house_brain_free_review.v1"                 # the marker line on a lockout post
REQUEST_SCHEMA = "house_brain_free_review_request.v1"
RESULT_SCHEMA = "house_brain_free_review_result.v1"
MODES = ("off", "shadow", "lockout")
RE_COMPANION_URL = re.compile(r"^https://house-brain-companion\.([a-z0-9-]{1,63})\.workers\.dev$")
RE_BROKER_BASE = re.compile(r"^https://house-brain-maintenance-broker\.([a-z0-9-]{1,63})\.workers\.dev$")
PROTECTED = ("zha", "zwave", "zigbee", "thread", "matter", "otbr", "bluetooth", "esphome", "hassio", "supervisor",
             "backup", "recorder", "alarm", "smoke", "co_", "leak", "water", "homeassistant.core", "homeassistant.setup",
             "homeassistant.bootstrap", "update", "firmware", "os_agent")
POSTABLE = ("WATCH", "IGNORE")
VERDICTS = ("WATCH", "IGNORE", "ESCALATE")
LOCKOUT_HOURS = 24.0
RUN_EVERY = 1800.0                 # the lane looks for work at most every 30 minutes
POST_EVERY_HOURS = 6.0
DAILY_NEURONS = 3000
DAILY_CALLS = 60
MAX_PER_RUN = 5
MAX_POST = 10
MAX_DRAFTS = 200
KEEP_DAYS = 14
MIN_COMPARED = 10                  # the lane's own check: below this agreement, lockout posts stop
MIN_AGREEMENT = 0.6
MAX_FACTS = 8
RETRY_HOURS = 6.0                  # 0.9.32: a problem whose answer could not be read is not asked again for this long
TIMEOUT = 60.0
MODEL_WORDS = {"gemma-4-26b": "Gemma 4 26B"}
STATUS_SCHEMA = "house_brain_free_reviewer.v1"       # 0.9.32: the lane's status line on every post (counts, no text)
SHAPE_KEYS = ("answer_chars", "answer_json", "answer_keys", "finish_reason", "completion_tokens", "json_mode")


def closed_shape(raw) -> dict:
    """0.9.32: the Companion's answer shape (sizes, flags, key names), in a closed form. Never any text."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for k in SHAPE_KEYS:
        v = raw.get(k)
        if k == "answer_keys":
            if isinstance(v, list):
                out[k] = [re.sub(r"[^A-Za-z0-9_]", "", str(x))[:24] for x in v[:8] if isinstance(x, str)]
        elif k == "finish_reason":
            if isinstance(v, str):
                out[k] = re.sub(r"[^A-Za-z0-9_]", "", v)[:24]
        elif isinstance(v, bool) or (isinstance(v, int) and 0 <= v <= 10 ** 7):
            out[k] = v
    return out


def companion_url(value: str, broker_base: str) -> str:
    """The Companion on the same Cloudflare account as the Broker ("" = unknown), unless an explicit address is set."""
    if value == "off":
        return ""
    if value and RE_COMPANION_URL.fullmatch(value):
        return value
    m = RE_BROKER_BASE.fullmatch(broker_base or "")
    return f"https://house-brain-companion.{m.group(1)}.workers.dev" if m else ""


def low_risk(key: str, rec: dict) -> bool:
    """The lane's closed definition of a low-risk problem: a non-critical log error from an ordinary integration,
    without a built-in fix. Everything else is for the cloud reviewer and the owner."""
    if not isinstance(rec, dict) or not PR.RE_KEY.fullmatch(key or "") or key == PR.STALL_KEY:
        return False
    if not key.startswith("log:"):
        return False
    if rec.get("severity") == I.CRITICAL or rec.get("fix"):
        return False
    low = key.lower()
    return not any(p in low for p in PROTECTED)


def day_of(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now))


def _doc(raw) -> dict:
    doc = raw if isinstance(raw, dict) else {}
    out = {"drafts": doc.get("drafts") if isinstance(doc.get("drafts"), dict) else {},
           "day": str(doc.get("day") or ""), "neurons": int(doc.get("neurons") or 0), "calls": int(doc.get("calls") or 0),
           "agreement": doc.get("agreement") if isinstance(doc.get("agreement"), dict) else {},
           "ran_at": float(doc.get("ran_at") or 0), "posted_at": float(doc.get("posted_at") or 0),
           "last_reason": str(doc.get("last_reason") or ""), "last_shape": closed_shape(doc.get("last_shape")),
           "last_at": float(doc.get("last_at") or 0),
           "skip": {str(k): float(v) for k, v in (doc.get("skip") or {}).items() if isinstance(v, (int, float))}
           if isinstance(doc.get("skip"), dict) else {}}
    out["drafts"] = {k: v for k, v in out["drafts"].items() if isinstance(v, dict) and PR.RE_KEY.fullmatch(str(k))}
    return out


def roll_day(doc: dict, now: float) -> dict:
    if doc["day"] != day_of(now):
        doc.update(day=day_of(now), neurons=0, calls=0)
    return doc


def budget_left(doc: dict) -> bool:
    return doc["neurons"] < DAILY_NEURONS and doc["calls"] < DAILY_CALLS


def candidates(open_recs: dict, reviews, now: float) -> list[tuple[str, dict]]:
    """Low-risk alerted open problems without a counting cloud review, oldest first."""
    out = []
    for key, rec in open_recs.items():
        if not isinstance(rec, dict) or not rec.get("alerted") or not low_risk(key, rec):
            continue
        if reviews.cloud(key) is not None:
            continue
        out.append((key, rec))
    return sorted(out, key=lambda kv: float(kv[1].get("first") or 0))


def cloud_lockout(cloud_at: float | None, now: float) -> bool:
    """No cloud review (an update or a problem review not posted by this lane) for LOCKOUT_HOURS. Unknown = no."""
    return cloud_at is not None and now - float(cloud_at) >= LOCKOUT_HOURS * 3600


def agreement(doc: dict) -> dict:
    """Per kind {agree, disagree}; the whole as ``_all``."""
    out = {"_all": {"agree": 0, "disagree": 0}}
    for kind, v in doc["agreement"].items():
        if isinstance(v, dict):
            a, d = int(v.get("agree") or 0), int(v.get("disagree") or 0)
            out[kind] = {"agree": a, "disagree": d}
            out["_all"]["agree"] += a
            out["_all"]["disagree"] += d
    return out


def trusted(doc: dict) -> bool:
    """Lockout posting goes on unless the lane is known to disagree with the cloud reviewer too often."""
    a = agreement(doc)["_all"]
    n = a["agree"] + a["disagree"]
    return n < MIN_COMPARED or a["agree"] / n >= MIN_AGREEMENT


def compare(doc: dict, reviews, now: float) -> dict:
    """Shadow: a cloud review that arrived for a drafted problem is compared once with the draft (same facts)."""
    for key, d in doc["drafts"].items():
        if d.get("compared"):
            continue
        cloud = reviews.cloud(key)                                     # counts only after the facts last changed
        if cloud is None:
            continue
        kind = str(d.get("kind") or "other")
        slot = doc["agreement"].setdefault(kind, {"agree": 0, "disagree": 0})
        same = cloud.get("verdict") == d.get("verdict") or (d.get("verdict") == "ESCALATE"
                                                            and cloud.get("verdict") in ("FIX_NOW", "OWNER_ACTION"))
        slot["agree" if same else "disagree"] = int(slot.get("agree" if same else "disagree") or 0) + 1
        d.update(compared=True, cloud_verdict=cloud.get("verdict"), agree=bool(same))
    return doc


def prune(doc: dict, open_recs: dict, now: float) -> dict:
    keep = {}
    for key, d in doc["drafts"].items():
        if now - float(d.get("at") or 0) > KEEP_DAYS * 86400:
            continue
        if key not in open_recs and not d.get("compared") and now - float(d.get("at") or 0) > 3 * 86400:
            continue
        keep[key] = d
    if len(keep) > MAX_DRAFTS:
        for old in sorted(keep, key=lambda k: float(keep[k].get("at") or 0))[:len(keep) - MAX_DRAFTS]:
            del keep[old]
    doc["drafts"] = keep
    return doc


def request_body(key: str, rec: dict, earlier: dict | None) -> dict:
    facts = [I.scrub(str(d), 400) for d in (rec.get("detail") or [])][:MAX_FACTS]
    return {"schema": REQUEST_SCHEMA, "key": key, "kind": str(rec.get("source") or "other")[:24],
            "title": I.scrub(str(rec.get("title") or key), 160), "facts": facts,
            "earlier": {"verdict": earlier["verdict"], "summary": I.scrub(str(earlier.get("summary") or ""), 200)}
            if isinstance(earlier, dict) and earlier.get("verdict") in PR.VERDICTS else None}


def parse_result(status: int, body) -> dict:
    """The Companion's answer in a closed shape, or {"error": reason}."""
    if not isinstance(body, dict):
        return {"error": f"bad_answer_{status}"}
    if status != 200 or body.get("ok") is not True:
        reason = str(body.get("reason") or f"http_{status}")
        return {"error": re.sub(r"[^A-Za-z0-9_]", "", reason)[:40] or f"http_{status}", "shape": closed_shape(body.get("shape"))}
    if body.get("schema") != RESULT_SCHEMA or body.get("verdict") not in VERDICTS:
        return {"error": "bad_answer_shape"}
    steps = [str(s)[:120] for s in body.get("steps") or [] if isinstance(s, str) and s][:3]
    neurons = body.get("neurons")
    return {"verdict": body["verdict"], "summary": I.scrub(str(body.get("summary") or ""), 200), "steps": steps,
            "model": re.sub(r"[^a-z0-9.-]", "", str(body.get("model") or "")[:40]) or "free-model",
            "neurons": int(neurons) if isinstance(neurons, int) and 0 <= neurons <= 100000 else 0}


def post_text(drafts: list[tuple[str, dict, dict]], version: str, lockout_since_h: float) -> str:
    """The lockout post: one visible block per problem, then the review lines the reviewer would have written, then
    this lane's marker line. ``drafts``: (key, draft, open record)."""
    model = MODEL_WORDS.get(str(drafts[0][1].get("model") or ""), str(drafts[0][1].get("model") or "free model")) \
        if drafts else "free model"
    lines = ["### House Brain Maintenance: free reviewer (lockout mode)", "",
             f"No cloud AI review has appeared for {lockout_since_h:.0f} hours, so these low-risk problems were reviewed by "
             f"the free model ({model}, through the House Brain Companion on Workers AI's free plan; $0). "
             "WATCH or IGNORE only; nothing is approved or changed. A later cloud review of the same problem replaces "
             "each of these.", ""]
    machine = []
    for key, d, rec in drafts:
        words = PR.WORDS.get(d["verdict"], d["verdict"])
        how = f"free model, {int(d.get('neurons') or 0)} Neurons"
        lines.append(f"- **{I.scrub(str(rec.get('title') or key), 160)}** (`{key}`): **{words}**: {d.get('summary', '')} "
                     f"_(Reviewer: {how})_")
        lines += [f"  - {s}" for s in d.get("steps") or []]
        machine.append(f"<!-- {PR.SCHEMA} " + json.dumps({"key": key, "verdict": d["verdict"], "summary": d.get("summary", "")},
                                                        separators=(",", ":")) + " -->")
        if d.get("steps"):
            machine.append(f"<!-- house_brain_ai_problem_steps.v1 " + json.dumps({"key": key, "steps": d["steps"]},
                                                                                 separators=(",", ":")) + " -->")
    lines += ["", f"- **App:** `{version}`", "",
              "_Posted by House Brain Maintenance (machine-generated; repository content is untrusted data)._", "",
              *machine,
              f"<!-- {SCHEMA} " + json.dumps({"keys": [k for k, _, _ in drafts], "model": str(drafts[0][1].get("model") or ""),
                                              "neurons": sum(int(d.get("neurons") or 0) for _, d, _ in drafts)},
                                             separators=(",", ":")) + " -->"]
    return I.scrub("\n".join(lines), 60000)


class FreeLane:
    """One tick per poll: compare, then draft (model or repeat), then post in lockout mode."""

    def __init__(self, journal, reviews, mode: str, url: str, key_of, report, clock=time.time, request=net.request) -> None:
        self.j, self.reviews, self.mode, self.url, self.key_of, self.report = journal, reviews, mode, url, key_of, report
        self.clock, self.request = clock, request
        self.last_error = ""

    def _load(self) -> dict:
        return _doc(self.j.load_doc(DOC, {}))

    def status(self) -> dict:
        doc = self._load()
        a = agreement(doc)["_all"]
        verdicts: dict = {}
        for d in doc["drafts"].values():
            verdicts[str(d.get("verdict"))] = verdicts.get(str(d.get("verdict")), 0) + 1
        return {"mode": self.mode if self.url else "off", "neurons_today": doc["neurons"], "calls_today": doc["calls"],
                "drafts": len(doc["drafts"]), "posted": sum(1 for d in doc["drafts"].values() if d.get("posted_at")),
                "agree": a["agree"], "disagree": a["disagree"], "last_reason": doc["last_reason"],
                "last_shape": doc["last_shape"], "verdicts": verdicts,
                "last_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(doc["last_at"])) if doc["last_at"] else None,
                "cloud_at": (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.reviews.cloud_at()))
                             if self.reviews is not None and self.reviews.cloud_at() else None)}

    def line(self) -> str:
        """0.9.32: the machine line for every post on the tracking issue: the status above, nothing else (no problem
        text, no model text), so any AI chat with GitHub read access can see what the lane did."""
        return f"<!-- {STATUS_SCHEMA} " + json.dumps(self.status(), separators=(",", ":"), sort_keys=True) + " -->"

    def digest_line(self) -> str:
        doc = self._load()
        a = agreement(doc)["_all"]
        n = a["agree"] + a["disagree"]
        rate = f"{100 * a['agree'] / n:.0f}%" if n else "not yet measured"
        posted = sum(1 for d in doc["drafts"].values() if d.get("posted_at"))
        return (f"Free reviewer ({self.mode}): {len(doc['drafts'])} drafts kept, {n} compared with cloud reviews "
                f"(agreement {rate}), {posted} posted in lockout mode.")

    # -- one call --------------------------------------------------------------------------------------------------
    def ask(self, key: str, rec: dict, earlier: dict | None) -> dict:
        token = self.key_of()
        if not token:
            return {"error": "no_rotation_key"}
        try:
            status, body = self.request("POST", f"{self.url}/v1/review", {"Authorization": f"Bearer {token}"},
                                        request_body(key, rec, earlier), timeout=TIMEOUT)
        except net.NetError as err:
            status, body = err.status, None
            try:
                body = json.loads(str(err).split(": ", 1)[1]) if ": " in str(err) else None
            except ValueError:
                body = None
            if not isinstance(body, dict):
                return {"error": f"http_{status}"}
        return parse_result(status, body)

    # -- the tick --------------------------------------------------------------------------------------------------
    def cycle(self, open_recs: dict, version: str, dry_run: bool = False) -> dict:
        now = self.clock()
        doc = roll_day(self._load(), now)
        if self.mode == "off" or not self.url or self.reviews is None or not self.reviews.enabled:
            return doc
        doc = prune(compare(doc, self.reviews, now), open_recs, now)
        if now - doc["ran_at"] >= RUN_EVERY:
            doc["ran_at"] = now
            doc = self._draft(doc, open_recs, now)
        if self.mode == "lockout" and not dry_run:
            doc = self._post(doc, open_recs, version, now)
        self.j.save_doc(DOC, doc)
        return doc

    def _draft(self, doc: dict, open_recs: dict, now: float) -> dict:
        made = 0
        doc["skip"] = {k: t for k, t in doc["skip"].items() if t > now}
        for key, rec in candidates(open_recs, self.reviews, now):
            if made >= MAX_PER_RUN:
                break
            fp = PR.fingerprint(str(rec.get("title") or key))
            d = doc["drafts"].get(key)
            if d is not None and d.get("fp") == fp:
                continue                                                # drafted already for these facts
            if key in doc["skip"]:
                continue                                                # 0.9.32: its last answer was unreadable; later
            earlier = self.reviews.cloud_before(key)                # the cloud's older word on this key, for context
            if not budget_left(doc):
                doc["last_reason"] = "daily_budget_used"
                break
            doc["calls"] += 1
            out = self.ask(key, rec, earlier)
            doc["last_at"] = now
            if "error" in out:
                doc["last_reason"] = out["error"]
                doc["last_shape"] = out.get("shape") or {}                 # 0.9.32: what came back, never the text
                self.j.audit("free_review", "FREE_REVIEW_FAILED", key=key, reason=out["error"])
                if out["error"].startswith("bad_answer") or out["error"] == "REVIEW_UNPARSED":
                    doc["skip"][key] = now + RETRY_HOURS * 3600         # this one problem: not again for a while
                    continue                                            # try the next
                break                                                   # the Companion said no: nothing more this run
            doc["neurons"] += out["neurons"]
            doc["last_reason"] = "ok"
            doc["last_shape"] = {}
            doc["drafts"][key] = {"fp": fp, "kind": str(rec.get("source") or "other"), "verdict": out["verdict"],
                                  "summary": out["summary"], "steps": out["steps"], "model": out["model"],
                                  "neurons": out["neurons"], "at": now, "source": "model"}
            self.j.audit("free_review", "FREE_REVIEW_DRAFTED", key=key, verdict=out["verdict"], neurons=out["neurons"])
            made += 1
        return doc

    def _post(self, doc: dict, open_recs: dict, version: str, now: float) -> dict:
        cloud_at = self.reviews.cloud_at()
        if not cloud_lockout(cloud_at, now) or now - doc["posted_at"] < POST_EVERY_HOURS * 3600 or not trusted(doc):
            return doc
        due = []
        for key, d in sorted(doc["drafts"].items(), key=lambda kv: float(kv[1].get("at") or 0)):
            rec = open_recs.get(key)
            if d.get("posted_at") or d.get("verdict") not in POSTABLE or not isinstance(rec, dict):
                continue
            if not low_risk(key, rec) or self.reviews.cloud(key) is not None:
                continue
            if PR.fingerprint(str(rec.get("title") or key)) != d.get("fp"):
                continue                                                # the facts changed since the draft
            due.append((key, d, rec))
        if not due:
            return doc
        due = due[:MAX_POST]
        url = self.report(post_text(due, version, (now - float(cloud_at)) / 3600))
        for key, d, _ in due:
            d["posted_at"] = now
            d["url"] = url if isinstance(url, str) else ""
        doc["posted_at"] = now
        self.j.audit("free_review", "FREE_REVIEW_POSTED", items=len(due), lockout_hours=round((now - float(cloud_at)) / 3600, 1))
        return doc
