"""Systems AI cycles: read, learn, group, explain, propose. It never acts.

Fast cycle (60 s): Home Assistant states (kill switch, memory sensors, sibling status sensors).
Slow cycle (300 s): Supervisor info for the House Brain Apps, their /health blocks, the system log
and Repairs. Hourly: database size. Daily: statistic ids, forecast freeze and scoring, pruning. Every fast cycle
the archive keeper reads one entity-day of cooling history (archive.py), so at most one read a minute.
Proposals are counted in a shadow scorecard and offered read-only to Maintenance; only
Maintenance, after the owner's Approve, can carry one out.
"""
import re
import resource
import time
from datetime import datetime, timedelta, timezone

from . import __version__
from . import app_health
from .archive import Archive
from .incidents import Incidents
from .learn import trend, days_until, freeze, score, hourly
from .logs import LogLearner
from .store import Store
from .transport import TransportError

PHOENIX = timezone(timedelta(hours=-7))
SELF = 'house_brain_systems_ai'
RE_SELF = re.compile(r'([0-9a-f]{8})_house_brain_systems_ai')
# The House Brain Apps this overseer looks for (store prefix and local copies).
FAMILY = ('house_brain_energy_ai', 'house_brain_maintenance', 'house_brain_deployer', 'house_brain_ai_access',
          'house_brain_engineering_inspector', 'house_brain_engineering_observer',
          'house_brain_maintenance_inventory_scout')
# Apps that serve house_brain_app_health.v1 at GET /health on their ingress port.
HEALTH_PUBLISHERS = frozenset({'house_brain_energy_ai'})
APP_HEALTH_REASONS = {'DEGRADED': 'APP_HEALTH_DEGRADED', 'FAILED': 'APP_HEALTH_FAILED', 'STALE': 'APP_HEALTH_STALE',
                      'INVALID': 'APP_HEALTH_INVALID', 'UNREACHABLE': 'APP_HEALTH_UNREACHABLE'}
FAST_S, SLOW_S, HOURLY_S, DAY_S, INSTALLED_REFRESH_S = 60, 300, 3600, 86400, 3600
PROPOSE_AFTER_S = 900
PROPOSAL_TTL_S = 3600
MEMORY = {'free': 'sensor.system_monitor_memory_free', 'used_pct': 'sensor.system_monitor_memory_usage',
          'swap_pct': 'sensor.system_monitor_swap_usage',
          'pressure_full_pct': 'sensor.system_monitor_memory_pressure_full_300s_average',
          'last_boot': 'sensor.system_monitor_last_boot'}
SIBLING_STATUS = {'maintenance': 'sensor.house_brain_maintenance_status',
                  'deployer': 'sensor.house_brain_deployer_status',
                  'ai_activity': 'sensor.house_brain_ai_activity',
                  'ai_connectors': 'sensor.house_brain_ai_connectors'}
ARBITER_PHASE = 'input_select.cooling_arbiter_v2_phase'
LOW_FREE_MIB, HIGH_SWAP_PCT, HIGH_PRESSURE_PCT = 400, 80, 5
LOW_DISK_GB, DISK_FLOOR_GB = 5, 2
# S10: one shared list of readings no AI may trust (repository evidence; owner-reviewed source).
UNTRUSTED = [
    {'what': 'Ondilo pool temperature', 'why': 'reads -81 F; not a usable water temperature (#120/#158)'},
    {'what': 'Ondilo pH', 'why': 'untrusted until the probe is repaired or replaced (#120/#158)'},
    {'what': 'AquaRite CELL-5 production seed', 'why': 'quarantined; never a chemistry input'},
    {'what': 'Evap local dew point when sources disagree', 'why': 'the disagreement guard decides, not one source'},
    {'what': 'B-hyve Orbit volume when null', 'why': 'a null volume is a gap, never derived gallons'},
]
WAITING = {'S5_busiest_entities': 'WAITING_FOR_MAINTENANCE_RELAY (Maintenance already ranks them daily)',
           'S6_offline_evaluators': 'LATER_RELEASE',
           'S9_outside_liveness': 'NOT_VISIBLE_FROM_THE_PI (the outside check runs off the Pi)',
           'S13_tap_ledger': 'WAITING_FOR_MAINTENANCE_RELAY (approval history lives in Maintenance)'}


def number(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n == n and abs(n) < 1e15 else None


def mib(text):
    """Core's estimated_db_size text ("123.45 MiB", "7.7 GiB") in MiB; anything else None."""
    m = re.fullmatch(r'\s*([0-9]+(?:\.[0-9]+)?)\s*(KiB|MiB|GiB|TiB)\s*', str(text or ''))
    if not m:
        return None
    return float(m.group(1)) * {'KiB': 1 / 1024, 'MiB': 1, 'GiB': 1024, 'TiB': 1024 * 1024}[m.group(2)]


def app_problems(name, slug, info, observed):
    """Problems for one sibling App from its Supervisor info and its health block."""
    out = {}
    state, boot = info.get('state'), info.get('boot')
    if state == 'error':
        out[f'APP:{slug}:ERROR'] = {'severity': 'ERROR', 'reason': 'APP_STATE_ERROR', 'subject': slug,
                                    'owner': 'systems_ai', 'proposal': ('start_app', slug)}
    elif state != 'started' and boot == 'auto':
        out[f'APP:{slug}:STOPPED'] = {'severity': 'ERROR', 'reason': 'APP_STOPPED_WITH_START_ON_BOOT',
                                      'subject': slug, 'owner': 'systems_ai', 'proposal': ('start_app', slug)}
    if state == 'started' and observed is not None and observed['status'] != 'OK':
        status = observed['status']
        out[f'APP:{slug}:HEALTH'] = {
            'severity': 'WARNING' if status == 'DEGRADED' else 'ERROR',
            'reason': APP_HEALTH_REASONS[status], 'subject': slug, 'owner': 'systems_ai',
            'detail': observed['reasons'][:6],
            'proposal': ('restart_app', slug) if status in ('STALE', 'UNREACHABLE') or (
                status == 'FAILED' and 'CYCLE_OVERDUE' in observed['reasons']) else None}
    return out


def memory_problems(values):
    out = {}
    if values['free'] is None or values['swap_pct'] is None:
        out['MEM:SENSOR_MISSING'] = {'severity': 'INFO', 'reason': 'MEMORY_SENSOR_MISSING', 'subject': 'host',
                                     'owner': 'systems_ai'}
    if values['free'] is not None and values['free'] < LOW_FREE_MIB:
        out['MEM:LOW_FREE'] = {'severity': 'WARNING', 'reason': 'MEMORY_FREE_LOW', 'subject': 'host',
                               'owner': 'systems_ai'}
    if values['swap_pct'] is not None and values['swap_pct'] > HIGH_SWAP_PCT:
        out['MEM:SWAP_HIGH'] = {'severity': 'WARNING', 'reason': 'SWAP_USAGE_HIGH', 'subject': 'host',
                                'owner': 'systems_ai'}
    if values['pressure_full_pct'] is not None and values['pressure_full_pct'] > HIGH_PRESSURE_PCT:
        out['MEM:PRESSURE'] = {'severity': 'WARNING', 'reason': 'MEMORY_PRESSURE_FULL', 'subject': 'host',
                               'owner': 'systems_ai'}
    return out


def good_time_to_ask(states, now):
    """S12: advice only. A phone ask is best while the Arbiter is IDLE and outside quiet hours."""
    row = states.get(ARBITER_PHASE) if isinstance(states, dict) else None
    phase = row.get('state') if isinstance(row, dict) else None
    hour = datetime.fromtimestamp(now, PHOENIX).hour
    if phase in (None, 'unknown', 'unavailable'):
        return {'status': 'UNKNOWN', 'why': 'Cooling Arbiter phase is not readable'}
    if not 7 <= hour < 22:
        return {'status': 'WAIT', 'why': 'quiet hours (22:00-07:00)', 'arbiter_phase': phase}
    if phase != 'IDLE':
        return {'status': 'WAIT', 'why': 'the Cooling Arbiter is running (' + phase + ')', 'arbiter_phase': phase}
    return {'status': 'GOOD_TIME', 'why': 'Arbiter idle and outside quiet hours', 'arbiter_phase': phase}


class Runtime:
    def __init__(self, data_dir, client, gate):
        # gate(states, now) -> None (run) or a hold reason; the kill-switch interlock lives in __main__.py.
        self.gate = gate
        self.store = Store(f'{data_dir}/systems.sqlite')
        self.client = client
        self.incidents = Incidents(self.store)
        self.logs = LogLearner(self.store)
        self.archive = Archive(self.store, client)
        self.archive_last = None
        self.prefix = None
        self.installed = self.store.get('installed', {})
        self.last = {'fast': 0., 'slow': 0., 'hourly': 0., 'daily': 0., 'installed': 0.}
        self.status = {'state': 'STARTING', 'physical_actions': False, 'version': __version__}
        self.view = {}
        self.proposals = []
        self.cycle_at = None
        self.good_at = None
        self.cycle_seconds = None
        self.hold = None
        self.inputs = {}
        self.states = None
        self.problems_fast = {}

    # -- helpers -----------------------------------------------------------------------------
    def _metric(self, name, value, now):
        if value is None:
            return
        self.store.ingest([{'entity': 'metric:' + name, 'source_at': now, 'observed_at': now, 'value': float(value),
                            'quality': 'GOOD', 'family': 'systems', 'classification': 'MEASURED'}])
        # Sample spacing differs per metric (60 s, 300 s, hourly); a gap longer than that stays missing.
        gap = 7300 if name == 'db_mib' else 700 if name == 'disk_free_gb' else 180
        rows = self.store.series('metric:' + name, now - 3 * 3600)
        self.store.rollup('metric:' + name, hourly(rows, gap), now)

    def _hourly(self, name, now):
        return self.store.rollup('metric:' + name, [], now)

    def _change(self, key, value, now, kind):
        old = self.store.get('last:' + key)
        if old is not None and old != value:
            self.incidents.change(now, kind, key, old, value)
        if old != value:
            self.store.put('last:' + key, value)

    # -- fast cycle --------------------------------------------------------------------------
    def fast(self, now):
        try:
            states = self.client.states()
            self.inputs['core_api'] = True
        except (OSError, ValueError, TransportError):
            states = None
            self.inputs['core_api'] = False
        self.states = states
        self.hold = self.gate(states, now)
        problems = {}
        if states is None:
            problems['CORE_API_UNREADABLE'] = {'severity': 'ERROR', 'reason': 'CORE_API_UNREADABLE',
                                               'subject': 'home_assistant', 'owner': 'systems_ai'}
            self._change('core_api', 'DOWN', now, 'CORE_API')
        else:
            self._change('core_api', 'UP', now, 'CORE_API')
        if self.hold is not None:
            if self.hold != 'KILL_SWITCH_FULL_STOP' and states is not None:
                problems['KILL:' + self.hold] = {'severity': 'WARNING', 'reason': self.hold, 'subject': 'kill_switch',
                                                 'owner': 'ai_access'}
            self.problems_fast = problems
            return
        values = {}
        for key, entity in MEMORY.items():
            row = states.get(entity)
            values[key] = number(row.get('state')) if isinstance(row, dict) and key != 'last_boot' else None
        boot = (states.get(MEMORY['last_boot']) or {}).get('state')
        if boot not in (None, 'unknown', 'unavailable'):
            self._change('host_boot', boot, now, 'HOST_BOOT')
        for name in ('free', 'swap_pct', 'pressure_full_pct'):
            self._metric('mem_' + name, values[name], now)
        problems.update(memory_problems(values))
        for name, entity in SIBLING_STATUS.items():
            row = states.get(entity)
            if isinstance(row, dict) and name in ('deployer', 'maintenance'):
                self._change(name + '_status', str(row.get('state'))[:64], now,
                             'DEPLOY' if name == 'deployer' else 'MAINTENANCE')
        maint = states.get(SIBLING_STATUS['maintenance'])
        open_count = ((maint or {}).get('attributes') or {}).get('open_problems') if isinstance(maint, dict) else None
        if not isinstance(open_count, int):
            watcher = ((maint or {}).get('attributes') or {}).get('watcher') if isinstance(maint, dict) else None
            open_count = watcher.get('open_problems') if isinstance(watcher, dict) else None
        if isinstance(open_count, int) and open_count > 0:
            # One owner per problem: Maintenance's findings are counted, never raised a second time.
            problems['MAINT:OPEN_PROBLEMS'] = {'severity': 'INFO', 'reason': 'MAINTENANCE_HAS_OPEN_PROBLEMS',
                                               'subject': str(open_count), 'owner': 'maintenance'}
        self.view['memory'] = values
        self.view['maintenance_open_problems'] = open_count
        activity = states.get(SIBLING_STATUS['ai_activity'])
        attrs = (activity or {}).get('attributes') or {} if isinstance(activity, dict) else {}
        self.view['free_limits'] = {'ai_requests_7d': attrs.get('total_requests'),
                                    'ai_activity': (activity or {}).get('state') if isinstance(activity, dict) else None,
                                    'not_yet_published': ['Workers AI Neurons', 'KV writes', 'NWS calls',
                                                          'GitHub Actions minutes']}
        self.view['good_time_to_ask'] = good_time_to_ask(states, now)
        self.problems_fast = problems

    # -- slow cycle --------------------------------------------------------------------------
    def discover(self, now):
        if self.prefix is None:
            me = self.client.info('/addons/self/info')
            m = RE_SELF.fullmatch(str(me.get('slug')))
            self.prefix = m.group(1) if m else 'local'
        if now - self.last['installed'] >= INSTALLED_REFRESH_S or not self.installed:
            found = {}
            for name in FAMILY:
                for prefix in ({self.prefix, 'local'}):
                    slug = f'{prefix}_{name}'
                    try:
                        self.client.info(f'/addons/{slug}/info')
                        found[slug] = name
                    except (OSError, ValueError, TransportError):
                        continue
            self.installed = found
            self.store.put('installed', found)
            self.last['installed'] = now

    def slow(self, now):
        problems = {}
        apps = {}
        try:
            self.discover(now)
            sup = self.client.info('/info')
            host = self.client.info('/host/info')
            resolution = self.client.info('/resolution/info')
            self.inputs['supervisor_api'] = True
        except (OSError, ValueError, TransportError):
            self.inputs['supervisor_api'] = False
            problems['SUPERVISOR_UNREADABLE'] = {'severity': 'ERROR', 'reason': 'SUPERVISOR_UNREADABLE',
                                                 'subject': 'supervisor', 'owner': 'systems_ai'}
            sup = host = resolution = None
        if sup is not None:
            for key in ('homeassistant', 'supervisor', 'hassos'):
                if isinstance(sup.get(key), str):
                    self._change('version:' + key, sup[key], now, 'UPDATE')
            free, total = number(host.get('disk_free')), number(host.get('disk_total'))
            self._metric('disk_free_gb', free, now)
            self.view['disk'] = {'free_gb': free, 'total_gb': total}
            if free is not None and (free < LOW_DISK_GB or (total and free / total < .1)):
                problems['DISK:LOW'] = {'severity': 'WARNING', 'reason': 'DISK_FREE_LOW', 'subject': 'host',
                                        'owner': 'maintenance'}
            for reason in (resolution.get('unhealthy') or [])[:10]:
                problems[f'SUP:UNHEALTHY:{reason}'] = {'severity': 'ERROR', 'reason': 'SUPERVISOR_UNHEALTHY',
                                                       'subject': str(reason)[:48], 'owner': 'systems_ai'}
            for issue in (resolution.get('issues') or [])[:20]:
                kind = str((issue or {}).get('type') if isinstance(issue, dict) else issue)[:48]
                problems[f'SUP:ISSUE:{kind}'] = {'severity': 'WARNING', 'reason': 'SUPERVISOR_ISSUE',
                                                 'subject': kind, 'owner': 'systems_ai'}
            for slug, name in sorted(self.installed.items()):
                try:
                    info = self.client.info(f'/addons/{slug}/info')
                except (OSError, ValueError, TransportError):
                    apps[slug] = {'name': name, 'state': 'UNREADABLE'}
                    continue
                observed = None
                if name in HEALTH_PUBLISHERS and info.get('state') == 'started':
                    port = info.get('ingress_port')
                    try:
                        raw = self.client.health(info.get('hostname'), port)
                    except TransportError:
                        raw = None
                    observed = app_health.observe(raw, now, expected_app=name)
                self._change('app_version:' + slug, info.get('version'), now, 'UPDATE')
                self._change('app_state:' + slug, info.get('state'), now, 'APP_STATE')
                problems.update(app_problems(name, slug, info, observed))
                apps[slug] = {'name': name, 'state': info.get('state'), 'boot': info.get('boot'),
                              'version': info.get('version'), 'update_available': info.get('update_available'),
                              'health': observed['status'] if observed else None,
                              'health_reasons': observed['reasons'][:6] if observed else [],
                              'activity': (observed.get('doc') or {}).get('activity') if observed else None}
                countable = info.get('state') == 'started' or info.get('boot') == 'auto'
                if countable:
                    self.incidents.slo('app:' + name, info.get('state') == 'started'
                                       and (observed is None or observed['status'] == 'OK'), now)
        self.view['apps'] = apps
        try:
            self.logs.observe(self.client.ws({'type': 'system_log/list'}), now)
            issues = self.client.ws({'type': 'repairs/list_issues'})
            issues = issues.get('issues') if isinstance(issues, dict) else issues
            self.inputs['websocket'] = True
            active = [i for i in (issues or []) if isinstance(i, dict) and not i.get('ignored') and not i.get('dismissed_version')]
            self.view['repairs'] = {'active': len(active), 'by_severity': {
                s: sum(i.get('severity') == s for i in active) for s in ('warning', 'error', 'critical')}}
            for i in active[:30]:
                if i.get('severity') in ('error', 'critical'):
                    key = f"REPAIRS:{str(i.get('domain'))[:32]}:{str(i.get('issue_id'))[:48]}"
                    problems[key] = {'severity': 'CRITICAL' if i['severity'] == 'critical' else 'ERROR',
                                     'reason': 'REPAIR_ISSUE', 'subject': str(i.get('domain'))[:32], 'owner': 'systems_ai'}
        except (OSError, ValueError, TransportError):
            self.inputs['websocket'] = False
            problems['HA:WEBSOCKET_UNREADABLE'] = {'severity': 'WARNING', 'reason': 'CORE_WEBSOCKET_UNREADABLE',
                                                   'subject': 'home_assistant', 'owner': 'systems_ai'}
        report = self.logs.report(now)
        self.view['logs'] = report
        for u in report['unusual_integrations'][:10]:
            problems[f"LOG:{u['integration']}"] = {'severity': 'WARNING', 'reason': 'ERROR_RATE_UNUSUAL',
                                                    'subject': u['integration'], 'owner': 'systems_ai'}
        for service in ('core_api', 'supervisor_api', 'websocket'):
            self.incidents.slo(service, self.inputs.get(service), now)
        self.incidents.slo('kill_switch', self.hold in (None, 'KILL_SWITCH_FULL_STOP'), now)
        return problems

    def hourly(self, now):
        try:
            size = mib(self.client.ws({'type': 'system_health/info'}).get('estimated_db_size'))
        except (OSError, ValueError, TransportError, AttributeError):
            size = None
        self.inputs['db_size'] = size is not None
        self._metric('db_mib', size, now)
        self.view['db_mib'] = size
        problems = {}
        if size is None:
            problems['DB:SIZE_UNREADABLE'] = {'severity': 'INFO', 'reason': 'DB_SIZE_UNREADABLE', 'subject': 'recorder',
                                              'owner': 'systems_ai'}
        free = (self.view.get('disk') or {}).get('free_gb')
        if size is not None and free is not None and free * 1024 < size:
            # Repair or repack needs free space at least the size of the database.
            problems['DB:DISK_HEADROOM'] = {'severity': 'WARNING', 'reason': 'DB_LARGER_THAN_FREE_DISK',
                                            'subject': 'recorder', 'owner': 'systems_ai'}
        self.problems_hourly = problems

    def daily(self, now):
        try:
            ids = self.client.ws({'type': 'recorder/list_statistic_ids'})
            current = sorted({str(i.get('statistic_id')) for i in ids if isinstance(i, dict)})[:20000]
            before = self.store.get('lts:ids')
            gone = sorted(set(before or []) - set(current))
            self.store.put('lts:ids', current)
            self.view['long_term_statistics'] = {'ids': len(current), 'disappeared_since_yesterday': gone[:50]}
            self.problems_daily = {'LTS:DISAPPEARED': {'severity': 'WARNING', 'reason': 'LONG_TERM_STATISTICS_DISAPPEARED',
                                                       'subject': str(len(gone)), 'owner': 'systems_ai'}} if before and gone else {}
        except (OSError, ValueError, TransportError, AttributeError):
            self.view['long_term_statistics'] = {'status': 'UNREADABLE'}
            self.problems_daily = {}
        for name in ('db_mib', 'disk_free_gb', 'mem_free', 'mem_swap_pct'):
            freeze(self.store, name, self._hourly(name, now), now)
        self.store.prune(now, retention_days=7)
        self.archive.prune(now)

    # -- one loop step -----------------------------------------------------------------------
    def step(self, now):
        start = time.monotonic()
        failed = False
        try:
            if now - self.last['fast'] >= FAST_S:
                self.fast(now)
                self.last['fast'] = now
                if self.hold is None:
                    self.archive_last = self.archive.step(now)
                if self.hold is None and now - self.last['slow'] >= SLOW_S:
                    self.problems_slow = self.slow(now)
                    self.last['slow'] = now
                if self.hold is None and now - self.last['hourly'] >= HOURLY_S:
                    self.hourly(now)
                    self.last['hourly'] = now
                if self.hold is None and now - self.last['daily'] >= DAY_S:
                    self.daily(now)
                    self.last['daily'] = now
                self.finish(now)
            else:
                return self.status
        except Exception:  # noqa: BLE001 - any failure is reported, never acted on
            failed = True
            self.status = {**self.status, 'state': 'DEGRADED', 'at': now, 'cycle_failed': True}
        self.cycle_at = int(now)
        if not failed:
            self.good_at = int(now)
        self.cycle_seconds = time.monotonic() - start
        return self.status

    def finish(self, now):
        held = self.hold is not None
        problems = {} if held else {**getattr(self, 'problems_slow', {}), **getattr(self, 'problems_hourly', {}),
                                    **getattr(self, 'problems_daily', {})}
        problems.update(self.problems_fast)
        result = self.incidents.update({k: {kk: vv for kk, vv in v.items() if kk != 'proposal'}
                                        for k, v in problems.items()}, now)
        self.proposals = [] if held else self._proposals(problems, result, now)
        self._score_proposals(now)
        forecasts = {}
        for name, threshold, rising in (('db_mib', None, True), ('disk_free_gb', DISK_FLOOR_GB, False),
                                        ('mem_free', LOW_FREE_MIB, False), ('mem_swap_pct', HIGH_SWAP_PCT, True)):
            rows = self._hourly(name, now)
            f = {'in_30_days': trend(rows, now, 30 * 86400), 'scorecard': score(self.store, name, rows, now)}
            if threshold is not None:
                f['crossing'] = days_until(rows, now, threshold, rising)
            forecasts[name] = f
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        self.status = {
            'state': 'HELD' if held else 'SHADOW', 'version': __version__, 'mode': 'SHADOW', 'at': now,
            'physical_actions': False, 'control_authority': 'NONE', 'hold_reason': self.hold,
            'open_problems': result['open'], 'hidden_by_parent': result['hidden_by_parent'],
            'flapping': result['flapping'], 'incidents': self.incidents.recent(),
            'error_budgets': self.incidents.slo_report(now), 'forecasts': forecasts,
            'changes': self.incidents.state['changes'][-30:], 'proposals': self.proposals,
            'proposal_scorecard': self.store.get('proposal_scorecard', {}),
            'archive': {**self.archive.report(now), 'last_read': self.archive_last},
            'view': self.view, 'untrusted_readings': UNTRUSTED, 'not_yet': WAITING, 'inputs': dict(self.inputs),
            'resources': {'peak_rss_mb': round(peak, 1), 'db_bytes': self.store.path.stat().st_size,
                          'paid_api_calls': 0, 'external_calls': 0, 'requests_total': self.client.calls}}

    def _proposals(self, problems, result, now):
        out = []
        for p in result['open']:
            spec = (problems.get(p['key']) or {}).get('proposal')
            if not spec or p.get('flapping') or now - p['since'] < PROPOSE_AFTER_S:
                continue
            kind, target = spec
            out.append({'id': p['id'] + ':' + kind, 'kind': kind, 'target': target, 'reason': p['reason'],
                        'problem': p['key'], 'opened_at': p['since'], 'expires_at': now + PROPOSAL_TTL_S,
                        'executed': False})
        return out[:10]

    def _score_proposals(self, now):
        """Shadow scorecard: distinct proposals per local day, counted once, never executed."""
        book = self.store.get('proposal_scorecard', {})
        day = datetime.fromtimestamp(now, PHOENIX).date().isoformat()
        row = book.setdefault(day, {'proposals': [], 'executed': 0})
        for p in self.proposals:
            if p['id'] not in row['proposals'] and len(row['proposals']) < 100:
                row['proposals'].append(p['id'])
        for old in sorted(book)[:-35]:
            book.pop(old)
        self.store.put('proposal_scorecard', book)

    def close(self):
        self.client.close()
        self.store.close()
