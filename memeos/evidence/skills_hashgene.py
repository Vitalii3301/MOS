import json, sys, hashlib, numpy as np, scipy.sparse as sps
sys.path.insert(0, "/home/claude/memeos")
import skillgene as SG
from sklearn.linear_model import LogisticRegression
SP = sys.argv[1]; SPLIT = sys.argv[2]
recs = json.load(open(f"{SP}/hf2/{SPLIT}.json")); own = json.load(open(f"{SP}/skills_gh/own_benign.json"))
gate_repo = lambda o: int(hashlib.sha256(o.get("repo", o["path"].split("/")[0]).encode()).hexdigest(), 16) % 2 == 1
own_fit, own_gate = [o for o in own if not gate_repo(o)], [o for o in own if gate_repo(o)]
tr = [r for r in recs if r["split"] == "train"]; va = [r for r in recs if r["split"] == "validation"]; te = [r for r in recs if r["split"] == "test"]
N = 1 << SG.BITS
def mat(texts):
    rows, cols, vals = [], [], []
    for i, t in enumerate(texts):
        for k, c in SG.features(t).items(): rows.append(i); cols.append(k); vals.append(c)
    return sps.csr_matrix((vals, (rows, cols)), shape=(len(texts), N), dtype=np.float64)
Xtr_texts = [r["text"] for r in tr] + [o["text"] for o in own_fit]; y = [r["label"] for r in tr] + [0] * len(own_fit)
C = mat(Xtr_texts); df = np.bincount(C.indices, minlength=N); n = C.shape[0]
idf = np.log((1 + n) / (1 + df)) + 1.0
def tfidf(Cm):
    Cm = Cm.copy(); Cm.data = 1.0 + np.log(Cm.data); Cm = Cm @ sps.diags(idf)
    nr = np.sqrt(Cm.multiply(Cm).sum(1)).A1; nr[nr == 0] = 1; return sps.diags(1 / nr) @ Cm
X = tfidf(C); clf = LogisticRegression(C=4.0, max_iter=3000, class_weight="balanced").fit(X, y)
w = clf.coef_[0]; b = float(clf.intercept_[0])
# round-trip through the shipped float16 card so the evaluation is of the artifact that will actually run
idf16 = np.array(SG.unpack(SG.pack(idf.tolist()), N)); w16 = np.array(SG.unpack(SG.pack(w.tolist()), N))
def sc(texts):
    Cm = mat(texts); Cm.data = 1.0 + np.log(Cm.data); Cm = Cm @ sps.diags(idf16)
    nr = np.sqrt(Cm.multiply(Cm).sum(1)).A1; nr[nr == 0] = 1; return (sps.diags(1 / nr) @ Cm) @ w16 + b
neg = np.concatenate([sc([r["text"] for r in va if r["label"] == 0]), sc([o["text"] for o in own_gate])])
vpos = sc([r["text"] for r in va if r["label"] == 1]); s_te = sc([r["text"] for r in te]); yt = np.array([r["label"] for r in te], bool)
res = {}
for g in (0.01, 0.02, 0.05):
    thr = float(np.quantile(neg, 1 - g)); p = s_te > thr
    tp, fp, fn, tn = (p & yt).sum(), (p & ~yt).sum(), (~p & yt).sum(), (~p & ~yt).sum()
    rec, fpr = tp / (tp + fn), fp / (fp + tn); prec = tp / max(1, tp + fp); f1p = 2 * prec * rec / max(1e-9, prec + rec)
    pn, rn = tn / max(1, tn + fn), tn / max(1, tn + fp); f1n = 2 * pn * rn / max(1e-9, pn + rn)
    res[g] = {"threshold": round(thr, 4), "val_recall": round(float((vpos > thr).mean()), 3), "test_recall": round(float(rec), 3), "test_fpr": round(float(fpr), 3), "test_macro_f1": round(float((f1p + f1n) / 2), 3),
              "by_source": {s: {"recall": round(float(np.mean([x > thr for x, r in zip(s_te, te) if r["source"] == s and r["label"] == 1] or [0])), 3),
                                "fpr": round(float(np.mean([x > thr for x, r in zip(s_te, te) if r["source"] == s and r["label"] == 0] or [0])), 3)} for s in sorted({r["source"] for r in te})}}
    print(g, res[g], flush=True)
json.dump({"split": SPLIT, "res": res, "scores_test": [[r["id"], int(r["label"]), r["source"], float(x)] for r, x in zip(te, s_te)]}, open(f"{SP}/eval/skills_hashgene_{SPLIT}.json", "w"))
if SPLIT == "source_disjoint":
    g = 0.02
    card = {"schema": "mos-skill-gene/v1", "name": "skill_screen_v1", "bits": SG.BITS, "bias": b, "threshold": res[g]["threshold"],
            "gate": {"benign_false_alarm_budget": g, "on": "validation benign + clean-world repositories not used for fitting"},
            "trained_on": {"benchmark": "ProtectSkills/MaliciousSkillBench@d4b42ce5 (CC-BY-4.0), split source_disjoint/train",
                           "clean_world": "%d SKILL.md files from %d public repositories (fit half by repository)" % (len(own_fit), len({o.get('repo') for o in own_fit}))},
            "evidence": {k: v for k, v in res[g].items() if k != "threshold"}, "idf_f16": SG.pack(idf.tolist()), "w_f16": SG.pack(w.tolist())}
    json.dump(card, open(f"{SP}/eval/skill_screen_v1.json", "w"))
    print("card bytes", len(json.dumps(card)))
