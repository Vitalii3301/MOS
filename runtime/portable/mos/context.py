from __future__ import annotations
import math, re
from typing import Any
from .models import ContextItem
from .util import sha256_json
from .memory import MemorySystem
from .strategies import StrategyGenome
from .epistemic import EpistemicLedger
from .metacog import MetaCognition
from .hypotheses import HypothesisTracker
from .memes import MemeEngine

def tokens(s:str)->set[str]:
    return {x for x in re.findall(r"[\w-]{3,}",s.lower())}

def rel(a:str,b:str)->float:
    x=tokens(a); y=tokens(b)
    if not x or not y:return 0.0
    return len(x&y)/math.sqrt(len(x)*len(y))

class ContextCompiler:
    def __init__(self,memory:MemorySystem,strategies:StrategyGenome,episodes:EpistemicLedger,meta:MetaCognition,
                 hypotheses:HypothesisTracker|None=None,memes:MemeEngine|None=None,max_items:int=32,max_chars:int=12000):
        self.memory=memory;self.strategies=strategies;self.episodes=episodes;self.meta=meta;self.hypotheses=hypotheses;self.memes=memes
        self.max_items=max_items;self.max_chars=max_chars
    def compile(self,goal:str,user_input:str,state:dict[str,Any])->dict[str,Any]:
        items=[]
        state_keys=("current_goal","emotion","energy","energy_recovery_rate","confidence","debt","pain","context","cycle_count","capabilities","last_error")
        for k in state_keys:
            if k not in state:continue
            v=state[k];text=f"{k}: {v}"
            items.append(ContextItem(f"STATE:{k}","state",text,"state",rel(goal+" "+user_input,text),.75))
        for m in self.memory.retrieve(goal+" "+user_input,top_k=8):
            items.append(ContextItem(m.memory_id,"memory",m.content,"memory",
                      rel(goal+" "+user_input,m.content),m.importance,1-m.strength,0,.7,metadata=m.metadata))
        for score,s in self.strategies.select(user_input,goal,top_k=6):
            text=f"{s.name}: {self.strategies.render(s,user_input,state)}"
            items.append(ContextItem(s.strategy_id,"strategy",text,"strategy",min(1,score/2),.82))
        for e in self.episodes.recent(8):
            text=f"{e.episode_id}: H={e.hypothesis}; decision={e.decision}; outcome={e.outcome}; revision={e.revision}"
            cp=.7 if e.revision else .15
            items.append(ContextItem(e.episode_id,"episode",text,"episode",rel(goal+" "+user_input,text),.84,.2,cp))
        if self.hypotheses is not None:
            for h in self.hypotheses.active(8):
                text=(f"{h['hypothesis_id']}: {h['description']} | p={h['probability']:.3f} | "
                      f"tests={h['test_count']} | status={h['status']}")
                uncertainty=max(0.0,1.0-abs(float(h['probability'])-.5)*2)
                items.append(ContextItem(h['hypothesis_id'],"hypothesis",text,"hypothesis",
                          rel(goal+" "+user_input,text),.86,uncertainty,.25 if h['status']=='weakened' else .05))
        if self.memes is not None:
            for m in self.memes.relevant_catalog(goal+" "+user_input,limit=5):
                fit="unknown" if m["fitness"] is None else f"{float(m['fitness']):.3f}"
                text=f"Meme {m['name']} v{m['version']} | fitness={fit} | {m['description']}"
                items.append(ContextItem(m["meme_id"],"meme",text,"meme_engine",
                          rel(goal+" "+user_input,text),.8,.25 if m["fitness"] is None else max(0.0,1.0-float(m["fitness"])),.05,
                          metadata={"name":m["name"],"version":m["version"],"fitness":m["fitness"],"parents":m["parents"],"execution":"explicit_only"}))
        for m in self.meta.active_meta(8):
            items.append(ContextItem(m["memory_id"],"meta_memory",m["statement"],"meta_memory",
                      rel(goal+" "+user_input,m["statement"]),m["importance"],1-m["confidence"],
                      .8 if m["review_status"]=="review_due" else .1))
        for s in self.meta.active_self():
            imp=.98 if s["kind"] in {"weakness","mandatory_check"} and s["status"]!="mitigated" else .72
            items.append(ContextItem(s["item_id"],f"self_{s['kind']}",s["statement"],"self_model",
                      rel(goal+" "+user_input,s["statement"]),imp,1-s["confidence"],
                      .6 if s["status"]=="under_review" else .1,metadata={"status":s["status"],"scope_limit":s["scope_limit"]}))
        for x in items:
            x.attention_score=max(0,min(1,0.34*x.relevance+0.28*x.importance+0.14*(1-x.uncertainty)+
                                        0.14*x.contradiction_pressure+0.10*x.recency))
            if x.kind in {"self_weakness","self_mandatory_check"}:x.attention_score=min(1,x.attention_score+.12)
        items.sort(key=lambda x:(x.attention_score,x.importance),reverse=True)
        selected=[]; chars=0
        for x in items:
            cost=len(x.text)+40
            if len(selected)>=self.max_items or chars+cost>self.max_chars:continue
            selected.append(x);chars+=cost
        prompt={
            "schema":"mos-active-context/v3","goal":goal,
            "state_focus":{k:state.get(k) for k in ("current_goal","emotion","energy","confidence","debt","pain","context")},
            "items":[{"id":x.item_id,"kind":x.kind,"text":x.text,"source":x.source,
                      "score":round(x.attention_score,6),"metadata":x.metadata} for x in selected],
            "budget":{"candidates":len(items),"selected":len(selected),"omitted":len(items)-len(selected),"chars":chars},
        }
        prompt["context_sha256"]=sha256_json(prompt)
        return prompt
