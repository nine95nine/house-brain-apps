"""Room baselines and drift (design R1 §4.1; absorbs the Learning/Diagnosis 1.4-R5 room math).

Learning 1.4-R5 keeps, per room and per period, a `statistics` mean over 7 days of a sample sensor that
holds the room temperature only inside that period's hours (local 0-4, 4-8, 8-12, 12-16, 16-20, 20-24).
The sample sensor changes whenever the room temperature changes, and becomes available (one sample) at
the start of each period. This module computes the same mean from the App's own change points:
- every numeric change point inside a period's hours in the last 7 days, plus the value held at the
  start of each period window (the sample the YAML takes when its sample sensor becomes available);
- the most recent 1,000 samples per period at most (the YAML's `sampling_size`).

A baseline is READY only after samples on at least MIN_DAYS distinct days; until then it is LEARNING and
no drift is judged. Drift is a room outside its band (Learning's own default threshold, 2.5 °F) for at
least HOLD_S, with hysteresis: it clears only once back within EXIT_F for HOLD_S. A missing reading is
UNKNOWN (never NORMAL) and holds the last judgement until data returns. Every drift carries the cooling
context (the Arbiter phase) at its start and now. Pure functions; nothing here reads the network.
"""
from datetime import datetime, timedelta

from .entities import PERIODS
from .series import ZONE, local

WINDOW_S = 7 * 86400
MAX_SAMPLES = 1000
MIN_DAYS = 3
ENTER_F = 2.5          # Learning 1.4-R5 input_number.house_brain_learning_room_deviation_threshold default
EXIT_F = 1.5
HOLD_S = 30 * 60
TEMP_RANGE = (20., 130.)


def period_of(at):
    """The Learning period slug for a time (local hour)."""
    hour = local(at).hour
    return next(slug for slug, _, lo, hi in PERIODS if lo <= hour < hi)


def _windows(start, end):
    """(slug, window start, window end) for every period window overlapping [start, end), local time."""
    day = local(start).date()
    out = []
    while True:
        midnight = datetime(day.year, day.month, day.day, tzinfo=ZONE)
        if midnight.timestamp() >= end:
            break
        for slug, _, lo, hi in PERIODS:
            a, b = (midnight + timedelta(hours=lo)).timestamp(), (midnight + timedelta(hours=hi)).timestamp()
            if b > start and a < end:
                out.append((slug, max(a, start), min(b, end)))
        day = day + timedelta(days=1)
    return out


def period_means(points, now):
    """{slug: {'mean', 'samples', 'days', 'status'}} from change points [(t, value, word)] sorted by time.
    A gap ends the held value: nothing is sampled from a gap."""
    lo, hi = TEMP_RANGE
    start = now - WINDOW_S
    samples = {slug: [] for slug, *_ in PERIODS}
    pts = [(t, v if v is not None and lo <= v <= hi else None) for t, v, _ in points]
    i, held = 0, None
    for slug, a, b in _windows(start, now):
        while i < len(pts) and pts[i][0] <= a:
            held = pts[i][1]
            i += 1
        if held is not None:
            samples[slug].append((a, held))
        while i < len(pts) and pts[i][0] < b:
            held = pts[i][1]
            if held is not None:
                samples[slug].append((pts[i][0], held))
            i += 1
    out = {}
    for slug, rows in samples.items():
        rows = rows[-MAX_SAMPLES:]
        days = len({local(t).date() for t, _ in rows})
        if not rows:
            out[slug] = {'mean': None, 'samples': 0, 'days': 0, 'status': 'LEARNING'}
            continue
        out[slug] = {'mean': round(sum(v for _, v in rows) / len(rows), 2), 'samples': len(rows), 'days': days,
                     'status': 'READY' if days >= MIN_DAYS else 'LEARNING'}
    return out


def drift_step(prev, deviation, now, phase=None):
    """One step of the drift state machine. prev/result: {'state': NORMAL|DRIFT|UNKNOWN, 'direction',
    'since', 'pending', 'held', 'phase_at_start'}. deviation is current minus baseline in °F, or None."""
    prev = dict(prev or {'state': 'NORMAL'})
    if deviation is None:
        # Safe hold: report UNKNOWN, remember the last judgement, drop any half-made transition.
        held = prev.get('held') if prev.get('state') == 'UNKNOWN' else prev.get('state')
        return {**prev, 'state': 'UNKNOWN', 'held': held, 'pending': None}
    if prev.get('state') == 'UNKNOWN':
        prev = {**prev, 'state': prev.get('held') or 'NORMAL'}
    state = prev['state']
    direction = 'WARM' if deviation > 0 else 'COOL'
    if state == 'NORMAL':
        if abs(deviation) >= ENTER_F:
            pending = prev.get('pending')
            if not pending or pending.get('direction') != direction:
                pending = {'direction': direction, 'at': now}
            if now - pending['at'] >= HOLD_S:
                return {'state': 'DRIFT', 'direction': direction, 'since': pending['at'], 'pending': None,
                        'held': None, 'phase_at_start': phase}
            return {**prev, 'pending': pending}
        return {'state': 'NORMAL', 'pending': None, 'held': None}
    # DRIFT: stays until back within EXIT_F on its own side for HOLD_S.
    inside = abs(deviation) < EXIT_F or (deviation > 0) != (prev.get('direction') == 'WARM')
    if inside:
        pending = prev.get('pending') or {'direction': 'CLEAR', 'at': now}
        if now - pending['at'] >= HOLD_S:
            return {'state': 'NORMAL', 'pending': None, 'held': None}
        return {**prev, 'pending': pending}
    return {**prev, 'pending': None}


def room_view(room, points, current, now, prev_drift, phase):
    """One room: its 6 period baselines, the current period's baseline and deviation, and its drift."""
    means = period_means(points, now)
    slug = period_of(now)
    base = means[slug]
    lo, hi = TEMP_RANGE
    temp = current if current is not None and lo <= current <= hi else None
    deviation = round(temp - base['mean'], 2) if temp is not None and base['status'] == 'READY' else None
    drift = drift_step(prev_drift, deviation, now, phase)
    return {'room': room, 'period': slug, 'temperature': temp, 'baseline': base['mean'],
            'baseline_status': base['status'], 'deviation': deviation, 'drift': drift,
            'cooling_context': {'arbiter_phase_now': phase, 'arbiter_phase_at_start': drift.get('phase_at_start')},
            'periods': means}
