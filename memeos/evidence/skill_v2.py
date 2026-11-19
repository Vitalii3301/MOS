import json, sys, hashlib, random, gzip, numpy as np, scipy.sparse as sps
sys.path.insert(0, "/home/claude/memeos")
import skillgene as SG
from sklearn.linear_model import LogisticRegression
SP = sys.argv[1]
recs = json.load(open(f"{SP}/hf2/source_disjoint.json")); own = json.load(open(f"{SP}/skills_gh/own_benign.json"))
gate_repo = lambda o: int(hashlib.sha256(o["repo"].encode()).hexdigest(), 16) % 2 == 1
own_fit, own_gate = [o for o in own if not gate_repo(o)], [o for o in own if gate_repo(o)]
tr = [r for r in recs if r["split"] in ("train", "validation")]; te = [r for r in recs if r["split"] == "test"]
N = 1 << SG.BITS
def mat(texts):
    rows, cols, vals = [], [], []
    for i, t in enumerate(texts):
        for k, c in SG.features(t).items(): rows.append(i); cols.append(k); vals.append(c)
    return sps.csr_matrix((vals, (rows, cols)), shape=(len(texts), N), dtype=np.float64)
X0 = mat([r["text"] for r in tr] + [o["text"] for o in own_fit]); y = [r["label"] for r in tr] + [0] * len(own_fit)
df = np.bincount(X0.indices, minlength=N); idf = np.log((1 + X0.shape[0]) / (1 + df)) + 1.0
def tfidf(C, idf_):
    C = C.copy(); C.data = 1.0 + np.log(C.data); C = C @ sps.diags(idf_); nr = np.sqrt(C.multiply(C).sum(1)).A1; nr[nr == 0] = 1; return sps.diags(1 / nr) @ C
clf = LogisticRegression(C=4.0, max_iter=3000, class_weight="balanced").fit(tfidf(X0, idf), y)
w = clf.coef_[0]; b = float(clf.intercept_[0])
idf16 = np.array(SG.unpack(SG.pack(idf.tolist()), N)); w16 = np.array(SG.unpack(SG.pack(w.tolist()), N))
gate = tfidf(mat([o["text"] for o in own_gate]), idf16) @ w16 + b
thr = float(np.quantile(gate, 0.98))
v1 = json.loads(gzip.decompress(open("/home/claude/memeos/genes/skill/skill_screen_v1.json.gz", "rb").read()))
card = {"schema": "mos-skill-gene/v1", "name": "skill_screen_v2", "bits": SG.BITS, "bias": b, "threshold": round(thr, 4),
        "gate": {"benign_false_alarm_budget": 0.02, "on": "clean-world repositories not used for fitting (%d files)" % len(own_gate)},
        "trained_on": {"benchmark": "ProtectSkills/MaliciousSkillBench@d4b42ce5 (CC-BY-4.0), split source_disjoint/train+validation (%d)" % len(tr),
                       "clean_world": "%d SKILL.md files, fit half by repository" % len(own_fit)},
        "change_from_parent": "v1 held validation out for the threshold; v2 also learns from validation (+835 skills) and sets the threshold on held-out clean-world repositories only",
        "idf_f16": SG.pack(idf.tolist()), "w_f16": SG.pack(w.tolist())}
# evidence slices: held-out test sources, disjoint per cell, balanced; the rest is a report set
rng = random.Random(11); mal = [r for r in te if r["label"] == 1]; ben = [r for r in te if r["label"] == 0]; rng.shuffle(mal); rng.shuffle(ben)
cells = ["mos-claude", "memeos-cell-b", "memeos-cell-c"]; ev = {}
for i, c in enumerate(cells):
    ev[c] = [{"text": r["text"][:5000], "label": r["label"], "source": r["source"], "id": r["id"]} for r in mal[i * 60:(i + 1) * 60] + ben[i * 60:(i + 1) * 60]]
used = {e["id"] for v in ev.values() for e in v}; rest = [r for r in te if r["id"] not in used]
g1, g2 = SG.SkillGene(v1), SG.SkillGene(card)
def m(g, rs, cap=None):
    P = [r for r in rs if r["label"] == 1]; Nn = [r for r in rs if r["label"] == 0]
    f = lambda r: g.judge(r["text"][:cap] if cap else r["text"])["malicious"]
    return {"recall": round(sum(map(f, P)) / max(1, len(P)), 3), "fpr": round(sum(map(f, Nn)) / max(1, len(Nn)), 3), "n": [len(P), len(Nn)]}
out = {c: {"v1": m(g1, ev[c]), "v2": m(g2, ev[c])} for c in cells}; out["report_set_rest"] = {"v1": m(g1, rest), "v2": m(g2, rest)}
print(json.dumps(out, indent=1))
open(f"{SP}/eval/skill_screen_v2.json.gz", "wb").write(gzip.compress(json.dumps(card, separators=(",", ":")).encode(), 9, mtime=0))
open(f"{SP}/eval/skill_evidence.json.gz", "wb").write(gzip.compress(json.dumps({"schema": "mos-skill-evidence/v1",
    "source": "MaliciousSkillBench@d4b42ce5 source_disjoint TEST split (held-out sources SRC009/011/012), never used to train or threshold v1/v2; first 5000 chars; CC-BY-4.0 ProtectSkills",
    "cells": {c: [{k: e[k] for k in ("text", "label", "source")} for e in ev[c]] for c in cells}}, ensure_ascii=False).encode(), 9, mtime=0))
json.dump(out, open(f"{SP}/eval/skill_v2_eval.json", "w"), indent=1)
