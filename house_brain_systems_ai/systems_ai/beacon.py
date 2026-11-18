"""0.3.19 version beacon: which App runs which version, on the family line the AI connectors already read.

Brief C (front desk, 2026-11-18; Energy AI 0.1.9 release review run 6, findings C1 and H3): a release could be
confirmed by machine only for Apps with their own status sensor, and "app health failed" did not say which App.
Systems AI already reads every House Brain App's Supervisor info (installed version) and, for the /health
publishers, house_brain_app_health.v1 (running version). This module keeps one small row per App in the App's own
store and renders a bounded fragment for the digest text:

    apps 24h: Systems 0.3.19, Cooling 0.3.0 failed, Energy 0.1.9 (running 0.1.8), +2 more

* Apps whose version or status changed in the last 24 h, plus every App not OK now (any age), not-OK first, then
  newest change first.
* The fragment is fitted to the room the rest of the line leaves: whole entries only, then "+N more"; nothing when
  not even that fits. It never shortens any other part of the line (the caller passes the room).
* Versions are printed only when they match the health schema's version pattern, else "?"; names come from a fixed
  map, else the slug's own lower-case letters. Nothing else from another App reaches the exported text.
* The rows survive a restart (store key "beacon"), so Systems AI's own update is the first change it reports. On
  the first start with a beacon, Apps are seeded from the existing last:app_version: keys.

Read only: no new route, field, schema or entity. The rows are not exported as such; only the fragment text is.
"""
import math
import re

from .app_health import VERSION_RE

STORE_KEY = 'beacon'
WINDOW_S = 86400
MAX_ROWS = 64
SELF = 'house_brain_systems_ai'
PREFIX = 'apps 24h: '
NAMES = {'house_brain_energy_ai': 'Energy', 'house_brain_maintenance': 'Maintenance', 'house_brain_deployer': 'Deployer',
         'house_brain_ai_access': 'AI Access', 'house_brain_engineering_inspector': 'Inspector',
         'house_brain_engineering_observer': 'Observer', 'house_brain_maintenance_inventory_scout': 'Scout',
         'house_brain_cooling_ai': 'Cooling', 'house_brain_water_ai': 'Water',
         'house_brain_house_health_ai': 'House Health', 'house_brain_pool_ai': 'Pool', 'house_brain_tang': 'Tang',
         SELF: 'Systems'}
RE_NAME = re.compile(r'house_brain_[a-z0-9_]{1,48}')
RE_SLUG_NAME = re.compile(r'(?:local|[0-9a-f]{8})_(house_brain_[a-z0-9_]{1,48})')
STATUS_WORDS = ('OK', 'DEGRADED', 'FAILED', 'STALE', 'INVALID', 'UNREACHABLE', 'ERROR', 'STOPPED', 'UNREADABLE')


def version(value):
    return value if isinstance(value, str) and VERSION_RE.fullmatch(value) else None


def label(name):
    """Fixed short name; an App outside the map shows its own slug letters (bounded), never free text."""
    if name in NAMES:
        return NAMES[name]
    if isinstance(name, str) and RE_NAME.fullmatch(name):
        return name[len('house_brain_'):][:20]
    return 'app'


def name_of(subject):
    """The App name inside a Supervisor slug (local_house_brain_x, 1a2b3c4d_house_brain_x), else None."""
    m = RE_SLUG_NAME.fullmatch(subject) if isinstance(subject, str) else None
    return m.group(1) if m else None


def status_of(row, always_on=()):
    """One status word per App: its own /health word when it publishes one; else from the Supervisor state, with
    the same rules as the App problems (error; stopped with Start on boot; an always-on App stopped)."""
    state = row.get('state')
    if state == 'UNREADABLE':
        return 'UNREADABLE'
    health = row.get('health')
    if state == 'started' and health in STATUS_WORDS:
        return health
    if state == 'error':
        return 'ERROR'
    if state not in ('started', None) and (row.get('boot') == 'auto' or row.get('name') in always_on):
        return 'STOPPED'
    return 'OK'


def update(store, apps, now, self_version, seed=None, always_on=()):
    """Fold this cycle's App view into the stored rows; return the rows. apps = runtime view['apps'] (slug -> row)."""
    now = float(now) if isinstance(now, (int, float)) and math.isfinite(now) else 0.0
    old = store.get(STORE_KEY) or {}
    old = old if isinstance(old, dict) else {}
    first = not old
    seen = {}
    for slug, row in sorted((apps or {}).items()):
        name = row.get('name')
        if not isinstance(name, str) or not RE_NAME.fullmatch(name) or name in seen:
            continue
        prev = old.get(name) if isinstance(old.get(name), dict) else None
        installed = version(row.get('version'))
        if installed is None and row.get('state') == 'UNREADABLE' and prev:
            installed = prev.get('version')         # an unreadable info keeps the last known version
        running = version((row.get('running_version')))
        status = status_of(row, always_on)
        if prev is None and first and seed:
            prev = {'version': version(seed.get(slug)), 'status': 'OK', 'changed_at': None}
        if prev is None:
            changed = None if first else now         # an App installed while Systems AI runs is a change
        elif prev.get('version') != installed and prev.get('version') is not None or prev.get('status') != status:
            changed = now
        else:
            changed = prev.get('changed_at')
        seen[name] = {'version': installed, 'running': running, 'status': status, 'changed_at': changed}
    prev = old.get(SELF) if isinstance(old.get(SELF), dict) else None
    mine = version(self_version)
    seen[SELF] = {'version': mine, 'running': mine, 'status': 'OK',
                  'changed_at': now if prev is None or prev.get('version') != mine else prev.get('changed_at')}
    rows = dict(sorted(seen.items())[:MAX_ROWS])
    if rows != old:
        store.put(STORE_KEY, rows)
    return rows


def entries(rows, now):
    """Rows to show, best first: not OK now (any age), then changed within 24 h, newest first."""
    out = []
    for name, r in (rows or {}).items():
        if not isinstance(r, dict):
            continue
        at = r.get('changed_at')
        at = min(float(at), now) if isinstance(at, (int, float)) and math.isfinite(at) else None
        bad = r.get('status') != 'OK'
        if bad or (at is not None and now - at <= WINDOW_S):
            out.append((not bad, -(at or 0), name, r))
    return [(name, r) for _, _, name, r in sorted(out, key=lambda e: e[:3])]


def text(name, r):
    v = version(r.get('version'))
    run = version(r.get('running'))
    word = f"{label(name)} {v or '?'}"
    if run and v and run != v:
        word += f" (running {run})"
    status = r.get('status')
    if status != 'OK':
        word += ' ' + (status.lower() if status in STATUS_WORDS else 'unknown')
    return word


def fragment(rows, now, room):
    """The fragment, at most `room` characters: whole entries, then "+N more"; '' when nothing fits or is due."""
    shown = [text(n, r) for n, r in entries(rows, now)]
    if not shown or room <= 0:
        return ''
    for keep in range(len(shown), -1, -1):
        parts = shown[:keep] + ([f"+{len(shown) - keep} more"] if keep < len(shown) else [])
        line = PREFIX + ', '.join(parts)
        if len(line) <= room:
            return line
    return ''
