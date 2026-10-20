"""Persist decision-time forecasts, score only later observations, support rollback."""
import hashlib
import statistics
from .store import encode
from .forecast import predict, seasonal


class ModelAudit:
    def __init__(self, store):
        self.store=store
        store.db.executescript('''CREATE TABLE IF NOT EXISTS forecast_audit (
            role TEXT, issued REAL, target REAL, model_id TEXT, candidate REAL, baseline REAL,
            actual REAL, PRIMARY KEY(role,target));
            CREATE TABLE IF NOT EXISTS forecast_reference (
            role TEXT, issued REAL, target REAL, reference REAL, PRIMARY KEY(role,target));''')
        store.db.commit()

    def observe(self, role, rows, report, now, allow_issue=True, reference=None):
        if role not in {'load','solar'}:
            raise ValueError('MODEL_ROLE')
        rows=[(at,value) for at,value in rows if at+3600<=now]
        pending={r[0] for r in self.store.db.execute(
            'SELECT target FROM forecast_audit WHERE role=? AND actual IS NULL AND issued<target',(role,))}
        # A prediction is fixed before its target interval starts. Missing targets stay unscored.
        with self.store.db:
            self.store.db.executemany('UPDATE forecast_audit SET actual=? WHERE role=? AND target=? '
                                     'AND actual IS NULL AND issued<target',
                                     [(value,role,at) for at,value in rows if at in pending])
            self.store.db.execute('DELETE FROM forecast_audit WHERE target<?',(now-35*86400,))
            self.store.db.execute('DELETE FROM forecast_reference WHERE target<?',(now-35*86400,))
        trained=rows[-1][0] if rows else None
        model=report.get('model') if report.get('promote') else None
        model_id=hashlib.sha256(encode(model or {'kind':'seasonal','through':trained}).encode()).hexdigest()[:16]
        if rows and allow_issue:
            target=(int(now//3600)+1)*3600
            baseline=seasonal(rows,target)
            candidate=predict(model,target) if model else baseline
            with self.store.db:
                self.store.db.execute('INSERT OR IGNORE INTO forecast_audit VALUES (?,?,?,?,?,?,NULL)',
                                     (role,now,target,model_id,candidate,baseline))
                if reference is not None:
                    # The existing integration's estimate is frozen before its target hour, like ours.
                    self.store.db.execute('INSERT OR IGNORE INTO forecast_reference VALUES (?,?,?,?)',
                                         (role,now,target,reference))
        scored=list(self.store.db.execute('SELECT a.candidate,a.baseline,a.actual,r.reference FROM forecast_audit a '
                                         'LEFT JOIN forecast_reference r ON r.role=a.role AND r.target=a.target AND r.issued<r.target '
                                         'WHERE a.role=? AND a.actual IS NOT NULL ORDER BY a.target DESC LIMIT 168',(role,)))
        result={'status':'COLLECTING','count':len(scored),'model_id':model_id,
                'savings_claim':False,'method':'immutable prediction made before observed target hour'}
        if len(scored)>=24:
            mae=statistics.mean(abs(c-a) for c,b,a,r in scored)
            base=statistics.mean(abs(b-a) for c,b,a,r in scored)
            result.update(status='DRIFT_REVIEW' if mae>max(base*1.2,base+50) else 'TRACKING',
                          deployed_mae_w=mae,baseline_mae_w=base)
            paired=[(c,a,r) for c,b,a,r in scored if r is not None]
            if len(paired)>=24:
                # Same hours only: the deployed forecast must beat the existing reference to keep promotion.
                deployed=statistics.mean(abs(c-a) for c,a,r in paired)
                reference_mae=statistics.mean(abs(r-a) for c,a,r in paired)
                result.update(reference_count=len(paired),reference_mae_w=reference_mae,
                              paired_deployed_mae_w=deployed,reference_better=reference_mae<deployed*.95)
        self.store.put('forecast-audit:'+role,result)
        return result

    def rollback(self, role, now):
        if role not in {'load','solar'}:
            raise ValueError('MODEL_ROLE')
        previous=self.store.get('previous-model:'+role)
        if not isinstance(previous,dict):
            raise ValueError('NO_PRIOR_MODEL')
        current=self.store.get('model:'+role)
        with self.store.db:
            for key,value in [('model:'+role,previous),('previous-model:'+role,current),
                              ('model-hold:'+role,now+86400)]:
                self.store.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)',(key,encode(value)))
        self.store.event('model-rollback:'+role+':'+str(now),now,'MODEL_ROLLBACK',
                         {'role':role,'automatic_promotion_held_until':now+86400})
        return {'role':role,'status':'ROLLED_BACK','hold_until':now+86400,'physical_actions':False}
