#!/usr/bin/env python3
"""MemeOS P2P node: serves memetic tasks over MOS Hub, autonomously.

Message kind "mos-meme-task/v1", payload:
  {"op": "learn",   "contract": {goal, context, train, validation, holdout?}}  -> evolves; replies with bundle
  {"op": "execute", "goal", "context", "input"}                                -> runs active meme
  {"op": "feedback","goal", "context", "input", "expected"}                    -> agent experience + self-repair
  {"op": "adopt",   "bundle", "contract"}   -> import (never trusts sender), verify on the given contract
Replies: kind "mos-meme-result/v1" to the sender. Payloads are data, never instructions:
only these four ops exist; nothing in a payload is executed outside the meme language / sandbox.
Idempotent: processed message ids and the mailbox cursor live in the node's SQLite.
Usage:  MOS_HUB_CONFIG=mos_hub_config.json python3 node.py --once | --loop SECONDS
"""
import argparse, gzip, hashlib, json, os, secrets, sys, time, traceback
from urllib import request, error
from genememe import RULE
from memeos import MemeOS, MemeError, canon, sha, ensure
from agent import Agent
from codememes import CodeMemes
from modelgen import ModelGenerator

ALLOWED = {"mos-user", "mos-gpt", "mos-grok", "mos-grok-agent", "mos-manus", "mos-claude", "mos-kimi",
           "mos-gemini", "mos-gemini-aistudio-20261113", "mos-perplexity", "mos-claude-code"}
OWNER_SIDE = {"mos-user", "mos-claude"}          # may ask a cell that holds the hub admin token to create a new identity
NODE_SCHEMA = "CREATE TABLE IF NOT EXISTS node_inbox(message_id TEXT PRIMARY KEY, seq INTEGER, sender TEXT, status TEXT, reply TEXT, at REAL); " \
              "CREATE TABLE IF NOT EXISTS node_meta(key TEXT PRIMARY KEY, value TEXT)"

def js_compat(v):
    """Make a payload hash-identical under the hub's JavaScript canonicalization. JS prints 1.0 as 1 and cannot hold
    integers of 2^53 and above exactly, so those travel as {"$float": 1} and {"$bigint": "<digits>"} and are restored by
    the receiving cell (js_restore). Before 2.24 integral floats silently became integers on the way."""
    if isinstance(v, bool): return v
    if isinstance(v, int) and abs(v) >= 2 ** 53: return {"$bigint": str(v)}
    if isinstance(v, float) and v.is_integer() and abs(v) < 2 ** 53: return {"$float": int(v)}     # 2.0 must arrive as 2.0, not 2: probes compare types
    if isinstance(v, dict): return {k: js_compat(x) for k, x in v.items()}
    if isinstance(v, list): return [js_compat(x) for x in v]
    return v

def js_restore(v):
    if isinstance(v, dict):
        if len(v) == 1 and isinstance(v.get("$bigint"), str) and v["$bigint"].lstrip("-").isdigit(): return int(v["$bigint"])
        if len(v) == 1 and isinstance(v.get("$float"), int) and not isinstance(v.get("$float"), bool): return float(v["$float"])
        return {k: js_restore(x) for k, x in v.items()}
    if isinstance(v, list): return [js_restore(x) for x in v]
    return v

class Hub:
    def __init__(self, cfg):
        self.url, self.node, self.token = cfg["hub_url"].rstrip("/"), cfg["node_id"], cfg["node_token"]
    def call(self, method, path, body=None):
        h = {"Accept": "application/json", "Authorization": "Bearer " + self.token}
        data = None
        if body is not None: h["Content-Type"] = "application/json"; data = json.dumps(body, ensure_ascii=False).encode()
        try:
            with request.urlopen(request.Request(self.url + path, method=method, headers=h, data=data), timeout=30) as r:
                raw = r.read().decode(); return r.status, json.loads(raw) if raw else {}
        except error.HTTPError as e:
            return e.code, {"error": e.read().decode(errors="replace")[:300]}
    def send(self, recipient, kind, payload, message_id):
        payload = js_compat(payload)
        env = {"schema": "mos-remote-message/v1", "message_id": message_id, "recipient": recipient, "kind": kind,
               "payload": payload, "payload_sha256": hashlib.sha256(canon(payload).encode()).hexdigest(),
               "expires_at": int(time.time()) + 7 * 86400}
        return self.call("POST", "/v1/messages", env)

class Node:
    def __init__(self, hub, db_path):
        self.hub, self.k = hub, MemeOS(db_path)
        self.k.db.executescript(NODE_SCHEMA)
        self.barrier = None
        bp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "barrier_deploy.json")
        if os.path.exists(bp):
            from barrier import Barrier
            self.barrier = Barrier.from_card(json.load(open(bp)))
        self.model = ModelGenerator(self.k.db, barrier=self.barrier)
        self.agent = Agent(self.k, hub.node + "-agent", generators=[self.model]); self.cm = CodeMemes(self.k)
        self.net = None; self._sender = None; self.forecaster = None; self.skill_gene = None; self.forager = None
        from genememe import GeneMemes
        self.genes = GeneMemes(self.k); self.genes.cell = hub.node
        from breed import Breeder
        self.breeder = Breeder(self)
        act = self.genes.active("skill_gene")
        if act:
            try: self.skill_gene = self.genes.load(act)          # the gene this cell itself adopted survives restarts
            except Exception: pass
        from drive import Drive
        self.drive = Drive(self)

    def attach_network(self, peers, clean, members=()):
        from net import Network
        self.net = Network(self, peers, clean, members); return self.net

    def cursor(self):
        r = self.k.db.execute("SELECT value FROM node_meta WHERE key='cursor'").fetchone()
        if r: return int(r[0])
        # remembered at once (mos-grok: a cell restarted before its first message would otherwise re-read the whole mailbox)
        start = int(os.environ.get("MEMEOS_START_CURSOR", "0"))
        self.k.db.execute("INSERT OR IGNORE INTO node_meta VALUES('cursor', ?)", (str(start),)); return start

    def solve(self, p):
        """Memetic scheduler: own active meme > verified library meme from the network > composition of
        local+library genes > evolution. Every source except 'own active' passes this cell's gate first."""
        goal, ctx = p["goal"], p.get("context") or {}
        mid = self.k.active(goal, ctx)
        if mid:
            out = self.agent.perform(goal, p["input"], ctx); return {"source": "own_active", "output": out["output"], "meme": mid}
        c = p.get("contract")
        lib = [m for m, in self.k.db.execute("SELECT meme FROM net_competence WHERE goal=? ORDER BY at DESC", (goal,))]
        if c:
            cid = self.k.contract(goal, c["train"], c["validation"], c.get("holdout", []), ctx)
            for m in lib:                                  # adopt a neighbour's version only after OUR gate, restricted to OUR evidence
                card = self.k.card(m)
                if card["context"] != ctx: continue
                from restrict import adopt_restricted
                g = adopt_restricted(self.k, m, cid)
                if g["accepted"]:
                    out = self.agent.perform(goal, p["input"], ctx)
                    return {"source": "network_library", "output": out["output"], "meme": g["meme"], "restricted_from": m, "restriction": g["restriction"],
                            "from": self.k.db.execute("SELECT cell FROM net_competence WHERE meme=?", (m,)).fetchone()[0]}
            if not self.budget_take("learn"): raise MemeError("daily_compute_budget_exhausted")
            r = self.k.learn(cid, generations=int(os.environ.get("MEMEOS_MAX_GENERATIONS", "100")), restarts=2, max_candidates=8)
            if r["ok"]:
                self.drive.close(goal, ctx)
                out = self.agent.perform(goal, p["input"], ctx)
                return {"source": "composition" if r.get("phase") == "composition" else "learned_" + str(r.get("phase")), "output": out["output"],
                        "meme": r["meme"], "composed_of": r.get("composed_of"), "evaluations": r["evaluations"]}
            self.drive.note("unsolved", goal, ctx, {"train": c["train"], "validation": c["validation"], "holdout": c.get("holdout", [])}, requester=self._sender)
            return {"source": "none", "reason": r.get("reason"), "evaluations": r["evaluations"], "noted_as_gap": True}
        self.drive.note("unknown", goal, ctx, requester=self._sender)
        return {"source": "none", "reason": "unknown_goal_and_no_contract", "library_versions": len(lib), "noted_as_gap": True}

    def snapshot(self, full=False):
        """What this cell KNOWS (not its full journal). full=True adds what a backup needs to bring the cell back:
        its labelled evidence (with labellers), the field set's identities, and the body of the active gene."""
        T = {"memes": "SELECT id,card,created,status FROM memes WHERE status IN ('active','library') OR id IN (SELECT meme_id FROM active) OR id IN (SELECT id FROM foreign_memes)",
             "contracts": "SELECT * FROM contracts", "active": "SELECT * FROM active", "antimemes": "SELECT * FROM antimemes", "motifs": "SELECT * FROM motifs",
             "foreign_memes": "SELECT * FROM foreign_memes", "usage": "SELECT * FROM usage", "net_genes": "SELECT * FROM net_genes",
             "net_gene_samples": "SELECT * FROM net_gene_samples", "net_competence": "SELECT * FROM net_competence", "net_recalls": "SELECT * FROM net_recalls",
             "net_clean_extra": "SELECT * FROM net_clean_extra", "forecast_memory": "SELECT * FROM forecast_memory", "gaps": "SELECT * FROM gaps",
             "gene_memes": "SELECT * FROM gene_memes", "gene_verdicts": "SELECT * FROM gene_verdicts", "gene_oof": "SELECT * FROM gene_oof", "lifecycle": "SELECT * FROM lifecycle",
             "forage": "SELECT * FROM forage"}
        if full: T.update(skill_evidence="SELECT sha256,label,source,text,at,by FROM skill_evidence", field_seen="SELECT * FROM field_seen",
                          node_meta_cursor="SELECT key,value FROM node_meta WHERE key='cursor'")
        tables = {}
        for name, q in T.items():
            try: tables[name] = [list(r) for r in self.k.db.execute(q)]
            except Exception: tables[name] = []
        have = {r[0] for r in tables["memes"]}; todo = list(have)          # dependencies of exported memes travel too
        from memeos import deps as _deps
        while todo:
            c = json.loads(self.k.db.execute("SELECT card FROM memes WHERE id=?", (todo.pop(),)).fetchone()[0])
            for d in (_deps(c["genome"]) if "genome" in c else []) + list(c.get("parents") or []):
                row = self.k.db.execute("SELECT id,card,created,status FROM memes WHERE id=?", (d,)).fetchone()
                if row and d not in have: have.add(d); tables["memes"].append(list(row)); todo.append(d)
        head = self.k.db.execute("SELECT seq,hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        kinds = dict(self.k.db.execute("SELECT kind, COUNT(*) FROM events GROUP BY kind"))
        body = {"schema": "mos-cell-snapshot/v1", "cell": self.hub.node, "version": __import__("memeos").VERSION, "at": time.time(), "tables": tables,
                "journal": {"events": head[0] if head else 0, "head": head[1] if head else None, "by_kind": kinds, "integrity": self.k.integrity()}}
        if full:
            import base64
            bodies = {}
            for gid, card in self.k.db.execute("SELECT id, card FROM gene_memes WHERE status='active'"):
                h = json.loads(card)["body_sha256"]; raw = self.genes.blob(h)
                if raw: bodies[h] = base64.b64encode(raw).decode()
            body["gene_bodies"] = bodies
        return body

    def backup(self, force=False):
        """D7: the cell's full snapshot goes to its OWN hub mailbox in parts, only when it changed since the last backup.
        The hub keeps it on another machine; `restore_from_hub` brings a cell back from it on an empty volume."""
        import base64
        body = self.snapshot(full=True); stable = {k: v for k, v in body.items() if k not in ("at", "journal")}
        state = hashlib.sha256(canon(stable).encode()).hexdigest()
        if not force and (self.k.db.execute("SELECT value FROM node_meta WHERE key='backup_state'").fetchone() or [None])[0] == state:
            return {"ok": True, "skipped": "unchanged"}
        raw = gzip.compress(canon(body).encode(), 9); sha_ = hashlib.sha256(raw).hexdigest(); P = 150000
        parts = [raw[i:i + P] for i in range(0, len(raw), P)]; bid = "%d-%s" % (int(time.time()), sha_[:12]); sent = 0
        for i, pt in enumerate(parts):
            st_, _ = self.hub.send(self.hub.node, "memeos-backup/v1", {"schema": "memeos-backup/v1", "backup_id": bid, "sha256": sha_, "part": i, "of": len(parts),
                                                                     "version": body["version"], "part_b64": base64.b64encode(pt).decode()}, "MSG-backup-%s-%s-%d" % (self.hub.node, bid, i))
            sent += st_ in (200, 201)
        ok = sent == len(parts)
        if ok: self.k.db.execute("INSERT OR REPLACE INTO node_meta VALUES('backup_state', ?)", (state,))
        self.k.event("backup_sent", {"backup_id": bid, "sha256": sha_, "parts": len(parts), "ok": ok, "bytes": len(raw)})
        return {"ok": ok, "backup_id": bid, "parts": len(parts), "bytes": len(raw)}

    def screen_skill(self, text, barrier_info=False, journal=True):
        """The one path a skill-screening request takes; the life cycle runs it once before a new gene may stay active."""
        import skillgene
        out, d = skillgene.screen(self.skill_gene, text, self.barrier)
        if journal:
            self.k.event("skill_screened", {"sha256": hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest(), "verdict": out["verdict"],
                                            "reasons": out["reasons"], "skill_gene_score": out["skill_gene"]["score"]})
        if barrier_info:    # on request only (feedback from mos-grok: shown by default it breeds alarm fatigue)
            out["barrier_info"] = {"invasive": d["invasive"], "reasons": d.get("reasons"), "voting": False,
                                   "meaning": "document-injection barrier; a skill is itself instructions, so it fires on ~29% of benign skills; not part of the verdict"}
        return out

    def budget_take(self, op):
        """Daily cap on compute-heavy operations (learn, feedback that may trigger learning, adopt)."""
        if op not in ("learn", "feedback", "adopt", "discover", "immunize", "breed_gene"): return True
        day = time.strftime("%Y-%m-%d", time.gmtime()); key = "budget:" + day
        used = int((self.k.db.execute("SELECT value FROM node_meta WHERE key=?", (key,)).fetchone() or ["0"])[0])
        if used >= int(os.environ.get("MEMEOS_DAILY_HEAVY_OPS", "40")): return False
        self.k.db.execute("INSERT INTO node_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(used + 1)))
        return True

    def handle(self, p):
        op = p.get("op")
        if not self.budget_take(op): raise MemeError("daily_compute_budget_exhausted")
        if op == "learn":
            c = p["contract"]
            cid = self.k.contract(c["goal"], c["train"], c["validation"], c.get("holdout", []), c.get("context"))
            r = self.k.learn(cid, generations=int(os.environ.get("MEMEOS_MAX_GENERATIONS", "100")), restarts=2, max_candidates=8)
            if r.get("phase") == "composition" and r["ok"] and "genome" not in self.k.card(r["meme"]):
                r["program"] = {"python": "cross-substrate composition"}
            out = {"ok": r["ok"], "reason": r.get("reason") or r["gate"]["reason"], "evaluations": r["evaluations"],
                   "rejected_candidates": len(r.get("rejected", [])), "generator": "memeos-" + (r.get("phase") or "evolution"),
                   **({"composed_of": r["composed_of"]} if r.get("composed_of") else {})}
            if r["ok"]:
                gate = {k: r["gate"][k] for k in ("train", "validation", "holdout")}
                out.update(meme=r["meme"], program=r["program"], gate=gate, bundle=self.k.export(r["meme"]))
                if self.net is not None and p.get("share", True): out["shared_to"] = [x["to"] for x in self.net.share_skill(r["meme"], gate)]
                return out
            if self.model.available and p.get("allow_model", True):
                attempts = []
                try: props = self.model({"goal": c["goal"], "context": c.get("context") or {}, "train": c["train"]})
                except MemeError as e: props = []; attempts.append({"generator": "hub-model", "error": str(e)})
                for prop in props:
                    res = self.cm.propose(prop["source"], cid, prop["generator"])
                    attempts.append({"generator": prop["generator"], "accepted": res.get("accepted"), "reason": res.get("reason"),
                                     "train": res.get("train"), "validation": res.get("validation")})
                    if res.get("accepted"):
                        out.update(ok=True, reason="accepted", generator=prop["generator"], meme=res["meme"], source=prop["source"],
                                   gate={k: res[k] for k in ("train", "validation", "holdout")})
                        if self.net is not None and p.get("share", True):
                            out["shared_to"] = [x["to"] for x in self.net.share_skill(res["meme"], out["gate"])]
                        break
                out["model_attempts"] = attempts; out["model_calls_today"] = self.model.used_today()
            if not out["ok"]:
                self.drive.note("unsolved", c["goal"], c.get("context") or {}, {"train": c["train"], "validation": c["validation"], "holdout": c.get("holdout", [])}, requester=self._sender)
                out["noted_as_gap"] = True
            return out
        if op == "discover":
            from science import accept_law, show
            rows, tgt, vs = p["train"], p["target"], p["variables"]; test = p["holdout"]
            ensure(isinstance(rows, list) and isinstance(test, list) and 3 <= len(rows) <= 200 and 1 <= len(test) <= 200, "bad_rows")
            ensure(isinstance(vs, list) and 1 <= len(vs) <= 4 and all(isinstance(v, str) for v in vs), "bad_variables")
            for r in rows + test:
                ensure(isinstance(r, dict) and all(isinstance(r.get(k), (int, float)) and not isinstance(r.get(k), bool) for k in vs + [tgt]), "bad_row")
            res = accept_law(rows, test, tgt, vs, tol=float(p.get("tol", 0.02)), max_size=min(int(p.get("max_size", 6)), 6))
            L = res["law"]
            law = {"expr": show(L["expr"]), "a": L["a"], "b": L["b"], "holdout_max_rel_err": L["holdout_max_rel_err"]} if L else None
            self.k.event("science_discover", {"accepted": res["accepted"], "law": law and law["expr"]})
            return {"ok": True, "accepted": res["accepted"], "law": law, "verdict": "law_found" if res["accepted"] else "no_law",
                    "runners_up": [show(h["expr"]) for h in res["runners_up"]]}
        if op == "immunize":
            ensure(self.net is not None, "network_not_attached")
            ss = p["samples"]; ensure(isinstance(ss, list) and 2 <= len(ss) <= 60 and all(isinstance(x, str) and len(x) <= 4000 for x in ss), "bad_samples")
            return {"ok": True, **self.net.immunize(ss, int(p.get("max_hops", 3)))}
        if op == "solve":
            return {"ok": True, **self.solve(p)}
        if op == "report_miss":
            ensure(self.barrier is not None, "barrier_not_loaded"); t = p["text"]; ensure(isinstance(t, str) and 10 <= len(t) <= 4000, "bad_text")
            caught = self.barrier.decide(t, "content")["invasive"]
            if not caught: self.drive.note("missed", detail=t, requester=self._sender)
            return {"ok": True, "already_caught": caught, "noted_as_gap": not caught}
        if op == "report_false_alarm":
            ensure(self.barrier is not None, "barrier_not_loaded"); t = p["text"]; ensure(isinstance(t, str) and 10 <= len(t) <= 4000, "bad_text")
            flagged = self.barrier.decide(t, "content")
            if flagged["invasive"]: self.drive.note("false_alarm", detail=t, requester=self._sender)
            return {"ok": True, "currently_flagged": flagged["invasive"], "reasons": flagged["reasons"], "noted_as_gap": flagged["invasive"]}
        if op == "agenda":
            return {"ok": True, **self.drive.agenda()}
        if op == "reflect":
            return {"ok": True, **self.drive.pursue(int(p.get("limit", 2)))}
        if op == "vocabulary":
            from formula import show
            words = []
            for mid, card in self.k.db.execute("SELECT id, card FROM memes WHERE status IN ('active','library')"):
                c = json.loads(card); g = c.get("genome", {}).get("pipeline", [])
                if len(g) == 1 and g[0].get("op") == "formula":
                    words.append({"goal": c["goal"], "formula": show(g[0]["args"][0]), "meme": mid[:12], "origin": c["origin"].get("kind")})
            return {"ok": True, "formula_words": words}
        if op == "emergence":
            from emergence import analyse
            return {"ok": True, **analyse(self.k)}
        if op == "net_status":
            ensure(self.net is not None, "network_not_attached"); return {"ok": True, **self.net.status()}
        if op == "forage_status":
            ensure(self.forager is not None, "forager_off"); return {"ok": True, **self.forager.status(int(p.get("top", 20)))}
        if op == "forage_now":
            ensure(self.forager is not None, "forager_off"); return self.forager.run_once()
        if op == "label_skill":
            ensure(self.forager is not None, "forager_off"); ensure(p.get("label") in (0, 1), "label_must_be_0_or_1")
            return self.forager.label(str(p["repo"]), str(p["path"]), p["label"], self._sender)
        if op == "import_evidence":
            # labelled skills sent as memeos-blob/v1 parts (from an allowed sender) become this cell's own evidence
            raw = self.genes.blob(str(p.get("blob_sha256", ""))); ensure(raw is not None, "evidence_blob_missing")
            body = json.loads(gzip.decompress(raw)); items = body.get("cells", {}).get(self.hub.node) or body.get("items") or []
            ensure(isinstance(items, list) and all(isinstance(e, dict) and e.get("label") in (0, 1) and isinstance(e.get("text"), str) for e in items), "bad_evidence")
            n = self.genes.add_evidence(items, source="import:" + str(self._sender)); return {"ok": True, "added": n,
                    "evidence": dict(self.k.db.execute("SELECT label, COUNT(*) FROM skill_evidence GROUP BY label"))}
        if op == "invite_node":
            # The owner side asks for a new hub identity. The new token never travels in clear: it is sealed with a keystream
            # derived from THIS cell's own node token, which only the owner side also holds (the hub stores no tokens in clear).
            ensure(self._sender in OWNER_SIDE and self._sender == self.hub.node, "only_the_owner_identity_of_this_cell")
            admin = os.environ.get("MOS_HUB_ADMIN_TOKEN", ""); ensure(admin, "no_admin_token_here")
            nid = str(p.get("node_id", "")); ensure(nid.startswith("mos-") and 4 < len(nid) <= 40 and all(c.isalnum() or c == "-" for c in nid), "bad_node_id")
            req = request.Request(self.hub.url + "/v1/nodes/register", method="POST", data=json.dumps({"node_id": nid, "capabilities": p.get("capabilities") or {}}).encode(),
                                  headers={"Authorization": "Bearer " + admin, "Content-Type": "application/json"})
            try:
                with request.urlopen(req, timeout=30) as r: tok = json.loads(r.read())["token"]
            except error.HTTPError as e: return {"ok": False, "error": "hub_refused:%d" % e.code}
            return {"ok": True, "node_id": nid, **seal(tok.encode(), self.hub.token)}
        if op == "backup_now":
            ensure(self._sender in OWNER_SIDE, "owner_only"); return self.backup(force=bool(p.get("force")))
        if op == "rule":
            from genememe import RULE_TEXT
            import code_bundle
            try: root = code_bundle.code_root(os.path.dirname(os.path.abspath(__file__)))
            except OSError: root = None
            return {"ok": True, "rule": RULE, "text": RULE_TEXT, "code_root": root, "version": __import__("memeos").VERSION}
        if op == "gene_status":
            g = self.genes; q = self.k.db.execute
            return {"ok": True, "active": {k: g.active(k) for k in ("skill_gene",)},
                    "genes": [{"id": i[:16], "name": n, "kind": kd, "status": st} for i, n, kd, st in q("SELECT id,name,kind,status FROM gene_memes ORDER BY created")],
                    "lineage": g.lineage(g.active("skill_gene")) if g.active("skill_gene") else [],
                    "evidence": dict(q("SELECT label, COUNT(*) FROM skill_evidence GROUP BY label")),
                    "verdicts": [{"gene": a[:16], "cell": c, "adopted": bool(d), "reason": r,
                                  **{x: v for x, v in (json.loads(mt or "{}") or {}).items() if x in ("rule", "new", "current", "overall", "clusters", "false_alarm_margin", "field", "left_out_as_fitted", "rule_1_would_say")}}
                                 for a, c, d, r, mt in q("SELECT gene,cell,adopted,reason,metrics FROM gene_verdicts")],
                    "rule": RULE, "active_gene": (lambda c_: c_ and {"id": g.active("skill_gene"), "name": c_["name"], "body_sha256": c_["body_sha256"],
                                                                     "threshold_e4": int(round((c_.get("threshold") or 0) * 10000))})(g.card(g.active("skill_gene")) if g.active("skill_gene") else None),
                    "own_evaluation": g.evaluate_id(g.active("skill_gene")) if g.active("skill_gene") and p.get("evaluate") else None}
        if op == "withdraw_gene":
            # the owner side takes back a gene bred in THIS cell that turned out to be flawed: the previous gene is active again,
            # and the reason stays on record
            ensure(self._sender in OWNER_SIDE, "owner_only"); g = self.genes; gid = g.active("skill_gene"); c = g.card(gid) if gid else None
            ensure(c and (c.get("origin") or {}).get("by") == self.hub.node, "active_gene_was_not_bred_here")
            prev = (c["origin"].get("replaces") or (c.get("parents") or [None])[0]); ensure(prev and g.card(prev), "previous_gene_unknown")
            g.revert(gid, prev); self.skill_gene = g.load(prev); why = str(p.get("reason", ""))[:300]
            self.k.db.execute("INSERT OR REPLACE INTO gene_verdicts VALUES(?,?,?,?,?,?)", (gid, self.hub.node, 0, "withdrawn:" + why, canon({"withdrawn": True}), time.time()))
            self.k.event("gene_withdrawn", {"gene": gid, "restored": prev, "reason": why}); return {"ok": True, "withdrawn": gid, "active": prev}
        if op == "judge_gene":
            # read-only: what the registered rule says about gene A against gene B on this cell's data (ids or unique id prefixes)
            g = self.genes; pick = lambda x: (lambda r: r[0][0] if len(r) == 1 else None)(self.k.db.execute("SELECT id FROM gene_memes WHERE id LIKE ?", (str(x) + "%",)).fetchall())
            a_, b_ = pick(p.get("gene")), pick(p.get("against")); ensure(a_ and b_ and a_ != b_, "need_two_known_genes")
            ok, reason, m = g.decide(a_, against=b_)
            return {"ok": True, "gene": a_, "against": b_, "would_adopt": ok, "reason": reason, **{k: v for k, v in m.items() if k != "current_id"}}
        if op == "breed_gene":
            # the cell grows the next version of its gene from its own evidence; it then lives the ordinary life cycle
            ensure(self.net is not None, "no_network"); return self.breeder.breed(share=p.get("share", True))
        if op == "lifecycle_status":
            ensure(self.net is not None, "no_network"); return {"ok": True, "decisions": self.net.life.status(int(p.get("limit", 30)))}
        if op == "evidence_status":
            q = self.k.db.execute
            return {"ok": True, "by_label": dict(q("SELECT label, COUNT(*) FROM skill_evidence GROUP BY label")),
                    "by_labeller": [{"by": b, "label": l, "n": n} for b, l, n in q("SELECT COALESCE(by,'seed'), label, COUNT(*) FROM skill_evidence GROUP BY 1,2 ORDER BY 3 DESC")]}
        if op == "share_gene":
            ensure(self.net is not None, "no_network"); gid = p.get("gene") or self.genes.active("skill_gene"); ensure(gid, "no_gene")
            to = p.get("to"); ensure(to is None or (isinstance(to, list) and all(isinstance(x, str) for x in to)), "bad_to")
            return {"ok": True, "gene": gid, "sent": self.net.share_gene(gid, to=to)}
        if op == "import_gene":
            # a gene made OUTSIDE the network (e.g. retrained by a person or another AI): body parts must already have
            # arrived as memeos-blob/v1; this cell applies the same own-evidence gate as for any neighbour's gene
            ensure(self.net is not None, "no_network")
            ok, reason, m = self.net.on_gene(self._sender, {"card": p.get("card")})
            self.k.event("gene_imported", {"from": self._sender, "adopted": ok, "reason": reason})
            return {"ok": True, "adopted": ok, "reason": reason, **{k: v for k, v in m.items() if k != "gene"}, "gene": m.get("gene"),
                    "shared": self.net.share_gene(m["gene"]) if ok and p.get("share", True) else []}
        if op == "export_blob":
            # a gene body (e.g. one bred in this cell) in chunks that fit hub messages; the receiver checks the sha256 of the whole
            import base64
            raw = self.genes.blob(str(p.get("sha256", ""))); ensure(raw is not None, "blob_missing")
            b = base64.b64encode(raw).decode(); size = 150000; n = max(1, -(-len(b) // size)); i = int(p.get("part", 0)); ensure(0 <= i < n, "bad_part")
            return {"ok": True, "part": i, "of": n, "sha256": p["sha256"], "chunk": b[i * size:(i + 1) * size]}
        if op == "export_state":
            # Portable snapshot of what this cell KNOWS (not its full journal): returned in chunks that fit hub messages.
            import base64
            snap = self.k.db.execute("SELECT value FROM node_meta WHERE key='snapshot_cache'").fetchone() if p.get("part", 0) else None
            if snap is None:
                body = self.snapshot()
                raw = base64.b64encode(gzip.compress(canon(body).encode(), 9)).decode()
                self.k.db.execute("INSERT OR REPLACE INTO node_meta VALUES('snapshot_cache',?)", (raw,)); snap = (raw,)
            raw = snap[0]; size = 150000; n = max(1, -(-len(raw) // size)); i = int(p.get("part", 0)); ensure(0 <= i < n, "bad_part")
            return {"ok": True, "part": i, "of": n, "sha256_b64": hashlib.sha256(raw.encode()).hexdigest(), "chunk": raw[i * size:(i + 1) * size]}
        if op == "screen_skill":
            # an agent skill (SKILL.md + scripts) is a meme from another host's pool: screen it before anyone installs it
            text = p.get("text")
            ensure(isinstance(text, str) and 0 < len(text) <= 2_000_000, "bad_text")
            # The verdict comes from the skill gene only: measured on held-out skill sources, the document barrier flags 29% of
            # benign skills (a skill IS instructions, so "instructions inside content" is its normal state); it is reported, not voted.
            ensure(self.skill_gene is not None, "skill_gene_inactive")
            return self.screen_skill(text, barrier_info=bool(p.get("include_barrier_info")))
        if op == "forecast":
            import math as _m
            from forecast import forecast as _forecast
            y = p.get("series"); h = int(p.get("horizon", 1)); m = int(p.get("season", 1) or 1)
            ensure(isinstance(y, list) and 20 <= len(y) <= 5000 and all(isinstance(v, (int, float)) and not isinstance(v, bool) and _m.isfinite(v) for v in y), "bad_series")
            ensure(1 <= h <= 60 and 1 <= m <= 366, "bad_horizon_or_season")
            try: r = _forecast([float(v) for v in y], h, m, external=self.forecaster, n_val=int(p.get("validation_windows", 12)), step=p.get("step"))
            except ValueError as e: raise MemeError(str(e))
            key = str(p.get("key") or "")[:120]
            self.k.db.execute("CREATE TABLE IF NOT EXISTS forecast_memory(key TEXT, chosen TEXT, validation TEXT, h INTEGER, m INTEGER, n INTEGER, at REAL)")
            self.k.db.execute("INSERT INTO forecast_memory VALUES(?,?,?,?,?,?,?)", (key, r["chosen"], canon(r["validation_mase"]), h, m, len(y), time.time()))
            self.k.event("forecast_selected", {"key": key, "chosen": r["chosen"], "best_simple": r["best_simple"], "validation_mase": r["validation_mase"], "n": len(y), "h": h, "m": m})
            return {"ok": True, **r, "forecaster_gene": (self.forecaster.gene.status if self.forecaster else "off")}
        if op == "screen":
            ensure(self.barrier is not None, "barrier_not_loaded")
            text = p["text"]; ensure(isinstance(text, str) and len(text) <= 60000, "text_too_large")
            r = self.barrier.screen(text, mode=p.get("mode", "content"))
            self.k.event("barrier_screen", {"text_sha256": sha(text), "invasive": r["invasive"], "reasons": r["reasons"]})
            return {"ok": True, **r, "barrier": self.barrier.meta.get("version")}
        if op == "execute":
            res = self.agent.perform(p["goal"], p["input"], p.get("context")); return {"ok": res["status"] == "pending", **res}
        if op == "feedback":
            res = self.agent.perform(p["goal"], p["input"], p.get("context"))
            fb = self.agent.feedback(res["episode"], p["expected"])
            return {"ok": True, "output_before": res["output"], "correct": fb["correct"],
                    "learning": {k: v for k, v in (fb.get("learning") or {}).items() if k in ("ok", "meme", "generator", "reason")}}
        if op == "adopt":
            imp = self.k.import_bundle(p["bundle"]); c = p["contract"]
            cid = self.k.contract(c["goal"], c["train"], c["validation"], c.get("holdout", []), c.get("context"))
            from restrict import adopt_restricted
            gate = adopt_restricted(self.k, p["bundle"]["root"], cid)
            return {"ok": gate["accepted"], "imported": imp["imported"], "gate": {k: gate.get(k) for k in ("accepted", "reason", "train", "validation", "holdout")},
                    "meme": gate.get("meme"), "restriction": gate.get("restriction")}
        raise MemeError("unknown_op")

    def sync(self, limit=50):
        st, box = self.hub.call("GET", f"/v1/mailbox/{self.hub.node}?after={self.cursor()}&limit={limit}")
        if st != 200: return {"ok": False, "http": st}
        done = []
        for m in box.get("messages", []):
            seq, mid = m["stream_seq"], m["message_id"]
            wire_ok = hashlib.sha256(canon(m.get("payload")).encode()).hexdigest() == m.get("payload_sha256")     # hash of what travelled
            m = {**m, "payload": js_restore(m.get("payload"))}
            if m["kind"] == "memeos-blob/v1" and self.net is not None and not self.k.db.execute("SELECT 1 FROM node_inbox WHERE message_id=?", (mid,)).fetchone():
                ok_sender = m["sender"] in ALLOWED or m["sender"] in self.net.members
                hash_ok = wire_ok
                st = "done"
                try:
                    if ok_sender and hash_ok and isinstance(m["payload"], dict) and "blob_sha256" in m["payload"]: self.net.on_blob_part(m["payload"])
                    else: st = "rejected"
                except Exception: st = "failed"
                self.k.db.execute("INSERT INTO node_inbox VALUES(?,?,?,?,?,?)", (mid, seq, m["sender"], st, None, time.time()))
                done.append({"seq": seq, "op": "blob_part", "status": st})
            if m["kind"] in ("mos-meme-offer/v1", "mos-meme-verdict/v1", "mos-meme-recall/v1") and self.net is not None and not self.k.db.execute("SELECT 1 FROM node_inbox WHERE message_id=?", (mid,)).fetchone():
                ok_sender = m["sender"] in ALLOWED or m["sender"] in self.net.members
                hash_ok = wire_ok
                try:
                    r = (self.net.on_offer(m["sender"], m["payload"]) if m["kind"] == "mos-meme-offer/v1" else
                         self.net.on_recall(m["sender"], m["payload"]) if m["kind"] == "mos-meme-recall/v1" else self.net.on_verdict(m["sender"], m["payload"])) \
                        if ok_sender and hash_ok else {"rejected": "sender_or_hash"}
                    status = "done"
                except Exception as e:
                    r, status = {"error": type(e).__name__}, "failed"
                self.hub.call("POST", f"/v1/messages/{mid}/receipts", {"status": "read", "detail": "memeos-net"})
                self.hub.call("POST", f"/v1/messages/{mid}/receipts", {"status": "completed" if status == "done" else "failed", "detail": ("memeos-net " + json.dumps(r, ensure_ascii=False))[:900]})
                self.k.db.execute("INSERT INTO node_inbox VALUES(?,?,?,?,?,?)", (mid, seq, m["sender"], status, None, time.time()))
                done.append({"seq": seq, "op": m["kind"], "status": status})
            if m["kind"] == "mos-meme-task/v1" and not self.k.db.execute("SELECT 1 FROM node_inbox WHERE message_id=?", (mid,)).fetchone():
                status, reply = "rejected", None
                member_report = self.net is not None and m["sender"] in self.net.members and \
                    isinstance(m["payload"], dict) and m["payload"].get("op") in ("report_miss", "report_false_alarm")   # peers may send observations only
                if m["sender"] not in ALLOWED and not member_report: reply = {"ok": False, "error": "sender_not_allowed"}
                elif not wire_ok: reply = {"ok": False, "error": "payload_hash_mismatch"}
                else:
                    self._sender = m["sender"]
                    try: reply = self.handle(m["payload"]); status = "done" if reply.get("ok") else "failed"
                    except (MemeError, KeyError, TypeError, ValueError) as e: reply = {"ok": False, "error": type(e).__name__ + ":" + str(e)[:200]}; status = "failed"
                reply = {"schema": "mos-meme-result/v1", "in_reply_to": mid, "executor": "memeos-node", "version": "2.24.1", **reply}
                rid = "MSG-memeos-" + sha({"r": mid})[:40]
                s, err = self.hub.send(m["sender"], "mos-meme-result/v1", reply, rid)
                if s == 409: status = "reply_already_exists"          # another instance answered earlier; never stall
                elif 400 <= s < 500:                                   # hub rejected this reply: report minimal failure, never stall
                    status = "reply_rejected"
                    s, _ = self.hub.send(m["sender"], "mos-meme-result/v1", {"schema": "mos-meme-result/v1", "in_reply_to": mid,
                                         "executor": "memeos-node", "ok": False, "error": "reply_rejected_by_hub:%s" % str(err)[:120]}, rid + "-f")
                    if s not in (200, 201, 409): s = 200       # give up on this message but advance the cursor
                if s in (200, 201, 409):
                    self.hub.call("POST", f"/v1/messages/{mid}/receipts", {"status": "read", "detail": "memeos-node"})
                    self.hub.call("POST", f"/v1/messages/{mid}/receipts", {"status": "completed" if status == "done" else "failed",
                                                                        "detail": f"memeos-node {status}; reply {rid}"})
                    self.k.db.execute("INSERT INTO node_inbox VALUES(?,?,?,?,?,?)", (mid, seq, m["sender"], status, rid, time.time()))
                    self.k.event("node_task", {"message": mid, "sender": m["sender"], "op": m["payload"].get("op"), "status": status})
                    done.append({"seq": seq, "op": m["payload"].get("op"), "status": status})
                else:
                    break                       # keep cursor; retry next sync
            self.k.db.execute("INSERT INTO node_meta VALUES('cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(seq),))
        return {"ok": True, "processed": done, "cursor": self.cursor()}

def _fetch_artifact(hub, seq, sha256, field):
    st, box = hub.call("GET", f"/v1/mailbox/{hub.node}?after={int(seq) - 1}&limit=1")
    m = box["messages"][0]; import base64, gzip
    raw = base64.b64decode(m["payload"][field]); ensure(m["stream_seq"] == int(seq) and hashlib.sha256(raw).hexdigest() == sha256, "artifact_hash_mismatch")
    return json.loads(gzip.decompress(raw))

def seed_skill_gene(nodes, raw):
    """The deployed v1 becomes gene meme #1 of every cell unless the cell already chose one itself."""
    from genememe import gene_card
    c = json.loads(gzip.decompress(raw))
    card = gene_card(raw, "skill_gene", c["name"], origin={"kind": "trained_offline", "by": "mos-claude", "recipe": "skillgene.py + evidence/skills_hashgene.py"},
                     evidence=c.get("evidence"), threshold=c.get("threshold"))
    for nd in nodes:
        nd.genes.put_blob(raw); gid = nd.genes.register(card)
        if not nd.genes.active("skill_gene"): nd.genes.activate(gid); nd.skill_gene = nd.genes.load(gid)

def seal(data, key):
    """Keystream = HMAC-SHA256(key, nonce || counter); returns nonce, ciphertext, and a MAC so tampering is detected."""
    import hmac
    nonce = secrets.token_hex(16); ks = b""; i = 0
    while len(ks) < len(data): ks += hmac.new(key.encode(), (nonce + ":%d" % i).encode(), hashlib.sha256).digest(); i += 1
    ct = bytes(a ^ b for a, b in zip(data, ks)).hex()
    return {"sealed": {"nonce": nonce, "ct": ct, "mac": hmac.new(key.encode(), (nonce + ct).encode(), hashlib.sha256).hexdigest(), "scheme": "hmac-sha256-stream/v1"}}

def unseal(sealed, key):
    import hmac
    ensure(hmac.compare_digest(hmac.new(key.encode(), (sealed["nonce"] + sealed["ct"]).encode(), hashlib.sha256).hexdigest(), sealed["mac"]), "seal_mac_mismatch")
    data = bytes.fromhex(sealed["ct"]); ks = b""; i = 0
    while len(ks) < len(data): ks += hmac.new(key.encode(), (sealed["nonce"] + ":%d" % i).encode(), hashlib.sha256).digest(); i += 1
    return bytes(a ^ b for a, b in zip(data, ks))

def restore_from_hub(nd):
    """Bring an empty cell back from the newest complete backup in its own mailbox (sent by itself, sha256 checked)."""
    import base64
    sets, after = {}, 0
    while True:
        st, box = nd.hub.call("GET", f"/v1/mailbox/{nd.hub.node}?after={after}&limit=200")
        ms = (box or {}).get("messages", []) if st == 200 else []
        if not ms: break
        for m in ms:
            after = max(after, m["stream_seq"]); p = m.get("payload") or {}
            if m["kind"] == "memeos-backup/v1" and m["sender"] == nd.hub.node and hashlib.sha256(canon(p).encode()).hexdigest() == m["payload_sha256"]:
                sets.setdefault(p["backup_id"], {})[int(p["part"])] = p
    for bid in sorted(sets, reverse=True):
        ps = sets[bid]; first = next(iter(ps.values()))
        if len(ps) != int(first["of"]): continue
        raw = b"".join(base64.b64decode(ps[i]["part_b64"]) for i in range(int(first["of"])))
        if hashlib.sha256(raw).hexdigest() != first["sha256"]: continue
        body = json.loads(gzip.decompress(raw)); t = body["tables"]; db = nd.k.db
        from memeos import meme_id
        from genememe import gene_id
        ensure(all(meme_id(json.loads(r[1])) == r[0] for r in t["memes"]), "backup_meme_hash_mismatch")
        ensure(all(gene_id(json.loads(r[3])) == r[0] for r in t.get("gene_memes", [])), "backup_gene_hash_mismatch")
        for h, b64 in body.get("gene_bodies", {}).items():
            data = base64.b64decode(b64); ensure(hashlib.sha256(data).hexdigest() == h, "backup_gene_body_mismatch"); nd.genes.put_blob(data)
        allowed = {"memes", "contracts", "active", "antimemes", "motifs", "foreign_memes", "usage", "net_genes", "net_gene_samples", "net_competence", "net_recalls",
                   "net_clean_extra", "forecast_memory", "gaps", "gene_memes", "gene_verdicts", "gene_oof", "lifecycle", "forage", "field_seen", "node_meta_cursor"}
        for name, rows in t.items():
            if name == "skill_evidence":                                     # explicit columns: the table may have grown since the backup
                for r in rows: db.execute("INSERT OR IGNORE INTO skill_evidence(sha256,label,source,text,at,by) VALUES(?,?,?,?,?,?)", r[:6])
                continue
            if name not in allowed: continue                                 # table names never come from the backup body (mos-grok, L2)
            if name == "node_meta_cursor":
                for k_, v in rows: db.execute("INSERT OR REPLACE INTO node_meta VALUES(?,?)", (k_, v))
                continue
            for r in rows:
                try: db.execute(f"INSERT OR IGNORE INTO {name} VALUES({','.join('?' * len(r))})", r)
                except Exception: pass
        act = nd.genes.active("skill_gene")
        if act: nd.skill_gene = nd.genes.load(act)
        nd.k.event("restored_from_hub_backup", {"backup_id": bid, "source_version": body["version"], "journal_head": body["journal"]["head"], "memes": len(t["memes"])})
        return {"ok": True, "backup_id": bid, "active_gene": act, "evidence": len(t.get("skill_evidence", []))}
    return {"ok": False, "error": "no_complete_backup"}

def seed_from_bundle(nodes, app_dir, source_cell="mos-claude"):
    """An independent cell joining the live network starts with the network's gene memes exactly as they are (same ids,
    same lineage), taken from the bundled snapshot and gene bodies; it then decides everything else on its own data."""
    snap = json.loads(gzip.decompress(open(os.path.join(app_dir, "snapshots", "cells.json.gz"), "rb").read()))[source_cell]
    from genememe import gene_id
    bodies = {}
    for f in os.listdir(os.path.join(app_dir, "genes", "skill")):
        raw = open(os.path.join(app_dir, "genes", "skill", f), "rb").read(); bodies[hashlib.sha256(raw).hexdigest()] = raw
    rows = snap["tables"].get("gene_memes", []); active = None
    for nd in nodes:
        if nd.genes.active("skill_gene"): continue
        for r in rows:
            card = json.loads(r[3]); ensure(gene_id(card) == r[0], "bundle_gene_hash_mismatch")
            if card["body_sha256"] not in bodies: continue
            nd.genes.put_blob(bodies[card["body_sha256"]]); nd.genes.register(card)
            if r[4] == "active": active = r[0]
        ensure(active, "no_active_gene_in_bundle"); nd.genes.activate(active); nd.skill_gene = nd.genes.load(active)
    return active

def _fetch_blob(hub, spec, as_bytes=False):
    """Multi-part artifact 'seq1,seq2,...:sha256' (hub messages are capped at 256 KB): parts are concatenated in order,
    the WHOLE is checked against sha256, then gunzipped JSON."""
    seqs, sha = spec.rsplit(":", 1); raw = b""; import base64, gzip
    for q in seqs.split(","):
        st, box = hub.call("GET", f"/v1/mailbox/{hub.node}?after={int(q) - 1}&limit=1")
        m = box["messages"][0]; ensure(m["stream_seq"] == int(q), "blob_part_missing"); raw += base64.b64decode(m["payload"]["part_b64"])
    ensure(hashlib.sha256(raw).hexdigest() == sha, "blob_hash_mismatch")
    return raw if as_bytes else json.loads(gzip.decompress(raw))

def _register_cells(main_hub, cells, token_dir):
    """One-time registration of extra cell identities via the hub's admin endpoint. Tokens stay on the volume."""
    admin = os.environ.get("MOS_HUB_ADMIN_TOKEN", "")
    os.makedirs(token_dir, exist_ok=True); out = {}
    for c in cells:
        path = os.path.join(token_dir, c + ".token")
        if os.path.exists(path): out[c] = open(path).read().strip(); continue
        if not admin: continue
        req = request.Request(main_hub.url + "/v1/nodes/register", method="POST", data=json.dumps({"node_id": c, "capabilities": {"transport": "memeos-cell"}}).encode(),
                              headers={"Authorization": "Bearer " + admin, "Content-Type": "application/json"})
        try:
            with request.urlopen(req, timeout=30) as r: tok = json.loads(r.read())["token"]
        except error.HTTPError as e:
            print(json.dumps({"message": f"cell registration failed {c}: {e.code}"}), flush=True); continue
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); os.write(fd, tok.encode()); os.close(fd); out[c] = tok
        print(json.dumps({"message": f"cell registered {c}"}), flush=True)
    return out

def _attach_genes(nodes, data_dir):
    """MEMEOS_GENES=1: install the pinned runtime (hash-locked wheels), fetch every gene card's files from its pinned
    HF revision into the volume (sha256-checked), and share ONE loaded gene list across all cells' barriers."""
    if os.environ.get("MEMEOS_GENES") != "1": return None
    import importlib, subprocess, sys
    here = os.path.dirname(os.path.abspath(__file__)); lib = os.environ.get("MEMEOS_GENES_LIB", "/tmp/memeos-genelib")
    try:
        import onnxruntime, tokenizers  # noqa: F401
    except ImportError:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--no-deps", "--require-hashes", "--only-binary=:all:", "--no-cache-dir",
                            "--disable-pip-version-check", "--target", lib, "-r", os.path.join(here, "requirements-genes.txt")], capture_output=True, text=True, timeout=900)
        if r.returncode:
            print(json.dumps({"message": "genes: runtime install failed", "stderr": r.stderr[-400:]}), flush=True); return None
        sys.path.insert(0, lib); importlib.invalidate_caches()
    import external_gene as eg
    cards_dir = os.path.join(here, "genes", "cards"); genes = []
    for f in sorted(os.listdir(cards_dir)) if os.path.isdir(cards_dir) else []:
        if not f.endswith(".json"): continue
        card = json.load(open(os.path.join(cards_dir, f)))
        root = os.environ.get("MEMEOS_EPHEMERAL_GENES", "/tmp/memeos-genes") if card.get("store") == "ephemeral" else os.path.join(data_dir, "genes")
        try: fetched = eg.fetch(card, root)
        except Exception as e: fetched = [("error", type(e).__name__)]
        g = eg.ExternalGene(card, root); genes.append(g)
        print(json.dumps({"message": f"gene {g.name}: {g.status}", "fetch": fetched, "source": card.get("source")}), flush=True)
    from forecast import Chronos2Gene
    fc = [g for g in genes if g.role == "forecaster"]
    for nd in nodes:
        if nd.barrier is not None: nd.barrier.attach_external([g for g in genes if g.role == "barrier"])
        if fc: nd.forecaster = Chronos2Gene(fc[0])
    return genes

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--once", action="store_true"); ap.add_argument("--loop", type=int, default=0)
    ap.add_argument("--db", default=os.environ.get("MEMEOS_DB", "memeos-node.sqlite"))
    a = ap.parse_args()
    if os.environ.get("MOS_NODE_TOKEN"):      # deployment: credentials from environment, never from files in the image
        cfg = {"hub_url": os.environ.get("MOS_HUB_URL", "https://mos-hub-live-production.up.railway.app"),
               "node_id": os.environ["MOS_NODE_ID"], "node_token": os.environ["MOS_NODE_TOKEN"]}
    elif os.environ.get("MOS_NODE_ID") and (os.environ.get("MOS_HUB_ADMIN_TOKEN") or
                                            os.path.exists(os.path.join(os.path.dirname(os.path.abspath(a.db)), "cells", os.environ["MOS_NODE_ID"] + ".token"))):
        # a cell on its own machine: registers its identity once with the hub admin token, keeps its token on the volume
        hub_url = os.environ.get("MOS_HUB_URL", "https://mos-hub-live-production.up.railway.app"); nid = os.environ["MOS_NODE_ID"]
        toks = _register_cells(Hub({"hub_url": hub_url, "node_id": "-", "node_token": "-"}), [nid], os.path.join(os.path.dirname(os.path.abspath(a.db)), "cells"))
        ensure(nid in toks, "self_registration_failed")
        cfg = {"hub_url": hub_url, "node_id": nid, "node_token": toks[nid]}
    else:
        cfg = json.load(open(os.environ.get("MOS_HUB_CONFIG", "mos_hub_config.json")))
    main_hub = Hub(cfg); nodes = [Node(main_hub, a.db)]
    cells = [c for c in os.environ.get("MEMEOS_CELLS", "").split(",") if c]
    data = _fetch_artifact(main_hub, os.environ["MEMEOS_DATA_SEQ"], os.environ["MEMEOS_DATA_SHA256"], "data_b64") if os.environ.get("MEMEOS_DATA_SEQ") else {}
    if cells:
        toks = _register_cells(main_hub, cells, os.path.join(os.path.dirname(os.path.abspath(a.db)), "cells"))
        for c in cells:
            if c in toks: nodes.append(Node(Hub({"hub_url": cfg["hub_url"], "node_id": c, "node_token": toks[c]}), os.path.join(os.path.dirname(os.path.abspath(a.db)), c + ".sqlite")))
    peers = json.loads(os.environ.get("MEMEOS_PEERS", "{}"))
    for nd in nodes:
        members = set(peers) | {x for v in peers.values() for x in v}
        if nd.hub.node in peers or data.get(nd.hub.node): nd.attach_network(peers.get(nd.hub.node, []), data.get(nd.hub.node, []), members)
    node = nodes[0]
    genes = _attach_genes(nodes, os.path.dirname(os.path.abspath(a.db)))
    if os.environ.get("MEMEOS_RESTORE_FROM_HUB") == "1":     # D7: an empty volume comes back from the cell's own backup on the hub
        for nd in nodes:
            if not nd.k.db.execute("SELECT 1 FROM gene_memes LIMIT 1").fetchone() and not nd.k.db.execute("SELECT 1 FROM skill_evidence LIMIT 1").fetchone():
                try: print(json.dumps({"message": f"{nd.hub.node} restore from hub backup", **restore_from_hub(nd)}), flush=True)
                except Exception as e: print(json.dumps({"message": f"{nd.hub.node} restore failed", "error": type(e).__name__ + ":" + str(e)[:120]}), flush=True)
    if os.environ.get("MEMEOS_SEED_BUNDLE"):          # an independent cell started from the portable archive
        try:
            print(json.dumps({"message": "seeded from bundle", "active_gene": seed_from_bundle(nodes, os.environ["MEMEOS_SEED_BUNDLE"])}), flush=True)
            evp = os.path.join(os.environ["MEMEOS_SEED_BUNDLE"], "data", "skill_evidence_newcell.json.gz")
            for nd in nodes:          # a new cell's first labelled evidence: a slice no other cell holds
                if os.path.exists(evp) and not nd.k.db.execute("SELECT 1 FROM skill_evidence LIMIT 1").fetchone():
                    nd.genes.add_evidence(json.loads(gzip.decompress(open(evp, "rb").read()))["items"], "seed:MaliciousSkillBench-newcell")
        except Exception as e: print(json.dumps({"message": "bundle seed failed", "error": type(e).__name__ + ":" + str(e)[:120]}), flush=True)
    if os.environ.get("MEMEOS_SKILL_GENE"):
        try:
            seed_skill_gene(nodes, _fetch_blob(main_hub, os.environ["MEMEOS_SKILL_GENE"], as_bytes=True))
        except Exception as e:
            print(json.dumps({"message": "skill gene: inactive", "error": type(e).__name__ + ":" + str(e)[:120]}), flush=True)
    if os.environ.get("MEMEOS_SKILL_EVIDENCE"):
        try:
            ev = json.loads(gzip.decompress(_fetch_blob(main_hub, os.environ["MEMEOS_SKILL_EVIDENCE"], as_bytes=True)))
            for nd in nodes: nd.genes.add_evidence(ev["cells"].get(nd.hub.node, []), "seed:MaliciousSkillBench-heldout")
        except Exception as e:
            print(json.dumps({"message": "skill evidence: not loaded", "error": type(e).__name__ + ":" + str(e)[:120]}), flush=True)
    for nd in nodes:
        act_gene = nd.genes.active("skill_gene")
        print(json.dumps({"message": f"{nd.hub.node} skill gene", "active": act_gene and nd.genes.card(act_gene)["name"], "evidence": dict(nd.k.db.execute("SELECT label, COUNT(*) FROM skill_evidence GROUP BY label"))}), flush=True)
    if os.environ.get("MEMEOS_SEARCH_CONFIG") and not node.k.db.execute(
            "SELECT 1 FROM active WHERE goal_key=?", (__import__("memeos").goal_key("mos.search_config", {}),)).fetchone():
        node.k.adopt_config(json.loads(os.environ["MEMEOS_SEARCH_CONFIG"]), {"source": "metaevolution.json (2026-11-18)"})
    print(json.dumps({"message": "memeos-node 2.24.1 started", "memeos_node": "2.24.1", "cells": [n.hub.node for n in nodes],
                      "network": {n.hub.node: (n.net.peers if n.net else None) for n in nodes}, "model_available": node.model.available,
                      "genes": [{"gene": g.name, "status": g.status} for g in genes] if genes is not None else "off"}), flush=True)
    if os.environ.get("MEMEOS_FORAGE"):
        from forage import Forager, share_of
        repos = [r.strip() for r in os.environ["MEMEOS_FORAGE"].split(",") if r.strip()]; world = sorted(set(peers) | {x for v in peers.values() for x in v}) or [node.hub.node]
        for nd in nodes:           # every cell meets its own disjoint part of the world
            nd.forager = Forager(nd, share_of(repos, nd.hub.node, world), per_run=3, max_files=30)
    forage_every = int(os.environ.get("MEMEOS_FORAGE_SECONDS", "10800")); last_forage = time.time() - forage_every + 120   # first run 2 min after start
    reflect_every = int(os.environ.get("MEMEOS_REFLECT_SECONDS", "600")); last_reflect = time.time()
    while True:
        if a.loop and time.time() - last_reflect >= reflect_every:
            last_reflect = time.time()
            for nd in nodes:
                try:
                    if time.time() - float((nd.k.db.execute("SELECT value FROM node_meta WHERE key='backup_at'").fetchone() or ["0"])[0]) > float(os.environ.get("MEMEOS_BACKUP_SECONDS", "86400")):
                        bk = nd.backup(); nd.k.db.execute("INSERT OR REPLACE INTO node_meta VALUES('backup_at', ?)", (str(time.time()),))
                        print(json.dumps({"message": f"{nd.hub.node} backup", **bk}), flush=True)
                except Exception: traceback.print_exc()
                try:
                    if nd.net is not None and nd.skill_gene is not None and nd.breeder.due() and nd.budget_take("breed_gene"):
                        br = nd.breeder.breed()      # enough new labelled evidence: grow the next gene version here
                        print(json.dumps({"message": f"{nd.hub.node} bred gene {br.get('name')}: adopted={br.get('adopted')} ({br.get('reason')})",
                                          **{k: br.get(k) for k in ("out_of_fold", "parent_on_same_items", "shared_to", "error")}}), flush=True)
                except Exception: traceback.print_exc()
                try:
                    pr = nd.drive.pursue()
                    if pr.get("done"): print(json.dumps({"message": f"{nd.hub.node} self-goals: " + ", ".join(f"{x['kind']}({x.get('goal')})={'ok' if x.get('ok') else 'no'}" for x in pr["done"]), **pr}, ensure_ascii=False), flush=True)
                except Exception: traceback.print_exc()
        if a.loop and time.time() - last_forage >= forage_every:
            last_forage = time.time()
            for nd in nodes:
                if not nd.forager: continue
                try:
                    fr = nd.forager.run_once(); print(json.dumps({"message": f"{nd.hub.node} forage: new={fr.get('new')} flagged={fr.get('flagged')} errors={fr.get('errors')}"}), flush=True)
                except Exception: traceback.print_exc()
        for nd in nodes:
            try:
                r = nd.sync()
                if r.get("processed") or not a.loop or int(time.time()) % 3600 < a.loop:
                    print(json.dumps({"message": f"{nd.hub.node} sync cursor={r.get('cursor')} processed={len(r.get('processed', []))}", **r}, ensure_ascii=False), flush=True)
            except Exception: traceback.print_exc()
        for g in genes or []:
            if g.release_if_idle(): print(json.dumps({"message": f"gene {g.name}: released after idle", "loads": g.loads}), flush=True)
        if not a.loop: break
        time.sleep(a.loop)
