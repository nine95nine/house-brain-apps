"""The RO tank, learned in psi only (design R1 §4).

From the one-a-minute pressure series it finds refills (rising), draws (falling fast) and the idle
overnight bleed. It reports rates, durations and counts in psi and minutes. Gallons are NOT
AVAILABLE: they need the P0 bench values (membrane rating, precharge, shut-off fraction) and a
second calibration point, which the fish-room lane has not recorded. Nothing here is an alert, and
no alert threshold is invented: the two episode floors below only decide what counts as an
episode, and the slope limit is learned from this device's own noise.
"""
import statistics
from datetime import datetime
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('America/Phoenix')
SMOOTH = 5                # minutes: a centred median removes ADC flicker
SPAN = 5                  # minutes either side for the slope
MIN_SLOPE = 0.5           # psi/h floor for a rising or falling minute (the learned noise limit can raise it)
NOISE_K = 4               # learned slope limit = NOISE_K × the device's own slope noise
MIN_REFILL_MIN, MIN_DRAW_MIN = 5, 2
MIN_EPISODE_PSI = 1.0     # an episode must move the pressure at least this much
PLATEAU_MIN = 15
NIGHT = (0, 5)            # local hours for the idle bleed
MIN_NIGHT_MIN = 120
GAUGE_RANGE = (-5., 150.)
MIN_CLASS_DRAWS = 10      # draws before the two draw sizes are learned
CLASS_RATIO = 2.0         # the two sizes must differ at least this much, or there is one class


def local(t):
    return datetime.fromtimestamp(t, ZONE)


def runs(rows):
    """Contiguous minute runs. One missed minute (a slow cycle) holds the previous reading, as the device
    itself held it; any longer gap ends the run and is never filled."""
    out, cur = [], []
    for t, v in rows:
        if cur and t - cur[-1][0] == 120:
            cur.append((t - 60, cur[-1][1]))
        elif cur and t - cur[-1][0] != 60:
            out.append(cur)
            cur = []
        cur.append((t, v))
    if cur:
        out.append(cur)
    return out


def smooth(seg):
    half = SMOOTH // 2
    vals = [v for _, v in seg]
    return [(t, statistics.median(vals[max(0, i - half):i + half + 1])) for i, (t, _) in enumerate(seg)]


def slopes(seg):
    """psi/h at each minute from a centred difference over 2 × SPAN minutes."""
    out = []
    for i in range(SPAN, len(seg) - SPAN):
        out.append((seg[i][0], seg[i][1], (seg[i + SPAN][1] - seg[i - SPAN][1]) * 60 / (2 * SPAN)))
    return out


def noise_limit(all_slopes):
    """The device's own slope noise (robust spread of steady minutes) × NOISE_K, never below MIN_SLOPE."""
    vals = [s for _, _, s in all_slopes]
    if len(vals) < 60:
        return MIN_SLOPE
    med = statistics.median(vals)
    mad = statistics.median(abs(v - med) for v in vals) * 1.4826
    return max(MIN_SLOPE, NOISE_K * mad)


def trim(cls, i, j, kind, eps):
    """The slope window widens an episode by up to SPAN minutes each side: keep only the minutes where the
    pressure itself moved in the episode's direction by more than the device's own jitter."""
    if kind == 'S':
        return i, j
    sign = 1 if kind == 'R' else -1
    lo, hi = max(i - 1, 0), j
    moved = [k for k in range(lo + 1, hi + 1) if (cls[k][1] - cls[k - 1][1]) * sign > eps]
    if not moved:
        return i, j
    return moved[0] - 1, moved[-1]


def episodes(rows):
    """Refills, draws and the per-minute class from the pressure series. Every number in psi or minutes."""
    segs = [smooth(s) for s in runs([(t, v) for t, v in rows if GAUGE_RANGE[0] <= v <= GAUGE_RANGE[1]])]
    per = [slopes(s) for s in segs if len(s) > 2 * SPAN]
    limit = noise_limit([x for p in per for x in p])
    # A minute only counts as moving when it moves more than the device's own minute-to-minute jitter.
    steps = [abs(b[1] - a[1]) for seg in segs for a, b in zip(seg, seg[1:])]
    jitter = statistics.median(steps) * 1.4826 if len(steps) >= 60 else 0.
    eps = max(limit / 60, 2 * jitter)
    refills, draws = [], []
    for p in per:
        cls = [(t, v, 'R' if s > limit else 'F' if s < -limit else 'S') for t, v, s in p]
        i = 0
        while i < len(cls):
            j = i
            while j + 1 < len(cls) and cls[j + 1][2] == cls[i][2]:
                j += 1
            kind = cls[i][2]
            i0, j0 = trim(cls, i, j, kind, eps)
            minutes = j0 - i0 + 1
            change = cls[j0][1] - cls[i0][1]
            if kind == 'R' and minutes >= MIN_REFILL_MIN and change >= MIN_EPISODE_PSI:
                after = [v for _, v, c in cls[j + 1:j + 1 + PLATEAU_MIN] if c == 'S']
                refills.append({'start': cls[i0][0], 'end': cls[j0][0], 'minutes': minutes,
                                'from_psi': round(cls[i0][1], 2), 'to_psi': round(cls[j0][1], 2),
                                'rise_psi_h': round(change * 60 / minutes, 2),
                                'plateau_psi': round(statistics.median(after), 2) if len(after) >= 5 else None})
            elif kind == 'F' and minutes >= MIN_DRAW_MIN and -change >= MIN_EPISODE_PSI:
                draws.append({'start': cls[i0][0], 'end': cls[j0][0], 'minutes': minutes,
                              'drop_psi': round(-change, 2), 'to_psi': round(cls[j0][1], 2)})
            i = j + 1
    return {'refills': refills, 'draws': draws, 'slope_limit_psi_h': round(limit, 3)}


def bleed(rows, eps):
    """Idle overnight bleed per night (psi/h, Theil-Sen), only on nights with no refill or draw."""
    busy = [(e['start'] - 600, e['end'] + 600) for e in eps['refills'] + eps['draws']]
    nights = {}
    for t, v in rows:
        lt = local(t)
        if NIGHT[0] <= lt.hour < NIGHT[1] and not any(a <= t <= b for a, b in busy):
            nights.setdefault(lt.strftime('%Y-%m-%d'), []).append((t, v))
    out = {}
    for day, pts in sorted(nights.items()):
        if len(pts) < MIN_NIGHT_MIN or any(a <= pts[-1][0] and b >= pts[0][0] for a, b in busy):
            continue
        sample = pts[::max(1, len(pts) // 120)]
        slopes_ = [(y2 - y1) / (x2 - x1) for i, (x1, y1) in enumerate(sample) for x2, y2 in sample[i + 1:] if x2 != x1]
        if slopes_:
            out[day] = round(statistics.median(slopes_) * 3600, 3)
    return out


def draw_classes(draws):
    """Small and large draws (design R1 §4: ATO-like small draws, tap-like larger draws), split where this
    tank's own draw sizes fall into two groups (two-means on the psi drop). Sizes only, in psi: what made a
    draw is never claimed. One class when the sizes do not separate."""
    drops = sorted(d['drop_psi'] for d in draws)
    if len(drops) < MIN_CLASS_DRAWS:
        return {'status': 'LEARNING', 'draws': len(drops)}
    best = None
    for k in range(1, len(drops)):
        lo, hi = drops[:k], drops[k:]
        ml, mh = statistics.mean(lo), statistics.mean(hi)
        cost = sum((x - ml) ** 2 for x in lo) + sum((x - mh) ** 2 for x in hi)
        if best is None or cost < best[0]:
            best = (cost, k, ml, mh)
    _, k, ml, mh = best
    if ml <= 0 or mh / ml < CLASS_RATIO:
        return {'status': 'ONE_CLASS', 'draws': len(drops), 'median_drop_psi': round(statistics.median(drops), 2)}
    return {'status': 'TWO_CLASSES', 'draws': len(drops), 'split_psi': round((drops[k - 1] + drops[k]) / 2, 2),
            'small': {'count': k, 'median_drop_psi': round(statistics.median(drops[:k]), 2)},
            'large': {'count': len(drops) - k, 'median_drop_psi': round(statistics.median(drops[k:]), 2)},
            'note': 'Sizes in psi only; which device or tap made a draw is not known.'}


def summary(rows, now):
    """Per local day: refills, draws and bleed; plus the latest learned medians (shown, never alerted)."""
    eps = episodes(rows)
    nights = bleed(rows, eps)
    days = {}
    for e in eps['refills']:
        d = days.setdefault(local(e['start']).strftime('%Y-%m-%d'), {'refills': [], 'draws': []})
        d['refills'].append(e)
    for e in eps['draws']:
        d = days.setdefault(local(e['start']).strftime('%Y-%m-%d'), {'refills': [], 'draws': []})
        d['draws'].append(e)
    table = {}
    for day, d in sorted(days.items())[-30:]:
        r = d['refills']
        table[day] = {'refills': len(r), 'draws': len(d['draws']),
                      'refill_minutes_median': statistics.median(x['minutes'] for x in r) if r else None,
                      'rise_psi_h_median': statistics.median(x['rise_psi_h'] for x in r) if r else None,
                      'plateau_psi_median': statistics.median(x['plateau_psi'] for x in r if x['plateau_psi'] is not None)
                      if any(x['plateau_psi'] is not None for x in r) else None,
                      'draw_psi_total': round(sum(x['drop_psi'] for x in d['draws']), 2),
                      'night_bleed_psi_h': nights.get(day)}
    recent = [e for e in eps['refills'] if e['start'] >= now - 14 * 86400]
    classes = draw_classes([d for d in eps['draws'] if d['start'] >= now - 14 * 86400])
    if classes['status'] == 'TWO_CLASSES':
        for day, d in days.items():
            if day in table:
                table[day]['small_draws'] = sum(x['drop_psi'] < classes['split_psi'] for x in d['draws'])
                table[day]['large_draws'] = sum(x['drop_psi'] >= classes['split_psi'] for x in d['draws'])
    return {'days': table, 'nights': nights, 'slope_limit_psi_h': eps['slope_limit_psi_h'],
            'learned': {'refills_14d': len(recent),
                        'refill_minutes_median': statistics.median(e['minutes'] for e in recent) if recent else None,
                        'rise_psi_h_median': statistics.median(e['rise_psi_h'] for e in recent) if recent else None,
                        'plateau_psi_median': statistics.median(e['plateau_psi'] for e in recent if e['plateau_psi'] is not None)
                        if any(e['plateau_psi'] is not None for e in recent) else None},
            'draw_classes': classes,
            'gallons': 'NOT_AVAILABLE',
            'gallons_why': 'Needs the P0 bench values (membrane rating, precharge, shut-off fraction) and a second '
                           'calibration point; never derived from runtime or ratio.',
            'episodes': eps}
