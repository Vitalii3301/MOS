from __future__ import annotations
import json, os, subprocess, sys
from pathlib import Path
import pytest

from mos import MOSRuntime, CallableLLMInterface, SubprocessLLMInterface
from mos.memory import vectorize
from mos.security import CodePolicy, AntiMeme
from mos.llm import LLMInterface

def fresh(tmp_path, llm=None):
    return MOSRuntime(tmp_path/'mos.sqlite3',llm=llm)

def test_boot_and_integrity(tmp_path):
    r=fresh(tmp_path);v=r.verify();assert v['db_integrity']=='ok' and v['event_chain'] and v['strategies']>=15;assert r.state()['capabilities']['base_model_weight_change']['active'] is False;r.close()

def test_state_persists_fresh_instance(tmp_path):
    r=fresh(tmp_path);r.set_goal('persistent_goal');r.store.set_state('energy',42.25);r.close();r=fresh(tmp_path);assert r.state()['current_goal']=='persistent_goal';assert r.state()['energy']==42.25;r.close()

def test_state_persists_new_python_process(tmp_path):
    db=tmp_path/'cross.sqlite3';root=Path(__file__).resolve().parents[1];env=os.environ.copy();env['PYTHONPATH']=str(root)
    code1=f"from mos import MOSRuntime\nr=MOSRuntime(r'{db}');r.set_goal('cross_process_goal');r.store.set_state('pain',2.5);r.close()"
    code2=f"import json\nfrom mos import MOSRuntime\nr=MOSRuntime(r'{db}');print(json.dumps({{'goal':r.state()['current_goal'],'pain':r.state()['pain']}}));r.close()"
    subprocess.run([sys.executable,'-c',code1],check=True,env=env);out=subprocess.run([sys.executable,'-c',code2],check=True,env=env,capture_output=True,text=True).stdout;assert json.loads(out)=={'goal':'cross_process_goal','pain':2.5}

def test_deterministic_vectors(): assert vectorize('same input',16)==vectorize('same input',16)

def test_deterministic_vectors_cross_process(tmp_path):
    root=Path(__file__).resolve().parents[1];code="import json;from mos.memory import vectorize;print(json.dumps(vectorize('same input',16)))";env=os.environ.copy();env['PYTHONPATH']=str(root)
    a=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True,check=True).stdout;b=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True,check=True).stdout;assert json.loads(a)==json.loads(b)

def test_antimeme_regex():
    a=AntiMeme('Bias','always|never|impossible','consider alternatives');assert a.detect('ALWAYS') and a.detect('impossible') and not a.detect('sometimes')

def test_code_policy_blocks_import_and_open():
    p=CodePolicy();assert not p.scan('import os\nresult=1').allowed;assert not p.scan("result=open('x').read()").allowed;assert p.scan('result=sum([1,2,3])').allowed

def test_meme_create_execute(tmp_path):
    r=fresh(tmp_path);r.memes.create('adder',"def meme_main(input_data):\n    return input_data['a']+input_data['b']");assert r.memes.execute('adder',{'a':2,'b':5})=={'ok':True,'result':7};r.close()

def test_meme_timeout(tmp_path):
    r=fresh(tmp_path);r.memes.create('loop','def meme_main(input_data):\n    while True:\n        pass');out=r.memes.execute('loop',None,timeout=.15);assert out['ok'] is False and out['error']=='timeout';r.close()

def test_meme_rejects_import(tmp_path):
    r=fresh(tmp_path)
    with pytest.raises(ValueError):r.memes.create('bad','import os\nresult=1')
    r.close()

def test_meme_version_diff_rollback(tmp_path):
    r=fresh(tmp_path);r.memes.create('x','result=1');r.memes.update('x','result=2','improve');d=r.memes.diff('x',1,2);assert '-result=1' in d and '+result=2' in d;assert r.memes.rollback('x',1);assert r.memes.current('x')['version']==3;assert r.memes.execute('x',None)['result']==1;r.close()

def test_meme_fitness_and_evolution(tmp_path):
    r=fresh(tmp_path);r.memes.create('identity','def meme_main(input_data):\n    return 0');cases=[{'input':1,'expected':1},{'input':2,'expected':2},{'input':3,'expected':3}];good='def meme_main(input_data):\n    return input_data';bad='def meme_main(input_data):\n    return -1';ev=r.memes.evolve('identity',[bad,good],cases);assert ev['improved'] and ev['to']==1.0 and ev['regressions']==0 and ev['candidate_source']=='host_supplied';assert r.memes.execute('identity',3)['result']==3;r.close()

def test_meme_ontology_lineage(tmp_path):
    r=fresh(tmp_path);r.memes.create('parent','result=1');r.memes.create('child','result=2',parents=['parent']);o=r.memes.ontology();assert any(e=={'from':'parent','to':'child'} for e in o['edges']);r.close()

def test_mpl_create_execute_list(tmp_path):
    r=fresh(tmp_path);script='''meme calc {\ndef meme_main(input_data):\n    return input_data * 2\n}\nexecute calc 4\nlist''';out=r.mpl.execute(script);assert out[1]['result']['result']==8;assert any(n['name']=='calc' for n in out[2]['ontology']['nodes']);r.close()

def test_memory_retrieve(tmp_path):
    r=fresh(tmp_path);r.memory.add('pressure drop old 20 new 15',kind='long_term',importance=.9);r.memory.add('unrelated flowers',kind='long_term',importance=.2);got=r.memory.retrieve('pressure drop',top_k=1);assert 'pressure drop' in got[0].content;r.close()

def test_memory_usage_reinforces(tmp_path):
    r=fresh(tmp_path);mid=r.memory.add('repeated important memory',importance=.9);before=r.store.conn.execute('SELECT usage_count,strength FROM memory WHERE memory_id=?',(mid,)).fetchone();r.memory.retrieve('important memory',top_k=1);after=r.store.conn.execute('SELECT usage_count,strength FROM memory WHERE memory_id=?',(mid,)).fetchone();assert after['usage_count']==before['usage_count']+1 and after['strength']>=before['strength'];r.close()

def test_strategy_selection(tmp_path):
    r=fresh(tmp_path);sel=r.strategies.select('план следующий strategy',r.state()['current_goal']);assert any(s.name=='ActionPlanning' for _,s in sel);r.close()

def test_strategy_learning_persists(tmp_path):
    r=fresh(tmp_path)
    for i in range(3):r.memory.add(f'quasar context {i}',kind='user')
    created=r.strategies.learn();assert 'Auto_Quasar' in created;r.close();r=fresh(tmp_path);assert any(s.name=='Auto_Quasar' for s in r.strategies.all());r.close()

def test_strategy_evolution_persists(tmp_path):
    r=fresh(tmp_path);sid=next(s.strategy_id for s in r.strategies.all() if s.name=='ActionPlanning')
    with r.store.tx() as c:c.execute('UPDATE strategy_stats SET uses=5,success=5,fail=0 WHERE strategy_id=?',(sid,))
    made=r.strategies.evolve();assert made;evolved=made[0];assert all(s.name!=evolved for s in r.strategies.all());r.close();r=fresh(tmp_path);assert any(s.name==evolved for s in r.strategies.candidates());r.close()

def test_episode_full_lifecycle(tmp_path):
    r=fresh(tmp_path);eid=r.episodes.open('goal','hyp');r.episodes.add_evidence(eid,{'kind':'test','value':1});r.episodes.decide(eid,'go','act');r.episodes.outcome(eid,'bad','revise');ep=r.episodes.get(eid);assert ep.status=='revised' and ep.revision=='revise' and len(ep.evidence)==1;assert r.store.verify_event_chain();r.close()

def test_meta_learning_from_revision(tmp_path):
    r=fresh(tmp_path);eid=r.episodes.open('g','h');r.episodes.decide(eid,'d','a');r.episodes.outcome(eid,'failed','change');items=r.meta.learn_from_episode(r.episodes.get(eid));assert len(items)==2;assert any(x['kind']=='weakness' for x in r.meta.active_self());r.close()

def test_context_contains_self_model_and_strategies(tmp_path):
    r=fresh(tmp_path);c=r.compiler.compile(r.state()['current_goal'],'проверь evidence и план',r.state());kinds={x['kind'] for x in c['items']};assert 'strategy' in kinds and any(k.startswith('self_') for k in kinds);assert c['budget']['selected']<=32;r.close()

def test_context_deterministic_same_state(tmp_path):
    r=fresh(tmp_path);a=r.compiler.compile('g','plain request',r.state());b=r.compiler.compile('g','plain request',r.state());assert a['context_sha256']==b['context_sha256'];r.close()

def test_mandatory_check_numeric(tmp_path):
    r=fresh(tmp_path);r.meta.self_item('mandatory_check','numeric','Parse numeric comparisons before stance.',confidence=1.0);c=r.compiler.compile('g','old 10 new 8',r.state());checks=r.checks.route(c,'old 10 new 8');assert any(x['check_id']=='numeric_comparison' for x in checks);r.close()

def test_callable_llm_bridge(tmp_path):
    seen=[];r=fresh(tmp_path,CallableLLMInterface(lambda p: seen.append(p) or 'HOST'));res=r.process('plan next');assert res.response=='HOST' and seen and seen[0]['schema']=='mos-llm-cycle/v4.1';assert seen[0]['state_boundary']['base_model_weight_change'] is False;r.close()

def test_subprocess_llm_bridge(tmp_path):
    host=tmp_path/'host.py';host.write_text("import sys,json\np=json.load(sys.stdin)\nprint('SUB:'+p['cycle_id'])\n",encoding='utf-8');r=fresh(tmp_path,SubprocessLLMInterface([sys.executable,str(host)]));res=r.process('план next');assert res.response.startswith('SUB:CYCLE-');r.close()

def test_cycle_persists_response_and_episode(tmp_path):
    r=fresh(tmp_path);res=r.process('проверь систему');assert r.state()['last_response']==res.response;assert r.episodes.get(res.episode_id).decision is not None;assert r.verify()['event_chain'];r.close()

def test_outcome_revision_changes_self_model(tmp_path):
    r=fresh(tmp_path);res=r.process('test');r.record_outcome(res.episode_id,'failed','need stronger evidence');assert r.state()['pain']==1.0;assert any(x['kind']=='weakness' for x in r.meta.active_self());r.close()

def test_relay_export_import_and_conflict(tmp_path):
    r=fresh(tmp_path);r.set_goal('relay_goal');snap=tmp_path/'snap.json';env=r.relay.export(snap);rev=env['payload']['revision'];r.set_goal('changed');r.relay.import_snapshot(snap,expected_revision=rev);assert r.state()['current_goal']=='relay_goal'
    with pytest.raises(RuntimeError):r.relay.import_snapshot(snap,expected_revision=999)
    r.close()

def test_relay_detects_tamper(tmp_path):
    r=fresh(tmp_path);p=tmp_path/'s.json';r.relay.export(p);d=json.loads(p.read_text());d['payload']['tables']['state'][0]['value']='999';p.write_text(json.dumps(d))
    with pytest.raises(ValueError):r.relay.import_snapshot(p)
    r.close()

def test_research_without_adapter_is_honest(tmp_path):
    r=fresh(tmp_path);x=r.research.run('current claim');assert x['status']=='adapter_unavailable' and x['network_used'] is False;r.close()

def test_research_adapter(tmp_path):
    class A:
        def fetch(self,q):return [{'source':'fixture','claim':q}]
    r=MOSRuntime(tmp_path/'x.sqlite3',research_adapter=A());x=r.research.run('abc');assert x['status']=='completed' and x['adapter_used'] and not x['network_used'] and x['evidence'][0]['claim']=='abc';r.close()

def test_event_chain_tamper_detection(tmp_path):
    r=fresh(tmp_path);r.process('one');assert r.store.verify_event_chain()
    with r.store.tx() as c:c.execute("UPDATE events SET body='{}' WHERE seq=(SELECT min(seq) FROM events)")
    assert not r.store.verify_event_chain();r.close()

def test_sqlite_integrity_after_cycles(tmp_path):
    r=fresh(tmp_path)
    for i in range(12):r.process(f'cycle {i} plan verify')
    assert r.verify()['db_integrity']=='ok' and r.verify()['episodes']==12;r.close()

def test_atomic_backup(tmp_path):
    r=fresh(tmp_path);r.process('backup');b=tmp_path/'backup.sqlite3';r.store.backup(b);r.close();rb=MOSRuntime(b);assert rb.verify()['db_integrity']=='ok' and rb.state()['last_input']=='backup';rb.close()

def test_local_fallback_explicit(tmp_path):
    r=fresh(tmp_path);assert r.verify()['llm_mode']=='local_rules';assert 'no language model' in LLMInterface().generate({'active_context':{'goal':'g'},'strategy_outputs':[]});assert r.verify()['llm_provider_active'] is False;r.close()

def test_no_hidden_claims_in_capabilities(tmp_path):
    r=fresh(tmp_path);c=r.state()['capabilities'];assert c['hidden_cross_chat_memory']['active'] is False;assert c['base_model_weight_change']['active'] is False;assert c['consciousness_or_feelings']['active'] is False;r.close()

def test_restart_after_full_cycle(tmp_path):
    r=fresh(tmp_path);res=r.process('полный цикл план evidence');r.record_outcome(res.episode_id,'ok');s=r.state();r.close();r=fresh(tmp_path);assert r.state()['cycle_count']==s['cycle_count'];assert r.state()['last_response']==s['last_response'];assert r.verify()['episodes']==1;r.close()

def test_many_memes_versions_and_rollback(tmp_path):
    r=fresh(tmp_path);r.memes.create('v','result=0')
    for i in range(1,8):r.memes.update('v',f'result={i}',f'v{i}')
    assert r.memes.current('v')['version']==8;assert r.memes.rollback('v',4);assert r.memes.execute('v',None)['result']==3;r.close()

def test_mpl_unknown_command_fails(tmp_path):
    r=fresh(tmp_path)
    with pytest.raises(ValueError):r.mpl.execute('explode everything')
    r.close()

def test_strategy_dynamic_cap(tmp_path):
    r=fresh(tmp_path)
    for n in range(30):
        for i in range(3):r.memory.add(f'tokenword{n} signal {i}',kind='user')
    r.strategies.learn(max_new=30,max_dynamic=5);count=sum(1 for s in r.strategies.all() if s.action_kind=='learned_token');assert count<=5;r.close()

def test_verify_counts(tmp_path):
    r=fresh(tmp_path);r.memes.create('m','result=1');r.process('x');v=r.verify();assert v['memes']==1 and v['episodes']==1 and v['self_model']>=3;r.close()

def test_state_controller_language_and_emotion(tmp_path):
    r=fresh(tmp_path);before=r.state()['energy'];r.process('почему нужен следующий план strategy?');s=r.state();assert s['context']['language']=='ru';assert s['energy']<before;assert s['emotion'] in {'curious','motivated'};r.close()

def test_observer_manual_writes_meta_memory(tmp_path):
    r=fresh(tmp_path);x=r.observer.observe('manual');assert x['reason']=='manual';assert any(m['kind']=='self_observation' for m in r.meta.active_meta());r.close()

def test_background_reflection_runs_and_stops(tmp_path):
    import time
    r=fresh(tmp_path);assert r.start_background_reflection(.05);time.sleep(.18);assert r.observer.running;r.stop_background_reflection();assert not r.observer.running;assert r.store.conn.execute("SELECT count(*) n FROM events WHERE event_type='self_observation'").fetchone()['n']>=2;r.close()

def test_orchestrator_dispatch(tmp_path):
    from mos.orchestrator import MOSOrchestrator
    o=MOSOrchestrator(tmp_path/'pool',n_nodes=2)
    try:
        res=o.dispatch('план next verify');assert res.response;v=o.verify_all();assert len(v)==2 and all(x['db_integrity']=='ok' for x in v)
    finally:o.close()

def test_r2_migration_real_fixture(tmp_path):
    from mos.migration import migrate_r2
    root=Path(__file__).resolve().parents[1]/'fixtures'/'r2'
    out=migrate_r2(root/'restored_memory_r2.json',root/'restored_stats_r2.json',tmp_path/'migrated.sqlite3')
    assert out['status']=='verified' and out['goal']=='probe_goal_ABC' and out['language']=='ru'
    assert out['memory_count']>=10 and out['hypotheses']==1 and out['event_chain']
    assert 'RuleBypass' in out['strategies_archived']
    r=MOSRuntime(tmp_path/'migrated.sqlite3');assert any(s.name=='Auto_Следующий' for s in r.strategies.all());assert not any(s.name=='RuleBypass' for s in r.strategies.all());r.close()

def test_r2_migration_preserves_dynamic_restart(tmp_path):
    from mos.migration import migrate_r2
    root=Path(__file__).resolve().parents[1]/'fixtures'/'r2'
    db=tmp_path/'m.sqlite3';migrate_r2(root/'restored_memory_r2.json',root/'restored_stats_r2.json',db)
    a=MOSRuntime(db);names1={s.name for s in a.strategies.all()};a.close();b=MOSRuntime(db);names2={s.name for s in b.strategies.all()};row=b.store.conn.execute("SELECT status FROM strategies WHERE name='ActionPlanning_v55'").fetchone();b.close();assert names1==names2 and 'ActionPlanning_v55' not in names2 and row['status']=='quarantined'

def test_http_service_auth_and_cycle(tmp_path):
    import urllib.request,urllib.error
    from mos.service import MOSHTTPService
    s=MOSHTTPService(tmp_path/'http.sqlite3',host='127.0.0.1',port=0,token='secret');s.start_background();host,port=s.address;base=f'http://{host}:{port}'
    try:
        with pytest.raises(urllib.error.HTTPError) as ei:urllib.request.urlopen(base+'/health',timeout=3)
        assert ei.value.code==401
        req=urllib.request.Request(base+'/health',headers={'Authorization':'Bearer secret'})
        health=json.loads(urllib.request.urlopen(req,timeout=3).read());assert health['status']=='ok'
        data=json.dumps({'text':'план verify'}).encode();req=urllib.request.Request(base+'/ask',data=data,method='POST',headers={'Authorization':'Bearer secret','Content-Type':'application/json'})
        res=json.loads(urllib.request.urlopen(req,timeout=5).read());assert res['response'] and res['episode_id']
    finally:s.shutdown()

def test_http_service_mpl(tmp_path):
    import urllib.request
    from mos.service import MOSHTTPService
    s=MOSHTTPService(tmp_path/'http2.sqlite3',host='127.0.0.1',port=0,token=None);s.start_background();host,port=s.address
    try:
        body={'script':'meme z {\nresult=9\n}\nexecute z'};req=urllib.request.Request(f'http://{host}:{port}/mpl',data=json.dumps(body).encode(),method='POST',headers={'Content-Type':'application/json'})
        out=json.loads(urllib.request.urlopen(req,timeout=5).read());assert out['results'][1]['result']['result']==9
    finally:s.shutdown()

def test_outcome_feedback_drives_strategy_evolution_and_restart(tmp_path):
    r=fresh(tmp_path)
    for i in range(3):
        res=r.process('план следующий strategy')
        feedback=r.record_outcome(res.episode_id,'worked',success=True)
    names={s.name for s in r.strategies.candidates()};assert any(n.startswith('ActionPlanning_v') for n in names)
    stats=r.store.conn.execute("SELECT st.uses,st.success FROM strategy_stats st JOIN strategies s USING(strategy_id) WHERE s.name='ActionPlanning'").fetchone();assert stats['uses']>=3 and stats['success']>=3
    r.close();r=fresh(tmp_path);assert any(s.name.startswith('ActionPlanning_v') for s in r.strategies.candidates());r.close()

def test_failed_outcome_updates_strategy_fail_and_debt(tmp_path):
    r=fresh(tmp_path);res=r.process('план следующий strategy');before=r.state()['debt'];r.record_outcome(res.episode_id,'failed','revise',success=False);after=r.state()['debt'];assert after>before
    stats=r.store.conn.execute("SELECT st.fail FROM strategy_stats st JOIN strategies s USING(strategy_id) WHERE s.name='ActionPlanning'").fetchone();assert stats['fail']>=1;r.close()
