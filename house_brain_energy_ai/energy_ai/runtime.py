"""Durable local collection/training/analysis loop with bounded work per tick."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
import hashlib
import os
from pathlib import Path
import resource
import signal
import time

from . import __version__
from .contracts import mark_idle, sample, validate_bindings, timestamp
from .store import Store, encode
from .forecast import hourly, evaluate, seasonal_many, predict
from .analytics import baseload, robust_anomaly, cycles, clusters, IdentityRegistry
from .planning import energy_integral, BankObjective, Job, schedule
from . import templates
from .reviewed import evaluate_private_inputs
from .context import load_context, enroll, update_passive_evidence
from .model_audit import ModelAudit
from .native import validate_adapters, billing_observation, existing_surfaces
from .reference import observe as observe_reference
from .scorecard import record as record_scorecard
from .rateplan import stay_put_value, what_if
from . import insights
from . import weather


class Runtime:
    def __init__(self, config, data_dir, accepted, tariff, client=None, *, billing_policy=None, identity_register=None,
                 adapters=None, retained_bill=None, statements=None):
        self.bindings=validate_bindings(config["bindings"])
        if config.get("mode","SHADOW") not in {"BUILD/REPLAY","SHADOW"}:
            raise ValueError("CONTROL_MODES_NOT_COMMISSIONED")
        self.mode=config.get("mode","SHADOW")
        self.config=config
        self.data_dir=Path(data_dir)
        self.store=Store(self.data_dir/"energy.sqlite")
        self.identities=IdentityRegistry(self.store,accepted)
        self.model_audit=ModelAudit(self.store)
        self.billing_policy=billing_policy
        self.identity_register=identity_register
        self.tariff=tariff
        self.adapters=validate_adapters(adapters or config.get('_adapters') or
            {'schema_version':1,'billing':None,'telemetry':None,'surfaces':[],'questions':[]})
        self.retained_bill=retained_bill
        self.statements=statements
        self.client=client
        self.last_train=self.store.get("last_train",0)
        self.last_prune=0
        self.last_review=0
        self.last_rateplan=0
        self.rate_plan={}
        self.insights={}
        self.weather_rows=[]
        self.reviewed={}
        self.status={"state":"STARTING","physical_actions":False}
        self.paused=self.store.get("paused",False)

    def rollback_model(self, role, now=None):
        return self.model_audit.rollback(role,time.time() if now is None else now)

    def backfill_one(self, now):
        """One entity/day per tick; current units never substitute for historic units."""
        if self.client is None or not self.config.get("backfill",False):
            return {"status":"NOT_ENABLED"}
        progress=self.store.get("backfill",{"index":0,"accepted":0,"refused":0,
                                           "anchor":int(now//86400)*86400})
        targets=[b for b in self.bindings if b.role in {"load","solar"}]
        if progress["index"]>=30*len(targets) or not targets:
            return {**progress,"status":"COMPLETE_OR_RETENTION_LIMITED"}
        index=progress["index"]
        b=targets[index%len(targets)]
        end=progress["anchor"]-(index//len(targets))*86400
        start=end-86400
        try:
            rows=self.client.history(b.entity,start,end)
            accepted=[]
            for row in rows:
                if not isinstance(row,dict):
                    raise ValueError("HISTORY_ROW_INVALID")
                if row.get("entity_id")!=b.entity:
                    raise ValueError("HISTORY_ENTITY_MISMATCH")
                at=timestamp(row.get("last_updated") or row["last_changed"])
                historical={**row,"last_reported":datetime.fromtimestamp(at,timezone.utc).isoformat()}
                s=sample(historical,b,at)
                if start<=at<end and s["quality"]=="GOOD":
                    accepted.append(s)
                else:
                    progress["refused"]+=1
            self.store.ingest(accepted)
            self.store.rollup(b.entity,hourly(sorted((s["source_at"],s["value"]) for s in accepted)),now,allow_older=True)
            progress["accepted"]+=len(accepted)
            progress["last_error"]=None
        except (ValueError,KeyError,TypeError,OSError):
            progress["last_error"]="HISTORY_UNAVAILABLE_OR_REFUSED"
        # No endless retries on an unsupported endpoint. Re-import is a reviewed operation.
        progress["index"]+=1
        if progress["index"]==30*len(targets):
            self.last_train=0
            self.store.put("last_train",0)
        self.store.put("backfill",progress)
        return progress

    def approve_template(self, template_id, approved):
        """Owner decision on the page (0.1.5): approve or withdraw one learned template. Shadow planning only."""
        return templates.approve(self.store,getattr(self,"template_proposals",{}),template_id,approved,time.time())

    def pause(self, paused):
        if type(paused) is not bool:
            raise ValueError("INVALID_PAUSE")
        self.store.put("paused",paused)
        self.paused=paused

    def tick(self, states, now=None):
        now=time.time() if now is None else now
        start=time.monotonic()
        values=mark_idle([sample(states.get(b.entity),b,now) for b in self.bindings],states)
        self.store.ingest(values)
        try:
            context=load_context(self.data_dir,now,self.bindings)
            enroll(self.store,context,now)
        except (ValueError,KeyError,TypeError,OSError,OverflowError):
            context={"status":"REFUSED","planning":None,"reason":"PRIVATE_CONTEXT_INVALID_OR_EXPIRED"}
        issues={v["role"]:v["quality"] for v in values if v["quality"] not in ("GOOD","IDLE")}
        series={b.role:self.store.series(b.entity,now-7*86400) for b in self.bindings
                if b.role in {"load","solar","grid","unknown"}}
        by_role={b.role:b for b in self.bindings}
        hours={k:self.store.rollup(by_role[k].entity,hourly(v),now) for k,v in series.items()
               if k in {"load","solar"}}
        peak_ram_mb=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024
        budget_hold=peak_ram_mb>128
        model_status={}
        forecasts={}
        forecast_audit={}
        solar_reference=observe_reference(states,now,self.adapters.get('solar_reference'))
        outdoor=weather.observe(states,now,self.adapters.get('weather_reference'))
        if outdoor is not None:
            # Minute samples from the shared poll; complete hours roll up into the existing hourly table.
            self.weather_rows=[r for r in self.weather_rows if r[0]>=now-7200]+[(now,outdoor)]
            self.store.rollup("weather:outdoor-temperature",hourly(self.weather_rows),now)
        for role,rows in hours.items():
            report=self.store.get("model:"+role,{"status":"INSUFFICIENT_HISTORY","promote":False})
            if (now-self.last_train>=86400 and not budget_hold and not self.paused
                    and now>=self.store.get("model-hold:"+role,0)):
                report=evaluate(rows)
                prior=self.store.get("model:"+role)
                if prior is not None:
                    self.store.put("previous-model:"+role,prior)
                self.store.put("model:"+role,report)
            if (role=="solar" and report.get("promote")
                    and self.store.get("forecast-audit:solar",{}).get("reference_better") is True):
                # Promotion can only be withheld here; the existing forecast integration stays the authority.
                report={**report,"promote":False,"promotion_hold":"EXISTING_REFERENCE_FORECAST_BETTER"}
            model_status[role]={k:v for k,v in report.items() if k!="model"}
            forecast_audit[role]=self.model_audit.observe(role,rows,report,now,allow_issue=role not in issues and not self.paused,
                reference=solar_reference.get("next_hour_mean_w") if role=="solar" else None)
            if rows and role not in issues:
                first=(int(now//1800)+1)*1800
                times=[first+i*1800 for i in range(48)]
                predicted=([predict(report["model"],t) for t in times] if report.get("promote")
                           else seasonal_many(rows,times))
                forecasts[role]=[{"at":t,"watts":w} for t,w in zip(times,predicted)]
        if now-self.last_train>=86400 and not budget_hold and not self.paused:
            self.last_train=now
            self.store.put("last_train",now)
        if now-self.last_prune>=86400:
            self.store.prune(now)
            self.last_prune=now
        diagnostics={"baseload":baseload(hours.get("load",[]))}
        if now-self.last_review>=3600:
            self.reviewed=evaluate_private_inputs(self.data_dir, self.billing_policy, self.identity_register, self.retained_bill)
            self.last_review=now
        diagnostics["reviewed_evidence"]=self.reviewed
        diagnostics['historical_statements']=self.statements
        native_billing=billing_observation(states,now,self.adapters['billing'],self.tariff)
        diagnostics['native_billing']=native_billing
        diagnostics['existing_surfaces']=existing_surfaces(states,now,self.adapters['surfaces'])
        diagnostics['solar_reference']=solar_reference
        load=next((v for v in values if v["role"]=="load" and v["quality"]=="GOOD"),None)
        if load:
            diagnostics["load_anomaly"]=robust_anomaly([v for _,v in hours.get("load",[])],load["value"])
        unknown_cycles=cycles(series.get("unknown",[]))
        diagnostics["unknown_patterns"]=clusters(unknown_cycles)
        diagnostics["unknown_unclustered_cycles"]=len(unknown_cycles)-sum(g["count"] for g in diagnostics["unknown_patterns"])
        diagnostics["identification_priorities"]=sorted([
            {"candidate":"UNKNOWN_PATTERN_"+str(i+1),
             "estimated_observed_kwh":g["watts"]*g["duration_s"]*g["count"]/3600000,
             "next_step":"Use a natural isolated cycle and independent appliance state; no extra load test",
             "identity":"UNRESOLVED"} for i,g in enumerate(diagnostics["unknown_patterns"])
        ],key=lambda x:-x["estimated_observed_kwh"])[:5]
        diagnostics["unknown_patterns_note"]="Other is an aggregate; overlapping loads are not appliance identities"
        detections={}
        detection_events={}
        for binding in self.bindings:
            if binding.role.startswith("detection:"):
                observed=self.store.series(binding.entity,now-7*86400,limit=12000)
                events=cycles(observed)
                detection_events[binding.entity]={"events":events,"rows":observed}
                detections[binding.entity]={"binding_status":"HA_ENTITY_ONLY_STABLE_SENSE_ID_UNVERIFIED",
                    "cycles":len(events),"clusters":clusters(events),"identity":"UNRESOLVED",
                    "last_observation":observed[-1][0] if observed else None,
                    "auto_confirmation":False,"control_eligible":False}
                detections[binding.entity]["unclustered_cycles"]=len(events)-sum(
                    g["count"] for g in detections[binding.entity]["clusters"])
                for stable_id,entry in self.store.get("identity_bindings",{}).items():
                    if entry.get("entity")==binding.entity and entry.get("verified") is True:
                        detections[binding.entity]["binding_status"]="REVIEWED_REGISTRY_BINDING"
                        detections[binding.entity]["stable_id"]=stable_id
                        record=self.store.get("identity:"+stable_id)
                        if record:
                            detections[binding.entity]["owner_review"]=record
                            if record["decision"]=="CONFIRM":
                                detections[binding.entity]["identity"]=record["identity"]
                                detections[binding.entity]["identity_classification"]="USER-CONFIRMED"
                if events:
                    detections[binding.entity]["duration_check"]=robust_anomaly(
                        [e["duration_s"] for e in events[:-1]],events[-1]["duration_s"])
        diagnostics["sense_detections"]=detections
        # 0.1.5: flexible-load templates learned from these devices' own cycles; planning uses approved ones only.
        roles={b.entity:b.role for b in self.bindings if b.role.startswith("detection:")}
        self.template_proposals=templates.propose(detections,detection_events,roles)
        approved_templates,drifted_templates=templates.review(self.store,self.template_proposals)
        diagnostics["load_templates"]={"proposed":self.template_proposals,"approved":sorted(approved_templates),
                                       "needs_review":drifted_templates,"physical_actions":False}
        diagnostics["passive_correlations"]=update_passive_evidence(self.store,states,now,detection_events,self.adapters['telemetry'])
        diagnostics["forecast_audit"]=forecast_audit
        if "grid" in series:
            diagnostics["grid_energy_24h"]=energy_integral([r for r in series["grid"] if r[0]>=now-86400])
            grid=by_role["grid"].entity
            # Hourly import/export energy (mean W over a full hour = Wh), kept 365 days for the rate-plan what-if.
            self.store.rollup("grid-import:"+grid,hourly([(t,max(v,0.)) for t,v in series["grid"]]),now)
            self.store.rollup("grid-export:"+grid,hourly([(t,max(-v,0.)) for t,v in series["grid"]]),now)
        if now-self.last_rateplan>=3600:
            grid=by_role["grid"].entity if "grid" in by_role else None
            last=((self.retained_bill or {}).get("bills") or [{}])[-1] if isinstance(self.retained_bill,dict) else {}
            opening={p:(last.get(p) or {}).get("net_balance_kwh") or 0. for p in ("on_peak","off_peak")}
            imports=self.store.rollup("grid-import:"+grid,[],now) if grid else []
            exports=self.store.rollup("grid-export:"+grid,[],now) if grid else []
            self.rate_plan={"stay_put":stay_put_value(self.retained_bill,self.tariff),
                            "what_if":what_if(imports,exports,self.tariff,
                                              self.adapters.get("rate_plan_alternatives") or [],opening)}
            self.insights={"always_on":insights.always_on(self.store,now,diagnostics["baseload"],self.tariff),
                           "bill_projection":insights.bill_projection(imports,exports,self.tariff,opening,now),
                           "cycle_receipts":insights.cycle_receipts(detections,detection_events,self.tariff),
                           "cleaning_value":insights.cleaning_value(self.store,self.tariff,now),
                           "outage_budget":insights.outage_budget(hours.get("load",[])),
                           "explain_bills":insights.explain_bills(self.retained_bill),
                           "forecast_calibration":{role:insights.forecast_calibration(self.store,role)
                                                   for role in ("load","solar")},
                           "bank_runway":insights.bank_runway(self.retained_bill),
                           "self_use":insights.self_use(hours.get("solar",[]),exports)}
            self.insights["credit_headroom"]=insights.credit_headroom(self.insights["bank_runway"],self.tariff,now)
            self.insights["credit_plan"]=insights.credit_plan(self.insights["credit_headroom"],now)
            self.insights["cooling_hint"]=insights.cooling_hint(self.insights["bank_runway"],self.tariff,now)
            temps=self.store.rollup("weather:outdoor-temperature",[],now)
            fitted=weather.weather_load(hours.get("load",[]),temps)
            self.insights["weather_load"]=fitted
            self.insights["weather_months"]=weather.weather_months(
                temps,fitted.get("kwh_per_cooling_degree_hour") if fitted["status"]=="SCORED" else None)
            self.last_rateplan=now
        diagnostics["rate_plan"]=self.rate_plan
        sense_floor=(states.get(self.adapters.get("sense_always_on_entity") or "sensor.always_on_power") or {})
        try:
            sense_floor_w=float(sense_floor.get("state")) if (sense_floor.get("attributes") or {}).get(
                "unit_of_measurement")=="W" else None
        except (TypeError,ValueError):
            sense_floor_w=None
        diagnostics["insights"]={**self.insights,"counter_health":insights.counter_health(
            self.store,states,now,load["value"] if load else None),
            "night_extra":insights.night_extra(diagnostics["baseload"],sense_floor_w,self.tariff,now)}
        plan_digest=None
        plan={"status":"BLOCKED","reasons":["BILLING_CYCLE_BANK_AND_JOB_CONFIGURATION_REQUIRED"]}
        planning=context.get("planning")
        if planning and planning.get('billing_source')=='native':
            if not native_billing['usable_for_planning']:
                planning=None
                plan={'status':'BLOCKED','reasons':['NATIVE_BILLING_NOT_QUALIFIED']}
            else:
                planning={**planning,'bank_kwh':native_billing['bank_kwh'],
                          'billing_month':native_billing['billing_month'],
                          'billing_cycle_end':native_billing['billing_cycle_end'],
                          'expires':min(planning['expires'],native_billing['expires']),
                          'evidence_ref':native_billing['evidence_ref']}
        essential_bad=any(role in issues for role in ("load","solar","grid"))
        if self.paused or budget_hold:
            plan={"status":"BLOCKED","reasons":["PAUSED" if self.paused else "RESOURCE_HOLD"]}
        if (planning and not self.paused and not budget_hold and not essential_bad
                and all(k in forecasts for k in ("load","solar"))):
            # Gross load forecast includes flexible demand unless independently removed.
            if planning.get("base_excludes_flexible_loads") is not True:
                plan={"status":"BLOCKED","reasons":["FLEXIBLE_LOAD_DOUBLE_COUNT_RISK"]}
            else:
                try:
                    objective=BankObjective(self.tariff,planning["billing_month"],planning["bank_kwh"],
                                            planning["terminal_credit_value"],planning["evidence_ref"])
                    digest=hashlib.sha256(encode({"context":context["digest"],"planning":planning,
                                                 "forecasts":forecasts}).encode()).hexdigest()
                    cached=self.store.get("plan-cache",{})
                    if cached.get("digest")==digest and cached.get("expires",0)>now:
                        result=cached["result"]
                    else:
                        result=schedule([r["at"] for r in forecasts["load"]],
                                        [r["watts"] for r in forecasts["load"]],
                                        [r["watts"] for r in forecasts["solar"]],
                                        [Job(**j) for j in planning["jobs"]],objective,
                                        uncertainty_dollars=planning["uncertainty_dollars"])
                        self.store.put("plan-cache",{"digest":digest,"result":result,"expires":planning["expires"]})
                    plan_digest=digest
                    plan={"status":"SHADOW_PROPOSAL","result":result,"expires":min(now+1800,planning["expires"]),
                          "context_revision":context["revision"],"context_evidence":context["evidence_ref"]}
                except (ValueError,KeyError,TypeError):
                    plan={"status":"BLOCKED","reasons":["PLANNING_CONFIGURATION_INVALID"]}
        if context.get("planning") is None and not self.paused and not budget_hold and not essential_bad:
            # 0.1.5: no delivered packet, so the half-hour context is built here from the approved templates.
            plan=templates.plan(approved_templates,forecasts,self.retained_bill,self.tariff,now)
            if plan["status"]=="SHADOW_PROPOSAL":
                plan_digest=hashlib.sha256(encode({"templates":approved_templates,"forecasts":forecasts,
                                                  "bank":plan["bank_kwh"]}).encode()).hexdigest()
        opportunities=[{"id":"NATIVE_QUALIFICATION","owner":"Energy AI engineering/release lane",
                        "reason":"ARM64 and HAOS validation and installation approval required",
                        "supported_value_dollars":None},
                       {"id":"STABLE_SENSE_BINDINGS","owner":"Energy AI + existing Inspector interface owner",
                        "reason":"Review stable unique IDs before identity promotion",
                        "supported_value_dollars":None}]
        if plan["status"]=="BLOCKED":
            opportunities.append({"id":"PLANNING_CONTEXT","owner":"Energy AI engineering",
                "reason":plan["reasons"],"supported_value_dollars":None})
        self.store.put("opportunities",opportunities)
        scorecard=record_scorecard(self.store,now,plan,plan_digest,forecast_audit)
        diagnostics["insights"]["weekly_summary"]=insights.weekly_summary(scorecard,diagnostics["insights"],self.rate_plan)
        self.status=dict(state="PAUSED" if self.paused else "DEGRADED" if issues else "SHADOW",
                         version=__version__,mode=self.mode,at=now,physical_actions=False,
                         data_quality=issues or {"status":"GOOD"},samples=values,
                         model_status=model_status,forecasts=forecasts,plan_status=plan,
                         diagnostics=diagnostics,accepted_identity_register=self.identities.accepted,
                         identity_questions=self.adapters['questions'],
                         opportunities=opportunities,scorecard=scorecard,
                         sense_update_queue=self.identities.queue(),
                         reviewed_context={"status":context["status"],"revision":context.get("revision"),
                                           "reason":context.get("reason")},
                         identity_bindings=self.store.get("identity_bindings",{}),
                         health={"peak_ram_mb":peak_ram_mb,"cycle_seconds":time.monotonic()-start,
                                 "db_bytes":self.store.path.stat().st_size,"optional_training_suspended":budget_hold,
                                 "host_budget":"NOT_BENCHMARKED; development caps only",
                                 "paid_api_calls":0,"external_forecast_calls":0},
                         control_blockers=["NO_COMMISSIONED_EXECUTION_ADAPTER","INSTALLATION_NOT_ACCEPTED"])
        self.status["history_backfill"]=self.store.get("backfill",{"status":"NOT_STARTED"})
        self.store.put("last_status",self.status)
        return self.status

    def hold(self, now, reason):
        """Kill-switch hold: no collection, model update, forecast or plan this cycle."""
        self.status={**self.status,"state":"KILL_SWITCH_HOLD","at":now,"physical_actions":False,
                     "hold_reason":reason,"plan_status":{"status":"UNAVAILABLE"},"forecasts":{}}
        return self.status

    def failure(self, now, code):
        self.status={**self.status,"state":"DEGRADED","at":now,"physical_actions":False,
                     "data_quality":{"all":code},"plan_status":{"status":"UNAVAILABLE"},"forecasts":{}}
        return self.status

    def close(self):
        self.store.close()
