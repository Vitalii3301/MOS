"""Real-code memes: genome = Python source defining meme(x), executed only in the sandbox.
Shares the MemeOS store: contracts, journal, antimemes, lineage, active pointers.
Generators (this chat, the hub model, any node) only PROPOSE source; the gate decides."""
import json, time
from memeos import MemeOS, MemeError, ensure, sha, canon, goal_key
from sandbox import scan, evaluate_source

def code_card(source, goal, context=None, parents=(), origin=None, name=""):
    ensure(isinstance(source, str) and 0 < len(source) <= 20000, "bad_source")
    return {"schema": "mos-code-meme/v1", "name": name or goal, "goal": goal, "context": context or {},
            "parents": sorted(parents), "origin": origin or {"kind": "authored"}, "source": source}

class CodeMemes:
    def __init__(self, kernel: MemeOS): self.k = kernel

    def screen(self, card):
        v = ["static:" + x for x in scan(card["source"])]
        for sig in (sha(card), sha({"source": card["source"]})):
            r = self.k.db.execute("SELECT reason FROM antimemes WHERE signature=?", (sig,)).fetchone()
            if r: v.append("antimeme:" + r[0])
        return v

    def add(self, card):
        bad = self.screen(card); ensure(not bad, "rejected_by_antimeme:" + ",".join(bad))
        mid = sha(card)
        if not self.k.db.execute("SELECT 1 FROM memes WHERE id=?", (mid,)).fetchone():
            self.k.db.execute("INSERT INTO memes(id,card,created) VALUES(?,?,?)", (mid, canon(card), time.time()))
            self.k.event("code_meme_recorded", {"id": mid, "parents": card["parents"], "origin": card["origin"]})
        return mid

    def _passes(self, source, cid):
        c = self.k._contract(cid); cases = c["train"] + c["validation"]
        ok, n, _ = evaluate_source(source, cases); return ok, n

    def promote(self, mid, cid):
        card = self.k.card(mid); c = self.k._contract(cid)
        ensure(card["schema"] == "mos-code-meme/v1", "not_code_meme")
        from restrict import tainted
        if tainted(self.k, mid):
            self.k.event("rejected", {"id": mid, "contract": cid, "reason": "foreign_unrestricted"})
            return {"accepted": False, "reason": "foreign_unrestricted", "train": None, "validation": None, "holdout": None}
        ensure(card["goal"] == c["goal"] and card["context"] == c["context"], "goal_or_context_mismatch")
        tr, tn, _ = evaluate_source(card["source"], c["train"]); va, vn, det = evaluate_source(card["source"], c["validation"])
        prior = [r[0] for r in self.k.db.execute("SELECT id FROM contracts WHERE goal=? AND context=? AND id<>?",
                                                   (c["goal"], canon(c["context"]), cid))]
        regress = [p for p in prior if (lambda r: r[0] < r[1])(self._passes(card["source"], p))]
        key = goal_key(c["goal"], c["context"])
        act = self.k.db.execute("SELECT meme_id FROM active WHERE goal_key=?", (key,)).fetchone()
        if act:
            a = self.k.card(act[0])
            base = self._passes(a["source"], cid)[0] if a["schema"] == "mos-code-meme/v1" else \
                   __import__("memeos").evaluate(a["genome"], c["train"] + c["validation"], self.k.resolve)[1]
        perfect = tr == tn and va == vn
        improves = not act or act[0] == mid or tr + va > base
        ok = perfect and not regress and improves
        reason = "accepted" if ok else "test_failure" if not perfect else "regression" if regress else "no_improvement"
        holdout = evaluate_source(card["source"], c["holdout"])[:2] if c["holdout"] else None
        if ok:
            if act and act[0] != mid: self.k.db.execute("UPDATE memes SET status='superseded' WHERE id=?", (act[0],))
            self.k.db.execute("INSERT INTO active VALUES(?,?,NULL) ON CONFLICT(goal_key) DO UPDATE SET previous=active.meme_id, meme_id=excluded.meme_id", (key, mid))
            self.k.db.execute("UPDATE memes SET status='active' WHERE id=?", (mid,))
        self.k.event("promoted" if ok else "rejected", {"id": mid, "kind": "code", "contract": cid, "reason": reason,
                     "train": [tr, tn], "validation": [va, vn], "holdout_report_only": holdout, "regressions": regress})
        return {"accepted": ok, "reason": reason, "train": (tr, tn), "validation": (va, vn), "holdout": holdout}

    def propose(self, source, cid, generator, parents=None):
        """Any generator (LLM, human, other node) submits source; it is screened, recorded, gated."""
        c = self.k._contract(cid); act = self.k.active(c["goal"], c["context"])
        card = code_card(source, c["goal"], c["context"], parents=parents if parents is not None else ([act] if act else []),
                         origin={"kind": "proposed", "generator": generator})
        try: mid = self.add(card)
        except MemeError as e:
            self.k.event("proposal_blocked", {"generator": generator, "reason": str(e)[:300]})
            return {"accepted": False, "reason": str(e)}
        return {"meme": mid, **self.promote(mid, cid)}

    def execute(self, goal, value, context=None):
        mid = self.k.active(goal, context); ensure(mid, "no_active_meme"); card = self.k.card(mid)
        if card["schema"] != "mos-code-meme/v1": return self.k.execute(goal, value, context)
        from sandbox import run_source
        r = run_source(card["source"], [value]); ensure(r["ok"] and r["results"][0]["ok"], "execution_failed")
        self.k.event("executed", {"id": mid, "input": sha(value), "output": sha(r["results"][0]["value"])})
        return r["results"][0]["value"], mid

    def hybridize(self, a, b, cid):
        """Real-code hybrid: child = b(a(x)) and a(b(x)) built from both genomes; gate picks."""
        sa, sb = self.k.card(a)["source"], self.k.card(b)["source"]
        def wrap(first, second):
            return (first.replace("def meme(", "def _gene_a(") + "\n" + second.replace("def meme(", "def _gene_b(")
                    + "\n\ndef meme(x):\n    return _gene_b(_gene_a(x))\n")
        results = []
        for s in (wrap(sa, sb), wrap(sb, sa)):
            results.append(self.propose(s, cid, "hybridize", parents=[a, b]))
            if results[-1].get("accepted"): return results[-1]
        return results[-1]
