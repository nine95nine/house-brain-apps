"""Shadow scorecard: what the shadow planner proposed and how well forecasts did, per local day.

Planner figures are the planner's own modeled benefit at decision time, never measured savings:
nothing is executed, so no outcome exists to measure. Each distinct proposal counts once.
"""
from datetime import datetime
from .planning import TZ

KEY = 'scorecard:days'
KEEP_DAYS = 35
MAX_PROPOSALS_PER_DAY = 96
MAX_REASONS = 16


def day(now):
    return datetime.fromtimestamp(now, TZ).strftime('%Y-%m-%d')


def record(store, now, plan, plan_digest, forecast_audit):
    """Fold one cycle into today's row; bounded, idempotent per proposal digest."""
    days = store.get(KEY, {})
    today = day(now)
    row = days.get(today) or {'cycles': 0, 'proposals': {}, 'blocked': {}, 'forecast': {}}
    row['cycles'] += 1
    if plan.get('status') == 'SHADOW_PROPOSAL' and plan_digest:
        result = plan.get('result') or {}
        if plan_digest not in row['proposals'] and len(row['proposals']) < MAX_PROPOSALS_PER_DAY:
            row['proposals'][plan_digest] = round(float(result.get('modeled_benefit') or 0.), 4)
    elif plan.get('status') == 'BLOCKED':
        for reason in (plan.get('reasons') or [])[:MAX_REASONS]:
            if isinstance(reason, str) and (reason in row['blocked'] or len(row['blocked']) < MAX_REASONS):
                row['blocked'][reason] = row['blocked'].get(reason, 0) + 1
    for role, audit in (forecast_audit or {}).items():
        if isinstance(audit, dict) and audit.get('status') in ('TRACKING', 'DRIFT_REVIEW'):
            row['forecast'][role] = {k: audit[k] for k in
                ('status', 'count', 'deployed_mae_w', 'baseline_mae_w', 'reference_mae_w', 'reference_better')
                if k in audit}
    days[today] = row
    for old in sorted(days)[:-KEEP_DAYS]:
        days.pop(old)
    store.put(KEY, days)
    return summarize(days, now)


def summarize(days, now, window=7):
    recent = [d for d in sorted(days) if d <= day(now)][-window:]
    benefits = [b for d in recent for b in days[d]['proposals'].values()]
    blocked = {}
    for d in recent:
        for reason, count in days[d]['blocked'].items():
            blocked[reason] = blocked.get(reason, 0) + count
    latest_forecast = next((days[d]['forecast'] for d in reversed(recent) if days[d]['forecast']), {})
    return {'window_days': len(recent), 'cycles': sum(days[d]['cycles'] for d in recent),
            'distinct_proposals': len(benefits),
            'modeled_benefit_dollars': ({'min': min(benefits), 'max': max(benefits)} if benefits else None),
            'blocked_cycles_by_reason': dict(sorted(blocked.items(), key=lambda kv: -kv[1])[:MAX_REASONS]),
            'forecast_skill': latest_forecast,
            'basis': 'planner modeled benefit at decision time; shadow only, nothing executed, not measured savings',
            'savings_claim': False, 'physical_actions': False}
