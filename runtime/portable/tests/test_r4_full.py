from __future__ import annotations
import json, os, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest

from mos import MOSRuntime, CallableLLMInterface


def fresh(tmp_path, name="mos.sqlite3", **kw):
    return MOSRuntime(tmp_path/name, **kw)


def test_full_relay_restores_all_cognitive_tables(tmp_path):
    src = fresh(tmp_path, "src.sqlite3")
    src.set_goal("full_relay_goal")
    for i in range(3):
        src.memory.add(f"quasar continuity {i}", kind="user", importance=.8)
    src.strategies.learn()
    src.memes.create("calc", "def meme_main(input_data):\n    return input_data*2")
    src.memes.update("calc", "def meme_main(input_data):\n    return input_data*3", "triple")
    cycle = src.process("research verify план continuity")
    src.record_outcome(cycle.episode_id, "worked", success=True)
    src.meta.remember("fact", "relay-specific meta memory", importance=.91, confidence=.99, verification_status="verified")
    snapshot = tmp_path/"full_snapshot.json"
    env = src.relay.export(snapshot)
    source_counts = env["payload"]["table_counts"]
    src.close()

    dst = fresh(tmp_path, "dst.sqlite3")
    dst.set_goal("wrong_goal")
    dst.memes.create("junk", "result=99")
    dst.relay.import_snapshot(snapshot)
    assert dst.state()["current_goal"] == "full_relay_goal"
    assert dst.memes.current("calc")["version"] == 2
    assert dst.memes.execute("calc", 4)["result"] == 12
    assert not dst.store.conn.execute("SELECT 1 FROM memes WHERE name='junk'").fetchone()
    assert any(s.name == "Auto_Quasar" for s in dst.strategies.all())
    assert dst.store.conn.execute("SELECT count(*) n FROM hypotheses").fetchone()["n"] >= 1
    assert dst.store.conn.execute("SELECT count(*) n FROM episodes").fetchone()["n"] >= 1
    assert any(x["statement"] == "relay-specific meta memory" for x in dst.meta.active_meta(50))
    assert dst.verify()["event_chain"]
    # The imported chain has one additional relay_import event; cognitive tables otherwise restore.
    assert dst.store.conn.execute("SELECT count(*) n FROM memory").fetchone()["n"] == source_counts["memory"]
    dst.close()


def test_relay_backup_created_and_tamper_detected(tmp_path):
    r = fresh(tmp_path)
    snap = tmp_path/"s.json"
    r.relay.export(snap)
    env = json.loads(snap.read_text(encoding="utf-8"))
    env["payload"]["tables"]["memory"].append({"fake": "row"})
    snap.write_text(json.dumps(env), encoding="utf-8")
    with pytest.raises(ValueError):
        r.relay.import_snapshot(snap)
    r.close()


def test_event_chain_atomic_under_concurrent_append(tmp_path):
    r = fresh(tmp_path)
    def write(i):
        return r.store.append_event(f"CONCURRENT-{i}", "probe", {"i": i})
    with ThreadPoolExecutor(max_workers=12) as ex:
        hashes = list(ex.map(write, range(50)))
    assert len(set(hashes)) == 50
    assert r.store.verify_event_chain()
    assert r.store.conn.execute("SELECT count(*) n FROM events WHERE event_type='probe'").fetchone()["n"] == 50
    r.close()


def test_concurrent_runtime_cycles_are_serialized_and_consistent(tmp_path):
    r = fresh(tmp_path)
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(lambda i: r.process(f"план verify concurrent {i}"), range(10)))
    assert len({x.cycle_id for x in results}) == 10
    assert r.state()["cycle_count"] == 10
    assert r.verify()["event_chain"]
    assert r.verify()["episodes"] == 10
    r.close()


def test_failed_llm_cycle_is_recorded_and_revises_state(tmp_path):
    def boom(payload):
        raise RuntimeError("provider down")
    r = fresh(tmp_path, llm=CallableLLMInterface(boom))
    with pytest.raises(RuntimeError, match="provider down"):
        r.process("план verify provider")
    assert r.state()["pain"] > 0 and r.state()["debt"] > 0
    assert r.state()["last_error"]["error"].startswith("RuntimeError")
    row = r.store.conn.execute("SELECT status,revision,outcome FROM episodes ORDER BY created_at DESC LIMIT 1").fetchone()
    assert row["status"] == "revised" and row["revision"] == "repair_or_retry_failed_cycle"
    assert r.verify()["event_chain"]
    r.close()


def test_research_is_automatically_routed_into_cycle_and_context(tmp_path):
    class Adapter:
        network_used = False
        def fetch(self, query):
            return [{"source": "fixture://one", "claim": "verified fixture evidence", "query": query}]
    seen = []
    r = MOSRuntime(tmp_path/"research.sqlite3", llm=CallableLLMInterface(lambda p: seen.append(p) or "HOST"), research_adapter=Adapter())
    res = r.process("research source verify evidence for architecture")
    assert res.research and res.research["status"] == "completed"
    assert res.research["adapter_used"] and res.research["network_used"] is False
    assert any(x["kind"] == "research" for x in r.episodes.get(res.episode_id).evidence)
    assert seen and seen[0]["research"]["evidence"][0]["claim"] == "verified fixture evidence"
    assert any(item["source"] == "memory" and "verified fixture evidence" in item["text"] for item in seen[0]["active_context"]["items"])
    r.close()


def test_research_requested_without_adapter_is_explicit_unknown(tmp_path):
    seen=[]
    r=MOSRuntime(tmp_path/"no_research.sqlite3",llm=CallableLLMInterface(lambda p:seen.append(p) or "HOST"))
    res=r.process("verify source proof")
    assert res.research and res.research["status"]=="adapter_unavailable"
    assert seen[0]["research"]["status"]=="adapter_unavailable"
    r.close()


def test_hypothesis_updates_from_outcome_and_enters_future_context(tmp_path):
    r=fresh(tmp_path)
    res=r.process("план test hypothesis")
    before=r.hypotheses.get(res.hypothesis_id)
    assert before["test_count"]==0
    fb=r.record_outcome(res.episode_id,"worked",success=True)
    after=r.hypotheses.get(res.hypothesis_id)
    assert after["test_count"]==1 and after["success_count"]==1
    assert fb["hypothesis_update"]["hypothesis_id"]==res.hypothesis_id
    ctx=r.compiler.compile(r.state()["current_goal"],"similar hypothesis",r.state())
    assert any(x["kind"]=="hypothesis" and x["id"]==res.hypothesis_id for x in ctx["items"])
    r.close()


def test_self_model_validation_lifecycle(tmp_path):
    r=fresh(tmp_path)
    a=r.validate_weakness("provider_integration",passed=True,evidence_ref="sealed:pass")
    assert a["status"]=="mitigated"
    assert any(x["domain"]=="provider_integration" and x["status"]=="mitigated" for x in r.meta.active_self())
    b=r.validate_weakness("provider_integration",passed=False,evidence_ref="sealed:contradiction")
    assert b["status"]=="under_review"
    assert any(x["domain"]=="provider_integration" and x["status"]=="under_review" for x in r.meta.active_self())
    assert r.verify()["event_chain"]
    r.close()


def test_mpl_full_lifecycle(tmp_path):
    r=fresh(tmp_path)
    out=r.mpl.execute('''
meme calc {
def meme_main(input_data):
    return input_data
}
mutate calc {
result = 99
}
status calc
update calc {
def meme_main(input_data):
    return input_data * 2
}
evaluate calc [{"input":2,"expected":4},{"input":3,"expected":6}]
evolve calc {"candidates":["def meme_main(input_data):\\n    return 0","def meme_main(input_data):\\n    return input_data * 3"],"cases":[{"input":2,"expected":6},{"input":3,"expected":9},{"input":4,"expected":12}]}
execute calc 4
list
''')
    assert [x["op"] for x in out]==["meme","mutate","status","update","evaluate","evolve","execute","list"]
    assert out[4]["fitness"]==1.0
    assert out[5]["result"]["improved"] is True
    assert out[6]["result"]["result"]==12
    assert r.memes.current("calc")["version"]>=4
    r.close()


def test_migration_fixture_is_portable_and_no_absolute_mnt_dependency(tmp_path):
    from mos.migration import migrate_r2
    root=Path(__file__).resolve().parents[1]
    fixture=root/"fixtures"/"r2"
    assert fixture.exists()
    out=migrate_r2(fixture/"restored_memory_r2.json",fixture/"restored_stats_r2.json",tmp_path/"m.sqlite3")
    assert out["status"]=="verified" and out["event_chain"]
    source="\n".join(p.read_text(encoding="utf-8") for p in list((root/"mos").glob("*.py"))+[root/"tests"/"test_r4_legacy.py"])
    assert "/mnt/data/_mos_build" not in source


def test_runtime_version_and_capability_boundaries(tmp_path):
    r=fresh(tmp_path)
    v=r.verify();s=r.state()
    assert v["runtime_version"]=="4.2.0" and v["schema_version"]==4
    assert s["capabilities"]["full_state_relay"]["active"] is True
    assert s["capabilities"]["base_model_weight_change"]["active"] is False
    assert s["capabilities"]["hidden_cross_chat_memory"]["active"] is False
    r.close()

def test_rollback_is_append_only_and_future_updates_do_not_collide(tmp_path):
    r=fresh(tmp_path)
    r.memes.create('versioned','result=1')
    r.memes.update('versioned','result=2','v2')
    assert r.memes.rollback('versioned',1)
    assert r.memes.current('versioned')['version']==3
    assert r.memes.execute('versioned',None)['result']==1
    v4=r.memes.update('versioned','result=4','after rollback')
    assert v4==4 and r.memes.execute('versioned',None)['result']==4
    diff=r.memes.diff('versioned',2,3)
    assert '-result=2' in diff and '+result=1' in diff
    r.close()

def test_orchestrator_memory_sync_and_background(tmp_path):
    import time
    from mos import MOSOrchestrator
    o=MOSOrchestrator(tmp_path/'pool',n_nodes=2)
    try:
        o.nodes[0].memory.add('cross node verified memory',kind='long_term',importance=.95)
        out=o.sync_memories();assert out['nodes']==2 and out['copied']>=1
        assert o.nodes[1].store.conn.execute("SELECT 1 FROM memory WHERE content='cross node verified memory'").fetchone()
        assert o.start_reflection_all(.03)==2;time.sleep(.09)
        assert all(r.observer.running for r in o.nodes)
        o.stop_reflection_all();assert all(not r.observer.running for r in o.nodes)
        assert all(v['event_chain'] for v in o.verify_all())
    finally:o.close()


def test_http_v4_full_control_endpoints(tmp_path):
    import urllib.request
    from mos.service import MOSHTTPService
    s=MOSHTTPService(tmp_path/'svc.sqlite3',host='127.0.0.1',port=0,token='t');s.start_background();host,port=s.address;base=f'http://{host}:{port}'
    def post(path,obj):
        req=urllib.request.Request(base+path,data=json.dumps(obj).encode(),method='POST',headers={'Authorization':'Bearer t','Content-Type':'application/json'})
        return json.loads(urllib.request.urlopen(req,timeout=5).read())
    def get(path):
        req=urllib.request.Request(base+path,headers={'Authorization':'Bearer t'})
        return json.loads(urllib.request.urlopen(req,timeout=5).read())
    try:
        assert get('/health')['runtime_version']=='4.2.0'
        assert 'items' in get('/self-model') and 'items' in get('/meta-memory')
        post('/background',{'enabled':True,'interval':.03});assert s.runtime.observer.running
        post('/background',{'enabled':False});assert not s.runtime.observer.running
        snap=tmp_path/'svc_snapshot.json';post('/snapshot',{'path':str(snap)});assert snap.exists()
        backup=tmp_path/'svc_backup.sqlite3';post('/backup',{'path':str(backup)});assert backup.exists()
        out=post('/validate-weakness',{'domain':'provider_integration','passed':True,'evidence_ref':'http:test'});assert out['status']=='mitigated'
        assert get('/ontology')['nodes']==[]
    finally:s.shutdown()

def test_meme_execution_from_fresh_python_c_host(tmp_path):
    root=Path(__file__).resolve().parents[1]
    db=tmp_path/'inline.sqlite3'
    code=f'''from mos import MOSRuntime\nr=MOSRuntime(r"{db}")\nr.memes.create("inline","def meme_main(input_data):\\n    return input_data+1")\nprint(r.memes.execute("inline",4))\nr.close()'''
    env=os.environ.copy();env['PYTHONPATH']=str(root)
    p=subprocess.run([sys.executable,'-c',code],cwd=root,env=env,capture_output=True,text=True,timeout=15)
    assert p.returncode==0,p.stderr
    assert "'result': 5" in p.stdout or '"result": 5' in p.stdout

def test_lineage_aware_selection_keeps_one_actionplanning_family_member(tmp_path):
    from mos.migration import migrate_r2
    root=Path(__file__).resolve().parents[1]
    fixture=root/'fixtures'/'r2';db=tmp_path/'lin.sqlite3'
    migrate_r2(fixture/'restored_memory_r2.json',fixture/'restored_stats_r2.json',db)
    r=MOSRuntime(db)
    selected=r.strategies.select('план следующий strategy',r.state()['current_goal'],top_k=10)
    names=[s.name for _,s in selected]
    family=[n for n in names if n=='ActionPlanning' or n.startswith('ActionPlanning_v')]
    assert len(family)==1
    r.close()


def test_migration_preserves_base_stats_and_evolution_checkpoint(tmp_path):
    from mos.migration import migrate_r2
    root=Path(__file__).resolve().parents[1];fixture=root/'fixtures'/'r2';db=tmp_path/'stats.sqlite3'
    legacy=json.loads((fixture/'restored_stats_r2.json').read_text(encoding='utf-8'))
    migrate_r2(fixture/'restored_memory_r2.json',fixture/'restored_stats_r2.json',db)
    r=MOSRuntime(db)
    row=r.store.conn.execute("SELECT st.uses,st.success,st.fail,ev.generation,ev.last_evolved_uses FROM strategy_stats st JOIN strategies s USING(strategy_id) JOIN strategy_evolution ev USING(strategy_id) WHERE s.name='ActionPlanning'").fetchone()
    assert row['uses']==int(legacy['ActionPlanning']['uses']) and row['success']==int(legacy['ActionPlanning']['success']) and row['fail']==int(legacy['ActionPlanning']['fail'])
    assert row['generation']>=95 and row['last_evolved_uses']==row['uses']
    assert r.strategies.evolve(max_new=10)==[]
    r.close()


def test_evolution_requires_new_evidence_after_checkpoint(tmp_path):
    r=fresh(tmp_path)
    sid=next(s.strategy_id for s in r.strategies.all() if s.name=='ActionPlanning')
    with r.store.tx() as c:c.execute('UPDATE strategy_stats SET uses=3,success=3,fail=0 WHERE strategy_id=?',(sid,))
    first=r.strategies.evolve(max_new=2);assert first==['ActionPlanning_v2']
    assert r.strategies.evolve(max_new=2)==[]
    with r.store.tx() as c:c.execute('UPDATE strategy_stats SET uses=6,success=6,fail=0 WHERE strategy_id=?',(sid,))
    second=r.strategies.evolve(max_new=2);assert second==['ActionPlanning_v3']
    r.close()


def test_debt_aware_energy_recovery_is_bounded_and_persistent(tmp_path, monkeypatch):
    r=fresh(tmp_path,'energy.sqlite3')
    # Pin state exactly and use an explicit clock for deterministic recovery.
    r.store.set_state('energy',50.0);r.store.set_state('debt',0.0);r.store.set_state('energy_recovery_rate',0.7);r.store.set_state('last_tick',100.0)
    s=r.state_controller.recover(max_elapsed=60.0,now=110.0)
    assert s['energy']==pytest.approx(57.0)
    # Maximum debt suppression is 50%.
    r.store.set_state('energy',50.0);r.store.set_state('debt',10.0);r.store.set_state('last_tick',100.0)
    s=r.state_controller.recover(max_elapsed=60.0,now=110.0)
    assert s['energy']==pytest.approx(53.5)
    # Offline time is capped; it cannot silently recharge without bound.
    r.store.set_state('energy',10.0);r.store.set_state('debt',0.0);r.store.set_state('last_tick',0.0)
    s=r.state_controller.recover(max_elapsed=60.0,now=10_000.0)
    assert s['energy']==pytest.approx(52.0)
    r.close()
    r2=fresh(tmp_path,'energy.sqlite3')
    assert r2.state()['energy']==pytest.approx(52.0) and r2.state()['energy_recovery_rate']==pytest.approx(.7)
    r2.close()


def test_pre_cycle_recovers_then_charges_cost(tmp_path, monkeypatch):
    import mos.state as state_mod
    r=fresh(tmp_path,'pre_energy.sqlite3')
    r.store.set_state('energy',50.0);r.store.set_state('debt',0.0);r.store.set_state('pain',0.0);r.store.set_state('last_tick',100.0)
    monkeypatch.setattr(state_mod.time,'time',lambda:110.0)
    s=r.state_controller.pre_cycle('x',0)
    # recover 7.0, then cost = 1 + 1/900
    assert s['energy']==pytest.approx(57.0-(1.0+1/900),abs=1e-4)
    r.close()


def test_relay_rejects_database_schema_version_mismatch(tmp_path):
    r=fresh(tmp_path,'schema.sqlite3');snap=tmp_path/'schema.json'
    r.relay.export(snap);env=json.loads(snap.read_text(encoding='utf-8'))
    env['payload']['schema_version']=999
    from mos.util import sha256_json
    env['state_hash']=sha256_json(env['payload'])
    snap.write_text(json.dumps(env),encoding='utf-8')
    with pytest.raises(ValueError,match='database schema version mismatch'):
        r.relay.import_snapshot(snap)
    r.close()


def test_http_non_loopback_requires_token(tmp_path):
    from mos.service import MOSHTTPService
    with pytest.raises(ValueError,match='requires MOS_API_TOKEN'):
        MOSHTTPService(tmp_path/'remote.sqlite3',host='0.0.0.0',port=0,token=None)


def test_http_request_body_limit(tmp_path):
    import urllib.request, urllib.error
    from mos.service import MOSHTTPService
    s=MOSHTTPService(tmp_path/'small.sqlite3',host='127.0.0.1',port=0,token='t',max_body_bytes=1024);s.start_background();host,port=s.address
    try:
        req=urllib.request.Request(f'http://{host}:{port}/ask',data=json.dumps({'text':'x'*5000}).encode(),method='POST',headers={'Authorization':'Bearer t','Content-Type':'application/json'})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req,timeout=5)
        assert exc.value.code==413
        body=json.loads(exc.value.read());assert body['error']=='request_too_large'
    finally:s.shutdown()


def test_llm_subprocess_output_limit(tmp_path):
    from mos.llm import SubprocessLLMInterface
    host=tmp_path/'big_host.py';host.write_text('import sys,json\njson.load(sys.stdin)\nprint("X"*4096)\n',encoding='utf-8')
    llm=SubprocessLLMInterface([sys.executable,str(host)],max_output_bytes=1024)
    with pytest.raises(RuntimeError,match='output exceeds limit'):
        llm.generate({'a':1})


def test_strategy_learning_ignores_agent_and_internal_prose(tmp_path):
    r=fresh(tmp_path,'learn_source.sqlite3')
    for _ in range(5):
        r.memory.add('selfecho selfecho selfecho',kind='working',importance=.8,metadata={'source':'agent'})
        r.memory.add('internalword internalword',kind='working',importance=.8,metadata={'source':'internal'})
    assert all(token not in {'selfecho','internalword'} for token,_ in r.memory.repeated_tokens())
    assert r.strategies.learn()==[]
    for _ in range(3):r.memory.add('userquasar userquasar',kind='user',importance=.8,metadata={'source':'user'})
    assert any(token=='userquasar' for token,_ in r.memory.repeated_tokens())
    assert 'Auto_Userquasar' in r.strategies.learn()
    r.close()


def test_self_observer_counter_continues_across_restart(tmp_path):
    db=tmp_path/'observer.sqlite3';r=MOSRuntime(db)
    for _ in range(4):r.observer.observe('cycle')
    assert r.observer.counter==4
    before=r.store.conn.execute("SELECT count(*) n FROM meta_memory WHERE kind='self_observation'").fetchone()['n']
    r.close()
    r2=MOSRuntime(db);assert r2.observer.counter==4
    r2.observer.observe('cycle');assert r2.observer.counter==5
    after=r2.store.conn.execute("SELECT count(*) n FROM meta_memory WHERE kind='self_observation'").fetchone()['n']
    assert after==before+1
    r2.close()


def test_identical_active_hypothesis_is_reused_and_accumulates_evidence(tmp_path):
    r=fresh(tmp_path,'hyp_reuse.sqlite3')
    a=r.process('план continuity alpha');b=r.process('план continuity beta')
    assert a.hypothesis_id==b.hypothesis_id
    assert r.store.conn.execute('SELECT count(*) n FROM hypotheses').fetchone()['n']==1
    r.record_outcome(a.episode_id,'ok',success=True)
    r.record_outcome(b.episode_id,'no',success=False)
    h=r.hypotheses.get(a.hypothesis_id)
    assert h['test_count']==2 and h['success_count']==1 and h['probability']==pytest.approx(.5)
    assert r.store.conn.execute("SELECT count(*) n FROM events WHERE event_type='hypothesis_reused'").fetchone()['n']>=1
    r.close()


def test_relevant_meme_metadata_enters_context_but_code_does_not(tmp_path):
    seen=[];r=MOSRuntime(tmp_path/'meme_ctx.sqlite3',llm=CallableLLMInterface(lambda p:seen.append(p) or 'HOST'))
    secret_code='def meme_main(input_data):\n    return "SECRET_CODE_BODY"'
    r.memes.create('runtime_health',secret_code,'checks runtime health deterministically')
    r.process('use runtime_health meme for runtime health check')
    items=seen[0]['active_context']['items'];mi=[x for x in items if x['kind']=='meme' and x['metadata'].get('name')=='runtime_health']
    assert mi and mi[0]['source']=='meme_engine' and mi[0]['metadata']['execution']=='explicit_only'
    assert 'SECRET_CODE_BODY' not in json.dumps(seen[0],ensure_ascii=False)
    # Legacy raw state blobs are persistent but excluded from LLM-facing state candidates.
    r.store.set_state('legacy_r2_state',{'huge':'X'*10000})
    ctx=r.compiler.compile(r.state()['current_goal'],'ordinary request',r.state())
    assert not any(x['id']=='STATE:legacy_r2_state' for x in ctx['items'])
    r.close()
