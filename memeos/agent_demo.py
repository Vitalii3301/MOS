"""Autonomous run: a stream of real-world inputs arrives; the world returns the correct answer
after each action (feedback). Nobody calls learn() by hand. We measure errors over time."""
import json, sys, time
from memeos import MemeOS
from agent import Agent

GOAL, CTX = "Canonical MOS hub node id", {"domain": "hub-protocol", "format": "node-id"}
TRUTH = ["mos-claude", "mos-gpt", "mos-grok", "mos-grok-agent", "mos-gemini", "mos-kimi", "mos-manus", "mos-perplexity", "mos-user",
         "mos-gemini-aistudio", "mos-claude-node", "mos-hub-relay"]
def variants(t):
    w = t.split("-")
    return [" ".join(w).upper(), "_".join(x.title() for x in w), "  " + "  ".join(w) + " ", " - ".join(w), "__".join(w),
            w[0].upper() + " " + "_".join(w[1:]), "-".join(w).title() + "  ", " ".join(w).title() + "!!"]
STREAM = [(v, t) for i in range(8) for t in TRUTH for v in [variants(t)[i]]]   # 96 events, harder formats later

def run(path=":memory:"):
    k = MemeOS(path); a = Agent(k, "mos-claude-agent"); log = []
    for n, (inp, truth) in enumerate(STREAM):
        r = a.perform(GOAL, inp, CTX)
        fb = a.feedback(r["episode"], truth)
        ev = {"n": n, "input": inp, "output": r["output"], "correct": fb["correct"], "status": r["status"]}
        if "learning" in fb:
            L = fb["learning"]; ev["learned"] = L.get("ok"); ev["learning_reason"] = L.get("reason") or (L.get("attempts") or [{}])[-1].get("reason")
            if L.get("ok"): ev["new_meme"] = L["meme"][:12]
        log.append(ev)
    tick = a.ecosystem_tick()
    windows = [sum(not e["correct"] for e in log[i:i + 12]) for i in range(0, len(log), 12)]
    lineage = []
    m = k.active(GOAL, CTX)
    while m:
        c = k.card(m); lineage.append({"meme": m[:12], "program": [s["op"] + (json.dumps(s["args"], ensure_ascii=False) if "args" in s else "") for s in c["genome"]["pipeline"]]})
        m = c["parents"][0] if c["parents"] else None
    return {"events": len(log), "errors_per_12_events": windows, "learning_events": [e for e in log if "learned" in e],
            "final_lineage_newest_first": lineage, "census": a.census(), "ecosystem_tick": tick, "integrity": k.integrity()["ok"]}

if __name__ == "__main__":
    t = time.time(); r = run(); r["seconds"] = round(time.time() - t, 1)
    json.dump(r, open("evidence/agent_autonomous_run.json", "w"), indent=2, ensure_ascii=False)
    print("errors per 12 events:", r["errors_per_12_events"])
    for e in r["learning_events"]: print(f"  event {e['n']:>2} {e['input']!r:28} -> {e['output']!r:26} learned={e['learned']} {e.get('new_meme','')} {'' if e['learned'] else e.get('learning_reason')}")
    print("lineage:"); [print("  ", x) for x in r["final_lineage_newest_first"]]
    print("census:", r["census"], "integrity:", r["integrity"], f"{r['seconds']}s")
