"""Owner-facing read-only insights from data the app already holds. No new request, alert or control.

* always_on: overnight baseline per local day, month-over-month change and yearly cost at the plan's rates;
* bill_projection: current calendar month's base cost so far plus a projected band, with EPR-6 bucket netting;
* cycle_receipts: time-of-use cost of recent completed cycles on detection bindings (identity stays as reviewed);
* cleaning_value: solar output against the frozen existing forecast; a drop from the recent best is a soiling hint;
* outage_budget: overnight load as a planning number for the Preparedness lane;
* explain_bills: one deterministic paragraph per statement against the same period a year earlier;
* forecast_calibration: an 80% interval learned on older scored hours, checked on newer ones.
Every figure is an estimate on base charges; anything the data cannot attribute stays "unknown".
"""
from calendar import monthrange
from datetime import datetime, timedelta
import statistics
from .planning import TZ, holiday, period
from .rateplan import season

ALWAYS_ON_KEY = 'insights:always-on-floor'   # 0.1.4: the floor, not the 1-5 am median (old key kept, unread)
KEEP_ALWAYS_ON_DAYS = 400
MIN_PROJECTION_DAYS = 7
MAX_RECEIPTS = 10
MAX_RECEIPT_BINDINGS = 20
MIN_CLEANING_DAYS = 21
MIN_CALIBRATION_HOURS = 48
UNKNOWN = 'unknown'


def _local(at):
    return datetime.fromtimestamp(at, TZ)


def _rate(tariff, at):
    return tariff['charges']['energy_per_kwh'][season(_local(at).month)][period(at)]


def _quantile(values, q):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def always_on(store, now, base, tariff):
    """Store today's overnight median; compare complete months; price a year of that constant load."""
    days = store.get(ALWAYS_ON_KEY, {})
    if base.get('status') == 'ESTIMATED':
        days[_local(now).strftime('%Y-%m-%d')] = round(base.get('always_on_floor_w', base['overnight_median_w']), 1)
        for old in sorted(days)[:-KEEP_ALWAYS_ON_DAYS]:
            days.pop(old)
        store.put(ALWAYS_ON_KEY, days)
    if not days:
        return {'status': 'WAITING', 'missing': ['OVERNIGHT_BASELINE_NIGHTS']}
    months = {}
    for day, watts in days.items():
        months.setdefault(day[:7], []).append(watts)
    monthly = {m: round(statistics.median(v), 1) for m, v in sorted(months.items()) if len(v) >= 7}
    latest = days[max(days)]
    year_start = datetime(_local(now).year, 1, 1, tzinfo=TZ).timestamp()
    yearly_rate = sum(_rate(tariff, year_start + h * 3600) for h in range(8760))
    names = list(monthly)
    change = (round(monthly[names[-1]] - monthly[names[-2]], 1) if len(names) >= 2 else None)
    return {'status': 'ESTIMATED', 'latest_overnight_w': latest, 'monthly_median_w': dict(list(monthly.items())[-13:]),
            'month_over_month_change_w': change,
            'yearly_cost_dollars_at_latest': round(latest / 1000 * yearly_rate, 2),
            'basis': 'quietest 1-5 am hour of each night, median over nights; includes necessary always-on services; '
                     'base energy rates only',
            'physical_actions': False}


def bill_projection(import_rows, export_rows, tariff, opening_bank, now):
    """Calendar month to date plus a band for the rest; bucket netting against the bank, base charges only."""
    local = _local(now)
    month = local.strftime('%Y-%m')
    imports, exports = dict(import_rows or []), dict(export_rows or [])
    daily = {}
    for at in imports.keys() & exports.keys():
        if _local(at).strftime('%Y-%m') != month:
            continue
        day = daily.setdefault(_local(at).day, {'on_peak': 0., 'off_peak': 0.})
        day[period(at)] += (imports[at] - exports[at]) / 1000
    if len(daily) < MIN_PROJECTION_DAYS:
        return {'status': 'WAITING', 'missing': ['MONTH_TO_DATE_HOURLY_DAYS_BELOW_7'], 'days': len(daily),
                'recommendation': None}
    days_in_month = monthrange(local.year, local.month)[1]
    remaining = max(0, days_in_month - local.day)
    rates = tariff['charges']['energy_per_kwh'][season(local.month)]
    bank = {p: max(0., float((opening_bank or {}).get(p) or 0.)) for p in ('on_peak', 'off_peak')}

    def cost(extra):
        total = days_in_month * tariff['charges']['basic_service_charge_per_day']
        for bucket in ('on_peak', 'off_peak'):
            net_import = sum(d[bucket] for d in daily.values()) + extra[bucket] * remaining
            total += max(0., net_import - bank[bucket]) * rates[bucket]
        return round(total, 2)
    per_day = {b: [d[b] for d in daily.values()] for b in ('on_peak', 'off_peak')}
    return {'status': 'PROJECTED', 'month': month, 'days_observed': len(daily), 'days_remaining': remaining,
            'projected_dollars': cost({b: statistics.mean(v) for b, v in per_day.items()}),
            'band_dollars': {'low': cost({b: _quantile(v, .1) for b, v in per_day.items()}),
                             'high': cost({b: _quantile(v, .9) for b, v in per_day.items()})},
            'basis': 'calendar month approximates the billing cycle; base energy rates and basic service only; '
                     'adjustors, taxes and fees excluded', 'recommendation': None, 'physical_actions': False}


def cycle_receipts(detections, events_by_entity, tariff):
    """Cost of each recent completed cycle at its start time's rate; the label stays the reviewed identity."""
    out = {}
    for entity in sorted(events_by_entity)[:MAX_RECEIPT_BINDINGS]:
        events = events_by_entity[entity]['events'][-MAX_RECEIPTS:]
        identity = (detections.get(entity) or {}).get('identity', 'UNRESOLVED')
        out[entity] = {'identity': identity, 'cycles': [
            {'start': e['start'], 'kwh': round(e['watts'] * e['duration_s'] / 3.6e6, 3), 'period': period(e['start']),
             'cost_dollars': round(e['watts'] * e['duration_s'] / 3.6e6 * _rate(tariff, e['start']), 3)}
            for e in events]}
    return {'status': 'ESTIMATED' if out else 'NO_DETECTION_BINDINGS', 'bindings': out,
            'basis': 'median cycle watts x duration at the base energy rate of the start time; '
                     'banked credits are valued at the same retail rate', 'physical_actions': False}


def cleaning_value(store, tariff, now):
    """Daily actual/reference solar ratio; recent median against the best weekly median of 60 days."""
    rows = store.db.execute(
        'SELECT a.target,a.actual,r.reference FROM forecast_audit a JOIN forecast_reference r '
        'ON r.role=a.role AND r.target=a.target AND r.issued<r.target WHERE a.role=? AND a.actual IS NOT NULL '
        'AND a.target>=?', ('solar', now - 60 * 86400))
    days = {}
    for target, actual, reference in rows:
        if reference > 200:
            day = days.setdefault(_local(target).strftime('%Y-%m-%d'), [0., 0., 0])
            day[0] += actual
            day[1] += reference
            day[2] += 1
    ratios = [(d, a / r, a / 1000) for d, (a, r, n) in sorted(days.items()) if n >= 6 and r > 0]
    if len(ratios) < MIN_CLEANING_DAYS:
        return {'status': 'WAITING', 'missing': ['PAIRED_SOLAR_DAYS_BELOW_21'], 'days': len(ratios),
                'recommendation': None}
    weekly = [statistics.median(r for _, r, _ in ratios[i:i + 7]) for i in range(len(ratios) - 6)]
    best, recent = max(weekly), weekly[-1]
    loss = max(0., 1 - recent / best) if best > 0 else 0.
    daily_kwh = statistics.mean(k for _, _, k in ratios[-7:])
    rates = tariff['charges']['energy_per_kwh'][season(_local(now).month)]
    lost = loss * daily_kwh * 30
    return {'status': 'ESTIMATED', 'output_vs_best_week_percent': round(-loss * 100, 1),
            'monthly_value_dollars': {'low': round(lost * rates['off_peak'], 2), 'high': round(lost * rates['on_peak'], 2)},
            'basis': 'actual solar against the existing forecast, frozen before each hour; a drop can also be '
                     'shading, weather the forecast missed or equipment; Solar Health Watch stays the authority',
            'recommendation': None, 'physical_actions': False}


def outage_budget(load_hours):
    nights = [w for t, w in load_hours if 1 <= _local(t).hour < 5]
    if len(nights) < 12:
        return {'status': 'WAITING', 'missing': ['OVERNIGHT_LOAD_HOURS_BELOW_12']}
    p50, p90 = statistics.median(nights) / 1000, _quantile(nights, .9) / 1000
    return {'status': 'ESTIMATED', 'overnight_kwh_per_hour': {'typical': round(p50, 3), 'high': round(p90, 3)},
            'hours_per_10_kwh': {'typical': round(10 / p50, 1) if p50 > 0 else None,
                                 'conservative': round(10 / p90, 1) if p90 > 0 else None},
            'basis': 'whole-house overnight load, an upper bound for essential loads; planning number for the '
                     'Preparedness lane; no generator or battery control', 'physical_actions': False}


def explain_bills(bill_packet):
    """Same billing period a year earlier; the statements carry no weather, EV or appliance breakdown."""
    bills = (bill_packet or {}).get('bills') if isinstance(bill_packet, dict) else None
    if not isinstance(bills, list) or not bills:
        return {'status': 'NOT_AVAILABLE', 'reason': 'RETAINED_STATEMENTS_MISSING'}
    try:
        by_period = {b['billing_period']: b for b in bills}
        stories = []
        for label in sorted(by_period)[-12:]:
            prior_label = f'{int(label[:4]) - 1}{label[4:]}'
            if prior_label not in by_period:
                continue
            now, then = by_period[label], by_period[prior_label]
            total = lambda b, key: sum(float(b[p][key]) for p in ('on_peak', 'off_peak'))
            used, used_before = total(now, 'energy_use_kwh'), total(then, 'energy_use_kwh')
            earned, earned_before = total(now, 'credits_earned_kwh'), total(then, 'credits_earned_kwh')
            billed, billed_before = total(now, 'billed_kwh'), total(then, 'billed_kwh')
            stories.append({'billing_period': label, 'compared_with': prior_label, 'text': (
                f'{label}: grid use {used:.0f} kWh ({used - used_before:+.0f} vs {prior_label}); '
                f'export credits {earned:.0f} kWh ({earned - earned_before:+.0f}); billed {billed:.0f} kWh '
                f'({billed - billed_before:+.0f}). Weather, EV charging and always-on share: {UNKNOWN} '
                f'(statements carry no breakdown).')})
    except (KeyError, TypeError, ValueError):
        return {'status': 'NOT_AVAILABLE', 'reason': 'RETAINED_STATEMENTS_UNREADABLE'}
    return {'status': 'EXPLAINED' if stories else 'NO_PRIOR_YEAR_PERIOD', 'stories': stories,
            'basis': 'retained statements only; deterministic text, no language model', 'recommendation': None}


def forecast_calibration(store, role):
    """Learn an 80% absolute-error interval on the older half of scored hours, then measure coverage on the newer half."""
    scored = [row for row in store.db.execute(
        'SELECT candidate,actual FROM forecast_audit WHERE role=? AND actual IS NOT NULL ORDER BY target', (role,))]
    if len(scored) < MIN_CALIBRATION_HOURS:
        return {'status': 'COLLECTING', 'count': len(scored)}
    half = len(scored) // 2
    width = _quantile([abs(c - a) for c, a in scored[:half]], .8)
    newer = scored[half:]
    coverage = sum(abs(c - a) <= width for c, a in newer) / len(newer)
    status = 'OVERCONFIDENT' if coverage < .7 else 'TOO_WIDE' if coverage > .9 else 'CALIBRATED'
    return {'status': status, 'interval_w': round(width, 1), 'nominal': 0.8, 'coverage': round(coverage, 3),
            'checked_hours': len(newer),
            'basis': 'interval learned on older hours only, coverage measured on later hours; read-only trust signal'}


COUNTER_KEY = 'insights:counters'
MAX_COUNTERS = 50
STUCK_AFTER_S = 3 * 3600
STUCK_BUSY_HOURS = 3         # 0.1.4: complete busy hours without a change (night rest is not a stall)
REGULAR_MIN_HOURS = 24      # 0.1.4: busy hours observed before a counter can be judged
REGULAR_SHARE = 0.9         # and the share of them in which it advanced


def bank_runway(bill_packet):
    """Next 12 billing months per bucket: same month's most recent net from the statements, December true-up payout."""
    bills = (bill_packet or {}).get('bills') if isinstance(bill_packet, dict) else None
    if not isinstance(bills, list) or not bills:
        return {'status': 'NOT_AVAILABLE', 'reason': 'RETAINED_STATEMENTS_MISSING'}
    try:
        params = bill_packet.get('owner_parameters') or {}
        true_up = int((params.get('annual_true_up_billing_month') or {}).get('value') or 12)
        payout = float((params.get('true_up_payout_rate_per_kwh') or {}).get('value') or 0.)
        ordered = sorted(bills, key=lambda b: b['billing_period'])
        latest_by_month = {int(b['billing_period'][5:7]): b for b in ordered}
        last = ordered[-1]
        bank = {p: float(last[p]['net_balance_kwh']) for p in ('on_peak', 'off_peak')}
        year, month = int(last['billing_period'][:4]), int(last['billing_period'][5:7])
        months, first_shortfall, settled = [], {}, 0.
        for _ in range(12):
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
            same = latest_by_month.get(month)
            if same is None:
                return {'status': 'NOT_AVAILABLE', 'reason': 'STATEMENT_MONTH_MISSING'}
            row = {'billing_period': f'{year}-{month:02d}', 'billed_kwh': {}}
            for bucket in ('on_peak', 'off_peak'):
                net = bank[bucket] + float(same[bucket]['credits_earned_kwh']) - float(same[bucket]['energy_use_kwh'])
                bank[bucket] = max(0., net)
                row['billed_kwh'][bucket] = round(max(0., -net), 1)
                if net < 0 and bucket not in first_shortfall:
                    first_shortfall[bucket] = row['billing_period']
            if month == true_up:
                row['bank_before_true_up_kwh'] = {p: round(v, 1) for p, v in bank.items()}
                settled += sum(bank.values()) * payout
                bank = {p: 0. for p in bank}
            row['bank_kwh'] = {p: round(v, 1) for p, v in bank.items()}
            months.append(row)
    except (KeyError, TypeError, ValueError):
        return {'status': 'NOT_AVAILABLE', 'reason': 'RETAINED_STATEMENTS_UNREADABLE'}
    return {'status': 'PROJECTED', 'months': months, 'first_billed_month': first_shortfall,
            'true_up_payout_dollars': round(settled, 2), 'true_up_payout_rate_per_kwh': payout,
            'basis': "assumes each coming month repeats that month's most recent statement; weather, solar and "
                     'use changes are not predicted', 'recommendation': None}


def counter_health(store, states, now, load_w):
    """Daily energy counters that stop advancing while the house is using power; report only, no alert.

    0.1.4: only a counter that normally moves is judged. Each counter learns, per busy hour (8 am to 8 pm, load
    above 200 W), whether it advanced; it can be flagged only after REGULAR_MIN_HOURS busy hours in which it
    advanced at least REGULAR_SHARE of the time. Appliance counters that sit still for hours are never flagged."""
    seen = store.get(COUNTER_KEY, {})
    local = _local(now)
    hour = int(now // 3600)
    busy = load_w is not None and load_w > 200 and 8 <= local.hour < 20
    findings = {}
    learning = 0
    candidates = sorted(e for e, s in states.items() if isinstance(s, dict)
                        and (s.get('attributes') or {}).get('state_class') == 'total_increasing'
                        and (s.get('attributes') or {}).get('unit_of_measurement') == 'kWh')[:MAX_COUNTERS]
    for entity in candidates:
        record = seen.get(entity) or {}
        try:
            value = float(states[entity].get('state'))
        except (TypeError, ValueError):
            value = None
        if record.get('hour') != hour:
            if record.get('hour_busy') and record.get('hour_value') is not None and record.get('value') is not None:
                moved = record['value'] != record['hour_value']
                record['busy_hours'] = record.get('busy_hours', 0) + 1
                record['moving_hours'] = record.get('moving_hours', 0) + moved
                record['stalled_hours'] = 0 if moved else record.get('stalled_hours', 0) + 1
            record.update(hour=hour, hour_value=value, hour_busy=False)
        record['hour_busy'] = record.get('hour_busy', False) or busy
        if record.get('value') != value:
            record.update(value=value, since=now, stalled_hours=0)
            if value is not None:
                # Judged only when it moves, so a stall or an outage cannot argue it out of being regular.
                record['regular'] = (record.get('busy_hours', 0) >= REGULAR_MIN_HOURS
                                     and record.get('moving_hours', 0) >= REGULAR_SHARE * record['busy_hours'])
        seen[entity] = record
        if not record.get('regular'):
            learning += record.get('busy_hours', 0) < REGULAR_MIN_HOURS
            continue
        if value is None:
            findings[entity] = 'UNAVAILABLE'
        elif busy and record.get('stalled_hours', 0) >= STUCK_BUSY_HOURS:
            findings[entity] = 'ZERO_WHILE_HOUSE_USES_POWER' if value == 0 else 'NOT_ADVANCING'
    for gone in set(seen) - set(candidates):
        seen.pop(gone)
    store.put(COUNTER_KEY, seen)
    return {'status': 'REVIEW' if findings else 'OK', 'checked': len(candidates), 'learning': learning,
            'findings': findings,
            'basis': 'kWh total_increasing counters that normally advance in busy hours (8 am to 8 pm, load above '
                     '200 W) but have not changed for 3 complete busy hours; appliance counters that pause are not judged; a hint for '
                     'the owning lane',
            'alert_sent': False}


def self_use(solar_hours, export_rows):
    """Monthly share of solar used in the house rather than exported, from the hourly rollups already kept."""
    exports = dict(export_rows or [])
    months = {}
    for at, watts in solar_hours:
        if at in exports:
            month = months.setdefault(_local(at).strftime('%Y-%m'), [0., 0.])
            month[0] += watts / 1000
            month[1] += min(exports[at], watts) / 1000
    out = {m: {'solar_kwh': round(s, 1), 'exported_kwh': round(e, 1),
               'self_use_percent': round(100 * (s - e) / s, 1)} for m, (s, e) in sorted(months.items()) if s >= 1}
    return {'status': 'ESTIMATED' if out else 'WAITING', 'months': dict(list(out.items())[-13:]),
            'basis': 'hourly means; export is capped at solar for the same hour'}


def weekly_summary(scorecard, view, rate_plan):
    """Plain sentences from the views above; anything not yet available is said so, never filled in."""
    lines = []
    if scorecard and scorecard.get('window_days'):
        days, made = scorecard['window_days'], scorecard['distinct_proposals']
        lines.append(f"Over {days} day{'' if days == 1 else 's'} the shadow planner made {made} "
                     f"proposal{'' if made == 1 else 's'}; none was carried out, so no savings are claimed.")
    always = view.get('always_on') or {}
    if always.get('status') == 'ESTIMATED':
        lines.append(f"Always-on load is about {always['latest_overnight_w']:.0f} W, about "
                     f"${always['yearly_cost_dollars_at_latest']:.0f} a year at base rates.")
    projection = view.get('bill_projection') or {}
    if projection.get('status') == 'PROJECTED':
        band = projection['band_dollars']
        lines.append(f"This month is heading for about ${projection['projected_dollars']:.0f} in base charges "
                     f"(${band['low']:.0f}-${band['high']:.0f}).")
    stay = (rate_plan or {}).get('stay_put') or {}
    if stay.get('last_12_statements_value_dollars') is not None:
        lines.append(f"Banking on the current plan was worth at least ${stay['last_12_statements_value_dollars']:.0f} "
                     'over the last 12 statements; leaving the plan is permanent.')
    counters = view.get('counter_health') or {}
    if counters.get('findings'):
        lines.append(f"{len(counters['findings'])} energy counter(s) look stuck; see data health.")
    return {'text': ' '.join(lines) if lines else 'Not enough data yet for a summary.',
            'generated_by': 'fixed templates, no language model'}


def credit_headroom(runway, tariff, now):
    """On-peak kWh projected to be paid out at the true-up: shifting that much off-peak use into on-peak hours
    before then uses credit that would otherwise sell at the payout rate. Information only."""
    if (runway or {}).get('status') != 'PROJECTED':
        return {'status': 'NOT_AVAILABLE', 'reason': 'BANK_RUNWAY_UNAVAILABLE'}
    current = _local(now).strftime('%Y-%m')
    window = []
    for row in runway['months']:
        if row['billing_period'] < current:
            continue
        before = row.get('bank_before_true_up_kwh')
        window.append((before or row['bank_kwh'])['on_peak'])
        if before is not None:
            break
    else:
        return {'status': 'NO_TRUE_UP_AHEAD', 'recommendation': None}
    # Shifting starts now, so every month until the payout must keep a non-negative bank: the minimum is safe.
    kwh = max(0., min(window)) if window else 0.
    payout = runway['true_up_payout_rate_per_kwh']
    off_peak = tariff['charges']['energy_per_kwh'][season(_local(now).month)]['off_peak']
    return {'status': 'ESTIMATED' if kwh > 0 else 'NONE', 'shiftable_on_peak_kwh': round(kwh),
            'true_up_month': next(r['billing_period'] for r in runway['months'] if r.get('bank_before_true_up_kwh')),
            'value_if_shifted_dollars': round(kwh * max(0., off_peak - payout), 2),
            'on_peak_window': 'weekdays 12:00-19:00 (holidays excluded)',
            'basis': 'banked on-peak credit only offsets on-peak use and its leftover is paid out at the true-up '
                     'rate; off-peak use is billed at the off-peak rate. Statement-based projection; the owning '
                     'lanes (pool, EV, cooling) decide whether anything moves', 'recommendation': None,
            'physical_actions': False}


def _true_up_deadline(true_up_month):
    """The true-up statement (statement month YYYY-MM) closes on the 2nd of that month, local midnight."""
    year, month = int(true_up_month[:4]), int(true_up_month[5:7])
    return datetime(year, month, 2, tzinfo=TZ).timestamp()


def credit_plan(headroom, now):
    """0.1.5 (owner pop-up 2026-10-25 "all the above"): true-up countdown and today's credit-burn window.

    Banked on-peak credit left at the true-up sells at the low payout rate, while off-peak use is billed, so until
    then flexible use is cheapest on weekday on-peak hours. Spread evenly over the weekdays left. Information only."""
    if (headroom or {}).get('status') != 'ESTIMATED':
        return {'status': 'NOT_AVAILABLE', 'reason': (headroom or {}).get('reason') or (headroom or {}).get('status'),
                'recommendation': None}
    deadline = _true_up_deadline(headroom['true_up_month'])
    day = _local(now).date()
    weekdays = [day + timedelta(days=d) for d in range(max(0, int((deadline - now) // 86400)) + 1)]
    weekdays = [d for d in weekdays if d.weekday() < 5 and not holiday(d)
                and datetime(d.year, d.month, d.day, 19, tzinfo=TZ).timestamp() <= deadline]
    today_ok = bool(weekdays) and weekdays[0] == day and _local(now).hour < 19
    per_day = round(headroom['shiftable_on_peak_kwh'] / len(weekdays), 1) if weekdays else 0.
    return {'status': 'ESTIMATED', 'true_up_month': headroom['true_up_month'],
            'days_left': max(0, int((deadline - now) // 86400)), 'weekdays_left': len(weekdays),
            'surplus_on_peak_kwh': headroom['shiftable_on_peak_kwh'], 'kwh_per_weekday': per_day,
            'value_if_used_dollars': headroom['value_if_shifted_dollars'],
            'today': 'weekday on-peak 12:00-19:00' if today_ok else 'not an on-peak day',
            'basis': 'surplus on-peak credit / weekdays left before the true-up statement closes; shifting more '
                     'than this would be billed on-peak later', 'recommendation': None, 'physical_actions': False}


def cooling_hint(runway, tariff, now):
    """Which hours cooling energy is paid for (for the Cooling AI lane): on-peak is covered by banked credit until
    the first billed on-peak month; off-peak is billed once its bank is empty. Information only."""
    if (runway or {}).get('status') != 'PROJECTED':
        return {'status': 'NOT_AVAILABLE', 'recommendation': None}
    first = runway.get('first_billed_month') or {}
    current = _local(now).strftime('%Y-%m')
    rates = tariff['charges']['energy_per_kwh'][season(_local(now).month)]
    on_covered = first.get('on_peak') is None or first['on_peak'] > current
    off_billed = first.get('off_peak') is not None and first['off_peak'] <= current
    return {'status': 'ESTIMATED', 'on_peak_covered_until': first.get('on_peak'),
            'off_peak_billed_since': first.get('off_peak'),
            'on_peak_cost_per_kwh': 0. if on_covered else rates['on_peak'],
            'off_peak_cost_per_kwh': rates['off_peak'] if off_billed else 0.,
            'cheaper_to_cool': ('on-peak (credit-covered)' if on_covered and off_billed else
                                'off-peak' if not on_covered else 'either (both covered)'),
            'basis': 'statement-based bank runway; marginal energy rate only, comfort decisions stay with Cooling',
            'recommendation': None, 'physical_actions': False}


def night_extra(base, sense_always_on_w, tariff, now):
    """Overnight load above Sense's 24 h always-on floor (for example a night vent fan or A/C), priced at the
    off-peak rate it is billed at. Information only."""
    floor = (base or {}).get('always_on_floor_w')
    if floor is None or sense_always_on_w is None:
        return {'status': 'NOT_AVAILABLE', 'recommendation': None}
    extra = max(0., floor - sense_always_on_w)
    rate = tariff['charges']['energy_per_kwh'][season(_local(now).month)]['off_peak']
    kwh_month = extra / 1000 * 5 * 30          # 1-5 am only: a lower bound for the whole night
    return {'status': 'ESTIMATED', 'extra_w': round(extra), 'kwh_per_month_1_to_5am': round(kwh_month, 1),
            'dollars_per_month_1_to_5am': round(kwh_month * rate, 2),
            'basis': 'quietest 1-5 am hour minus Sense always-on; whole-night use is higher', 'recommendation': None,
            'physical_actions': False}
