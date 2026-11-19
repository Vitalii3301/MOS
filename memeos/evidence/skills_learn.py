"""Learning on MaliciousSkillBench with MOS discipline. Selection only on train/validation (+ MOS's own clean world);
test (held-out sources) is report-only."""
import json, random, sys, numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.svm import LinearSVC
SP = sys.argv[1]; SPLIT = sys.argv[2] if len(sys.argv) > 2 else "source_disjoint"
recs = json.load(open(f"{SP}/hf2/{SPLIT}.json")); own = json.load(open(f"{SP}/skills_gh/own_benign.json"))
cap = lambda t: t[:60000]
tr = [r for r in recs if r["split"] == "train"]; va = [r for r in recs if r["split"] == "validation"]; te = [r for r in recs if r["split"] == "test"]
import hashlib
gate_repo = lambda o: int(hashlib.sha256(o.get("repo", o["path"].split("/")[0]).encode()).hexdigest(), 16) % 2 == 1   # split the clean world BY REPOSITORY
own_fit, own_gate = [o for o in own if not gate_repo(o)], [o for o in own if gate_repo(o)]
def metrics(pred, ys):
    pred, ys = np.array(pred, bool), np.array(ys, bool)
    tp = (pred & ys).sum(); fp = (pred & ~ys).sum(); fn = (~pred & ys).sum(); tn = (~pred & ~ys).sum()
    rec = tp / max(1, tp + fn); fpr = fp / max(1, fp + tn); prec = tp / max(1, tp + fp)
    f1p = 2 * prec * rec / max(1e-9, prec + rec); precn = tn / max(1, tn + fn); recn = tn / max(1, tn + fp)
    f1n = 2 * precn * recn / max(1e-9, precn + recn)
    return {"recall": round(float(rec), 3), "fpr": round(float(fpr), 3), "macro_f1": round(float((f1p + f1n) / 2), 3)}
out = {"split": SPLIT, "n": {"train": len(tr), "validation": len(va), "test": len(te), "own_benign_fit": len(own_fit), "own_benign_gate": len(own_gate)}}
yte = [r["label"] for r in te]
def run(name, extra_neg, model, gate_fpr=None):
    X = [cap(r["text"]) for r in tr] + [cap(o["text"]) for o in extra_neg]; y = [r["label"] for r in tr] + [0] * len(extra_neg)
    v = TfidfVectorizer(ngram_range=(1, 2), min_df=2, max_features=200000, sublinear_tf=True)
    M = v.fit_transform(X); clf = model().fit(M, y)
    sc = lambda rs: clf.decision_function(v.transform([cap(r["text"]) for r in rs]))
    thr = 0.0
    if gate_fpr is not None:     # MOS gate: threshold so that benign false alarms stay <= gate_fpr on validation benign AND on own clean world
        neg = np.concatenate([sc([r for r in va if r["label"] == 0]), sc(own_gate)])
        thr = float(np.quantile(neg, 1 - gate_fpr))
    s = sc(te); out[name] = {"threshold": round(thr, 4), "test": metrics(s > thr, yte),
                             "test_by_source": {src: metrics([x > thr for x, r in zip(s, te) if r["source"] == src], [r["label"] for r in te if r["source"] == src]) for src in sorted({r["source"] for r in te})}}
    print(name, out[name]["test"], flush=True)
    return v, clf, thr
run("paper_like_tfidf_svm", [], lambda: LinearSVC(C=1.0, class_weight="balanced"))
run("tfidf_lr", [], lambda: LogisticRegression(C=4.0, max_iter=3000, class_weight="balanced"))
run("tfidf_lr+own_clean_world", own_fit, lambda: LogisticRegression(C=4.0, max_iter=3000, class_weight="balanced"))
for g in (0.01, 0.05):
    run(f"mos_gate_fpr{g}", own_fit, lambda: LogisticRegression(C=4.0, max_iter=3000, class_weight="balanced"), gate_fpr=g)
json.dump(out, open(f"{SP}/eval/skills_learn_{SPLIT}.json", "w"), indent=1)
