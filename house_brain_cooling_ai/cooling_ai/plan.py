"""Cooling AI's own plan each minute, and the shadow scorecard against what the house actually did.

The plan is a *recommendation* only. It is compared with *actual operation* (relay feedback and
the Ecobee's call), never with the Arbiter's phase as if that were operation, and nothing here is
ever executed. Moments the Arbiter does not own, or where the plant is between modes, are set
aside as not comparable and never counted as Cooling AI being right.

The counterfactual comfort and cost figures are SIMULATED from the learned rates: they are shown
with their band and are never presented as savings.
"""
import math
from datetime import datetime
from zoneinfo import ZoneInfo

from .entities import MODES, SHADOW_MODE, actual_mode, decision

ZONE = ZoneInfo('America/Phoenix')
START_BAND_F = 0.5          # start cooling at target + 0.5 °F, keep going until the target is met
ORDER = ('VENT', 'WET_EVAP', 'AC')     # cheapest first (fan, fan + pump, compressor)
KEEP_DAYS = 14
TRANSITION_PHASES = frozenset({'EVAP_RELEASE', 'RECONCILE', 'OFF', 'DISABLED', 'EVAP_DRYOUT', 'MANUAL_EVAP'})
LEARNING = 'LEARNING'
# Fixed reason codes. Comparable minutes are AGREE or AI_PREFERS_<plan>_ARBITER_<actual>.
NOT_COMPARABLE = ('NC_INPUT_MISSING', 'NC_LIFE_SAFETY', 'NC_PANIC', 'NC_HUMAN_THERMOSTAT', 'NC_OWNER_OVERRIDE',
                  'NC_PAUSED', 'NC_PENDING_OR_TRANSITION', 'NC_ACTUAL_UNCLEAR', 'NC_AI_LEARNING')
DISAGREE = tuple(f'AI_PREFERS_{a}_ARBITER_{b}' for a in MODES for b in MODES if a != b)
CODES = ('AGREE',) + DISAGREE + NOT_COMPARABLE
PLAN_WHY = ('COMFORT_MET', 'BEST_LEARNED_MODE', 'KEEP_RUNNING', 'NOTHING_LEARNED_HELPS', 'MODES_STILL_LEARNING',
            'INPUT_MISSING')


def local_day(at):
    return datetime.fromtimestamp(at, ZONE).strftime('%Y-%m-%d')


def target_of(row):
    """The comfort target the Arbiter itself publishes, raised to its cooling floor; None if missing."""
    comfort = row.get('comfort')
    if comfort is None:
        comfort = row.get('evap_target')
    if comfort is None:
        return None
    if row.get('charge_ok') == 'on' and row.get('charge_target') is not None:
        comfort = min(comfort, row['charge_target'])
    floor = row.get('floor')
    return max(comfort, floor) if floor is not None else comfort


def eligible(row, mode):
    """The Arbiter's live permission flags; Cooling AI never plans a mode they block."""
    t_in, vent_floor = row.get('t_in'), row.get('vent_floor')
    if mode == 'VENT':
        return row.get('vent_ok') == 'on' and row.get('vent_stop') != 'on' and \
            (vent_floor is None or (t_in is not None and t_in > vent_floor))
    if mode == 'WET_EVAP':
        return row.get('wet_ok') == 'on' and row.get('evap_perm') == 'on'
    if mode == 'AC':
        return row.get('thermostat') in ('cool', 'heat_cool', 'auto')
    return True


def plan(row, models, previous=None):
    """(mode or LEARNING or None, why). Uses only learned rates whose whole band is cooling."""
    t_in, target = row.get('t_in'), target_of(row)
    if t_in is None or target is None:
        return None, 'INPUT_MISSING'
    if t_in <= target:
        return 'IDLE', 'COMFORT_MET'
    if previous in ORDER and eligible(row, previous) and cools(models.get(previous), row):
        return previous, 'KEEP_RUNNING'
    if t_in < target + START_BAND_F:
        return 'IDLE', 'COMFORT_MET'        # a new or different mode starts only past the start band
    for mode in ORDER:
        if not eligible(row, mode):
            continue
        works = cools(models.get(mode), row)
        if works is None:
            # A cheaper mode it has not learned yet: say so, never guess past it to a dearer one.
            return LEARNING, 'MODES_STILL_LEARNING'
        if works:
            return mode, 'BEST_LEARNED_MODE'
    return 'IDLE', 'NOTHING_LEARNED_HELPS'


def cools(model, row, t_in=None):
    """True when the model's whole band says the mode cools the house now; None when not learned."""
    if not model or model.get('status') != 'LEARNED':
        return None
    rate = predict(model, row, t_in)
    return None if rate is None else rate[0] + rate[1] < 0


def predict(model, row, t_in=None):
    """(rate °F/h, band half-width) for this mode at these conditions, or None."""
    x = drive(model['mode'], row, t_in)
    if x is None or model.get('band') is None:
        return None
    return model['slope'] * x + model['intercept'], model['band']


def drive(mode, row, t_in=None):
    t_in = row.get('t_in') if t_in is None else t_in
    other = row.get('wb') if mode == 'WET_EVAP' else row.get('t_out')
    return None if t_in is None or other is None else other - t_in


def compare(row, ai, now):
    """One reason code for this minute (see CODES)."""
    d = decision(row.get('decision'))
    if d is None:
        return 'NC_INPUT_MISSING'
    owner = row.get('owner') or d['owner']
    if owner == 'LIFE_SAFETY' or row.get('shadow') == 'OFF_LIFE_SAFETY':
        return 'NC_LIFE_SAFETY'
    if row.get('panic') in ('Force AC', 'Critical'):
        return 'NC_PANIC'
    if owner == 'HUMAN_THERMOSTAT' or human_override(row, now):
        return 'NC_HUMAN_THERMOSTAT'
    if owner == 'LOCAL_MANUAL' or (row.get('evap_mode') not in (None, 'Auto')):
        return 'NC_OWNER_OVERRIDE'
    if row.get('pause') == 'on' or d['reason'].startswith('PAUSE') or d['reason'] == 'OWNER_PAUSED':
        return 'NC_PAUSED'
    phase = row.get('phase') or d['phase']
    if phase.endswith('_PENDING') or phase in TRANSITION_PHASES or d['phase'].endswith('_PENDING'):
        return 'NC_PENDING_OR_TRANSITION'
    actual = actual_mode(row)
    if actual not in MODES:
        return 'NC_ACTUAL_UNCLEAR'
    if ai == LEARNING:
        return 'NC_AI_LEARNING'
    if ai not in MODES:
        return 'NC_INPUT_MISSING'
    return 'AGREE' if ai == actual else f'AI_PREFERS_{ai}_ARBITER_{actual}'


def human_override(row, now):
    """A thermostat override window still open (input_datetime state is a local wall-clock time)."""
    value = row.get('human_until')
    if not isinstance(value, str):
        return False
    try:
        until = datetime.fromisoformat(value)
    except ValueError:
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=ZONE)
    return until.timestamp() > now


class Scorecard:
    """Minutes per code per local day, plant switches each side would make, and the SIMULATED plan list."""

    def __init__(self, store):
        self.store = store
        self.days = store.get('scorecard:days', {})
        self.sim = store.get('scorecard:sim', {'t_sim': None})
        self.last = store.get('scorecard:last', {})

    def add(self, row, ai, code, models, power, now, minutes=1.):
        day = local_day(now)
        rec = self.days.setdefault(day, {'codes': {}, 'ai_switches': 0, 'house_switches': 0, 'shadow_agree': 0,
                                         'shadow_compared': 0, 'sim': {'ai_degree_h': 0., 'house_degree_h': 0.,
                                                                       'band_degree_h': 0., 'ai_kwh': 0.,
                                                                       'house_kwh': 0., 'cost_delta': 0.,
                                                                       'minutes': 0, 'bank_covered_minutes': 0,
                                                                       'bank_unknown_minutes': 0}})
        rec['codes'][code] = rec['codes'].get(code, 0) + minutes
        actual = actual_mode(row)
        if ai in MODES and self.last.get('ai') in MODES and ai != self.last['ai']:
            rec['ai_switches'] += 1
        if actual in MODES and self.last.get('house') in MODES and actual != self.last['house']:
            rec['house_switches'] += 1
        self.last = {'ai': ai if ai in MODES else self.last.get('ai'),
                     'house': actual if actual in MODES else self.last.get('house')}
        shadow = SHADOW_MODE.get(row.get('shadow'))
        if code == 'AGREE' or code in DISAGREE:
            if shadow is not None:
                rec['shadow_compared'] += minutes
                rec['shadow_agree'] += minutes if shadow == ai else 0
            self._simulate(rec['sim'], row, ai, actual, code, models, power, minutes)
        else:
            self.sim = {'t_sim': None}
        self._prune(now)
        self.store.put('scorecard:days', self.days)
        self.store.put('scorecard:sim', self.sim)
        self.store.put('scorecard:last', self.last)

    def _simulate(self, sim, row, ai, actual, code, models, power, minutes):
        """SIMULATED: during a disagreement, follow a shadow indoor temperature under Cooling AI's plan."""
        target, t_in = target_of(row), row.get('t_in')
        if code == 'AGREE' or target is None or t_in is None:
            self.sim = {'t_sim': None}
            return
        t_sim = self.sim.get('t_sim')
        t_sim = t_in if t_sim is None else t_sim
        mode_model = models.get('IDLE' if ai == 'IDLE' else ai)
        rate = predict(mode_model, row, t_sim) if mode_model and mode_model.get('status') == 'LEARNED' else None
        if rate is None:
            self.sim = {'t_sim': None}
            return
        hours = minutes / 60.
        run_h = self.sim.get('run_h', 0.) + hours
        t_sim = t_sim + rate[0] * hours
        sim['ai_degree_h'] += max(0., t_sim - target) * hours
        sim['house_degree_h'] += max(0., t_in - target) * hours
        # The shadow temperature's band widens with the time it has been integrated (band × run hours).
        sim['band_degree_h'] += rate[1] * run_h * hours
        sim['minutes'] += minutes
        ai_kw, house_kw = power.get(ai), power.get(actual)
        if ai_kw is not None and house_kw is not None:
            sim['ai_kwh'] += ai_kw * hours
            sim['house_kwh'] += house_kw * hours
            on_peak, bank = row.get('on_peak'), row.get('bank_kwh')
            if on_peak == 'on' and bank is None:
                sim['bank_unknown_minutes'] += minutes          # unknown is not $0
            elif on_peak == 'on' and bank > 0:
                sim['bank_covered_minutes'] += minutes
            elif row.get('price') is not None and on_peak in ('on', 'off'):
                sim['cost_delta'] += (ai_kw - house_kw) * hours * row['price']
        self.sim = {'t_sim': t_sim, 'run_h': run_h}

    def _prune(self, now):
        keep = {local_day(now - n * 86400) for n in range(KEEP_DAYS)}
        for day in [d for d in self.days if d not in keep]:
            self.days.pop(day)

    def report(self, now):
        today = self.days.get(local_day(now)) or {}
        out = {}
        for day, rec in sorted(self.days.items()):
            codes = rec['codes']
            comparable = sum(v for k, v in codes.items() if k == 'AGREE' or k in DISAGREE)
            top = sorted(((v, k) for k, v in codes.items() if k in DISAGREE), reverse=True)[:3]
            out[day] = {'comparable_minutes': comparable, 'agree_minutes': codes.get('AGREE', 0),
                        'agreement': codes.get('AGREE', 0) / comparable if comparable else None,
                        'top_differences': [{'code': k, 'minutes': v} for v, k in top],
                        'not_comparable': {k: v for k, v in codes.items() if k in NOT_COMPARABLE},
                        'ai_switches': rec['ai_switches'], 'house_switches': rec['house_switches'],
                        'shadow_selection_agreement': rec['shadow_agree'] / rec['shadow_compared']
                        if rec['shadow_compared'] else None,
                        'simulated': {**{k: round(v, 3) if isinstance(v, float) else v for k, v in rec['sim'].items()},
                                      'evidence_class': 'SIMULATED',
                                      'note': 'Modelled from learned rates; not a measured result and not savings.'}}
        return {'today': out.get(local_day(now)), 'days': out, 'executed': False,
                'has_today': bool(today)}


def precool_value(rows, models, power, prices, precool_f=2.):
    """SIMULATED pre-cool value for one weekday: on-peak A/C time a 2 °F pre-cool before 12:00 might avoid.

    rows: that day's 5-minute rows. Zero, with the reason, while the EPR-6 bank covers on-peak."""
    peak = [r for r in rows if r.get('on_peak') == 'on']
    if not peak or any(r.get('holiday') == 'on' for r in rows):
        return {'status': 'NOT_A_PEAK_DAY'}
    ac_h = sum(actual_mode(r) == 'AC' for r in peak) * 5 / 60.
    idle = models.get('IDLE')
    rates = [predict(idle, r) for r in peak] if idle and idle.get('status') == 'LEARNED' else []
    rates = [x[0] for x in rates if x is not None]
    kw, spread = power.get('AC'), prices.get('spread')
    if not rates or kw is None or spread is None:
        return {'status': 'LEARNING', 'ac_on_peak_hours': round(ac_h, 2)}
    drift = sum(rates) / len(rates)
    avoided_h = ac_h if drift <= 0 else min(ac_h, precool_f / drift)
    if any(r.get('bank_kwh') is None for r in peak):
        return {'status': 'BANK_UNKNOWN', 'ac_on_peak_hours': round(ac_h, 2)}
    banked = any(r['bank_kwh'] > 0 for r in peak)
    value = 0. if banked else avoided_h * kw * spread
    return {'status': 'BANK_COVERS' if banked else 'ESTIMATED', 'ac_on_peak_hours': round(ac_h, 2),
            'avoided_hours': round(avoided_h, 2), 'kwh': round(avoided_h * kw, 2), 'value': round(value, 2),
            'assumption': f'pre-cool {precool_f:.0f} °F before 12:00; drift {drift:.2f} °F/h on-peak',
            'evidence_class': 'SIMULATED'}


def finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
