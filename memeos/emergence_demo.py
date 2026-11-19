"""Measured: composition of existing memes vs evolution from scratch on NEW composite goals; plus emergent analysis."""
import json, random, time
from memeos import MemeOS
from tasks import TASKS, C
from emergence import analyse, compose

BASE = ["slug", "reverse_words", "title_words", "initials", "snake_case", "csv_sum", "dedupe_sort", "word_count"]
NEW = {
 "reverse_then_slug": (C([("Hello Big World", "world-big-hello"), ("Alpha Beta", "beta-alpha"), ("one two three four", "four-three-two-one")]),
                       C([("Memetic Operating System", "system-operating-memetic"), ("Data Lake", "lake-data")]), C([("X Y Z", "z-y-x"), ("Solo", "solo")])),
 "title_then_reverse": (C([("big data lake", "Lake Data Big"), ("hello world", "World Hello"), ("a b c", "C B A")]),
                        C([("memetic operating system", "System Operating Memetic"), ("red fox", "Fox Red")]), C([("one", "One"), ("x y", "Y X")])),
 "reverse_then_initials": (C([("ada lovelace", "LA"), ("alan mathison turing", "TMA"), ("grace hopper", "HG")]),
                           C([("x y z", "ZYX"), ("linus torvalds", "TL")]), C([("a b", "BA"), ("tim berners lee", "LBT")])),
 "reverse_then_snake": (C([("Hello World", "world_hello"), ("Big Data Lake", "lake_data_big"), ("A B", "b_a")]),
                        C([("Memetic Core Unit", "unit_core_memetic"), ("Red Fox", "fox_red")]), C([("One Two", "two_one"), ("Q", "q")])),
}

def run():
    k = MemeOS(seed=0)
    for n in BASE:
        tr, va, ho = TASKS[n]; r = k.learn(k.contract(n, tr, va, ho), rng=random.Random(1))
    out = {"base_learned": sorted(m for m in BASE if k.active(m)), "tasks": {}}
    for name, (tr, va, ho) in NEW.items():
        t0 = time.time(); r = compose(k, k.contract(name, tr, va, ho)); tc = time.time() - t0
        k2 = MemeOS(seed=0); t0 = time.time(); r2 = k2.learn(k2.contract(name, tr, va, ho), rng=random.Random(1)); te = time.time() - t0
        out["tasks"][name] = {"composition": {"ok": r["ok"], "evaluations": r["evaluations"], "seconds": round(tc, 2),
                                              "composed_of": r.get("composed_of"), "holdout": r.get("gate", {}).get("holdout")},
                              "scratch_evolution": {"ok": r2["ok"], "evaluations": r2["evaluations"], "seconds": round(te, 2),
                                                    "holdout": r2.get("gate", {}).get("holdout") if r2["ok"] else None}}
    out["emergent"] = analyse(k)
    return out

if __name__ == "__main__":
    r = run(); json.dump(r, open("evidence/emergence.json", "w"), indent=1, ensure_ascii=False)
    print("base:", r["base_learned"])
    for n, v in r["tasks"].items():
        c, s = v["composition"], v["scratch_evolution"]
        print(f"{n:22} COMPOSE ok={c['ok']} evals={c['evaluations']:>4} {c['seconds']:>5}s {c['composed_of']} holdout={c['holdout']} | SCRATCH ok={s['ok']} evals={s['evaluations']:>7} {s['seconds']:>5}s holdout={s['holdout']}")
    e = r["emergent"]; print("\nemergent:", {k: e[k] for k in ("memes", "probe_inputs", "behaviour_clusters")})
    for cv in e["convergent_evolution"][:6]: print("  convergent:", cv)
    print("  synonym goals:", e["synonym_goals"])
    print("  hidden identities:", len(e["hidden_identities"])); [print("   ", x) for x in e["hidden_identities"][:12]]
