"""Pump runs, their energy when Sense corroborates them, and their cost by APS window (design R1 §4.4).

Runs come from `switch.pool_pump` edges (Jandy is the authority; Sense never creates a run). The #111 R1
core thresholds the owner promoted on 2026-10-31 (`docs/validation/POOL_PUMP_111_THRESHOLD_PROMOTION_
2026-10-31.md`): a run under 1,800 s is a SHORT_RUN, a start less than 600 s after the last stop is a
RAPID_RESTART. An `unavailable`/`unknown` blip shorter than 600 s with the same state on both sides is
bridged (the `u` blips of the #111 readouts); a longer gap ends the run as END_UNKNOWN.

Energy: whole-home Sense power (`sensor.sense_51446_energy`, W) sampled each minute from its change
points (a reading not refreshed for 15 minutes is a gap, the recorder package's POWER_STALE rule). A run
is corroborated only when BOTH edges show a pump-sized step: the median over 10 minutes after the start
(60 s settle) minus the 10 minutes before, and the 10 minutes before the stop minus the 10 minutes after,
each within the observed pump signature 800-1,100 W, each from at least 4 samples. These Sense numbers are
the #111 registry's ANALYSIS DEFAULTS (the R2 Sense group was NOT promoted: DISAGREEMENT), so most runs
are expected to read "not corroborated". kWh = mean step x hours, only for a corroborated run; an
uncorroborated run never gets a figure.

Cost: APS Legacy ET-2 base energy prices (`contracts/aps_legacy_et2_tariff.v1.json`; adjustors and taxes
excluded, so not an all-in price), split by the live `binary_sensor.aps_on_peak` minutes; summer prices
for usage April to September (the contract's usage-date approximation). Any minute of the run with the
on-peak flag unknown leaves the cost unknown.
"""
from statistics import median

from .demand import segments
from .entities import number
from .series import local

MIN_RUN_S = 1800          # #111 R1 min_runtime_s (owner, 2026-10-31)
MIN_OFF_S = 600           # #111 R1 min_off_s
BLIP_S = 600
SETTLE_S = 60             # R2 analysis default (not promoted)
WINDOW_S = 600            # R2 baseline/post window, analysis default
MIN_SAMPLES = 4           # R2 min_samples_per_segment, analysis default
STEP_W = (800., 1100.)    # R2 run profile OBSERVED_SIGNATURE, analysis default
STALE_S = 900
SAMPLE_S = 60
RATES = {'summer': {'on_peak': 0.32334, 'off_peak': 0.08054},     # aps_legacy_et2_tariff.v1 energy_per_kwh
         'winter': {'on_peak': 0.26230, 'off_peak': 0.07939}}
SUMMER_MONTHS = frozenset({4, 5, 6, 7, 8, 9})
SCORE_MIN = 3
SCORE_TOLERANCE = 0.15


def runs(switch, now):
    """Completed and open runs from the switch's change points, blips bridged."""
    pts = [(t, s if s in ('on', 'off') else None) for t, _, s in switch]
    merged = []
    for i, (t, s) in enumerate(pts):
        if s is None:
            nxt = pts[i + 1] if i + 1 < len(pts) else None
            prev = merged[-1][1] if merged else None
            if nxt is not None and nxt[1] is not None and nxt[1] == prev and nxt[0] - t < BLIP_S:
                continue                                    # a short blip: the state carries on
        if merged and merged[-1][1] == s:
            continue
        merged.append((t, s))
    out, last_stop = [], None
    for i, (t, s) in enumerate(merged):
        if s != 'on':
            continue
        nxt = merged[i + 1] if i + 1 < len(merged) else None
        end = nxt[0] if nxt else None
        status = 'RUNNING' if nxt is None else 'COMPLETE' if nxt[1] == 'off' else 'END_UNKNOWN'
        run = {'start': t, 'end': end, 'status': status, 'seconds': None if end is None else round(end - t),
               'flags': []}
        if status == 'COMPLETE' and end - t < MIN_RUN_S:
            run['flags'].append('SHORT_RUN')
        if last_stop is not None and t - last_stop < MIN_OFF_S:
            run['flags'].append('RAPID_RESTART')
        out.append(run)
        last_stop = end if status == 'COMPLETE' else None
    return out


def samples(power, a, b):
    """Sense watts each minute over [a, b): a gap or a reading older than 15 minutes is no sample."""
    out = []
    t = a
    i = 0
    while t < b:
        while i + 1 < len(power) and power[i + 1][0] <= t:
            i += 1
        if power and power[i][0] <= t and t - power[i][0] <= STALE_S:
            w = number(power[i][1], 0, 200000)
            if w is not None:
                out.append(w)
        t += SAMPLE_S
    return out


def corroborate(power, run):
    """Both edges must show a pump-sized step from at least 4 samples each; otherwise NOT_CORROBORATED."""
    if run['status'] != 'COMPLETE':
        return {'status': 'NOT_JUDGED', 'reason': 'RUN_NOT_COMPLETE'}
    s, e = run['start'], run['end']
    windows = {'before': samples(power, s - WINDOW_S, s), 'after_start': samples(power, s + SETTLE_S, s + SETTLE_S + WINDOW_S),
               'before_stop': samples(power, e - SETTLE_S - WINDOW_S, e - SETTLE_S), 'after_stop': samples(power, e + SETTLE_S, e + SETTLE_S + WINDOW_S)}
    if any(len(v) < MIN_SAMPLES for v in windows.values()):
        return {'status': 'NOT_CORROBORATED', 'reason': 'SENSE_SAMPLES_INSUFFICIENT'}
    m = {k: median(v) for k, v in windows.items()}
    on_step, off_step = m['after_start'] - m['before'], m['before_stop'] - m['after_stop']
    ok = all(STEP_W[0] <= x <= STEP_W[1] for x in (on_step, off_step))
    return {'status': 'CORROBORATED' if ok else 'NOT_CORROBORATED', 'on_step_w': round(on_step, 1),
            'off_step_w': round(off_step, 1), 'reason': None if ok else 'SENSE_STEP_OUTSIDE_PUMP_SIGNATURE'}


def on_peak_seconds(flag, a, b):
    """(on-peak seconds, unknown seconds) over [a, b) from `binary_sensor.aps_on_peak`."""
    on = unknown = 0.
    covered = 0.
    for lo, hi, _, s in segments(flag, a, b):
        covered += hi - lo
        if s == 'on':
            on += hi - lo
        elif s != 'off':
            unknown += hi - lo
    return on, unknown + max(0., (b - a) - covered)


def season(at):
    return 'summer' if local(at).month in SUMMER_MONTHS else 'winter'


def cost(run, kwh, flag):
    """Base energy price of a run's kWh, split by on-peak minutes; None when any minute's window is unknown."""
    on, unknown = on_peak_seconds(flag, run['start'], run['end'])
    out = {'on_peak_s': round(on), 'window_unknown_s': round(unknown), 'season': season(run['start'])}
    if kwh is None:
        return {**out, 'usd': None}
    if unknown > 0:
        return {**out, 'usd': None, 'reason': 'APS_WINDOW_UNKNOWN'}
    rates = RATES[out['season']]
    span = run['end'] - run['start']
    share = on / span if span > 0 else 0.
    return {**out, 'usd': round(kwh * (share * rates['on_peak'] + (1 - share) * rates['off_peak']), 4)}


def assess(switch, power_of, flag, now):
    """Every run with its flags, corroboration, kWh (only when corroborated) and cost. `power_of(a, b)` gives
    the Sense change points for [a, b) (the runtime reads them from the App's own store)."""
    out = []
    for run in runs(switch, now):
        c = corroborate(power_of(run['start'] - WINDOW_S - SAMPLE_S, (run['end'] or run['start']) + SETTLE_S + WINDOW_S
                                 + SAMPLE_S), run)
        kwh = None
        if c['status'] == 'CORROBORATED':
            kwh = round((c['on_step_w'] + c['off_step_w']) / 2 * run['seconds'] / 3600 / 1000, 3)
        run.update(sense=c, kwh=kwh)
        if run['status'] == 'COMPLETE':
            run['cost'] = cost(run, kwh, flag)
        out.append(run)
    return out


def scorecard(all_runs):
    """kWh per run: the median step of earlier corroborated runs x this run's hours, against Sense's own
    figure, within 15 %. Never a figure for an uncorroborated run (counted: must stay 0)."""
    good = [r for r in all_runs if r.get('kwh') is not None and (r.get('sense') or {}).get('status') == 'CORROBORATED']
    claims = sum(1 for r in all_runs if r.get('kwh') is not None and (r.get('sense') or {}).get('status') != 'CORROBORATED')
    scored = []
    for j, r in enumerate(good):
        before = [(x['sense']['on_step_w'] + x['sense']['off_step_w']) / 2 for x in good[:j]]
        if len(before) < SCORE_MIN:
            continue
        guess = median(before) * r['seconds'] / 3600 / 1000
        scored.append(abs(guess - r['kwh']) / r['kwh'] <= SCORE_TOLERANCE)
    status = 'NOT_MET' if claims else 'WAITING' if len(scored) < SCORE_MIN else \
        'MET' if sum(scored) / len(scored) >= 0.9 else 'NOT_MET'
    return {'corroborated_runs': len(good), 'scored': len(scored), 'within': sum(scored),
            'claims_without_corroboration': claims, 'status': status}


def view(all_runs):
    done = [r for r in all_runs if r['status'] == 'COMPLETE']
    week = done[-7:]
    return {'runs': len(done), 'open': sum(r['status'] == 'RUNNING' for r in all_runs),
            'end_unknown': sum(r['status'] == 'END_UNKNOWN' for r in all_runs),
            'short_runs': sum('SHORT_RUN' in r['flags'] for r in all_runs),
            'rapid_restarts': sum('RAPID_RESTART' in r['flags'] for r in all_runs),
            'corroborated': sum(r['kwh'] is not None for r in done),
            'on_peak_minutes': round(sum(r['cost']['on_peak_s'] for r in done) / 60),
            'last_runs': [{k: r.get(k) for k in ('start', 'end', 'seconds', 'status', 'flags', 'kwh')}
                          | {'sense': r['sense']['status'], 'usd': (r.get('cost') or {}).get('usd'),
                             'on_peak_s': (r.get('cost') or {}).get('on_peak_s')} for r in week],
            'scorecard': scorecard(all_runs), 'thresholds': {'min_run_s': MIN_RUN_S, 'min_off_s': MIN_OFF_S},
            'sense_rule': 'R2 analysis defaults, not promoted (#111): 800-1,100 W step on both edges'}
