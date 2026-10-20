"""Read existing native energy surfaces through the shared states poll.

Configuration and household entity names are private. This adapter checks the
installed estimator's inputs and arithmetic; it never maintains another bank,
changes a helper, notifies, or sends a service/equipment command.
"""
from datetime import datetime
import re
from .contracts import number, timestamp
from .planning import TZ, period

ENTITY = re.compile(r'^(sensor|binary_sensor|input_number|input_datetime|input_text|select)\.[a-z0-9_]{1,100}$')
BILLING_FIELDS = frozenset({'status', 'on_peak', 'off_peak', 'price', 'tou', 'holiday',
    'seed', 'period_start', 'closed_on', 'closed_off', 'read_day', 'import_source', 'export_source',
    'import_selector', 'export_selector', 'meters', 'max_age_s', 'evidence_ref'})


def validate_adapters(config):
    base = {'schema_version', 'billing', 'telemetry', 'surfaces', 'questions'}
    optional = {'solar_reference', 'rate_plan_alternatives', 'weather_reference'}
    if not isinstance(config, dict) or not base <= set(config) <= base | optional:
        raise ValueError('ADAPTER_FIELDS')
    if type(config['schema_version']) is not int or config['schema_version'] != 1:
        raise ValueError('ADAPTER_SCHEMA')
    billing = config['billing']
    if billing is not None:
        if not isinstance(billing, dict) or set(billing) != BILLING_FIELDS:
            raise ValueError('BILLING_ADAPTER_FIELDS')
        for key in BILLING_FIELDS - {'meters', 'max_age_s', 'evidence_ref'}:
            if not isinstance(billing[key], str) or not ENTITY.fullmatch(billing[key]):
                raise ValueError('BILLING_ADAPTER_ENTITY')
        if type(billing['max_age_s']) is not int or not 60 <= billing['max_age_s'] <= 900:
            raise ValueError('BILLING_ADAPTER_AGE')
        if not isinstance(billing['evidence_ref'], str) or not 1 <= len(billing['evidence_ref']) <= 256:
            raise ValueError('BILLING_ADAPTER_EVIDENCE')
        if not isinstance(billing['meters'], dict) or set(billing['meters']) != {'import_on','import_off','export_on','export_off'}:
            raise ValueError('BILLING_ADAPTER_METERS')
        if any(not isinstance(v,str) or not ENTITY.fullmatch(v) for v in billing['meters'].values()):
            raise ValueError('BILLING_ADAPTER_METERS')
    telemetry = config['telemetry']
    if telemetry is not None:
        if not isinstance(telemetry,dict) or set(telemetry) != {'ready','fresh','charging','age'}:
            raise ValueError('TELEMETRY_ADAPTER_FIELDS')
        if any(not isinstance(v,str) or not ENTITY.fullmatch(v) for v in telemetry.values()):
            raise ValueError('TELEMETRY_ADAPTER_ENTITY')
    for field in ('surfaces','questions'):
        rows=config[field]
        if not isinstance(rows,list) or len(rows)>32:
            raise ValueError('ADAPTER_LIST_LIMIT')
        for row in rows:
            required={'entity','label','evidence_ref'} if field=='surfaces' else {'entity','label','explanation'}
            if not isinstance(row,dict) or set(row)!=required or not ENTITY.fullmatch(row['entity']):
                raise ValueError('ADAPTER_ROW')
            if any(not isinstance(v,str) or not 1<=len(v)<=512 for v in row.values()):
                raise ValueError('ADAPTER_TEXT')
    if 'solar_reference' in config:
        from .reference import validate_reference
        validate_reference(config['solar_reference'])
    if 'weather_reference' in config:
        from .weather import validate_weather
        validate_weather(config['weather_reference'])
    if 'rate_plan_alternatives' in config:
        from .rateplan import validate_alternatives
        validate_alternatives(config['rate_plan_alternatives'])
    return config


def state(states, entity, now, unit=None, fresh=False, max_age=300):
    row=states.get(entity)
    if not isinstance(row,dict) or row.get('entity_id')!=entity:
        raise ValueError('NATIVE_SOURCE_MISSING')
    at=timestamp(row.get('last_reported') or row['last_updated'])
    if at>now or fresh and now-at>max_age:
        raise ValueError('NATIVE_SOURCE_STALE_OR_FUTURE')
    value=row.get('state')
    if value in (None,'unknown','unavailable',''):
        raise ValueError('NATIVE_SOURCE_UNAVAILABLE')
    if unit is not None and row.get('attributes',{}).get('unit_of_measurement')!=unit:
        raise ValueError('NATIVE_UNIT_MISMATCH')
    return row


def billing_observation(states, now, profile, tariff):
    empty={'status':'NOT_CONFIGURED','usable_for_planning':False,'physical_actions':False}
    if profile is None:
        return empty
    try:
        # Derived template states may be unchanged for hours. Require current
        # source reports, then validate their current arithmetic/period instead
        # of treating a recent snapshot or unchanged bank label as fresh proof.
        for key in ('import_source','export_source'):
            row=state(states,profile[key],now,'MWh',True,profile['max_age_s'])
            number(row['state'],0,1e6)
        rows={key:state(states,profile[key],now) for key in BILLING_FIELDS-
              {'meters','max_age_s','evidence_ref','import_source','export_source'}}
        seed=rows['seed']['state']
        match=re.search(r'\bcoverage=(0(?:\.\d+)?|1(?:\.0+)?)\b',seed)
        if not seed.startswith('SEEDED ') or not match or number(match[1],0,1)<.95:
            raise ValueError('BANK_SEED_COVERAGE_REQUIRED')
        if rows['status']['state'] not in {'COVERED','LOW','EMPTY'}:
            raise ValueError('BANK_NOT_READY')
        on=rows['tou']['state']
        if on not in {'on','off'} or rows['holiday']['state'] not in {'on','off'}:
            raise ValueError('NATIVE_TOU_UNKNOWN')
        current_period='on_peak' if on=='on' else 'off_peak'
        if current_period!=period(now):
            raise ValueError('NATIVE_TOU_DISAGREEMENT')
        if any(rows[key]['state']!=current_period for key in ('import_selector','export_selector')):
            raise ValueError('BANK_METER_TARIFF_DISAGREEMENT')
        values={k:number(state(states,e,now,'MWh')['state'],0,1e6) for k,e in profile['meters'].items()}
        banks={}
        for p,suffix in (('on_peak','on'),('off_peak','off')):
            row=state(states,profile[p],now,'kWh')
            banks[p]=number(row['state'],0,100000)
            closed=number(rows['closed_'+suffix]['state'],0,100000)
            expected=max(0,closed+(values['export_'+suffix]-values['import_'+suffix])*1000)
            if abs(banks[p]-expected)>.51:
                raise ValueError('BANK_ESTIMATOR_INPUT_DISAGREEMENT')
        read_day=number(rows['read_day']['state'],1,28)
        if read_day!=int(read_day):
            raise ValueError('READ_DAY_INVALID')
        local=datetime.fromtimestamp(now,TZ)
        year,month=local.year,local.month
        if local.day>=read_day:
            month=month%12+1
            if month==1:year+=1
        cycle_end=datetime(year,month,int(read_day),tzinfo=TZ).timestamp()
        price=state(states,profile['price'],now,'USD/kWh')
        attrs=price.get('attributes',{})
        rates=tariff['charges']['energy_per_kwh']['summer' if 5<=month<=10 else 'winter']
        if (number(attrs['statement_month'],1,12)!=month or attrs['period']!=current_period
                or abs(number(price['state'],0,10)-rates[current_period])>1e-7):
            raise ValueError('NATIVE_PRICE_CYCLE_DISAGREEMENT')
        period_start=datetime.fromisoformat(rows['period_start']['state']).replace(tzinfo=TZ).timestamp()
        if not period_start<=now<cycle_end or now-period_start>35*86400:
            raise ValueError('BANK_PERIOD_INVALID')
        horizon=(int(now//1800)+1)*1800
        # Current bank is not an APS statement, nor proof of its annual value.
        return {'status':'OBSERVED_ESTIMATE','native_status':rows['status']['state'],
                'usable_for_planning':horizon+48*1800<=cycle_end,'bank_kwh':banks,
                'billing_month':month,'billing_cycle_end':cycle_end,'period':current_period,
                'base_price_usd_per_kwh':number(price['state'],0,10),'seed_coverage':number(match[1]),
                'evidence_ref':profile['evidence_ref'],'observed_at':now,'expires':now+profile['max_age_s'],
                'basis':'existing native estimator checked against its current inputs',
                'statement_balance':False,'full_bill_claimed':False,'physical_actions':False,
                'limitation':'read-day jitter; adjustors/taxes and annual marginal credit value are separate'}
    except (ValueError,KeyError,TypeError,OverflowError,AttributeError):
        return {**empty,'status':'UNAVAILABLE','reason':'NATIVE_BILLING_INPUTS_MISSING_STALE_OR_INCONSISTENT'}


def existing_surfaces(states, now, profiles):
    result=[]
    for profile in profiles:
        try:
            row=state(states,profile['entity'],now)
            observed={'status':'REPORTED','state':str(row['state'])[:512],
                      'source_at':timestamp(row.get('last_reported') or row['last_updated']),
                      'freshness':'underlying domain inputs not independently qualified'}
        except (ValueError,KeyError,TypeError,AttributeError):
            observed={'status':'NOT_AVAILABLE'}
        result.append({**profile,**observed,'authority':'existing domain only','notification_sent':False})
    return result
