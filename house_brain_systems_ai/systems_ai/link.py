"""House link (Systems AI 0.3.5; design docs/house-brain/MACBOOK_HOUSE_LINK_R1_2026-11-08.md, owner pop-ups 2026-11-07
"it needs to be integrated", "read and write", 2026-11-08 approval).

One port on the home network for the MacBook research service, off until the owner sets both options:
- ``link_peer``: the MacBook's address. Any other address gets 403 with no body.
- ``link_key``: a long random key, sent as ``Authorization: Bearer <key>`` and compared in constant time.

Routes:
- ``GET /link/v1/state``: the kill switch as Systems AI reads it, Systems AI's own hold, the House Brain App
  states and versions, and open case counts.
- ``GET /link/v1/questions``: open investigations (id, question, problem keys, READ_RESEARCH_ONLY, expiry).
- ``POST /link/v1/answers``: one ``house_brain_research_result.v1`` per request. It is validated strictly (0.3.7: no
  duplicate keys and no unknown fields at any level) and queued;
  the runtime applies it on its own thread as untrusted evidence. It is never a proposal and never an action.
- ``GET /link/v1/health``: Systems AI's own house_brain_app_health.v1 block.
- ``GET /link/v1/readings[?ids=a,b]`` (0.3.6): the privacy-filtered entity snapshot (readings.py), ids only without a query,
  at most 50 entities with one.
- ``POST /link/v1/findings`` (0.3.14, A3b design R1 §4, owner pop-up 2026-11-15 "Approve and merge"): one
  ``house_brain_research_finding.v1`` per request, from the weekly curiosity routine (``cq-<role>-<yyyy>w<ww>``) or the AI
  model watch (``mw-<yyyy>w<ww>``). Validated as strictly as an answer, at most 20 a Phoenix day (separate from the 120
  answers), and kept by findings.py only as reading material for the owner. A finding never reaches a case, an
  investigation, a hypothesis, a suggestion, O&M, Maintenance or the Deployer.

- ``GET /link/v1/cases`` and ``POST /link/v1/repair`` (0.3.15, A6.1): the O&M investigator's two signed routes
  (onm_link.py). Off until the owner also sets ``onm_key``. They are checked by signature, not by ``link_key``.

While Systems AI is held, questions are empty and answers and findings are refused (409). Nothing here can call a Home Assistant
service, write a state or start, stop or change anything. Control authority stays NONE.
"""
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import ipaddress
import json
import re
import threading
import time
import urllib.parse

from . import onm_link

PORT = 8791
MAX_BODY = 64 * 1024
MAX_PENDING = 50
ANSWERS_PER_DAY = 120
MAX_ITEMS = 20
MAX_QUESTIONS = 15
KEY_MIN = 32
RESULT_SCHEMA = 'house_brain_research_result.v1'
RESULT_STATUS = ('ANSWERED', 'NOTHING_FOUND', 'NOT_CHECKED', 'QUESTION_REFUSED')
SOURCE_STATUS = ('OK', 'FAILED', 'SKIPPED')
RE_TASK = re.compile(r'inv-[A-Za-z0-9:_.\-]{1,96}')
RE_CODE = re.compile(r'[A-Za-z0-9:_.\-]{0,64}')
TEXT = {'title': 300, 'source': 40, 'date': 40}
URL_MAX = 500
# 0.3.7 (idea register X1, from Harness R4 wire admission): an answer may carry only these keys, each once.
RESULT_KEYS = frozenset({'schema', 'task', 'role', 'question', 'status', 'items', 'not_checked', 'permission',
                         'authority'})
RESULT_REQUIRED = frozenset({'schema', 'task', 'status', 'items', 'permission', 'authority'})
ITEM_KEYS = frozenset({'title', 'url', 'source', 'date', 'untrusted'})
SOURCE_KEYS = frozenset({'source', 'status', 'code'})
# 0.3.14 (A3b design R1 §4): the findings route. Exactly these keys, each once, at every level.
FINDING_SCHEMA = 'house_brain_research_finding.v1'
FINDING_KEYS = frozenset({'schema', 'id', 'role', 'kind', 'question', 'status', 'items', 'not_checked', 'permission',
                          'authority'})
FINDING_ROLES = ('energy', 'cooling', 'water', 'pool', 'house_health', 'systems', 'ai')
KIND_BY_PREFIX = {'cq': 'CURIOSITY', 'mw': 'MODEL_WATCH'}          # id prefix -> kind; they must agree
RESEARCH_STATUS = ('FOUND', 'NOTHING_FOUND', 'NOT_CHECKED', 'QUESTION_REFUSED')
FINDING_MAX_ITEMS = 10
FINDINGS_PER_DAY = 20                                                # a Phoenix day; separate from ANSWERS_PER_DAY
RE_FINDING_ID = re.compile(r'(?:cq-(?P<role>[a-z_]{2,12})|mw)-20[0-9]{2}w(?:0[1-9]|[1-4][0-9]|5[0-3])')
PHOENIX_OFFSET_S = 7 * 3600                                          # Arizona keeps UTC-7 all year


def canonical_url(url):
    """0.3.8 (idea register X2): one page, one source. Lower-case host without 'www.', no fragment, no utm_*
    tracking parameters, no trailing slash. Used only to count sources; the stored URL is unchanged."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or '').lower().rstrip('.')
    host = host[4:] if host.startswith('www.') else host
    query = urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
                                    if not k.lower().startswith('utm_')])
    return host, f"{host}{parts.path.rstrip('/') or '/'}" + (f'?{query}' if query else '')


def independence(items):
    """(distinct sources, distinct sites, duplicates collapsed) for validated items. A site is the host: two pages
    on one site corroborate each other less than two sites, and a repeated page is one source, never two."""
    pages, sites = set(), set()
    for it in items:
        host, page = canonical_url(it['url'])
        pages.add(page)
        sites.add(host)
    return len(pages), len(sites), len(items) - len(pages)


class LinkError(ValueError):
    """A fixed reason code; never echoes the request."""


def configured(options):
    """(key, peer) when both options are usable, else None (the link stays off)."""
    key = options.get('link_key') if isinstance(options, dict) else None
    peer = options.get('link_peer') if isinstance(options, dict) else None
    if not isinstance(key, str) or len(key) < KEY_MIN or not isinstance(peer, str):
        return None
    try:
        address = ipaddress.ip_address(peer.strip())
    except ValueError:
        return None
    if address.is_loopback or address.is_unspecified or address.is_multicast:
        return None
    return key, str(address)


def _text(value, limit, field):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise LinkError('ANSWER_FIELD_INVALID:' + field)
    return value


def _url(value):
    if not isinstance(value, str) or len(value) > URL_MAX or not value.startswith('https://') or any(
            c in value for c in ' \t\r\n"<>'):
        raise LinkError('ANSWER_URL_INVALID')
    return value


def validate(doc):
    """A research result reduced to what Systems AI keeps: status, at most 10 items, the sources not checked."""
    if not isinstance(doc, dict) or doc.get('schema') != RESULT_SCHEMA:
        raise LinkError('ANSWER_SCHEMA_INVALID')
    if not RESULT_REQUIRED <= doc.keys() <= RESULT_KEYS:
        raise LinkError('ANSWER_FIELDS_INVALID')
    for field in ('role', 'question'):                      # carried by the client, never kept here
        if doc.get(field) is not None:
            _text(doc[field], TEXT['title'], field)
    if doc.get('permission') != 'READ_RESEARCH_ONLY' or doc.get('authority') != 'NO_EXECUTION_AUTHORITY':
        raise LinkError('ANSWER_AUTHORITY_INVALID')
    task = doc.get('task')
    if not isinstance(task, str) or not RE_TASK.fullmatch(task):
        raise LinkError('ANSWER_TASK_INVALID')
    if doc.get('status') not in RESULT_STATUS:
        raise LinkError('ANSWER_STATUS_INVALID')
    items = doc.get('items')
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise LinkError('ANSWER_ITEMS_INVALID')
    kept = []
    for it in items:
        if not isinstance(it, dict) or it.get('untrusted') is not True:
            raise LinkError('ANSWER_ITEM_NOT_UNTRUSTED')
        if not it.keys() <= ITEM_KEYS:
            raise LinkError('ANSWER_FIELDS_INVALID')
        kept.append({'title': _text(it.get('title', ''), TEXT['title'], 'title'), 'url': _url(it.get('url')),
                     'source': _text(it.get('source', ''), TEXT['source'], 'source'),
                     'date': _text(it.get('date') or '', TEXT['date'], 'date')})
    not_checked = []
    for row in doc.get('not_checked') or []:
        if not isinstance(row, dict) or row.get('status') not in SOURCE_STATUS or not row.keys() <= SOURCE_KEYS:
            raise LinkError('ANSWER_SOURCES_INVALID')
        code = row.get('code') or ''
        if not isinstance(code, str) or not RE_CODE.fullmatch(code):
            raise LinkError('ANSWER_SOURCES_INVALID')
        not_checked.append({'source': _text(row.get('source', ''), TEXT['source'], 'source'),
                            'status': row['status'], 'code': code})
    if len(not_checked) > MAX_ITEMS:
        raise LinkError('ANSWER_SOURCES_INVALID')
    unique, seen = [], set()
    for it in kept:                                      # 0.3.8: a repeated page never crowds out another source
        page = canonical_url(it['url'])[1]
        if page not in seen:
            seen.add(page)
            unique.append(it)
    return {'task': task, 'status': doc['status'], 'items': unique[:10], 'not_checked': not_checked,
            'untrusted': True, 'authority': 'NO_EXECUTION_AUTHORITY'}


def _finding_text(value, limit, field):
    try:
        return _text(value, limit, field)
    except LinkError:
        raise LinkError('FINDING_FIELD_INVALID:' + field) from None


def _finding_url(value):
    try:
        return _url(value)
    except LinkError:
        raise LinkError('FINDING_URL_INVALID') from None


def validate_finding(doc):
    """0.3.14: one curiosity or model-watch finding, reduced to what the page shows. Untrusted reading material only:
    it carries no task, so it can never be matched to an investigation."""
    if not isinstance(doc, dict) or doc.get('schema') != FINDING_SCHEMA:
        raise LinkError('FINDING_SCHEMA_INVALID')
    if doc.keys() != FINDING_KEYS:
        raise LinkError('FINDING_FIELDS_INVALID')
    if (doc.get('permission'), doc.get('authority')) != ('READ_RESEARCH_ONLY', 'NO_EXECUTION_AUTHORITY'):
        raise LinkError('FINDING_AUTHORITY_INVALID')
    fid = doc.get('id')
    match = RE_FINDING_ID.fullmatch(fid) if isinstance(fid, str) else None
    if match is None:
        raise LinkError('FINDING_ID_INVALID')                      # an inv- id belongs on /answers, never here
    if doc.get('role') not in FINDING_ROLES:
        raise LinkError('FINDING_ROLE_INVALID')
    if match.group('role') not in (None, doc.get('role')):
        raise LinkError('FINDING_ROLE_INVALID')                    # cq-<role>-... names its own role
    if doc.get('kind') != KIND_BY_PREFIX.get(fid[:2]):
        raise LinkError('FINDING_KIND_INVALID')
    if doc.get('status') not in RESEARCH_STATUS:
        raise LinkError('FINDING_STATUS_INVALID')
    question = _finding_text(doc.get('question'), TEXT['title'], 'question')
    items = doc.get('items')
    if not isinstance(items, list) or len(items) > FINDING_MAX_ITEMS:
        raise LinkError('FINDING_ITEMS_INVALID')
    kept = []
    for it in items:
        if not isinstance(it, dict) or not it.keys() <= ITEM_KEYS:
            raise LinkError('FINDING_FIELDS_INVALID')
        if it.get('untrusted') is not True:
            raise LinkError('FINDING_ITEM_NOT_UNTRUSTED')
        kept.append({'title': _finding_text(it.get('title', ''), TEXT['title'], 'title'),
                     'url': _finding_url(it.get('url')),
                     'source': _finding_text(it.get('source', ''), TEXT['source'], 'source'),
                     'date': _finding_text(it.get('date') or '', TEXT['date'], 'date')})
    rows = doc.get('not_checked')
    if not isinstance(rows, list) or len(rows) > MAX_ITEMS:
        raise LinkError('FINDING_SOURCES_INVALID')
    not_checked = []
    for row in rows:
        if not isinstance(row, dict) or not row.keys() <= SOURCE_KEYS or row.get('status') not in SOURCE_STATUS:
            raise LinkError('FINDING_SOURCES_INVALID')
        code = row.get('code') or ''
        if not isinstance(code, str) or not RE_CODE.fullmatch(code):
            raise LinkError('FINDING_SOURCES_INVALID')
        not_checked.append({'source': _finding_text(row.get('source', ''), TEXT['source'], 'source'),
                            'status': row['status'], 'code': code})
    return {'id': fid, 'role': doc['role'], 'kind': doc['kind'], 'question': question, 'status': doc['status'],
            'items': kept, 'not_checked': not_checked, 'untrusted': True, 'authority': 'NO_EXECUTION_AUTHORITY'}


def phoenix_day(now=None):
    """The Phoenix calendar day (UTC-7, no daylight saving) the findings cap counts in."""
    at = time.time() if now is None else now
    return time.strftime('%Y-%m-%d', time.gmtime(at - PHOENIX_OFFSET_S))


def _no_constant(_):
    raise LinkError('ANSWER_NOT_JSON')


def _unique_keys(pairs):
    """object_pairs_hook: a key given twice is refused (the last one would otherwise win silently)."""
    out = {}
    for key, value in pairs:
        if key in out:
            raise LinkError('ANSWER_DUPLICATE_KEY')
        out[key] = value
    return out


def parse_answer(raw):
    """Bytes of one POST body to a dict: strict JSON, no NaN/Infinity, no duplicate keys at any depth."""
    return json.loads(raw.decode('utf-8'), parse_constant=_no_constant, object_pairs_hook=_unique_keys)


class Inbox:
    """Answers waiting for the runtime thread. Bounded; the daily cap counts accepted answers."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pending = deque(maxlen=MAX_PENDING)
        self.day = None
        self.count = 0

    def put(self, answer, day):
        with self.lock:
            if self.day != day:
                self.day, self.count = day, 0
            if self.count >= ANSWERS_PER_DAY:
                raise LinkError('ANSWER_DAILY_CAP')
            if len(self.pending) >= MAX_PENDING:
                raise LinkError('ANSWER_QUEUE_FULL')
            self.count += 1
            self.pending.append(answer)

    def take(self):
        with self.lock:
            out = list(self.pending)
            self.pending.clear()
            return out


def parse_finding(raw):
    """0.3.14: the same strict parser as an answer (no NaN/Infinity, no key twice at any depth), finding codes."""
    try:
        return parse_answer(raw)
    except LinkError as error:
        if str(error) == 'ANSWER_DUPLICATE_KEY':
            raise LinkError('FINDING_DUPLICATE_KEY') from None
        raise LinkError('FINDING_NOT_JSON') from None


class FindingInbox:
    """0.3.14: findings waiting for the runtime thread. Its own count: 20 a Phoenix day, never the answers' 120.
    Over the cap nothing is queued."""

    def __init__(self):
        self.lock = threading.Lock()
        self.queue = []
        self.day = None
        self.accepted = 0

    def put(self, finding, day):
        with self.lock:
            if self.day != day:
                self.day, self.accepted = day, 0
            if self.accepted >= FINDINGS_PER_DAY:
                raise LinkError('FINDING_DAILY_CAP')
            if len(self.queue) >= FINDINGS_PER_DAY:
                raise LinkError('FINDING_QUEUE_FULL')
            self.accepted += 1
            self.queue.append(finding)

    def take(self):
        with self.lock:
            out, self.queue = self.queue, []
            return out


def state_view(status, states, kill_entity):
    """What the MacBook needs to decide whether it may work. App states only, no household readings."""
    row = (states or {}).get(kill_entity) if isinstance(states, dict) else None
    view = status.get('view') or {}
    apps = {}
    for slug, a in sorted((view.get('apps') or {}).items()):
        apps[str(a.get('name'))[:64]] = {'state': a.get('state'), 'version': a.get('version'),
                                         'health': a.get('health')}
    counts = ((status.get('cases') or {}).get('counts') or {})
    return {'schema': 'house_brain_link_state.v1', 'at': status.get('at'), 'systems_ai': status.get('version'),
            'hold': status.get('hold_reason'),
            'kill_switch': {'state': row.get('state'), 'last_updated': row.get('last_updated')}
            if isinstance(row, dict) else None,
            'apps': apps, 'open_cases': {k: v for k, v in counts.items() if k in ('OPEN', 'ACTION_SUGGESTED')},
            'control_authority': 'NONE'}


def questions_view(status, cases):
    if status.get('hold_reason') is not None:
        return {'schema': 'house_brain_link_questions.v1', 'held': True, 'questions': []}
    out = []
    for item in (status.get('investigations') or {}).get('items') or []:
        if item.get('closed_at') is not None or item.get('status') in ('CLOSED', 'EXPIRED'):
            continue
        case = (cases or {}).get(item.get('case')) or {}
        out.append({'id': item['id'], 'question': item.get('question'), 'problems': list(case.get('problems') or [])[:10],
                    'permission': 'READ_RESEARCH_ONLY', 'expires_at': item.get('expires_at')})
    return {'schema': 'house_brain_link_questions.v1', 'held': False, 'questions': out[:MAX_QUESTIONS]}


class LinkServer:
    """state(), questions(), health() return dicts; held() says whether Systems AI is held; inbox takes answers."""

    def __init__(self, key, peer, state, questions, health, held, inbox, day, port=PORT, host='0.0.0.0',
                 readings=None, parse_ids=None, findings=None, finding_day=phoenix_day, onm_key=None, onm=None):
        token = ('Bearer ' + key).encode()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def log_message(self, *args):
                pass

            def respond(self, code, data=None):
                raw = b'' if data is None else json.dumps(data, allow_nan=False).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                if raw:
                    self.wfile.write(raw)

            def allowed(self):
                if self.client_address[0] != peer:
                    return False
                given = (self.headers.get('Authorization') or '').encode()
                return hmac.compare_digest(given, token)

            def signed(self, method):
                """0.3.15 (A6.1): the two O&M routes. True when this request was answered here. Only from the
                peer address, only with ``onm_key`` set, and only with a valid signature over the exact body."""
                if owner.onm is None or onm_key is None or self.client_address[0] != peer \
                        or self.path not in onm_link.ROUTES:
                    return False
                if onm_link.ROUTES[self.path] != method:
                    self.respond(405, {'error': 'METHOD_NOT_ALLOWED'})
                    return True
                try:
                    length = int(self.headers.get('Content-Length') or '0')
                except ValueError:
                    length = -1
                if length < 0 or length > onm_link.MAX_REPAIR_BYTES or (method == 'GET' and length):
                    self.respond(413 if length > 0 else 400)
                    return True
                body = self.rfile.read(length) if length else b''
                header = onm_link.precheck(onm_key, self.headers.get(onm_link.SIGNED_HEADER), body, method,
                                           self.path, int(time.time() * 1000))
                if header is None:
                    self.respond(403)
                    return True
                code, signed, raw = owner.onm(method, self.path, header, body)
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Cache-Control', 'no-store')
                if signed is not None:
                    self.send_header(onm_link.SIGNED_HEADER, json.dumps(signed, sort_keys=True, separators=(',', ':')))
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                if raw:
                    self.wfile.write(raw)
                return True

            def do_GET(self):
                if self.signed('GET'):
                    return
                if not self.allowed():
                    return self.respond(403)
                if self.path == '/link/v1/state':
                    return self.respond(200, owner.state())
                if self.path == '/link/v1/questions':
                    return self.respond(200, owner.questions())
                if self.path == '/link/v1/health':
                    return self.respond(200, owner.health())
                if self.path.split('?', 1)[0] == '/link/v1/readings' and owner.readings is not None:
                    ids, bad = owner.parse_ids(self.path.split('?', 1)[1] if '?' in self.path else '')
                    if bad:
                        return self.respond(400, {'error': bad})
                    return self.respond(200, owner.readings(ids))
                return self.respond(404, {'error': 'NOT_FOUND'})

            def do_POST(self):
                if self.signed('POST'):
                    return
                if not self.allowed():
                    return self.respond(403)
                if self.path == '/link/v1/findings' and owner.findings is not None:
                    return self.post_finding()
                if self.path != '/link/v1/answers':
                    return self.respond(404, {'error': 'NOT_FOUND'})
                if owner.held():
                    return self.respond(409, {'error': 'HELD'})
                try:
                    length = int(self.headers.get('Content-Length') or '-1')
                except ValueError:
                    length = -1
                if length < 0 or length > MAX_BODY:
                    return self.respond(413, {'error': 'ANSWER_TOO_LARGE'})
                try:
                    doc = parse_answer(self.rfile.read(length))
                    inbox.put(validate(doc), day())
                except LinkError as error:
                    return self.respond(429 if 'CAP' in str(error) or 'FULL' in str(error) else 422,
                                        {'error': str(error)})
                except (ValueError, UnicodeDecodeError):
                    return self.respond(422, {'error': 'ANSWER_NOT_JSON'})
                return self.respond(202, {'accepted': True, 'authority': 'NO_EXECUTION_AUTHORITY'})

            def post_finding(self):
                """0.3.14: one finding. Held 409, over 64 KB 413, invalid 422, over the daily cap 429; only a
                valid finding under the cap is queued, and only for the findings store."""
                is_held = owner.held()
                if is_held:
                    return self.respond(409, {'error': 'HELD'})
                try:
                    size = int(self.headers.get('Content-Length') or '-1')
                except ValueError:
                    size = -1
                if not 0 <= size <= MAX_BODY:
                    return self.respond(413, {'error': 'FINDING_TOO_LARGE'})
                try:
                    finding = validate_finding(parse_finding(self.rfile.read(size)))
                    owner.findings.put(finding, owner.finding_day())
                except LinkError as error:
                    code = str(error)
                    return self.respond(429 if code in ('FINDING_DAILY_CAP', 'FINDING_QUEUE_FULL') else 422,
                                        {'error': code})
                except (ValueError, UnicodeDecodeError):
                    return self.respond(422, {'error': 'FINDING_NOT_JSON'})
                return self.respond(202, {'accepted': True, 'kind': finding['kind'],
                                          'authority': 'NO_EXECUTION_AUTHORITY'})

            def refuse(self):
                return self.respond(405, {'error': 'METHOD_NOT_ALLOWED'})

            do_PUT = do_PATCH = do_DELETE = refuse

        self.state, self.questions, self.health, self.held = state, questions, health, held
        self.readings, self.parse_ids = readings, parse_ids         # 0.3.6 GET /link/v1/readings
        self.findings, self.finding_day = findings, finding_day     # 0.3.14 POST /link/v1/findings (off when None)
        self.onm = onm                                              # 0.3.15 the O&M desk (onm_link.Desk.submit)
        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.server.timeout = 2

    def run(self):
        self.server.serve_forever(poll_interval=.5)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
