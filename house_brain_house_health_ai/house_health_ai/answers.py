"""House Health AI's answers to Systems AI's questions (A4, 0.1.1): GET /answer?topic=self|power_network.

Built from what the runtime already holds in memory (the last cycle's outage class, its open episode
and the last learned UPS view) and its own health block. No Home Assistant read, no network call and
no store read happens for a question. Power and network facts only: never a room, a room name or a
person; never more than the page shows.
"""
from . import __version__
from .entities import NETWORK, NOA, UPS
from .health import APP, build
from .specialist_answer import entity, fact, gap, record, respond

TOPICS = ('self', 'power_network')
ACTIVE = ('POWER', 'ISP', 'LAN')


def power_network(runtime, at):
    """(problem, facts, not_checked) for the outage class and the UPS, as of the last cycle and learn."""
    if runtime.inputs.get('core_api') is not True:
        return False, [], [gap('power_network', 'CORE_API_UNREADABLE')]
    view, seen = runtime.now_view, runtime.cycle_at
    if not view:
        return False, [], [gap('power_network', 'NO_READING_YET')]
    problems, notes, gaps = [], [], []
    out = view['outage']
    noa = out['noa_port']
    words = (f"Outage class {out['class']} ({out['detail']}); WAN {noa['wan']}, LAN {noa['lan']}, "
             f"utility power {out['refined_power']}, UPS {noa['ups']}")
    if runtime.inputs.get('outage') is not True:
        gaps.append(gap('outage', 'OUTAGE_INPUTS_UNREADABLE'))
    elif out['class'] in ACTIVE or out['detail'] == 'PARTIAL':
        problems.append(fact(words, entity(NETWORK['ups_status'], seen), at, seen))
    else:
        notes.append(fact(words, entity(NETWORK['ups_status'], seen), at, seen))
    episode = (runtime.memory or {}).get('episode')
    if out.get('episode_open') and episode:
        start = int(episode['start'])
        problems.append(fact(f'An outage episode is open, started {max(0, at - start) // 60} min ago',
                             record('outage_episode', start), at, start))
    live = out.get('live_noa_class')
    if live is not None and live != out['detail']:
        notes.append(fact(f"NOA 1.0's live class reads {live}", entity(NOA['class'], seen), at, seen))
    ups = (runtime.learned or {}).get('ups')
    learned_at = int(runtime.last['learn']) or seen
    if runtime.inputs.get('ups') is not True or not ups:
        gaps.append(gap('ups', 'UPS_UNREADABLE' if runtime.inputs.get('ups') is not True else 'UPS_NOT_LEARNED_YET'))
    else:
        battery = {True: 'on battery', False: 'on utility'}.get(ups['on_battery_now'], 'unknown')
        line = (f"UPS {battery}; charge {_num(ups['charge'], '%')}, runtime {_num(ups['runtime_min'], 'min')}, "
                f"load {_num(ups['load'], '%')}; battery band {ups['band']}")
        target = problems if ups['on_battery_now'] is True or ups['band'] == 'WATCH' else notes
        target.append(fact(line, entity(UPS['charge'], learned_at), at, learned_at))
    return bool(problems), problems + notes, gaps


def _num(value, unit):
    return 'unknown' if value is None else f'{value:.0f} {unit}'


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
                   lambda topic, at: power_network(runtime, at), lambda at: history(runtime, at))
