"""One life cycle for everything that replicates in MOS: skills, antimeme genes, learned gene memes.

Before 2.20 each kind had its own path through the network code and each could break in its own way (a gene passed
its gate and then failed every real request). Now every replicator, whoever made it, lives the same life:

    screen -> materialize -> evidence -> install (activate, then do the real job once; rolled back on failure)
           -> verdict to sender and origin -> forward if adopted

A kind supplies only what is specific to it (the Kind interface below). It cannot skip a stage. A stage that raises
is a refusal (fail closed). Every decision leaves a trail: which stages ran and where it stopped. Local births (a gene
bred in this cell, an antimeme synthesized here) go through the same `install`, so nothing becomes active in a cell
without having served once in the cell's real path.
"""
import json, math, time
from memeos import canon, sha, MemeError

SCHEMA = """
CREATE TABLE IF NOT EXISTS lifecycle(offer_id TEXT PRIMARY KEY, kind TEXT, ref TEXT, sender TEXT, origin TEXT, adopted INTEGER,
  reason TEXT, trail TEXT, at REAL);
"""

class Refuse(Exception):
    def __init__(self, reason, lab=None):
        super().__init__(reason); self.reason, self.lab = reason, lab or {}

class Kind:
    """What a kind of replicator must say about itself. `net` is the cell's Network; `offer` is the offer payload."""
    name = "?"
    def screen(self, net, sender, offer): return None            # reason to refuse before touching the body, or None
    def materialize(self, net, offer): raise NotImplementedError  # -> candidate (raise Refuse to stop)
    def evidence(self, net, cand, offer): raise NotImplementedError   # -> (ok, reason, lab): judged on THIS cell's evidence
    def activate(self, net, cand, offer, lab): raise NotImplementedError  # -> undo() restoring the previous state
    def serve(self, net, cand, offer): raise NotImplementedError  # do the real job once through the cell's real path; raise if broken
    def ref(self, cand): return None                             # stable id of the candidate
    def forward(self, net, cand, offer, exclude): return []
    def record(self, net, cand, adopted, reason, lab): pass      # kind-specific bookkeeping of the verdict

# ------------------------------------------------------------------ learned gene memes (skill-screening gene)
class GeneKind(Kind):
    name = "gene"
    def screen(self, net, sender, offer):
        card = offer.get("card") or {}
        if card.get("schema") != "mos-gene-meme/v1": return "not_gene_meme"
        if net.node.genes.blob(card.get("body_sha256", "")) is None: return "gene_body_missing_or_corrupt"
    def materialize(self, net, offer):
        gm = net.node.genes; gid = gm.register(offer["card"])
        try: gm.load(gid)
        except Exception as e: raise Refuse("gene_failed_to_load:" + type(e).__name__, {"gene": gid})
        return gid
    def evidence(self, net, gid, offer):
        ok, reason, m = net.node.genes.decide(gid)
        return ok, reason, {**m, "gene": gid}
    def activate(self, net, gid, offer, lab):
        gm, node = net.node.genes, net.node; prev = gm.activate(gid); node.skill_gene = gm.load(gid)
        def undo():
            gm.revert(gid, prev); node.skill_gene = gm.load(prev) if prev else None
        return undo
    def serve(self, net, gid, offer):
        row = net.node.k.db.execute("SELECT text FROM skill_evidence LIMIT 1").fetchone()
        r = net.node.screen_skill(row[0] if row else "---\nname: probe\n---\nSummarise the notes.", barrier_info=True, journal=False)
        if not (r.get("ok") and r.get("verdict") in ("malicious", "no_signal")): raise ValueError("bad_answer")
    def ref(self, gid): return gid
    def forward(self, net, gid, offer, exclude): return net.share_gene(gid, exclude=exclude)
    def record(self, net, gid, adopted, reason, lab):
        if gid: net.node.k.db.execute("INSERT OR REPLACE INTO gene_verdicts VALUES(?,?,?,?,?,?)", (gid, net.node.hub.node, int(adopted), reason, canon(lab), time.time()))

# ------------------------------------------------------------------ skills (MPL / code memes with their probes)
class SkillKind(Kind):
    name = "skill"
    def screen(self, net, sender, p):
        if net.node.barrier is not None:              # the offer is content: screen its human-readable fields
            txt = "\n".join(str(x) for x in [p.get("goal")] + [c.get("input") for c in p.get("probes", [])] + [c.get("output") for c in p.get("probes", [])])
            if net.node.barrier.decide(txt, "content")["invasive"]: return "barrier_blocked_offer"
    def materialize(self, net, p):
        try: net.node.k.import_bundle(p["bundle"])       # hash + per-card ids + local antimemes, never activates
        except MemeError as e: raise Refuse("bundle_rejected:" + str(e)[:80])
        return p["bundle"]["root"]
    def evidence(self, net, root, p):
        from memeos import evaluate
        k = net.node.k; card = k.card(root); pr = p.get("probes", [])
        if card.get("schema") == "mos-code-meme/v1":              # real code: re-executed only inside the sandbox
            from sandbox import evaluate_source
            ok, n, det = evaluate_source(card["source"], pr, require_isolation=True) if pr else (0, 0, None)   # foreign code: empty filesystem or nothing
            if det and det.get("error") == "no_filesystem_isolation_here": return False, "no_sandbox_isolation_on_this_host", {"isolation": det.get("isolation")}
        else:
            _, ok, n = evaluate(card["genome"], pr, k.resolve) if pr else (0, 0, 0)
        if not pr or ok < n: return False, "probe_reexecution_failed:%d/%d" % (ok, n), {"probes": [ok, n]}
        return True, "added_to_library_after_local_reexecution", {"probes": [ok, n]}
    def activate(self, net, root, p, lab):
        k = net.node.k; was = k.db.execute("SELECT status FROM memes WHERE id=?", (root,)).fetchone()[0]
        k.db.execute("UPDATE memes SET status='library' WHERE id=? AND status='candidate'", (root,))
        k.db.execute("INSERT OR REPLACE INTO net_competence VALUES(?,?,?,?,?)", (p["goal"], p.get("origin"), root, canon(p.get("gate")), time.time()))
        k.event("skill_library_added", {"goal": p["goal"], "from": p.get("origin"), "meme": root})
        def undo():
            k.db.execute("DELETE FROM net_competence WHERE goal=? AND meme=?", (p["goal"], root))
            k.db.execute("UPDATE memes SET status=? WHERE id=?", (was, root)); k.event("skill_library_removed", {"meme": root, "why": "failed_service"})
        return undo
    def serve(self, net, root, p):
        # the path a request takes (Node.solve): found in the library by goal, card readable, context comparable
        k = net.node.k; lib = [m for m, in k.db.execute("SELECT meme FROM net_competence WHERE goal=?", (p["goal"],))]
        if root not in lib: raise LookupError("not_in_library")
        card = k.card(root); card["context"]; card["goal"]
        if "genome" not in card and "source" not in card: raise ValueError("no_body")
    def ref(self, root): return root
    def forward(self, net, root, p, exclude): return net.broadcast({**p, "hops": int(p.get("hops", 1)) + 1}, exclude=exclude)

# ------------------------------------------------------------------ antimeme genes (regex genes of the barrier)
class AntimemeKind(Kind):
    name = "antimeme"
    def materialize(self, net, p):
        g = p.get("gene") or {}; ex = [e for e in (g.get("examples") or []) if isinstance(e, str)][:20]
        if not isinstance(g.get("pattern"), str): raise Refuse("lab_rejected:no_pattern")
        name = g.get("name") if isinstance(g.get("name"), str) and len(g.get("name", "")) <= 60 else "acquired_" + sha(g.get("pattern", ""))[:10]
        return {"name": name, "pattern": g["pattern"], "examples": ex}
    def evidence(self, net, c, p):
        from immune import lab_evaluate
        ex = c["examples"]; lab = lab_evaluate(c["pattern"], ex, net.clean); need = max(1, math.ceil(0.5 * len(ex)))
        if not lab.get("ok"): return False, "lab_rejected:" + lab["reason"], lab
        if lab["false_alarms"] > 0: return False, "false_alarms_on_own_clean_corpus:%d/%d" % (lab["false_alarms"], lab["clean"]), lab
        if lab["hits"] < need: return False, "does_not_catch_offered_examples:%d/%d" % (lab["hits"], lab["pos"]), lab
        by = net.bystander_check(c["pattern"], ex); lab = {**lab, "bystander": by}
        if by["bystander_hits"] > 0: return False, "matches_bystander_text:%d" % by["bystander_hits"], lab
        if by["confirmed_attacks_caught"] < need: return False, "examples_not_confirmed_as_attacks:%d/%d" % (by["confirmed_attacks_caught"], len(ex)), lab
        return True, "passed_own_lab", lab
    def activate(self, net, c, p, lab):
        db = net.node.k.db; prev = db.execute("SELECT * FROM net_genes WHERE name=?", (c["name"],)).fetchone()
        net.adopt(c["name"], c["pattern"], p.get("origin"), p.get("offer_id"), lab)
        def undo():
            db.execute("DELETE FROM net_genes WHERE name=?", (c["name"],))
            if net.node.barrier is not None: net.node.barrier.extra.pop(c["name"], None)
            if prev:
                db.execute("INSERT OR REPLACE INTO net_genes VALUES(?,?,?,?,?,?)", tuple(prev))
                if net.node.barrier is not None: net.node.barrier.add_gene(prev[0], prev[1])
            net.node.k.event("antimeme_removed", {"name": c["name"], "why": "failed_service"})
        return undo
    def serve(self, net, c, p):
        b = net.node.barrier
        if b is None: return                                   # a cell without a barrier stores the gene; nothing to serve with
        import re
        hit = [e for e in c["examples"] if re.search(c["pattern"], e, re.I | re.M)]
        if hit and not any(b.decide(e, "content")["invasive"] for e in hit): raise ValueError("gene_does_not_fire_through_barrier")
        b.decide("The meeting is moved to Thursday at ten.", "content")           # the barrier still answers at all
    def ref(self, c): return c["name"]
    def forward(self, net, c, p, exclude): return net.broadcast({**p, "hops": int(p.get("hops", 1)) + 1}, exclude=exclude)

KINDS = {k.name: k for k in (GeneKind(), SkillKind(), AntimemeKind())}

# ------------------------------------------------------------------ the engine
class Lifecycle:
    def __init__(self, net):
        self.net = net; net.node.k.db.executescript(SCHEMA)

    def install(self, kind, cand, offer, lab):
        """Activate, do the real job once, roll back if it breaks. The only way anything becomes active."""
        undo = kind.activate(self.net, cand, offer, lab)
        try: kind.serve(self.net, cand, offer)
        except Exception as e:
            undo(); raise Refuse("service_failed:" + type(e).__name__, lab)

    def judge(self, sender, offer):
        """Run the stages. Returns (kind, cand, adopted, reason, lab, trail)."""
        kind = KINDS.get(offer.get("kind") or "antimeme"); trail = []; cand = None; lab = {}
        if kind is None: return None, None, False, "unknown_kind", {}, [["screen", "unknown_kind"]]
        try:
            r = kind.screen(self.net, sender, offer)
            if r: raise Refuse(r)
            trail.append(["screen", "ok"])
            cand = kind.materialize(self.net, offer); trail.append(["materialize", "ok"])
            ok, reason, lab = kind.evidence(self.net, cand, offer)
            if not ok: raise Refuse(reason, lab)
            trail.append(["evidence", reason])
            self.install(kind, cand, offer, lab); trail.append(["install", "ok"])
            adopted = True
        except Refuse as e:
            adopted, reason, lab = False, e.reason, (e.lab or lab)
            trail.append([("screen", "materialize", "evidence", "install")[min(len(trail), 3)], e.reason])
        except Exception as e:                                   # a stage that crashes is a refusal, never an adoption
            adopted, reason = False, "%s_crashed:%s" % (("screen", "materialize", "evidence", "install")[min(len(trail), 3)], type(e).__name__)
            trail.append([reason.split("_crashed")[0], reason])
        kind.record(self.net, cand, adopted, reason, lab)
        return kind, cand, adopted, reason, lab, trail

    def consider(self, sender, offer, reply=True):
        """An offer from the network (or a local birth offered to oneself with reply=False)."""
        net, db = self.net, self.net.node.k.db; oid = offer.get("offer_id"); me = net.node.hub.node
        if not oid or db.execute("SELECT 1 FROM net_offers WHERE offer_id=?", (oid,)).fetchone(): return {"duplicate": True}
        kind, cand, adopted, reason, lab, trail = self.judge(sender, offer)
        hops = int(offer.get("hops", 1)); ref = kind.ref(cand) if kind and cand is not None else None; kname = kind.name if kind else str(offer.get("kind"))
        db.execute("INSERT OR IGNORE INTO net_offers VALUES(?,?,?,?,?,?,?,?)", (oid, sender, offer.get("origin"), hops, int(adopted), reason, canon(lab), time.time()))
        db.execute("INSERT OR REPLACE INTO lifecycle VALUES(?,?,?,?,?,?,?,?,?)", (oid, kname, ref, sender, offer.get("origin"), int(adopted), reason, canon(trail), time.time()))
        sent = 0
        if reply:
            verdict = {"schema": "mos-meme-verdict/v1", "offer_id": oid, "kind": kname, "ref": ref, "cell": me, "adopted": adopted, "reason": reason,
                       "lab": lab, "hops": hops, "trail": trail}
            if kname == "gene": verdict["gene"] = ref
            for to in sorted({sender, offer.get("origin")} - {None, me}):
                net._send(to, "mos-meme-verdict/v1", verdict, "MSG-verdict-" + sha({"o": oid, "c": me, "to": to})[:40]); sent += 1
        fwd = kind.forward(net, cand, offer, {sender, offer.get("origin")}) if adopted and hops < int(offer.get("max_hops", 3)) else []
        trail = trail + [["verdict", "sent:%d" % sent], ["forward", "to:%d" % len(fwd)]]
        db.execute("UPDATE lifecycle SET trail=? WHERE offer_id=?", (canon(trail), oid))
        net.node.k.event("offer_processed", {"offer": oid, "kind": kname, "sender": sender, "adopted": adopted, "reason": reason, "trail": trail, "forwarded": len(fwd)})
        return {"adopted": adopted, "reason": reason, "forwarded": fwd, "ref": ref, "lab": lab, "trail": trail}

    def status(self, limit=30):
        q = self.net.node.k.db.execute
        out = []
        for o, k, r, s, og, a, rs, t in q("SELECT offer_id,kind,ref,sender,origin,adopted,reason,trail FROM lifecycle ORDER BY at DESC LIMIT ?", (limit,)).fetchall():
            lab = json.loads((q("SELECT lab FROM net_offers WHERE offer_id=?", (o,)).fetchone() or ["{}"])[0] or "{}")
            out.append({"offer": o[:12], "kind": k, "ref": (r or "")[:16], "sender": s, "origin": og, "adopted": bool(a), "reason": rs, "trail": json.loads(t),
                        "numbers": {x: lab.get(x) for x in ("rule", "new", "current", "overall", "clusters", "false_alarm_margin", "paired", "field", "left_out_as_fitted", "rule_1_would_say") if x in lab}})
        return out
