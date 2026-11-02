"""World Change Radar (Systems AI 0.2.1; design R2 §2, owner pop-ups 2026-10-31 "In Systems AI", "Approve design",
2026-11-01 "Yes, build 0.2.1", "Both now, GitHub unproven").

Public security news that touches something this house runs, and nothing else:

- CISA Known Exploited Vulnerabilities (KEV): vulnerabilities attackers are using now;
- GitHub security advisories for Home Assistant Core, Home Assistant OS, the Supervisor and ESPHome.

Rules (each from a defect found in the earlier P2 code, verified against live data on 2026-10-31):

- A body is judged by parsing it, never by its content type; one that does not parse is a failed read.
- No field-length limit (a real KEV product name is 179 characters); only the whole body is bounded.
- A failed, refused or unparsable read keeps the last good result with its age. Nothing turns a failure
  into "no threats", and a source never read says "not checked".
- An unknown installed version is UNKNOWN_VERSION, never "not affected".

Budget: reads only at 03:20 and 15:20 Phoenix time, at most 2 attempts per source per local day (a failed
read is retried once, 30 minutes later, from the same 2), one source per cycle, so at most 10 outside
requests a day. Every attempt is saved before it is sent, so a restart never repeats one. Nothing is read
while the kill switch holds Systems AI. $0: no key, no paid API.

Nothing here acts. A finding can only suggest the matching update, which stays the owner's tap in
Maintenance.
"""
import json
import re
from datetime import date, datetime, timedelta, timezone

PHOENIX = timezone(timedelta(hours=-7))
SLOTS = ((3, 20), (15, 20))
ATTEMPTS_PER_DAY = 2
RETRY_S = 1800
STALE_S = 36 * 3600
KEV_RECENT_DAYS = 30
MAX_FINDINGS = 10
SHOW = 25

KEV = 'kev'
# Watched GitHub repositories -> the component whose installed version they are matched to.
REPOS = {'home-assistant/core': 'core', 'home-assistant/operating-system': 'os',
         'home-assistant/supervisor': 'supervisor', 'esphome/esphome': 'esphome'}
SOURCES = (KEV,) + tuple('gh:' + r for r in REPOS)
COMPONENTS = {'core': 'Home Assistant Core', 'os': 'Home Assistant OS', 'supervisor': 'Supervisor',
              'esphome': 'ESPHome'}
# Supervisor /info keys for each component. 0.2.2: 'esphome' is the ESPHome add-on's own version, which runtime.py
# reads from its Supervisor info and adds under that key.
VERSION_KEYS = {'core': 'homeassistant', 'os': 'hassos', 'supervisor': 'supervisor', 'esphome': 'esphome'}
# Package names that belong to one component. A range for another component's package is never compared with this
# component's version (Core's '< 2023.9.0' says nothing about OS 16.2); a range with no package name is used as is.
PACKAGES = {'core': frozenset({'homeassistant'}), 'esphome': frozenset({'esphome'})}
# KEV (vendorProject, product) -> component, lower case; '*' is any product. Reviewed in the repository:
# a missing alias is a missed match, so the page always shows "N known, M match this house".
KEV_ALIASES = {('home assistant', '*'): 'core', ('nabu casa', '*'): 'core', ('esphome', '*'): 'esphome',
               ('linux', 'kernel'): 'os'}
RE_CVE = re.compile(r'CVE-[0-9]{4}-[0-9]{4,}')
RE_GHSA = re.compile(r'GHSA(?:-[23456789cfghjmpqrvwx]{4}){3}')
RE_DAY = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
RE_VERSION = re.compile(r'v?([0-9]+(?:\.[0-9]+){0,3})(?:[-.]?(a|b|rc|dev)([0-9]*))?')
RE_CLAUSE = re.compile(r'\s*(<=|>=|==|<|>|=)?\s*(\S+)\s*')
PRE = {'dev': 0, 'a': 1, 'b': 2, 'rc': 3}


class ParseError(ValueError):
    pass


# -- versions ------------------------------------------------------------------------------------
def version(text):
    """A comparable key for '2026.10.2', '16.2', '2025.10.0b1'; None for anything else."""
    m = RE_VERSION.fullmatch(text.strip()) if isinstance(text, str) else None
    if not m:
        return None
    nums = tuple(int(n) for n in m.group(1).split('.'))
    nums += (0,) * (4 - len(nums))
    pre = (PRE[m.group(2)], int(m.group(3) or 0)) if m.group(2) else (9, 0)
    return nums + pre


def in_range(installed, expression):
    """True / False when the installed version is / is not in a GitHub range like '>= 0.95, < 2021.1.5';
    None when either cannot be read."""
    have = version(installed)
    if have is None or not isinstance(expression, str) or not expression.strip():
        return None
    result = True
    for clause in expression.split(','):
        m = RE_CLAUSE.fullmatch(clause)
        bound = version(m.group(2)) if m else None
        if bound is None:
            return None
        op = m.group(1) or '='
        result &= {'<': have < bound, '<=': have <= bound, '>': have > bound, '>=': have >= bound,
                   '=': have == bound, '==': have == bound}[op]
    return result


# -- parsers (real captured snapshots in tests/house_brain_systems_ai/snapshots) ------------------
def parse_kev(raw):
    """The KEV catalogue -> (catalog version, entry count, entries that match a component alias)."""
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError) as error:
        raise ParseError('KEV_NOT_JSON') from error
    rows = doc.get('vulnerabilities') if isinstance(doc, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ParseError('KEV_NO_VULNERABILITIES')
    matched, bad = [], 0
    for row in rows:
        if not (isinstance(row, dict) and isinstance(row.get('cveID'), str) and RE_CVE.fullmatch(row['cveID'])
                and isinstance(row.get('vendorProject'), str) and isinstance(row.get('product'), str)
                and isinstance(row.get('dateAdded'), str) and RE_DAY.fullmatch(row['dateAdded'])):
            bad += 1
            continue
        component = kev_component(row['vendorProject'], row['product'])
        if component:
            matched.append({'id': row['cveID'], 'component': component, 'vendor': row['vendorProject'],
                            'product': row['product'], 'name': str(row.get('vulnerabilityName') or ''),
                            'added': row['dateAdded'],
                            'ransomware': str(row.get('knownRansomwareCampaignUse') or 'Unknown')})
    if bad > max(5, len(rows) // 100):
        raise ParseError('KEV_TOO_MANY_BAD_ROWS')
    return {'catalog': str(doc.get('catalogVersion') or ''), 'known': len(rows) - bad, 'skipped': bad,
            'matched': matched}


def kev_component(vendor, product):
    v, p = vendor.strip().lower(), product.strip().lower()
    return KEV_ALIASES.get((v, p)) or KEV_ALIASES.get((v, '*'))


def parse_advisories(raw):
    """A GitHub repository security-advisories page -> published, not withdrawn advisories."""
    try:
        rows = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError) as error:
        raise ParseError('ADVISORIES_NOT_JSON') from error
    if not isinstance(rows, list):
        raise ParseError('ADVISORIES_NOT_A_LIST')
    out, bad = [], 0
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('ghsa_id'), str) or not RE_GHSA.fullmatch(row['ghsa_id']):
            bad += 1
            continue
        if row.get('state') not in (None, 'published') or row.get('withdrawn_at'):
            continue
        vulns = []
        for v in row.get('vulnerabilities') or []:
            if isinstance(v, dict):
                package = v.get('package') if isinstance(v.get('package'), dict) else {}
                vulns.append({'package': package.get('name').strip().lower() if isinstance(package.get('name'), str) else None,
                              'range': v.get('vulnerable_version_range') if isinstance(v.get('vulnerable_version_range'), str) else None,
                              'patched': v.get('patched_versions') if isinstance(v.get('patched_versions'), str) else None})
        cve = row.get('cve_id') if isinstance(row.get('cve_id'), str) and RE_CVE.fullmatch(row['cve_id']) else None
        out.append({'id': row['ghsa_id'], 'cve': cve, 'summary': str(row.get('summary') or ''),
                    'severity': row.get('severity') if row.get('severity') in ('critical', 'high', 'medium', 'low') else None,
                    'published': str(row.get('published_at') or ''), 'vulns': vulns})
    if rows and bad == len(rows):
        raise ParseError('ADVISORY_SHAPE')
    return out


def _patched_out(installed, patched, hit):
    """0.3.2 (live 2026-11-02): GitHub ranges can be open-ended (ESPHome GHSA-4h3h-63v6-88qx: affected '>=2025.9.0',
    patched '2025.12.7'). An installed version at or past the single patched version is not affected."""
    if hit is not True or not isinstance(patched, str):      # 'a, b' never parses as one version: stays a hit
        return hit
    fixed, have = version(patched), version(installed)
    return False if fixed is not None and have is not None and have >= fixed else hit


def advisory_verdict(advisory, installed, component=None):
    """TOUCHES_YOU, NOT_AFFECTED or UNKNOWN_VERSION (also when no range for this component can be read)."""
    if installed is None or version(installed) is None:
        return 'UNKNOWN_VERSION', None
    others = set().union(*(names for c, names in PACKAGES.items() if c != component)) if component else set()
    answers = [(_patched_out(installed, v['patched'], in_range(installed, v['range'])), v['patched'])
               for v in advisory['vulns']
               if v.get('package') is None or v['package'] in PACKAGES.get(component, ()) or v['package'] not in others]
    hit = [p for a, p in answers if a is True]
    if hit:
        return 'TOUCHES_YOU', hit[0]
    if answers and all(a is False for a, _ in answers):
        return 'NOT_AFFECTED', None
    return 'UNKNOWN_VERSION', None


def local_day(now):
    return datetime.fromtimestamp(now, PHOENIX).date().isoformat()


class Radar:
    def __init__(self, store, client=None):
        self.store = store
        self.client = client
        self.state = store.get('radar:state', {'sources': {}, 'versions': {}})
        # KEV matches once raised stay raised until that component is updated: ageing past 30 days never closes
        # a case as if it were fixed.
        self.state.setdefault('raised', [])

    # -- schedule and budget --------------------------------------------------------------------
    def _src(self, name):
        return self.state['sources'].setdefault(name, {'day': None, 'attempts': 0, 'slots': [], 'retry_at': None,
                                                      'last_attempt_at': None, 'last_error': None,
                                                      'last_good_at': None, 'first_good_at': None,
                                                      'result': None})

    def due(self, now):
        """The one source to read now, or None."""
        if self.client is None:
            return None
        local = datetime.fromtimestamp(now, PHOENIX)
        today = local.date().isoformat()
        slots = [f'{h:02}:{m:02}' for h, m in SLOTS if (local.hour, local.minute) >= (h, m)]
        for name in SOURCES:
            s = self._src(name)
            if s['day'] != today:
                s.update(day=today, attempts=0, slots=[], retry_at=None)
            if s['attempts'] >= ATTEMPTS_PER_DAY:
                continue
            if (slots and slots[-1] not in s['slots']) or (s['retry_at'] is not None and now >= s['retry_at']):
                return name
        return None

    def step(self, now):
        """At most one outside request. The attempt is saved before it is sent."""
        name = self.due(now)
        if name is None:
            return None
        s = self._src(name)
        local = datetime.fromtimestamp(now, PHOENIX)
        slots = [f'{h:02}:{m:02}' for h, m in SLOTS if (local.hour, local.minute) >= (h, m)]
        s['attempts'] += 1
        s['slots'] = sorted(set(s['slots']) | set(slots))
        s['retry_at'] = None
        s['last_attempt_at'] = now
        self.store.put('radar:state', self.state)
        try:
            if name == KEV:
                raw, more = self.client.kev(), False
                result = parse_kev(raw)
            else:
                raw, more = self.client.advisories(name[3:])
                result = {'advisories': parse_advisories(raw), 'partial': bool(more)}
        except Exception as error:  # noqa: BLE001 - a failed read keeps the last good result
            s['last_error'] = str(error)[:80] or type(error).__name__
            if s['attempts'] < ATTEMPTS_PER_DAY:
                s['retry_at'] = now + RETRY_S
            self.store.put('radar:state', self.state)
            return {'source': name, 'ok': False, 'error': s['last_error']}
        s.update(result=result, last_good_at=now, last_error=None)
        s['first_good_at'] = s['first_good_at'] or now
        self.store.put('radar:state', self.state)
        return {'source': name, 'ok': True}

    # -- installed versions -----------------------------------------------------------------------
    def observe_versions(self, info, now):
        """Supervisor /info -> installed versions, and since when (None while unknown)."""
        for component, key in VERSION_KEYS.items():
            value = info.get(key) if isinstance(info, dict) else None
            if not isinstance(value, str):
                continue
            known = self.state['versions'].get(component)
            if known is None:
                self.state['versions'][component] = {'version': value, 'since': None}
            elif known['version'] != value:
                self.state['versions'][component] = {'version': value, 'since': now}
        self.store.put('radar:state', self.state)

    def forget(self, component):
        """The component is no longer found: its version is unknown again, never kept from before."""
        if self.state['versions'].pop(component, None) is not None:
            self.store.put('radar:state', self.state)

    def installed(self, component):
        row = self.state['versions'].get(component)
        return row['version'] if row else None

    # -- findings ---------------------------------------------------------------------------------
    def findings(self, now):
        out, ghsa_by_cve = [], {}
        for repo, component in REPOS.items():
            result = self._src('gh:' + repo)['result']
            for a in (result or {}).get('advisories', []):
                verdict, fixed = advisory_verdict(a, self.installed(component), component)
                row = {'id': a['id'], 'cve': a['cve'], 'source': 'GitHub ' + repo, 'component': component,
                       'state': verdict, 'fixed_in': fixed, 'severity': a['severity'], 'title': a['summary'],
                       'date': a['published'][:10]}
                out.append(row)
                if a['cve']:
                    ghsa_by_cve[a['cve']] = row
        kev = self._src(KEV)['result'] or {}
        today = date.fromisoformat(local_day(now))
        for k in kev.get('matched', []):
            installed = self.installed(k['component'])
            since = (self.state['versions'].get(k['component']) or {}).get('since')
            added = date.fromisoformat(k['added'])
            linked = ghsa_by_cve.get(k['id'])
            if installed is None:
                state = 'UNKNOWN_VERSION'
            elif linked is not None and linked['state'] == 'NOT_AFFECTED':
                state = 'NOT_AFFECTED'
            elif since is not None and date.fromisoformat(local_day(since)) > added:
                state = 'UPDATED_SINCE'          # updated after it was listed: check the vendor note
            elif (today - added).days > KEV_RECENT_DAYS and k['id'] not in self.state['raised']:
                state = 'OLDER_MATCH'
            else:
                state = 'TOUCHES_YOU'
            out.append({'id': k['id'], 'cve': k['id'], 'source': 'CISA KEV', 'component': k['component'],
                        'state': state, 'fixed_in': (linked or {}).get('fixed_in'), 'severity': None,
                        'title': k['name'] or f"{k['vendor']} {k['product']}", 'date': k['added'],
                        'ransomware': k['ransomware']})
        order = {'TOUCHES_YOU': 0, 'UNKNOWN_VERSION': 1, 'UPDATED_SINCE': 2, 'OLDER_MATCH': 3, 'NOT_AFFECTED': 4}
        out.sort(key=lambda f: f['date'], reverse=True)
        return sorted(out, key=lambda f: order[f['state']])

    def problems(self, now):
        out = {}
        found = self.findings(now)
        touching = [f for f in found if f['state'] == 'TOUCHES_YOU']
        raised = [f['id'] for f in touching if f['source'] == 'CISA KEV']
        cleared = {f['id'] for f in found if f['source'] == 'CISA KEV' and f['state'] in ('UPDATED_SINCE', 'NOT_AFFECTED')}
        keep = [i for i in self.state['raised'] if i not in cleared and i not in raised] + raised
        if keep != self.state['raised']:
            self.state['raised'] = keep[-200:]
            self.store.put('radar:state', self.state)
        touching.sort(key=lambda f: f['date'], reverse=True)
        for f in touching[:MAX_FINDINGS]:
            out['WORLD:' + f['id']] = {'severity': 'WARNING', 'reason': 'WORLD_CHANGE_TOUCHES_YOU',
                                       'subject': COMPONENTS[f['component']], 'owner': 'systems_ai'}
        stale = [n for n in SOURCES if self._stale(n, now)]
        if stale:
            out['WORLD:NOT_CHECKED'] = {'severity': 'INFO', 'reason': 'WORLD_CHANGE_NOT_CHECKED',
                                        'subject': f'{len(stale)} source(s)', 'owner': 'systems_ai'}
        return out

    def _stale(self, name, now):
        s = self._src(name)
        if s['last_attempt_at'] is None:
            return False                         # not yet due: "not checked yet", not a problem
        return s['last_good_at'] is None or now - s['last_good_at'] > STALE_S

    def report(self, now):
        found = self.findings(now)
        sources = {}
        for name in SOURCES:
            s = self._src(name)
            r = s['result'] or {}
            sources[name] = {
                'status': 'NOT_CHECKED_YET' if s['last_good_at'] is None and s['last_attempt_at'] is None
                else 'FAILED_NEVER_READ' if s['last_good_at'] is None
                else 'STALE' if now - s['last_good_at'] > STALE_S else 'OK',
                'proven_on_live_data': s['first_good_at'] is not None,
                'last_good_at': s['last_good_at'], 'last_attempt_at': s['last_attempt_at'],
                'last_error': s['last_error'], 'attempts_today': s['attempts'] if s['day'] == local_day(now) else 0,
                'known': r.get('known') if name == KEV else len(r.get('advisories', [])) if s['result'] else None,
                'partial': bool(r.get('partial'))}
        counts = {k: sum(f['state'] == k for f in found)
                  for k in ('TOUCHES_YOU', 'UNKNOWN_VERSION', 'UPDATED_SINCE', 'OLDER_MATCH', 'NOT_AFFECTED')}
        shown = [f for f in found if f['state'] != 'NOT_AFFECTED'][:SHOW]
        for f in shown:
            if f['state'] == 'TOUCHES_YOU':
                f['suggestion'] = f"Check Maintenance for a {COMPONENTS[f['component']]} update" + (
                    f" (fixed in {f['fixed_in']})" if f['fixed_in'] else '') + '; nothing is changed by Systems AI.'
        return {'sources': sources, 'counts': counts, 'findings': shown,
                'installed': {c: self.installed(c) for c in COMPONENTS},
                'budget': {'reads_per_day_max': ATTEMPTS_PER_DAY * len(SOURCES), 'slots': [f'{h:02}:{m:02}' for h, m in SLOTS],
                           'paid': False}}
