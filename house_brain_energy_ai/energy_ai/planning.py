"""Bank-aware shadow scheduling. No schedule is a command or utility bill."""
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
import itertools
import math
import time
from zoneinfo import ZoneInfo
from .contracts import number

TZ = ZoneInfo("America/Phoenix")


def holiday(day):
    days = set()
    for year in (day.year-1, day.year, day.year+1):
        for month, dom in ((1,1),(7,4),(12,25)):
            d = date(year,month,dom)
            days.add(d)
            days.add(d-timedelta(days=1) if d.weekday()==5 else d+timedelta(days=1) if d.weekday()==6 else d)
        days.add(date(year,5,31)-timedelta(days=date(year,5,31).weekday()))
        days.add(date(year,9,1)+timedelta(days=(-date(year,9,1).weekday())%7))
        days.add(date(year,11,1)+timedelta(days=(3-date(year,11,1).weekday())%7+21))
    return day in days


def period(at):
    d = datetime.fromtimestamp(at,TZ)
    return "on_peak" if d.weekday()<5 and 12 <= d.hour < 19 and not holiday(d.date()) else "off_peak"


def energy_integral(rows, max_gap_s=180):
    totals = {p:{"import_kwh":0.,"export_kwh":0.,"coverage_s":0.} for p in ("on_peak","off_peak")}
    missing = 0.
    for (a,watts),(b,_) in zip(rows,rows[1:]):
        if b <= a:
            raise ValueError("NONMONOTONIC_HISTORY")
        if b-a > max_gap_s:
            missing += b-a
            continue
        number(watts)
        # Split at minute/calendar boundaries, never assign crossing-TOU energy to one bucket.
        cursor=a
        while cursor < b:
            end=min(b,(int(cursor//60)+1)*60)
            row=totals[period(cursor)]
            row["import_kwh"] += max(watts,0)*(end-cursor)/3600000
            row["export_kwh"] += max(-watts,0)*(end-cursor)/3600000
            row["coverage_s"] += end-cursor
            cursor=end
    return {"periods":totals,"missing_s":missing,"classification":"DERIVED",
            "method":"bounded left-hold integration of measured W; missing intervals excluded"}


class BankObjective:
    def __init__(self, tariff, billing_month, bank, terminal_credit_value, evidence_ref):
        if tariff["plan"]["rate_schedule"] != "Legacy ET-2" or tariff["plan"]["demand_charge"]:
            raise ValueError("UNSUPPORTED_TARIFF")
        if not evidence_ref or not 1 <= billing_month <= 12:
            raise ValueError("BILLING_CYCLE_EVIDENCE_REQUIRED")
        self.rates = tariff["charges"]["energy_per_kwh"]["summer" if 5<=billing_month<=10 else "winter"]
        self.bank={p:number(bank[p],0,100000) for p in self.rates}
        self.credit={p:number(terminal_credit_value[p],0,self.rates[p]) for p in self.rates}
        self.evidence=evidence_ref

    def cost(self, times, net_w, interval_s):
        net={p:0. for p in self.rates}
        for at,w in zip(times,net_w):
            net[period(at)]+=w*interval_s/3600000
        payable=sum(max(0,net[p]-self.bank[p])*self.rates[p] for p in net)
        remaining={p:max(0,self.bank[p]-net[p]) for p in net}
        # Terminal value is explicit sensitivity input, not free energy / proven payout.
        return payable-sum(remaining[p]*self.credit[p] for p in net)


@dataclass(frozen=True)
class Job:
    id: str
    domain: str
    target: str
    watts: float
    slots: int
    earliest: int
    deadline: int
    original_start: int
    allowed: tuple
    running: bool = False
    commissioned: bool = False


def schedule(times, base_w, solar_w, jobs, objective, interval_s=1800, max_evaluations=10000,
             minimum_benefit=0.05, uncertainty_dollars=0., grid_import_limit_w=None):
    n=len(times)
    if not 1<=n<=96 or len(base_w)!=n or len(solar_w)!=n or len(jobs)>4:
        raise ValueError("PLAN_SIZE")
    if interval_s not in (900,1800) or any(b-a!=interval_s for a,b in zip(times,times[1:])):
        raise ValueError("PLAN_INTERVAL")
    if any(t % interval_s for t in times):
        raise ValueError("PLAN_ALIGNMENT")
    if not 1<=max_evaluations<=10000:
        raise ValueError("SEARCH_BUDGET")
    if len({j.id for j in jobs}) != len(jobs) or len({j.target for j in jobs}) != len(jobs):
        raise ValueError("DUPLICATE_TARGET")
    for v in base_w+solar_w:
        number(v,0,200000)
    choices=[]
    for j in jobs:
        number(j.watts,1,30000)
        if any(not isinstance(v,str) or not v or len(v)>128 for v in (j.id,j.domain,j.target)):
            raise ValueError("JOB_IDENTITY")
        if type(j.running) is not bool or type(j.commissioned) is not bool:
            raise ValueError("JOB_BOOLEAN")
        if (any(type(v) is not int for v in (j.slots,j.earliest,j.deadline,j.original_start))
                or not 1<=j.slots<=n or not 0<=j.earliest<j.deadline<=n):
            raise ValueError("JOB_WINDOW")
        if len(j.allowed)!=n or any(type(v) is not bool for v in j.allowed):
            raise ValueError("JOB_ALLOWED_WINDOWS")
        starts=[s for s in range(j.earliest,j.deadline-j.slots+1) if all(j.allowed[s:s+j.slots])]
        if j.original_start not in starts:
            raise ValueError("NATIVE_PLAN_INFEASIBLE_REVIEW_REQUIRED")
        choices.append([j.original_start] if j.running else starts)
    def score(starts):
        net=[l-p for l,p in zip(base_w,solar_w)]
        for j,s in zip(jobs,starts):
            for k in range(s,s+j.slots):
                net[k]+=j.watts
        if grid_import_limit_w is not None and max(net)>number(grid_import_limit_w,1,200000):
            return float("inf")
        return objective.cost(times,net,interval_s)
    original=tuple(j.original_start for j in jobs)
    original_cost=score(original)
    if not math.isfinite(original_cost):
        raise ValueError("NATIVE_PLAN_EXCEEDS_POLICY_LIMIT")
    best,best_cost=original,original_cost
    total=math.prod(map(len,choices))
    count=0
    started=time.monotonic()
    for candidate in itertools.islice(itertools.product(*choices),max_evaluations):
        if time.monotonic()-started>2:
            break
        count+=1
        cost=score(candidate)
        if cost < best_cost-1e-9:
            best,best_cost=candidate,cost
    benefit=original_cost-best_cost
    if benefit <= max(minimum_benefit,uncertainty_dollars):
        best,best_cost,benefit=original,original_cost,0.
    return dict(starts={j.id:s for j,s in zip(jobs,best)}, baseline_cost=original_cost,
                objective_cost=best_cost, modeled_benefit=benefit, evaluations=count,
                exhaustive=count==total, no_action=best==original, physical_actions=False,
                classification="ESTIMATED", required="commissioned native controller adapter",
                caveat="conditional horizon objective; excludes fixed charges, adjustors and true-up uncertainty")
