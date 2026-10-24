"""The one Home Assistant status entity (0.1.3): what the page says, in short flat lines an AI can read.

Owner pop-ups 2026-10-24 "Take it over here" and "Status sensor counts as advice": the House Brain
read policy exports only single-value attributes (a list or object drops the whole entity), at most 24
of them and 255 characters each, so every attribute here is one short string, number or boolean.
Values come from the same diagnostics the page shows; entity ids, device names and private settings
never appear (counts and roles only). Nothing here is a recommendation or a control.
"""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

ENTITY = 'sensor.house_brain_energy_ai_status'
MAX_CHARS = 255
HEARTBEAT_S = 900          # re-publish unchanged content every 15 minutes, so readers can tell it is alive
ATTRIBUTES = ('version', 'mode', 'physical_actions', 'summary', 'data_status', 'plan', 'always_on',
              'this_month', 'bank_runway', 'credit_headroom', 'stay_put', 'heat_and_use', 'panels',
              'outage_planning', 'solar_used_at_home', 'forecast_trust', 'energy_counters', 'scorecard',
              'updated')


def money(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 'n/a'
    return '$' + str(int(Decimal(str(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP)))


def _num(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _cut(text):
    text = ' '.join(str(text).split())
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS - 1] + '…'


def _data_health(status):
    quality = status.get('data_quality') or {}
    issues = {k: v for k, v in quality.items() if k != 'status'} if isinstance(quality, dict) else {}
    idle = sorted(s.get('role', '?') for s in status.get('samples') or [] if s.get('quality') == 'IDLE')
    head = 'GOOD' if not issues else 'DEGRADED: ' + ', '.join(f'{k} {v}' for k, v in sorted(issues.items()))
    return head + (('; idle: ' + ', '.join(idle)) if idle else '')


def _plan(status):
    plan = status.get('plan_status') or {}
    reasons = plan.get('reasons') or []
    return (plan.get('status') or 'UNAVAILABLE') + ((': ' + ', '.join(map(str, reasons))) if reasons else '')


def lines(status):
    """Attribute name -> short value; mirrors the page's 'In plain words' section."""
    diag = status.get('diagnostics') or {}
    i = diag.get('insights') or {}
    a = i.get('always_on') or {}
    p = i.get('bill_projection') or {}
    r = i.get('bank_runway') or {}
    h = i.get('credit_headroom') or {}
    w = i.get('weather_load') or {}
    c = i.get('cleaning_value') or {}
    o = i.get('outage_budget') or {}
    u = i.get('self_use') or {}
    f = i.get('forecast_calibration') or {}
    k = i.get('counter_health') or {}
    s = (diag.get('rate_plan') or {}).get('stay_put') or {}
    card = status.get('scorecard') or {}
    first = r.get('first_billed_month') or {}
    months = u.get('months') or {}
    last_month = sorted(months)[-1] if months else None
    findings = k.get('findings') or {}
    return {
        'summary': ((i.get('weekly_summary') or {}).get('text') or 'Not enough data yet for a summary.'),
        'data_status': _data_health(status),   # not 'data_health': the read policy drops attribute names with a restricted word
        'plan': _plan(status),
        'always_on': (f"{_num(a.get('latest_overnight_w'))} W, about {money(a.get('yearly_cost_dollars_at_latest'))}/yr"
                      + (f" ({'+' if a['month_over_month_change_w'] >= 0 else ''}{_num(a['month_over_month_change_w'])} W vs last month)"
                         if isinstance(a.get('month_over_month_change_w'), (int, float)) else '')
                      if a.get('status') == 'ESTIMATED' else 'waiting for overnight data'),
        'this_month': (f"about {money(p.get('projected_dollars'))} base charges (range "
                       f"{money((p.get('band_dollars') or {}).get('low'))}-{money((p.get('band_dollars') or {}).get('high'))}); "
                       'adjustors, taxes and fees not included'
                       if p.get('status') == 'PROJECTED' else 'waiting for a week of this month\'s hourly data'),
        'bank_runway': ('; '.join(f"{b.replace('_', '-')} " + (f"first billed {first[b]}" if first.get(b) else 'covers 12 months')
                                  for b in ('on_peak', 'off_peak'))
                        + f"; true-up payout {money(r.get('true_up_payout_dollars'))}"
                        if r.get('status') == 'PROJECTED' else 'waiting for retained statements'),
        'credit_headroom': (f"{_num(h.get('shiftable_on_peak_kwh'))} kWh on-peak credit paid out at the {h.get('true_up_month')} "
                            f"true-up; using it on-peak ({h.get('on_peak_window')}) instead is worth up to "
                            f"{money(h.get('value_if_shifted_dollars'))}; information only"
                            if h.get('status') == 'ESTIMATED' else 'not available: ' + str(h.get('reason') or h.get('status') or 'no projection yet')),
        'stay_put': (f"banking on the current plan worth at least {money(s.get('last_12_statements_value_dollars'))} "
                     'over the last 12 statements; leaving it is permanent'
                     if s.get('status') == 'COMPUTED' else 'waiting for retained statements'),
        'heat_and_use': (f"{_num(w.get('kwh_per_cooling_degree_hour'))} kWh per cooling degree-hour above 75 F; "
                         f"error {_num(w.get('weather_mae_kwh'))} vs {_num(w.get('flat_mae_kwh'))} kWh/day flat"
                         + (' (better)' if w.get('weather_model_better') else ' (not better yet)')
                         if w.get('status') == 'SCORED' else 'waiting for about 3 weeks of outdoor temperature with load'),
        'panels': (f"{_num(c.get('output_vs_best_week_percent'))}% vs best week, worth "
                   f"{money((c.get('monthly_value_dollars') or {}).get('low'))}-{money((c.get('monthly_value_dollars') or {}).get('high'))}/month"
                   if c.get('status') == 'ESTIMATED' else 'waiting for 21 paired days'),
        'outage_planning': (f"about {_num((o.get('hours_per_10_kwh') or {}).get('conservative'))} to "
                            f"{_num((o.get('hours_per_10_kwh') or {}).get('typical'))} hours per 10 kWh overnight"
                            if o.get('status') == 'ESTIMATED' else 'waiting for overnight data'),
        'solar_used_at_home': (f"{_num(months[last_month].get('self_use_percent'))}% in {last_month}"
                               if last_month else 'waiting for solar and export data'),
        'forecast_trust': (f"load {(f.get('load') or {}).get('status') or 'collecting'}, "
                           f"solar {(f.get('solar') or {}).get('status') or 'collecting'}"),
        'energy_counters': (f"{len(findings)} counter(s) flagged: " + ', '.join(sorted(set(map(str, findings.values()))))
                            if findings else 'no stuck counters seen'),
        'scorecard': f"{card.get('distinct_proposals') or 0} shadow proposals, none carried out, no savings claimed",
    }


def sensor_payload(status, now):
    """(state, attributes) for ENTITY: every attribute one bounded scalar."""
    attrs = {'version': str(status.get('version') or 'unknown'), 'mode': str(status.get('mode') or 'unknown'),
             'physical_actions': False}
    attrs.update({key: _cut(value) for key, value in lines(status).items()})
    attrs['updated'] = datetime.fromtimestamp(int(now) // 60 * 60, timezone.utc).strftime('%Y-%m-%dT%H:%MZ')
    return _cut(status.get('state') or 'UNKNOWN')[:64], attrs


def due(previous, payload, now):
    """Publish when anything but 'updated' changed, or every HEARTBEAT_S as a liveness signal."""
    if previous is None:
        return True
    (old_state, old_attrs), old_at = previous
    state, attrs = payload
    unchanged = state == old_state and {k: v for k, v in attrs.items() if k != 'updated'} == \
        {k: v for k, v in old_attrs.items() if k != 'updated'}
    return not unchanged or now - old_at >= HEARTBEAT_S
