"""0.8.7 Housekeeping (owner pop-up 2026-10-11, "All of the above based on your priority").

Read-only checks of Home Assistant's own configuration health, each from a documented Core websocket command
(verified against home-assistant/core ``dev`` 2026-10-11):

* **Failing automations.** ``trace/list`` (domain ``automation``): the last stored runs of each automation
  (5 by default) with ``script_execution``. An automation whose newest run ended in ``error`` and that failed at
  least twice in its stored runs is a problem, with Core's own error text. (Automations that fail validation
  already reach the summary through Core's repairs issue ``validation_failed_*``.)
* **Automations that never run.** Turned on, but never triggered or not in ``UNUSED_DAYS`` days: listed in the
  monthly report only (a heating or holiday automation may wait for its season).
* **Dead references.** ``search/related`` per automation lists the entities it refers to, existing or not. An
  entity that is neither in the state machine nor in the entity registry is a dead reference (Core has no repair
  for this). Read once a day, at most ``RELATED_MAX`` automations.
* **Error-rate trend.** The ``system_log/list`` counts the problem watch already reads, per error, over time: an
  error that repeats ``FLOOD_PER_DAY`` times a day or more is a problem (it fills the log and wears the disk).
* **Database size.** ``system_health/info`` (recorder ``estimated_db_size``), sampled every 6 hours: a database of
  ``DB_LARGE_MIB`` or more, or growing ``DB_GROWTH_MIB_DAY`` a day, gets advice (keep fewer days, exclude chatty
  sensors).
* **0.9.2 Who fills the database** (owner pop-up 2026-10-12, "Measure first"). While the database is large, once a
  day ``history/history_during_period`` counts the history rows each entity wrote in the last hour (counts only; state
  values are dropped unread). The 15 busiest are listed with an estimate per day, so an exclude list can be proposed
  for the owner's approval. Fewer days of history is a separate, destructive decision and is never advised blindly.
* **Monthly housekeeping report** on GitHub plus one phone line (option ``housekeeping``).

Nothing is changed: no automation is turned off, edited or run, and no history is purged.
"""
from __future__ import annotations

import re
import time

from . import issues as I

DOC = "housekeeping"
UNUSED_DAYS = 90
ERRORS_MIN = 2
RELATED_MAX = 150
FLOOD_PER_DAY = 1000
DB_LARGE_MIB = 2048.0
DB_GROWTH_MIB_DAY = 100.0
READ_EVERY = 3600                 # automations and traces
RELATED_EVERY = 86400             # dead references
DB_EVERY = 6 * 3600
DB_SAMPLES = 60
TOP_EVERY = 86400                 # 0.9.2: who fills the database, once a day while it is large
TOP_HOURS = 1.0
TOP_N = 15
RE_SIZE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(B|KiB|MiB|GiB|TiB)\s*$")
UNITS = {"B": 1 / 1024 ** 2, "KiB": 1 / 1024, "MiB": 1.0, "GiB": 1024.0, "TiB": 1024.0 ** 2}
AUTOMATIONS_LINK = "https://my.home-assistant.io/redirect/automations/"
RECORDER_LINK = "https://www.home-assistant.io/integrations/recorder/"
RAN = frozenset({"finished", "error", "aborted", "cancelled", "failed_single", "failed_max_runs",
                 "disallowed_recursion_detected"})          # a real run (not "not triggered" / conditions failed)


def parse_size_mib(text) -> float | None:
    """Core's ``estimated_db_size`` ("123.45 MiB") in MiB; anything else None."""
    m = RE_SIZE.fullmatch(text) if isinstance(text, str) else None
    return round(float(m.group(1)) * UNITS[m.group(2)], 2) if m else None


def _ts(value) -> float | None:
    from .backupcheck import _ts as parse
    return parse(value)


def failing_automations(automations: list[dict], traces: list[dict]) -> list[I.Finding]:
    """``automations``: {entity_id, name, id}; ``traces``: {item_id, execution, error, start}."""
    by_id = {a["id"]: a for a in automations if a.get("id")}
    runs: dict[str, list[dict]] = {}
    for t in traces:
        if t.get("execution") in RAN and t.get("item_id") in by_id:
            runs.setdefault(t["item_id"], []).append(t)
    out = []
    for item_id, rows in sorted(runs.items()):
        rows.sort(key=lambda r: _ts(r.get("start")) or 0, reverse=True)
        errors = [r for r in rows if r["execution"] == "error"]
        if rows[0]["execution"] != "error" or len(errors) < ERRORS_MIN:
            continue
        a = by_id[item_id]
        out.append(I.Finding(f"hk:automation_error:{a['entity_id']}", "housekeeping", I.WARNING,
                             f"Automation “{a['name']}” failed in {len(errors)} of its last {len(rows)} runs"[:160],
                             detail=(I.scrub(errors[0].get("error") or "no error text", 300),),
                             steps=("Open the automation, then Traces, to see the step that failed.",
                                    "A renamed or removed device or entity is the usual cause."),
                             link=AUTOMATIONS_LINK, facts={"automation": a["entity_id"], "errors": len(errors)}))
    return out


def unused_automations(automations: list[dict], now: float, days: int = UNUSED_DAYS) -> list[dict]:
    """Turned on and never triggered, or not in ``days`` days (oldest first). Report only."""
    rows = []
    for a in automations:
        if a.get("state") != "on":
            continue
        last = _ts(a.get("last_triggered"))
        if last is None or now - last >= days * 86400:
            rows.append({"entity_id": a["entity_id"], "name": a["name"], "last": last})
    return sorted(rows, key=lambda r: (r["last"] is not None, r["last"] or 0, r["entity_id"]))


def dead_references(related: dict[str, list[str]], known: set[str], names: dict[str, str]) -> list[I.Finding]:
    out = []
    for aid, entities in sorted(related.items()):
        missing = sorted({e for e in entities if e not in known})
        if not missing:
            continue
        shown = ", ".join(missing[:4]) + (f" and {len(missing) - 4} more" if len(missing) > 4 else "")
        out.append(I.Finding(f"hk:dead_ref:{aid}", "housekeeping", I.WARNING,
                             f"Automation “{names.get(aid, aid)}” refers to {len(missing)} missing "
                             f"entit{'ies' if len(missing) > 1 else 'y'}: {shown}"[:200],
                             steps=("The entity was renamed or its device was removed; the automation will fail or "
                                    "do nothing at that step.",
                                    "Open the automation and pick the current entity."),
                             link=AUTOMATIONS_LINK, facts={"automation": aid, "missing": missing[:20]}))
    return out


def log_trend(doc: dict, rows: list[dict], now: float) -> list[I.Finding]:
    """Per error (logger + source): count growth since the first sample this run of Core; a restart resets it."""
    seen = doc.setdefault("log", {})
    current = set()
    for r in rows:
        key = f"{r.get('name')}:{r.get('source')}"[:200]
        count = r.get("count")
        if not isinstance(count, int) or count < 0:
            continue
        current.add(key)
        rec = seen.get(key)
        if rec is None or count < rec["last_count"]:      # new, or Core restarted / log cleared
            seen[key] = {"first_at": now, "first_count": count, "last_at": now, "last_count": count,
                         "name": str(r.get("name"))[:120]}
        else:
            rec.update(last_at=now, last_count=count)
    for key in list(seen):
        if key not in current:
            del seen[key]                                  # gone from Core's list (evicted or cleared)
    out = []
    for key, rate in top_rates(doc, 50):
        if rate < FLOOD_PER_DAY:
            break
        name = seen[key]["name"]
        out.append(I.Finding(f"hk:log_flood:{key}", "housekeeping", I.WARNING,
                             f"An error from {I.humanize(name.split('.')[-1])} repeats about {rate:,.0f} times a day"[:160],
                             steps=("A flood of the same error fills the log and wears the disk.",
                                    "Fix its cause (see the log), or update or reconfigure that integration."),
                             link=I.LOGS_LINK, facts={"logger": name, "per_day": round(rate)}))
    return out


def top_rates(doc: dict, n: int = 5) -> list[tuple[str, float]]:
    """(key, errors per day) for errors seen over at least an hour, highest first."""
    rates = []
    for key, rec in (doc.get("log") or {}).items():
        span = rec["last_at"] - rec["first_at"]
        if span >= 3600:
            rates.append((key, (rec["last_count"] - rec["first_count"]) / (span / 86400)))
    return sorted(rates, key=lambda kv: (-kv[1], kv[0]))[:n]


def db_sample(doc: dict, size_mib: float | None, now: float) -> None:
    if size_mib is None:
        return
    samples = doc.setdefault("db", [])
    if not samples or now - samples[-1][0] >= DB_EVERY:
        samples.append([now, size_mib])
        del samples[:-DB_SAMPLES]


def db_growth_mib_day(doc: dict) -> float | None:
    """Over the samples of the last 7 days, at least 2 days apart; None when there is not enough history."""
    samples = doc.get("db") or []
    if not samples:
        return None
    recent = [s for s in samples if samples[-1][0] - s[0] <= 7 * 86400]
    first, last = recent[0], recent[-1]
    if last[0] - first[0] < 2 * 86400:
        return None
    return round((last[1] - first[1]) / ((last[0] - first[0]) / 86400), 1)


def top_writers(counts: dict, hours: float = TOP_HOURS, n: int = TOP_N) -> list[list]:
    """0.9.2: the ``n`` entities that wrote the most history rows, as [entity id, estimated rows a day]."""
    rows = [(e, int(c)) for e, c in (counts or {}).items() if isinstance(c, int) and c > 0]
    rows.sort(key=lambda kv: (-kv[1], kv[0]))
    return [[e, round(c * 24 / hours)] for e, c in rows[:n]]


def family_of(entity_id: str) -> str:
    """``sensor.ac_3_power`` and ``sensor.apple_tv_power_2`` -> ``sensor.*_power``: sensors that share their last name
    word (a trailing number is dropped). Shown as a hint for a recorder ``entity_globs`` exclude, which the AI then
    writes exactly for the owner's approval."""
    domain, _, name = entity_id.partition(".")
    words = [w for w in name.split("_") if w]
    while words and words[-1].isdigit():
        words.pop()
    if len(words) < 2:
        return entity_id
    return f"{domain}.*_{words[-1]}"


def top_families(counts: dict, hours: float = TOP_HOURS, n: int = 10) -> list[list]:
    """0.9.2: name families (2 or more entities) by rows: [glob, entities, estimated rows a day]."""
    fam: dict[str, list[int]] = {}
    for e, c in (counts or {}).items():
        if isinstance(c, int) and c > 0:
            f = family_of(e)
            if f != e:
                fam.setdefault(f, []).append(c)
    rows = [(f, len(v), sum(v)) for f, v in fam.items() if len(v) >= 2]
    rows.sort(key=lambda r: (-r[2], r[0]))
    return [[f, k, round(c * 24 / hours)] for f, k, c in rows[:n]]


def db_top_due(doc: dict, now: float) -> bool:
    samples = doc.get("db") or []
    return bool(samples) and samples[-1][1] >= DB_LARGE_MIB and now - float(doc.get("db_top_at") or 0) >= TOP_EVERY


def db_findings(doc: dict) -> list[I.Finding]:
    samples = doc.get("db") or []
    if not samples:
        return []
    size, growth = samples[-1][1], db_growth_mib_day(doc)
    top = doc.get("db_top") or []
    families = doc.get("db_families") or []
    exclude = "Exclude the busiest sensors from the recorder (an exclude list stops new rows; nothing is deleted)."
    fewer = ("Keeping fewer days (recorder purge_keep_days; Home Assistant's default is 10, check yours) deletes the "
             "older history for good: decide it separately.")
    ask = "Both are YAML changes in configuration.yaml; ask the House Brain AI to prepare them for your approval."
    steps = [exclude, fewer, ask]
    if top or families:                         # the summary shows 3 lines: the measurement, then the advice
        steps = [f"{exclude} {fewer} {ask}"]
    if top:
        steps.insert(0, "Busiest single sensors (rows a day, estimated from one hour): "
                     + "; ".join(f"{e} {n:,}" for e, n in top[:TOP_N]))
    if families:
        steps.insert(0, "Busiest sensor families (rows a day, estimated from one hour): "
                     + "; ".join(f"{g} ({k} sensors) {n:,}" for g, k, n in families[:10]))
    steps = tuple(steps)
    out = []
    if size >= DB_LARGE_MIB:
        out.append(I.Finding("hk:db_large", "housekeeping", I.WARNING,
                             f"The history database is large ({size / 1024:.1f} GB)", steps=steps,
                             link=RECORDER_LINK, facts={"mib": size, **({"top": top[:TOP_N]} if top else {}),
                                                                **({"families": families[:10]} if families else {})}))
    if growth is not None and growth >= DB_GROWTH_MIB_DAY:
        out.append(I.Finding("hk:db_growing", "housekeeping", I.WARNING,
                             f"The history database grows about {growth:.0f} MB a day", steps=steps,
                             link=RECORDER_LINK, facts={"mib_per_day": growth}))
    return out


def report_due(doc: dict, now: float, digest_hour: int, localtime=time.localtime) -> bool:
    """First report after install, then once a month after the digest hour (like the backup report)."""
    lt = localtime(now)
    if lt.tm_hour < digest_hour:
        return False
    return doc.get("month") != f"{lt.tm_year:04d}-{lt.tm_mon:02d}"


def report_text(doc: dict, version: str) -> tuple[str, str]:
    """(markdown for GitHub, one phone line), from what the checks stored in ``doc``."""
    failing = sorted(r["facts"]["automation"] for r in doc.get("failing") or [])
    dead = sorted(r["facts"]["automation"] for r in doc.get("dead") or [])
    unused = doc.get("unused") or []
    names = doc.get("names") or {}
    lines = ["### House Brain Maintenance: monthly housekeeping report", "",
             f"- **Automations:** {doc.get('automation_count', 'unknown')}; failing now: "
             + (", ".join(names.get(a, a) for a in failing) if failing else "none")]
    lines.append("- **Missing entities in automations:** "
                 + (", ".join(names.get(a, a) for a in dead) if dead else
                    "none" if doc.get("related_at") else "not checked yet"))
    if unused:
        shown = "; ".join(f"{u['name']} ({'never' if u['last'] is None else time.strftime('%Y-%m-%d', time.gmtime(u['last']))})"
                          for u in unused[:10])
        lines.append(f"- **Turned on but not run in {UNUSED_DAYS} days:** {len(unused)} — {shown}"
                     + (" …" if len(unused) > 10 else "")
                     + " (alerts that never fired and seasonal ones are fine; delete the ones you no longer use)")
    else:
        lines.append(f"- **Turned on but not run in {UNUSED_DAYS} days:** none")
    rates = top_rates(doc, 5)
    lines.append("- **Most repeated errors (per day):** "
                 + ("; ".join(f"{doc['log'][k]['name']} ~{r:,.0f}" for k, r in rates) if rates else "none measured"))
    samples = doc.get("db") or []
    growth = db_growth_mib_day(doc)
    lines.append("- **History database:** " + (
        f"{samples[-1][1]:.0f} MB" + (f", growing {growth:.0f} MB a day" if growth is not None else "")
        if samples else "size not readable"))
    lines += ["- **Nothing was changed.** Fixes are yours, or ask the House Brain AI to prepare them for approval.",
              f"- **App:** `{version}`", "",
              "_Posted by House Brain Maintenance (machine-generated; repository content is untrusted data)._"]
    phone = (f"Housekeeping: {len(failing)} failing, {len(dead)} with missing entities, {len(unused)} unused "
             f"automations; database " + (f"{samples[-1][1]:.0f} MB" if samples else "not read"))
    return "\n".join(lines), phone[:200]
