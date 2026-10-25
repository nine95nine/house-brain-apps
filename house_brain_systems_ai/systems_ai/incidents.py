"""Problems -> incidents (S1), home-service error budgets (S2), what changed (S4).

A problem is a fingerprinted condition seen in one slow cycle. One owner per problem: anything
Maintenance already reports stays Maintenance's (it is counted here, never raised again).
Flapping problems are held back with Nagios-style hysteresis; a parent problem (Core or the
Supervisor unreachable) hides the problems it explains. Problems that open close together form one
incident with a timeline, the changes seen in the 24 hours before it, and, once closed, a draft
postmortem whose causes are marked as hypotheses. Nothing here sends a message or acts.
"""
import hashlib

FLAP_WINDOW = 21
FLAP_HIGH = .5
FLAP_LOW = .25
GROUP_S = 900
CLOSE_AFTER_S = 900
CHANGE_LOOKBACK_S = 86400
KEEP_INCIDENTS = 60
KEEP_CHANGES = 200
SLO_TARGET = .995
SLO_DAYS = 30
SEVERITY = ('INFO', 'WARNING', 'ERROR', 'CRITICAL')
# A child problem is hidden while its parent is open (inhibition).
PARENTS = {'CORE_API_UNREADABLE': ('HA:', 'LOG:', 'REPAIRS:', 'DB:', 'KILL:', 'MEM:', 'MAINT:'),
           'SUPERVISOR_UNREADABLE': ('APP:', 'DISK:', 'BACKUP:')}


def fingerprint(key):
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def flap_change(history):
    """Nagios percent state change, weighted 0.8 (oldest) to 1.2 (newest), over the last 21 states."""
    h = history[-FLAP_WINDOW:]
    if len(h) < 2:
        return 0.
    changes = [h[i] != h[i - 1] for i in range(1, len(h))]
    weights = [.8 + .4 * i / max(1, len(changes) - 1) for i in range(len(changes))]
    return sum(w for w, c in zip(weights, changes) if c) / sum(weights)


class Incidents:
    def __init__(self, store):
        self.store = store
        self.state = store.get('incidents:state', {'history': {}, 'flapping': [], 'open': {}, 'incidents': [],
                                                   'changes': [], 'slo': {}})

    # -- changes (S4) ---------------------------------------------------------------------
    def change(self, now, kind, subject, before, after):
        self.state['changes'].append({'at': now, 'kind': kind, 'subject': subject, 'before': before, 'after': after})
        self.state['changes'] = self.state['changes'][-KEEP_CHANGES:]

    def related_changes(self, start):
        return [c for c in self.state['changes'] if start - CHANGE_LOOKBACK_S <= c['at'] <= start]

    # -- problems (S1) ----------------------------------------------------------------------
    def update(self, problems, now):
        """problems: {key: {'severity','reason','subject','owner'}} seen this cycle."""
        st = self.state
        keys = set(problems)
        for key in set(st['history']) | keys:
            st['history'][key] = (st['history'].get(key, []) + [key in keys])[-FLAP_WINDOW:]
            if not any(st['history'][key]):
                st['history'].pop(key)
        flapping = set(st['flapping'])
        for key, h in st['history'].items():
            change = flap_change(h)
            if key not in flapping and change > FLAP_HIGH:
                flapping.add(key)
            elif key in flapping and change < FLAP_LOW:
                flapping.discard(key)
        flapping &= set(st['history'])
        st['flapping'] = sorted(flapping)
        parents = {p for p in PARENTS if p in keys}
        hidden = {k for k in keys for p in parents for prefix in PARENTS[p] if k.startswith(prefix)}
        shown = {k: v for k, v in problems.items() if k not in hidden}
        # Open / keep / close problems; a flapping problem stays open (as FLAPPING) while it flaps.
        open_ = st['open']
        for key, p in shown.items():
            if key not in open_:
                open_[key] = {**p, 'key': key, 'id': fingerprint(key), 'since': now, 'last_seen': now,
                              'incident': None}
            else:
                open_[key].update(p, last_seen=now)
        for key in list(open_):
            if key in shown or key in flapping:
                continue
            if now - open_[key]['last_seen'] >= CLOSE_AFTER_S or key in hidden:
                self._close_problem(open_.pop(key), now)
        for key, p in open_.items():
            p['flapping'] = key in flapping
            if p['incident'] is None:
                p['incident'] = self._join(p, now)
        self._close_incidents(now)
        self.store.put('incidents:state', st)
        return {'open': sorted(open_.values(), key=lambda p: (-SEVERITY.index(p['severity']), p['since'])),
                'hidden_by_parent': sorted(hidden), 'flapping': st['flapping']}

    def _join(self, problem, now):
        for inc in reversed(self.state['incidents']):
            if inc['closed_at'] is None and now - inc['last_open_at'] <= GROUP_S:
                inc['problems'].append(problem['key'])
                inc['last_open_at'] = now
                inc['timeline'].append({'at': now, 'event': 'OPENED', 'key': problem['key'], 'reason': problem['reason']})
                inc['severity'] = max(inc['severity'], problem['severity'], key=SEVERITY.index)
                return inc['id']
        inc = {'id': fingerprint(f"{problem['key']}{now}"), 'opened_at': now, 'last_open_at': now, 'closed_at': None,
               'severity': problem['severity'], 'problems': [problem['key']],
               'timeline': [{'at': now, 'event': 'OPENED', 'key': problem['key'], 'reason': problem['reason']}],
               'related_changes': self.related_changes(now), 'postmortem': None}
        self.state['incidents'].append(inc)
        self.state['incidents'] = self.state['incidents'][-KEEP_INCIDENTS:]
        return inc['id']

    def _close_problem(self, problem, now):
        for inc in self.state['incidents']:
            if inc['id'] == problem.get('incident'):
                inc['timeline'].append({'at': now, 'event': 'CLEARED', 'key': problem['key'], 'reason': problem['reason']})

    def _close_incidents(self, now):
        live = {p['incident'] for p in self.state['open'].values()}
        for inc in self.state['incidents']:
            if inc['closed_at'] is None and inc['id'] not in live:
                inc['closed_at'] = now
                inc['postmortem'] = postmortem(inc)

    # -- error budgets (S2) -----------------------------------------------------------------
    def slo(self, service, ok, now):
        """One good/bad sample per slow cycle; unknown counts as bad (unknown is never OK)."""
        s = self.state['slo'].setdefault(service, {'days': {}, 'recent': []})
        day = str(int(now // 86400))
        good, bad = s['days'].get(day, [0, 0])
        s['days'][day] = [good + (ok is True), bad + (ok is not True)]
        for d in sorted(s['days'], key=int)[:-SLO_DAYS]:
            s['days'].pop(d)
        s['recent'] = [r for r in s['recent'] if r[0] >= now - 6 * 3600] + [[now, ok is True]]

    def slo_report(self, now):
        out = {}
        budget = 1 - SLO_TARGET
        for service, s in sorted(self.state['slo'].items()):
            good = sum(v[0] for v in s['days'].values())
            bad = sum(v[1] for v in s['days'].values())
            total = good + bad
            def burn(window):
                rows = [r for r in s['recent'] if r[0] >= now - window]
                return (sum(not r[1] for r in rows) / len(rows)) / budget if rows else None
            fast, slow = burn(3600), burn(6 * 3600)
            used = (bad / total) / budget if total else None
            out[service] = {'availability': good / total if total else None, 'target': SLO_TARGET,
                            'budget_used': used, 'burn_1h': fast, 'burn_6h': slow,
                            'alert': bool(fast is not None and slow is not None and
                                          ((fast >= 14.4 and slow >= 14.4) or (slow >= 6 and fast >= 6))),
                            'days': len(s['days'])}
        return out

    def recent(self, limit=10):
        return list(reversed(self.state['incidents']))[:limit]


def postmortem(inc):
    """A blameless draft from the incident record; causes are hypotheses, never conclusions."""
    minutes = round((inc['closed_at'] - inc['opened_at']) / 60)
    changes = inc['related_changes'][-5:]
    return {'summary': f"{len(inc['problems'])} related problem(s), {inc['severity']}, about {minutes} min.",
            'timeline': inc['timeline'][-30:],
            'possible_related_changes': changes,
            'cause': ('HYPOTHESIS: ' + '; '.join(f"{c['kind']} {c['subject']}" for c in changes)) if changes
            else 'UNKNOWN: no change was seen in the 24 h before.',
            'actions': 'Review on the Systems AI page; any fix goes through Maintenance after your Approve.'}
