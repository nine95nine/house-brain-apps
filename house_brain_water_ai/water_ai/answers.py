"""Water AI's answers to Systems AI's questions (A4, 0.1.1): GET /answer?topic=self|water_data.

Built from what the runtime already holds in memory (the last cycle's view of the RO monitor and the
yard timers, and the history read note) and its own health block. No Home Assistant read, no network
call and no store read happens for a question. Zone keys only (never a timer's device name), never
more than the page shows. Leak sensors are not connected and the R7 probes are not commissioned, so
they are always named as not checked, never as dry. An unusable RO monitor, silent yard timers or an
unreadable history are facts and are also named in not_checked.
"""
from . import __version__
from .entities import RO_PRESSURE
from .health import APP, build
from .specialist_answer import entity, fact, gap, record, respond

TOPICS = ('self', 'water_data')


def water_data(runtime, at):
    """(problem, facts, not_checked) for the water readings Water AI took at its last cycle."""
    if runtime.inputs.get('core_api') is not True:
        return False, [], [gap('water_data', 'CORE_API_UNREADABLE')]
    view, seen = runtime.now_view, runtime.cycle_at
    if not view:
        return False, [], [gap('water_data', 'NO_READING_YET')]
    problems, notes, gaps = [], [], []
    ro = view['ro']
    if ro['ok']:
        notes.append(fact(f"RO tank pressure {ro['psi']:.1f} psi; monitor online and plausible",
                          entity(RO_PRESSURE, seen), at, seen))
    else:
        psi = f"{ro['psi']:.1f} psi" if ro['psi'] is not None else 'missing'
        problems.append(fact(f"RO tank pressure monitor not usable: pressure {psi}, online {ro['online'] or 'unknown'}, "
                             f"plausible {ro['plausible'] or 'unknown'}", entity(RO_PRESSURE, seen), at, seen))
        gaps.append(gap('ro_monitor', 'RO_MONITOR_NOT_USABLE'))
    zones = view['zones']
    silent = sorted(z for z, d in zones.items() if not d['live'])
    if silent:
        problems.append(fact('Yard timers with no live reading: ' + ', '.join(silent), record('zones', seen), at, seen))
        gaps.append(gap('yard_timers', 'TIMER_NO_LIVE_READING'))
    faults = sorted(z for z, d in zones.items() if d['fault'] == 'on')
    if faults:
        problems.append(fact('Yard timer fault reported: ' + ', '.join(faults), record('zones', seen), at, seen))
    floor = (view.get('context') or {}).get('low_battery_threshold')
    low = sorted((z, d['battery']) for z, d in zones.items() if d['battery'] is not None and floor is not None
                 and d['battery'] < floor)
    if low:
        problems.append(fact('Yard timer battery below the low-battery threshold: '
                             + ', '.join(f'{z} {b:.0f} %' for z, b in low), record('zones', seen), at, seen))
    if runtime.inputs.get('history') is False:
        problems.append(fact(f'Recorder history read failed (last read {runtime.backfill_last or "unknown"})',
                             record('history', seen), at, seen))
        gaps.append(gap('history', 'RECORDER_HISTORY_UNREADABLE'))
    back = ((view.get('slots') or {}).get('offline_timers') or {}).get('now_live') or 0
    if back:
        notes.append(fact(f'{back} timers kept offline are reporting again', record('zones', seen), at, seen))
    gaps += [gap('leak', 'LEAK_SENSORS_NOT_CONNECTED'), gap('r7_probes', 'R7_PROBES_NOT_COMMISSIONED')]
    return bool(problems), problems + notes, gaps


def history(runtime, at):
    """The `self` topic's store fact: what the last cycle's report counted (no store read here)."""
    h = (runtime.status or {}).get('history') or {}
    if not isinstance(h.get('points'), int):
        return []
    return [fact(f"History kept: {h['points']} change points, {h.get('psi_minutes', 0)} pressure minutes; "
                 f"{h.get('entity_days_waiting', 0)} entity-days still to read", record('history', runtime.cycle_at),
                 at, runtime.cycle_at)]


def answer(runtime, query, now):
    """The GET /answer body (house_brain_specialist_answer.v1)."""
    return respond(APP, __version__, TOPICS, query, now, runtime.hold, build(runtime, now),
                   lambda topic, at: water_data(runtime, at), lambda at: history(runtime, at))
