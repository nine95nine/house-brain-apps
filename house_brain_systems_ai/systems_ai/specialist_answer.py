"""house_brain_specialist_answer.v1: a specialist App's read-only answer to one Systems AI question (A4).

Canonical source (design docs/house-brain/A4_SPECIALIST_QUESTIONS_DESIGN_R1_2026-11-09.md §2-§5). Each
specialist App carries a byte-identical copy (no shared runtime dependency between Apps);
tests/test_specialist_answer_contract.py checks the copies. Standard library only.

An App serves the answer at GET /answer?topic=<topic> on the internal Supervisor network only, the
same way it serves /health. respond() is the whole route: it reads nothing but what the App passes
in (its own health block, its hold reason and one answer function over data it already holds), so a
question never causes a Home Assistant read, a network call or a store read.

Query rule (one rule for every App): the query must be exactly `topic=<topic>`. Anything else (no
query, unknown or repeated keys, encoded characters, a topic the App does not answer) is a 200
NOT_MY_TOPIC answer, never an error and never a guess. Every other method on /answer is 405.

Statuses: ANSWERED (at least one fact worth knowing), NOTHING_SEEN (everything checked, nothing
found), NOT_CHECKED (something could not be checked; the reason is in not_checked as
`<part>:<REASON_CODE>`), NOT_MY_TOPIC. Missing or stale inputs, a kill-switch hold, no finished
cycle or an overdue loop are NOT_CHECKED, never NOTHING_SEEN. One input the App watches being missing,
stale or unusable at its last cycle is itself a finding: it is a fact (with the input's own evidence
and age) and the input is also named in not_checked as `<part>:<REASON_CODE>`, so the answer is
ANSWERED and still says what could not be checked. No advice, no proposal, no authority.

Systems AI reads an answer with observe(): no answer, an invalid or a stale answer is recorded as
not checked, never as "nothing wrong".
"""
import json
import math
import re

SCHEMA = 'house_brain_specialist_answer.v1'
STATUSES = ('ANSWERED', 'NOTHING_SEEN', 'NOT_CHECKED', 'NOT_MY_TOPIC')
AUTHORITY = 'NO_EXECUTION_AUTHORITY'
MAX_FACTS = 5
MAX_TEXT = 160
MAX_NOT_CHECKED = 8
MAX_BYTES = 4096
MAX_QUERY = 64
MAX_FUTURE_S = 60
MAX_ANSWER_AGE_S = 300
KEYS = {'schema', 'app', 'version', 'topic', 'at', 'status', 'facts', 'not_checked', 'authority'}
FACT_KEYS = {'text', 'evidence', 'age_s'}
APP_RE = re.compile(r'house_brain_[a-z0-9_]{1,48}')
VERSION_RE = re.compile(r'[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(-[a-z0-9]{1,16})?')
TOPIC_RE = re.compile(r'[a-z][a-z0-9_]{0,31}')
QUERY_RE = re.compile(r'topic=([a-z][a-z0-9_]{0,31})')
CODE_RE = re.compile(r'[A-Z][A-Z0-9_]{2,63}')
GAP_RE = re.compile(r'[a-z][a-z0-9_]{0,47}(:[A-Z][A-Z0-9_]{2,63})?')
# Evidence: an entity id with the time it was read, or one of the App's own records (optionally timed).
EVIDENCE_RE = re.compile(r'[a-z_]{1,32}\.[a-z0-9_]{1,160}@[0-9]{1,16}|record:[a-z0-9_.:-]{1,96}(@[0-9]{1,16})?')
# Never a person, presence, place, camera, lock, alarm or media id, and no control characters or '@' in text.
PRIVATE_RE = re.compile(r'(?<![a-z0-9_])(person|device_tracker|zone|camera|lock|alarm_control_panel|media_player)'
                        r'\.[a-z0-9_]')
TEXT_BAD_RE = re.compile(r'[\x00-\x1f\x7f@<>]')
HEALTH_OK = 'OK'


def _int(value, lo, hi):
    return type(value) is int and lo <= value <= hi


def stamp(value):
    """Whole Unix seconds, or None for anything that is not a finite time."""
    if type(value) in (int, float) and math.isfinite(value) and 1 <= value <= 2**53:
        return int(value)
    return None


def text(value):
    """One line, at most MAX_TEXT characters (cut with an ellipsis)."""
    line = TEXT_BAD_RE.sub(' ', str(value)).strip()
    return line if len(line) <= MAX_TEXT else line[:MAX_TEXT - 1] + '…'


def entity(entity_id, at):
    return f'{entity_id}@{stamp(at) or 0}'


def record(name, at=None):
    t = stamp(at)
    return f'record:{name}' if t is None else f'record:{name}@{t}'


def fact(words, evidence, at, seen_at):
    """One fact: its text, its evidence and how old the evidence is (seconds, never negative)."""
    seen, now = stamp(seen_at), stamp(at) or 1
    return {'text': text(words), 'evidence': evidence, 'age_s': max(0, now - seen) if seen is not None else 0}


def gap(part, code=None):
    """A not_checked item: the part, and why when it is known (`part:REASON_CODE`)."""
    return part if code is None else f'{part}:{code}'


def decide(problem, facts, not_checked):
    """ANSWERED when something worth knowing was found; else NOT_CHECKED when anything was not checked;
    else NOTHING_SEEN. A gap never reads as nothing seen."""
    if problem and facts:
        return 'ANSWERED'
    if not_checked:
        return 'NOT_CHECKED'
    return 'NOTHING_SEEN'


def topic_of(query):
    """The asked topic when the query is exactly `topic=<topic>`, else None."""
    if not isinstance(query, str) or len(query) > MAX_QUERY:
        return None
    match = QUERY_RE.fullmatch(query)
    return match.group(1) if match else None


def _evidence_at(evidence):
    return int(evidence.rsplit('@', 1)[1]) if '@' in evidence else None


def validate(doc):
    """Return a sorted list of problems; [] means a valid v1 answer."""
    if not isinstance(doc, dict):
        return ['SPECIALIST_ANSWER_NOT_OBJECT']
    if set(doc) != KEYS:
        return ['SPECIALIST_ANSWER_KEYS_INVALID']
    problems = []
    if doc['schema'] != SCHEMA:
        problems.append('SPECIALIST_ANSWER_SCHEMA_INVALID')
    if not isinstance(doc['app'], str) or not APP_RE.fullmatch(doc['app']):
        problems.append('SPECIALIST_ANSWER_APP_INVALID')
    if not isinstance(doc['version'], str) or not VERSION_RE.fullmatch(doc['version']):
        problems.append('SPECIALIST_ANSWER_VERSION_INVALID')
    if not isinstance(doc['topic'], str) or not TOPIC_RE.fullmatch(doc['topic']):
        problems.append('SPECIALIST_ANSWER_TOPIC_INVALID')
    if not _int(doc['at'], 1, 2**53):
        problems.append('SPECIALIST_ANSWER_TIME_INVALID')
    if doc['status'] not in STATUSES:
        problems.append('SPECIALIST_ANSWER_STATUS_INVALID')
    if doc['authority'] != AUTHORITY:
        problems.append('SPECIALIST_ANSWER_AUTHORITY_INVALID')
    facts, gaps = doc['facts'], doc['not_checked']
    if not isinstance(facts, list) or len(facts) > MAX_FACTS:
        problems.append('SPECIALIST_ANSWER_FACTS_INVALID')
    else:
        for f in facts:
            if (not isinstance(f, dict) or set(f) != FACT_KEYS or not isinstance(f['text'], str)
                    or not 1 <= len(f['text']) <= MAX_TEXT or TEXT_BAD_RE.search(f['text'])
                    or not isinstance(f['evidence'], str) or len(f['evidence']) > 200
                    or not EVIDENCE_RE.fullmatch(f['evidence']) or not _int(f['age_s'], 0, 2**31)):
                problems.append('SPECIALIST_ANSWER_FACTS_INVALID')
            elif PRIVATE_RE.search(f['text']) or PRIVATE_RE.search(f['evidence']):
                problems.append('SPECIALIST_ANSWER_PRIVATE_DATA')
            elif type(doc['at']) is int and (_evidence_at(f['evidence']) or 0) > doc['at'] + MAX_FUTURE_S:
                problems.append('SPECIALIST_ANSWER_EVIDENCE_FROM_FUTURE')
    if (not isinstance(gaps, list) or len(gaps) > MAX_NOT_CHECKED
            or not all(isinstance(g, str) and GAP_RE.fullmatch(g) for g in gaps) or len(set(gaps)) != len(gaps)):
        problems.append('SPECIALIST_ANSWER_NOT_CHECKED_INVALID')
    if problems:
        return sorted(set(problems))
    status = doc['status']
    if status == 'NOT_MY_TOPIC' and (facts or gaps):
        problems.append('SPECIALIST_ANSWER_NOT_MY_TOPIC_WITH_CONTENT')
    if status == 'ANSWERED' and not facts:
        problems.append('SPECIALIST_ANSWER_ANSWERED_WITHOUT_FACTS')
    if status == 'NOTHING_SEEN' and gaps:
        problems.append('SPECIALIST_ANSWER_NOTHING_SEEN_WITH_GAPS')
    if status == 'NOT_CHECKED' and not any(':' in g for g in gaps):
        problems.append('SPECIALIST_ANSWER_NOT_CHECKED_WITHOUT_REASON')
    try:
        if len(encode(doc).encode()) > MAX_BYTES:
            problems.append('SPECIALIST_ANSWER_TOO_LARGE')
    except (TypeError, ValueError):
        problems.append('SPECIALIST_ANSWER_NOT_JSON')
    return sorted(set(problems))


def encode(doc):
    return json.dumps(doc, allow_nan=False, sort_keys=True, separators=(',', ':'))


def build(app, version, topic, at, status, facts=(), not_checked=()):
    """A valid answer, or (if the parts do not make one) a fixed NOT_CHECKED answer that says so."""
    gaps = []
    for g in not_checked:
        if g not in gaps:
            gaps.append(g)
    doc = {'schema': SCHEMA, 'app': app, 'version': version,
           'topic': topic if isinstance(topic, str) and TOPIC_RE.fullmatch(topic) else 'unknown',
           'at': stamp(at) or 1, 'status': status, 'facts': list(facts)[:MAX_FACTS],
           'not_checked': gaps[:MAX_NOT_CHECKED], 'authority': AUTHORITY}
    if validate(doc):
        doc.update(status='NOT_CHECKED', facts=[], not_checked=[gap('all', 'ANSWER_SELF_CHECK_FAILED')])
    return doc


def from_health(health, at, extra=()):
    """The `self` topic: the App's own health detail from its own house_brain_app_health.v1 block (inputs
    missing, last cycle, store size and age). `extra` adds App facts (at most MAX_FACTS in all)."""
    cycle, data, res = health['cycle'], health['data'], health['resources']
    last, good = cycle['last_at'], cycle['last_good_at']
    reasons = list(health['reasons'])
    words = f"Health {health['health']}, activity {health['activity']}"
    facts = [fact(words + ('; reasons: ' + ', '.join(reasons) if reasons else ''), record('health', at), at, at)]
    seconds = cycle['last_seconds']
    facts.append(fact(f"Last cycle {max(0, at - last)} s ago" + (f" ({seconds:.1f} s long)" if seconds is not None else '')
                      + (f"; last good cycle {max(0, at - good)} s ago" if good is not None else '; no good cycle yet'),
                      record('cycle', last), at, last))
    facts.append(fact(f"Inputs {data['status']}: {data['good']} good, {data['idle']} idle, {data['bad']} bad of "
                      f"{data['inputs']}", record('inputs', last), at, last))
    gaps = []
    if res['db_bytes'] is None:
        gaps.append(gap('store', 'STORE_SIZE_UNKNOWN'))
    else:
        written = good if good is not None else last
        facts.append(fact(f"Store {res['db_bytes'] / 1048576:.1f} MB on disk; last written by a good cycle "
                          + (f"{max(0, at - written)} s ago" if good is not None else 'never'),
                          record('store', written), at, written))
    facts.extend(list(extra)[:MAX_FACTS - len(facts)])
    return decide(health['health'] != HEALTH_OK, facts, gaps), facts, gaps


def respond(app, version, topics, query, now, held, health, answer, extra=None):
    """The whole GET /answer route. `held` is the App's kill-switch hold reason (None when not held),
    `health` its own health block at serve time, answer(topic, at) -> (problem, facts, not_checked)
    answers its own topics from data it already holds, and extra(at) -> [fact] adds App facts to `self`."""
    at = stamp(now) or 1
    asked = topic_of(query)

    def out(status, facts=(), gaps=()):
        return build(app, version, asked, at, status, facts, gaps)

    if asked is None or asked not in topics:
        return out('NOT_MY_TOPIC')
    if held is not None:
        return out('NOT_CHECKED', gaps=[gap('all', held if isinstance(held, str) and CODE_RE.fullmatch(held)
                                             else 'KILL_SWITCH_HELD')])
    try:
        cycle = health['cycle']
        if health['activity'] == 'HELD':
            return out('NOT_CHECKED', gaps=[gap('all', 'KILL_SWITCH_HELD')])
        if cycle['last_at'] is None:
            return out('NOT_CHECKED', gaps=[gap('all', 'NO_CYCLE_YET')])
        if asked == 'self':
            return out(*from_health(health, at, extra(at) if extra is not None else ()))
        if 'CYCLE_OVERDUE' in health['reasons']:
            return out('NOT_CHECKED', gaps=[gap('all', 'CYCLE_OVERDUE')])
        problem, facts, gaps = answer(asked, at)
        return out(decide(problem, facts, gaps), facts, gaps)
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
        return out('NOT_CHECKED', gaps=[gap('all', 'ANSWER_FAILED')])


# -- consumer side (Systems AI) --------------------------------------------------------------------
def _no_duplicates(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError('SPECIALIST_ANSWER_DUPLICATE_KEY')
        seen[key] = value
    return seen


def _no_constant(value):
    raise ValueError('SPECIALIST_ANSWER_NOT_JSON')


def parse(raw):
    """Strict parse of a served body: bounded, no duplicate keys, no NaN/Infinity."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_BYTES:
        raise ValueError('SPECIALIST_ANSWER_TOO_LARGE')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError('SPECIALIST_ANSWER_NOT_JSON')


def observe(raw, now, expected_app=None, expected_topic=None):
    """Consumer view: {'status', 'reasons', 'doc'}. No answer, an unreadable, invalid, mismatched, stale
    or future answer reads NOT_CHECKED with SPECIALIST_ANSWER_INVALID (or why); never NOTHING_SEEN."""
    if raw is None:
        return {'status': 'NOT_CHECKED', 'reasons': ['SPECIALIST_UNREACHABLE'], 'doc': None}
    try:
        doc = parse(raw) if isinstance(raw, (bytes, bytearray)) else raw
    except ValueError as error:
        return {'status': 'NOT_CHECKED', 'reasons': ['SPECIALIST_ANSWER_INVALID', str(error)], 'doc': None}
    problems = validate(doc)
    if not problems and expected_app is not None and doc['app'] != expected_app:
        problems = ['SPECIALIST_ANSWER_APP_MISMATCH']
    if not problems and expected_topic is not None and doc['topic'] != expected_topic:
        problems = ['SPECIALIST_ANSWER_TOPIC_MISMATCH']
    if problems:
        return {'status': 'NOT_CHECKED', 'reasons': ['SPECIALIST_ANSWER_INVALID'] + problems, 'doc': None}
    t = stamp(now)
    if t is None:
        return {'status': 'NOT_CHECKED', 'reasons': ['SPECIALIST_ANSWER_NOW_INVALID'], 'doc': None}
    if doc['at'] > t + MAX_FUTURE_S:
        return {'status': 'NOT_CHECKED', 'reasons': ['SPECIALIST_ANSWER_FROM_FUTURE'], 'doc': None}
    if t - doc['at'] > MAX_ANSWER_AGE_S:
        return {'status': 'NOT_CHECKED', 'reasons': ['SPECIALIST_ANSWER_STALE'], 'doc': None}
    return {'status': doc['status'], 'reasons': [], 'doc': doc}
