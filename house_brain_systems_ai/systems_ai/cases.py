"""Incidents -> cases (Systems AI 0.2.0; design R2 §3, owner pop-ups 2026-10-31/11-01).

A case follows one incident from first sign to a verified fix, and remembers it after the incident has rolled off
(Incidents keeps 60). Its lifecycle is measured, never declared:

- OPEN while any of its problems is open; ACTION_SUGGESTED when Systems AI also holds a proposal for one of them
  (Maintenance or the Deployer acts only after the owner's tap; Systems AI never does);
- WATCHING for 24 h after every problem cleared;
- FIXED_VERIFIED when the watch passed with none of its problems back;
- REOPENED when one of its problems came back during the watch (the new case is linked).

The four repair checks come from the O&M research (P-series): service restored, cause corrected, side effects
reconciled, prevention proven. Systems AI can measure only the first and the last; the other two read
NOT_MEASURED, never VERIFIED. Unknown is never OK: while the kill switch holds Systems AI its slower checks
stop, so cases do not move; a problem that cleared during a hold is checked again from the end of the hold
and counts as restored only after a full watch with it absent.

The O&M offline ledger kernel (#1396) is not used as the store: it has no case closing and no retention by design.
Owner pop-up 2026-11-01 "Systems AI's own store". Cases live in this App's bounded store. Nothing here acts.
"""
WATCH_S = 86400
PREVENTION_S = 7 * 86400
KEEP_DAYS = 90
KEEP_CASES = 200
SHOW = 20
STATUSES = ('OPEN', 'ACTION_SUGGESTED', 'WATCHING', 'FIXED_VERIFIED', 'REOPENED')
LIVE = ('OPEN', 'ACTION_SUGGESTED', 'WATCHING')


def repair_slot():
    """0.3.0: the O&M repair record on each case (agentic design §11 C1). Created empty here, written only by O&M."""
    return {'owner': 'O&M', 'state': 'NOT_STARTED', 'cause': None, 'obligations': [], 'prevention': None}


def _new(inc):
    return {'id': inc['id'], 'opened_at': inc['opened_at'], 'closed_at': None, 'status': 'OPEN',
            'severity': inc['severity'], 'problems': list(inc['problems']), 'reasons': [],
            'reopened_as': None, 'reopens': None, 'suggested': [], 'incident_rolled_off': False,
            'cleared_while_held': False,
            'checks': {'service_restored': 'UNVERIFIED', 'cause_corrected': 'NOT_MEASURED',
                       'side_effects_reconciled': 'NOT_MEASURED', 'prevention_proven': 'UNVERIFIED'},
            'summary': None, 'repair': repair_slot()}


class Cases:
    def __init__(self, store):
        self.store = store
        self.state = store.get('cases:state', {'cases': {}})

    def update(self, incidents, proposals, now, held=False):
        """incidents: the Incidents list; proposals: Systems AI's own shadow proposals; held: kill-switch hold."""
        cases = self.state['cases']
        if held:
            self.state.setdefault('held_since', now)
            self.store.put('cases:state', self.state)
            return {**self.report(now), 'held': True}
        held_since = self.state.pop('held_since', None)
        suggested = {}
        for p in proposals:
            suggested.setdefault(p['problem'], []).append({'kind': p['kind'], 'target': p['target']})
        seen = {inc['id'] for inc in incidents}
        for inc in sorted(incidents, key=lambda i: i['opened_at']):
            case = cases.get(inc['id'])
            if case is None:
                if inc['closed_at'] is not None and now - inc['closed_at'] > KEEP_DAYS * 86400:
                    continue  # already past retention: never re-created after a prune
                case = cases[inc['id']] = _new(inc)
                self._link_reopen(case, now)
            case['problems'] = sorted(set(case['problems']) | set(inc['problems']))
            case.setdefault('repair', repair_slot())       # cases from before 0.3.0
            case['severity'] = inc['severity']
            case['reasons'] = sorted({t['reason'] for t in inc['timeline'] if t.get('reason')})[:10]
            if inc['closed_at'] is None:
                case['closed_at'] = None
                case['suggested'] = [s for k in case['problems'] for s in suggested.get(k, [])][:5]
                case['status'] = 'ACTION_SUGGESTED' if case['suggested'] else 'OPEN'
                case['checks']['service_restored'] = 'UNVERIFIED'
            elif case['closed_at'] is None:
                case['suggested'] = []
                if held_since is not None and inc['closed_at'] >= held_since:
                    case['closed_at'] = now      # cleared while not measured: the watch starts now
                    case['cleared_while_held'] = True
                else:
                    case['closed_at'] = inc['closed_at']
                    case['checks']['service_restored'] = 'VERIFIED'
                if case['status'] != 'REOPENED':
                    case['status'] = 'WATCHING'
                if inc.get('postmortem'):
                    case['summary'] = inc['postmortem'].get('summary')
        for case in cases.values():
            # An open incident pushed out of the 60 kept is no longer measured: say so, never call it fixed.
            case['incident_rolled_off'] = case['closed_at'] is None and case['id'] not in seen
            if case['status'] == 'WATCHING' and now - case['closed_at'] >= WATCH_S:
                case['status'] = 'FIXED_VERIFIED'
                case['checks']['service_restored'] = 'VERIFIED'
            if (case['checks']['prevention_proven'] == 'UNVERIFIED' and case['closed_at'] is not None
                    and case['status'] != 'REOPENED' and now - case['closed_at'] >= PREVENTION_S):
                case['checks']['prevention_proven'] = 'VERIFIED'
        self._prune(now)
        self.store.put('cases:state', self.state)
        return self.report(now)

    def _link_reopen(self, new, now):
        """A problem back within the watch reopens the older case; within 7 days it fails prevention."""
        keys = set(new['problems'])
        for old in self.state['cases'].values():
            if old['id'] == new['id'] or old['closed_at'] is None or not keys & set(old['problems']):
                continue
            since = new['opened_at'] - old['closed_at']
            if 0 <= since < WATCH_S and old['status'] in ('WATCHING', 'FIXED_VERIFIED'):
                old['status'] = 'REOPENED'
                old['reopened_as'] = new['id']
                new['reopens'] = old['id']
            if 0 <= since < PREVENTION_S:
                old['checks']['prevention_proven'] = 'FAILED'

    def _prune(self, now):
        cases = self.state['cases']
        for cid in [c['id'] for c in cases.values()
                    if (c['status'] not in LIVE and c['closed_at'] is not None
                        and now - c['closed_at'] > KEEP_DAYS * 86400)
                    or (c.get('incident_rolled_off') and now - c['opened_at'] > KEEP_DAYS * 86400)]:
            cases.pop(cid)
        if len(cases) > KEEP_CASES:
            closed = sorted((c for c in cases.values() if c['status'] not in LIVE), key=lambda c: c['opened_at'])
            for c in closed[:len(cases) - KEEP_CASES]:
                cases.pop(c['id'])

    def report(self, now):
        cases = sorted(self.state['cases'].values(), key=lambda c: -c['opened_at'])
        counts = {s: sum(c['status'] == s for c in cases) for s in STATUSES}
        return {'counts': counts, 'cases': cases[:SHOW], 'kept': len(cases), 'held': False,
                'note': 'Cases suggest; they never act. Any fix is your tap in Maintenance or the Deployer.'}
