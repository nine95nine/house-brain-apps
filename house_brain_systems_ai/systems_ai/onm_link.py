"""O&M link (Systems AI 0.3.15; step A6.1 of docs/house-brain/ONM_A6_INVESTIGATOR_DESIGN_R1_2026-11-14.md, owner
pop-ups 2026-11-14 "Approve as written" and 2026-11-15 "This O&M chat").

Two signed routes on the House link port, for the O&M investigator on the MacBook. They are off until the owner sets
``onm_key`` (and the House link itself is on):

- ``GET /link/v1/cases``: live cases plus cases cleared in the last 8 days (7 days until prevention is proven, plus
  one day so O&M can see it proven). At most 50 cases and 256 KB. While Systems AI is held: no cases, ``held: true``.
- ``POST /link/v1/repair``: one ``house_brain_onm_repair_record.v1`` for one case, at most 64 KB and 96 stored a
  Phoenix day (0.3.16: the same day as the findings cap on this link; it was the UTC day in 0.3.15).
  It is stored in ``case['repair']`` and nowhere else. Refused while held.

Every request and every answer carries an HMAC-SHA256 signature over the exact body, a timestamp and a strictly
increasing sequence number (``onm_signed_link.py``, a byte-identical copy of the O&M library's ``signed_link.py``).
The key is never sent, logged or stored here; only a keyed fingerprint is kept, so a new key starts a new sequence.

On the wire the signed header travels as compact JSON in the ``X-HB-Signed`` HTTP header, and the body is the exact
bytes that were signed. A request that fails any signature check gets 403 with no body and changes nothing.

A repair record never changes a case's status, its four checks, or its revision. The revision (``rev``) moves on only
when Systems AI itself changes the case or its investigation, and a record written against an older revision is
refused (compare-and-set). Everything here runs on the runtime thread (``Desk`` hands requests over), so the case
store has one writer. Nothing here can call Home Assistant or act: control authority stays NONE.
"""
from collections import deque
import hashlib
import hmac
import json
import threading
import time

from . import onm_repair_record as repair_record
from . import onm_signed_link as signed_link

CASES_PATH, REPAIR_PATH = '/link/v1/cases', '/link/v1/repair'
ROUTES = {CASES_PATH: 'GET', REPAIR_PATH: 'POST'}
SIGNED_HEADER = 'X-HB-Signed'
MAX_HEADER_CHARS = 2048
CASES_SCHEMA = 'house_brain_systems_ai_cases.v1'
RESULT_SCHEMA = 'house_brain_onm_repair_result.v1'
REPAIRS_PER_DAY = 96
PHOENIX_OFFSET_S = 7 * 3600          # Arizona keeps UTC-7 all year; the same value as link.PHOENIX_OFFSET_S
MAX_CASES = 50
MAX_CASES_BYTES = 256 * 1024
MAX_REPAIR_BYTES = repair_record.MAX_BYTES
# Cases cleared this long ago are still served: prevention is proven 7 days after a case clears (cases.PREVENTION_S),
# and O&M must be able to read that before the case leaves the list.
CLOSED_WINDOW_S = 8 * 86400
# The case store holds at most 2,000,000 bytes (store.put). Repair records may never fill it: past this size a
# record is refused, so Systems AI's own case updates always fit.
STATE_BUDGET_BYTES = 1_000_000
LIVE = ('OPEN', 'ACTION_SUGGESTED', 'WATCHING')
KEY_MIN = signed_link.MIN_KEY
KEY_MAX = 512
KEY_ID_TAG = b'HB-ONM-LINK-key-id-v1'
WAIT_S = 25
QUEUE_DEPTH = 4
REFUSALS_KEPT = 10
CASE_FIELDS = ('id', 'opened_at', 'closed_at', 'status', 'severity', 'problems', 'reasons', 'checks', 'reopened_as',
               'reopens', 'suggested', 'summary', 'incident_rolled_off', 'cleared_while_held', 'repair')
INVESTIGATION_FIELDS = ('id', 'case', 'status', 'tier', 'opened_at', 'closed_at', 'expires_at', 'outcome', 'question',
                        'permission', 'execution_authority', 'answer')
BUSY = (503, None, b'')
FORBIDDEN = (403, None, b'')
REFUSAL_HTTP = {'SYSTEMS_AI_HELD': 409, 'REPAIR_REV_CONFLICT': 409, 'REPAIR_DAILY_LIMIT': 429, 'UNKNOWN_CASE': 404,
                'REPAIR_STORE_FULL': 507}


def configured(options, linked):
    """The O&M key as bytes, or None (the two routes stay off). It needs the House link on, a key of 32 to 512
    printable characters, and a key that is not the House link key (the research account must never hold it)."""
    key = options.get('onm_key') if isinstance(options, dict) else None
    if not linked or not isinstance(key, str):
        return None
    key = key.strip()
    if not KEY_MIN <= len(key) <= KEY_MAX or not key.isascii() or not key.isprintable():
        return None
    if hmac.compare_digest(key.encode(), str(linked[0]).encode()):
        return None
    return key.encode()


def _encode(doc):
    return json.dumps(doc, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _reject_constant(_):
    raise ValueError('constant')


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('duplicate key')
        out[key] = value
    return out


def parse(raw):
    """Strict JSON: UTF-8, no NaN or Infinity, no key given twice at any depth. Raises ValueError."""
    return json.loads(raw.decode('utf-8'), parse_constant=_reject_constant, object_pairs_hook=_unique)


def precheck(key, header_text, body, method, path, now_ms):
    """HTTP-thread filter: the parsed header when the signature, body digest and clock are right, else None. It
    keeps unsigned traffic off the runtime thread. The sequence number is checked later, by the one writer."""
    if not isinstance(header_text, str) or not 0 < len(header_text) <= MAX_HEADER_CHARS:
        return None
    try:
        header = parse(header_text.encode('ascii'))
        signed_link.verify(key, header, body, role='onm', method=method, path=path, now_ms=now_ms, last_seq=0)
    except (ValueError, RecursionError):
        return None
    return header


def _stable(case, investigation):
    """What a revision covers: the case as Systems AI measures it and what its investigation concluded. The repair
    record, the hourly review ledger and reading ages are left out, so a revision moves only on a real change."""
    inv = investigation if isinstance(investigation, dict) else {}
    answer = inv.get('answer') if isinstance(inv.get('answer'), dict) else {}
    return {'case': {k: case.get(k) for k in CASE_FIELDS if k != 'repair'},
            'investigation': None if not inv else {
                'status': inv.get('status'), 'outcome': inv.get('outcome'), 'closed_at': inv.get('closed_at'),
                'playbooks': answer.get('playbooks'), 'recommended': answer.get('recommended'),
                'abstained': answer.get('abstained'),
                'hypotheses': [[h.get('kind'), h.get('fact', h.get('text'))]
                               for h in answer.get('hypotheses') or [] if isinstance(h, dict)]}}


class Desk:
    """Hands a signed request from an HTTP thread to the runtime thread and waits for its answer.

    A request that is not picked up within ``wait_s`` is withdrawn and answered 503: it was never applied. One that
    is already being served is always waited for, so the answer and what was stored cannot disagree."""

    def __init__(self, wait_s=WAIT_S, depth=QUEUE_DEPTH):
        self.wait_s, self.depth = wait_s, depth
        self.lock = threading.Lock()
        self.queue = deque()

    def submit(self, *request):
        job = {'request': request, 'done': threading.Event(), 'state': 'QUEUED', 'result': BUSY}
        with self.lock:
            if len(self.queue) >= self.depth:
                return BUSY
            self.queue.append(job)
        if not job['done'].wait(self.wait_s):
            with self.lock:
                if job['state'] == 'QUEUED':
                    job['state'] = 'WITHDRAWN'
                    self.queue.remove(job)
                    return BUSY
            job['done'].wait()
        return job['result']

    def serve(self, handle):
        """Runtime thread: answer everything waiting. Returns how many were served."""
        served = 0
        while True:
            with self.lock:
                if not self.queue:
                    return served
                job = self.queue.popleft()
                job['state'] = 'RUNNING'
            try:
                job['result'] = handle(*job['request'])
            except Exception:  # noqa: BLE001 - a failure is a 503 with no body, never a half answer
                job['result'] = BUSY
            job['done'].set()
            served += 1


class OnmLink:
    """The Systems AI side of the signed link. Runtime thread only."""

    def __init__(self, store):
        self.store = store
        self.key = None
        self.state = store.get('onm:link', {'key_id': None, 'seq_in': 0, 'seq_out': 0, 'day': None, 'stored': 0})
        self.revs = store.get('onm:revs', {})
        self.last = {'cases_at': None, 'repair_at': None, 'refusals': []}

    # ------------------------------------------------------------ set-up
    def enable(self, key):
        """Turn the routes on with this key. A different key than last time starts a new inbound sequence."""
        key_id = hmac.new(key, KEY_ID_TAG, hashlib.sha256).hexdigest()[:16]
        if self.state.get('key_id') != key_id:
            self.state.update(key_id=key_id, seq_in=0)
            self.store.put('onm:link', self.state)
        self.key = key

    # ------------------------------------------------------------ revisions
    def track(self, cases, items):
        """Give every case its revision: 1 when first seen, plus one each time Systems AI changes it."""
        changed = False
        for cid, case in cases.items():
            digest = hashlib.sha256(_encode(_stable(case, items.get('inv-' + cid)))).hexdigest()[:32]
            row = self.revs.get(cid)
            if row is None:
                self.revs[cid] = {'rev': 1, 'digest': digest}
                changed = True
            elif row['digest'] != digest:
                row.update(rev=row['rev'] + 1, digest=digest)
                changed = True
        for cid in [c for c in self.revs if c not in cases]:
            self.revs.pop(cid)
            changed = True
        if changed:
            self.store.put('onm:revs', self.revs)

    def view(self, case, items):
        row = {k: case.get(k) for k in CASE_FIELDS}
        inv = items.get('inv-' + case['id'])
        row['investigation'] = {k: inv.get(k) for k in INVESTIGATION_FIELDS} if isinstance(inv, dict) else None
        row['rev'] = self.revs[case['id']]['rev']
        return row

    # ------------------------------------------------------------ signed answers
    def _answer(self, header, doc, now, code=200):
        body = _encode(doc)
        self.state['seq_out'] = max(self.state['seq_out'] + 1, int(now * 1000))
        self.store.put('onm:link', self.state)             # both sequence numbers are saved before the answer leaves
        signed = signed_link.sign(self.key, role='systems_ai', method=header['method'], path=header['path'],
                                  ticket=header['ticket'], seq=self.state['seq_out'], ts_ms=int(now * 1000), body=body)
        return code, signed, body

    def _refuse(self, header, reason, now):
        self.last['refusals'] = (self.last['refusals'] + [{'at': int(now), 'reason': reason}])[-REFUSALS_KEPT:]
        return self._answer(header, {'schema': RESULT_SCHEMA, 'stored': False, 'reason': reason}, now,
                            REFUSAL_HTTP.get(reason, 422))

    # ------------------------------------------------------------ the two routes
    def handle(self, method, path, header, body, *, now, held, cases, investigations):
        """One request, already read from the wire. Returns (HTTP code, signed header or None, body bytes)."""
        if self.key is None or ROUTES.get(path) != method or (path == CASES_PATH and body):
            return FORBIDDEN
        try:
            self.state['seq_in'] = signed_link.verify(self.key, header, body, role='onm', method=method, path=path,
                                                      now_ms=int(now * 1000), last_seq=self.state['seq_in'])
        except signed_link.LinkHold as hold:
            self.last['refusals'] = (self.last['refusals']
                                     + [{'at': int(now), 'reason': str(hold)}])[-REFUSALS_KEPT:]
            return FORBIDDEN
        day = time.strftime('%Y-%m-%d', time.gmtime(now - PHOENIX_OFFSET_S))    # 0.3.16: the Phoenix day
        if self.state['day'] != day:
            self.state.update(day=day, stored=0)
        self.track(cases.state['cases'], investigations.state['items'])
        if path == CASES_PATH:
            return self._cases(header, now, held, cases, investigations)
        return self._repair(header, body, now, held, cases)

    def _cases(self, header, now, held, cases, investigations):
        self.last['cases_at'] = int(now)
        if held:
            return self._answer(header, {'schema': CASES_SCHEMA, 'held': True, 'cases': []}, now)
        items = investigations.state['items']
        chosen = [c for c in cases.state['cases'].values()
                  if c['status'] in LIVE or c['closed_at'] is None or now - c['closed_at'] <= CLOSED_WINDOW_S]
        chosen.sort(key=lambda c: (c['status'] not in LIVE and c['closed_at'] is not None, -c['opened_at'], c['id']))
        rows = [self.view(c, items) for c in chosen[:MAX_CASES]]
        doc = {'schema': CASES_SCHEMA, 'held': False, 'cases': rows, 'more': len(chosen) - len(rows)}
        while rows and len(_encode(doc)) > MAX_CASES_BYTES:    # the least urgent case goes first
            rows.pop()
            doc['more'] += 1
        return self._answer(header, doc, now)

    def _repair(self, header, body, now, held, cases):
        if held:
            return self._refuse(header, 'SYSTEMS_AI_HELD', now)
        if self.state['stored'] >= REPAIRS_PER_DAY:
            return self._refuse(header, 'REPAIR_DAILY_LIMIT', now)
        if len(body) > MAX_REPAIR_BYTES:
            return self._refuse(header, 'REPAIR_TOO_LARGE', now)
        try:
            record = parse(body)
            repair_record.validate(record)
        except repair_record.RepairHold as hold:
            return self._refuse(header, str(hold), now)
        except (ValueError, RecursionError):
            return self._refuse(header, 'REPAIR_CONTRACT', now)
        case = cases.state['cases'].get(record['case'])
        if case is None:
            return self._refuse(header, 'UNKNOWN_CASE', now)
        if record['ticket'] != header['ticket'] or record['seq'] != header['seq']:
            return self._refuse(header, 'REPAIR_BINDING_MISMATCH', now)    # the record is bound to its signed envelope
        rev = self.revs[case['id']]['rev']
        if record['rev'] != rev:
            return self._refuse(header, 'REPAIR_REV_CONFLICT', now)        # compare-and-set: never overwrite newer
        for name in repair_record.SYSTEMS_AI_MEASURES:
            if record['obligations'][name]['status'] != case['checks'].get(name, 'UNVERIFIED'):
                return self._refuse(header, 'REPAIR_OBLIGATION_OWNER', now)
        before = case.get('repair')
        case['repair'] = record                                            # only the repair slot; rev unchanged
        try:
            if len(_encode(cases.state)) > STATE_BUDGET_BYTES:
                raise ValueError
            self.store.put('cases:state', cases.state)                     # stored before the answer says so
        except ValueError:
            case['repair'] = before
            return self._refuse(header, 'REPAIR_STORE_FULL', now)
        except Exception:
            case['repair'] = before
            raise
        self.state['stored'] += 1
        self.last['repair_at'] = int(now)
        return self._answer(header, {'schema': RESULT_SCHEMA, 'stored': True, 'case': case['id'], 'rev': rev}, now)

    # ------------------------------------------------------------ what the page shows
    def report(self):
        return {'enabled': self.key is not None, 'cases_read_at': self.last['cases_at'],
                'repair_stored_at': self.last['repair_at'], 'stored_today': self.state['stored'],
                'stored_per_day_max': REPAIRS_PER_DAY, 'refusals': list(self.last['refusals']),
                'control_authority': 'NONE'}
