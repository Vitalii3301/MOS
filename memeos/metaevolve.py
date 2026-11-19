"""Self-optimization: MOS evolves the configuration of its own evolutionary engine.
Candidate configs are scored on META-TRAIN tasks; the winner is adopted only if it beats the
current config on META-HOLDOUT tasks never used during the meta-search (fresh seeds too)."""
import json, random, statistics, sys, time
from memeos import MemeOS, DEFAULT_CONFIG, canon
from tasks import TASKS, C
from experiment import TARGETS

META_TRAIN = {k: TASKS[k] for k in ("slug", "csv_sum", "initials", "snake_case", "reverse_words", "word_count")}
META_HOLDOUT = dict(TARGETS)          # 6 tasks never seen by the meta-search

def score(cfg, tasks, seeds, gens=60):
    solved, cost = 0, 0
    for name, (tr, va, ho) in tasks.items():
        for s in seeds:
            k = MemeOS(seed=s)
            r = k.learn(k.contract(name, tr, va, ho), enumerate_first=False, rng=random.Random(s),
                        generations=gens, restarts=2, max_candidates=4, config=cfg)
            solved += r["ok"]; cost += r["evaluations"]
    return solved, cost

def perturb(cfg, rng):
    c = json.loads(canon(cfg)); w = c["w"]
    for _ in range(rng.randint(1, 3)):
        k = rng.choice(list(w) + ["pop", "crossover", "tournament", "elite", "max_mutations", "random_init"])
        if k in w: w[k] = max(0.0, round(w[k] * rng.choice([0.5, 0.7, 1.4, 2.0]) + rng.choice([0, 0.02]), 3))
        elif k == "pop": c["pop"] = rng.choice([40, 60, 80, 120, 160])
        elif k == "crossover": c["crossover"] = rng.choice([0.0, 0.2, 0.45, 0.7, 0.9])
        elif k == "tournament": c["tournament"] = rng.choice([2, 3, 5, 7])
        elif k == "elite": c["elite"] = rng.choice([1, 2, 4, 8])
        elif k == "max_mutations": c["max_mutations"] = rng.choice([1, 2, 3, 4])
        elif k == "random_init": c["random_init"] = rng.choice([0.3, 0.5, 0.7, 0.9])
    if sum(w.values()) == 0: w["insert"] = 0.3
    return c

def meta_evolve(iterations=10, train_seeds=(0, 1, 2), holdout_seeds=tuple(range(100, 108)), log=print):
    rng = random.Random(42); base = json.loads(canon(DEFAULT_CONFIG))
    best, best_s = base, score(base, META_TRAIN, train_seeds); log("baseline meta-train", best_s)
    history = [{"config": base, "meta_train": best_s}]
    for i in range(iterations):
        cand = perturb(best, rng); s = score(cand, META_TRAIN, train_seeds)
        better = s[0] > best_s[0] or (s[0] == best_s[0] and s[1] < best_s[1] * 0.9)
        log(f"iter {i}: {s} {'ACCEPT' if better else 'reject'}"); history.append({"config": cand, "meta_train": s, "accepted": better})
        if better: best, best_s = cand, s
    h_base = score(base, META_HOLDOUT, holdout_seeds); h_best = score(best, META_HOLDOUT, holdout_seeds)
    log("meta-holdout baseline", h_base, "evolved", h_best)
    return {"baseline": base, "evolved": best, "meta_train": {"baseline": history[0]["meta_train"], "evolved": best_s},
            "meta_holdout": {"baseline": h_base, "evolved": h_best, "of": len(META_HOLDOUT) * len(holdout_seeds)},
            "adopt": h_best[0] > h_base[0] or (h_best[0] == h_base[0] and h_best[1] < h_base[1]), "history": history}

if __name__ == "__main__":
    t = time.time(); r = meta_evolve(log=lambda *a: print(*a, file=sys.stderr, flush=True)); r["seconds"] = round(time.time() - t)
    json.dump(r, open("evidence/metaevolution.json", "w"), indent=2, ensure_ascii=False)
    print(json.dumps({k: r[k] for k in ("meta_train", "meta_holdout", "adopt", "seconds")}, ensure_ascii=False))
