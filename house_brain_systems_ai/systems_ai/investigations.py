"""Investigations (Systems AI 0.3.0; agentic layer design R1 step A1, owner pop-up 2026-11-02 "Build A1").

Every case that matters gets an investigation, opened automatically (automation first, standing owner decision
2026-11-01): an ERROR/CRITICAL case, a case that came back (REOPENED or reopening an earlier one), or a WARNING case
still open after an hour. An investigation is a bounded work packet, the design's Autonomy Passport (R3.1 I08):
an id, the case it serves, a plain question, a budget, an expiry, and permission READ_RESEARCH_ONLY with no
execution authority. Delegation can never add authority.

This release answers with tier T0 only (deterministic, on the Pi, no model and no outside request):
- evidence ids: the case's problems, reasons, incident timeline and the changes seen before it;
- similar past cases (case memory): earlier cases with the same problem, and how each ended;
- ranked hypotheses, each marked SIMILAR_CASE or HYPOTHESIS with its evidence; competing explanations are kept
  side by side (R3.1 I12), never voted into one;
- the recommended request, only when Systems AI already suggests one; it stays the owner's tap in Maintenance;
- NEEDS_ENGINEER when nothing in the evidence explains the case. Unknown is never an answer.

0.3.3 adds playbooks (A2): a solved real case that matches ranks first. Outside research (A3) and the MacBook
investigator and models (A6-A8) add tiers later. Each case
also gets an empty repair record owned by O&M (design §11 C1). Systems AI creates it and never fills it in.
Nothing here acts.

0.3.4 adds a progress ledger and a stall counter (W26-C6, after Magentic-One: Fourney et al., arXiv 2411.04468).
Every hour each live investigation answers five fixed questions: is the request satisfied (a playbook, a fixed
earlier case or an existing suggestion answers it, so the next step is the owner's), are we in a loop (the answer
went back to one seen before), is progress being made (new facts or evidence since the last review), who acts next,
and what to ask. A review with no new fact adds one to the stall counter; progress takes one off (leaky, as in the
Agent Framework port). At the threshold (option stall_reviews, default 3, 2..10) the investigation reads STALLED with
reason CASE_STALLED_NO_PROGRESS and asks for a re-plan or the owner's answer, through the page and the family
digest Maintenance already relays. It never retries faster, adds no read and no request, takes no action and never
changes authority. Facts are kept as short digests, at most 64 per investigation; a closed one keeps none.
"""
import hashlib
import json

from . import playbooks

OPEN_AFTER_WARNING_S = 3600
EXPIRE_S = 7 * 86400
KEEP = 100
KEEP_DAYS = 90
SHOW = 15
# (the empty O&M repair record on each case is created by cases.py)
MAX_HYPOTHESES = 5
MAX_SIMILAR = 5
SEVERE = ('ERROR', 'CRITICAL')
LIVE = ('OPEN', 'ACTION_SUGGESTED', 'WATCHING')
STATUSES = ('WORKING', 'ANSWERED', 'NEEDS_ENGINEER', 'STALLED', 'EXPIRED', 'CLOSED')
# 0.3.4 progress ledger (W26-C6).
STALL_DEFAULT, STALL_MIN, STALL_MAX = 3, 2, 10
REVIEW_S = 3600            # one ledger review an hour: never faster than the evidence can change
KNOWN_FACTS = 64           # fact digests kept per live investigation
LEDGER_KEEP = 6            # ledger entries and answer fingerprints kept per investigation
STALLED_REASON = 'CASE_STALLED_NO_PROGRESS'
SATISFIED_KINDS = ('PLAYBOOK', 'SIMILAR_CASE')


def stall_threshold(value):
    """The stall_reviews option: an int 2..10. Anything else is the default 3, never "no limit"."""
    return value if type(value) is int and STALL_MIN <= value <= STALL_MAX else STALL_DEFAULT


ACTIVE = ('OPEN', 'ACTION_SUGGESTED')


def should_open(case, now):
    """0.3.1: only while the problem is still active. A case already cleared (WATCHING) or an old REOPENED case is
    not investigated again; the newer case that reopens it is (live 2026-11-02: 48 cleared cases were opened and
    closed at once on the 0.3.0 install)."""
    if case['status'] not in ACTIVE:
        return False
    if case['severity'] in SEVERE or case.get('reopens'):
        return True
    return now - case['opened_at'] >= OPEN_AFTER_WARNING_S


def question(case):
    keys = ', '.join(case['problems'][:3]) + (' …' if len(case['problems']) > 3 else '')
    return f"Why did {keys} happen, is it known, and what would fix it for good?"


class Investigations:
    def __init__(self, store, stall_reviews=STALL_DEFAULT):
        self.store = store
        self.stall_reviews = stall_threshold(stall_reviews)
        self.state = store.get('investigations:state', {'items': {}})
        if self.state.get('rule', 1) < 2:
            # 0.3.1, once: drop what the 0.3.0 rule opened for cases already cleared; they never investigated
            # anything live. Live items stay.
            self.state['items'] = {k: v for k, v in self.state['items'].items() if v.get('closed_at') is None}
            self.state['rule'] = 2

    def update(self, cases, incidents, now, held=False, matched=None):
        """cases: the Cases state dict {id: case}; incidents: the Incidents list; matched: playbooks.match() this
        cycle (0.3.3). Frozen while held."""
        items = self.state['items']
        if held:
            return {**self.report(now), 'held': True}
        by_id = {i['id']: i for i in incidents}
        for case in cases.values():
            iid = 'inv-' + case['id']
            item = items.get(iid)
            if item is None:
                if not should_open(case, now):
                    continue
                item = items[iid] = {
                    'id': iid, 'case': case['id'], 'opened_at': now, 'expires_at': now + EXPIRE_S,
                    'question': question(case), 'permission': 'READ_RESEARCH_ONLY', 'execution_authority': False,
                    'tier': 'T0', 'budget': {'outside_requests': 0, 'model_calls': 0, 'used': 0},
                    'status': 'WORKING', 'answer': None, 'outcome': None, 'closed_at': None}
            if item['status'] in ('CLOSED', 'EXPIRED'):
                continue
            if case['status'] not in LIVE:
                item.update(status='CLOSED', closed_at=now, outcome=case['status'])
                continue
            item['answer'] = answer(case, by_id.get(case['id']), cases, matched, item.get('research'))
            item['status'] = 'ANSWERED' if item['answer']['hypotheses'] else 'NEEDS_ENGINEER'
            if self._ledger(item, case, now):
                item['status'] = 'STALLED'
            if now >= item['expires_at']:
                item.update(status='EXPIRED', closed_at=now, outcome=case['status'])
        self._prune(now)
        self.store.put('investigations:state', self.state)
        return self.report(now)

    def add_research(self, answers, now):
        """0.3.5: research results from the MacBook (already validated by link.validate). Only an open investigation
        takes one; the newest replaces the older. Returns how many were kept."""
        kept = 0
        for ans in answers:
            item = self.state['items'].get(ans['task'])
            if item is None or item.get('closed_at') is not None:
                continue
            item['research'] = {**ans, 'at': now}
            kept += 1
        if kept:
            self.store.put('investigations:state', self.state)
        return kept

    def _ledger(self, item, case, now):
        """0.3.4 (W26-C6): record new facts every cycle; review once an hour. True = the investigation is stalled.
        Local computation over what this cycle already has: no read, no request, no action."""
        led = item.get('ledger')
        if led is None:            # new, or opened before 0.3.4: the first review is now
            led = item['ledger'] = {'stall_count': 0, 'reviews': 0, 'known': [], 'new_since_review': 0,
                                    'next_review_at': now, 'fingerprints': [], 'entries': [], 'reason': None,
                                    'stalled_at': None}
        known = set(led['known'])
        new = [f for f in dict.fromkeys(digest(f) for f in facts(item['answer'], case)) if f not in known]
        led['known'].extend(new)
        del led['known'][:-KNOWN_FACTS]
        led['new_since_review'] += len(new)
        satisfied = is_satisfied(item['answer'])
        if now >= led['next_review_at']:
            fp = fingerprint(item['answer'])
            looping = fp in led['fingerprints'][:-1] and fp != led['fingerprints'][-1]
            progress = led['new_since_review'] > 0 and not looping
            if satisfied:
                led['stall_count'] = 0
            elif progress:
                led['stall_count'] = max(0, led['stall_count'] - 1)        # leaky
            else:
                led['stall_count'] = min(self.stall_reviews, led['stall_count'] + 1)
            led['fingerprints'] = (led['fingerprints'] + [fp])[-LEDGER_KEEP:]
            led['reviews'] += 1
            stalled = not satisfied and led['stall_count'] >= self.stall_reviews
            led['entries'] = (led['entries'] + [{
                'at': now, 'facts_learned': led['new_since_review'], 'satisfied': satisfied, 'looping': looping,
                'progress': progress, 'stall_count': led['stall_count'],
                **next_step(stalled, satisfied)}])[-LEDGER_KEEP:]
            led['new_since_review'] = 0
            led['next_review_at'] = now + REVIEW_S
        led['stall_count'] = min(led['stall_count'], self.stall_reviews)       # a lowered threshold applies at once
        stalled = not satisfied and led['stall_count'] >= self.stall_reviews
        led['reason'] = STALLED_REASON if stalled else None
        led['stalled_at'] = (led['stalled_at'] or now) if stalled else None
        return stalled

    def _prune(self, now):
        items = self.state['items']
        for item in items.values():
            if item['closed_at'] is not None and (item.get('ledger') or {}).get('known'):
                item['ledger']['known'] = []           # a closed investigation learns nothing more
        for iid in [i['id'] for i in items.values() if i['closed_at'] is not None
                    and now - i['closed_at'] > KEEP_DAYS * 86400]:
            items.pop(iid)
        if len(items) > KEEP:
            done = sorted((i for i in items.values() if i['closed_at'] is not None), key=lambda i: i['opened_at'])
            for i in done[:len(items) - KEEP]:
                items.pop(i['id'])

    def report(self, now):
        rows = sorted(self.state['items'].values(), key=lambda i: (i['closed_at'] is not None, -i['opened_at']))
        shown = [{**i, 'ledger': {k: v for k, v in i['ledger'].items() if k != 'known'}} if i.get('ledger') else i
                 for i in rows[:SHOW]]
        return {'counts': {s: sum(i['status'] == s for i in rows) for s in STATUSES}, 'items': shown,
                'kept': len(rows), 'held': False, 'tier_now': 'T0',
                'stall_reviews': self.stall_reviews, 'review_every_s': REVIEW_S,
                'note': 'Investigations explain and recommend; they never act. Any fix is your tap in Maintenance '
                        'or the Deployer.'}


def answer(case, incident, cases, matched=None, research=None):
    """T0: evidence, case memory and ranked hypotheses. Deterministic; every claim cites evidence ids.
    0.3.3: a matching playbook (a solved real case) ranks first and gives the recommended request."""
    evidence = [f'case:{case["id"]}'] + [f'problem:{k}' for k in case['problems'][:10]]
    known = playbooks.for_case(case['problems'], matched or {})
    keys = set(case['problems'])
    similar = []
    for other in cases.values():
        if other['id'] == case['id'] or not keys & set(other['problems']) or other['opened_at'] > case['opened_at']:
            continue
        similar.append({'case': other['id'], 'opened_at': other['opened_at'], 'status': other['status'],
                        'shared': sorted(keys & set(other['problems']))[:3], 'summary': other.get('summary')})
    similar.sort(key=lambda s: -s['opened_at'])
    similar = similar[:MAX_SIMILAR]
    hyps = [{'kind': 'PLAYBOOK', 'text': f"Known case: {m['title']}. {m['request']}",
             'evidence': [f"playbook:{m['playbook']}"] + m['evidence'][:2]} for m in known[:2]]
    if research and research.get('items'):
        # 0.3.5 House link: public research from the MacBook, always untrusted evidence, never an instruction.
        top = research['items'][0]
        hyps.append({'kind': 'RESEARCH', 'text': f"Public research found {len(research['items'])} source(s) "
                     f"(untrusted): {top['title'][:120]} — {top['url']}", 'evidence': [f"research:{research['task']}@"
                                                                                    f"{int(research['at'])}"]})
    fixed = [s for s in similar if s['status'] == 'FIXED_VERIFIED']
    if fixed:
        s = fixed[0]
        hyps.append({'kind': 'SIMILAR_CASE', 'text': f"Seen before (case {s['case']}): it cleared and stayed fixed. "
                     'The same cause and fix are likely.', 'evidence': [f"case:{s['case']}"]})
    back = [s for s in similar if s['status'] == 'REOPENED']
    if back or case.get('reopens'):
        ref = case.get('reopens') or back[0]['case']
        hyps.append({'kind': 'HYPOTHESIS', 'text': 'It keeps coming back: the earlier clear treated a symptom, '
                     'not the cause.', 'evidence': [f'case:{ref}']})
    # 0.3.2 (live 2026-11-02): one line per subject, however often it flipped; updates and App states rank above
    # routine status churn (Maintenance IDLE/WATCHING, Core up/down).
    groups = {}
    for change in (incident or {}).get('related_changes', [])[-12:]:
        ev = f"change:{change['kind']}:{change['subject']}@{int(change['at'])}"
        evidence.append(ev)
        groups.setdefault((change['kind'], change['subject']), []).append((change, ev))
    rank = {'UPDATE': 0, 'APP_STATE': 1, 'HOST_BOOT': 2}
    for (kind, subject), rows in sorted(groups.items(), key=lambda g: (rank.get(g[0][0], 3), -g[1][-1][0]['at'])):
        first, last = rows[0][0], rows[-1][0]
        text = (f"{kind.lower()} {subject} changed shortly before ({first['before']} → {last['after']})." if len(rows) == 1
                else f"{kind.lower()} {subject} changed {len(rows)} times shortly before ({first['before']} → … → "
                     f"{last['after']}).")
        hyps.append({'kind': 'HYPOTHESIS', 'text': text, 'evidence': [ev for _, ev in rows][-3:]})
    parents = [k for k in keys if k.startswith('KILL:') or k in ('CORE_API_UNREADABLE', 'SUPERVISOR_UNREADABLE')]
    for k in parents[:2]:
        hyps.append({'kind': 'HYPOTHESIS', 'text': f'{k} holds or hides other work; the rest may follow from it.',
                     'evidence': [f'problem:{k}']})
    recommended = case['suggested'][0] if case.get('suggested') else None
    return {'tier': 'T0', 'evidence': evidence[:20], 'similar': similar, 'hypotheses': hyps[:MAX_HYPOTHESES],
            'recommended': recommended, 'playbooks': list(dict.fromkeys(m['playbook'] for m in known)),
            'confidence': hyps[0]['kind'] if hyps else 'NEEDS_ENGINEER',
            'abstained': not hyps}


# -- 0.3.4 progress ledger helpers (W26-C6) ----------------------------------------------------------------------
def facts(ans, case):
    """What the investigation knows now, as stable fact ids: evidence, earlier cases, playbooks, hypotheses, the
    case's status and the suggestion. A new id since the last review is progress."""
    out = list(ans['evidence']) + [f"similar:{s['case']}:{s['status']}" for s in ans['similar']]
    out += [f'playbook:{p}' for p in ans['playbooks']]
    out += [f"hypothesis:{h['kind']}:{h['text']}" for h in ans['hypotheses']]
    out.append(f"case_status:{case['status']}")
    if ans['recommended']:
        out.append(f"recommended:{ans['recommended'].get('kind')}:{ans['recommended'].get('target')}")
    return out


def digest(fact):
    return hashlib.sha256(str(fact).encode()).hexdigest()[:12]


def fingerprint(ans):
    """The answer itself (hypotheses and suggestion), so an answer that comes back to an earlier one is a loop."""
    raw = json.dumps([[h['kind'], h['text']] for h in ans['hypotheses']] + [ans['recommended']], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def is_satisfied(ans):
    """Answered well enough that the next step is the owner's: a playbook, a fixed earlier case, or a suggestion
    Systems AI already holds. A guess from a change alone is not."""
    return bool(ans['recommended']) or any(h['kind'] in SATISFIED_KINDS for h in ans['hypotheses'])


def next_step(stalled, satisfied):
    """Who acts next and what to ask: fixed codes and fixed text, never words from the inputs."""
    if satisfied:
        return {'who_next': 'OWNER', 'next': 'OWNER_DECIDES_SUGGESTION', 'reason': None,
                'ask': 'A known answer is shown; any fix stays your tap in Maintenance or the Deployer.'}
    if stalled:
        return {'who_next': 'OWNER', 'next': 'RE_PLAN_OR_ASK_OWNER', 'reason': STALLED_REASON,
                'ask': 'No new evidence for several hourly reviews. Re-plan this case (an engineer or a later '
                       'research step), or tell Systems AI what you know.'}
    return {'who_next': 'SYSTEMS_AI', 'next': 'WAIT_FOR_EVIDENCE', 'reason': None, 'ask': None}
