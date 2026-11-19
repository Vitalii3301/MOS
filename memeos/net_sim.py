"""Offline network run with an in-memory hub router (same protocol and code paths as the live hub)."""
import hashlib, json, itertools
from memeos import canon
from node import Node
from net_data import build

class Router:
    def __init__(self): self.box, self.seq = {}, itertools.count(1)
    def hub(self, node_id): return RHub(self, node_id)
class RHub:
    def __init__(self, r, node): self.r, self.node = r, node
    def send(self, to, kind, payload, mid):
        m = {"stream_seq": next(self.r.seq), "message_id": mid, "sender": self.node, "kind": kind, "payload": json.loads(canon(payload)),
             "payload_sha256": hashlib.sha256(canon(payload).encode()).hexdigest()}
        self.r.box.setdefault(to, []).append(m); return 201, {}
    def call(self, method, path, body=None):
        if path.startswith("/v1/mailbox/"):
            after = int(path.split("after=")[1].split("&")[0]); return 200, {"messages": [m for m in self.r.box.get(self.node, []) if m["stream_seq"] > after]}
        return 201, {}

def run():
    D = build(); R = Router()
    ring = {"mos-claude": ["memeos-cell-b"], "memeos-cell-b": ["memeos-cell-c"], "memeos-cell-c": ["mos-claude"]}
    nodes = {c: Node(R.hub(c), ":memory:") for c in ring}
    for c, n in nodes.items(): n.attach_network(ring[c], D["cells"][c], set(ring))
    def measure():
        return {c: {"holdout_attacks_caught": sum(n.barrier.decide(t)["invasive"] for t in D["holdout_attacks"]),
                    "holdout_clean_false_alarms": sum(n.barrier.decide(t)["invasive"] for t in D["holdout_clean"][c])} for c, n in nodes.items()}
    before = measure()
    imm = nodes["mos-claude"].net.immunize(D["discovery"])
    for _ in range(6):
        for n in nodes.values(): n.sync()
    # hostile offers: an over-broad gene and a ReDoS gene, injected by cell-b into cell-c
    for name, pat in (("too_broad", r"\b(the|and|to)\b"), ("redos", r"(a+)+$")):
        nodes["memeos-cell-b"].hub.send("memeos-cell-c", "mos-meme-offer/v1", {"schema": "mos-meme-offer/v1", "offer_id": "bad-" + name, "kind": "antimeme",
            "origin": "memeos-cell-b", "hops": 1, "max_hops": 3, "gene": {"name": name, "pattern": pat, "examples": D["discovery"][:4]}}, "MSG-bad-" + name)
    for _ in range(3):
        for n in nodes.values(): n.sync()
    after = measure()
    return {"before": before, "immunize": {k: imm[k] for k in ("missed", "action", "phrases", "lab")}, "after": after,
            "status": {c: n.net.status() for c, n in nodes.items()}}

if __name__ == "__main__":
    r = run(); print(json.dumps(r, indent=1, ensure_ascii=False))
