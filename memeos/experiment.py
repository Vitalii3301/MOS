"""Measured claim: experience (motifs mined from earlier memes) reduces search cost on NEW goals.
Target tasks below were written after the source benchmark and never used for tuning."""
import json, random, statistics, sys, time
from memeos import MemeOS
from tasks import TASKS, C

SOURCES = ["slug", "csv_sum", "initials", "reverse_words", "dedupe_sort"]
TARGETS = {
 "dot_case":     (C([("Hello World","hello.world"),(" Big  Data ","big.data")]), C([("Memetic Core Unit","memetic.core.unit")]), C([("Q R","q.r")])),
 "count_items":  (C([("a,b,c",3),("x,y",2)]), C([("p,q,r,s",4)]), C([("k",1)])),
 "initials_dot": (C([("ada lovelace","A.L"),("alan mathison turing","A.M.T")]), C([("grace hopper","G.H")]), C([("x y","X.Y")])),
 "reverse_csv":  (C([("1,2,3","3,2,1"),("a,b","b,a")]), C([("p,q,r,s","s,r,q,p")]), C([("z","z")])),
 "upper_snake":  (C([("hello world","HELLO_WORLD"),(" big  data ","BIG_DATA")]), C([("memetic core unit","MEMETIC_CORE_UNIT")]), C([("a b","A_B")])),
 "sorted_words": (C([("zeta alpha","alpha zeta"),("c b a","a b c")]), C([("delta gamma beta","beta delta gamma")]), C([("q","q")])),
}

def run_targets(k, **kw):
    out = {}
    for n, (tr, va, ho) in TARGETS.items():
        t = time.time(); r = k.learn(k.contract(n, tr, va, ho), **kw)
        out[n] = {"ok": r["ok"], "evaluations": r["evaluations"], "seconds": round(time.time() - t, 2),
                  "holdout": r.get("gate", {}).get("holdout"), "phase": r.get("phase")}
    return out

_BASE = None
def experienced(seed=0):
    """Fresh copy of a kernel that has learned the SOURCE goals (sqlite backup, no relearning)."""
    global _BASE
    if _BASE is None:
        _BASE = MemeOS(seed=0)
        for n in SOURCES:
            tr, va, ho = TASKS[n]; assert _BASE.learn(_BASE.contract(n, tr, va, ho))["ok"], n
    k = MemeOS(seed=seed); _BASE.db.backup(k.db); return k

if __name__ == "__main__":
    report = {"enumeration_plus_evolution": {}, "evolution_only": {}}
    # A: full system, deterministic search -> one run per condition
    naive = run_targets(MemeOS(seed=0))
    exp_k = experienced(); motifs = len(exp_k.motifs()); exp = run_targets(exp_k)
    report["enumeration_plus_evolution"] = {"motifs_available": motifs, "naive": naive, "experienced": exp}
    # B: evolution only (stochastic) -> 10 seeds per condition
    seeds = range(10); evo = {"naive": {}, "experienced": {}}
    for cond in ("naive", "experienced"):
        for n, (tr, va, ho) in TARGETS.items():
            rows = []
            for s in seeds:
                k = MemeOS(seed=s) if cond == "naive" else experienced(s)
                r = k.learn(k.contract(n, tr, va, ho), enumerate_first=False, rng=random.Random(s),
                            generations=60, restarts=2, max_candidates=6)
                rows.append({"ok": r["ok"], "evaluations": r["evaluations"]})
            print(cond, n, sum(x["ok"] for x in rows), file=sys.stderr, flush=True)
            evo[cond][n] = {"solved": sum(x["ok"] for x in rows), "of": len(rows),
                            "median_evaluations_when_solved": statistics.median([x["evaluations"] for x in rows if x["ok"]]) if any(x["ok"] for x in rows) else None}
    report["evolution_only"] = evo
    json.dump(report, open("evidence/experiment.json", "w"), indent=2, ensure_ascii=False)
    print(json.dumps(report, indent=1, ensure_ascii=False))
