from __future__ import annotations
import json
from typing import Any
from .storage import SQLiteStore
from .util import stable_id, utcnow

class MetaCognition:
    def __init__(self,store:SQLiteStore): self.store=store
    def remember(self,kind:str,statement:str,*,importance:float=.7,confidence:float=.7,
                 verification_status:str="unknown",evidence_refs:list[str]|None=None,
                 review_status:str="active",review_when:list[str]|None=None,metadata:dict[str,Any]|None=None)->str:
        mid=stable_id("MM",{"kind":kind,"statement":statement})
        with self.store.tx() as c:
            c.execute("""INSERT INTO meta_memory(memory_id,kind,statement,importance,confidence,verification_status,evidence_refs,review_status,review_when,metadata,updated_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(memory_id) DO UPDATE SET importance=excluded.importance,confidence=excluded.confidence,
                         verification_status=excluded.verification_status,evidence_refs=excluded.evidence_refs,
                         review_status=excluded.review_status,review_when=excluded.review_when,metadata=excluded.metadata,updated_at=excluded.updated_at""",
                      (mid,kind,statement,importance,confidence,verification_status,json.dumps(evidence_refs or []),
                       review_status,json.dumps(review_when or []),json.dumps(metadata or {},ensure_ascii=False),utcnow()))
        return mid
    def self_item(self,kind:str,domain:str,statement:str,*,confidence:float=.7,status:str="active",
                  evidence_refs:list[str]|None=None,scope_limit:str="",metadata:dict[str,Any]|None=None)->str:
        iid=stable_id("SM",{"kind":kind,"domain":domain,"statement":statement})
        with self.store.tx() as c:
            c.execute("""INSERT INTO self_model(item_id,kind,domain,statement,confidence,status,evidence_refs,scope_limit,metadata,updated_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(item_id) DO UPDATE SET confidence=excluded.confidence,status=excluded.status,
                         evidence_refs=excluded.evidence_refs,scope_limit=excluded.scope_limit,metadata=excluded.metadata,updated_at=excluded.updated_at""",
                      (iid,kind,domain,statement,confidence,status,json.dumps(evidence_refs or []),scope_limit,
                       json.dumps(metadata or {},ensure_ascii=False),utcnow()))
        return iid
    def active_meta(self,limit:int=8)->list[dict[str,Any]]:
        rows=self.store.conn.execute("""SELECT * FROM meta_memory ORDER BY
           CASE review_status WHEN 'review_due' THEN 0 ELSE 1 END, importance DESC, confidence ASC LIMIT ?""",(limit,))
        return [{**dict(r),"evidence_refs":json.loads(r["evidence_refs"]),"review_when":json.loads(r["review_when"]),"metadata":json.loads(r["metadata"])} for r in rows]
    def active_self(self)->list[dict[str,Any]]:
        return [{**dict(r),"evidence_refs":json.loads(r["evidence_refs"]),"metadata":json.loads(r["metadata"])}
                for r in self.store.conn.execute("SELECT * FROM self_model WHERE status IN ('active','under_review','mitigated') ORDER BY kind,domain")]
    def learn_from_episode(self,episode)->list[str]:
        out=[]
        if episode.outcome and episode.revision:
            out.append(self.remember("failure_memory",
                    f"Episode {episode.episode_id} required revision: {episode.revision}",importance=.95,confidence=1.0,
                    verification_status="verified",evidence_refs=[episode.episode_id],review_when=["similar task recurs"]))
            out.append(self.self_item("weakness","decision_revision",
                    "Recent decision required outcome-driven revision.",confidence=.9,status="active",
                    evidence_refs=[episode.episode_id],scope_limit="Only the recorded episode and close analogues."))
        elif episode.outcome and not episode.revision:
            out.append(self.remember("validated_episode",
                    f"Episode {episode.episode_id} closed without revision: {episode.outcome}",importance=.7,confidence=.8,
                    verification_status="supported",evidence_refs=[episode.episode_id]))
        return out
    def validate_weakness(self,domain:str,*,passed:bool,evidence_ref:str)->dict[str,Any]:
        rows=list(self.store.conn.execute("SELECT * FROM self_model WHERE kind='weakness' AND domain=? AND status IN ('active','under_review','mitigated') ORDER BY updated_at DESC",(domain,)))
        if not rows:
            raise KeyError(domain)
        target_status='mitigated' if passed else 'under_review'
        changed=[]
        with self.store.tx() as c:
            for r in rows:
                refs=json.loads(r['evidence_refs']);
                if evidence_ref not in refs: refs.append(evidence_ref)
                c.execute("UPDATE self_model SET status=?,evidence_refs=?,updated_at=? WHERE item_id=?",
                          (target_status,json.dumps(refs,ensure_ascii=False),utcnow(),r['item_id']))
                changed.append(r['item_id'])
        statement=(f"Weakness domain '{domain}' {'passed targeted validation' if passed else 'received contradictory evidence'}: {evidence_ref}")
        self.remember('validation_event',statement,importance=.94,confidence=1.0,verification_status='verified',
                      evidence_refs=[evidence_ref],review_status='active',review_when=['new contradictory evidence' if passed else 'targeted rerun passes'])
        self.store.append_event(f"VAL-{stable_id('V',{'domain':domain,'evidence':evidence_ref,'passed':passed})}",
                                'self_model_validation',{'domain':domain,'passed':passed,'evidence_ref':evidence_ref,'status':target_status,'items':changed})
        return {'domain':domain,'passed':passed,'status':target_status,'items':changed,'evidence_ref':evidence_ref}
