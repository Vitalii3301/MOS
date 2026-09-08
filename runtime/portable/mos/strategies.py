from __future__ import annotations
import json
from typing import Any
from .storage import SQLiteStore
from .util import sha256_json, stable_id, utcnow
from .models import StrategyDef
from .memory import MemorySystem

BASE = [
 ("ContentAnalysis",1,[], "content_analysis",None,1.0),
 ("ContradictionCheck",2,["contradiction","conflict","противореч","конфликт"],"contradiction",None,1.15),
 ("ActionPlanning",2,["plan","next","strategy","план","следующ","стратег"],"planning",None,1.2),
 ("UserResonance",2,["why","how","почему","как"],"resonance",None,1.0),
 ("HypothesisGeneration",3,["hypothesis","test","гипотез","провер"],"hypothesis",None,1.1),
 ("EvidenceCheck",3,["evidence","verify","proof","доказ","провер"],"evidence",None,1.25),
 ("SelfReflection",3,["self","ошиб","weakness","само","рефлекс"],"reflection",None,1.1),
 ("ResearchRouting",3,["research","source","источник","исслед"],"research",None,1.1),
 ("MemeEngineering",3,["meme","memetic","мем","алгоритм"],"meme_engineering",None,1.0),
 ("DecisionAudit",4,["decision","audit","решение","аудит"],"decision_audit",None,1.1),
 ("StrategicThinking",3,["strategy","long-term","стратег","долгоср"],"strategic",None,1.15),
 ("TacticalThinking",3,["tactic","step","тактик","шаг"],"tactical",None,1.05),
 ("PhysicsAnalysis",3,["physics","force","energy","физик","энерг"],"physics",None,1.0),
 ("EconomicAnalysis",3,["cost","price","economic","стоим","эконом"],"economic",None,1.0),
 ("ConstraintNavigation",4,["rule","law","constraint","правил","закон","огранич"],"constraint_navigation",None,1.2),
]

class StrategyGenome:
    def __init__(self,store:SQLiteStore,memory:MemorySystem):
        self.store=store; self.memory=memory; self._ensure_base(); self._quarantine_legacy_evolved()

    def _quarantine_legacy_evolved(self):
        """Disable R4 descendants that replaced their parent's meaning.

        R4 stored these rows as action_kind='evolved', so a planning strategy
        rendered a generic sentence instead of the planning instruction. They
        must never remain active after an upgrade.
        """
        rows=self.store.conn.execute("SELECT strategy_id,name FROM strategies WHERE status='active' AND action_kind='evolved'").fetchall()
        if not rows:return
        with self.store.tx() as c:
            c.execute("UPDATE strategies SET status='quarantined' WHERE status='active' AND action_kind='evolved'")
        self.store.append_event(
            f"STRATEGY-MIGRATION-{stable_id('MIG',{'ids':[r['strategy_id'] for r in rows]})}",
            "strategy_quarantine",
            {"reason":"r4_semantic_regression","strategies":[{"id":r["strategy_id"],"name":r["name"]} for r in rows]},
        )
    def _ensure_base(self):
        for name,level,triggers,kind,arg,priority in BASE:
            sid=stable_id("STR",{"name":name})
            with self.store.tx() as c:
                c.execute("""INSERT OR IGNORE INTO strategies(strategy_id,name,level,triggers,action_kind,action_arg,priority,parent_id,status,version,learned_from)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                          (sid,name,level,json.dumps(triggers),kind,arg,priority,None,"active",1,"[]"))
                c.execute("INSERT OR IGNORE INTO strategy_stats(strategy_id,uses,success,fail,last_used) VALUES(?,?,?,?,NULL)",(sid,0,0,0))
                c.execute("INSERT OR IGNORE INTO strategy_evolution(strategy_id,last_evolved_uses,last_evolved_success,last_evolved_fail,generation,updated_at) VALUES(?,?,?,?,?,?)",
                          (sid,0,0,0,1,utcnow()))
    def all(self,status:str="active")->list[StrategyDef]:
        out=[]
        for r in self.store.conn.execute("SELECT * FROM strategies WHERE status=? ORDER BY level,name",(status,)):
            out.append(StrategyDef(r["strategy_id"],r["name"],r["level"],json.loads(r["triggers"]),r["action_kind"],
                                   r["action_arg"],r["priority"],r["parent_id"],r["status"],r["version"],json.loads(r["learned_from"])))
        return out

    def candidates(self)->list[StrategyDef]:
        return self.all("candidate")
    def _root_id(self,s:StrategyDef)->str:
        current=s;seen=set()
        while current.parent_id and current.parent_id not in seen:
            seen.add(current.strategy_id)
            row=self.store.conn.execute("SELECT * FROM strategies WHERE strategy_id=?",(current.parent_id,)).fetchone()
            if not row:break
            current=StrategyDef(row["strategy_id"],row["name"],row["level"],json.loads(row["triggers"]),row["action_kind"],
                                row["action_arg"],row["priority"],row["parent_id"],row["status"],row["version"],json.loads(row["learned_from"]))
        return current.strategy_id

    def select(self,text:str,goal:str,top_k:int=5)->list[tuple[float,StrategyDef]]:
        low=text.lower(); candidates=[]
        for s in self.all():
            hits=sum(1 for t in s.triggers if t in low)
            base=s.priority + hits*0.28 + (0.08 if not s.triggers else 0)
            if s.action_kind=="planning" and goal.lower() in low: base+=0.25
            if s.action_kind=="evidence" and any(x in low for x in ("доказ","proof","verify","провер")): base+=0.35
            if hits or not s.triggers: candidates.append((base,s))
        # One representative per lineage avoids flooding attention with near-duplicate
        # evolved descendants of the same strategy family.
        best_by_root={}
        for score,strategy in candidates:
            root=self._root_id(strategy)
            prev=best_by_root.get(root)
            if prev is None or (score,strategy.version,strategy.priority)>(prev[0],prev[1].version,prev[1].priority):
                best_by_root[root]=(score,strategy)
        scores=list(best_by_root.values())
        scores.sort(key=lambda x:(x[0],x[1].version,-x[1].level,x[1].name),reverse=True)
        return scores[:top_k]
    def render(self,s:StrategyDef,text:str,state:dict[str,Any])->str:
        goal=state.get("current_goal","")
        k=s.action_kind
        if k=="content_analysis": return f"Analyze the request against goal '{goal}' and current evidence."
        if k=="contradiction": return "Identify contradictions and prefer a discriminating test over narrative reconciliation."
        if k=="planning": return f"Build the next executable step for goal '{goal}'."
        if k=="resonance": return "Preserve the user's actual intent while separating facts, inference and unknowns."
        if k=="hypothesis": return f"Form a falsifiable hypothesis relevant to: {text[:180]}"
        if k=="evidence": return "Require reproducible evidence, receipts or an explicit uncertainty boundary."
        if k=="reflection": return "Compare the current action with known strengths, weaknesses and previous failures."
        if k=="research": return "Route externally verifiable claims through a research/evidence adapter before accepting them."
        if k=="meme_engineering": return "Treat reusable code/knowledge units as versioned memes with fitness and lineage."
        if k=="decision_audit": return "Record hypothesis → evidence → decision → action → outcome → revision."
        if k=="strategic": return "Evaluate long-horizon consequences, dependencies and rollback options."
        if k=="tactical": return "Select the smallest executable next step with a clear success condition."
        if k=="physics": return "Use quantitative physical constraints only when the task actually depends on them."
        if k=="economic": return "Evaluate cost, value, constraints and opportunity cost without inventing market data."
        if k=="constraint_navigation": return "Navigate constraints explicitly; do not bypass safety, law, authorization or evidence requirements."
        if k=="legacy_base": return f"Apply preserved legacy strategy '{s.name}' conservatively under current safety/evidence rules."
        if k=="learned_token": return f"Use learned recurring concept '{s.action_arg}' as a context feature, not as truth."
        if k=="evolved":
            # Compatibility only: legacy rows are quarantined on startup. If a
            # caller renders one explicitly, preserve the parent's semantics.
            row=self.store.conn.execute("SELECT * FROM strategies WHERE strategy_id=?",(s.parent_id,)).fetchone()
            if row:
                parent=StrategyDef(row["strategy_id"],row["name"],row["level"],json.loads(row["triggers"]),row["action_kind"],
                                   row["action_arg"],row["priority"],row["parent_id"],row["status"],row["version"],json.loads(row["learned_from"]))
                return self.render(parent,text,state)
            return "Reject an unresolvable evolved strategy and use the last verified version."
        return f"Apply strategy {s.name}."
    def record(self,ids:list[str],success:bool|None=None):
        with self.store.tx() as c:
            for sid in ids:
                c.execute("UPDATE strategy_stats SET uses=uses+1,last_used=? WHERE strategy_id=?",(utcnow(),sid))
                if success is True: c.execute("UPDATE strategy_stats SET success=success+1 WHERE strategy_id=?",(sid,))
                elif success is False: c.execute("UPDATE strategy_stats SET fail=fail+1 WHERE strategy_id=?",(sid,))
    def feedback(self,ids:list[str],success:bool):
        with self.store.tx() as c:
            for sid in ids:
                if success:
                    c.execute("UPDATE strategy_stats SET success=success+1 WHERE strategy_id=?",(sid,))
                else:
                    c.execute("UPDATE strategy_stats SET fail=fail+1 WHERE strategy_id=?",(sid,))
    def learn(self,max_new:int=3,max_dynamic:int=24)->list[str]:
        dynamic=self.store.conn.execute("SELECT count(*) n FROM strategies WHERE action_kind='learned_token'").fetchone()["n"]
        if dynamic>=max_dynamic:return []
        covered={t for s in self.all() for t in s.triggers}; created=[]
        for token,count in self.memory.repeated_tokens():
            if count<3 or token in covered: continue
            name=f"Auto_{token.capitalize()}"; sid=stable_id("STR",{"name":name})
            with self.store.tx() as c:
                c.execute("""INSERT OR IGNORE INTO strategies(strategy_id,name,level,triggers,action_kind,action_arg,priority,parent_id,status,version,learned_from)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                          (sid,name,2,json.dumps([token]),"learned_token",token,0.9,None,"active",1,json.dumps(["memory_frequency"])))
                c.execute("INSERT OR IGNORE INTO strategy_stats(strategy_id,uses,success,fail,last_used) VALUES(?,?,?,?,NULL)",(sid,0,0,0))
                c.execute("INSERT OR IGNORE INTO strategy_evolution(strategy_id,last_evolved_uses,last_evolved_success,last_evolved_fail,generation,updated_at) VALUES(?,?,?,?,?,?)",
                          (sid,0,0,0,1,utcnow()))
            created.append(name); covered.add(token)
            if len(created)>=max_new or dynamic+len(created)>=max_dynamic: break
        return created
    def evolve(self,max_new:int=2)->list[str]:
        """Propose descendants but never activate them without a blind quality gate."""
        rows=self.store.conn.execute("""SELECT s.*,st.uses,st.success,st.fail,
                                               coalesce(ev.last_evolved_uses,0) last_evolved_uses,
                                               coalesce(ev.last_evolved_success,0) last_evolved_success,
                                               coalesce(ev.last_evolved_fail,0) last_evolved_fail,
                                               coalesce(ev.generation,1) generation
                                        FROM strategies s
                                        JOIN strategy_stats st USING(strategy_id)
                                        LEFT JOIN strategy_evolution ev USING(strategy_id)
                                        WHERE s.status='active' AND s.action_kind!='evolved'
                                        ORDER BY (st.success-st.fail) DESC,st.uses DESC""").fetchall()
        created=[]
        for r in rows:
            du=int(r["uses"])-int(r["last_evolved_uses"]); ds=int(r["success"])-int(r["last_evolved_success"]); df=int(r["fail"])-int(r["last_evolved_fail"])
            if du<3 or ds<=df:continue
            generation=int(r["generation"])+1
            base=r["name"];name=f"{base}_v{generation}"
            sid=stable_id("STR",{"name":name});triggers=json.loads(r["triggers"])+["improve","улучш"]
            if self.store.conn.execute("SELECT 1 FROM strategies WHERE name=?",(name,)).fetchone():
                # Advance the checkpoint even if an imported historical child already
                # occupies this generation, preventing repeated attempts every tick.
                with self.store.tx() as c:
                    c.execute("INSERT OR REPLACE INTO strategy_evolution(strategy_id,last_evolved_uses,last_evolved_success,last_evolved_fail,generation,updated_at) VALUES(?,?,?,?,?,?)",
                              (r["strategy_id"],r["uses"],r["success"],r["fail"],generation,utcnow()))
                continue
            with self.store.tx() as c:
                c.execute("""INSERT INTO strategies(strategy_id,name,level,triggers,action_kind,action_arg,priority,parent_id,status,version,learned_from)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                          (sid,name,min(int(r["level"])+1,5),json.dumps(sorted(set(triggers))),r["action_kind"],r["action_arg"],
                           min(2.0,float(r["priority"])*1.03),r["strategy_id"],"candidate",generation,json.dumps([r["strategy_id"],"feedback_proposal"])))
                c.execute("INSERT INTO strategy_stats(strategy_id,uses,success,fail,last_used) VALUES(?,?,?,?,NULL)",(sid,0,0,0))
                c.execute("INSERT INTO strategy_evolution(strategy_id,last_evolved_uses,last_evolved_success,last_evolved_fail,generation,updated_at) VALUES(?,?,?,?,?,?)",
                          (sid,0,0,0,generation,utcnow()))
                c.execute("INSERT OR REPLACE INTO strategy_evolution(strategy_id,last_evolved_uses,last_evolved_success,last_evolved_fail,generation,updated_at) VALUES(?,?,?,?,?,?)",
                          (r["strategy_id"],r["uses"],r["success"],r["fail"],generation,utcnow()))
            proposal={"candidate_id":sid,"candidate_name":name,"parent_id":r["strategy_id"],"generation":generation,
                      "status":"candidate","semantic_action_kind":r["action_kind"],"triggers":sorted(set(triggers))}
            proposal["proposal_sha256"]=sha256_json(proposal)
            self.store.append_event(f"{sid}:proposal","strategy_candidate_proposed",proposal)
            created.append(name)
            if len(created)>=max_new:break
        return created

    def activate_candidate(self,name:str,evaluation:dict[str,Any])->dict[str,Any]:
        """Activate a proposal only after a blind, regression-free held-out test."""
        row=self.store.conn.execute("SELECT * FROM strategies WHERE name=?",(name,)).fetchone()
        if not row:raise KeyError(name)
        if row["status"]!="candidate":raise ValueError("strategy is not a candidate")
        evidence=dict(evaluation)
        required=("evaluator","holdout_id","cases","blind","baseline_score","candidate_score","regressions")
        missing=[k for k in required if k not in evidence]
        if missing:raise ValueError("missing evaluation fields: "+", ".join(missing))
        cases=int(evidence["cases"]);baseline=float(evidence["baseline_score"]);candidate=float(evidence["candidate_score"])
        passed=(bool(evidence["blind"]) and cases>=3 and int(evidence["regressions"])==0 and candidate>baseline)
        receipt={"candidate_id":row["strategy_id"],"candidate_name":name,"parent_id":row["parent_id"],
                 "evaluation":evidence,"passed":passed,"evaluated_at":utcnow()}
        receipt["receipt_sha256"]=sha256_json(receipt)
        with self.store.tx() as c:
            c.execute("UPDATE strategies SET status=? WHERE strategy_id=?",("active" if passed else "rejected",row["strategy_id"]))
        self.store.append_event(f"{row['strategy_id']}:evaluation:{receipt['receipt_sha256'][:12]}","strategy_candidate_evaluated",receipt)
        return receipt

    def reject_candidate(self,name:str,reason:str)->dict[str,Any]:
        row=self.store.conn.execute("SELECT strategy_id,status FROM strategies WHERE name=?",(name,)).fetchone()
        if not row:raise KeyError(name)
        if row["status"]!="candidate":raise ValueError("strategy is not a candidate")
        with self.store.tx() as c:c.execute("UPDATE strategies SET status='rejected' WHERE strategy_id=?",(row["strategy_id"],))
        body={"candidate_id":row["strategy_id"],"candidate_name":name,"reason":reason,"rejected_at":utcnow()}
        self.store.append_event(f"{row['strategy_id']}:rejected","strategy_candidate_rejected",body)
        return body
