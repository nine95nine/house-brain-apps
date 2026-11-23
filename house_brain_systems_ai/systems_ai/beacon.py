"""0.3.19 version beacon: which App runs which version, on the family line the AI connectors already read.

Brief C (front desk, 2026-11-18; Energy AI 0.1.9 release review run 6, findings C1 and H3): a release could be
confirmed by machine only for Apps with their own status sensor, and "app health failed" did not say which App.
Systems AI already reads every House Brain App's Supervisor info (installed version) and, for the /health
publishers, house_brain_app_health.v1 (running version). This module keeps one small row per App in the App's own
store and renders a bounded fragment for the digest text:

    apps: Systems 0.3.22, Energy 0.1.9 (running 0.1.8), Cooling 0.3.0 failed, +2 more

* 0.3.22: Apps whose version changed in the last 72 h (newest first, with their status word), then every App not
  OK now. (0.3.19-0.3.21 listed any version or status change within 24 h, not-OK first.)
* The fragment is fitted to the room the rest of the line leaves: whole entries only, then "+N more"; nothing when
  not even that fits. It never shortens any other part of the line (the caller passes the room).
* Versions are printed only when they match the health schema's version pattern, else "?"; names come from a fixed
  map, else the slug's own lower-case letters. Nothing else from another App reaches the exported text.
* The rows survive a restart (store key "beacon"), so Systems AI's own update is the first change it reports. On
  the first start with a beacon, Apps are seeded from the existing last:app_version: keys.

* 0.3.23 (#942 7080080195 P1; audit 7079933085 A1, A2, S2): the status word follows the problem list. A FAILED that
  Systems AI grades as a short blip (runtime.failed_is_transient, 0.3.21) prints "degraded", like its WARNING; an
  App seen started at least once and now stopped prints "stopped" whatever its boot setting (display only: no new
  problem, severity or proposal); a not-OK entry adds " since HH:MMZ" (UTC; " since MM-DD" after 24 h) when that
  costs no entry.

Read only: no new route, field, schema or entity. The rows are not exported as such; only the fragment text is.
"""
import math
import re
import time

from .app_health import VERSION_RE

STORE_KEY = 'beacon'
# 0.3.22 (review run 9, owner pop-ups 2026-11-21 "All of them", "N1 takes 0.3.22"): versions first, for 72 h
# (the 2026-11-18 readback missed a 24 h window during a 64 h account pause); "status changed, OK now" is no
# longer listed (it crowded out the version news live: "apps 24h: Pool 0.1.1, Cooling 0.3.0, +4 more").
VERSION_WINDOW_S = 3 * 86400
MAX_ROWS = 64
SELF = 'house_brain_systems_ai'
PREFIX = 'apps: '
NAMES = {'house_brain_energy_ai': 'Energy', 'house_brain_maintenance': 'Maintenance', 'house_brain_deployer': 'Deployer',
         'house_brain_ai_access': 'AI Access', 'house_brain_engineering_inspector': 'Inspector',
         'house_brain_engineering_observer': 'Observer', 'house_brain_maintenance_inventory_scout': 'Scout',
         'house_brain_cooling_ai': 'Cooling', 'house_brain_water_ai': 'Water',
         'house_brain_house_health_ai': 'House Health', 'house_brain_pool_ai': 'Pool', 'house_brain_tang': 'Tang',
         'house_brain_dashboard_steward': 'Steward',
         SELF: 'Systems'}
RE_NAME = re.compile(r'house_brain_[a-z0-9_]{1,48}')
RE_SLUG_NAME = re.compile(r'(?:local|[0-9a-f]{8})_(house_brain_[a-z0-9_]{1,48})')
SINCE_DAY_S = 86400
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


def status_of(row, always_on=(), seen_started=False, transient=False):
    """One status word per App: its own /health word when it publishes one; else from the Supervisor state, with
    the same rules as the App problems (error; stopped with Start on boot; an always-on App stopped).

    0.3.23: a FAILED the problem list grades as a blip (transient) is DEGRADED here too, and an App seen started
    before (seen_started) is STOPPED when it is stopped now, whatever its boot setting."""
    state = row.get('state')
    if state == 'UNREADABLE':
        return 'UNREADABLE'
    health = row.get('health')
    if state == 'started' and health in STATUS_WORDS:
        if health == 'DEGRADED' and row.get('health_accepted') is True:
            return 'OK'                  # 0.3.25: an owner-accepted reason (runtime.ACCEPTED_HEALTH_REASONS) is not news
        return 'DEGRADED' if health == 'FAILED' and transient is True else health
    if state == 'error':
        return 'ERROR'
    if state not in ('started', None) and (row.get('boot') == 'auto' or row.get('name') in always_on
                                           or seen_started is True):
        return 'STOPPED'
    return 'OK'


def update(store, apps, now, self_version, seed=None, always_on=(), transient=(), ever_started=()):
    """Fold this cycle's App view into the stored rows; return the rows. apps = runtime view['apps'] (slug -> row).

    0.3.23: transient = App names whose FAILED is a blip this cycle (runtime.failed_is_transient); ever_started =
    slugs the change log shows started, used once for a row written before 0.3.23 (no seen_started yet)."""
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
        legacy = prev is None or 'seen_started' not in prev
        was_up = (prev or {}).get('seen_started') is True or (legacy and slug in ever_started)
        status = status_of(row, always_on, seen_started=was_up, transient=name in transient)
        seen_started = was_up or row.get('state') == 'started'
        if status == 'OK':
            bad_since = None
        elif prev is not None and prev.get('status') not in (None, 'OK'):
            bad_since = prev.get('bad_since', prev.get('changed_at'))   # still not OK: keep when it went bad
        else:
            bad_since = now
        if prev is None and first and seed:
            prev = {'version': version(seed.get(slug)), 'status': 'OK', 'changed_at': None, 'version_at': None}
        if prev is None:
            changed = None if first else now         # an App installed while Systems AI runs is a change
            version_at = changed
        else:
            moved = prev.get('version') != installed and prev.get('version') is not None
            changed = now if moved or prev.get('status') != status else prev.get('changed_at')
            version_at = now if moved else prev.get('version_at')     # rows from 0.3.19-0.3.21 have none
        seen[name] = {'version': installed, 'running': running, 'status': status, 'changed_at': changed,
                      'version_at': version_at, 'seen_started': seen_started, 'bad_since': bad_since}
    prev = old.get(SELF) if isinstance(old.get(SELF), dict) else None
    mine = version(self_version)
    at = now if prev is None or prev.get('version') != mine else prev.get('version_at')
    seen[SELF] = {'version': mine, 'running': mine, 'status': 'OK', 'changed_at': at, 'version_at': at,
                  'seen_started': True, 'bad_since': None}
    rows = dict(sorted(seen.items())[:MAX_ROWS])
    if rows != old:
        store.put(STORE_KEY, rows)
    return rows


def _at(value, now):
    return min(float(value), now) if isinstance(value, (int, float)) and math.isfinite(value) else None


def entries(rows, now):
    """Rows to show, best first: a version changed within 72 h (newest first, any status), then not OK now
    (newest change first). An App whose status changed and is OK again is not listed (0.3.22)."""
    out = []
    for name, r in (rows or {}).items():
        if not isinstance(r, dict):
            continue
        v_at = _at(r.get('version_at'), now)
        if v_at is not None and now - v_at <= VERSION_WINDOW_S:
            out.append((0, -v_at, name, r))
        elif r.get('status') != 'OK':
            out.append((1, -(_at(r.get('changed_at'), now) or 0), name, r))
    return [(name, r) for _, _, name, r in sorted(out, key=lambda e: e[:3])]


def since(r, now):
    """0.3.23: " since HH:MMZ" (UTC) for a not-OK row that went bad in the last 24 h, " since MM-DD" before; else ''."""
    at = _at(r.get('bad_since'), now)
    if r.get('status') == 'OK' or at is None or at <= 0:
        return ''
    t = time.gmtime(at)
    return time.strftime(' since %H:%MZ' if now - at < SINCE_DAY_S else ' since %m-%d', t)


def text(name, r, now=None):
    v = version(r.get('version'))
    run = version(r.get('running'))
    word = f"{label(name)} {v or '?'}"
    if run and v and run != v:
        word += f" (running {run})"
    status = r.get('status')
    if status != 'OK':
        word += ' ' + (status.lower() if status in STATUS_WORDS else 'unknown')
        if now is not None:
            word += since(r, now)
    return word


def _fit(shown, room):
    """(line, entries kept): whole entries, then "+N more"; ('', -1) when not even that fits."""
    for keep in range(len(shown), -1, -1):
        parts = shown[:keep] + ([f"+{len(shown) - keep} more"] if keep < len(shown) else [])
        line = PREFIX + ', '.join(parts)
        if len(line) <= room:
            return line, keep
    return '', -1


def fragment(rows, now, room):
    """The fragment, at most `room` characters: whole entries, then "+N more"; '' when nothing fits or is due.
    0.3.23: the " since" times are added only when they cost no whole entry."""
    due = entries(rows, now)
    if not due or room <= 0:
        return ''
    plain, kept = _fit([text(n, r) for n, r in due], room)
    timed, kept_timed = _fit([text(n, r, now) for n, r in due], room)
    return timed if kept_timed >= kept and kept_timed >= 0 else plain
