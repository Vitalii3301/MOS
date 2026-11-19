"""Gene memes: learned detectors as first-class memes of MOS.

A gene meme is a card (schema mos-gene-meme/v1) with an id = sha256 of the card, a lineage (parents), an origin
(who made it and how), the maker's evidence, and the sha256 of its body (the weights blob, which can be larger than
one hub message and travels as parts). Every cell keeps its own registry and its own labelled evidence. A gene from
another cell is adopted only if, on the RECEIVER's evidence, it is significantly better than the gene the receiver
already uses (the registered rule, RULE_TEXT below; the same in every cell). The receiver's verdict, with its numbers, goes back
to the sender and into both journals; an adopted gene is forwarded to the receiver's peers, who decide for themselves.
"""
import base64, gzip, hashlib, json, math, time
from memeos import canon, ensure

SCHEMA = """
CREATE TABLE IF NOT EXISTS gene_memes(id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, card TEXT NOT NULL,
  status TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS blobs(sha256 TEXT PRIMARY KEY, data BLOB NOT NULL, at REAL);
CREATE TABLE IF NOT EXISTS blob_parts(sha256 TEXT, part INTEGER, of INTEGER, data BLOB, at REAL, PRIMARY KEY(sha256, part));
CREATE TABLE IF NOT EXISTS skill_evidence(sha256 TEXT PRIMARY KEY, label INTEGER NOT NULL, source TEXT, text TEXT NOT NULL, at REAL);
CREATE TABLE IF NOT EXISTS gene_judged(gene TEXT, sha256 TEXT, flagged INTEGER, PRIMARY KEY(gene, sha256));
CREATE TABLE IF NOT EXISTS gene_oof(gene TEXT, sha256 TEXT, flagged INTEGER, PRIMARY KEY(gene, sha256));
CREATE TABLE IF NOT EXISTS gene_verdicts(gene TEXT, cell TEXT, adopted INTEGER, reason TEXT, metrics TEXT, at REAL, PRIMARY KEY(gene, cell));
"""
PART = 150000
# The adoption rule is a registered text: its sha256 travels with every verdict and with the code message, so a peer can
# see which rule judged a gene. It changes only with a new id in a new version and applies only to decisions made after.
RULE_TEXT = """significant/v4 (in force from MemeOS 2.24)
A cell adopts a new gene over its current one iff ALL hold, on this cell's own data:
0. Units. Labelled items are grouped into template clusters (near-copies of one text: same words, other names/URLs;
   MinHash similarity >= 0.5 on normalised word 4-grams). A cluster votes once: for the new gene if it is right where the
   current is wrong on more of the cluster's items than the reverse, for the current gene in the opposite case.
   An item a gene was fitted on (same item identity, or a near-copy of a fitted item by the signatures in its card)
   counts only through an out-of-fold decision made when it was bred here; otherwise it is left out for both genes.
1. Significance: more clusters vote for the new gene than for the current one, exact two-sided sign test over the voting
   clusters, p <= alpha_n, where n is the number of candidate genes this cell has judged so far including this one and
   alpha_n = 0.05 * 6 / (pi^2 * n^2)   (the alphas of all attempts ever add up to at most 0.05).
2. False alarms, non-inferiority: benign clusters where only the new gene alarms, minus benign clusters where only the
   current one alarms, is at most floor(benign clusters / 50).
3. Its false-alarm rate on labelled benign items is <= 0.15, or not above the current gene's.
4. Field set (real skills this cell met in the world, unlabelled, at most the 400 most recent, labelled ones excluded):
   the new gene does not alarm where the current is silent more often than the reverse with p <= 0.05.
With fewer than 10 labelled items of either class the answer is no. A first gene needs only condition 3.
History: rule 1 (2.17-2.19) compared raw counts. Rule 2 (2.20) = sign test over items at 0.05 + "not significantly
worse"; introduced after the first bred child had been refused under rule 1 (forking-paths risk, recorded). Rule 3 (2.21)
added the field set. Rule 4 follows mos-grok's review of rule 3 (hub seq 2148): campaign clones made item-level tests
and out-of-fold estimates optimistic (H1), fitted items were matched by exact hash only (H2), repeated attempts at a fixed
alpha (M1), and "+4 false alarms, p=0.125" could pass (M2). Registered before any gene was bred or judged under it."""
RULE = {"id": "significant/v4", "sha256": hashlib.sha256(RULE_TEXT.encode()).hexdigest()}
ALPHA, FA_BUDGET, FIELD_MAX, SIG_K, SAME_TEMPLATE = 0.05, 0.15, 400, 24, 0.5

# ---- item identity and template signatures (mos-grok's review: clones of one campaign template are not independent items)
import re as _re
_URL = _re.compile(r"https?://\S+|\b\d{1,3}(?:\.\d{1,3}){3}\b"); _BLOB = _re.compile(r"[A-Za-z0-9+/=_-]{24,}"); _DIG = _re.compile(r"\d")
def norm_text(t):
    return " ".join(_DIG.sub("0", _BLOB.sub(" blob ", _URL.sub(" url ", t.lower()))).split())
def item_id(text):
    """The same skill has the same identity whether a cell keeps its first 5,000 characters or the whole text."""
    return hashlib.sha256(norm_text(text[:6000])[:3000].encode("utf-8", "surrogatepass")).hexdigest()
_P = (1 << 31) - 1          # signatures travel through the hub: every integer must be exact in JavaScript (< 2^53)
_AB = [(int.from_bytes(hashlib.blake2b(b"a%d" % i, digest_size=3).digest(), "big") | 1, int.from_bytes(hashlib.blake2b(b"b%d" % i, digest_size=3).digest(), "big")) for i in range(SIG_K)]
def signature(text):
    """MinHash over normalised word 4-grams of the first 8,000 characters."""
    w = norm_text(text[:8000]).split(); sh = {" ".join(w[i:i + 4]) for i in range(max(1, len(w) - 3))}
    xs = [int.from_bytes(hashlib.blake2b(x.encode("utf-8", "surrogatepass"), digest_size=7).digest(), "big") for x in sh] or [0]
    return [min((a * x + b) % _P for x in xs) for a, b in _AB]
def similar(s1, s2): return sum(a == b for a, b in zip(s1, s2)) / SIG_K >= SAME_TEMPLATE

def gene_card(body_raw, kind, name, parents=(), origin=None, evidence=None, threshold=None):
    return {"schema": "mos-gene-meme/v1", "kind": kind, "name": name, "parents": list(parents), "origin": origin or {},
            "evidence": evidence or {}, "threshold": threshold, "body_sha256": hashlib.sha256(body_raw).hexdigest(), "body_bytes": len(body_raw)}

def gene_id(card): return hashlib.sha256(canon(card).encode()).hexdigest()

class GeneMemes:
    def __init__(self, k):
        self.k = k; self.k.db.executescript(SCHEMA)
        if "by" not in [r[1] for r in k.db.execute("PRAGMA table_info(skill_evidence)")]:      # who labelled it (added in 2.20)
            k.db.execute("ALTER TABLE skill_evidence ADD COLUMN by TEXT")
        # rows written before 2.20 have no labeller: derive it from what the source says
        k.db.execute("UPDATE skill_evidence SET by='label:before-2.20' WHERE (by IS NULL OR by='seed') AND source LIKE 'forage:%'")
        k.db.execute("UPDATE skill_evidence SET by='import:'||substr(source,1,instr(source,'/')-1) WHERE by IS NULL AND source LIKE 'mos-%/%'")
        k.db.execute("UPDATE skill_evidence SET by='seed' WHERE by IS NULL")
        for col in ("item", "sig2"):
            if col not in [r[1] for r in k.db.execute("PRAGMA table_info(skill_evidence)")]: k.db.execute(f"ALTER TABLE skill_evidence ADD COLUMN {col} TEXT")
        self.cell = None                                   # set by the node: whose decisions count towards alpha spending

    # ---- storage
    def put_blob(self, raw):
        h = hashlib.sha256(raw).hexdigest()
        self.k.db.execute("INSERT OR IGNORE INTO blobs VALUES(?,?,?)", (h, raw, time.time())); return h
    def blob(self, h):
        r = self.k.db.execute("SELECT data FROM blobs WHERE sha256=?", (h,)).fetchone(); return r and bytes(r[0])
    def add_part(self, sha, part, of, data):
        self.k.db.execute("INSERT OR IGNORE INTO blob_parts VALUES(?,?,?,?,?)", (sha, int(part), int(of), data, time.time()))
        rows = self.k.db.execute("SELECT part, data FROM blob_parts WHERE sha256=? ORDER BY part", (sha,)).fetchall()
        if len(rows) == int(of):
            raw = b"".join(bytes(d) for _, d in rows)
            if hashlib.sha256(raw).hexdigest() == sha:
                self.put_blob(raw); self.k.db.execute("DELETE FROM blob_parts WHERE sha256=?", (sha,)); return True
            self.k.db.execute("DELETE FROM blob_parts WHERE sha256=?", (sha,)); self.k.event("blob_rejected", {"sha256": sha, "reason": "hash_mismatch"})
        return False

    def register(self, card, status="library"):
        ensure(card.get("schema") == "mos-gene-meme/v1", "not_gene_meme")
        raw = self.blob(card["body_sha256"]); ensure(raw is not None, "gene_body_missing")
        gid = gene_id(card)
        if not self.k.db.execute("SELECT 1 FROM gene_memes WHERE id=?", (gid,)).fetchone():
            self.k.db.execute("INSERT INTO gene_memes VALUES(?,?,?,?,?,?)", (gid, card["kind"], card["name"], canon(card), status, time.time()))
            self.k.event("gene_recorded", {"id": gid, "kind": card["kind"], "name": card["name"], "parents": card["parents"], "origin": card["origin"].get("kind")})
        return gid
    def card(self, gid):
        r = self.k.db.execute("SELECT card FROM gene_memes WHERE id=?", (gid,)).fetchone(); return r and json.loads(r[0])
    def active(self, kind):
        r = self.k.db.execute("SELECT id FROM gene_memes WHERE kind=? AND status='active'", (kind,)).fetchone(); return r and r[0]
    def activate(self, gid):
        kind = self.card(gid)["kind"]; prev = self.active(kind)
        self.k.db.execute("UPDATE gene_memes SET status='retired' WHERE kind=? AND status='active'", (kind,))
        self.k.db.execute("UPDATE gene_memes SET status='active' WHERE id=?", (gid,))
        self.k.event("gene_activated", {"id": gid, "kind": kind, "previous": prev}); return prev
    def load(self, gid):
        """The executable gene behind a card (skill genes only for now)."""
        c = self.card(gid); ensure(c["kind"] == "skill_gene", "unsupported_gene_kind")
        from skillgene import SkillGene
        return SkillGene(json.loads(gzip.decompress(self.blob(c["body_sha256"]))))
    def lineage(self, gid, depth=0):
        c = self.card(gid)
        if not c or depth > 20: return []
        st = self.k.db.execute("SELECT status FROM gene_memes WHERE id=?", (gid,)).fetchone()[0]
        out = [{"id": gid[:16], "name": c["name"], "status": st, "origin": c["origin"].get("kind"), "by": c["origin"].get("by")}]
        for p in c["parents"]: out += self.lineage(p, depth + 1)
        return out

    def revert(self, gid, prev):
        """Undo of activate (the life cycle rolls a gene back if it cannot serve)."""
        self.k.db.execute("UPDATE gene_memes SET status='library' WHERE id=?", (gid,))
        if prev: self.k.db.execute("UPDATE gene_memes SET status='active' WHERE id=?", (prev,))
        self.k.event("gene_reverted", {"id": gid, "restored": prev})

    # ---- evidence: every item has a source and a labeller; a gene is never judged on what it was fitted on
    def add_evidence(self, items, source="seed"):
        n = 0
        for e in items:
            h = hashlib.sha256(e["text"].encode("utf-8", "surrogatepass")).hexdigest()
            n += self.k.db.execute("INSERT OR IGNORE INTO skill_evidence(sha256,label,source,text,at,by) VALUES(?,?,?,?,?,?)",
                                   (h, int(e["label"]), e.get("source", source), e["text"], time.time(), source)).rowcount
        if n: self.k.event("skill_evidence_added", {"added": n, "source": source})
        return n
    def evidence_rows(self):
        return self.k.db.execute("SELECT sha256, label, text FROM skill_evidence ORDER BY sha256").fetchall()
    def idents(self):
        """{evidence sha: (item identity, template signature)}; computed once per item and kept."""
        db = self.k.db
        for h, t in db.execute("SELECT sha256, text FROM skill_evidence WHERE item IS NULL OR sig2 IS NULL").fetchall():
            db.execute("UPDATE skill_evidence SET item=?, sig2=? WHERE sha256=?", (item_id(t), json.dumps(signature(t)), h))
        return {h: (i, json.loads(sg)) for h, i, sg in db.execute("SELECT sha256, item, sig2 FROM skill_evidence")}
    def clusters(self):
        """{evidence sha: cluster id}: near-copies of one template (and exact same items) form one cluster."""
        ID = self.idents(); hs = sorted(ID); parent = {h: h for h in hs}
        def find(x):
            while parent[x] != x: parent[x] = parent[parent[x]]; x = parent[x]
            return x
        for a in range(len(hs)):
            ia, sa = ID[hs[a]]
            for b in range(a + 1, len(hs)):
                ib, sb = ID[hs[b]]
                if ia == ib or similar(sa, sb): parent[find(hs[b])] = find(hs[a])
        return {h: find(h) for h in hs}
    def fit_items(self, gid):
        o = (self.card(gid) or {}).get("origin") or {}
        return set(o.get("fit_items") or []), list(o.get("fit_sigs") or [])
    def fitted(self, gid):
        """Evidence items of this cell that this gene was fitted on: same sha, same item identity, or a near-copy."""
        fit, fsigs = self.fit_items(gid)
        if not (fit or fsigs): return set()
        ID = self.idents()
        return {h for h, (it, sg) in ID.items() if h in fit or it in fit or any(similar(sg, f) for f in fsigs)}
    def clean_base(self, gid):
        """The nearest ancestor (the gene itself included) that never saw any of this cell's evidence. A child must be
        grown from it: correcting a gene on the very items it was already fitted on would make every estimate optimistic."""
        seen = set()
        while gid and gid not in seen:
            seen.add(gid)
            if not self.fitted(gid): return gid
            gid = ((self.card(gid) or {}).get("parents") or [None])[0]
        return None
    def judgements(self, gid):
        """{evidence sha: flagged?} for every item this gene may honestly be judged on in this cell: an item it was fitted
        on counts only through its out-of-fold decision (stored when the gene was bred here); without one it is left out."""
        gene = self.load(gid); fit = self.fitted(gid); out = {}
        oof = dict(self.k.db.execute("SELECT sha256, flagged FROM gene_oof WHERE gene=?", (gid,)))
        for h, _, t in self.evidence_rows():
            if h in oof: out[h] = bool(oof[h])
            elif h not in fit: out[h] = bool(gene.judge(t)["malicious"])
        return out
    @staticmethod
    def _metrics(J, labels, items):
        """Counts as integers, rates as strings: these numbers travel through the hub, which is integers/strings only."""
        P = [h for h in items if labels[h] == 1]; N = [h for h in items if labels[h] == 0]
        tp = sum(J[h] for h in P); fp = sum(J[h] for h in N)
        return {"catches": tp, "malicious": len(P), "false_alarms": fp, "benign": len(N),
                "recall": "%.3f" % (tp / max(1, len(P))), "fpr": "%.3f" % (fp / max(1, len(N)))}
    def evaluate(self, gene):
        """A raw gene object on all of this cell's evidence (no fit bookkeeping)."""
        rows = self.evidence_rows(); labels = {h: l for h, l, _ in rows}
        return self._metrics({h: bool(gene.judge(t)["malicious"]) for h, _, t in rows}, labels, list(labels))
    def evaluate_id(self, gid):
        labels = {h: l for h, l, _ in self.evidence_rows()}; J = self.judgements(gid)
        return {**self._metrics(J, labels, list(J)), "left_out_as_fitted": len(labels) - len(J)}
    @staticmethod
    def _p2(b, c):
        """Exact two-sided sign test on b vs c discordant items."""
        n = b + c
        return 1.0 if n == 0 else min(1.0, 2 * sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2 ** n)
    @staticmethod
    def _pair(b, c):
        return {"only_new": b, "only_current": c, "p": "%.4g" % GeneMemes._p2(b, c)}
    def _paired(self, Jn, Jc, labels, items):
        """Per item: who alarms / catches where the other does not, overall and per source of the item."""
        src = dict(self.k.db.execute("SELECT sha256, source FROM skill_evidence")); out = {}; by = {}
        for lab, key in ((0, "false_alarms"), (1, "catches")):
            b = [h for h in items if labels[h] == lab and Jn[h] and not Jc[h]]; c = [h for h in items if labels[h] == lab and Jc[h] and not Jn[h]]
            out[key] = self._pair(len(b), len(c))
            for h, side in [(h, "only_new") for h in b] + [(h, "only_current") for h in c]:
                d = by.setdefault(str(src.get(h) or "?").split("/")[0][:40], {"false_alarms": {"only_new": 0, "only_current": 0}, "catches": {"only_new": 0, "only_current": 0}})
                d[key][side] += 1
        out["by_source"] = by
        return out
    def field_pair(self, gid, cur_id):
        """Condition 4: alarms of both genes on real skills this cell met in the world (unlabelled)."""
        db = self.k.db
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='field'").fetchone(): return None
        rows = db.execute("SELECT sha256, text FROM field WHERE 'forage:'||repo||'/'||path NOT IN (SELECT COALESCE(source,'') FROM skill_evidence) ORDER BY at DESC LIMIT ?", (FIELD_MAX,)).fetchall()
        if not rows: return None
        J = {}
        for g in (gid, cur_id):
            known = dict(db.execute("SELECT sha256, flagged FROM gene_judged WHERE gene=?", (g,))); gene = None; J[g] = {}
            for h, t in rows:
                if h not in known:
                    gene = gene or self.load(g); known[h] = int(gene.judge(t)["malicious"])
                    db.execute("INSERT OR REPLACE INTO gene_judged VALUES(?,?,?)", (g, h, known[h]))
                J[g][h] = bool(known[h])
        b = sum(1 for h, _ in rows if J[gid][h] and not J[cur_id][h]); c = sum(1 for h, _ in rows if J[cur_id][h] and not J[gid][h])
        return {"n": len(rows), "alarms_new": sum(J[gid].values()), "alarms_current": sum(J[cur_id].values()), **self._pair(b, c)}
    def decide(self, gid, against=None):
        """The registered rule (RULE_TEXT) against the currently active gene of the same kind
        (or against any other known gene: a read-only re-judgement, nothing changes)."""
        labels = {h: l for h, l, _ in self.evidence_rows()}; Jn = self.judgements(gid); cur_id = against or self.active(self.card(gid)["kind"])
        if cur_id == gid: return False, "already_active", {"new": self._metrics(Jn, labels, list(Jn))}
        Jc = self.judgements(cur_id) if cur_id else None
        items = [h for h in Jn if Jc is None or h in Jc]; new = self._metrics(Jn, labels, items); m = {"rule": RULE, "new": new, "left_out_as_fitted": len(labels) - len(items)}
        rate = lambda x: x["false_alarms"] / max(1, x["benign"])
        if new["malicious"] < 10 or new["benign"] < 10: return False, "too_little_own_evidence", m
        if not cur_id:
            ok = rate(new) <= FA_BUDGET
            return ok, "first_gene" if ok else "too_many_false_alarms_on_own_evidence", m
        cur = self._metrics(Jc, labels, items); pr = self._paired(Jn, Jc, labels, items)
        # cluster votes (rule 4, condition 0)
        C = self.clusters(); votes = {}
        for h in items:
            good_new, good_cur = Jn[h] == bool(labels[h]), Jc[h] == bool(labels[h]); v = votes.setdefault(C[h], {"new": 0, "cur": 0, "fa_new": 0, "fa_cur": 0, "benign": 0})
            v["new"] += good_new and not good_cur; v["cur"] += good_cur and not good_new
            if labels[h] == 0: v["benign"] = 1; v["fa_new"] += Jn[h] and not Jc[h]; v["fa_cur"] += Jc[h] and not Jn[h]
        right_new = sum(1 for v in votes.values() if v["new"] > v["cur"]); right_cur = sum(1 for v in votes.values() if v["cur"] > v["new"]); p = self._p2(right_new, right_cur)
        n = 1 + self.k.db.execute("SELECT COUNT(DISTINCT gene) FROM gene_verdicts WHERE cell IS ? AND gene != ?", (self.cell, gid)).fetchone()[0]
        alpha_n = ALPHA * 6 / (math.pi ** 2 * n * n); need = next(k for k in range(1, 200) if 2 / 2 ** k <= alpha_n)
        benign_clusters = sum(v["benign"] for v in votes.values()); margin = benign_clusters // 50
        extra_fa = sum(1 for v in votes.values() if v["fa_new"] > v["fa_cur"]) - sum(1 for v in votes.values() if v["fa_cur"] > v["fa_new"])
        r1 = ("refuse:more_false_alarms" if new["false_alarms"] > cur["false_alarms"] else "refuse:fewer_catches" if new["catches"] < cur["catches"] else
              "refuse:no_improvement" if (new["false_alarms"], new["catches"]) == (cur["false_alarms"], cur["catches"]) else "adopt")
        m.update(current=cur, current_id=cur_id, paired=pr, rule_1_would_say=r1, clusters={"items": len(items), "clusters": len(votes), "benign_clusters": benign_clusters},
                 overall={"unit": "template_cluster", "only_new_right": right_new, "only_current_right": right_cur, "p": "%.4g" % p, "attempt": n, "alpha": "%.4g" % alpha_n,
                          "needs_at_least_discordant_all_one_way": need},
                 false_alarm_margin={"extra_benign_clusters_alarmed": extra_fa, "allowed": margin})
        if rate(new) > max(FA_BUDGET, rate(cur)): return False, "false_alarm_budget_exceeded:%d/%d" % (new["false_alarms"], new["benign"]), m
        if extra_fa > margin: return False, "more_false_alarms_than_margin_on_own_evidence:%d>%d" % (extra_fa, margin), m
        if not (right_new > right_cur and p <= alpha_n): return False, "not_significantly_better_on_own_evidence:%d_vs_%d" % (right_new, right_cur), m
        f = self.field_pair(gid, cur_id); m["field"] = f
        if f and f["only_new"] > f["only_current"] and self._p2(f["only_new"], f["only_current"]) <= ALPHA:
            return False, "significantly_more_alarms_on_real_world_skills:%d_vs_%d" % (f["only_new"], f["only_current"]), m
        return True, "significantly_better_on_own_evidence", m

    # ---- transport helpers
    @staticmethod
    def parts(raw):
        return [raw[i:i + PART] for i in range(0, len(raw), PART)] or [b""]
