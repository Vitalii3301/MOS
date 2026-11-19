"""Cross-substrate network run (offline router). The code generator here is a STUB standing in for the hub model."""
import json
from node import Node
from net_sim import Router
from tasks import TASKS, C
from modelgen import ModelGenerator
from memeos import MemeOS
from sched_sim import contract

PAL = (C([("Abba", True), ("abc", False), ("Never odd or even", True), ("MOS hub", False)]),
       C([("Was it a car or a cat I saw?", True), ("memeos", False), ("A man, a plan, a canal: Panama!", True)]), C([("No lemon, no melon", True), ("Claude", False)]))
INIT_PAL = (C([("Abe Bo Ann", True), ("Ada Bob Cat", False), ("Otto Kim Oleg", True), ("Anna Eve", False)]),
            C([("Mia Ian Max", True), ("Bob Ann", False)]), C([("Eva Liv Eli", True), ("Tom Ben", False)]))
STUB_SRC = "import re\ndef meme(x):\n    s = re.sub(r'[^a-z0-9]', '', x.lower())\n    return s == s[::-1]\n"

def run():
    R = Router(); ring = {"mos-claude": ["memeos-cell-b"], "memeos-cell-b": ["memeos-cell-c"], "memeos-cell-c": ["mos-claude"]}
    nodes = {c: Node(R.hub(c), ":memory:") for c in ring}
    for c, n in nodes.items(): n.attach_network(ring[c], [], set(ring))
    B = nodes["memeos-cell-b"]
    B.model = ModelGenerator(B.k.db, env={"MOS_MODEL_CREDENTIAL": "stub", "MOS_MODEL": "stub"}, barrier=B.barrier,
                             fetch=lambda body: {"choices": [{"message": {"content": json.dumps({"candidates": [{"source": STUB_SRC}]})}}], "usage": {}})
    B.agent.generators = [B.model]
    def pump(k=6):
        for _ in range(k):
            for n in nodes.values(): n.sync()
    nodes["mos-claude"].hub.send("memeos-cell-b", "mos-meme-task/v1", {"op": "learn", "contract": contract("initials", TASKS["initials"])}, "T-init")
    nodes["mos-claude"].hub.send("memeos-cell-b", "mos-meme-task/v1", {"op": "learn", "contract": contract("palindrome", PAL)}, "T-pal")
    pump()
    A = nodes["mos-claude"]
    lib = {g: (A.k.card(m).get("schema")) for g, m in A.k.db.execute("SELECT goal, meme FROM net_competence")}
    r = A.solve({"goal": "initials_palindrome", "input": "Eva Liv Eli", "contract": contract("initials_palindrome", INIT_PAL)})
    iso = MemeOS(seed=0); s = iso.learn(iso.contract("initials_palindrome", *INIT_PAL), compose_first=False)
    return {"library_of_A": lib, "solve": {k: r.get(k) for k in ("source", "output", "composed_of", "evaluations")},
            "isolated_mpl_evolution": {"ok": s["ok"], "evaluations": s["evaluations"], "reason": s.get("reason")}}

if __name__ == "__main__":
    print(json.dumps(run(), indent=1, ensure_ascii=False))
