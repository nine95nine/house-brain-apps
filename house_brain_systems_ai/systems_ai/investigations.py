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

Playbooks (A2), outside research (A3) and the MacBook investigator and models (A6-A8) add tiers later. Each case
also gets an empty repair record owned by O&M (design §11 C1). Systems AI creates it and never fills it in.
Nothing here acts.
"""
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
STATUSES = ('WORKING', 'ANSWERED', 'NEEDS_ENGINEER', 'EXPIRED', 'CLOSED')


def should_open(case, now):
    if case['status'] not in LIVE and case['status'] != 'REOPENED':
        return False
    if case['severity'] in SEVERE or case['status'] == 'REOPENED' or case.get('reopens'):
        return True
    return case['status'] != 'WATCHING' and now - case['opened_at'] >= OPEN_AFTER_WARNING_S


def question(case):
    keys = ', '.join(case['problems'][:3]) + (' …' if len(case['problems']) > 3 else '')
    return f"Why did {keys} happen, is it known, and what would fix it for good?"


class Investigations:
    def __init__(self, store):
        self.store = store
        self.state = store.get('investigations:state', {'items': {}})

    def update(self, cases, incidents, now, held=False):
        """cases: the Cases state dict {id: case}; incidents: the Incidents list. Frozen while held."""
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
            item['answer'] = answer(case, by_id.get(case['id']), cases)
            item['status'] = 'ANSWERED' if item['answer']['hypotheses'] else 'NEEDS_ENGINEER'
            if now >= item['expires_at']:
                item.update(status='EXPIRED', closed_at=now, outcome=case['status'])
        self._prune(now)
        self.store.put('investigations:state', self.state)
        return self.report(now)

    def _prune(self, now):
        items = self.state['items']
        for iid in [i['id'] for i in items.values() if i['closed_at'] is not None
                    and now - i['closed_at'] > KEEP_DAYS * 86400]:
            items.pop(iid)
        if len(items) > KEEP:
            done = sorted((i for i in items.values() if i['closed_at'] is not None), key=lambda i: i['opened_at'])
            for i in done[:len(items) - KEEP]:
                items.pop(i['id'])

    def report(self, now):
        rows = sorted(self.state['items'].values(), key=lambda i: -i['opened_at'])
        return {'counts': {s: sum(i['status'] == s for i in rows) for s in STATUSES}, 'items': rows[:SHOW],
                'kept': len(rows), 'held': False, 'tier_now': 'T0',
                'note': 'Investigations explain and recommend; they never act. Any fix is your tap in Maintenance '
                        'or the Deployer.'}


def answer(case, incident, cases):
    """T0: evidence, case memory and ranked hypotheses. Deterministic; every claim cites evidence ids."""
    evidence = [f'case:{case["id"]}'] + [f'problem:{k}' for k in case['problems'][:10]]
    keys = set(case['problems'])
    similar = []
    for other in cases.values():
        if other['id'] == case['id'] or not keys & set(other['problems']) or other['opened_at'] > case['opened_at']:
            continue
        similar.append({'case': other['id'], 'opened_at': other['opened_at'], 'status': other['status'],
                        'shared': sorted(keys & set(other['problems']))[:3], 'summary': other.get('summary')})
    similar.sort(key=lambda s: -s['opened_at'])
    similar = similar[:MAX_SIMILAR]
    hyps = []
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
    for change in (incident or {}).get('related_changes', [])[-3:]:
        ev = f"change:{change['kind']}:{change['subject']}@{int(change['at'])}"
        evidence.append(ev)
        hyps.append({'kind': 'HYPOTHESIS', 'text': f"{change['kind'].lower()} {change['subject']} changed shortly "
                     f"before ({change['before']} → {change['after']}).", 'evidence': [ev]})
    parents = [k for k in keys if k.startswith('KILL:') or k in ('CORE_API_UNREADABLE', 'SUPERVISOR_UNREADABLE')]
    for k in parents[:2]:
        hyps.append({'kind': 'HYPOTHESIS', 'text': f'{k} holds or hides other work; the rest may follow from it.',
                     'evidence': [f'problem:{k}']})
    recommended = case['suggested'][0] if case.get('suggested') else None
    return {'tier': 'T0', 'evidence': evidence[:20], 'similar': similar, 'hypotheses': hyps[:MAX_HYPOTHESES],
            'recommended': recommended, 'confidence': hyps[0]['kind'] if hyps else 'NEEDS_ENGINEER',
            'abstained': not hyps}
