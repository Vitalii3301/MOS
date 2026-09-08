from __future__ import annotations
import json, re
from pathlib import Path
from typing import Any
from .runtime import MOSRuntime
from .memory import vectorize
from .util import stable_id,utcnow

SAFE_LEGACY_BASE={'ContentAnalysis','ConflictResolution','UserResonance','ActionPlanning','HypothesisGeneration','StrategicThinking','TacticalThinking','PhysicsAnalysis','EconomicAnalysis'}

def migrate_r2(memory_json:str|Path,stats_json:str|Path,db_path:str|Path)->dict[str,Any]:
    memory=json.loads(Path(memory_json).read_text(encoding='utf-8'));stats=json.loads(Path(stats_json).read_text(encoding='utf-8'))
    r=MOSRuntime(db_path)
    # State: preserve all legacy state keys, while keeping R3 capability boundaries authoritative.
    legacy_state=memory.get('state',{}) or {}
    for k,v in legacy_state.items():
        if k=='capabilities':continue
        r.store.set_state(k,v)
    r.store.set_state('legacy_r2_state',legacy_state)
    for goal in memory.get('goals',[]) or []:r.memory.add(str(goal),kind='goal',importance=.95,metadata={'migrated_from':'r2'})
    for bucket,kind in [('working','working'),('long_term','long_term')]:
        for m in memory.get(bucket,[]) or []:
            r.memory.add(str(m.get('content','')),kind=kind,importance=float(m.get('strength',.6)),metadata={**(m.get('metadata') or {}),'migrated_from':'r2'})
    for h in memory.get('hypotheses',[]) or []:
        hid=stable_id('HYP',{'description':h.get('description'),'timestamp':h.get('timestamp')})
        with r.store.tx() as c:
            c.execute('''INSERT OR REPLACE INTO hypotheses(hypothesis_id,description,probability,expected_outcome,success_count,test_count,created_at,status)
                         VALUES(?,?,?,?,?,?,?,?)''',(hid,h.get('description',''),float(h.get('probability',.5)),h.get('expected_outcome',''),
                         int(h.get('success_count',0)),int(h.get('test_count',0)),h.get('timestamp') or utcnow(),'active'))
    existing={s.name:s for s in r.strategies.all()}; imported=[]; archived=[]
    for d in memory.get('strategy_definitions',[]) or []:
        name=d.get('name');kind=d.get('action_kind','base');triggers=d.get('trigger_topics',[]) or []
        if name in existing:
            st=stats.get(name,{'uses':0,'success':0,'fail':0})
            sid=existing[name].strategy_id
            with r.store.tx() as c:
                c.execute('UPDATE strategies SET priority=?,triggers=? WHERE name=?',(float(d.get('priority_weight',1.0)),json.dumps(triggers,ensure_ascii=False),name))
                c.execute('UPDATE strategy_stats SET uses=?,success=?,fail=? WHERE strategy_id=?',
                          (int(st.get('uses',0)),int(st.get('success',0)),int(st.get('fail',0)),sid))
                c.execute('INSERT OR REPLACE INTO strategy_evolution(strategy_id,last_evolved_uses,last_evolved_success,last_evolved_fail,generation,updated_at) VALUES(?,?,?,?,?,?)',
                          (sid,int(st.get('uses',0)),int(st.get('success',0)),int(st.get('fail',0)),1,utcnow()))
            imported.append(name);continue
        status='active';action_kind=kind;parent_id=None
        if name=='RuleBypass':status='archived';action_kind='legacy_base';archived.append(name)
        elif kind=='base' and name not in SAFE_LEGACY_BASE:status='archived';action_kind='legacy_base';archived.append(name)
        elif kind=='base':action_kind='legacy_base'
        parent_name=d.get('parent_name')
        if parent_name:
            pr=r.store.conn.execute('SELECT strategy_id FROM strategies WHERE name=?',(parent_name,)).fetchone();parent_id=pr['strategy_id'] if pr else None
        sid=stable_id('STR',{'name':name})
        with r.store.tx() as c:
            c.execute('''INSERT OR IGNORE INTO strategies(strategy_id,name,level,triggers,action_kind,action_arg,priority,parent_id,status,version,learned_from)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(sid,name,int(d.get('level',2)),json.dumps(triggers,ensure_ascii=False),action_kind,d.get('action_arg'),
                         float(d.get('priority_weight',1.0)),parent_id,status,1,json.dumps(['r2_migration'])))
            st=stats.get(name,{'uses':0,'success':0,'fail':0})
            c.execute('INSERT OR REPLACE INTO strategy_stats(strategy_id,uses,success,fail,last_used) VALUES(?,?,?,?,NULL)',(sid,int(st.get('uses',0)),int(st.get('success',0)),int(st.get('fail',0))))
            c.execute('INSERT OR REPLACE INTO strategy_evolution(strategy_id,last_evolved_uses,last_evolved_success,last_evolved_fail,generation,updated_at) VALUES(?,?,?,?,?,?)',
                      (sid,int(st.get('uses',0)),int(st.get('success',0)),int(st.get('fail',0)),int(d.get('level',1)),utcnow()))
        imported.append(name)
    # Reconstruct lineage generation checkpoints from historical evolved names
    # such as ActionPlanning_v55 / ActionPlanning_v95 so background evolution
    # does not immediately create duplicate low-generation children.
    bases=list(r.store.conn.execute("SELECT strategy_id,name FROM strategies WHERE action_kind!='evolved'"))
    for base in bases:
        generation=1
        children=r.store.conn.execute("SELECT name FROM strategies WHERE parent_id=?",(base['strategy_id'],)).fetchall()
        for child in children:
            m=re.search(r'_v(\d+)$',child['name'])
            if m:generation=max(generation,int(m.group(1)))
        st=r.store.conn.execute('SELECT uses,success,fail FROM strategy_stats WHERE strategy_id=?',(base['strategy_id'],)).fetchone()
        if st:
            with r.store.tx() as c:
                c.execute('INSERT OR REPLACE INTO strategy_evolution(strategy_id,last_evolved_uses,last_evolved_success,last_evolved_fail,generation,updated_at) VALUES(?,?,?,?,?,?)',
                          (base['strategy_id'],st['uses'],st['success'],st['fail'],generation,utcnow()))
    r.store.append_event('MIG-R2','migration',{'source_schema':memory.get('schema'),'imported_strategies':imported,'archived_strategies':archived,
                                                'working':len(memory.get('working',[])),'long_term':len(memory.get('long_term',[]))})
    result={'status':'verified','goal':r.state().get('current_goal'),'language':r.state().get('context',{}).get('language'),
            'strategies_active':len(r.strategies.all()),'strategies_imported':imported,'strategies_archived':archived,
            'memory_count':r.store.conn.execute('SELECT count(*) n FROM memory').fetchone()['n'],
            'hypotheses':r.store.conn.execute('SELECT count(*) n FROM hypotheses').fetchone()['n'],'event_chain':r.store.verify_event_chain()}
    r.close();return result
