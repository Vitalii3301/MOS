from __future__ import annotations
import hashlib, json, math
from collections import Counter
from typing import Any
from .storage import SQLiteStore
from .util import stable_id, utcnow
from .models import MemoryRecord

def vectorize(text:str, dim:int=64)->list[float]:
    v=[0.0]*dim
    for token in text.lower().split():
        for j in range(dim):
            d=hashlib.sha256(f"{token}:{j}".encode()).digest()
            v[j]+=int.from_bytes(d[:8],"big")/(2**64-1)
    n=math.sqrt(sum(x*x for x in v))
    return [x/n for x in v] if n else v

def cosine(a:list[float],b:list[float])->float:
    if not a or not b: return 0.0
    da=math.sqrt(sum(x*x for x in a)); db=math.sqrt(sum(x*x for x in b))
    if not da or not db: return 0.0
    return sum(x*y for x,y in zip(a,b))/(da*db)

class MemorySystem:
    def __init__(self, store:SQLiteStore, vector_dim:int=64):
        self.store=store; self.vector_dim=vector_dim
    def add(self,content:str,*,kind:str="working",importance:float=0.5,metadata:dict[str,Any]|None=None)->str:
        created=utcnow(); metadata=metadata or {}
        mid=stable_id("MEM",{"content":content,"created_at":created})
        vec=vectorize(content,self.vector_dim)
        with self.store.tx() as c:
            c.execute("""INSERT INTO memory(memory_id,content,kind,importance,strength,usage_count,created_at,metadata,vector)
                         VALUES(?,?,?,?,?,?,?,?,?)""",
                      (mid,content,kind,float(importance),1.0,0,created,json.dumps(metadata,ensure_ascii=False),
                       json.dumps(vec)))
        return mid
    def retrieve(self,query:str,top_k:int=6,kinds:tuple[str,...]|None=None)->list[MemoryRecord]:
        q=vectorize(query,self.vector_dim); rows=list(self.store.conn.execute("SELECT * FROM memory"))
        scored=[]
        for r in rows:
            if kinds and r["kind"] not in kinds: continue
            rec=MemoryRecord(r["memory_id"],r["content"],r["kind"],r["importance"],r["strength"],
                             r["usage_count"],r["created_at"],json.loads(r["metadata"]),json.loads(r["vector"]))
            score=0.58*cosine(q,rec.vector)+0.25*rec.importance+0.17*rec.strength
            scored.append((score,rec))
        scored.sort(key=lambda x:(x[0],x[1].created_at),reverse=True)
        chosen=[r for _,r in scored[:top_k]]
        if chosen:
            with self.store.tx() as c:
                for r in chosen:
                    c.execute("UPDATE memory SET usage_count=usage_count+1,strength=min(1.0,strength+0.03) WHERE memory_id=?",(r.memory_id,))
        return chosen
    def decay(self):
        with self.store.tx() as c:
            c.execute("UPDATE memory SET strength=max(0.05,strength-(0.02/(1.0+usage_count)))")
    def repeated_tokens(self,limit:int=20)->list[tuple[str,int]]:
        rows=self.store.conn.execute("SELECT content,kind,metadata FROM memory WHERE kind IN ('working','user') ORDER BY created_at DESC LIMIT 80")
        counts=Counter()
        stop={"the","and","for","with","это","как","что","для","или","then","from","into","если","так","его","она"}
        accepted=0
        for r in rows:
            metadata=json.loads(r["metadata"]) if r["metadata"] else {}
            source=str(metadata.get("source","")).lower()
            # Native R4 user records use kind=user. Migrated R2 working memory is
            # accepted only when its source explicitly says user. Agent/internal
            # prose must never teach new strategies merely by repeating itself.
            if r["kind"]!="user" and source!="user":
                continue
            accepted+=1
            toks={x.strip(".,!?;:()[]{}\"'").lower() for x in r["content"].split()}
            for t in toks:
                if len(t)>=5 and t not in stop: counts[t]+=1
            if accepted>=40:break
        return counts.most_common(limit)
