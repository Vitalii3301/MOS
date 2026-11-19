"""Breeding: a cell grows the next version of its skill gene from its own labelled evidence, without anyone's help.

This closes the loop that was open before 2.20 (genes were trained outside MOS and carried in by hand):

    labelled evidence in the cell (seed, forage labels, other AIs' labels)
      -> child = parent + a small sparse correction fitted on that evidence
      -> judged out-of-fold: every item is scored by a child that never saw it (K folds)
      -> the same adoption rule as for any foreign gene (not more false alarms, not fewer catches, better in one)
      -> the life cycle installs it (activate + serve once, rollback on failure) and offers it to the peers,
         who judge it on THEIR evidence, leaving out any item the child was fitted on.

The recipe is fixed here, before any run, and is the same in every cell. Standard library only.
"""
import gzip, hashlib, json, math, random, time
from memeos import sha
from genememe import gene_card, gene_id, RULE
import skillgene as SG

RECIPE = {"name": "sparse_delta_logistic/v2", "folds": 5, "folds_by": "template cluster", "epochs": 12, "lr": 0.5, "l2": 0.02, "class_weight": "balanced",
          "threshold": "parent's (not re-fitted: too little data)", "min_per_class": 20}

def fit_delta(base, X, y, recipe=RECIPE):
    """Sparse correction d to the parent's weights: logistic loss on z = (parent score - threshold) + d.x, L2 pull to 0."""
    n = len(y); pos = sum(y); cw = {1: n / (2.0 * max(1, pos)), 0: n / (2.0 * max(1, n - pos))}
    d = {}; lr, l2 = recipe["lr"], recipe["l2"]; order = list(range(n)); rng = random.Random(0)
    for _ in range(recipe["epochs"]):
        rng.shuffle(order)
        for i in order:
            x = X[i]; z = base[i] + sum(v * d.get(k, 0.0) for k, v in x.items())
            g = (1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z)))) - y[i]) * cw[y[i]]
            for k, v in x.items(): d[k] = d.get(k, 0.0) * (1.0 - lr * l2) - lr * g * v
    return d

def fold_of(h, k): return int(h[:8], 16) % k

class Breeder:
    def __init__(self, node, recipe=RECIPE):
        self.node, self.recipe = node, dict(recipe)

    def _meta(self, key, value=None):
        db = self.node.k.db
        if value is None:
            r = db.execute("SELECT value FROM node_meta WHERE key=?", (key,)).fetchone(); return r and r[0]
        db.execute("INSERT INTO node_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def due(self, min_new=5):
        """Breed when enough NEW labelled evidence has arrived since the last attempt."""
        n = self.node.k.db.execute("SELECT COUNT(*) FROM skill_evidence").fetchone()[0]; last = self._meta("bred_at_count")
        if last is None: self._meta("bred_at_count", n); return False        # first sight: start counting from here
        return n - int(last) >= min_new

    def breed(self, share=True):
        node, gm, R = self.node, self.node.genes, self.recipe; t0 = time.time()
        parent_id = gm.active("skill_gene")
        if not parent_id: return {"ok": False, "error": "no_parent_gene"}
        rows = gm.evidence_rows(); y = [l for _, l, _ in rows]; H = [h for h, _, _ in rows]
        self._meta("bred_at_count", len(rows))
        if min(sum(y), len(y) - sum(y)) < R["min_per_class"]: return {"ok": False, "error": "too_little_evidence", "evidence": {"malicious": sum(y), "benign": len(y) - sum(y)}}
        # the child is grown from the nearest ancestor that never saw this cell's evidence, on ALL of the evidence:
        # stacking corrections on a gene already fitted on these items would make the out-of-fold numbers optimistic
        base_id = gm.clean_base(parent_id)
        if not base_id: return {"ok": False, "error": "no_ancestor_unfitted_on_this_cells_evidence"}
        parent = gm.load(base_id)
        X = [parent.vector(t) for _, _, t in rows]
        base = [sum(v * parent.w[k] for k, v in x.items()) + parent.b - parent.threshold for x in X]
        # out-of-fold: each item is judged by a child fitted without it
        # folds by template cluster: near-copies of one text always share a fold, so no fold model has seen a clone of
        # what it is asked to judge (mos-grok, review H1: 84 real malicious skills were 11 payload templates)
        K = R["folds"]; oof = {}; CL = gm.clusters(); ID = gm.idents()
        for f in range(K):
            tr = [i for i in range(len(H)) if fold_of(CL[H[i]], K) != f]; te = [i for i in range(len(H)) if fold_of(CL[H[i]], K) == f]
            if not te: continue
            d = fit_delta([base[i] for i in tr], [X[i] for i in tr], [y[i] for i in tr], R)
            for i in te: oof[H[i]] = base[i] + sum(v * d.get(k, 0.0) for k, v in X[i].items()) > 0
        # the child itself: fitted on all of the cell's evidence
        d = fit_delta(base, X, y, R); w = list(parent.w)
        for k, v in d.items(): w[k] += v
        pc = parent.card; root = pc["name"].split("+")[0]
        gen = 1 + sum(1 for c, in node.k.db.execute("SELECT card FROM gene_memes") if (json.loads(c).get("origin") or {}).get("by") == node.hub.node)
        body = {"schema": "mos-skill-gene/v1", "name": "%s+%s.g%d" % (root, node.hub.node.replace("memeos-", ""), gen), "bits": SG.BITS, "bias": parent.b,
                "threshold": parent.threshold, "gate": pc.get("gate"), "trained_on": pc.get("trained_on"),
                "bred": {"generation": gen, "by": node.hub.node, "base": pc["name"], "recipe": R, "fitted_items": len(H), "changed_weights": len(d)},
                "change_from_parent": "sparse correction to %s fitted in the cell on its own labelled evidence (%d items)" % (pc["name"], len(H)),
                "idf_f16": pc["idf_f16"], "w_f16": SG.pack(w)}
        raw = gzip.compress(json.dumps(body, sort_keys=True).encode(), 9, mtime=0)
        labels = dict(zip(H, y)); Jp = gm.judgements(parent_id); items = [h for h in H if h in Jp]
        oof_metrics = gm._metrics(oof, labels, items)
        card = gene_card(raw, "skill_gene", body["name"], parents=[base_id],
                         origin={"kind": "bred_in_cell", "by": node.hub.node, "recipe": R["name"], "born_under_rule": RULE["id"], "replaces": parent_id,
                                 "fit_items": sorted({ID[h][0] for h in H}), "fit_sigs": [ID[h][1] for h in H]},
                         evidence={"out_of_fold_on_breeder": oof_metrics, "parent_on_same_items": gm._metrics(Jp, labels, items),
                                   "template_clusters": len(set(CL[h] for h in H)),
                                   "note": "out-of-fold numbers estimate the recipe (5 fold models, folds by template cluster); the gene itself is "
                                           "fitted on all items. The real test is the peers' judgement on items it was not fitted on."}, threshold=parent.threshold)
        gid = gene_id(card); gm.put_blob(raw)
        node.k.db.executemany("INSERT OR REPLACE INTO gene_oof VALUES(?,?,?)", [(gid, h, int(v)) for h, v in oof.items()])
        # from here on it is an ordinary replicator: same life cycle as a gene that came from a neighbour
        offer = {"schema": "mos-meme-offer/v1", "offer_id": sha({"gene": gid, "born_under_rule": RULE}), "kind": "gene", "origin": node.hub.node, "hops": 0, "max_hops": 3, "card": card}
        r = node.net.life.consider(node.hub.node, offer, reply=False) if node.net is not None else {"adopted": False, "reason": "no_network"}
        out = {"ok": True, "gene": gid, "name": body["name"], "parent": parent_id, "grown_from": pc["name"], "adopted": bool(r.get("adopted")), "reason": r.get("reason") or ("already_considered" if r.get("duplicate") else None),
               "out_of_fold": oof_metrics, "parent_on_same_items": card["evidence"]["parent_on_same_items"], "paired": (r.get("lab") or {}).get("paired"),
               "changed_weights": len(d), "seconds": round(time.time() - t0, 1), "trail": r.get("trail")}
        sent = (node.net.share_gene(gid) if r.get("adopted") and share and not r.get("forwarded") else r.get("forwarded")) or []
        out["shared_to"] = [x["to"] for x in sent if x.get("http") in (200, 201)]; out["share_failed"] = [x for x in sent if x.get("http") not in (200, 201)]
        node.k.event("gene_bred", {k: out[k] for k in ("gene", "name", "parent", "adopted", "reason", "out_of_fold", "changed_weights", "seconds")})
        return out
