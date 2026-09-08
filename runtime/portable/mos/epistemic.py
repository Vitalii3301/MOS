from __future__ import annotations
import json
from typing import Any
from .storage import SQLiteStore
from .util import stable_id, utcnow
from .models import Episode

class EpistemicLedger:
    def __init__(self,store:SQLiteStore): self.store=store
    def open(self,goal:str,hypothesis:str)->str:
        now=utcnow(); eid=stable_id("EP",{"goal":goal,"hypothesis":hypothesis,"t":now})
        with self.store.tx() as c:
            c.execute("""INSERT INTO episodes(episode_id,goal,hypothesis,evidence,decision,action,outcome,revision,status,created_at,updated_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(eid,goal,hypothesis,"[]",None,None,None,None,"open",now,now))
        self.store.append_event(f"{eid}:open","episode_open",{"episode_id":eid,"goal":goal,"hypothesis":hypothesis})
        return eid
    def add_evidence(self,eid:str,evidence:dict[str,Any]):
        r=self.store.conn.execute("SELECT evidence FROM episodes WHERE episode_id=?",(eid,)).fetchone()
        if not r:raise KeyError(eid)
        arr=json.loads(r["evidence"]); arr.append(evidence)
        with self.store.tx() as c:c.execute("UPDATE episodes SET evidence=?,updated_at=? WHERE episode_id=?",(json.dumps(arr,ensure_ascii=False),utcnow(),eid))
        self.store.append_event(f"{eid}:evidence:{len(arr)}","evidence",{"episode_id":eid,"evidence":evidence})
    def decide(self,eid:str,decision:str,action:str):
        with self.store.tx() as c:c.execute("UPDATE episodes SET decision=?,action=?,status='decided',updated_at=? WHERE episode_id=?",(decision,action,utcnow(),eid))
        self.store.append_event(f"{eid}:decision","decision",{"episode_id":eid,"decision":decision,"action":action})
    def outcome(self,eid:str,outcome:str,revision:str|None=None):
        status="revised" if revision else "closed"
        with self.store.tx() as c:c.execute("UPDATE episodes SET outcome=?,revision=?,status=?,updated_at=? WHERE episode_id=?",(outcome,revision,status,utcnow(),eid))
        self.store.append_event(f"{eid}:outcome","outcome",{"episode_id":eid,"outcome":outcome,"revision":revision})
    def get(self,eid:str)->Episode:
        r=self.store.conn.execute("SELECT * FROM episodes WHERE episode_id=?",(eid,)).fetchone()
        if not r:raise KeyError(eid)
        return Episode(r["episode_id"],r["goal"],r["hypothesis"],json.loads(r["evidence"]),r["decision"],r["action"],r["outcome"],r["revision"],r["status"],r["created_at"],r["updated_at"])
    def recent(self,limit:int=8)->list[Episode]:
        ids=[r["episode_id"] for r in self.store.conn.execute("SELECT episode_id FROM episodes ORDER BY updated_at DESC LIMIT ?",(limit,))]
        return [self.get(x) for x in ids]
