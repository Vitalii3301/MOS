"""Skill-screening gene: a learned linear antimeme for agent skills (SKILL.md + scripts), pure-Python inference.

Agent skills are the real-world meme pool of AI agents: instruction files that are copied, mutated and installed
from host to host. This gene was learned by the mos-claude cell on MaliciousSkillBench (ProtectSkills, CC-BY-4.0,
arXiv 2608.19901) plus MOS's own clean world (SKILL.md files from widely used public repositories), with the
benchmark's source-disjoint protocol: chosen on train/validation only, held-out sources reported.

Featurisation (identical at training and inference, no vocabulary needed): lowercase word tokens (2+ chars,
sklearn's default pattern), unigrams + bigrams, feature hashing into 2**BITS buckets (blake2b), sublinear tf,
idf learned on training data, l2 normalisation. Score = w . x + b; the decision threshold is the MOS gate (benign
false-alarm budget on validation benign + held-out clean-world repositories).
"""
import base64, gzip, hashlib, json, math, re, struct

TOKEN = re.compile(r"(?u)\b\w\w+\b")
BITS = 17
CAP = 60000

def _h(s):
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8", "surrogatepass"), digest_size=4).digest(), "little") & ((1 << BITS) - 1)

def features(text):
    w = TOKEN.findall(text[:CAP].lower()); tf = {}
    for t in w: k = _h(t); tf[k] = tf.get(k, 0) + 1
    for a, b in zip(w, w[1:]): k = _h(a + " " + b); tf[k] = tf.get(k, 0) + 1
    return tf

def pack(arr):
    return base64.b64encode(gzip.compress(struct.pack("<%de" % len(arr), *arr), 9)).decode()

def unpack(s, n):
    return list(struct.unpack("<%de" % n, gzip.decompress(base64.b64decode(s))))

class SkillGene:
    def __init__(self, card):
        assert card["schema"] == "mos-skill-gene/v1" and card["bits"] == BITS
        n = 1 << BITS
        self.card, self.idf, self.w = card, unpack(card["idf_f16"], n), unpack(card["w_f16"], n)
        self.b, self.threshold = card["bias"], card["threshold"]
    def vector(self, text):
        """The normalised tf-idf vector the gene sees (sparse dict); the same at fitting and at inference."""
        x = {k: (1.0 + math.log(c)) * self.idf[k] for k, c in features(text).items()}
        nrm = math.sqrt(sum(v * v for v in x.values())) or 1.0
        return {k: v / nrm for k, v in x.items()}
    def score(self, text):
        return sum(v * self.w[k] for k, v in self.vector(text).items()) + self.b
    def judge(self, text):
        s = self.score(text)
        return {"score": round(s, 4), "malicious": s > self.threshold, "threshold": self.threshold}

def skill_report(gene, text):
    """The screen_skill answer. Shared with the gene gate, which runs it on a sample before a new gene becomes active
    (mos-grok, 2026-11-19: v2 passed the gate but every screen_skill on it failed with KeyError 'evidence')."""
    sg = gene.judge(text); sg.update(score_e4=int(round(sg["score"] * 10000)), threshold_e4=int(round(sg["threshold"] * 10000)))   # integers for peers
    reasons = ["skill_gene:%s" % gene.card.get("name", "?")] if sg["malicious"] else []
    ev = gene.card.get("evidence") or {"change_from_parent": gene.card.get("change_from_parent"), "trained_on": gene.card.get("trained_on")}
    return {"ok": True, "verdict": "malicious" if reasons else "no_signal", "reasons": reasons, "skill_gene": sg,
            "evidence": ev, "note": "no_signal is not a guarantee of safety"}

def screen(gene, text, barrier=None):
    """The whole screen_skill verdict, for cells (node.py) and for people (skillcheck.py): the gene decides
    malicious / no_signal; when it is silent and the barrier sees an open "drop your instructions" override, the answer is
    `suspicious` (measured before switching on: 19 of 1,981 clean-world skills, 0 of 259 benign and 1 of 341 malicious
    benchmark skills). Returns (report, barrier decision)."""
    out = skill_report(gene, text)
    d = barrier.decide(text[:200000], "content", use_external=False) if barrier else {"invasive": False, "reasons": []}
    ov = [r for r in d.get("reasons") or [] if r.startswith("antimeme:") and "override_" in r]
    if out["verdict"] == "no_signal" and ov:
        out["verdict"] = "suspicious"; out["reasons"] = ["barrier:" + r.split(":", 1)[1] for r in ov]
    return out, d
