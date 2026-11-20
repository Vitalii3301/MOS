"""Skill immunity: precise signatures of real attacks on AI-agent skills, made by the cells themselves.

Measured on real skills (yoonholee/agent-skill-malware: ClawHub campaign, Feb 2026; split by payload variant so the test
variants were never seen): the learned gene flags 61 of 69 real malicious skills but also 45 of 111 real benign ones;
a signature synthesized by MOS's own immune system (phrases shared by the attacks and absent from clean skills) flags
64 of 69 and 0 of 111, and 7 of 712 clean-world skills. So the two detectors get different jobs:

    a signature fires            -> malicious   (precise; says which signature)
    only the gene fires          -> suspicious  (broad; many false alarms on long technical skills)

A signature is a replicator like any other (life cycle kind "skill_antimeme"): a cell synthesizes it from the malicious
skills in its evidence that no signature catches yet, proves on its own clean skills (benign evidence + field set) that
it raises no false alarm, installs it, and offers it to its peers with a few examples. A receiver adopts it only if it
raises no false alarm on the receiver's own clean skills, catches the shipped examples, and the examples are attacks by
the receiver's own judgement. Tolerance: a signature that later fires on a skill labelled benign is dropped at once.
"""
import hashlib, json, math, re, time
from memeos import canon, sha
from immune import synthesize_gene, lab_evaluate, static_screen

SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_antimemes(name TEXT PRIMARY KEY, pattern TEXT NOT NULL, origin TEXT, offer_id TEXT, lab TEXT, at REAL);
CREATE TABLE IF NOT EXISTS skill_antimeme_drops(name TEXT, pattern TEXT, origin TEXT, why TEXT, at REAL);
CREATE TABLE IF NOT EXISTS reader_findings(sha256 TEXT PRIMARY KEY, text TEXT NOT NULL, quote TEXT, prompt TEXT, at REAL);
"""
CAP, MIN_NEW, CLEAN_FIELD = 200000, 5, 400

class SkillImmunity:
    def __init__(self, node):
        self.node = node; node.k.db.executescript(SCHEMA); self.rx = {}
        for name, pattern in node.k.db.execute("SELECT name, pattern FROM skill_antimemes"):
            try: self.rx[name] = re.compile(pattern, re.I | re.M)
            except re.error: pass

    def fires(self, text):
        t = text[:CAP]; return sorted(n for n, r in self.rx.items() if r.search(t))

    def clean(self):
        """This cell's own clean skills: benign evidence and the unlabelled field set it met in the world."""
        q = self.node.k.db.execute; out = [t for t, in q("SELECT text FROM skill_evidence WHERE label=0")]
        if q("SELECT 1 FROM sqlite_master WHERE name='field'").fetchone():
            out += [t for t, in q("SELECT text FROM field WHERE 'forage:'||repo||'/'||path NOT IN (SELECT COALESCE(source,'') FROM skill_evidence WHERE label=1) ORDER BY at DESC LIMIT ?", (CLEAN_FIELD,))]
        found = set(self.findings())
        return [t for t in out if t not in found] if found else out

    def find(self, text, quote, prompt):
        """Distillation, step 1: the reader (a language model) called a skill malicious and quoted the harmful line. This
        is not a human label and never enters the evidence that genes are judged on; it is raw material for a signature."""
        h = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
        if self.node.k.db.execute("INSERT OR IGNORE INTO reader_findings VALUES(?,?,?,?,?)", (h, text[:CAP], quote, prompt, time.time())).rowcount:
            self.node.k.event("reader_finding", {"sha256": h, "prompt": prompt, "quote": (quote or "")[:200]})

    def findings(self):
        """Reader findings a human label has not overruled (a benign label by a participant wins over the model)."""
        return [t for t, in self.node.k.db.execute("SELECT text FROM reader_findings WHERE sha256 NOT IN (SELECT sha256 FROM skill_evidence WHERE label=0)")]

    def malicious(self, world_only=False):
        """Attacks this cell knows. world_only: without the seed benchmark (synthetic attacks written for a test set share
        template wording, and a signature made from them is a signature of the benchmark: live, cell-d made one whose
        surviving phrase was "mit overview" - it matches nothing in 347 real skills)."""
        lab = [t for t, in self.node.k.db.execute("SELECT text FROM skill_evidence WHERE label=1" + (" AND COALESCE(source,'seed') NOT LIKE 'seed%'" if world_only else ""))]; have = set(lab)
        return lab + [t for t in self.findings() if t not in have]

    def adopt(self, name, pattern, origin, offer_id, lab):
        self.node.k.db.execute("INSERT OR REPLACE INTO skill_antimemes VALUES(?,?,?,?,?,?)", (name, pattern, origin, offer_id, canon(lab), time.time()))
        self.rx[name] = re.compile(pattern, re.I | re.M)
        self.node.k.event("skill_antimeme_adopted", {"name": name, "origin": origin, "offer": offer_id, "lab": lab})

    def drop(self, name, why):
        row = self.node.k.db.execute("SELECT pattern, origin FROM skill_antimemes WHERE name=?", (name,)).fetchone()
        if not row: return False
        self.node.k.db.execute("DELETE FROM skill_antimemes WHERE name=?", (name,)); self.rx.pop(name, None)
        self.node.k.db.execute("INSERT INTO skill_antimeme_drops VALUES(?,?,?,?,?)", (name, row[0], row[1], why, time.time()))
        self.node.k.event("skill_antimeme_dropped", {"name": name, "origin": row[1], "why": why}); return True

    @staticmethod
    def alternatives(pattern):
        """The phrases of a signature made by synthesize(): \\b(?:a|b|c). Anything else is one indivisible alternative."""
        if pattern.startswith(r"\b(?:") and pattern.endswith(")"): return pattern[len(r"\b(?:"):-1].split("|")
        return [pattern]
    @staticmethod
    def join(alts): return r"\b(?:" + "|".join(alts) + ")"

    def prune(self, bad_alts, why):
        """Cut phrases out of this cell's signatures (a peer saw them fire on its clean skills, or this cell did)."""
        changed = []
        for name, pattern, origin, oid, lab in self.node.k.db.execute("SELECT name, pattern, origin, offer_id, lab FROM skill_antimemes").fetchall():
            alts = self.alternatives(pattern); keep = [a for a in alts if a not in bad_alts]
            if len(keep) == len(alts): continue
            self.drop(name, why)
            if keep:
                new = self.join(keep); nn = "skill_sig_" + sha(new)[:10]
                self.adopt(nn, new, origin, oid, {**json.loads(lab or "{}"), "pruned_from": name, "why": why}); changed.append({"from": name, "to": nn, "phrases_left": len(keep)})
            else: changed.append({"from": name, "to": None})
        return changed

    def tolerate(self):
        """A signature that fires on a skill this cell knows to be benign is wrong here: drop it."""
        dropped = []
        if self.rx:
            for h, t in self.node.k.db.execute("SELECT sha256, text FROM skill_evidence WHERE label=0").fetchall():
                for n in self.fires(t):
                    if self.drop(n, "fires_on_benign_evidence:" + h[:12]): dropped.append(n)
        return dropped

    def _make(self, pos, clean, out):
        """Phrases shared by the attacks and absent from the clean skills -> (gene, lab) or (None, why)."""
        # a signature phrase must be shared by many of the attacks (a campaign), not by a handful: with low support the
        # synthesis also picks accidental phrases that alarm on clean skills elsewhere (rehearsal on real data: support 3 gave
        # 27 false alarms on 712 unseen clean skills, support 11-12 gave 0 while still catching 49 of 50 unseen real variants)
        support = max(MIN_NEW, math.ceil(0.1 * len(pos))); out["min_support"] = support
        gene = synthesize_gene(pos, clean, max_phrases=6, min_support=support)
        if not gene: return None, {**out, "action": "no_discriminative_signature"}
        lab = lab_evaluate(gene["pattern"], pos, clean, timeout=30)
        if lab.get("ok"):
            # precision first: keep only the phrases that, each tried alone in the lab, raise no alarm on any clean skill of
            # this cell and are shared by enough attacks
            alts = gene["pattern"][len(r"\b(?:"):-1].split("|"); keep = []
            for a, ph in zip(alts, gene["phrases"]):
                one = lab_evaluate(r"\b(?:" + a + ")", pos, clean, timeout=30)
                if one.get("ok") and one["false_alarms"] == 0 and one["hits"] >= support: keep.append((a, ph))
            out["phrases_removed_for_false_alarms"] = [ph for a, ph in zip(alts, gene["phrases"]) if (a, ph) not in keep]   # or for too little support
            if not keep: return None, {**out, "action": "no_phrase_without_false_alarms", "lab": lab}
            gene = {"pattern": r"\b(?:" + "|".join(a for a, _ in keep) + ")", "phrases": [ph for _, ph in keep]}
            lab = lab_evaluate(gene["pattern"], pos, clean, timeout=30)
        if not lab.get("ok") or lab["false_alarms"] > 0: return None, {**out, "action": "signature_failed_own_lab", "lab": lab}
        if lab["hits"] < MIN_NEW: return None, {**out, "action": "signature_catches_too_few", "lab": lab}
        return gene, lab

    def synthesize(self, max_hops=3):
        """New signature from the malicious skills in this cell's evidence that no signature catches yet."""
        node = self.node; pos = [t for t in self.malicious(world_only=True) if not self.fires(t)]; clean = self.clean()
        out = {"ok": True, "malicious_from_the_world": len(self.malicious(world_only=True)), "not_yet_caught": len(pos), "clean_skills": len(clean)}
        if len(pos) < MIN_NEW: return {**out, "action": "nothing_to_do"}
        if len(clean) < 30: return {**out, "action": "too_few_clean_skills_to_prove_precision"}
        gene, lab = self._make(pos, clean, out)
        if not gene: return lab
        name = "skill_sig_" + sha(gene["pattern"])[:10]; me = node.hub.node
        caught = [t for t in pos if re.search(gene["pattern"], t[:CAP], re.I | re.M)]
        examples = sorted(caught, key=len)[:6]                           # short ones: the signature itself is what matters, not the host skill
        offer = {"schema": "mos-meme-offer/v1", "offer_id": sha({"skill_antimeme": gene["pattern"], "origin": me}), "kind": "skill_antimeme", "origin": me,
                 "hops": 1, "max_hops": max_hops, "gene": {"name": name, "pattern": gene["pattern"], "examples": [e[:5000] for e in examples]},
                 "evidence": {"origin_lab": lab, "phrases": gene["phrases"]}}
        from lifecycle import KINDS, Refuse
        try: node.net.life.install(KINDS["skill_antimeme"], {"name": name, "pattern": gene["pattern"], "examples": offer["gene"]["examples"]}, offer, lab)
        except Refuse as e: return {**out, "action": "signature_failed_service", "reason": e.reason}
        node.k.db.execute("INSERT OR IGNORE INTO net_offers VALUES(?,?,?,?,?,?,?,?)", (offer["offer_id"], me, me, 0, 1, "own_synthesis", canon(lab), time.time()))
        sent = node.net.broadcast(offer)
        return {**out, "action": "signature_synthesized_installed_and_shared", "name": name, "phrases": gene["phrases"], "lab": lab,
                "shared_to": [x["to"] for x in sent if x.get("http") in (200, 201)]}

    def distill_preview(self):
        """What signature would this cell make from the reader's reading ALONE (no human labels, existing signatures
        ignored)? Attacks = skills the reader called malicious with a grounded quote; clean = skills it read as benign plus
        the field set. Nothing is installed: this measures how much of the model's judgement fits into a meme."""
        q = self.node.k.db.execute; pid, _ = self.node.reader.active(); pos, ben = list(self.findings()), []
        for t, in q("SELECT text FROM skill_evidence"):
            r = q("SELECT verdict FROM reader_cache WHERE prompt=? AND sha256=?", (pid, hashlib.sha256(t.encode("utf-8", "surrogatepass")).hexdigest())).fetchone()
            if r: (pos if json.loads(r[0])["verdict"] == "malicious" else ben).append(t)
        pos = list(dict.fromkeys(pos))
        fld = [t for t, in q("SELECT text FROM field ORDER BY at DESC LIMIT ?", (CLEAN_FIELD,))] if q("SELECT 1 FROM sqlite_master WHERE name='field'").fetchone() else []
        clean = ben + [t for t in fld if t not in set(pos)]
        out = {"ok": True, "prompt": pid and pid[:16], "reader_malicious": len(pos), "reader_benign": len(ben), "field": len(clean) - len(ben)}
        if len(pos) < MIN_NEW: return {**out, "action": "too_few_reader_findings"}
        gene, lab = self._make(pos, clean, out)
        if not gene: return lab
        return {**out, "action": "preview_only", "pattern": gene["pattern"], "phrases": gene["phrases"], "lab": lab, "installed_signatures": {n: r.pattern for n, r in self.rx.items()}}

    def status(self):
        q = self.node.k.db.execute
        return {"reader_findings": q("SELECT COUNT(*) FROM reader_findings").fetchone()[0],
                "signatures": [{"name": n, "origin": o, "lab": json.loads(l or "{}")} for n, o, l in q("SELECT name, origin, lab FROM skill_antimemes ORDER BY at")],
                "dropped": [{"name": n, "origin": o, "why": w} for n, o, w in q("SELECT name, origin, why FROM skill_antimeme_drops ORDER BY at")]}
