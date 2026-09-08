from __future__ import annotations

import json

import pytest

from mos import CallableLLMInterface, MOSRuntime
from mos.adapters import SafeURLResearchAdapter


def _planning_candidate(runtime: MOSRuntime) -> str:
    sid=next(s.strategy_id for s in runtime.strategies.all() if s.name=="ActionPlanning")
    with runtime.store.tx() as db:
        db.execute("UPDATE strategy_stats SET uses=3,success=3,fail=0 WHERE strategy_id=?",(sid,))
    return runtime.strategies.evolve(max_new=1)[0]


def test_rules_only_mode_is_not_reported_as_a_provider(tmp_path):
    runtime=MOSRuntime(tmp_path/"rules.sqlite3")
    result=runtime.process("план следующего шага")
    assert result.response.startswith("[MOS rules-only; no language model]")
    assert runtime.verify()["llm_provider_active"] is False
    assert runtime.state()["capabilities"]["external_llm_bridge"]["active"] is False
    runtime.close()


def test_provider_receipt_is_in_hash_chained_cycle_receipt(tmp_path):
    adapter=CallableLLMInterface(lambda payload:"provider answer")
    adapter.last_receipt={"provider_response_id":"test-1","model":"controlled-test"}
    runtime=MOSRuntime(tmp_path/"provider.sqlite3",llm=adapter)
    result=runtime.process("verify provider")
    event=runtime.store.conn.execute("SELECT body FROM events WHERE event_id=?",(f"{result.cycle_id}:receipt",)).fetchone()
    body=json.loads(event["body"])
    assert body["provider_active"] is True
    assert body["provider_receipt"]["provider_response_id"]=="test-1"
    assert runtime.store.verify_event_chain()
    runtime.close()


def test_strategy_candidate_preserves_semantics_and_needs_gate(tmp_path):
    runtime=MOSRuntime(tmp_path/"gate.sqlite3")
    name=_planning_candidate(runtime)
    candidate=next(s for s in runtime.strategies.candidates() if s.name==name)
    assert candidate.action_kind=="planning"
    before=runtime.strategies.render(next(s for s in runtime.strategies.all() if s.name=="ActionPlanning"),"план",runtime.state())
    assert runtime.strategies.render(candidate,"план",runtime.state())==before
    assert all(s.name!=name for s in runtime.strategies.all())
    receipt=runtime.strategies.activate_candidate(name,{"evaluator":"pytest","holdout_id":"blind-v1","cases":5,"blind":True,
                                                        "baseline_score":0.6,"candidate_score":0.8,"regressions":0})
    assert receipt["passed"] is True
    assert any(s.name==name for s in runtime.strategies.all())
    selected=runtime.strategies.select("улучшить план",runtime.state()["current_goal"])
    picked=next(s for _,s in selected if s.name==name)
    assert runtime.strategies.render(picked,"улучшить план",runtime.state())==before
    runtime.close()


def test_strategy_candidate_failing_gate_is_rejected(tmp_path):
    runtime=MOSRuntime(tmp_path/"reject.sqlite3")
    name=_planning_candidate(runtime)
    receipt=runtime.strategies.activate_candidate(name,{"evaluator":"pytest","holdout_id":"not-blind","cases":5,"blind":False,
                                                        "baseline_score":0.6,"candidate_score":0.9,"regressions":0})
    assert receipt["passed"] is False
    assert all(s.name!=name for s in runtime.strategies.all())
    assert runtime.verify()["rejected_strategies"]==1
    runtime.close()


def test_legacy_semantic_regression_is_quarantined_on_upgrade(tmp_path):
    path=tmp_path/"legacy.sqlite3";runtime=MOSRuntime(path)
    parent=next(s for s in runtime.strategies.all() if s.name=="ActionPlanning")
    with runtime.store.tx() as db:
        db.execute("INSERT INTO strategies(strategy_id,name,level,triggers,action_kind,action_arg,priority,parent_id,status,version,learned_from) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   ("STR-LEGACY-BAD","ActionPlanning_v99",3,'["улучш"]',"evolved",None,2.0,parent.strategy_id,"active",99,'[]'))
        db.execute("INSERT INTO strategy_stats(strategy_id,uses,success,fail,last_used) VALUES(?,?,?,?,NULL)",("STR-LEGACY-BAD",0,0,0))
        db.execute("INSERT INTO strategy_evolution(strategy_id,last_evolved_uses,last_evolved_success,last_evolved_fail,generation,updated_at) VALUES(?,?,?,?,?,?)",
                   ("STR-LEGACY-BAD",0,0,0,99,"2026-01-01T00:00:00Z"))
    runtime.close();runtime=MOSRuntime(path)
    row=runtime.store.conn.execute("SELECT status FROM strategies WHERE strategy_id='STR-LEGACY-BAD'").fetchone()
    assert row["status"]=="quarantined"
    assert runtime.verify()["quarantined_strategies"]==1
    assert runtime.store.verify_event_chain()
    runtime.close()


def test_network_flag_requires_actual_evidence(tmp_path):
    class Adapter:
        def fetch(self,query):return [{"claim":"local fixture","network_used":False}]
    runtime=MOSRuntime(tmp_path/"research.sqlite3",research_adapter=Adapter())
    result=runtime.research.run("fixture")
    assert result["adapter_used"] is True and result["network_used"] is False
    runtime.close()


def test_safe_url_adapter_has_explicit_boundary_and_blocks_private_network():
    adapter=SafeURLResearchAdapter()
    result=adapter.fetch("search for something without a URL")
    assert result[0]["status"]=="no_explicit_https_url" and result[0]["network_used"] is False
    with pytest.raises(ValueError):
        adapter.fetch("https://127.0.0.1/private")


def test_observer_capability_is_dynamic(tmp_path):
    runtime=MOSRuntime(tmp_path/"observer.sqlite3")
    assert runtime.verify()["capabilities"]["background_observer"]["active"] is False
    runtime.start_background_reflection(.05)
    assert runtime.verify()["capabilities"]["background_observer"]["active"] is True
    runtime.stop_background_reflection()
    assert runtime.verify()["capabilities"]["background_observer"]["active"] is False
    runtime.close()
