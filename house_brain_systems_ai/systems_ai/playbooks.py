"""Playbooks (Systems AI 0.3.3; agentic layer design R1 step A2, owner pop-up 2026-11-02 "Cases 1–5").

A playbook is a solved real case written down so the house's own agents recognise it the next time, with no model
and no subscription (Repair Vaccine Library, R3.1 I03; Digital Apprenticeship, I21). Each one has:
- a signature: what Systems AI sees when it happens again;
- read-only checks that confirm it or rule it out ("when not to use it");
- the exact request to the owner, which stays the owner's tap (Systems AI never acts);
- how the fix is verified;
- the real case it came from and the versions it was tested on.

The first five are the real cases chosen by the owner. Each has a replay test built from the recorded evidence
(tests/house_brain_systems_ai/test_systems_ai.py, PlaybookReplay). Matching is deterministic, reads only what the
runtime already collected, and runs every cycle, also while held: like the 0.2.3 rescue it adds no read and no act.
"""
import re

AUTHORITY = 'NONE'
ALWAYS_ON = ('house_brain_ai_access', 'house_brain_engineering_inspector', 'house_brain_engineering_observer')
NAMES = {'house_brain_ai_access': 'AI Access', 'house_brain_engineering_inspector': 'Inspector',
         'house_brain_engineering_observer': 'Observer'}
STALE_HOLDS = ('KILL_SWITCH_SENSOR_MISSING', 'KILL_SWITCH_SENSOR_INVALID', 'KILL_SWITCH_SENSOR_STALE_OR_FUTURE')
DAILY_LIMIT = re.compile(r'daily (?:install|lookup) limit is reached')
FIXED_STARTUP = (0, 3, 45)
AI_ACCESS_SLUG = 'house_brain_ai_access'


def _version(text):
    m = re.match(r'(\d+)\.(\d+)\.(\d+)', str(text or ''))
    return tuple(int(x) for x in m.groups()) if m else None


LIBRARY = (
    {'id': 'PB-AI-ACCESS-STOPPED', 'title': 'AI Access stopped, so every House Brain AI is held',
     'cases': [{'date': '2026-10-07', 'record': 'docs/validation/HAOS_PI_FREEZE_INCIDENT_2026-10-06.md'},
               {'date': '2026-11-01', 'record': 'docs/validation/SYSTEMS_AI_0_1_0_2026-10-20.md'}],
     'tested_on': {'house_brain_ai_access': ['0.1.2-dev', '0.1.9-dev'], 'house_brain_systems_ai': ['0.3.3']},
     'request': 'Start AI Access (Settings → Add-ons → House Brain AI Access → Start), or tap Start in '
                "Maintenance's \"Needs your decision\". Then check its Start on boot switch is on.",
     'verify': 'The kill switch reads fresh again within 5 minutes and the hold clears on its own.',
     'not_when': 'The switch says FULL_STOP (your own stop), or AI Access is running and the switch is still stale.'},
    {'id': 'PB-START-ON-BOOT-OFF', 'title': 'A read-only App stayed stopped after a restart: Start on boot is off',
     'cases': [{'date': '2026-11-01', 'record': 'docs/validation/READ_ONLY_APPS_START_ON_BOOT_2026-11-01.md'}],
     'tested_on': {'house_brain_engineering_inspector': ['0.2.20', '0.2.21'],
                   'house_brain_ai_access': ['0.1.9-dev', '0.1.10-dev'], 'house_brain_systems_ai': ['0.3.3']},
     'request': 'Start {app}, then turn Start on boot on in its Info tab. If there is no Start on boot switch, '
                'update it first (Inspector 0.2.21 and AI Access 0.1.10-dev added the switch).',
     'verify': 'The App is running and its Start on boot reads on, so the next restart brings it back by itself.',
     'not_when': 'You stopped it on purpose. Systems AI cannot tell that apart; it only suggests.'},
    {'id': 'PB-DEPLOYER-STARTUP-REFUSED', 'title': 'The Deployer is held at start (STARTUP_REFUSED)',
     'cases': [{'date': '2026-11-01', 'record': 'docs/validation/HOUSE_BRAIN_DEPLOYER_0_3_44_STARTUP_CAUSE_2026-11-02.md'}],
     'tested_on': {'house_brain_deployer': ['0.3.43', '0.3.44', '0.3.45'], 'house_brain_systems_ai': ['0.3.3']},
     'request': 'Restarting does not clear a startup hold. {fix}',
     'verify': 'After Start, sensor.house_brain_deployer_status leaves HELD.',
     'not_when': 'The hold is the daily approval limit (see PB-DEPLOYER-DAILY-LIMIT): nothing is broken then.'},
    {'id': 'PB-DEPLOYER-DAILY-LIMIT', 'title': 'The Deployer is waiting for its daily approval limit (nothing broken)',
     'cases': [{'date': '2026-11-02', 'record': 'docs/validation/HOUSE_BRAIN_DEPLOYER_0_3_44_STARTUP_CAUSE_2026-11-02.md'}],
     'tested_on': {'house_brain_deployer': ['0.3.44'], 'house_brain_systems_ai': ['0.3.3']},
     'request': "Nothing to repair. Tap 'Allow more today' on the Deployer page, or wait: it asks again by itself. "
                'Do not restart or update it for this.',
     'verify': 'sensor.house_brain_deployer_status returns to OK when the limit frees or is lifted.',
     'not_when': 'The status reason is STARTUP_REFUSED (see PB-DEPLOYER-STARTUP-REFUSED).'},
    {'id': 'PB-INTEGRATION-RELOGIN', 'title': 'An integration needs you to log in again',
     'cases': [{'date': '2026-11-02', 'record': 'docs/validation/HB454_MAINTENANCE_0_9_49_RELOGIN_PLAYBOOK_2026-11-02.md'}],
     'tested_on': {'house_brain_maintenance': ['0.9.49'], 'house_brain_systems_ai': ['0.3.3']},
     'request': 'Open Settings → Devices & services, find the integration asking to log in again ({what}), and '
                'log in. Maintenance owns this one (its rule R9) and closes it when the login works.',
     'verify': 'The Repairs card disappears and the integration\'s entities update again.',
     'not_when': 'Systems AI sees only the Repairs card, not the config entry: the integration name may be missing.',
     'signature_from_source': True},
)
BY_ID = {p['id']: p for p in LIBRARY}


def signals(*, hold, ai_access_state, apps, states, problems):
    """The read-only inputs a playbook may use, from what the runtime already holds. Nothing is read here."""
    states = states or {}
    deployer = states.get('sensor.house_brain_deployer_status')
    attrs = (deployer.get('attributes') or {}) if isinstance(deployer, dict) else {}
    reasons = attrs.get('reasons') if isinstance(attrs.get('reasons'), list) else []
    return {'hold': hold, 'ai_access_state': ai_access_state, 'apps': apps or {},
            'deployer': None if not isinstance(deployer, dict) else {
                'state': deployer.get('state'), 'reason': str(attrs.get('reason') or '')[:120],
                'reasons': [str(r)[:300] for r in reasons[:6]], 'version': str(attrs.get('version') or '')[:32]},
            'problems': sorted(problems or ())}


def _hit(pid, problems, checks, evidence, **fill):
    p = BY_ID[pid]
    return {'playbook': pid, 'title': p['title'], 'problems': problems, 'checks': checks, 'evidence': evidence,
            'request': p['request'].format(**fill) if fill else p['request'], 'verify': p['verify'],
            'not_when': p['not_when'], 'tier': 'T0', 'authority': AUTHORITY}


def match(sig):
    """Every playbook whose signature and checks hold now. Ruled-out twins are named in 'ruled_out'."""
    hits, ruled_out = [], []
    hold = sig.get('hold')
    if hold == 'KILL_SWITCH_FULL_STOP':
        ruled_out.append({'playbook': 'PB-AI-ACCESS-STOPPED', 'why': 'the switch says FULL_STOP: your own stop'})
    elif hold in STALE_HOLDS:
        state = sig.get('ai_access_state')
        if state not in (None, 'started', 'UNREADABLE'):
            hits.append(_hit('PB-AI-ACCESS-STOPPED', ['KILL:AI_ACCESS_STOPPED'],
                             [{'check': 'kill switch hold is missing/invalid/stale, not FULL_STOP', 'result': hold},
                              {'check': 'AI Access state', 'result': state}],
                             [f'hold:{hold}', f'app_state:{AI_ACCESS_SLUG}:{state}']))
        elif state == 'started':
            ruled_out.append({'playbook': 'PB-AI-ACCESS-STOPPED',
                              'why': 'AI Access is running but the switch is still stale: a different cause'})
    for slug, app in sorted((sig.get('apps') or {}).items()):
        name = app.get('name')
        if name not in ALWAYS_ON or app.get('state') in ('started', 'UNREADABLE', None):
            continue
        boot = app.get('boot_config') or app.get('boot')
        if boot == 'auto':
            continue                       # Start on boot is on: the 0.1 APP:…:STOPPED problem covers it
        hits.append(_hit('PB-START-ON-BOOT-OFF', [f'APP:{slug}:STOPPED_BOOT_OFF'],
                         [{'check': 'App state', 'result': app.get('state')},
                          {'check': 'Start on boot', 'result': boot or 'unknown'}],
                         [f'app_state:{slug}:{app.get("state")}', f'app_boot:{slug}:{boot}'],
                         app=NAMES.get(name, name)))
    dep = sig.get('deployer')
    if dep and dep.get('state') == 'HELD':
        reason, lines = dep.get('reason') or '', dep.get('reasons') or []
        if reason.startswith('STARTUP_REFUSED'):
            version = _version(dep.get('version'))
            if 'ACTIVE_SOURCE_LIMIT_GRANT' in reason and version is not None and version < FIXED_STARTUP:
                fix = ("Known cause (2026-11-01): an active 'Allow more today' lift. Update the Deployer to 0.3.45 "
                       'or later and press Start, or wait until midnight when the lift ends.')
            elif version is not None and version < FIXED_STARTUP:
                fix = 'Update the Deployer to 0.3.45 or later (it fixed the 2026-11-01 startup refusal) and press Start.'
            else:
                fix = 'Read the reason on the Deployer page ("Startup held" in its Documentation); this one needs ' \
                      'an engineer if it is not the known cause.'
            hits.append(_hit('PB-DEPLOYER-STARTUP-REFUSED', [],
                             [{'check': 'status reason', 'result': reason[:80]},
                              {'check': 'Deployer version', 'result': dep.get('version') or 'unknown'}],
                             [f'deployer_status:HELD:{reason[:60]}'], fix=fix))
            if any(DAILY_LIMIT.search(line) for line in lines):
                ruled_out.append({'playbook': 'PB-DEPLOYER-DAILY-LIMIT', 'why': 'the startup hold comes first'})
        elif any(DAILY_LIMIT.search(line) for line in lines):
            hits.append(_hit('PB-DEPLOYER-DAILY-LIMIT', [],
                             [{'check': 'status reasons name the daily limit', 'result': 'yes'},
                              {'check': 'status reason is not STARTUP_REFUSED', 'result': 'yes'}],
                             ['deployer_status:HELD:DAILY_APPROVAL_LIMIT']))
            ruled_out.append({'playbook': 'PB-DEPLOYER-STARTUP-REFUSED', 'why': 'no startup refusal'})
    for key in sig.get('problems') or []:
        parts = key.split(':', 2)
        if len(parts) == 3 and parts[0] == 'REPAIRS' and parts[2].startswith('config_entry_reauth'):
            rest = parts[2][len('config_entry_reauth'):].strip('_')
            what = (rest.rsplit('_', 1)[0] if '_' in rest else rest) or 'name not shown'
            hits.append(_hit('PB-INTEGRATION-RELOGIN', [key], [{'check': 'Repairs card', 'result': 'login needed'}],
                             [f'problem:{key}'], what=what))
    return {'matches': hits[:10], 'ruled_out': ruled_out[:10]}



def library():
    """The library as shown on the page: id, title, real cases, versions tested."""
    return [{k: p[k] for k in ('id', 'title', 'cases', 'tested_on')} for p in LIBRARY]


def for_case(case_problems, result):
    """Matches that explain one of the case's problems (used by investigations). Deployer matches name no problem:
    Maintenance owns a Deployer hold (0.9.47), so Systems AI raises none and shows the playbook on its page only."""
    keys = set(case_problems)
    return [m for m in result.get('matches', []) if keys & set(m['problems'])]
