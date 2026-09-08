from __future__ import annotations
import ast, difflib, json, subprocess, sys
from typing import Any, Callable
from .storage import SQLiteStore
from .security import CodePolicy
from .util import sha256_json, stable_id, utcnow

class MemeEngine:
    def __init__(self,store:SQLiteStore):
        self.store=store; self.policy=CodePolicy()
    def create(self,name:str,code:str,description:str="",parents:list[str]|None=None)->str:
        sec=self.policy.scan(code)
        if not sec.allowed: raise ValueError("Unsafe meme code: "+", ".join(sec.reasons))
        mid=stable_id("MEME",{"name":name})
        now=utcnow(); parents=parents or []
        with self.store.tx() as c:
            c.execute("INSERT INTO memes(meme_id,name,description,current_version,fitness,status,created_at) VALUES(?,?,?,?,?,?,?)",
                      (mid,name,description,1,None,"active",now))
            c.execute("INSERT INTO meme_versions(meme_id,version,code,parents,fitness,created_at,change_note) VALUES(?,?,?,?,?,?,?)",
                      (mid,1,code,json.dumps(parents),None,now,"initial"))
        return mid
    def _meme(self,name:str):
        r=self.store.conn.execute("SELECT * FROM memes WHERE name=?",(name,)).fetchone()
        if not r: raise KeyError(name)
        return r
    def current(self,name:str)->dict[str,Any]:
        m=self._meme(name)
        v=self.store.conn.execute("SELECT * FROM meme_versions WHERE meme_id=? AND version=?",(m["meme_id"],m["current_version"])).fetchone()
        return {"meme_id":m["meme_id"],"name":m["name"],"description":m["description"],"version":m["current_version"],
                "fitness":m["fitness"],"code":v["code"],"parents":json.loads(v["parents"]),"status":m["status"]}
    def execute(self,name:str,input_data:Any,timeout:float=2.0)->dict[str,Any]:
        cur=self.current(name)
        try:
            safe_input=json.loads(json.dumps(input_data,ensure_ascii=False,allow_nan=False))
        except (TypeError,ValueError) as exc:
            return {"ok":False,"error":f"input_not_json_serializable: {exc}"}
        request=json.dumps({"code":cur["code"],"input_data":safe_input},ensure_ascii=False,allow_nan=False)
        if len(request.encode("utf-8"))>1_000_000:
            return {"ok":False,"error":"input_too_large"}
        try:
            p=subprocess.run([sys.executable,"-m","mos.meme_worker"],input=request,text=True,capture_output=True,timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"ok":False,"error":"timeout"}
        if p.returncode!=0:
            return {"ok":False,"error":f"sandbox_process_failed:{p.returncode}","stderr":p.stderr[-1000:]}
        try:
            out=json.loads(p.stdout.strip())
        except Exception:
            return {"ok":False,"error":"invalid_sandbox_response","stderr":p.stderr[-1000:],"stdout":p.stdout[-1000:]}
        return out
    def update(self,name:str,code:str,change_note:str,parents:list[str]|None=None,fitness:float|None=None)->int:
        sec=self.policy.scan(code)
        if not sec.allowed: raise ValueError("Unsafe meme code: "+", ".join(sec.reasons))
        m=self._meme(name)
        row=self.store.conn.execute("SELECT max(version) maxv FROM meme_versions WHERE meme_id=?",(m["meme_id"],)).fetchone()
        newv=int(row["maxv"] or 0)+1; now=utcnow()
        with self.store.tx() as c:
            c.execute("INSERT INTO meme_versions(meme_id,version,code,parents,fitness,created_at,change_note) VALUES(?,?,?,?,?,?,?)",
                      (m["meme_id"],newv,code,json.dumps(parents or [name]),fitness,now,change_note))
            c.execute("UPDATE memes SET current_version=?,fitness=? WHERE meme_id=?",(newv,fitness,m["meme_id"]))
        return newv
    def evaluate(self,name:str,cases:list[dict[str,Any]],metric:Callable[[Any,Any],float]|None=None)->float:
        if not cases:return 0.0
        metric=metric or (lambda actual,expected: 1.0 if actual==expected else 0.0)
        scores=[]
        for c in cases:
            r=self.execute(name,c.get("input"))
            scores.append(metric(r.get("result") if r.get("ok") else None,c.get("expected")))
        score=sum(scores)/len(scores)
        m=self._meme(name)
        with self.store.tx() as db: db.execute("UPDATE memes SET fitness=? WHERE meme_id=?",(score,m["meme_id"]))
        return score
    def mutate(self,name:str,mutation:str,change_note:str="mutation")->int:
        cur=self.current(name)
        # Explicit mutation payload; no hidden code generator.
        return self.update(name,cur["code"]+"\n"+mutation,change_note,parents=[name])
    def evolve(self,name:str,candidates:list[str],cases:list[dict[str,Any]])->dict[str,Any]:
        """Evaluate host-supplied variants; commit only regression-free improvements."""
        if len(cases)<3:raise ValueError("at least three evaluation cases are required")
        cur=self.current(name)
        def outcomes(target:str)->list[bool]:
            values=[]
            for case in cases:
                result=self.execute(target,case.get("input"))
                values.append(bool(result.get("ok") and result.get("result")==case.get("expected")))
            return values
        baseline_outcomes=outcomes(name);baseline=sum(baseline_outcomes)/len(baseline_outcomes)
        best=(baseline,cur["code"],None,0,0);evaluations=[]
        for idx,code in enumerate(candidates):
            sec=self.policy.scan(code)
            if not sec.allowed:
                evaluations.append({"index":idx,"allowed":False,"reasons":sec.reasons,"code_sha256":sha256_json(code)})
                continue
            temp=f"__candidate_{stable_id('C',{'n':name,'i':idx})[-8:]}"
            try:
                self.create(temp,code,"ephemeral candidate",[name])
                candidate_outcomes=outcomes(temp);score=sum(candidate_outcomes)/len(candidate_outcomes)
                regressions=sum(1 for old,new in zip(baseline_outcomes,candidate_outcomes) if old and not new)
                improvements=sum(1 for old,new in zip(baseline_outcomes,candidate_outcomes) if not old and new)
                eligible=score>baseline and regressions==0
                evaluations.append({"index":idx,"allowed":True,"score":score,"regressions":regressions,
                                    "improvements":improvements,"eligible":eligible,"code_sha256":sha256_json(code)})
                if eligible and score>best[0]:best=(score,code,idx,regressions,improvements)
            finally:
                m=self.store.conn.execute("SELECT meme_id FROM memes WHERE name=?",(temp,)).fetchone()
                if m:
                    with self.store.tx() as db: db.execute("DELETE FROM memes WHERE meme_id=?",(m["meme_id"],))
        result={"schema":"mos-meme-evolution/v4.1","meme":name,"from_version":cur["version"],"from":baseline,
                "cases":len(cases),"cases_sha256":sha256_json(cases),"candidate_source":"host_supplied",
                "evaluations":evaluations,"improved":best[2] is not None,"selected_index":best[2],"to":best[0],
                "regressions":best[3],"improved_cases":best[4],"created_at":utcnow()}
        if best[2] is not None:
            v=self.update(name,best[1],f"evolution candidate {best[2]}",parents=[name],fitness=best[0])
            result["version"]=v
        else:result["version"]=cur["version"]
        result["receipt_sha256"]=sha256_json(result)
        self.store.append_event(f"{cur['meme_id']}:evolution:{result['receipt_sha256'][:12]}","meme_evolution",result)
        return result
    def diff(self,name:str,v1:int,v2:int)->str:
        m=self._meme(name)
        rows={r["version"]:r["code"] for r in self.store.conn.execute("SELECT version,code FROM meme_versions WHERE meme_id=?",(m["meme_id"],))}
        if v1 not in rows or v2 not in rows: raise KeyError("version")
        return "".join(difflib.unified_diff(rows[v1].splitlines(True),rows[v2].splitlines(True),
                                            fromfile=f"{name}@v{v1}",tofile=f"{name}@v{v2}"))
    def rollback(self,name:str,target:int)->bool:
        m=self._meme(name)
        r=self.store.conn.execute("SELECT code,parents,fitness FROM meme_versions WHERE meme_id=? AND version=?",(m["meme_id"],target)).fetchone()
        if not r:return False
        maxv=self.store.conn.execute("SELECT max(version) maxv FROM meme_versions WHERE meme_id=?",(m["meme_id"],)).fetchone()["maxv"]
        newv=int(maxv or 0)+1
        now=utcnow()
        with self.store.tx() as c:
            c.execute("INSERT INTO meme_versions(meme_id,version,code,parents,fitness,created_at,change_note) VALUES(?,?,?,?,?,?,?)",
                      (m["meme_id"],newv,r["code"],r["parents"],r["fitness"],now,f"rollback to v{target}"))
            c.execute("UPDATE memes SET current_version=?,fitness=? WHERE meme_id=?",(newv,r["fitness"],m["meme_id"]))
        return True

    def relevant_catalog(self,query:str,limit:int=5)->list[dict[str,Any]]:
        """Return safe metadata for memes relevant to a cognitive cycle.

        Code is intentionally not injected into the LLM context. Execution
        remains explicit through MPL/API; the model only sees identity,
        version, description, fitness and lineage metadata.
        """
        import re
        q={x for x in re.findall(r"[\w-]{3,}",query.lower())}
        meme_mode=bool(q & {"meme","memetic","memes","мем","мемы","меметический","меметика"})
        scored=[]
        for m in self.store.conn.execute("SELECT * FROM memes WHERE status='active' ORDER BY name"):
            v=self.store.conn.execute("SELECT parents FROM meme_versions WHERE meme_id=? AND version=?",(m["meme_id"],m["current_version"])).fetchone()
            text=f"{m['name']} {m['description']}".lower();t={x for x in re.findall(r"[\w-]{3,}",text)}
            overlap=len(q&t)/(max(1,len(q|t)))
            exact=0.8 if m['name'].lower() in query.lower() else 0.0
            fitness=float(m['fitness']) if m['fitness'] is not None else 0.0
            score=exact+overlap+0.08*fitness+(0.08 if meme_mode else 0.0)
            if score>0.0:
                scored.append((score,{"meme_id":m["meme_id"],"name":m["name"],"description":m["description"],
                                      "version":m["current_version"],"fitness":m["fitness"],
                                      "parents":json.loads(v["parents"]) if v else []}))
        scored.sort(key=lambda x:(x[0],x[1]["fitness"] if x[1]["fitness"] is not None else -1,x[1]["name"]),reverse=True)
        return [x for _,x in scored[:limit]]

    def ontology(self)->dict[str,Any]:
        nodes=[]; edges=[]
        for m in self.store.conn.execute("SELECT * FROM memes ORDER BY name"):
            nodes.append({"id":m["meme_id"],"name":m["name"],"version":m["current_version"],"fitness":m["fitness"]})
            v=self.store.conn.execute("SELECT parents FROM meme_versions WHERE meme_id=? AND version=?",(m["meme_id"],m["current_version"])).fetchone()
            for p in json.loads(v["parents"]): edges.append({"from":p,"to":m["name"]})
        return {"nodes":nodes,"edges":edges}
