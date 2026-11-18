"""EV tab (0.1.8; inputs hardened 0.1.9): an EV job builder, the cheapest window and banked-credit use. Advice only.

Owner pop-ups 2026-11-15 "all of them, your preference on how the priority should go" (AI family plan §6 step 5) and
"Bind it by settings delivery". Design: docs/validation/AI_FAMILY_LEFTOVERS_DESIGN_REVIEW_2026-11-15.md §7 B.

Hard rules (family plan §3 and §7):
* Nothing here wakes, charges or schedules a car. This module makes no request of any kind: it reads the states the
  runtime already holds and returns words and numbers. The car's own schedule keeps control.
* Trips are owner-entered only (the page's form). They stay in this App's own store and nothing about them is put
  in the exported status sensor: even the word "plan ready" would tell a reader the house empties within a day.
* The Bolt stays on its owner-attended hold, so no comparison between cars is made.
* Every number says how good its data is. A missing, stale or unbound reading is "not known", never a default.
* A learned number is called PROVEN only after it beats the naive baseline on later natural charging sessions.

0.1.9 (#1135; docs/validation/ENERGY_AI_0_1_9_DESIGN_REVIEW_2026-11-18.md, owner pop-up "Build all of the above"):
* Learning reads only a declared wall-side meter (`energy` plus `energy_evidence`) that is kWh and total_increasing.
  The car's battery-side counter (charge_energy_added, state_class total) and Sense's EV fragments are never meters.
* A counter that drops during a session discards that session; two drops, or a learned value implying more than
  100 % charging efficiency, make the meter SUSPECT and the naive value is used.
* The charging signal is the EV adapter's own `charging` (else telemetry's), checked for entity id and age.
* Learning OFF is said on the page with its reason; `features()` reports LIVE / INERT / SUSPECT with a since time.
"""
from datetime import datetime
import math
import statistics

from .contracts import timestamp
from .planning import TZ, BankObjective, Job, period, schedule

JOB_KEY = 'ev:job'
SESSION_KEY = 'ev:sessions'
SHOWN_KEY = 'ev:shown'
ARCHIVE_KEY = 'ev:sessions-archive'               # 0.1.9: books from another meter or rule, kept, never deleted
FEATURES_KEY = 'ev:features'                      # 0.1.9: first time each feature entered its current state
RULE = '0.1.9'
CAR_KEY = 'ev:car'                                # battery size and charger power: car facts, kept between trips
ADAPTER_FIELDS = frozenset({'soc', 'plugged'})
ADAPTER_OPTIONAL = frozenset({'charging', 'energy', 'energy_evidence'})   # energy is a wall meter, with evidence
NOT_ENTITIES = frozenset({'energy_evidence'})
BATTERY_SIDE_MARKERS = ('charge_energy_added',)  # battery-side: charging losses invisible, restarts each session
CHARGING_ON_MAX_AGE_S = 24 * 3600                 # an "on" older than this is not believed (a session is at most 8 h)
MAX_EFFICIENCY = 1.0                              # more wall-to-battery than 100 % is impossible: the meter is wrong
SUSPECT_DROPS = 2                                 # counter drops during sessions before the meter is SUSPECT
DROP_TOLERANCE_KWH = 0.05                         # a smaller dip is meter noise, not a reset (run 5)
SOC_FRESH_S = 900                                 # GOOD up to here
SOC_LAST_KNOWN_S = 24 * 3600                      # LAST_KNOWN up to here (a sleeping car reports nothing new)
SOC_MAX_AGE_S = 72 * 3600                         # OLD up to here, planned with extra time; beyond it "not known"
READY_MARGIN_SLOTS = 1                            # the plan ends at least 30 minutes before the departure
TAPER_ABOVE_PERCENT = 90.                         # charging slows near full: one more half hour above this target
RATE_BOUNDS = (0.1, 2.5)                          # plausible wall kWh per 1 % for any car
RATE_AROUND_NAIVE = (0.5, 2.0)                    # and, with the battery size known, this share of the naive value
MAX_SLOTS = 16                                    # 8 hours, the scheduler's bound for one job
NAIVE_EFFICIENCY = 0.90                           # wall-to-battery share used only by the naive baseline
MIN_SESSION_PERCENT = 5.                          # a smaller session is too noisy to learn from
MIN_PRIOR_SESSIONS = 3                            # before the learned value is even scored
MIN_SCORED = 5                                    # scored sessions before PROVEN can be said
LOSS_STREAK = 3                                   # learned loses this many in a row: back to the naive value
KEEP_SESSIONS = 60
UNCERTAINTY_DOLLARS = 0.10
BOLT = 'NOT_COMPARED_OWNER_ATTENDED_HOLD'
NOT_KNOWN = 'not known'


def _number(value, low, high):
    if type(value) not in (int, float) or isinstance(value, bool) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError('EV_JOB_INVALID')
    return float(value)


def set_job(store, payload, now):
    """The owner's trip, entered on the page. {"clear": true} removes it. Nothing is sent anywhere."""
    if not isinstance(payload, dict):
        raise ValueError('EV_JOB_INVALID')
    if set(payload) == {'clear'}:
        if payload['clear'] is not True:
            raise ValueError('EV_JOB_INVALID')
        store.put(JOB_KEY, None)
        return {'ev_job': None}
    if set(payload) != {'depart', 'target_percent', 'battery_kwh', 'kwh', 'charge_kw'}:
        raise ValueError('EV_JOB_FIELDS')
    if not isinstance(payload['depart'], str) or len(payload['depart']) != 16:
        raise ValueError('EV_JOB_DEPART')
    try:
        depart = datetime.strptime(payload['depart'], '%Y-%m-%dT%H:%M').replace(tzinfo=TZ).timestamp()
    except ValueError:
        raise ValueError('EV_JOB_DEPART') from None
    if not now < depart <= now + 14 * 86400:
        raise ValueError('EV_JOB_DEPART')
    direct, target = payload['kwh'], payload['target_percent']
    if (direct is None) == (target is None):
        raise ValueError('EV_JOB_TARGET_OR_KWH')          # exactly one of them
    job = {'depart': depart, 'entered_at': now, 'charge_kw': _number(payload['charge_kw'], 1, 20),
           'kwh': None if direct is None else _number(direct, 1, 150),
           'target_percent': None if target is None else _number(target, 20, 100),
           'battery_kwh': None if target is None else _number(payload['battery_kwh'], 10, 250)}
    if direct is not None and payload['battery_kwh'] is not None:
        job['battery_kwh'] = _number(payload['battery_kwh'], 10, 250)
    car = dict(store.get(CAR_KEY) or {})
    car['charge_kw'] = job['charge_kw']
    if job.get('battery_kwh'):
        car['battery_kwh'] = job['battery_kwh']
    store.put(CAR_KEY, car)
    store.put(JOB_KEY, job)
    return {'ev_job': job}


def _age(row, now):
    try:
        age = now - timestamp(row.get('last_reported') or row['last_updated'])
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    return age if age >= -60 else None


def observe(states, now, ids):
    """Charge level and plug state as Home Assistant already holds them. Never a request to the car."""
    if not ids:
        return {'status': 'NOT_BOUND', 'soc_percent': None, 'quality': NOT_KNOWN, 'plugged': None,
                'reason': 'EV_BINDINGS_REQUIRED'}
    out = {'status': 'OBSERVED', 'soc_percent': None, 'soc_age_s': None, 'quality': NOT_KNOWN, 'plugged': None,
           'physical_authority': False}
    row = states.get(ids['soc'])
    if isinstance(row, dict) and row.get('entity_id') == ids['soc']:
        age = _age(row, now)
        try:
            level = float(row.get('state'))
        except (TypeError, ValueError):
            level = None
        unit = (row.get('attributes') or {}).get('unit_of_measurement')
        if level is not None and math.isfinite(level) and 0 <= level <= 100 and unit == '%' and age is not None:
            out['soc_age_s'] = int(max(age, 0))
            if age <= SOC_MAX_AGE_S:
                out['soc_percent'] = level
                out['quality'] = 'GOOD' if age <= SOC_FRESH_S else 'LAST_KNOWN' if age <= SOC_LAST_KNOWN_S else 'OLD'
            else:
                out['reason'] = 'SOC_OLDER_THAN_72H'
        else:
            out['reason'] = 'SOC_UNREADABLE'
    else:
        out['reason'] = 'SOC_MISSING'
    plug = states.get(ids['plugged'])
    if isinstance(plug, dict) and plug.get('entity_id') == ids['plugged'] and plug.get('state') in ('on', 'off'):
        out['plugged'] = plug['state'] == 'on'
    return out


# -- learning from natural charging sessions ----------------------------------------------------------------------
def meter(states, ids):
    """(kWh, None) from a bound wall meter, or (None, reason). A wrong kind is METER_KIND_*, an absent reading
    METER_MISSING / METER_UNREADABLE (a gap, not a fault). Only kWh + total_increasing is a meter here."""
    entity = (ids or {}).get('energy')
    if not entity:
        return None, 'NO_WALL_METER'
    row = states.get(entity)
    if not isinstance(row, dict) or row.get('entity_id') != entity:
        return None, 'METER_MISSING'
    attributes = row.get('attributes') or {}
    if attributes.get('unit_of_measurement') != 'kWh':
        return None, 'METER_KIND_UNIT'
    if attributes.get('state_class') != 'total_increasing':
        return None, 'METER_KIND_STATE_CLASS'
    try:
        value = float(row['state'])
    except (KeyError, TypeError, ValueError):
        return None, 'METER_UNREADABLE'
    return (value, None) if math.isfinite(value) and value >= 0 else (None, 'METER_UNREADABLE')


def charging_state(states, now, ids, telemetry):
    """(True/False/None, source, reason). The EV adapter's own signal first, then telemetry's. "on" is believed only
    when the row is this entity and its report is at most a day old; "off" at any valid age."""
    entity, source = (ids or {}).get('charging'), 'ev'
    if not entity:
        entity, source = (telemetry or {}).get('charging'), 'telemetry'
    if not entity:
        return None, None, 'NO_CHARGING_SIGNAL'
    row = states.get(entity)
    if not isinstance(row, dict) or row.get('entity_id') != entity:
        return None, source, 'CHARGING_MISSING'
    age = _age(row, now)
    if age is None or row.get('state') not in ('on', 'off'):
        return None, source, 'CHARGING_UNREADABLE'
    if row['state'] == 'on' and age > CHARGING_ON_MAX_AGE_S:
        return None, source, 'CHARGING_ON_TOO_OLD'
    return row['state'] == 'on', source, None


def naive_kwh_per_percent(battery_kwh):
    return battery_kwh / 100. / NAIVE_EFFICIENCY


def learned(book):
    """Median wall kWh per 1 % over the kept sessions, or None while there are too few."""
    rates = [s['kwh_per_percent'] for s in book.get('sessions', [])]
    return statistics.median(rates) if len(rates) >= MIN_PRIOR_SESSIONS else None


def efficiency(book, battery_kwh):
    """Wall-to-battery share the learned value implies, or None. Above MAX_EFFICIENCY the meter cannot be the wall."""
    value = learned(book)
    return battery_kwh / 100. / value if value and battery_kwh else None


def standing(book, battery_kwh=None):
    """PROVEN only when the learned value beat the naive one on at least MIN_SCORED later sessions and has not just
    lost LOSS_STREAK in a row (then the naive value is used again). A meter that drops, or a learned value that
    implies more than 100 % efficiency, is SUSPECT and never PROVEN (0.1.9)."""
    implied = efficiency(book, battery_kwh)
    if book.get('counter_drops', 0) >= SUSPECT_DROPS:
        return {'status': 'SUSPECT', 'reason': 'COUNTER_DECREASED', 'counter_drops': book['counter_drops']}
    if implied is not None and implied > MAX_EFFICIENCY:
        return {'status': 'SUSPECT', 'reason': 'ABOVE_100_PERCENT_EFFICIENCY', 'implied_efficiency': round(implied, 3)}
    scored = [s for s in book.get('sessions', []) if s.get('learned_error_kwh') is not None]
    if len(scored) < MIN_SCORED:
        return {'status': 'NOT_PROVEN', 'scored': len(scored), 'needed': MIN_SCORED}
    mae_l = statistics.fmean(s['learned_error_kwh'] for s in scored)
    mae_n = statistics.fmean(s['naive_error_kwh'] for s in scored)
    recent = scored[-LOSS_STREAK:]
    rolled_back = all(s['learned_error_kwh'] > s['naive_error_kwh'] for s in recent)      # a tie is not a loss
    proven = mae_l < mae_n and not rolled_back
    return {'status': 'PROVEN' if proven else 'ROLLED_BACK' if rolled_back else 'NOT_PROVEN', 'scored': len(scored),
            'learned_mae_kwh': round(mae_l, 3), 'naive_mae_kwh': round(mae_n, 3)}


def plausible(rate, battery_kwh):
    """A session whose kWh per 1 % cannot be this car's is not learned from (a counter that reset mid-session, a
    second load on the same meter). It is counted, never silently used."""
    if not RATE_BOUNDS[0] <= rate <= RATE_BOUNDS[1]:
        return False
    if battery_kwh:
        naive = naive_kwh_per_percent(battery_kwh)
        return RATE_AROUND_NAIVE[0] * naive <= rate <= RATE_AROUND_NAIVE[1] * naive
    return True


def track(store, view, charging, energy_kwh, battery_kwh, now):
    """One natural session = charging on, then off. Scored before it is learned from, so the score is out of sample."""
    book = store.get(SESSION_KEY, {'open': None, 'sessions': []})
    book.setdefault('rejected', 0)
    book.setdefault('counter_drops', 0)
    soc = view.get('soc_percent')
    opened = book.get('open')
    last = book.get('last_energy_kwh')
    if energy_kwh is not None:
        if last is not None and last - DROP_TOLERANCE_KWH <= energy_kwh < last:
            energy_kwh = last                             # a few watt-hours of wobble: keep the high-water mark
        if opened is not None and last is not None and energy_kwh < last:
            book['open'] = opened = None                  # the counter went back: this session cannot be measured
            book['counter_drops'] += 1
        book['last_energy_kwh'] = energy_kwh
    if charging is True and opened is None and soc is not None and energy_kwh is not None:
        book['open'] = {'start': now, 'soc': soc, 'energy_kwh': energy_kwh}
    elif charging is False and opened is not None:
        book['open'] = None
        if soc is not None and energy_kwh is not None:
            gained, wall = soc - opened['soc'], energy_kwh - opened['energy_kwh']
            if gained >= MIN_SESSION_PERCENT and 0 < wall <= 200 and not plausible(wall / gained, battery_kwh):
                book['rejected'] += 1
            elif gained >= MIN_SESSION_PERCENT and 0 < wall <= 200:
                prior = learned(book)
                row = {'start': opened['start'], 'end': now, 'percent': round(gained, 1), 'wall_kwh': round(wall, 3),
                       'kwh_per_percent': round(wall / gained, 4), 'learned_error_kwh': None, 'naive_error_kwh': None}
                if prior is not None and battery_kwh:
                    row['learned_error_kwh'] = round(abs(prior * gained - wall), 3)
                    row['naive_error_kwh'] = round(abs(naive_kwh_per_percent(battery_kwh) * gained - wall), 3)
                book['sessions'] = (book['sessions'] + [row])[-KEEP_SESSIONS:]
    elif charging is None and opened is not None and now - opened['start'] > 2 * 86400:
        book['open'] = None                               # an unreadable session is dropped, never guessed
    store.put(SESSION_KEY, book)
    return book


# -- the plan ------------------------------------------------------------------------------------------------------
def _objective(bill_packet, tariff, now, times):
    bills = (bill_packet or {}).get('bills') if isinstance(bill_packet, dict) else None
    if not bills:
        raise LookupError('RETAINED_STATEMENTS_MISSING')
    local = datetime.fromtimestamp(now, TZ)
    end = local.replace(day=2, hour=0, minute=0, second=0, microsecond=0)
    if local >= end:
        end = end.replace(year=end.year + (end.month == 12), month=end.month % 12 + 1)
    if times[-1] + 1800 > end.timestamp():
        raise LookupError('PLAN_CROSSES_BILLING_CYCLE')
    last = sorted(bills, key=lambda b: b['billing_period'])[-1]
    bank = {p: float(last[p]['net_balance_kwh']) for p in ('on_peak', 'off_peak')}
    payout = float((((bill_packet.get('owner_parameters') or {}).get('true_up_payout_rate_per_kwh') or {})
                    .get('value')) or 0.)
    rates = tariff['charges']['energy_per_kwh']['summer' if 5 <= end.month <= 10 else 'winter']
    return BankObjective(tariff, end.month, bank, {p: min(payout, rates[p]) for p in rates},
                         'retained statements ' + last['billing_period'] + ' + owner-entered EV trip'), bank


def _blocked(reason, **extra):
    return {'status': 'BLOCKED', 'reasons': [reason], 'physical_actions': False, **extra}


def plan(job, view, per_percent, forecasts, bill_packet, tariff, now):
    """The cheapest continuous window that finishes by the owner's deadline, or a plain reason why not. Advice only."""
    if not job:
        return _blocked('NO_TRIP_ENTERED')
    if job['depart'] <= now:
        return _blocked('DEPARTURE_PASSED')
    if job['kwh'] is not None:
        need, basis = job['kwh'], 'OWNER_ENTERED_KWH'
    else:
        if view.get('soc_percent') is None:
            return _blocked('CHARGE_LEVEL_NOT_KNOWN', detail=view.get('reason'))
        gap = job['target_percent'] - view['soc_percent']
        if gap <= 0:
            return {'status': 'NOTHING_NEEDED', 'physical_actions': False,
                    'soc_percent': view['soc_percent'], 'target_percent': job['target_percent']}
        need, basis = gap * per_percent['value'], per_percent['basis']
    slots = math.ceil(need / job['charge_kw'] * 2 - 1e-9)
    # Being late is the one failure that matters here, so the plan always ends early: 30 minutes, plus 30 more when
    # the target is near full (charging slows there) and 30 more when the charge level is an old reading.
    margin, allowances = READY_MARGIN_SLOTS, ['READY_30_MIN_EARLY']
    if job['kwh'] is None and job['target_percent'] > TAPER_ABOVE_PERCENT:
        margin, allowances = margin + 1, allowances + ['SLOWER_NEAR_FULL']
    if job['kwh'] is None and view.get('quality') == 'OLD':
        margin, allowances = margin + 1, allowances + ['OLD_CHARGE_LEVEL']
    if slots > MAX_SLOTS:
        return _blocked('NEEDS_MORE_THAN_8_HOURS', need_kwh=round(need, 1))
    if not all(forecasts.get(r) for r in ('load', 'solar')):
        return _blocked('LOAD_OR_SOLAR_FORECAST_MISSING')
    times = [r['at'] for r in forecasts['load']]
    n = len(times)
    if job['depart'] > times[-1] + 1800:
        return _blocked('DEPARTURE_BEYOND_24_HOURS', need_kwh=round(need, 1), hours=slots / 2)
    deadline = sum(1 for t in times if t + 1800 <= job['depart'] - margin * 1800)
    latest_start = job['depart'] - (slots + margin) * 1800
    if deadline < slots:
        return _blocked('CANNOT_FINISH_BY_DEPARTURE', need_kwh=round(need, 1), hours=slots / 2)
    try:
        objective, bank = _objective(bill_packet, tariff, now, times)
        ev = Job(id='ev', domain='shadow', target='owner-entered-trip', watts=job['charge_kw'] * 1000., slots=slots,
                 earliest=0, deadline=deadline, original_start=0, allowed=tuple([True] * n))
        result = schedule(times, [r['watts'] for r in forecasts['load']], [r['watts'] for r in forecasts['solar']],
                          [ev], objective, uncertainty_dollars=UNCERTAINTY_DOLLARS)
    except LookupError as error:
        return _blocked(str(error))
    except (ValueError, KeyError, TypeError):
        return _blocked('PLANNING_CONFIGURATION_INVALID')
    start = times[result['starts']['ev']]
    periods = sorted({period(t) for t in times[result['starts']['ev']:result['starts']['ev'] + slots]})
    why = ('Starting as soon as it is plugged in is as cheap as any later window.' if result['no_action'] else
           'This window costs the least for this billing cycle, counting the banked credit in each rate period.')
    if 'on_peak' in periods and bank['on_peak'] > 0:
        why += ' Part of it is on-peak because unused on-peak credit is paid out at a lower rate than it is worth.'
    return {'status': 'SHADOW_PROPOSAL', 'physical_actions': False, 'commissioned': False,
            'need_kwh': round(need, 1), 'need_basis': basis, 'hours': slots / 2, 'charge_kw': job['charge_kw'],
            'start': start, 'end': start + slots * 1800, 'depart': job['depart'], 'periods': periods,
            'plug_in_by': int(latest_start), 'plugged': view.get('plugged'), 'allowances': allowances,
            'modeled_benefit': round(result['modeled_benefit'], 2), 'why': why,
            'load_forecast_note': 'ESTIMATED: the load forecast may already include usual charging',
            'classification': 'ESTIMATED', 'caveat': result['caveat']}


def book_tag(ids):
    """Which meter and rule a session book was learned under. A book under another tag is archived, not reused."""
    return {'meter': (ids or {}).get('energy'), 'rule': RULE}


def session_book(store, ids, now):
    """The session book for the bound meter. One from 0.1.8, or from another meter, moves to the archive (kept,
    never deleted: DATA_RETENTION_STANDARD) and learning starts again from nothing."""
    book = store.get(SESSION_KEY)
    tag = book_tag(ids)
    if isinstance(book, dict) and book.get('tag') == tag:
        return book
    if isinstance(book, dict) and (book.get('sessions') or book.get('open') or book.get('rejected')):
        store.put(ARCHIVE_KEY, (store.get(ARCHIVE_KEY) or []) + [{'archived_at': now, 'book': book}])
    book = {'open': None, 'sessions': [], 'tag': tag}
    store.put(SESSION_KEY, book)
    return book


def learning(ids, signal_source, meter_reason, stand):
    """ON, or OFF with the one reason the page shows. Configuration first, then the meter's kind, then its record."""
    if not ids:
        return 'OFF', 'NOT_BOUND'
    if signal_source is None:
        return 'OFF', 'NO_CHARGING_SIGNAL'
    if meter_reason in ('NO_WALL_METER', 'METER_KIND_UNIT', 'METER_KIND_STATE_CLASS'):
        return 'OFF', meter_reason
    if stand['status'] == 'SUSPECT':
        return 'OFF', stand['reason']
    return 'ON', None


def _feature(store, name, status, now, **detail):
    seen = store.get(FEATURES_KEY) or {}
    if (seen.get(name) or {}).get('status') != status:
        seen[name] = {'status': status, 'since': now}
        store.put(FEATURES_KEY, seen)
    return {'status': status, 'since': seen[name]['since'], **detail}


def features(store, now, view, signal, meter_reason, learn):
    """Inputs-ready manifest (run 3 §6): each EV feature is LIVE, INERT (an input is missing) or SUSPECT (an input
    is the wrong kind). Reports only; it changes nothing."""
    missing = [k for k, absent in (('soc', view.get('soc_percent') is None), ('plugged', view.get('plugged') is None))
               if absent]
    plan = _feature(store, 'ev_plan', 'INERT' if 'soc' in missing else 'LIVE', now, missing=missing)
    state, reason = learn
    value, source, signal_reason = signal
    if state == 'OFF' and reason in ('METER_KIND_UNIT', 'METER_KIND_STATE_CLASS', 'COUNTER_DECREASED',
                                     'ABOVE_100_PERCENT_EFFICIENCY'):
        learner = _feature(store, 'ev_learning', 'SUSPECT', now, kind=reason, charging_source=source)
    else:
        gaps = ([reason] if state == 'OFF' else []) + [r for r in (signal_reason, meter_reason)
                                                         if r and r not in ('NO_WALL_METER', 'NO_CHARGING_SIGNAL')]
        learner = _feature(store, 'ev_learning', 'INERT' if gaps else 'LIVE', now, missing=gaps,
                           charging_source=source)
    return {'ev_plan': plan, 'ev_learning': learner}


def evaluate(store, states, now, ev_ids, telemetry, forecasts, bill_packet, tariff):
    """The EV tab's whole view for one cycle. Reads only; the one write is this App's own store."""
    view = observe(states, now, ev_ids)
    job = store.get(JOB_KEY)
    if job and job['depart'] <= now:
        store.put(JOB_KEY, None)                          # a past trip is forgotten; trips are never kept as history
        job = None
    car = store.get(CAR_KEY) or {}
    battery = (job or {}).get('battery_kwh') or car.get('battery_kwh')
    signal = charging_state(states, now, ev_ids, telemetry)
    energy, meter_reason = meter(states, ev_ids)
    if ev_ids:
        session_book(store, ev_ids, now)
        book = track(store, view, signal[0], energy, battery, now)
    else:
        book = store.get(SESSION_KEY) or {'open': None, 'sessions': []}
    stand, value = standing(book, battery), learned(book)
    learn = learning(ev_ids, signal[1], meter_reason, stand)
    if learn[0] == 'ON' and stand['status'] == 'PROVEN' and value is not None:
        per = {'value': value, 'basis': 'LEARNED_PROVEN'}
    elif battery:
        per = {'value': naive_kwh_per_percent(battery), 'basis': 'ESTIMATE_NOT_PROVEN'}
    else:
        per = {'value': None, 'basis': NOT_KNOWN}
    result = plan(job, view, per, forecasts, bill_packet, tariff, now)
    shown = store.get(SHOWN_KEY, 0)
    if result['status'] == 'SHADOW_PROPOSAL' and job and store.get('ev:shown-for') != job['entered_at']:
        shown += 1
        store.put(SHOWN_KEY, shown)
        store.put('ev:shown-for', job['entered_at'])
    return {'vehicle': view, 'trip': None if not job else {'depart': job['depart'], 'kwh': job['kwh'],
                                                           'target_percent': job['target_percent'],
                                                           'charge_kw': job['charge_kw']},
            'car': {'battery_kwh': car.get('battery_kwh'), 'charge_kw': car.get('charge_kw')},
            'plan': result, 'kwh_per_percent': {**per, 'learned': None if value is None else round(value, 4),
                                                'sessions': len(book.get('sessions', [])),
                                                'rejected': book.get('rejected', 0),
                                                'counter_drops': book.get('counter_drops', 0), **stand,
                                                'learning': learn[0], 'learning_reason': learn[1]},
            'charging': {'state': signal[0], 'source': signal[1], 'reason': signal[2]},
            'features': features(store, now, view, signal, meter_reason, learn),
            'scorecard': {'plans_shown': shown, 'carried_out_by_this_app': 0, 'savings_claimed': None},
            'bolt': BOLT, 'never': 'no wake, no charge command, no schedule write', 'physical_actions': False}
