"""Energy AI's answers to Systems AI's questions (A4, 0.1.7): GET /answer?topic=self|power_data.

Built from the last cycle's status the App already keeps (the samples it took for its bound load,
solar and grid readings, the Sense device count and the history backfill note) and its own health
block. No Home Assistant read, no network call and no store read happens for a question. Sense
device names are never named (counts only); never more than the page shows. A missing, stale or
unusable reading is a fact with its own evidence and age, and its role is also named in not_checked
with the reading's quality code (`load:STALE_OR_FUTURE`).
"""
from . import __version__
from .health import APP
from .specialist_answer import CODE_RE, entity, fact, gap, record, respond, stamp

TOPICS = ('self', 'power_data')
ROLES = ('load', 'solar', 'grid')
FINE = ('GOOD', 'IDLE')


def power_data(status, at):
    """(problem, facts, not_checked) for the bound power readings, as of the last cycle."""
    if 'all' in (status.get('data_quality') or {}):
        return False, [], [gap('power_data', 'DATA_OR_STORAGE_FAILURE')]
    cycle_at = stamp(status.get('at'))
    samples = [s for s in status.get('samples') or [] if isinstance(s, dict)]
    if cycle_at is None or not samples:
        return False, [], [gap('power_data', 'NO_READING_YET')]
    problems, notes, gaps = [], [], []
    for role in ROLES:
        s = next((s for s in samples if s.get('role') == role), None)
        if s is None:
            gaps.append(gap(role, 'NOT_BOUND'))
            continue
        seen = stamp(s.get('source_at')) or cycle_at
        evidence = entity(s['entity'], seen)
        quality = s.get('quality')
        if quality == 'GOOD':
            notes.append(fact(f"{role.capitalize()} power reading GOOD, {s['value']:.0f} W, {max(0, at - seen)} s old",
                              evidence, at, seen))
        elif quality == 'IDLE':
            notes.append(fact(f'{role.capitalize()} power reading idle at 0 W', evidence, at, seen))
        else:
            old = f'; last reading {max(0, at - seen) // 60} min old' if stamp(s.get('source_at')) else ''
            problems.append(fact(f'{role.capitalize()} power reading {quality}{old}', evidence, at, seen))
            gaps.append(gap(role, quality if isinstance(quality, str) and CODE_RE.fullmatch(quality) else 'READING_INVALID'))
    devices = [s for s in samples if str(s.get('role', '')).startswith('detection:')]
    bad = sum(s.get('quality') not in FINE for s in devices)
    if bad:
        problems.append(fact(f'{bad} of {len(devices)} Sense device readings not usable at the last cycle',
                             record('sense_devices', cycle_at), at, cycle_at))
        gaps.append(gap('sense_devices', 'DEVICE_READINGS_NOT_USABLE'))
    backfill = status.get('history_backfill') or {}
    if backfill.get('last_error'):
        problems.append(fact(f"History backfill: {backfill['last_error']}", record('history_backfill', cycle_at), at, cycle_at))
        gaps.append(gap('history_backfill', 'HISTORY_UNAVAILABLE_OR_REFUSED'))
    return bool(problems), problems + notes, gaps


def history(status, at):
    """The `self` topic's store fact: the backfill progress the last cycle kept (no store read here)."""
    b = status.get('history_backfill') or {}
    cycle_at = stamp(status.get('at'))
    if not isinstance(b.get('accepted'), int) or cycle_at is None:
        return []
    return [fact(f"History backfill: {b['accepted']} readings accepted, {b.get('refused', 0)} refused",
                 record('history_backfill', cycle_at), at, cycle_at)]


def answer(tracker, query, now):
    """The GET /answer body (house_brain_specialist_answer.v1). `tracker` holds the last cycle's status."""
    status = tracker.status or {}
    held = (status.get('hold_reason') or 'KILL_SWITCH_HELD') if status.get('state') == 'KILL_SWITCH_HOLD' else None
    return respond(APP, __version__, TOPICS, query, now, held, tracker.build(now),
                   lambda topic, at: power_data(status, at), lambda at: history(status, at))
