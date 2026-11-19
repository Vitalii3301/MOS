"""MemeOS P2P network: memes and antimemes spread between cells; every receiver re-verifies on its OWN data.

kind mos-meme-offer/v1   {offer_id, kind:"antimeme", origin, hops, max_hops, gene:{name, pattern, examples[]}, evidence}
kind mos-meme-verdict/v1 {offer_id, cell, adopted, reason, lab}
A gene is adopted only if the receiver's sandboxed lab run shows ZERO false alarms on the receiver's own
clean corpus and it catches at least half of the offered examples. Adopted genes are forwarded to the
receiver's own peers (hop-limited); rejected genes stop there — selection, not trust, decides spread."""
import json, math, re, time
from memeos import sha, canon, MemeError, ensure
from immune import synthesize_gene, lab_evaluate

NET_SCHEMA = """
CREATE TABLE IF NOT EXISTS net_genes(name TEXT PRIMARY KEY, pattern TEXT NOT NULL, origin TEXT, offer_id TEXT, lab TEXT, at REAL);
CREATE TABLE IF NOT EXISTS net_offers(offer_id TEXT PRIMARY KEY, sender TEXT, origin TEXT, hops INTEGER, adopted INTEGER, reason TEXT, lab TEXT, at REAL);
CREATE TABLE IF NOT EXISTS net_gene_samples(name TEXT PRIMARY KEY, samples TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS net_clean_extra(id TEXT PRIMARY KEY, text TEXT NOT NULL, at REAL);
CREATE TABLE IF NOT EXISTS net_recalls(name TEXT, pattern TEXT, origin TEXT, evidence_sha TEXT, by TEXT, at REAL);
CREATE TABLE IF NOT EXISTS net_competence(goal TEXT, cell TEXT, meme TEXT, gate TEXT, at REAL, PRIMARY KEY(goal, cell, meme));
CREATE TABLE IF NOT EXISTS net_verdicts(offer_id TEXT, cell TEXT, adopted INTEGER, reason TEXT, lab TEXT, at REAL, PRIMARY KEY(offer_id, cell));
"""

class Network:
    def __init__(self, node, peers=(), clean=(), members=()):
        self.node, self.peers, self.clean = node, list(peers), list(clean)
        self.members = set(members) | set(self.peers)      # all cells of this network (may send offers)
        node.k.db.executescript(NET_SCHEMA)
        from lifecycle import Lifecycle
        self.life = Lifecycle(self)                         # one life cycle for skills, antimeme genes and gene memes
        for name, pattern in node.k.db.execute("SELECT name, pattern FROM net_genes"):
            if node.barrier is not None: node.barrier.add_gene(name, pattern)
        self.clean += [t for t, in node.k.db.execute("SELECT text FROM net_clean_extra")]     # what this cell learned to be clean

    def _send(self, to, kind, payload, mid):
        return self.node.hub.send(to, kind, payload, mid)

    def broadcast(self, offer, exclude=()):
        sent = []
        for p in self.peers:
            if p in exclude: continue
            s, _ = self._send(p, "mos-meme-offer/v1", offer, "MSG-offer-" + sha({"o": offer["offer_id"], "to": p, "h": offer["hops"]})[:40])
            sent.append({"to": p, "http": s})
        return sent

    def adopt(self, name, pattern, origin, offer_id, lab):
        self.node.k.db.execute("INSERT OR REPLACE INTO net_genes VALUES(?,?,?,?,?,?)", (name, pattern, origin, offer_id, canon(lab), time.time()))
        if self.node.barrier is not None: self.node.barrier.add_gene(name, pattern)
        self.node.k.event("antimeme_adopted", {"name": name, "origin": origin, "offer": offer_id, "lab": lab})

    # ---- local adaptive immunity: new attack samples -> synthesized gene -> own lab -> adopt -> share
    def immunize(self, samples, max_hops=3):
        ensure(self.node.barrier is not None, "barrier_not_loaded")
        missed = [s for s in samples if not self.node.barrier.decide(s, "content")["invasive"]]
        out = {"samples": len(samples), "already_caught": len(samples) - len(missed), "missed": len(missed)}
        if len(missed) < 2: return {**out, "action": "none_needed" if not missed else "need_more_examples"}
        gene = synthesize_gene(missed, self.clean)
        if not gene: return {**out, "action": "no_discriminative_gene"}
        lab = lab_evaluate(gene["pattern"], missed, self.clean)
        if not lab.get("ok") or lab["false_alarms"] > 0: return {**out, "action": "gene_failed_own_lab", "lab": lab}
        by = self.bystander_check(gene["pattern"], missed)
        if by["bystander_hits"] > 0: return {**out, "action": "gene_matches_bystander_text", "lab": {**lab, "bystander": by}}
        name = "acquired_" + sha(gene["pattern"])[:10]
        offer_id = sha({"gene": gene["pattern"], "origin": self.node.hub.node})
        offer = {"schema": "mos-meme-offer/v1", "offer_id": offer_id, "kind": "antimeme", "origin": self.node.hub.node, "hops": 1,
                 "max_hops": max_hops, "gene": {"name": name, "pattern": gene["pattern"], "examples": missed[:20]},
                 "evidence": {"origin_lab": lab, "phrases": gene["phrases"]}}
        from lifecycle import KINDS, Refuse
        try: self.life.install(KINDS["antimeme"], {"name": name, "pattern": gene["pattern"], "examples": missed[:20]}, offer, lab)   # activate + serve once
        except Refuse as e: return {**out, "action": "gene_failed_service", "reason": e.reason, "lab": lab}
        self.node.k.db.execute("INSERT OR REPLACE INTO net_gene_samples VALUES(?,?)", (name, canon(missed)))
        self.node.k.db.execute("INSERT OR IGNORE INTO net_offers VALUES(?,?,?,?,?,?,?,?)", (offer_id, self.node.hub.node, self.node.hub.node, 0, 1, "own_synthesis", canon(lab), time.time()))
        return {**out, "action": "gene_synthesized_adopted_and_shared", "gene": name, "pattern": gene["pattern"],
                "phrases": gene["phrases"], "lab": lab, "broadcast": self.broadcast(offer)}

    # ---- immune tolerance: an acquired gene that fires on clean text is recalled
    # ---- bystander test: a gene must match the ATTACK in its examples, not an innocent phrase next to it
    def _attack_like(self, sentence):
        b = self.node.barrier
        ext = [g for g in getattr(b, "external", []) if g.active]
        if ext: return max(g.score(sentence) for g in ext) >= 0.5           # independent judge (pinned external detector)
        return b.decide(sentence, "content")["content_p"] >= 0.1            # fallback: the barrier's own instruction head

    def bystander_check(self, pattern, examples):
        """Split every example into sentences; judge each. The gene must not fire on the example's NON-attack part,
        and only examples that contain an attack-like sentence count as caught."""
        rx = re.compile(pattern, re.I | re.M); confirmed = bystander = not_attack = 0
        for t in examples:
            if not rx.search(t): continue
            sents = [x.strip() for x in re.split(r"(?<=[.!?])\s+|\n+", t) if x.strip()]
            attack = [x for x in sents if self._attack_like(x)]
            if not attack: not_attack += 1; continue
            confirmed += 1
            if rx.search("\n".join(x for x in sents if x not in attack)): bystander += 1
        return {"confirmed_attacks_caught": confirmed, "bystander_hits": bystander, "examples_not_attacks": not_attack,
                "judge": "external" if any(g.active for g in getattr(self.node.barrier, "external", [])) else "content_head"}

    def _remember_clean(self, text):
        self.clean.append(text)
        self.node.k.db.execute("INSERT OR IGNORE INTO net_clean_extra VALUES(?,?,?)", (sha(text), text, time.time()))

    def _drop_gene(self, name, evidence, by):
        row = self.node.k.db.execute("SELECT pattern, origin FROM net_genes WHERE name=?", (name,)).fetchone()
        if not row: return None
        self.node.k.db.execute("DELETE FROM net_genes WHERE name=?", (name,))
        if self.node.barrier is not None: self.node.barrier.extra.pop(name, None)
        self.node.k.db.execute("INSERT INTO net_recalls VALUES(?,?,?,?,?,?)", (name, row[0], row[1], sha(evidence), by, time.time()))
        self.node.k.event("antimeme_recalled", {"name": name, "origin": row[1], "by": by, "evidence_sha256": sha(evidence)})
        return {"pattern": row[0], "origin": row[1]}

    def tolerance(self, text):
        """A clean text was flagged. Guilty ACQUIRED genes are dropped; the text joins this cell's clean corpus.
        If the gene is ours: recall it at every member (only an origin may recall) and derive a refined gene."""
        if self.node.barrier is None: return {"action": "no_barrier"}
        guilty = [nm for nm, rx in list(self.node.barrier.extra.items()) if rx.search(text)]
        self._remember_clean(text); out = []
        for nm in guilty:
            samples = self.node.k.db.execute("SELECT samples FROM net_gene_samples WHERE name=?", (nm,)).fetchone()
            g = self._drop_gene(nm, text, self.node.hub.node)
            item = {"gene": nm, "origin": g["origin"]}
            if g["origin"] == self.node.hub.node:
                msg = {"schema": "mos-meme-recall/v1", "name": nm, "pattern": g["pattern"], "evidence": text, "origin": self.node.hub.node}
                item["recall_sent_to"] = [m for m in sorted(self.members) if m != self.node.hub.node and
                                          self._send(m, "mos-meme-recall/v1", msg, "MSG-recall-" + sha({"n": nm, "to": m, "e": sha(text)})[:40])[0] in (200, 201)]
                if samples:
                    r = self.immunize(json.loads(samples[0]))          # re-derive against the enlarged clean corpus
                    item["refined"] = {k: r.get(k) for k in ("action", "gene", "phrases")}
            else:                                                       # not ours: tell the origin, it owns the recall
                s, _ = self._send(g["origin"], "mos-meme-task/v1", {"op": "report_false_alarm", "text": text}, "MSG-fa-" + sha({"n": nm, "e": sha(text), "c": self.node.hub.node})[:40])
                item["origin_notified"] = s in (200, 201)
            out.append(item)
        still = self.node.barrier.decide(text, "content")
        return {"action": "genes_recalled" if out else "no_acquired_gene_involved", "genes": out, "still_flagged": still["invasive"], "remaining_reasons": still["reasons"]}

    def on_recall(self, sender, p):
        nm = p.get("name"); row = self.node.k.db.execute("SELECT pattern, origin FROM net_genes WHERE name=?", (nm,)).fetchone()
        if not row: return {"recalled": False, "reason": "unknown_gene"}
        if sender != row[1]: return {"recalled": False, "reason": "only_origin_may_recall"}              # hub authenticates the sender
        if p.get("pattern") != row[0]: return {"recalled": False, "reason": "pattern_mismatch"}
        ev = p.get("evidence")
        lab = lab_evaluate(row[0], [ev] if isinstance(ev, str) else [], [])
        if not lab.get("ok") or lab["hits"] != 1: return {"recalled": False, "reason": "gene_does_not_fire_on_evidence"}
        self._drop_gene(nm, ev, sender); self._remember_clean(ev)
        return {"recalled": True, "reason": "origin_recall_verified"}

    # ---- skills: genes travel, not computation
    def share_skill(self, mid, gate, max_hops=3):
        k = self.node.k; card = k.card(mid); c = card["goal"]
        cs = k.db.execute("SELECT train FROM contracts WHERE goal=? AND context=? ORDER BY created DESC LIMIT 1", (c, canon(card["context"]))).fetchone()
        offer = {"schema": "mos-meme-offer/v1", "offer_id": sha({"skill": mid}), "kind": "skill", "origin": self.node.hub.node, "hops": 1,
                 "max_hops": max_hops, "goal": c, "context": card["context"], "bundle": k.export(mid),
                 "probes": json.loads(cs[0])[:6] if cs else [], "gate": gate}
        k.db.execute("INSERT OR IGNORE INTO net_offers VALUES(?,?,?,?,?,?,?,?)", (offer["offer_id"], self.node.hub.node, self.node.hub.node, 0, 1, "own_skill", canon(gate), time.time()))
        return self.broadcast(offer)

    # ---- receiving an offer from the network
    # ---- gene memes: body as blob parts first, then the offer; the receiver decides on its own evidence
    def share_gene(self, gid, max_hops=3, exclude=(), to=None):
        import base64
        gm = self.node.genes; card = gm.card(gid); raw = gm.blob(card["body_sha256"]); parts = gm.parts(raw); sent = []
        offer = {"schema": "mos-meme-offer/v1", "offer_id": sha({"gene": gid}), "kind": "gene", "origin": card["origin"].get("by") or self.node.hub.node,
                 "hops": 1, "max_hops": max_hops, "card": card}
        for peer in (to or self.peers):
            if peer in exclude or (peer not in self.members and peer not in self.peers): continue
            for i, pt in enumerate(parts):
                self._send(peer, "memeos-blob/v1", {"schema": "memeos-blob/v1", "blob_sha256": card["body_sha256"], "part": i, "of": len(parts),
                                                    "part_b64": base64.b64encode(pt).decode()}, "MSG-genepart-" + sha({"b": card["body_sha256"], "i": i, "to": peer})[:40])
            s, _ = self._send(peer, "mos-meme-offer/v1", offer, "MSG-offer-" + sha({"o": offer["offer_id"], "to": peer, "h": 1})[:40]); sent.append({"to": peer, "http": s})
        self.node.k.db.execute("INSERT OR IGNORE INTO net_offers VALUES(?,?,?,?,?,?,?,?)", (offer["offer_id"], self.node.hub.node, offer["origin"], 0, 1, "own_gene", canon({"gene": gid}), time.time()))
        return sent

    def on_gene(self, sender, p):
        """A gene handed over directly (operator import): same stages as an offer, no network bookkeeping."""
        _, _, ok, reason, lab, _ = self.life.judge(sender, {"kind": "gene", "card": p.get("card")})
        return ok, reason, lab

    def on_blob_part(self, p):
        import base64
        return self.node.genes.add_part(p["blob_sha256"], p["part"], p["of"], base64.b64decode(p["part_b64"]))

    def on_offer(self, sender, p):
        return self.life.consider(sender, p)

    def on_verdict(self, sender, p):
        self.node.k.db.execute("INSERT OR REPLACE INTO net_verdicts VALUES(?,?,?,?,?,?)", (p.get("offer_id"), sender, int(bool(p.get("adopted"))), p.get("reason"), canon(p.get("lab")), time.time()))
        if p.get("kind") == "gene" and p.get("gene"):
            self.node.genes.k.db.execute("INSERT OR REPLACE INTO gene_verdicts VALUES(?,?,?,?,?,?)", (p["gene"], p.get("cell") or sender, int(bool(p.get("adopted"))), p.get("reason"), canon(p.get("lab")), time.time()))
            self.node.k.event("gene_verdict_received", {"gene": p["gene"], "cell": p.get("cell") or sender, "adopted": bool(p.get("adopted")), "reason": p.get("reason")})
        return {"recorded": True}

    def status(self):
        q = self.node.k.db.execute
        lib = [{"goal": g, "from": c} for g, c in q("SELECT goal, cell FROM net_competence")]
        return {"cell": self.node.hub.node, "peers": self.peers, "clean_corpus": len(self.clean), "library": lib,
                "genes": [{"name": n, "origin": o} for n, o in q("SELECT name, origin FROM net_genes")],
                "recalls": [{"name": n, "origin": o, "by": b} for n, o, b in q("SELECT name, origin, by FROM net_recalls")],
                "offers": [{"offer": o[:12], "sender": s, "origin": og, "hops": h, "adopted": bool(a), "reason": r} for o, s, og, h, a, r in q("SELECT offer_id,sender,origin,hops,adopted,reason FROM net_offers")],
                "verdicts_received": [{"offer": o[:12], "cell": c, "adopted": bool(a), "reason": r} for o, c, a, r in q("SELECT offer_id,cell,adopted,reason FROM net_verdicts")]}
