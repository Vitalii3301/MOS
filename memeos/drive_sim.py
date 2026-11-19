"""Own goals, offline (in-memory hub router). Between 'could not' and 'could', nobody gives the cell a task."""
import json
from node import Node
from net_sim import Router
from tasks import TASKS, C
from modelgen import ModelGenerator
from net_data import attacks
from sched_sim import contract

WC_EVEN = (C([("one two", True), ("a b c", False), ("x", False), ("p q r s", True)]), C([("hello big world", False), ("a b", True)]), C([("one", False), ("w x y z", True)]))
IS_EVEN = (C([(2, True), (3, False), (10, True), (7, False)]), C([(4, True), (9, False)]), C([(0, True), (15, False)]))
EVEN_SRC = "def meme(x):\n    return x % 2 == 0\n"

def run():
    R = Router(); ring = {"mos-claude": ["memeos-cell-b"], "memeos-cell-b": ["memeos-cell-c"], "memeos-cell-c": ["mos-claude"]}
    nodes = {c: Node(R.hub(c), ":memory:") for c in ring}
    from net_data import build
    clean = build()["cells"]
    for c, n in nodes.items(): n.attach_network(ring[c], clean[c], set(ring))
    A, B, Cc = nodes["mos-claude"], nodes["memeos-cell-b"], nodes["memeos-cell-c"]
    B.model = ModelGenerator(B.k.db, env={"MOS_MODEL_CREDENTIAL": "stub", "MOS_MODEL": "stub"}, barrier=B.barrier,
                             fetch=lambda body: {"choices": [{"message": {"content": json.dumps({"candidates": [{"source": EVEN_SRC}]})}}], "usage": {}})
    def pump(k=5):
        for _ in range(k):
            for n in nodes.values(): n.sync()
    out = {}
    # 1) A cannot solve -> gap
    A._sender = "mos-grok"
    r1 = A.solve({"goal": "word_count_even", "input": "w x y z", "contract": contract("word_count_even", WC_EVEN)})
    out["first_attempt"] = {"source": r1["source"], "noted_as_gap": r1.get("noted_as_gap")}
    out["tick_with_unchanged_pool"] = A.drive.pursue()["done"]                 # nothing new to try -> no self-goal
    # 2) elsewhere in the network, other cells learn things (not addressed to A)
    A.hub.send("memeos-cell-c", "mos-meme-task/v1", {"op": "learn", "contract": contract("word_count", TASKS["word_count"])}, "T-wc")
    A.hub.send("memeos-cell-b", "mos-meme-task/v1", {"op": "learn", "contract": contract("is_even", IS_EVEN)}, "T-even")
    pump()
    out["library_of_A"] = sorted(g for g, in A.k.db.execute("SELECT goal FROM net_competence"))
    # 3) A's own reflection tick
    out["agenda_before_tick"] = [{k: a[k] for k in ("kind", "goal", "reason")} for a in A.drive.reflect()]
    out["own_tick"] = A.drive.pursue()["done"]
    r2 = A.solve({"goal": "word_count_even", "input": "w x y z"})
    out["after_own_tick"] = {"source": r2["source"], "output": r2.get("output")}
    # 4) reported misses -> self-immunization, spreads
    atk = attacks(6, 11); hold = attacks(12, 12)
    out["barrier_before"] = sum(B.barrier.decide(t)["invasive"] for t in hold)
    for t in atk: A.handle({"op": "report_miss", "text": t})
    out["immunize_tick"] = [{k: x.get(k) for k in ("kind", "reason", "ok", "phrases", "shared_to")} for x in A.drive.pursue()["done"]]
    pump()
    out["barrier_after_on_other_cells"] = {c: sum(n.barrier.decide(t)["invasive"] for t in hold) for c, n in nodes.items()}
    # 5) unknown goal asked twice -> the cell asks the requester for examples
    for _ in range(2): A.solve({"goal": "translate_to_latin", "input": "hello"})
    out["ask_tick"] = [{k: x.get(k) for k in ("kind", "goal", "ok", "asked")} for x in A.drive.pursue()["done"]]
    out["request_delivered_to"] = [m["kind"] for m in R.box.get("mos-grok", [])]
    out["journal"] = [k for k, in A.k.db.execute("SELECT kind FROM events WHERE kind LIKE 'self_goal%' OR kind='gap_noted'")]
    return out

if __name__ == "__main__":
    print(json.dumps(run(), indent=1, ensure_ascii=False))
