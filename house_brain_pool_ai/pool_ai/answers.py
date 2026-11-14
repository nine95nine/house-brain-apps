"""Pool AI's answers to Systems AI's questions (A4, 0.1.1): GET /answer?topic=self|pool_data.

Built from what the runtime already holds in memory (which listed pool inputs were readable at the
last cycle, the pump and spa state, the last known salt and the history read note) and its own
health block. No Home Assistant read, no network call and no store read happens for a question.
Never an Ondilo reading, an ORP or a water temperature number (there is no trusted one in 0.1, so
water temperature is always named as not checked); never more than the page shows. A missing input
or an unreadable history is a fact and is also named in not_checked.
"""
from . import __version__
from .entities import GENERATION, PUMP, SALT
from .health import APP, build
from .specialist_answer import entity, fact, gap, record, respond

TOPICS = ('self', 'pool_data')
# input key -> (what it is, the entity its evidence names)
INPUTS = {'generation': ('AquaRite generation readings', GENERATION['rate']),
          'salt': ('AquaRite last known salt', SALT['last_known']),
          'pump': ('Pool pump state', PUMP['switch']),
          'sense': ('Sense power reading for the pump', PUMP['sense_w'])}


def pool_data(runtime, at):
    """(problem, facts, not_checked) for the pool readings Pool AI took at its last cycle."""
    if runtime.inputs.get('core_api') is not True:
        return False, [], [gap('pool_data', 'CORE_API_UNREADABLE')]
    view, seen = runtime.now_view, runtime.cycle_at
    if not view:
        return False, [], [gap('pool_data', 'NO_READING_YET')]
    problems, notes, gaps = [], [], []
    for key, (words, entity_id) in INPUTS.items():
        if runtime.inputs.get(key) is not True:
            problems.append(fact(f'{words} missing or out of range at the last read', entity(entity_id, seen), at, seen))
            gaps.append(gap(key, 'INPUT_MISSING_OR_OUT_OF_RANGE'))
    salt = view.get('salt_last_known_ppm')
    notes.append(fact(f"Pump {view.get('pump', 'UNKNOWN')}; spa mode {view.get('spa_mode', 'UNKNOWN')}; on-peak "
                      f"{view.get('on_peak', 'UNKNOWN')}" + (f'; last known salt {salt:.0f} ppm' if salt is not None else ''),
                      entity(PUMP['switch'], seen), at, seen))
    if runtime.inputs.get('history') is False:
        problems.append(fact(f'Recorder history read failed (last read {runtime.backfill_last or "unknown"})',
                             record('history', seen), at, seen))
        gaps.append(gap('history', 'RECORDER_HISTORY_UNREADABLE'))
    gaps.append(gap('water_temperature', 'NO_TRUSTED_WATER_TEMPERATURE'))
    if runtime.inputs.get('tests') is not True:
        gaps.append(gap('manual_tests', 'POOL_TEST_INPUTS_UNREADABLE'))
    return bool(problems), problems + notes, gaps


def history(runtime, at):
    """The `self` topic's store fact: what the last cycle's report counted (no store read here)."""
    h = (runtime.status or {}).get('history') or {}
    if not isinstance(h.get('points'), int):
        return []
    return [fact(f"History kept: {h['points']} change points; {h.get('entity_days_waiting', 0)} series-days still to read",
                 record('history', runtime.cycle_at), at, runtime.cycle_at)]


def answer(runtime, query, now):
    """The GET /answer body (house_brain_specialist_answer.v1)."""
    return respond(APP, __version__, TOPICS, query, now, runtime.hold, build(runtime, now),
                   lambda topic, at: pool_data(runtime, at), lambda at: history(runtime, at))
