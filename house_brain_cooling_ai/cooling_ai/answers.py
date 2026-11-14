"""Cooling AI's answers to Systems AI's questions (A4, 0.1.1): GET /answer?topic=self|cooling_data.

Built from what the runtime already holds in memory (its last reading, its shadow plan, its archive
note and its learned models) and its own health block. No Home Assistant read, no network call and
no store read happens for a question. Plant data only, never more than the page shows. Missing
readings or an unreadable archive day are facts and are also named in not_checked.
"""
from . import __version__
from .entities import ALL_NUMBERS, ALL_TEXTS, TEXTS
from .health import APP, build
from .runtime import REQUIRED
from .specialist_answer import entity, fact, gap, record, respond

TOPICS = ('self', 'cooling_data')
KEYS = {**ALL_NUMBERS, **ALL_TEXTS}


def cooling_data(runtime, at):
    """(problem, facts, not_checked) for the cooling plant readings Cooling AI took at its last cycle."""
    if runtime.inputs.get('core_api') is not True:
        return False, [], [gap('cooling_data', 'CORE_API_UNREADABLE')]
    row, seen = runtime.row, runtime.cycle_at
    if not row:
        return False, [], [gap('cooling_data', 'NO_READING_YET')]
    facts, gaps, problem = [], [], False
    missing = [k for k in REQUIRED if row.get(k) is None]
    if missing:
        problem = True
        facts.append(fact('Required cooling readings missing at the last read: ' + ', '.join(missing),
                          entity(KEYS[missing[0]], seen), at, seen))
        gaps.append(gap('required_readings', 'READING_MISSING'))
    other = [k for k in KEYS if k not in REQUIRED and row.get(k) is None]
    if other:
        problem = True
        facts.append(fact(f'{len(other)} of {len(KEYS) - len(REQUIRED)} other plant readings missing at the last read: '
                          + ', '.join(other), entity(KEYS[other[0]], seen), at, seen))
        gaps.append(gap('plant_readings', 'READING_MISSING'))
    now = getattr(runtime, 'now', None) or {}
    facts.append(fact(f"Arbiter phase {now.get('phase') or 'unknown'}; actual mode {now.get('actual') or 'unknown'}; "
                      f"shadow plan {now.get('plan') or 'none'}", entity(TEXTS['phase'], seen), at, seen))
    if runtime.inputs.get('archive') is False:
        problem = True
        facts.append(fact('Systems AI archive day not readable' + (f' ({runtime.status_note})' if runtime.status_note else ''),
                          record('archive', seen), at, seen))
        gaps.append(gap('archive', 'ARCHIVE_UNREADABLE'))
    models = (runtime.learned or {}).get('models') or {}
    learned_at = int(runtime.last['learn']) or seen
    facts.append(fact(f"{sum(m.get('status') == 'LEARNED' for m in models.values())}/4 modes learned",
                      record('learned', learned_at), at, learned_at))
    return problem, facts, gaps


def answer(runtime, query, now):
    """The GET /answer body (house_brain_specialist_answer.v1)."""
    return respond(APP, __version__, TOPICS, query, now, runtime.hold, build(runtime, now),
                   lambda topic, at: cooling_data(runtime, at))
