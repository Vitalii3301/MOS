"""Skill exchange + memetic scheduler, offline with the in-memory hub router (same code paths as live)."""
import json, random
from node import Node
from net_sim import Router
from tasks import TASKS
from emergence_demo import NEW
from memeos import MemeOS

def contract(name, t): tr, va, ho = t; return {"goal": name, "train": tr, "validation": va, "holdout": ho}

def run():
    R = Router(); ring = {"mos-claude": ["memeos-cell-b"], "memeos-cell-b": ["memeos-cell-c"], "memeos-cell-c": ["mos-claude"]}
    nodes = {c: Node(R.hub(c), ":memory:") for c in ring}
    for c, n in nodes.items(): n.attach_network(ring[c], [], set(ring))
    def pump(k=6):
        for _ in range(k):
            for n in nodes.values(): n.sync()
    # B learns text skills, C learns others; A learns NOTHING itself
    teach = {"memeos-cell-b": ["reverse_words", "slug", "title_words"], "memeos-cell-c": ["initials", "snake_case"]}
    for cell, names in teach.items():
        for nm in names: nodes["mos-claude"].hub.send(cell, "mos-meme-task/v1", {"op": "learn", "contract": contract(nm, TASKS[nm])}, f"T-{cell}-{nm}")
    pump()
    A = nodes["mos-claude"]
    lib = [g for g, c in A.k.db.execute("SELECT goal, cell FROM net_competence")]
    results = {}
    for name, t in NEW.items():
        tr, va, ho = t
        r = A.solve({"goal": name, "input": ho[0]["input"], "contract": contract(name, t)})
        results[name] = {k: r.get(k) for k in ("source", "output", "composed_of", "evaluations")} | {"expected": ho[0]["output"]}
        k2 = MemeOS(seed=0); s = k2.learn(k2.contract(name, tr, va, ho), compose_first=False, rng=random.Random(1))
        results[name]["isolated_scratch_evaluations"] = s["evaluations"]; results[name]["isolated_ok"] = s["ok"]
    # a base goal A never learned: answered from a neighbour's verified library version
    r = A.solve({"goal": "slug", "input": "Memetic  Network Node", "contract": contract("slug", TASKS["slug"])})
    results["slug_from_library"] = {k: r.get(k) for k in ("source", "output", "from")}
    return {"library_of_A": sorted(lib), "results": results, "A_status": A.net.status()}

if __name__ == "__main__":
    r = run(); print(json.dumps(r, indent=1, ensure_ascii=False))
