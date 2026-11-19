"""Does the memetic hybrid beat its parts at an equal training budget? Real datasets, 5 splits each, test split used once."""
import json, sys, time
import numpy as np
from sklearn import datasets
from sklearn.linear_model import LogisticRegression
from neuro import memetic, gradient_only, evolution_only, accuracy

SETS = {"breast_cancer": datasets.load_breast_cancer, "wine": datasets.load_wine, "digits": datasets.load_digits}

def split(X, y, seed):
    rng = np.random.default_rng(seed); idx = rng.permutation(len(X)); n = len(X)
    tr, va, te = idx[:int(.6 * n)], idx[int(.6 * n):int(.8 * n)], idx[int(.8 * n):]
    m, s = X[tr].mean(0), X[tr].std(0) + 1e-9; Z = (X - m) / s
    return Z[tr], y[tr], Z[va], y[va], Z[te], y[te]

def run(seeds=range(20)):
    out = {}
    for name, load in SETS.items():
        d = load(); X, y = d.data.astype(float), d.target; k = len(set(y)); rows = []
        for s in seeds:
            Xtr, ytr, Xva, yva, Xte, yte = split(X, y, s)
            res = {"memetic": memetic(Xtr, ytr, Xva, yva, k, s), "gradient_only": gradient_only(Xtr, ytr, Xva, yva, k, s),
                   "evolution_only": evolution_only(Xtr, ytr, Xva, yva, k, s), "memetic_no_inheritance": memetic(Xtr, ytr, Xva, yva, k, s, lamarck=False)}
            row = {c: round(accuracy(r["W"], Xte, yte, r["g"]["act"]), 4) for c, r in res.items()}
            row["logistic_reference"] = round(float(LogisticRegression(max_iter=2000).fit(Xtr, ytr).score(Xte, yte)), 4)
            row["memetic_genome"] = res["memetic"]["g"]; row["memetic_is_linear"] = not res["memetic"]["g"]["hidden"]; rows.append(row)
            print(name, s, {k2: v for k2, v in row.items() if k2 != "memetic_genome"}, file=sys.stderr, flush=True)
        conds = [c for c in rows[0] if c not in ("memetic_genome", "memetic_is_linear")]
        out[name] = {"chose_linear_model": sum(r["memetic_is_linear"] for r in rows), "of": len(rows), "per_seed": rows, "mean_test_accuracy": {c: round(float(np.mean([r[c] for r in rows])), 4) for c in conds},
                     "std": {c: round(float(np.std([r[c] for r in rows])), 4) for c in conds}}
    pairs = [(r["memetic"], r["gradient_only"]) for n in SETS for r in out[n]["per_seed"]]
    out["memetic_vs_gradient_only"] = {"wins": sum(a > b for a, b in pairs), "ties": sum(a == b for a, b in pairs), "losses": sum(a < b for a, b in pairs),
                                       "mean_difference": round(float(np.mean([a - b for a, b in pairs])), 4)}
    pairs2 = [(r["memetic"], r["memetic_no_inheritance"]) for n in SETS for r in out[n]["per_seed"]]
    out["inheritance_effect"] = {"wins": sum(a > b for a, b in pairs2), "ties": sum(a == b for a, b in pairs2), "losses": sum(a < b for a, b in pairs2),
                                 "mean_difference": round(float(np.mean([a - b for a, b in pairs2])), 4)}
    from scipy.stats import wilcoxon
    def test(pairs):
        d = [a - b for a, b in pairs if a != b]
        return round(float(wilcoxon(d).pvalue), 4) if len(d) > 5 else None
    out["memetic_vs_gradient_only"]["wilcoxon_p"] = test(pairs); out["inheritance_effect"]["wilcoxon_p"] = test(pairs2)
    pl = [(r["memetic"], r["logistic_reference"]) for n in SETS for r in out[n]["per_seed"]]
    out["memetic_vs_logistic_reference"] = {"wins": sum(a > b for a, b in pl), "ties": sum(a == b for a, b in pl), "losses": sum(a < b for a, b in pl),
                                            "mean_difference": round(float(np.mean([a - b for a, b in pl])), 4), "wilcoxon_p": test(pl)}
    for n in SETS:
        pp = [(r["memetic"], r["gradient_only"]) for r in out[n]["per_seed"]]
        out[n]["memetic_vs_gradient_only"] = {"wins": sum(a > b for a, b in pp), "losses": sum(a < b for a, b in pp), "wilcoxon_p": test(pp)}
    return out

if __name__ == "__main__":
    t = time.time(); r = run(); r["seconds"] = round(time.time() - t, 1)
    json.dump(r, open(sys.argv[1] if len(sys.argv) > 1 else "evidence/neuro_experiment.json", "w"), indent=1)
    for n in SETS: print(n, r[n]["mean_test_accuracy"], "chose linear:", r[n]["chose_linear_model"], "/", r[n]["of"])
    for n in SETS: print(" ", n, "memetic vs gradient:", r[n]["memetic_vs_gradient_only"])
    print("memetic vs gradient-only:", r["memetic_vs_gradient_only"]); print("inheritance effect:", r["inheritance_effect"])
    print("memetic vs logistic reference:", r["memetic_vs_logistic_reference"], r["seconds"], "s")
