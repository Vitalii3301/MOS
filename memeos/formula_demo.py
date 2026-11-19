"""Formula words vs native evolution, same kernel, same contracts. Holdout used for the report only."""
import json, random, time
from memeos import MemeOS
from tasks import C
TASKS_F = {
 "is_even":        (C([(2, True), (3, False), (10, True), (7, False), (0, True), (-4, True)]), C([(4, True), (9, False), (12, True)]), C([(100, True), (101, False), (-7, False), (64, True)])),
 "divisible_by_3": (C([(3, True), (4, False), (9, True), (10, False), (0, True), (-6, True)]), C([(12, True), (13, False), (21, True)]), C([(99, True), (100, False), (-9, True), (8, False)])),
 "divisible_by_5": (C([(5, True), (6, False), (10, True), (12, False), (0, True), (-15, True)]), C([(25, True), (26, False)]), C([(1000, True), (1001, False), (-5, True), (7, False)])),
 "is_negative":    (C([(-1, True), (0, False), (5, False), (-30, True)]), C([(-7, True), (8, False)]), C([(-999, True), (999, False), (-2, True), (1, False)])),
 "is_prime":       (C([(2, True), (3, True), (4, False), (5, True), (9, False), (11, True), (15, False), (17, True)]), C([(13, True), (21, False)]), C([(29, True), (27, False)])),
}
def run():
    out = {}
    for name, (tr, va, ho) in TASKS_F.items():
        k = MemeOS(seed=0); t = time.time(); r = k.learn(k.contract(name, tr, va, ho), generations=40, restarts=1, max_candidates=4, rng=random.Random(1)); s1 = round(time.time() - t, 2)
        k2 = MemeOS(seed=0); t = time.time(); r2 = k2.learn(k2.contract(name, tr, va, ho), formula_first=False, generations=40, restarts=1, max_candidates=4, rng=random.Random(1)); s2 = round(time.time() - t, 2)
        out[name] = {"with_formula_words": {"ok": r["ok"], "phase": r.get("phase"), "evaluations": r["evaluations"], "holdout": r.get("gate", {}).get("holdout") if r["ok"] else None, "seconds": s1},
                     "native_only": {"ok": r2["ok"], "phase": r2.get("phase"), "evaluations": r2["evaluations"], "holdout": r2.get("gate", {}).get("holdout") if r2["ok"] else None, "seconds": s2}}
    return out
if __name__ == "__main__":
    r = run(); json.dump(r, open("evidence/formula_words.json", "w"), indent=1)
    for n, v in r.items(): print(f"{n:15}", v["with_formula_words"], "| native:", v["native_only"])
