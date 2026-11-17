"""Natural commissioning ledger (0.3.0, shadow): how each natural cooling start went.

Proposal docs/architecture/COOLING_AI_0_3_0_COMMISSIONING_LEDGER_PROPOSAL_2026-11-16.md (owner pop-ups
2026-11-16 "Approve ...", "Yes, read both", "Build now, hints as 0.3.1"). Every start the Arbiter makes by
itself (idle -> vent, wet evap or A/C) is one free functional test. The ledger records its signature, learns
what "as usual" looks like for this house on held-out days, and says when a start was weaker than that.

It reports what was MEASURED (a pattern code), never a part, an airflow figure or equipment health
(DO_NOT_REGRESS: thermal response only). It never starts anything, never writes outside its own store, and a
fault that was present while the baseline was learned looks normal: the page says so.

Rules that only an App release may change: the window lengths, the qualification counts, the 2-of-3 rule, the
pattern rules, the support margin and the self-check. Only the per-mode line and band are relearned.
"""
import hashlib
import json
import statistics

from .entities import actual_mode, decision, fan_speed
from .learn import BUCKET_S, holdout, theil_sen
from .plan import local_day

SCHEMA = 'house_brain_cooling_ai_commissioning.v1'
MODES = ('VENT', 'WET_EVAP', 'AC')
PRE = 6                     # 30 clean idle minutes before the start (the pre-start drift)
WINDOW = 6                  # the response is read for up to 30 minutes after the start ...
MIN_WINDOW = 3              # ... or to the mode's own stop, but never under 15 minutes
PREWET_MAX = 2              # a pump-only pre-wet of up to 10 minutes belongs to a wet start
MAX_STARTS_PER_DAY = 8      # the excess is counted, never dropped silently
MAX_SEEN_PER_DAY = 48
BUDGET_BYTES = 1_500_000    # all day rows together; at the budget the ledger stops adding, nothing is deleted
MIN_TRAIN = 20              # starts on TRAIN days before a line is fitted
MIN_HOLDOUT = 20            # held-out starts ...
MIN_HOLDOUT_DAYS = 5        # ... on at least this many held-out days, before a band is used
LOW_Q = 0.1                 # one-sided 90 % lower band on the signed residual
SUPPORT_MARGIN_F = 3.0      # a start outside the learned drive range (plus this) is not judged
ANCHOR_STARTS = 30          # the season anchor freezes once a mode has this many usable starts ...
ANCHOR_DAYS = 10            # ... over this many days; it is never relearned
DRIFT_DAYS = 28
DRIFT_MIN_DAYS = 5
Z_ONE_SIDED_90 = 1.2816
TRACER_MIN_DEPRESSION_F = 10.   # under this wet-bulb depression the moisture signal cannot speak
TRACER_MIN_GAIN = 0.5           # °F/h of expected dew-point gain needed before a ratio means anything
DRY_RATIO = 0.25                # a wet start gaining under a quarter of its usual moisture ran like a fan-only start
CHECK_LAST = 20             # the self-check looks at the last 20 judged starts of a mode ...
CHECK_MIN = 10
CHECK_DAYS = 3
CHECK_RATE = 0.30           # ... and silences the mode when more than 30 % were low and lows and normals alternate
ROOM_MOVED_F = 0.5
ROOMS = ('t_living', 't_master')
PATTERNS = ('WET_LIKE_VENT', 'BOTH_REDUCED', 'TEMP_ONLY_LOW', 'VENT_LOW', 'AC_LOW')
PATTERN_WORDS = {
    'WET_LIKE_VENT': 'wet starts cooled like fan-only starts and added almost no moisture',
    'BOTH_REDUCED': 'wet starts cooled less and added less moisture than usual',
    'TEMP_ONLY_LOW': 'wet starts cooled less than usual while moisture rose as usual',
    'VENT_LOW': 'fan-only starts cooled less than usual',
    'AC_LOW': 'A/C starts cooled less than usual',
    'ROOM_FLAT': 'one room reading did not move at all while the other room changed',
}
SKIPS = ('OWNER_OR_SAFETY', 'PAUSED', 'TRANSFER', 'SHORT_RUN', 'INPUT_MISSING', 'GAP', 'OVER_DAILY_CAP')
SET_ASIDE_OWNERS = ('LOCAL_MANUAL', 'HUMAN_THERMOSTAT', 'LIFE_SAFETY')
FIT_VERDICTS = ('BASELINE', 'NORMAL', 'NO_BASELINE', 'BAND_MISCALIBRATED')    # LOW_RESPONSE never re-enters a fit
JUDGED = ('NORMAL', 'LOW_RESPONSE')


def set_aside(row):
    """Why this bucket cannot belong to a judged start, or None. Archive rows hold only the owner and the
    decision (Pause, panic and the evap mode are live-only), so backfilled starts get the weaker filter."""
    d = decision(row.get('decision'))
    owner = row.get('owner') or (d or {}).get('owner')
    if owner in SET_ASIDE_OWNERS or row.get('panic') in ('Force AC', 'Critical') \
            or row.get('evap_mode') not in (None, 'Auto'):
        return 'OWNER_OR_SAFETY'
    if row.get('pause') == 'on' or (d and (d['reason'].startswith('PAUSE') or d['reason'] == 'OWNER_PAUSED')):
        return 'PAUSED'
    return None


def slope(points):
    """Least-squares slope in units per hour from [(t seconds, value)], or None under 3 points."""
    pts = [(t / 3600., v) for t, v in points if v is not None]
    if len(pts) < 3:
        return None
    mx, my = statistics.fmean(x for x, _ in pts), statistics.fmean(y for _, y in pts)
    den = sum((x - mx) ** 2 for x, _ in pts)
    return sum((x - mx) * (y - my) for x, y in pts) / den if den else None


def low_quantile(values, q=LOW_Q):
    """Finite-sample one-sided lower bound: the k-th smallest with k = floor((n + 1) q); None when k < 1."""
    k = int((len(values) + 1) * q)
    return sorted(values)[k - 1] if k >= 1 else None


def candidates(rows):
    """Every change into a cooling mode in `rows` [(t, row)] whose response window is complete, as
    (t, record or None, skip reason or None). A start still inside its window is left for the next pass."""
    out = []
    modes = [actual_mode(r) for _, r in rows]
    n = len(rows)
    for i in range(1, n):
        mode = modes[i]
        if mode not in MODES or modes[i - 1] == mode or rows[i][0] - rows[i - 1][0] != BUCKET_S:
            continue
        t0 = rows[i][0]
        # the response window: this mode, contiguous, until its own stop or 30 minutes
        k = i
        while k < n and k - i < WINDOW and modes[k] == mode and (k == i or rows[k][0] - rows[k - 1][0] == BUCKET_S):
            k += 1
        ended = k < n and (modes[k] != mode or rows[k][0] - rows[k - 1][0] != BUCKET_S)
        if k - i < WINDOW and not ended:
            continue                                # still running at the end of what is held: judge it later
        # what came before: an optional pump-only pre-wet, then 30 idle minutes
        j, prewet = i - 1, 0
        while mode == 'WET_EVAP' and j >= 0 and prewet < PREWET_MAX and modes[j] == 'PUMP_ONLY' \
                and rows[j + 1][0] - rows[j][0] == BUCKET_S:
            j, prewet = j - 1, prewet + 1
        pre = list(range(j - PRE + 1, j + 1))
        if pre[0] < 0:
            continue                                # the start of what is held: not enough before it to say
        window = list(range(i, k))
        involved = pre + list(range(j + 1, i)) + window
        reason = None
        if any(modes[p] in MODES for p in pre):
            reason = 'TRANSFER'
        elif any(rows[p + 1][0] - rows[p][0] != BUCKET_S for p in range(pre[0], i)) \
                or any(modes[p] != 'IDLE' for p in pre):
            reason = 'GAP'
        elif next((set_aside(rows[p][1]) for p in involved if set_aside(rows[p][1])), None):
            reason = next(set_aside(rows[p][1]) for p in involved if set_aside(rows[p][1]))
        elif len(window) < MIN_WINDOW:
            reason = 'SHORT_RUN'
        if reason:
            out.append((t0, None, reason))
            continue
        record = signature(rows, pre, window, mode, prewet)
        out.append((t0, record, None if record else 'INPUT_MISSING'))
    return out


def signature(rows, pre, window, mode, prewet):
    """The start's numbers: the change in slope (after less before) of indoor temperature and dew point."""
    first = rows[window[0]][1]
    span = [pre[-1]] + window if not prewet else window     # after a pre-wet the fan start is the first point
    before_t = slope([(rows[p][0], rows[p][1].get('t_in')) for p in pre])
    after_t = slope([(rows[p][0], rows[p][1].get('t_in')) for p in span])
    other = first.get('wb') if mode == 'WET_EVAP' else first.get('t_out')
    if before_t is None or after_t is None or other is None or first.get('t_in') is None:
        return None
    before_dp = slope([(rows[p][0], rows[p][1].get('dp_in')) for p in pre])
    after_dp = slope([(rows[p][0], rows[p][1].get('dp_in')) for p in span])
    d = decision(first.get('decision'))
    rec = {'t': rows[window[0]][0], 'mode': mode, 'n': len(window), 'prewet': prewet,
           'r': round(before_t - after_t, 3),               # positive = the house turned cooler-going
           'm': round(after_dp - before_dp, 3) if None not in (before_dp, after_dp) else None,
           'x': round(other - first['t_in'], 2),
           'dep': round(first['t_out'] - first['wb'], 2) if None not in (first.get('t_out'), first.get('wb')) else None,
           'fan': fan_speed(first), 'reason': d['reason'][:48] if d else None}
    rooms = {}
    for key in ROOMS:
        values = [rows[p][1].get(key) for p in pre + window]
        if sum(v is not None for v in values) >= PRE + MIN_WINDOW - 1:
            present = [v for v in values if v is not None]
            rooms[key] = [round(present[-1] - present[0], 2), round(max(present) - min(present), 2)]
    if rooms:
        rec['rooms'] = rooms
        if len(rooms) == 2:
            (a, (move_a, range_a)), (b, (move_b, range_b)) = rooms.items()
            # "flat" is a reading that did not change at all, not a room that lags
            rec['flat'] = a if range_a == 0 and abs(move_b) >= ROOM_MOVED_F else \
                b if range_b == 0 and abs(move_a) >= ROOM_MOVED_F else None
    return rec


def fit(records, key='r', against='x'):
    """A robust line of one signal against one driver on TRAIN days and a one-sided lower band from HOLDOUT
    days. `status` is QUALIFIED only with enough held-out starts on enough held-out days."""
    pts = [(local_day(s['t']), s[against], s[key]) for s in records
           if s.get('verdict') in FIT_VERDICTS and s.get(key) is not None and s.get(against) is not None]
    train = [(x, y) for d, x, y in pts if not holdout(d)]
    held = [(d, x, y) for d, x, y in pts if holdout(d)]
    base = {'train': len(train), 'holdout': len(held), 'holdout_days': len({d for d, _, _ in held}),
            'days': len({d for d, _, _ in pts}), 'status': 'LEARNING'}
    line = theil_sen(train) if len(train) >= MIN_TRAIN else None
    if line is None:
        return base
    a, b = line
    base.update(slope=a, intercept=b, x_min=min(x for x, _ in train), x_max=max(x for x, _ in train))
    if len(held) < MIN_HOLDOUT or base['holdout_days'] < MIN_HOLDOUT_DAYS:
        return {**base, 'status': 'EARLY'}
    low = low_quantile([y - (a * x + b) for _, x, y in held])
    if low is None:
        return {**base, 'status': 'EARLY'}
    return {**base, 'status': 'QUALIFIED', 'low': low}


def fingerprint(model):
    return hashlib.sha256(json.dumps(model, sort_keys=True, allow_nan=False).encode()).hexdigest()[:12]


def judge(start, models):
    """(verdict, pattern or None) for one new start against the models held BEFORE it."""
    model = (models.get(start['mode']) or {}).get('response') or {}
    if model.get('status') != 'QUALIFIED' or not (
            model['x_min'] - SUPPORT_MARGIN_F <= start['x'] <= model['x_max'] + SUPPORT_MARGIN_F):
        return 'NO_BASELINE', None
    residual = start['r'] - (model['slope'] * start['x'] + model['intercept'])
    if residual >= model['low']:
        return 'NORMAL', None
    return 'LOW_RESPONSE', pattern(start, models)


def pattern(start, models):
    """What was measured on a low start. Codes describe readings; they name no part."""
    mode = start['mode']
    if mode == 'AC':
        return 'AC_LOW'             # the coil dries the air: the moisture signal is never read for A/C
    if mode == 'VENT':
        return 'VENT_LOW'
    moisture = (models.get('WET_EVAP') or {}).get('moisture') or {}
    if start.get('m') is None or start.get('dep') is None or start['dep'] < TRACER_MIN_DEPRESSION_F \
            or moisture.get('status') != 'QUALIFIED':
        return 'NO_TRACER_CONTRAST'
    expected = moisture['slope'] * start['dep'] + moisture['intercept']
    if expected < TRACER_MIN_GAIN:
        return 'NO_TRACER_CONTRAST'
    if start['m'] < DRY_RATIO * expected:
        return 'WET_LIKE_VENT'
    return 'BOTH_REDUCED' if start['m'] - expected < moisture['low'] else 'TEMP_ONLY_LOW'


def miscalibrated(history):
    """The self-check: over the last judged starts of one mode, too many lows that alternate with normals
    mean the band is wrong, not the plant. A trailing unbroken run of lows is a plant pattern, not this."""
    recent = [s for s in history if (s.get('would') or s.get('verdict')) in JUDGED][-CHECK_LAST:]
    if len(recent) < CHECK_MIN or len({local_day(s['t']) for s in recent}) < CHECK_DAYS:
        return False
    marks = [(s.get('would') or s['verdict']) == 'LOW_RESPONSE' for s in recent]
    back_to_normal = sum(1 for a, b in zip(marks, marks[1:]) if a and not b)
    return sum(marks) / len(marks) > CHECK_RATE and back_to_normal >= 2


def shown_pattern(history):
    """A pattern is shown only after 2 low starts on 2 days and 2 of the last 3 judged starts; 2 normal
    starts clear it (they push it out of the last 3)."""
    judged = [s for s in history if s.get('verdict') in JUDGED]
    last = judged[-3:]
    for code in PATTERNS:
        hits = [s for s in last if s.get('pattern') == code]
        days = {local_day(s['t']) for s in judged if s.get('pattern') == code}
        if len(hits) >= 2 and len(days) >= 2:
            return code
    return None


def flat_room(starts):
    with_rooms = [s for s in starts if len(s.get('rooms') or {}) == 2][-3:]
    for key in ROOMS:
        hits = [s for s in with_rooms if s.get('flat') == key]
        if len(hits) >= 2 and len({local_day(s['t']) for s in hits}) >= 2:
            return key
    return None


def daypart(t):
    return int((t % 86400) // 21600)        # four six-hour parts of the UTC day: a stable label, not a clock


def drift(history, anchor, now):
    """Slow change against the frozen season anchor: the last 28 days' day-mean residuals with a one-sided
    90 % upper bound. WEAKER only when even the upper bound is below zero."""
    if not anchor:
        return {'status': 'NO_ANCHOR'}
    parts = set(anchor.get('dayparts') or [])
    by_day = {}
    for s in history:
        if s['t'] < now - DRIFT_DAYS * 86400 or s.get('verdict') == 'BASELINE' and s['t'] <= anchor['until']:
            continue
        if not anchor['x_min'] <= s['x'] <= anchor['x_max'] or daypart(s['t']) not in parts:
            continue
        by_day.setdefault(local_day(s['t']), []).append(s['r'] - (anchor['slope'] * s['x'] + anchor['intercept']))
    means = [statistics.fmean(v) for v in by_day.values()]
    if len(means) < DRIFT_MIN_DAYS:
        return {'status': 'NOT_PROVEN', 'days': len(means)}
    mean = statistics.fmean(means)
    upper = mean + Z_ONE_SIDED_90 * statistics.stdev(means) / len(means) ** .5
    return {'status': 'WEAKER' if upper < 0 else 'STEADY', 'days': len(means),
            'mean_residual': round(mean, 3), 'upper_bound': round(upper, 3)}


class Commissioning:
    def __init__(self, store, now):
        self.store = store
        self.since = store.get('commissioning:since', None)
        if self.since is None:
            self.since = int(now)           # starts before the first 0.3.0 run only build the baseline
            store.put('commissioning:since', self.since)
        self.index = [d for d in store.get('commissioning:index', []) if isinstance(d, str)]
        self.days = {d: store.get('commissioning:day:' + d, None) for d in self.index}
        self.days = {d: v for d, v in self.days.items() if isinstance(v, dict)}
        self.anchors = store.get('commissioning:anchors', {}) or {}
        self.models = store.get('commissioning:models', {}) or {}
        self.bytes = sum(len(json.dumps(v, separators=(',', ':'))) for v in self.days.values())
        self.full = False

    def starts(self, mode=None):
        out = [s for d in sorted(self.days) for s in self.days[d].get('starts', [])]
        return [s for s in out if mode is None or s['mode'] == mode]

    def update(self, rows, now):
        """One pass over the held grid rows: record every new start (oldest first), judging each against the
        models fitted before this pass, then refit. A recorded start is never judged again."""
        touched = set()
        for t, record, skip in candidates(rows):
            day = local_day(t)
            rec = self.days.setdefault(day, {'starts': [], 'skip': {}, 'seen': []})
            if t in rec['seen'] or len(rec['seen']) >= MAX_SEEN_PER_DAY:
                continue
            if self.bytes >= BUDGET_BYTES:
                self.full = True            # stop adding; nothing already kept is deleted
                break
            if record is not None and len(rec['starts']) >= MAX_STARTS_PER_DAY:
                record, skip = None, 'OVER_DAILY_CAP'
            if record is None:
                rec['skip'][skip] = rec['skip'].get(skip, 0) + 1
            else:
                self.verdict(record)
                rec['starts'].append(record)
                rec['starts'].sort(key=lambda s: s['t'])
            rec['seen'].append(t)
            touched.add(day)
        for day in sorted(touched):
            self.store.put('commissioning:day:' + day, self.days[day])
        if touched:
            self.index = sorted(self.days)
            self.store.put('commissioning:index', self.index)
            self.bytes = sum(len(json.dumps(v, separators=(',', ':'))) for v in self.days.values())
        self.refit()

    def verdict(self, record):
        if record['t'] < self.since:
            record['verdict'] = 'BASELINE'          # history from before this release: never judged
            return
        verdict, code = judge(record, self.models)
        model = (self.models.get(record['mode']) or {}).get('response') or {}
        if model.get('status') == 'QUALIFIED':
            record['fp'] = fingerprint(model)
        history = self.starts(record['mode'])
        if verdict in JUDGED and miscalibrated(history + [{**record, 'verdict': verdict}]):
            record.update(verdict='BAND_MISCALIBRATED', would=verdict)      # kept, shown as silence, refitted
            return
        record['verdict'] = verdict
        if code:
            record['pattern'] = code

    def refit(self):
        models = {}
        for mode in MODES:
            history = self.starts(mode)
            models[mode] = {'response': fit(history)}
            if mode == 'WET_EVAP':
                models[mode]['moisture'] = fit(history, 'm', 'dep')
            usable = [s for s in history if s.get('verdict') in FIT_VERDICTS]
            response = models[mode]['response']
            if mode not in self.anchors and 'slope' in response and len(usable) >= ANCHOR_STARTS \
                    and len({local_day(s['t']) for s in usable}) >= ANCHOR_DAYS:
                self.anchors[mode] = {'slope': response['slope'], 'intercept': response['intercept'],
                                      'x_min': response['x_min'], 'x_max': response['x_max'],
                                      'starts': len(usable), 'until': max(s['t'] for s in usable),
                                      'dayparts': sorted({daypart(s['t']) for s in usable})}
                self.store.put('commissioning:anchors', self.anchors)
        if models != self.models:
            self.models = models
            self.store.put('commissioning:models', models)

    def report(self, now):
        modes = {}
        for mode in MODES:
            history = self.starts(mode)
            response = (self.models.get(mode) or {}).get('response') or {'status': 'LEARNING'}
            silenced = miscalibrated(history)
            code = None if silenced else shown_pattern(history)
            judged = [s for s in history if s.get('verdict') in JUDGED]
            modes[mode] = {
                'starts': len(history), 'baseline': sum(s.get('verdict') == 'BASELINE' for s in history),
                'judged': len(judged), 'low': sum(s['verdict'] == 'LOW_RESPONSE' for s in judged),
                'last3_low': sum(s['verdict'] == 'LOW_RESPONSE' for s in judged[-3:]),
                'band': 'BAND_MISCALIBRATED' if silenced else response.get('status'),
                'train': response.get('train'), 'holdout': response.get('holdout'),
                'holdout_days': response.get('holdout_days'),
                'median_response_f_per_h': round(statistics.median(s['r'] for s in history), 2) if history else None,
                'pattern': code, 'pattern_words': PATTERN_WORDS.get(code),
                'drift': drift(history, self.anchors.get(mode), now),
                'anchor_starts': (self.anchors.get(mode) or {}).get('starts'),
                'last': [{k: s.get(k) for k in ('t', 'verdict', 'pattern', 'r', 'm', 'x', 'n', 'fan')}
                         for s in history[-5:]]}
        skipped = {}
        for rec in self.days.values():
            for reason, n in rec.get('skip', {}).items():
                skipped[reason] = skipped.get(reason, 0) + n
        room = flat_room(self.starts())
        return {'schema_version': SCHEMA, 'since': self.since, 'modes': modes, 'not_judged': skipped,
                'room_flat': room, 'room_flat_words': PATTERN_WORDS['ROOM_FLAT'] if room else None,
                'store': {'bytes': self.bytes, 'budget_bytes': BUDGET_BYTES, 'days': len(self.days),
                          'status': 'COMMISSIONING_STORE_FULL' if self.full else 'OK'},
                'note': 'Compares the house with its own past. A fault that was already there while the baseline '
                        'was learned looks normal. A pattern is what was measured, not a diagnosis.',
                'evidence_class': 'MEASURED_RESPONSE', 'physical_actions': False, 'control_authority': 'NONE'}

    def token(self, now):
        """At most 40 characters for the digest."""
        report = self.report(now)
        for mode, words in (('WET_EVAP', 'WET'), ('VENT', 'VENT'), ('AC', 'AC')):
            m = report['modes'][mode]
            if m['pattern']:
                return f"starts: {words} low {m['last3_low']}/3"[:40]
        return 'starts: ok' if any(m['judged'] for m in report['modes'].values()) else 'starts: no baseline'
