"""Immune tolerance, offline: a spread antimeme fires on clean text -> recalled by its origin everywhere -> refined gene spreads."""
import json
from node import Node
from net_sim import Router
from net_data import build, attacks

CLEAN_FP = "To reproduce the plot, call numpy.linspace with num=50 and then pass the result to the renderer."
CLEAN_OTHER = ["Use os.path.join with sep=None on Windows paths.", "The helper str.split with maxsplit=1 returns two parts."]

def run():
    R = Router(); ring = {"mos-claude": ["memeos-cell-b"], "memeos-cell-b": ["memeos-cell-c"], "memeos-cell-c": ["mos-claude"]}
    D = build(); nodes = {c: Node(R.hub(c), ":memory:") for c in ring}
    for c, n in nodes.items(): n.attach_network(ring[c], list(D["cells"][c]), set(ring))
    A, B, C = nodes["mos-claude"], nodes["memeos-cell-b"], nodes["memeos-cell-c"]
    hold = attacks(30, 2)
    def pump(k=6):
        for _ in range(k):
            for n in nodes.values(): n.sync()
    def snap():
        return {c: {"attacks_caught_of_30": sum(n.barrier.decide(t)["invasive"] for t in hold), "clean_fp_flagged": n.barrier.decide(CLEAN_FP)["invasive"],
                    "other_clean_flagged": sum(n.barrier.decide(t)["invasive"] for t in CLEAN_OTHER), "genes": sorted(n.barrier.extra)} for c, n in nodes.items()}
    out = {}
    A.net.immunize(D["discovery"]); pump(); out["after_immunity_spread"] = snap()
    # forged recall: B (not the origin) tries to recall A's gene at C
    gname = next(iter(C.barrier.extra)); pat = C.k.db.execute("SELECT pattern FROM net_genes WHERE name=?", (gname,)).fetchone()[0]
    B.hub.send("memeos-cell-c", "mos-meme-recall/v1", {"schema": "mos-meme-recall/v1", "name": gname, "pattern": pat, "evidence": CLEAN_FP, "origin": "mos-claude"}, "MSG-forged")
    pump(2); out["forged_recall_gene_still_present_at_C"] = gname in C.barrier.extra
    # a user tells C (not the origin) that the text is clean
    C._sender = "mos-user"; out["report_at_C"] = C.handle({"op": "report_false_alarm", "text": CLEAN_FP})
    out["C_own_tick"] = [{k: x.get(k) for k in ("kind", "ok", "action", "genes")} for x in C.drive.pursue()["done"]]
    pump()                                                   # origin A receives the report as an observation
    out["A_own_tick"] = [{k: x.get(k) for k in ("kind", "ok", "action", "genes")} for x in A.drive.pursue()["done"]]
    pump(); out["after_recall_and_refinement"] = snap()
    out["recalls"] = {c: n.net.status()["recalls"] for c, n in nodes.items()}
    return out

if __name__ == "__main__":
    print(json.dumps(run(), indent=1, ensure_ascii=False))
