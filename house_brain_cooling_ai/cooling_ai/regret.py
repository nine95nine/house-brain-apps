"""Rule-attributed regret ledger (0.2.0): which Arbiter rule costs the most money or comfort. SHADOW only.

Each comparable minute is keyed by the Arbiter's own REASON (the 5th field of
sensor.cooling_arbiter_v2_decision), the scorecard code and the weather band. A disagreement minute adds the
scorecard's SIMULATED increments: house minus Cooling AI, so a positive cost means the house spent more and
a positive degree-hour delta means the house was warmer. A minute that could not be priced (no learned
model, no power or price, an unknown EPR-6 bank) is counted as unpriced and adds no dollars: unknown is
never $0. A rule is REGRET only when the house both spent more and was not cooler; when it spent more but
kept the house cooler it is a COMFORT_TRADE, so money is never ranked above comfort. Nothing here acts.
"""
from .plan import DISAGREE, local_day

LEDGER_DAYS = 120
MAX_KEYS_PER_DAY = 400          # bounded store growth; the excess folds into one OTHER row, still counted
SCHEMA = 'house_brain_cooling_ai_regret.v1'
WINDOWS = (7, 28)
TOP = 5
MAX_LISTED = 20                 # every rule seen, most costly first, for the evidence view
FIELDS = ('minutes', 'agree_minutes', 'priced_minutes', 'bank_covered_minutes', 'unpriced_minutes',
          'out_of_support_minutes', 'cost_delta_usd', 'degree_h_delta')
MIN_EVIDENCE_DAYS = 3           # fewer distinct days than this is labelled THIN, never a firm ranking
MIN_REPORT_USD = 0.25           # below this in a window a rule is not ranked (noise floor, as FDD tools do)
Z_LOW = 1.2816                  # one-sided 90 % lower bound on the window total, from day-to-day spread
OTHER = 'OTHER|OTHER|OTHER'


def band(row):
    """Weather band: outdoor °F and outdoor dew point, each in 5 °F steps; '?' when unknown."""
    def step(v, tag):
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v != v:
            return f'{tag}?'
        lo = int(v // 5) * 5
        return f'{tag}{lo}-{lo + 5}'
    return step(row.get('t_out'), 'T') + '/' + step(row.get('dp_out'), 'DP')


def clean(reason):
    """The Arbiter REASON as a bounded token (it is our own fixed vocabulary, but never trusted blindly)."""
    if not isinstance(reason, str) or not reason:
        return 'UNKNOWN'
    out = ''.join(c for c in reason.upper() if c.isalnum() or c == '_')[:48]
    return out or 'UNKNOWN'


class Ledger:
    def __init__(self, store):
        # One small store row per local day (the store refuses a value over 2 MB, and a single 120-day value
        # could pass that); each minute rewrites only today's row.
        self.store = store
        self.index = [d for d in store.get('regret:index', []) if isinstance(d, str)]
        self.days = {d: store.get('regret:day:' + d, {}) or {} for d in self.index}
        self.days = {d: v for d, v in self.days.items() if v}

    def add(self, reason, code, row, outcome, now, minutes=1.):
        """Count one comparable minute. Not-comparable minutes (NC_*) never enter the ledger."""
        if code != 'AGREE' and code not in DISAGREE:
            return
        day = self.days.setdefault(local_day(now), {})
        key = f'{clean(reason)}|{code}|{band(row)}'
        if key not in day and len(day) >= MAX_KEYS_PER_DAY:
            key = OTHER
        rec = day.setdefault(key, {f: 0. for f in FIELDS})
        if code == 'AGREE':
            rec['agree_minutes'] += minutes
        else:
            rec['minutes'] += minutes
            outcome = outcome or {}
            if outcome and not outcome.get('in_support', True):
                rec['out_of_support_minutes'] += minutes   # the model would be extrapolating: not priced
                rec['unpriced_minutes'] += minutes
            elif outcome.get('bank_covered'):
                rec['bank_covered_minutes'] += minutes
                rec['priced_minutes'] += minutes          # known $0 by the EPR-6 rule, not an unknown
                rec['degree_h_delta'] += outcome.get('degree_h_delta', 0.)
            elif outcome.get('priced'):
                rec['priced_minutes'] += minutes
                rec['cost_delta_usd'] += outcome['cost_delta_usd']
                rec['degree_h_delta'] += outcome.get('degree_h_delta', 0.)
            else:
                rec['unpriced_minutes'] += minutes
        today = local_day(now)
        self.store.put('regret:day:' + today, self.days[today])
        if today not in self.index:
            self.index.append(today)
            self._prune(now)
            self.store.put('regret:index', self.index)

    def _prune(self, now):
        # Only days older than the window go; a clock that stepped back never removes newer days.
        oldest = local_day(now - (LEDGER_DAYS - 1) * 86400)
        for day in [d for d in self.days if d < oldest]:
            self.days.pop(day)
            self.store.put('regret:day:' + day, {})       # emptied, never left holding stale rows
        self.index = [d for d in self.index if d >= oldest]

    def report(self, now):
        out = {'schema_version': SCHEMA, 'evidence_class': 'SIMULATED', 'executed': False,
               'note': 'Modelled from learned rates; which rule decided comes from the Arbiter. Not savings.',
               'days_kept': len(self.days), 'windows': {}}
        for n in WINDOWS:
            keep = {local_day(now - k * 86400) for k in range(n)}
            rules = {}
            for day, recs in self.days.items():
                if day not in keep:
                    continue
                for key, rec in recs.items():
                    reason, code, wband = key.split('|', 2)
                    r = rules.setdefault(reason, {**{f: 0. for f in FIELDS}, 'days': set(), 'codes': {}, 'bands': {},
                                                  'daily': {}})
                    for f in FIELDS:
                        r[f] += rec[f]
                    if rec['minutes']:
                        r['days'].add(day)
                        r['daily'][day] = r['daily'].get(day, 0.) + rec['cost_delta_usd']
                        r['codes'][code] = r['codes'].get(code, 0) + rec['minutes']
                        r['bands'][wband] = r['bands'].get(wband, 0) + rec['minutes']
            table = [summary(reason, r) for reason, r in rules.items()]
            ranked = sorted((t for t in table if t['verdict'] in ('REGRET', 'COMFORT_TRADE', 'NOT_PROVEN')),
                            key=lambda t: (-t['cost_low_usd'], -t['cost_delta_usd'], -t['minutes'], t['reason']))
            out['windows'][f'{n}d'] = {
                'top': ranked[:TOP],
                'not_qualified': sorted((t['reason'] for t in table if t['verdict'] == 'MODEL_NOT_QUALIFIED')),
                'rules_seen': len(table),
                'rules': sorted(table, key=lambda t: (-t['cost_delta_usd'], -t['minutes'], t['reason']))[:MAX_LISTED],
                'disagree_minutes': round(sum(t['minutes'] for t in table), 1),
                'agree_minutes': round(sum(t['agree_minutes'] for t in table), 1)}
        return out


def lower_bound(r):
    """One-sided 90 % lower bound on the window's cost, treating each evidence day as one draw (minutes in a
    day are strongly autocorrelated, so a minute-level bound would be far too narrow). None under 3 days."""
    vals = list(r['daily'].values())
    n = len(vals)
    if n < MIN_EVIDENCE_DAYS:
        return None
    mean = sum(vals) / n
    sd = (sum((v - mean) ** 2 for v in vals) / (n - 1)) ** 0.5
    return n * (mean - Z_LOW * sd / n ** 0.5)


def verdict(r):
    if not r['minutes']:
        return 'AGREES'
    if not r['priced_minutes']:
        return 'MODEL_NOT_QUALIFIED'
    if r['cost_delta_usd'] > 0:
        if r['cost_delta_usd'] < MIN_REPORT_USD:
            return 'BELOW_FLOOR'
        low = lower_bound(r)
        if low is None:
            low = 0.
        if low <= 0:
            return 'NOT_PROVEN'
        return 'REGRET' if r['degree_h_delta'] >= 0 else 'COMFORT_TRADE'
    return 'HOUSE_NOT_DEARER'


def evidence(r):
    return 'THIN' if len(r['days']) < MIN_EVIDENCE_DAYS else 'OK'


def summary(reason, r):
    top = lambda d: max(sorted(d), key=lambda k: d[k]) if d else None
    return {'reason': reason, 'verdict': verdict(r), 'minutes': round(r['minutes'], 1),
            'agree_minutes': round(r['agree_minutes'], 1), 'priced_minutes': round(r['priced_minutes'], 1),
            'unpriced_minutes': round(r['unpriced_minutes'], 1),
            'bank_covered_minutes': round(r['bank_covered_minutes'], 1),
            'out_of_support_minutes': round(r['out_of_support_minutes'], 1), 'evidence': evidence(r),
            'cost_delta_usd': round(r['cost_delta_usd'], 4), 'degree_h_delta': round(r['degree_h_delta'], 4),
            'cost_low_usd': round(lower_bound(r) or 0., 4),
            'evidence_days': len(r['days']), 'top_code': top(r['codes']), 'top_band': top(r['bands'])}
