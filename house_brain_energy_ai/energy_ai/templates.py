"""Flexible-load templates and the in-App planning context (0.1.5). Shadow only: nothing here can switch a load.

Owner pop-ups 2026-10-25: plan around EV charging, the pool pump, dryer and dishwasher "and any other"; "learn from
Sense and the new Sense AI": a template is learned only from a bound Sense detection device's own cycles; the identity
comes from the Sense AI lane's register when confirmed, otherwise it says so; nothing plans until the owner approves the
template on this App's page. A planning context is valid for one half-hour, so it is built here every cycle instead of
being delivered (a delivered packet could never stay current).
"""
from datetime import datetime, timedelta
import hashlib
import math
import statistics
from .planning import TZ, BankObjective, Job, schedule

TEMPLATE_KEY = 'plan:templates'
MIN_RUNS = 3                 # cycles in the 7 days of kept samples before a template is proposed
DRIFT_SHARE = 0.3            # an approved template whose live watts or length drift more than this needs review
MAX_SLOTS = 16               # 8 hours
MAX_JOBS = 4                 # the scheduler's limit
UNCERTAINTY_DOLLARS = 0.10   # a proposal must beat this to differ from the usual time


def _local(at):
    return datetime.fromtimestamp(at, TZ)


def _dominant(clusters):
    """The cluster with the most observed energy; ties go to the larger one."""
    good = [c for c in clusters or [] if c.get('count', 0) >= MIN_RUNS and c.get('watts', 0) > 0]
    return max(good, key=lambda c: (c['watts'] * c['duration_s'] * c['count'], c['watts']), default=None)


def propose(detections, detection_events, roles):
    """Template proposals: role -> watts, half-hour slots, usual start hour, runs in 7 days, identity source."""
    out = {}
    for entity, info in (detections or {}).items():
        role = roles.get(entity)
        cluster = _dominant(info.get('clusters'))
        if role is None or cluster is None:
            continue
        events = [e for e in (detection_events.get(entity) or {}).get('events', [])
                  if abs(e['watts'] - cluster['watts']) <= max(50, cluster['watts'] * .2)]
        if len(events) < MIN_RUNS:
            continue
        confirmed = info.get('identity_classification') == 'USER-CONFIRMED'
        out[role] = {'id': role, 'watts': round(cluster['watts']),
                     'slots': min(MAX_SLOTS, max(1, math.ceil(cluster['duration_s'] / 1800))),
                     'usual_start_hour': int(statistics.median(_local(e['start']).hour for e in events)),
                     'runs_7d': len(events),
                     'identity': info.get('identity') if confirmed else None,
                     'identity_source': 'SENSE_AI_CONFIRMED' if confirmed else 'SENSE_DEVICE_NAME_ONLY'}
    return out


def approve(store, proposals, template_id, approved, at):
    """Owner decision from the page. Approval copies the proposal as it is now; a refusal removes it."""
    if type(approved) is not bool or not isinstance(template_id, str):
        raise ValueError('TEMPLATE_DECISION_INVALID')
    saved = store.get(TEMPLATE_KEY, {})
    if approved:
        if template_id not in proposals:
            raise ValueError('TEMPLATE_NOT_PROPOSED')
        saved[template_id] = {**proposals[template_id], 'approved_at': at}
    else:
        saved.pop(template_id, None)
    store.put(TEMPLATE_KEY, saved)
    return {'template': template_id, 'approved': approved, 'approved_count': len(saved)}


def review(store, proposals):
    """Approved templates still matching their device, and the ones that drifted (need a new approval)."""
    active, drifted = {}, []
    for tid, t in store.get(TEMPLATE_KEY, {}).items():
        now_t = proposals.get(tid)
        if now_t and (abs(now_t['watts'] - t['watts']) > DRIFT_SHARE * t['watts']
                      or abs(now_t['slots'] - t['slots']) > max(1, DRIFT_SHARE * t['slots'])):
            drifted.append(tid)
        else:
            active[tid] = t
    return active, sorted(drifted)


def cycle_end(now):
    """Estimated end of the current APS billing cycle (statements end on the 2nd); ESTIMATED, local midnight."""
    local = _local(now)
    first = local.replace(day=2, hour=0, minute=0, second=0, microsecond=0)
    if local >= first:
        first = (first.replace(day=1) + timedelta(days=32)).replace(day=2)
    return first.timestamp(), first.month


def build_jobs(templates, times):
    """One hypothetical run per approved template in the 24 h horizon, at its usual hour ("if it runs today")."""
    jobs, skipped = [], []
    n = len(times)
    ranked = sorted(templates.values(), key=lambda t: -t['watts'] * t['slots'])
    for t in ranked[:MAX_JOBS]:
        usual = next((k for k, at in enumerate(times) if _local(at).hour == t['usual_start_hour']
                      and _local(at).minute == 0 and k + t['slots'] <= n), None)
        if usual is None:
            skipped.append(t['id'])
            continue
        jobs.append(Job(id=t['id'], domain='shadow', target=hashlib.sha256(t['id'].encode()).hexdigest()[:12],
                        watts=float(t['watts']), slots=t['slots'], earliest=0, deadline=n,
                        original_start=usual, allowed=tuple([True] * n)))
    return jobs, skipped + [t['id'] for t in ranked[MAX_JOBS:]]


def base_without(load_w, times, jobs):
    """The load forecast learned from history already contains each template's usual run; take it out there."""
    base = list(load_w)
    for j in jobs:
        for k in range(j.original_start, j.original_start + j.slots):
            base[k] = max(0., base[k] - j.watts)
    return base


def plan(templates, forecasts, bill_packet, tariff, now):
    """Shadow proposal from approved templates, or a BLOCKED reason. Never a command."""
    if not templates:
        return {'status': 'BLOCKED', 'reasons': ['NO_APPROVED_LOAD_TEMPLATES']}
    if not all(forecasts.get(r) for r in ('load', 'solar')):
        return {'status': 'BLOCKED', 'reasons': ['LOAD_OR_SOLAR_FORECAST_MISSING']}
    bills = (bill_packet or {}).get('bills') if isinstance(bill_packet, dict) else None
    if not bills:
        return {'status': 'BLOCKED', 'reasons': ['RETAINED_STATEMENTS_MISSING']}
    times = [r['at'] for r in forecasts['load']]
    end, month = cycle_end(now)
    if times[-1] + 1800 > end:
        return {'status': 'BLOCKED', 'reasons': ['PLAN_CROSSES_BILLING_CYCLE']}
    last = sorted(bills, key=lambda b: b['billing_period'])[-1]
    try:
        bank = {p: float(last[p]['net_balance_kwh']) for p in ('on_peak', 'off_peak')}
        params = bill_packet.get('owner_parameters') or {}
        payout = float((params.get('true_up_payout_rate_per_kwh') or {}).get('value') or 0.)
        rates = tariff['charges']['energy_per_kwh']['summer' if 5 <= month <= 10 else 'winter']
        objective = BankObjective(tariff, month, bank, {p: min(payout, rates[p]) for p in rates},
                                  'retained statements ' + last['billing_period'] + ' + learned templates')
        jobs, skipped = build_jobs(templates, times)
        if not jobs:
            return {'status': 'BLOCKED', 'reasons': ['NO_TEMPLATE_RUN_IN_HORIZON'], 'skipped': skipped}
        result = schedule(times, base_without([r['watts'] for r in forecasts['load']], times, jobs),
                          [r['watts'] for r in forecasts['solar']], jobs, objective,
                          uncertainty_dollars=UNCERTAINTY_DOLLARS)
    except (ValueError, KeyError, TypeError) as exc:
        return {'status': 'BLOCKED', 'reasons': ['PLANNING_CONFIGURATION_INVALID'], 'detail': str(exc)[:64]}
    moves = []
    for j in jobs:
        start = result['starts'][j.id]
        moves.append({'template': j.id, 'usual_start': times[j.original_start], 'proposed_start': times[start],
                      'moved': start != j.original_start, 'kw': round(j.watts / 1000, 2), 'hours': j.slots / 2})
    return {'status': 'SHADOW_PROPOSAL', 'source': 'LEARNED_TEMPLATES', 'result': result, 'moves': moves,
            'skipped': skipped, 'expires': min(now + 1800, end), 'bank_kwh': bank, 'billing_month': month,
            'base_excludes_flexible_loads': 'ESTIMATED: each template removed at its usual time',
            'physical_actions': False, 'commissioned': False}
