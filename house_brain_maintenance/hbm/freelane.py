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

0.9.50 (the reviewer chat's spec R1 B2/B4/B5/B6 and D7; owner D1/D7 2026-10-31/11-01, pop-up 2026-11-02 "Yes, build
0.9.50 as proposed"; Companion 0.2.0 live 2026-11-02):
* **Tiers.** The request is ``house_brain_free_review_request.v2`` with a ``tier``: ``hard`` (Qwen3.8 27B through
  the Companion) for App crashes, integration failures, backups, the radio families and repeats the rules could not
  settle; ``everyday`` (Gemma) for the low-risk log errors as before. A hard problem is ``protected`` (never IGNORE)
  and the hard tier may answer OWNER_ACTION.
* **Budget split.** Gemma ≤ DAILY_NEURONS_EASY, Qwen ≤ DAILY_NEURONS_HARD (together the 3,000 lane), at most
  MAX_HARD_PER_RUN Qwen call a run; a refusal or a spent tier means ``rules_only`` for the rest of the day (the
  status line says so).
* **Trust per family.** Agreement with the cloud reviewer is counted per ``<model>:<family>`` too; a model's
  OWNER_ACTION is kept (and may be posted) only for a trusted family (≥ TRUST_MIN_COMPARED compared at
  ≥ TRUST_MIN_AGREEMENT); otherwise it becomes ESCALATE. The lane-wide ``trusted()`` check stays.
* **needs_you.** Every ESCALATE (and every untrusted OWNER_ACTION) goes on the ``needs_you`` list: one owner line in
  the morning summary, one machine line ``house_brain_needs_you.v1`` on the lane's posts and the summary, never a
  review; the reviewer watchdog does not count those as stalled. An item leaves when it clears, when its facts change
  (it is drafted again) or when the owner or the cloud reviews it.
* **D7 stall.** ``reviewer:stalled`` fires when an item is neither reviewed (cloud, owner, rule or internal) nor on
  needs_you after 26 h, or when the internal reviewer has not succeeded for INTERNAL_STALL_HOURS while it had work.
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
REQUEST_SCHEMA_V2 = "house_brain_free_review_request.v2"      # 0.9.50: tier, sources, context (Companion 0.2.0)
RESULT_SCHEMA_V2 = "house_brain_free_review_result.v2"
RESULT_SCHEMAS = (RESULT_SCHEMA, RESULT_SCHEMA_V2)
TIERS = ("everyday", "hard")
HARD_PREFIXES = ("app:", "entry:", "backup:")                   # spec B2: App crashes, integration failures, backups
RADIO_MARKERS = ("zha", "zwave", "z-wave", "zigbee", "thread", "matter", "otbr", "bluetooth", "esphome", "deconz")
DAILY_NEURONS_EASY = 1200
DAILY_NEURONS_HARD = 1800
MAX_HARD_PER_RUN = 1
TRUST_MIN_COMPARED = 10
TRUST_MIN_AGREEMENT = 0.8
NEEDS_YOU_SCHEMA = "house_brain_needs_you.v1"
MAX_NEEDS_YOU = 50
MAX_NEEDS_YOU_NAMED = 3
INTERNAL_STALL_HOURS = 6.0
MODES = ("off", "shadow", "lockout", "primary")     # 0.9.42: primary (owner D1 2026-10-31) posts without the 24 h wait
POST_EVERY_HOURS_PRIMARY = 1.0                       # spec R1 B1: at most one comment an hour
RULE_POSTABLE = ("WATCH", "IGNORE", "OWNER_ACTION")  # what a rule verdict may post (owner D2 2026-10-31)
RE_COMPANION_URL = re.compile(r"^https://house-brain-companion\.([a-z0-9-]{1,63})\.workers\.dev$")
RE_BROKER_BASE = re.compile(r"^https://house-brain-maintenance-broker\.([a-z0-9-]{1,63})\.workers\.dev$")
PROTECTED = ("zha", "zwave", "zigbee", "thread", "matter", "otbr", "bluetooth", "esphome", "hassio", "supervisor",
             "backup", "recorder", "alarm", "smoke", "co_", "leak", "water", "homeassistant.core", "homeassistant.setup",
             "homeassistant.bootstrap", "update", "firmware", "os_agent")
POSTABLE = ("WATCH", "IGNORE", "OWNER_ACTION")      # 0.9.50: OWNER_ACTION is stored only for a trusted family
VERDICTS = ("WATCH", "IGNORE", "ESCALATE", "OWNER_ACTION")
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
TIMEOUT = 150.0                    # 0.9.38: was 60; Companion 0.1.7 may ask once more with 3,000 tokens of room
MODEL_WORDS = {"gemma-4-26b": "Gemma 4 26B", "qwen3.8-27b": "Qwen3.8 27B"}
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
           if isinstance(doc.get("skip"), dict) else {},
           "posted_rules": {str(k): float(v) for k, v in (doc.get("posted_rules") or {}).items()     # 0.9.42
                            if isinstance(v, (int, float))} if isinstance(doc.get("posted_rules"), dict) else {},
           # 0.9.50: the tier split, the trust table, the needs_you list, the last success and the rules-only mark
           "neurons_easy": int(doc.get("neurons_easy") or 0), "neurons_hard": int(doc.get("neurons_hard") or 0),
           "trust": doc.get("trust") if isinstance(doc.get("trust"), dict) else {},
           "needs_you": {}, "ok_at": float(doc.get("ok_at") or 0), "rules_only_since": float(doc.get("rules_only_since") or 0)}
    out["drafts"] = {k: v for k, v in out["drafts"].items() if isinstance(v, dict) and PR.RE_KEY.fullmatch(str(k))}
    raw_ny = doc.get("needs_you") if isinstance(doc.get("needs_you"), dict) else {}
    for k, v in list(raw_ny.items())[:MAX_NEEDS_YOU]:
        if isinstance(v, dict) and PR.RE_KEY.fullmatch(str(k)):
            out["needs_you"][str(k)] = {"at": float(v.get("at") or 0), "title": str(v.get("title") or "")[:120],
                                        "why": str(v.get("why") or "")[:24], "fp": str(v.get("fp") or "")[:64]}
    out["trust"] = {str(k): {"agree": int(v.get("agree") or 0), "disagree": int(v.get("disagree") or 0)}
                    for k, v in out["trust"].items() if isinstance(v, dict)}
    return out


def roll_day(doc: dict, now: float) -> dict:
    if doc["day"] != day_of(now):
        doc.update(day=day_of(now), neurons=0, calls=0, neurons_easy=0, neurons_hard=0, rules_only_since=0.0)
    return doc


def budget_left(doc: dict, tier: str = "everyday") -> bool:
    """0.9.50: the day's lane (3,000 and 60 calls) and the tier's own share (Gemma 1,200 / Qwen 1,800)."""
    if doc["neurons"] >= DAILY_NEURONS or doc["calls"] >= DAILY_CALLS:
        return False
    if tier == "hard":
        return doc["neurons_hard"] < DAILY_NEURONS_HARD
    return doc["neurons_easy"] < DAILY_NEURONS_EASY


def radio(key: str, rec: dict) -> bool:
    text = (str(key) + " " + str(((rec or {}).get("facts") or {}).get("logger") if isinstance((rec or {}).get("facts"), dict) else "")
            + " " + str((rec or {}).get("title") or "")).lower()
    return any(m in text for m in RADIO_MARKERS)


def tier_for(key: str, rec: dict, repeats: int = 0) -> str | None:
    """0.9.50 (spec B2): which tier reviews this open problem, or None (not the lane's). ``hard`` for App crashes,
    integration failures, backups, the radio families and repeats (R3's count) the rules could not settle; ``everyday``
    for the low-risk log errors; never the watchdog, a blip's or re-login's child, a re-login (rule R9) or a retired
    device. A hard problem may be CRITICAL or carry a fix: the verdict is text, the fix still asks or self-heals."""
    if not isinstance(rec, dict) or not PR.RE_KEY.fullmatch(key or "") or key == PR.STALL_KEY:
        return None
    if rec.get("parent") or rec.get("reauth"):
        return None
    if key.startswith(("net:", "offline:", "battery:", "reviewer:", "sup:")):
        return None                                                     # the rules' (R1/R2/R6/R7), the Supervisor's, the watchdog
    if low_risk(key, rec):
        return "everyday"
    if key.startswith(HARD_PREFIXES) or radio(key, rec):
        return "hard"
    if repeats >= 3:
        return "hard"
    return None


def protected_key(key: str, rec: dict) -> bool:
    """A hard-tier problem the model may never call harmless (spec B2: WATCH, OWNER_ACTION or ESCALATE only)."""
    return key.startswith(HARD_PREFIXES) or radio(key, rec) or any(p in str(key).lower() for p in PROTECTED)


def family_of(key: str, rec: dict) -> str:
    from . import rules as RU
    return RU.family(key, rec)


def trust_key(model: str, family: str) -> str:
    return f"{model or 'free-model'}:{family or 'other'}"


def trusted_family(doc: dict, model: str, family: str) -> bool:
    """0.9.50 (spec B6): at least TRUST_MIN_COMPARED compared and TRUST_MIN_AGREEMENT agreement for this model and
    family; a new model id starts from zero (old counters are kept)."""
    t = doc["trust"].get(trust_key(model, family)) or {}
    n = int(t.get("agree") or 0) + int(t.get("disagree") or 0)
    return n >= TRUST_MIN_COMPARED and int(t.get("agree") or 0) / n >= TRUST_MIN_AGREEMENT


def note_needs_you(doc: dict, key: str, title: str, why: str, fp: str, now: float) -> None:
    """0.9.50 (spec B4): an ESCALATE draft or an untrusted OWNER_ACTION is the owner's to look at; never a review."""
    doc["needs_you"][key] = {"at": float(now), "title": str(title)[:120], "why": str(why)[:24], "fp": str(fp)[:64]}
    if len(doc["needs_you"]) > MAX_NEEDS_YOU:
        for old in sorted(doc["needs_you"], key=lambda k: doc["needs_you"][k]["at"])[:len(doc["needs_you"]) - MAX_NEEDS_YOU]:
            del doc["needs_you"][old]


def prune_needs_you(doc: dict, open_recs: dict, reviews) -> dict:
    """An item leaves needs_you when it clears, when its facts changed (it is drafted again) or when the owner or
    the cloud reviewed it."""
    keep = {}
    for key, it in doc["needs_you"].items():
        rec = open_recs.get(key)
        if not isinstance(rec, dict):
            continue
        if PR.fingerprint(str(rec.get("title") or key)) != it.get("fp"):
            continue
        if reviews is not None and reviews.cloud(key) is not None:
            continue
        keep[key] = it
    doc["needs_you"] = keep
    return doc


def needs_you_line(doc: dict) -> str:
    items = sorted(doc["needs_you"].values(), key=lambda it: it["at"])
    if not items:
        return ""
    names = "; ".join(I.scrub(it["title"], 60) for it in items[:MAX_NEEDS_YOU_NAMED])
    more = f" and {len(items) - MAX_NEEDS_YOU_NAMED} more" if len(items) > MAX_NEEDS_YOU_NAMED else ""
    return f"Needs you ({len(items)}): {names}{more}. The internal reviewer could not settle these; a look from you or the cloud reviewer decides."


def needs_you_machine(doc: dict) -> str:
    return f"<!-- {NEEDS_YOU_SCHEMA} " + json.dumps({"keys": sorted(doc["needs_you"])[:MAX_NEEDS_YOU]}, separators=(",", ":")) + " -->"


def candidates(open_recs: dict, reviews, now: float, recur: dict | None = None) -> list[tuple[str, dict, str]]:
    """Alerted open problems without a counting cloud review that a tier reviews, oldest first: (key, record,
    tier). 0.9.50: the hard tier joins (spec B2); ``recur`` is the 0.9.42 episode doc for the repeat count."""
    from . import rules as RU
    out = []
    for key, rec in open_recs.items():
        if not isinstance(rec, dict) or not rec.get("alerted"):
            continue
        tier = tier_for(key, rec, RU.episodes(recur or {}, key, now) if recur is not None else 0)
        if tier is None:
            continue
        if reviews.cloud(key) is not None:
            continue
        got = reviews.get(key)
        if isinstance(got, dict) and got.get("source") == "rule":                      # 0.9.41: rules first
            continue
        out.append((key, rec, tier))
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
        tslot = doc["trust"].setdefault(trust_key(str(d.get("model") or ""), str(d.get("family") or "other")),   # 0.9.50 B6
                                        {"agree": 0, "disagree": 0})
        tslot["agree" if same else "disagree"] = int(tslot.get("agree" if same else "disagree") or 0) + 1
        d.update(compared=True, cloud_verdict=cloud.get("verdict"), agree=bool(same))
    return doc


def prune(doc: dict, open_recs: dict, now: float) -> dict:
    doc["posted_rules"] = {k: t for k, t in doc.get("posted_rules", {}).items()        # 0.9.42
                           if k in open_recs or now - float(t) <= KEEP_DAYS * 86400}
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


def request_body(key: str, rec: dict, earlier: dict | None, tier: str = "everyday", repeats: int = 0) -> dict:
    """0.9.50: the v2 request (Companion 0.2.0): tier, no fetched sources yet (the update review of 0.9.51 adds
    them), and the context the model may use (family, repeats, the after-update tag, protected)."""
    facts = [I.scrub(str(d), 400) for d in (rec.get("detail") or [])][:MAX_FACTS]
    tag = re.sub(r"[^A-Za-z0-9_:@.+-]", "", str(rec.get("after_update") or ""))[:80]   # an item tag, not an e-mail
    return {"schema": REQUEST_SCHEMA_V2, "key": key, "kind": str(rec.get("source") or "other")[:24],
            "title": I.scrub(str(rec.get("title") or key), 160), "facts": facts,
            "earlier": {"verdict": earlier["verdict"], "summary": I.scrub(str(earlier.get("summary") or ""), 200)}
            if isinstance(earlier, dict) and earlier.get("verdict") in PR.VERDICTS else None,
            "tier": tier if tier in TIERS else "everyday", "sources": [],
            "context": {"family": family_of(key, rec), "repeats": max(0, min(99, int(repeats or 0))),
                        "after_update": tag, "protected": protected_key(key, rec)}}


def parse_result(status: int, body) -> dict:
    """The Companion's answer in a closed shape, or {"error": reason}."""
    if not isinstance(body, dict):
        return {"error": f"bad_answer_{status}"}
    if status != 200 or body.get("ok") is not True:
        reason = str(body.get("reason") or f"http_{status}")
        return {"error": re.sub(r"[^A-Za-z0-9_]", "", reason)[:40] or f"http_{status}", "shape": closed_shape(body.get("shape"))}
    if body.get("schema") not in RESULT_SCHEMAS or body.get("verdict") not in VERDICTS:
        return {"error": "bad_answer_shape"}
    steps = [str(s)[:120] for s in body.get("steps") or [] if isinstance(s, str) and s][:3]
    neurons = body.get("neurons")
    tier = body.get("tier") if body.get("tier") in TIERS else "everyday"
    return {"verdict": body["verdict"], "summary": I.scrub(str(body.get("summary") or ""), 200), "steps": steps,
            "model": re.sub(r"[^a-z0-9.-]", "", str(body.get("model") or "")[:40]) or "free-model",
            "model_id": re.sub(r"[^A-Za-z0-9@/._-]", "", str(body.get("model_id") or "")[:80]),       # 0.9.50
            "tier": tier,
            "neurons": int(neurons) if isinstance(neurons, int) and 0 <= neurons <= 100000 else 0}


def post_text(drafts: list[tuple[str, dict, dict]], version: str, lockout_since_h: float, mode: str = "lockout",
              needs_you: dict | None = None) -> str:
    """The lane's post: one visible block per problem, then the review lines the reviewer would have written, then
    this lane's marker line (0.9.42: with ``sources``, key -> "rule" or "model"). ``drafts``: (key, draft, open
    record); a draft with ``source: rule`` came from ``hbm/rules.py`` (no model call)."""
    models = [str(d.get("model") or "") for _, d, _ in drafts if d.get("source") != "rule"]
    model = " / ".join(dict.fromkeys(MODEL_WORDS.get(m, m or "free model") for m in models)) if models else "free model"
    if mode == "primary":
        lines = ["### House Brain Maintenance: internal reviewer", "",
                 "These problems were reviewed inside the house: by this App's closed rules, or by the free model "
                 f"({model}, through the House Brain Companion on Workers AI's free plan; $0). Nothing is approved or "
                 "changed; a cloud or owner review of the same problem replaces each of these.", ""]
    else:
        lines = ["### House Brain Maintenance: free reviewer (lockout mode)", "",
                 f"No cloud AI review has appeared for {lockout_since_h:.0f} hours, so these low-risk problems were reviewed by "
                 f"the free model ({model}, through the House Brain Companion on Workers AI's free plan; $0), or by this "
                 "App's closed rules. Nothing is approved or changed. A later cloud review of the same problem replaces "
                 "each of these.", ""]
    machine = []
    for key, d, rec in drafts:
        words = PR.WORDS.get(d["verdict"], d["verdict"])
        how = f"rule {d.get('rule', '')}".strip() if d.get("source") == "rule" else \
            f"free model {MODEL_WORDS.get(str(d.get('model') or ''), str(d.get('model') or ''))}, {int(d.get('neurons') or 0)} Neurons"
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
              f"<!-- {SCHEMA} " + json.dumps({"keys": [k for k, _, _ in drafts], "model": models[0] if models else "",
                                              "neurons": sum(int(d.get("neurons") or 0) for _, d, _ in drafts),
                                              "sources": {k: ("rule" if d.get("source") == "rule" else "model")
                                                          for k, d, _ in drafts}},
                                             separators=(",", ":")) + " -->"]
    if needs_you is not None:
        lines.append(needs_you_machine({"needs_you": needs_you}))                      # 0.9.50 B4: never a review
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
                # 0.9.50 (spec C: fields only): the tier split, the needs_you count, rules-only and rule posts
                "tiers": {"everyday": doc["neurons_easy"], "hard": doc["neurons_hard"]},
                "needs_you": len(doc["needs_you"]), "rules_only": bool(doc["rules_only_since"]),
                "rules": len(doc["posted_rules"]),
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
    def ask(self, key: str, rec: dict, earlier: dict | None, tier: str = "everyday", repeats: int = 0) -> dict:
        token = self.key_of()
        if not token:
            return {"error": "no_rotation_key"}
        try:
            status, body = self.request("POST", f"{self.url}/v1/review", {"Authorization": f"Bearer {token}"},
                                        request_body(key, rec, earlier, tier, repeats), timeout=TIMEOUT)
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
        doc = prune_needs_you(doc, open_recs, self.reviews)                     # 0.9.50 B4
        if now - doc["ran_at"] >= RUN_EVERY:
            doc["ran_at"] = now
            doc = self._draft(doc, open_recs, now)
        if self.mode in ("lockout", "primary") and not dry_run:
            doc = self._post(doc, open_recs, version, now)
        self.j.save_doc(DOC, doc)
        return doc

    def _draft(self, doc: dict, open_recs: dict, now: float) -> dict:
        from . import rules as RU
        made = 0
        hard_made = 0
        doc["skip"] = {k: t for k, t in doc["skip"].items() if t > now}
        if doc["rules_only_since"]:                                     # 0.9.50 B5: spent or refused today: rules only
            doc["last_reason"] = "rules_only"
            return doc
        recur = RU.recur_doc(self.j.load_doc(RU.RECUR_DOC, {}))
        for key, rec, tier in candidates(open_recs, self.reviews, now, recur):
            if made >= MAX_PER_RUN:
                break
            if tier == "hard" and hard_made >= MAX_HARD_PER_RUN:
                continue                                                # spec B5: at most one Qwen call a run
            fp = PR.fingerprint(str(rec.get("title") or key))
            d = doc["drafts"].get(key)
            if d is not None and d.get("fp") == fp:
                continue                                                # drafted already for these facts
            if key in doc["skip"]:
                continue                                                # 0.9.32: its last answer was unreadable; later
            earlier = self.reviews.cloud_before(key)                # the cloud's older word on this key, for context
            if not budget_left(doc, tier):
                doc["last_reason"] = "daily_budget_used"
                doc["rules_only_since"] = now                           # 0.9.50: spent means rules only today
                self.j.audit("free_review", "FREE_REVIEW_RULES_ONLY", tier=tier, reason="daily_budget_used")
                break
            doc["calls"] += 1
            out = self.ask(key, rec, earlier, tier, RU.episodes(recur, key, now))
            doc["last_at"] = now
            if "error" in out:
                doc["last_reason"] = out["error"]
                doc["last_shape"] = out.get("shape") or {}                 # 0.9.32: what came back, never the text
                self.j.audit("free_review", "FREE_REVIEW_FAILED", key=key, reason=out["error"])
                if out["error"].startswith("bad_answer") or out["error"] == "REVIEW_UNPARSED":
                    doc["skip"][key] = now + RETRY_HOURS * 3600         # this one problem: not again for a while
                    continue                                            # try the next
                doc["rules_only_since"] = now                           # 0.9.50: refused means rules only today
                self.j.audit("free_review", "FREE_REVIEW_RULES_ONLY", tier=tier, reason=out["error"][:40])
                break                                                   # the Companion said no: nothing more this run
            doc["neurons"] += out["neurons"]
            doc["neurons_hard" if tier == "hard" else "neurons_easy"] += out["neurons"]
            doc["last_reason"] = "ok"
            doc["last_shape"] = {}
            doc["ok_at"] = now
            family = family_of(key, rec)
            verdict = out["verdict"]
            why = ""
            if verdict == "OWNER_ACTION" and not trusted_family(doc, out["model"], family):   # 0.9.50 B6
                verdict, why = "ESCALATE", "owner_action_untrusted"
            elif verdict == "ESCALATE":
                why = "escalate"
            doc["drafts"][key] = {"fp": fp, "kind": str(rec.get("source") or "other"), "verdict": verdict,
                                  "summary": out["summary"], "steps": out["steps"], "model": out["model"],
                                  "model_id": out.get("model_id", ""), "tier": tier, "family": family,
                                  "neurons": out["neurons"], "at": now, "source": "model"}
            if why:
                note_needs_you(doc, key, str(rec.get("title") or key), why, fp, now)        # 0.9.50 B4
            self.j.audit("free_review", "FREE_REVIEW_DRAFTED", key=key, verdict=verdict, neurons=out["neurons"], tier=tier)
            made += 1
            if tier == "hard":
                hard_made += 1
        return doc

    def needs_you_keys(self) -> set[str]:
        """0.9.50: the keys on the needs_you list (the reviewer watchdog does not count them as stalled)."""
        return set(self._load()["needs_you"])

    def needs_you_summary(self) -> tuple[str, str]:
        """(the owner line for the morning summary, the machine line) or ("", "")."""
        doc = self._load()
        return (needs_you_line(doc), needs_you_machine(doc)) if doc["needs_you"] else ("", "")

    def internal_stalled(self, open_recs: dict, now: float) -> float | None:
        """0.9.50 (D7): since when the internal reviewer has not succeeded although it had work, or None. Only in
        lockout or primary mode, only after INTERNAL_STALL_HOURS, only while a candidate still waits without any review."""
        if self.mode not in ("lockout", "primary") or not self.url or self.reviews is None:
            return None
        doc = self._load()
        since = max(float(doc["ok_at"]), float(doc["rules_only_since"]))
        if not since or now - since < INTERNAL_STALL_HOURS * 3600:
            return None
        waiting = [k for k, rec, _ in candidates(open_recs, self.reviews, now) if self.reviews.get(k) is None
                   and k not in doc["needs_you"] and k not in doc["drafts"]]
        return since if waiting else None

    def rule_drafts(self, open_recs: dict, doc: dict) -> list[tuple[str, dict, dict]]:
        """0.9.42: rule verdicts (``hbm/rules.py``, stored by the Watcher as reviews with ``source: rule``) of open
        problems that this lane has not posted yet, as draft-shaped items (no model, 0 Neurons). A cloud or owner
        review of the key means nothing to post."""
        out = []
        for key, rec in sorted(open_recs.items(), key=lambda kv: float((kv[1] or {}).get("first") or 0)):
            if not isinstance(rec, dict) or not rec.get("alerted"):
                continue
            r = self.reviews.get(key)
            if not isinstance(r, dict) or r.get("source") != "rule" or r.get("verdict") not in RULE_POSTABLE:
                continue
            if float(doc["posted_rules"].get(key, 0)) >= float(r.get("at") or 0):
                continue
            out.append((key, {"verdict": r["verdict"], "summary": str(r.get("summary") or ""), "steps": list(r.get("steps") or []),
                              "model": "rule", "neurons": 0, "source": "rule", "rule": str(r.get("rule") or "")}, rec))
        return out

    def _post(self, doc: dict, open_recs: dict, version: str, now: float) -> dict:
        cloud_at = self.reviews.cloud_at()
        every = POST_EVERY_HOURS_PRIMARY if self.mode == "primary" else POST_EVERY_HOURS
        if self.mode == "lockout" and not cloud_lockout(cloud_at, now):   # 0.9.42: primary needs no cloud silence
            return doc
        if now - doc["posted_at"] < every * 3600:
            return doc
        due = []
        if trusted(doc):                                                # model drafts: only while the lane agrees
            for key, d in sorted(doc["drafts"].items(), key=lambda kv: float(kv[1].get("at") or 0)):
                rec = open_recs.get(key)
                if d.get("posted_at") or d.get("verdict") not in POSTABLE or not isinstance(rec, dict):
                    continue
                if tier_for(key, rec, 3 if d.get("tier") == "hard" else 0) is None or self.reviews.cloud(key) is not None:
                    continue                                            # 0.9.50: a hard-tier draft posts like any other
                if d.get("verdict") == "OWNER_ACTION" and not trusted_family(doc, str(d.get("model") or ""), str(d.get("family") or "other")):
                    continue                                            # 0.9.50 B6: trust was lost since the draft
                if PR.fingerprint(str(rec.get("title") or key)) != d.get("fp"):
                    continue                                            # the facts changed since the draft
                due.append((key, d, rec))
        for key, d, rec in self.rule_drafts(open_recs, doc):            # 0.9.42: rule verdicts, trust not needed
            due.append((key, d, rec))
        if not due:
            return doc
        due = due[:MAX_POST]
        url = self.report(post_text(due, version, (now - float(cloud_at or now)) / 3600, self.mode, doc["needs_you"]))
        for key, d, _ in due:
            if d.get("source") == "rule":
                doc["posted_rules"][key] = now
                if isinstance(url, str) and url:
                    self.reviews.note_url(key, url)                    # the page links the rule verdict to its post
                continue
            d["posted_at"] = now
            d["url"] = url if isinstance(url, str) else ""
        doc["posted_at"] = now
        self.j.audit("free_review", "FREE_REVIEW_POSTED", items=len(due), lockout_hours=round((now - float(cloud_at)) / 3600, 1))
        return doc
