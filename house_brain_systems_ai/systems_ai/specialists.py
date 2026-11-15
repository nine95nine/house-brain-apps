"""Specialist questions (Systems AI 0.3.13; A4 design docs/house-brain/A4_SPECIALIST_QUESTIONS_DESIGN_R1_2026-11-09.md,
owner pop-ups 2026-11-09 "Approve and merge" and 2026-11-14 "Merge, then continue").

The five specialist Apps (Energy, Cooling, Water, House Health and Pool AI) serve a read-only
`GET /answer?topic=<topic>` on the internal Supervisor network, where they serve /health. Systems AI asks one of them
about a live investigation whose problem keys match a fixed route, and keeps the validated answer on the investigation.
investigations.answer() turns it into one SPECIALIST hypothesis line, ranked below a matching playbook and above public
research. The answer is evidence only: never a suggestion, never an action, never a change of authority.

Rules:
- ROUTES and LOG_ROUTES are a fixed, reviewed table (like radar.KEV_ALIASES). A problem key with no route asks nothing;
  there is no guess.
- At most one question per investigation per hour, at most ANSWERS_PER_DAY questions per specialist per local day
  (counters kept in the store, so a restart does not reset them), at most MAX_ASKS_PER_CYCLE per cycle, nothing while
  Systems AI is held, and only to an App the last slow cycle saw `started`.
- The answer is checked strictly by observe() of the byte-identical shared module specialist_answer.py: bounded body, no
  duplicate keys, no unknown field at any level, schema, app (must be the App asked), topic (must be the topic asked),
  status, authority, at most 5 facts of at most 160 characters, bounded evidence and age, not stale and not from the
  future. Anything else is recorded as SPECIALIST_ANSWER_INVALID.
- No answer, an invalid answer, NOT_CHECKED or NOT_MY_TOPIC is shown as "not checked (reason)", never as "nothing wrong".

Not routed (no reliable signal in Systems AI, so no question; design §3 rows left out on purpose):
- `HA:WEBSOCKET_UNREADABLE` "during an outage": Systems AI holds no outage signal of its own (outage class is House
  Health AI's own data), and a WebSocket read failure alone is not an outage.
- "Arbiter entities" and "leak sensors": Systems AI raises no problem key for single entities, so nothing can match.
"""
import fnmatch
import re
from datetime import datetime, timedelta, timezone

from . import specialist_answer
from .transport import TransportError

PHOENIX = timezone(timedelta(hours=-7))
NAMES = {'house_brain_energy_ai': 'Energy AI', 'house_brain_cooling_ai': 'Cooling AI',
         'house_brain_water_ai': 'Water AI', 'house_brain_house_health_ai': 'House Health AI',
         'house_brain_pool_ai': 'Pool AI'}
TOPIC_WORDS = {'self': 'its own health', 'power_data': 'power data', 'cooling_data': 'cooling data',
               'water_data': 'water data', 'power_network': 'power and network', 'pool_data': 'pool data'}
# LOG:<integration> routes: (fnmatch pattern on the integration name, App, topic). Design §3 names; the integration is
# logs.integration() of the logger (`homeassistant.components.<x>` / `custom_components.<x>`), i.e. the HA domain.
LOG_ROUTES = (
    ('sense', 'house_brain_energy_ai', 'power_data'),
    ('enphase_envoy', 'house_brain_energy_ai', 'power_data'),
    ('enphase*', 'house_brain_energy_ai', 'power_data'),
    ('ecobee', 'house_brain_cooling_ai', 'cooling_data'),
    ('*cooling*', 'house_brain_cooling_ai', 'cooling_data'),
    ('rachio', 'house_brain_water_ai', 'water_data'),
    ('bhyve', 'house_brain_water_ai', 'water_data'),
    ('nut', 'house_brain_house_health_ai', 'power_network'),
    ('ping', 'house_brain_house_health_ai', 'power_network'),
    ('netgear', 'house_brain_house_health_ai', 'power_network'),
    ('google_wifi', 'house_brain_house_health_ai', 'power_network'),
    ('njspc*', 'house_brain_pool_ai', 'pool_data'),
    ('aquarite*', 'house_brain_pool_ai', 'pool_data'),
    ('ondilo*', 'house_brain_pool_ai', 'pool_data'),
    ('jandy*', 'house_brain_pool_ai', 'pool_data'),
)
# APP:<slug>:<kind> for one of the five specialists -> that App, topic `self`. The slug is <prefix>_<name>, prefix
# `local` or the 8-hex store prefix (runtime.discover()).
RE_APP_KEY = re.compile(r'APP:(?:local|[0-9a-f]{8})_(house_brain_[a-z0-9_]{1,48}):[A-Z_]{1,32}')
RE_LOG_KEY = re.compile(r'LOG:([a-z0-9_]{1,48})')
ROUTES_PER_INVESTIGATION = 2       # routes kept and shown per investigation (one question an hour among them)
ASK_EVERY_S = 3600                 # at most one question per investigation per hour
ANSWERS_PER_DAY = 24               # at most this many questions per specialist per local day
MAX_ASKS_PER_CYCLE = 5             # each question is one bounded 5 s GET; a cycle never waits longer than this
KEEP_ANSWER_S = 2 * 3600           # an older answer reads "not checked" (SPECIALIST_ANSWER_OLD), never as current
KEEP_DAYS = 3
LIVE_CASES = ('OPEN', 'ACTION_SUGGESTED', 'WATCHING')     # = investigations.LIVE (no import cycle)
MAX_REASONS = 4
NOT_ASKED_REASON = 'SPECIALIST_NOT_ASKED_YET'
NOT_FOUND_REASON = 'SPECIALIST_NOT_FOUND'
NOT_STARTED_REASON = 'SPECIALIST_NOT_STARTED'
BUDGET_REASON = 'SPECIALIST_DAILY_BUDGET_SPENT'
TARGET_REASON = 'SPECIALIST_TARGET_REFUSED'
OLD_REASON = 'SPECIALIST_ANSWER_OLD'
INVALID_REASON = 'SPECIALIST_ANSWER_INVALID'
UNREACHABLE_REASON = 'SPECIALIST_UNREACHABLE'


def route(key):
    """(app, topic) for one problem key, or None. Fixed table only: no route is no question."""
    if not isinstance(key, str):
        return None
    m = RE_APP_KEY.fullmatch(key)
    if m:
        return (m.group(1), 'self') if m.group(1) in NAMES else None
    m = RE_LOG_KEY.fullmatch(key)
    if m:
        for pattern, app, topic in LOG_ROUTES:
            if fnmatch.fnmatchcase(m.group(1), pattern):
                return app, topic
    return None


def routes(problems):
    """The distinct routes of a case's problem keys, in problem order, at most ROUTES_PER_INVESTIGATION."""
    out = []
    for key in problems or ():
        r = route(key)
        if r is not None and r not in out:
            out.append(r)
    return out[:ROUTES_PER_INVESTIGATION]


def local_day(at):
    return datetime.fromtimestamp(at, PHOENIX).date().isoformat()


def record(app, topic, now, observed=None, reasons=None):
    """What is kept on the investigation for one route: the validated answer, or why there is none."""
    doc = (observed or {}).get('doc')
    if doc is None:
        why = list(reasons if reasons is not None else (observed or {}).get('reasons') or [INVALID_REASON])
        if reasons is None and why[0] not in (INVALID_REASON, UNREACHABLE_REASON):
            why.insert(0, INVALID_REASON)        # stale, from the future or an unreadable clock: still invalid
        return {'app': app, 'topic': topic, 'asked_at': int(now), 'status': 'NOT_CHECKED',
                'reasons': why[:MAX_REASONS], 'at': None, 'version': None, 'facts': [], 'not_checked': []}
    return {'app': app, 'topic': topic, 'asked_at': int(now), 'status': doc['status'], 'reasons': [],
            'at': doc['at'], 'version': doc['version'], 'facts': [dict(f) for f in doc['facts']],
            'not_checked': list(doc['not_checked'])}


class Asker:
    """Asks the specialists for live investigations, within the budgets. State: the investigation items (kept in the
    Investigations store record) and the per-specialist daily counters ('specialists:budget')."""

    def __init__(self, store):
        self.store = store
        self.budget = store.get('specialists:budget', {})
        self.last = {'asked': 0, 'held': False, 'at': None}

    def used(self, app, now):
        return self.budget.get(local_day(now), {}).get(app, 0)

    def step(self, investigations, cases, targets, client, now, held=False):
        """investigations: the Investigations object; cases: {id: case}; targets: {app name: {'state', 'hostname',
        'port'}} from the last slow cycle; client.answer(hostname, port, topic) -> bytes or None. Returns how many
        questions were sent. Nothing at all while held."""
        if held:
            self.last = {'asked': 0, 'held': True, 'at': now}
            return 0
        items = investigations.state['items']
        sent = touched = 0
        for item in sorted(items.values(), key=lambda i: (i['opened_at'], i['id'])):
            if item.get('closed_at') is not None or item['status'] in ('CLOSED', 'EXPIRED'):
                continue
            case = cases.get(item['case'])
            wanted = routes(case['problems']) if case and case['status'] in LIVE_CASES else []
            if not wanted:
                continue
            rec = item.setdefault('specialist', {'asked_at': None, 'answers': {}})
            if rec['asked_at'] is not None and now - rec['asked_at'] < ASK_EVERY_S:
                continue
            if sent >= MAX_ASKS_PER_CYCLE:
                break
            # The route never asked first, then the one asked longest ago (table order breaks ties).
            app, topic = min(wanted, key=lambda r: (rec['answers'].get(f'{r[0]}:{r[1]}') or {}).get('asked_at', -1))
            rec['asked_at'] = int(now)
            touched += 1
            result = self.ask(app, topic, targets.get(app), client, now)
            sent += bool(result.pop('sent', False))
            rec['answers'][f'{app}:{topic}'] = result
            for key in [k for k in rec['answers'] if tuple(k.split(':', 1)) not in wanted]:
                rec['answers'].pop(key)
        for day in sorted(self.budget)[:-KEEP_DAYS]:
            self.budget.pop(day)
        if touched:
            self.store.put('specialists:budget', self.budget)
            self.store.put('investigations:state', investigations.state)
        self.last = {'asked': sent, 'held': False, 'at': now}
        return sent

    def ask(self, app, topic, target, client, now):
        if not isinstance(target, dict):
            return record(app, topic, now, reasons=[NOT_FOUND_REASON])
        if target.get('state') != 'started':
            return record(app, topic, now, reasons=[NOT_STARTED_REASON])
        if self.used(app, now) >= ANSWERS_PER_DAY:
            return record(app, topic, now, reasons=[BUDGET_REASON])
        day = self.budget.setdefault(local_day(now), {})
        day[app] = day.get(app, 0) + 1          # counted before the read: a failed read still used one
        try:
            raw = client.answer(target.get('hostname'), target.get('port'), topic)
        except TransportError:
            day[app] -= 1                       # refused before any request: nothing was sent
            return record(app, topic, now, reasons=[TARGET_REASON])
        observed = specialist_answer.observe(raw, now, expected_app=app, expected_topic=topic)
        return {**record(app, topic, now, observed), 'sent': True}

    def report(self, now):
        return {'asked_today': dict(self.budget.get(local_day(now), {})), 'per_day_limit': ANSWERS_PER_DAY,
                'ask_every_s': ASK_EVERY_S, 'last_cycle': dict(self.last),
                'note': 'Specialist answers are evidence only; they never suggest or act.'}


def line(rec, app, topic, now):
    """One SPECIALIST hypothesis for one route. `explains` is True only for an ANSWERED answer; nothing seen, not
    checked or no answer never explains a case and never reads as "nothing wrong"."""
    name = NAMES.get(app, app)          # never a KeyError, even for a route the table should not hold
    stable = f'specialist:{app}:{topic}'
    if rec is None:
        return _not_checked(name, stable, NOT_ASKED_REASON, [stable])
    evidence = [f"{stable}@{rec['at'] if rec['at'] is not None else rec['asked_at']}"]
    if now is not None and now - rec['asked_at'] > KEEP_ANSWER_S:
        return _not_checked(name, stable, OLD_REASON, evidence)
    status = rec['status']
    if status == 'ANSWERED' and rec['facts']:
        n = len(rec['facts'])
        said = '; '.join(f['text'] for f in rec['facts'])
        gaps = f" Not checked: {', '.join(rec['not_checked'][:3])}." if rec['not_checked'] else ''
        text = f"{name} says ({n} fact{'s' if n != 1 else ''}): {said}"
        text = (text if len(text) <= 400 else text[:399] + '…') + gaps
        keys = sorted(f['evidence'].rsplit('@', 1)[0] for f in rec['facts'])
        return {'kind': 'SPECIALIST', 'text': text, 'evidence': evidence, 'explains': True,
                'fact': f"{stable}:ANSWERED:{'|'.join(keys)}:{'|'.join(rec['not_checked'])}"}
    if status == 'NOTHING_SEEN':
        return {'kind': 'SPECIALIST', 'text': f'{name} saw nothing unusual in {TOPIC_WORDS.get(topic, topic)}',
                'evidence': evidence, 'explains': False, 'fact': f'{stable}:NOTHING_SEEN'}
    if status == 'NOT_MY_TOPIC':
        why = 'NOT_MY_TOPIC'
    elif rec['reasons']:
        why = ', '.join(rec['reasons'][:2])
    else:
        why = ', '.join(rec['not_checked'][:2]) or 'NOT_CHECKED'
    return _not_checked(name, stable, why, evidence)


def _not_checked(name, stable, why, evidence):
    return {'kind': 'SPECIALIST', 'text': f'{name}: not checked ({why})', 'evidence': evidence, 'explains': False,
            'fact': f'{stable}:NOT_CHECKED:{why}'}


def lines(problems, rec, now):
    """The SPECIALIST lines for a case: one per route, in route order."""
    answers = (rec or {}).get('answers') or {}
    return [line(answers.get(f'{app}:{topic}'), app, topic, now) for app, topic in routes(problems)]
