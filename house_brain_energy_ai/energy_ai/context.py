"""Reviewed private context and passive native state adapters; never permissions."""
import hashlib
from datetime import datetime
from .planning import TZ
from .contracts import number, timestamp
from .store import encode
from .reviewed import load_packet
from .analytics import cycles, correlate


def text(value, limit=256):
    if not isinstance(value,str) or not value.strip() or len(value)>limit:
        raise ValueError('CONTEXT_TEXT_INVALID')
    return value


def load_context(root, now, bindings):
    packet=load_packet(root,'context_packet.json',limit=131072)
    if packet is None:
        return {'status':'NOT_CONFIGURED','bindings':[], 'planning':None}
    if (not isinstance(packet,dict) or type(packet.get('schema_version')) is not int
            or packet.get('schema_version')!=1):
        raise ValueError('CONTEXT_SCHEMA')
    if set(packet)-{'schema_version','revision','reviewed_by','evidence_ref','issued_at','expires','bindings','planning'}:
        raise ValueError('CONTEXT_UNKNOWN_FIELD')
    for field in ('revision','reviewed_by','evidence_ref'):
        text(packet[field])
    issued=number(packet['issued_at'],0,4e9)
    expiry=number(packet['expires'],0,4e9)
    if not issued<=now<expiry or expiry-issued>31*86400:
        raise ValueError('CONTEXT_EXPIRED_OR_FUTURE')
    entries=packet.get('bindings',[])
    if not isinstance(entries,list) or len(entries)>32:
        raise ValueError('CONTEXT_BINDING_LIMIT')
    allowed={b.entity for b in bindings if b.role.startswith('detection:') and b.family=='sense'}
    stable_ids=set(); entities=set()
    for entry in entries:
        if not isinstance(entry,dict) or set(entry)!={'stable_id','entity','ha_unique_id','integration','registry_evidence_ref','enabled'}:
            raise ValueError('CONTEXT_BINDING_SCHEMA')
        for k in ('stable_id','ha_unique_id','registry_evidence_ref'):
            text(entry[k],128 if k=='stable_id' else 256)
        if (entry['integration']!='sense' or entry['entity'] not in allowed
                or type(entry['enabled']) is not bool or entry['stable_id'] in stable_ids
                or entry['entity'] in entities):
            raise ValueError('CONTEXT_BINDING_INVALID')
        stable_ids.add(entry['stable_id']); entities.add(entry['entity'])
    result={**packet,'status':'REVIEWED_CONTEXT','digest':hashlib.sha256(encode(packet).encode()).hexdigest()}
    plan=packet.get('planning')
    if plan is not None:
        required={'valid_from','expires','horizon_start','billing_month','billing_cycle_end','bank_kwh',
                  'terminal_credit_value','evidence_ref','base_excludes_flexible_loads',
                  'baseline_evidence_ref','native_schedule_evidence_ref','jobs','uncertainty_dollars'}
        if (not isinstance(plan,dict) or not required<=set(plan) or set(plan)-required-{'billing_source'}
                or plan.get('billing_source','reviewed') not in {'reviewed','native'}):
            raise ValueError('CONTEXT_PLAN_SCHEMA')
        start=number(plan['valid_from'],0,4e9); end=number(plan['expires'],0,4e9)
        horizon=number(plan['horizon_start'],0,4e9)
        cycle_end=number(plan['billing_cycle_end'],0,4e9)
        if not start<=now<end or end-start>1800 or horizon!=(int(now//1800)+1)*1800:
            raise ValueError('PLAN_CONTEXT_EXPIRED_OR_MISALIGNED')
        if horizon+48*1800>cycle_end:
            raise ValueError('PLAN_CROSSES_BILLING_CYCLE')
        if plan['base_excludes_flexible_loads'] is not True:
            raise ValueError('FLEXIBLE_LOAD_DOUBLE_COUNT_RISK')
        for field in ('evidence_ref','baseline_evidence_ref','native_schedule_evidence_ref'):
            text(plan[field])
        number(plan['uncertainty_dollars'],0,1000)
        if (type(plan['billing_month']) is not int or not 1<=plan['billing_month']<=12
                or datetime.fromtimestamp(cycle_end,TZ).month!=plan['billing_month']):
            raise ValueError('BILL_MONTH')
        if not isinstance(plan['jobs'],list) or len(plan['jobs'])>4:
            raise ValueError('CONTEXT_JOB_LIMIT')
        for j in plan['jobs']:
            if not isinstance(j,dict) or j.get('commissioned',False) is not False:
                raise ValueError('CONTEXT_CANNOT_GRANT_CONTROL')
    return result


def enroll(store, context, now):
    """Only reviewed registry evidence binds IDs; never wattage/name inference."""
    if context.get('status')!='REVIEWED_CONTEXT':
        return
    if store.get('context-digest')==context['digest']:
        return
    if context['issued_at']<=store.get('context-issued-at',-1):
        raise ValueError('CONTEXT_ROLLBACK_REFUSED')
    current=store.get('identity_bindings',{})
    pending=dict(current)
    for row in context.get('bindings',[]):
        stable=row['stable_id']
        old=current.get(stable)
        identity={k:row[k] for k in ('entity','ha_unique_id','integration')}
        if old and any(old.get(k)!=v for k,v in identity.items()):
            raise ValueError('STABLE_BINDING_REASSIGNMENT_REFUSED')
        if any(k!=stable and v.get('entity')==row['entity'] and v.get('verified') is True
               for k,v in pending.items()):
            raise ValueError('ENTITY_ALREADY_BOUND')
        pending[stable]={**identity,'verified':row['enabled'],'registry_evidence_ref':row['registry_evidence_ref'],
                         'reviewed_by':context['reviewed_by'],'at':now,'classification':'USER-CONFIRMED',
                         'physical_authority':False}
    if len(pending)>32:
        raise ValueError('BINDING_CAPACITY')
    # One transaction preserves the registry and its audit marker together.
    with store.db:
        store.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)',('identity_bindings',encode(pending)))
        store.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)',('context-digest',encode(context['digest'])))
        store.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)',('context-issued-at',encode(context['issued_at'])))
        store.db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?,?)',
                         ('context:'+context['digest'],now,'REVIEWED_CONTEXT',encode({'revision':context['revision'],
                          'evidence_ref':context['evidence_ref'],'binding_count':len(pending)})))


def reported(state, now, max_age=180):
    if not isinstance(state,dict):
        return False
    try:
        at=timestamp(state.get('last_reported') or state['last_updated'])
        return 0<=now-at<=max_age
    except (ValueError,KeyError,TypeError,AttributeError):
        return False


def tesla_observation(states, now, ids=None):
    """Accepted Tesla Fleet telemetry contract, never Sense EV vehicle inference."""
    if ids is None:
        return {'status':'UNAVAILABLE','reason':'PRIVATE_TELEMETRY_BINDINGS_REQUIRED'}
    values={k:states.get(v) for k,v in ids.items()}
    if not all(reported(v,now) and v.get('entity_id')==ids[k] for k,v in values.items()):
        return {'status':'UNAVAILABLE','reason':'TELEMETRY_MISSING_OR_STALE'}
    if values['ready']['state']!='ONLINE_READY' or values['fresh']['state']!='on':
        return {'status':'UNAVAILABLE','reason':'FLEET_NOT_READY'}
    try:
        if values['age'].get('attributes',{}).get('unit_of_measurement')!='min':
            raise ValueError()
        number(values['age']['state'],0,3)
    except (ValueError,TypeError,KeyError):
        return {'status':'UNAVAILABLE','reason':'TELEMETRY_AGE_INVALID'}
    value=values['charging'].get('state')
    if value not in ('on','off'):
        return {'status':'UNAVAILABLE','reason':'CHARGING_UNKNOWN'}
    return {'status':'OBSERVED','active':value=='on','entity':ids['charging'],
            'source_at':timestamp(values['charging'].get('last_reported') or values['charging']['last_updated']),
            'family':'tesla_fleet','physical_authority':False}


def continuous_windows(rows, max_gap=180):
    windows=[]
    for (a,_),(b,_) in zip(rows,rows[1:]):
        if 0<b-a<=max_gap:
            if windows and windows[-1][1]==a:
                windows[-1][1]=b
            else:
                windows.append([a,b])
    return windows


def intersection(left, right):
    result=[]
    i=j=0
    while i<len(left) and j<len(right):
        a,b=left[i]; c,d=right[j]
        if max(a,c)<min(b,d):
            result.append([max(a,c),min(b,d)])
        if b<=d:
            i+=1
        else:
            j+=1
    return result


def covered(events, windows):
    result=[]
    i=0
    for event in events:
        while i<len(windows) and windows[i][1]<event['start']:
            i+=1
        if i<len(windows) and windows[i][0]<=event['start'] and event['end']<=windows[i][1]:
            result.append(event)
    return result


def update_passive_evidence(store, states, now, detections, ids=None):
    obs=tesla_observation(states,now,ids)
    track=store.get('context:tesla',{'previous_at':None,'previous_active':None,'start':None,
                                    'events':[],'windows':[]})
    track.setdefault('windows',[])
    last=track['previous_at']
    at=obs.get('source_at',now)
    watermark=track.get('source_watermark')
    if obs['status']=='OBSERVED' and watermark is not None and at<watermark:
        obs={'status':'UNAVAILABLE','reason':'OUT_OF_ORDER_NATIVE_REPORT'}
    if obs['status']!='OBSERVED':
        track.update(previous_at=None,previous_active=None,start=None)
    elif watermark is not None and at==watermark:
        pass  # A repeated cached sample cannot create another independent observation.
    elif last is None or at<last or at-last>180:
        track.update(previous_at=at,previous_active=obs['active'],start=None)
    else:
        if track['windows'] and track['windows'][-1][1]==last:
            track['windows'][-1][1]=at
        else:
            track['windows'].append([last,at])
        if obs['active'] and track['previous_active'] is False:
            track['start']=at
        elif not obs['active'] and track['start'] is not None:
            track['events'].append({'start':track['start'],'end':at})
            track['start']=None
        track.update(previous_at=at,previous_active=obs['active'])
    if obs['status']=='OBSERVED' and (watermark is None or at>watermark):
        track['source_watermark']=at
    track['events']=[e for e in track['events'] if e['start']>=now-7*86400][-256:]
    track['windows']=[w for w in track['windows'] if w[1]>=now-7*86400][-2048:]
    store.put('context:tesla',track)
    evidence={}
    for entity,data in detections.items():
        windows=continuous_windows(data['rows'])
        common=intersection(windows,track['windows'])
        measured=covered(data['events'],common)
        independent=covered(track['events'],common)
        result=correlate(measured,independent,'sense','tesla_fleet')
        # Counts describe temporal agreement, never classification accuracy or identity.
        result['unmatched_detection_cycles']=result.pop('false_matches')
        result['unmatched_independent_cycles']=result.pop('missed')
        result['temporal_match_fraction']=result.pop('precision')
        result['independent_cycle_match_fraction']=result.pop('recall')
        result['excluded_detection_cycles']=len(data['events'])-len(measured)
        result['count_semantics']='only cycles in common observed coverage; no labeled appliance ground truth'
        result['stable_vehicle_identity']=False
        result['eligible_for_confirmation']=False
        evidence[entity]=result
    store.put('passive-evidence',evidence)
    return {'tesla':obs,'complete_independent_cycles':len(track['events']),'candidates':evidence,
            'note':'Shared EV detections remain ambiguous; no wake or vehicle commands'}
