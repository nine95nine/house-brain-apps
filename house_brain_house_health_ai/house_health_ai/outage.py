"""Outage class POWER / ISP / LAN / UNKNOWN (design R1 §4.3; absorbs NOA 1.0 with the R2 power roles).

`noa()` is an exact port of the live NOA 1.0 template package
(`production/accepted/network-advisory/house_brain_network_outage_advisory_v1_0_R1.yaml`): WAN status,
LAN status, grid evidence (Sense only while WAN is UP, Envoy over 50 W means grid present), utility power
from the UPS flags, and the outage class. It is scored minute by minute against the live
`sensor.house_brain_network_outage_class`, so the port itself is checked.

`classify()` then applies the owner's current power-role facts (decision D3, `docs/CURRENT_STATUS.md`):
the Orbi main router and the ISP modem are NOT on the UPS; the Orbi satellite, ZEN16, Pi and network
switch are. Like the R2 model (`tools/evaluate_network_outage_advisory_r2.py` refine()), a modem that is
only on utility power and is visibly online is a local witness that utility power is present at its
outlet; that can only make a POWER verdict less certain, never more. Every difference from NOA 1.0 is a
named delta. Pure functions; nothing here reads the network.
"""

POWER_ROLES = {'ISP_MODEM': 'UTILITY_ONLY', 'ROUTER': 'UTILITY_ONLY', 'MESH_SATELLITE': 'UPS',
               'NETWORK_SWITCH': 'UPS', 'HA_HOST': 'UPS', 'ZEN16': 'UPS'}
TO_CLASS = {'UTILITY_POWER_OUTAGE': 'POWER', 'ISP_OUTAGE': 'ISP', 'LOCAL_LAN_FAULT': 'LAN', 'NO_OUTAGE': 'NONE',
            'PARTIAL': 'UNKNOWN', 'UNKNOWN': 'UNKNOWN'}
ACTIVE = ('POWER', 'ISP', 'LAN')
QUIET_S = 300          # an episode ends after 5 quiet minutes
MIN_EPISODES = 3       # scorecard bar (design R1 §5)


def _float(value, default):
    """Jinja `| float(default)`: a number, else the default."""
    if isinstance(value, bool) or value is None:
        return default
    try:
        n = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return n if n == n and n not in (float('inf'), float('-inf')) else default


def wan_status(ping, cloud):
    if ping == 'on':
        return 'UP'
    if ping == 'off':
        return 'DOWN'
    if cloud == 'off':
        return 'UP'
    if cloud == 'on':
        return 'DOWN'
    return 'UNKNOWN'


def lan_status(ping):
    return 'UP' if ping == 'on' else 'DOWN' if ping == 'off' else 'UNKNOWN'


def grid_evidence(wan, volts_state, solar_state, elevation):
    volts, solar = _float(volts_state, -1.), _float(solar_state, -1.)
    elev = _float(elevation, -90.)
    mains = 'none'
    if wan == 'UP' and volts >= 0:
        mains = 'present' if volts >= 100 else ('absent' if volts < 50 else 'none')
    producing = 'none'
    if solar >= 0:
        producing = 'yes' if solar > 50 else 'no'
    expected = elev >= 20
    strong_present = mains == 'present' or producing == 'yes'
    strong_lost = mains == 'absent'
    weak_lost = producing == 'no' and expected and mains == 'none'
    if strong_present and strong_lost:
        return 'CONTRADICTORY'
    if strong_lost:
        return 'LOST'
    if strong_present:
        return 'PRESENT'
    if weak_lost:
        return 'LOSS_SUGGESTED'
    return 'UNKNOWN'


def ups_evidence(raw):
    flags = str(raw).split() if raw is not None else ['None']
    return 'ON_BATTERY' if 'OB' in flags else ('ON_UTILITY' if 'OL' in flags else 'ABSENT')


def utility_power(ups, grid):
    if ups == 'ON_BATTERY':
        return 'LOCAL_CIRCUIT_SUSPECTED' if grid == 'PRESENT' else 'CONTRADICTORY' if grid == 'CONTRADICTORY' else 'LOST'
    if ups == 'ON_UTILITY':
        return 'CONTRADICTORY' if grid in ('LOST', 'CONTRADICTORY') else 'PRESENT'
    return {'LOST': 'LOST', 'PRESENT': 'PRESENT', 'CONTRADICTORY': 'CONTRADICTORY'}.get(grid, 'UNKNOWN')


def outage_class(wan, lan, power):
    if power == 'LOST':
        return 'UTILITY_POWER_OUTAGE'
    if power in ('CONTRADICTORY', 'LOCAL_CIRCUIT_SUSPECTED'):
        return 'PARTIAL'
    if lan == 'DOWN':
        return 'LOCAL_LAN_FAULT' if power == 'PRESENT' else 'UNKNOWN'
    if wan == 'DOWN':
        return 'ISP_OUTAGE' if lan == 'UP' and power == 'PRESENT' else 'UNKNOWN'
    if wan == 'UP' and lan == 'UP' and power == 'PRESENT':
        return 'NO_OUTAGE'
    return 'UNKNOWN'


def noa(v):
    """The NOA 1.0 chain from raw states: v = {'ups_status', 'wan_ping', 'lan_ping', 'cloud_down', 'volts',
    'solar_w', 'elevation'} (missing keys read as unknown, exactly as the template's states() would)."""
    wan = wan_status(v.get('wan_ping'), v.get('cloud_down'))
    lan = lan_status(v.get('lan_ping'))
    grid = grid_evidence(wan, v.get('volts'), v.get('solar_w'), v.get('elevation'))
    ups = ups_evidence(v.get('ups_status') if v.get('ups_status') is not None else 'unknown')
    power = utility_power(ups, grid)
    return {'wan': wan, 'lan': lan, 'grid': grid, 'ups': ups, 'power': power, 'class': outage_class(wan, lan, power)}


def classify(v, roles=POWER_ROLES):
    """House Health's class: NOA 1.0 refined by the power-role facts. Returns the NOA chain, the class
    (POWER/ISP/LAN/NONE/UNKNOWN), its detail, named deltas from NOA 1.0 and the power-role reasons."""
    base = noa(v)
    power, deltas, reasons = base['power'], [], []
    modem_witness = roles.get('ISP_MODEM') == 'UTILITY_ONLY' and v.get('wan_ping') == 'on'
    if power == 'LOST' and modem_witness:
        power = 'CONTRADICTORY' if base['grid'] == 'LOST' else 'LOCAL_CIRCUIT_SUSPECTED'
        deltas.append('R2_MODEM_ONLINE_WHILE_UPS_ON_BATTERY')
    elif power == 'UNKNOWN' and modem_witness:
        power = 'PRESENT'
        deltas.append('R2_MODEM_ONLINE_IMPLIES_UTILITY_PRESENT')
    raw = outage_class(base['wan'], base['lan'], power)
    cls = TO_CLASS[raw]
    if cls == 'POWER':
        if base['wan'] == 'DOWN' and roles.get('ISP_MODEM') == 'UTILITY_ONLY':
            reasons.append('WAN_LOSS_EXPECTED_MODEM_NOT_ON_UPS')
        if base['lan'] == 'DOWN' and roles.get('ROUTER') == 'UTILITY_ONLY':
            reasons.append('LAN_LOSS_EXPECTED_ROUTER_NOT_ON_UPS')
        elif base['lan'] == 'UP' and roles.get('ROUTER') == 'UTILITY_ONLY':
            reasons.append('LAN_UP_WHILE_ROUTER_RECORDED_UTILITY_ONLY_RECHECK_POWER_FACT')
    return {'noa_port': base, 'class': cls, 'detail': raw, 'refined_power': power, 'deltas': deltas,
            'reasons': reasons, 'on_ups': sorted(r for r, p in roles.items() if p == 'UPS'),
            'not_on_ups': sorted(r for r, p in roles.items() if p == 'UTILITY_ONLY')}


def _dominant(counts):
    active = {k: n for k, n in counts.items() if k in ACTIVE}
    return max(sorted(active), key=lambda k: active[k]) if active else 'UNKNOWN'


def episode_step(ep, now, cls, detail, live_noa):
    """Track one outage episode. Active while House Health or the live NOA sees POWER/ISP/LAN (or PARTIAL);
    it ends after QUIET_S quiet. Returns (episode or None, finished record or None)."""
    live = TO_CLASS.get(live_noa) if isinstance(live_noa, str) else None
    active = cls in ACTIVE or detail == 'PARTIAL' or live in ACTIVE or live_noa == 'PARTIAL'
    if ep is None:
        if not active:
            return None, None
        ep = {'start': now, 'last_active': now, 'ours': {}, 'noa': {}}
    if active:
        ep = {**ep, 'last_active': now, 'ours': {**ep['ours'], cls: ep['ours'].get(cls, 0) + 1},
              'noa': {**ep['noa'], live or 'MISSING': ep['noa'].get(live or 'MISSING', 0) + 1}}
        return ep, None
    if now - ep['last_active'] < QUIET_S:
        return ep, None
    ours, theirs = _dominant(ep['ours']), _dominant(ep['noa'])
    return None, {'start': ep['start'], 'end': ep['last_active'],
                  'minutes': round((ep['last_active'] - ep['start']) / 60 + 1, 1), 'ours': ours, 'noa': theirs,
                  'agree': ours == theirs, 'power_called_isp': theirs == 'POWER' and ours == 'ISP'}


def scorecard(episodes):
    """Design R1 §5: same class in every natural outage seen (at least MIN_EPISODES), and no POWER called ISP."""
    n = len(episodes)
    agree = sum(1 for e in episodes if e['agree'])
    isp = sum(1 for e in episodes if e['power_called_isp'])
    met = n >= MIN_EPISODES and agree == n and isp == 0
    return {'episodes': n, 'agree': agree, 'power_called_isp': isp,
            'status': 'MET' if met else 'NOT_MET' if (n and (agree < n or isp)) else 'WAITING'}
