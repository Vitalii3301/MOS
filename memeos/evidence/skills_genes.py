import json, random, re, sys, time
sys.path.insert(0, "/home/claude/memeos")
from immune import synthesize_gene
SP = sys.argv[1]; SPLIT = sys.argv[2]; NPH = int(sys.argv[3])
recs = json.load(open(f"{SP}/hf2/{SPLIT}.json")); own = json.load(open(f"{SP}/skills_gh/own_benign.json"))
random.Random(7).shuffle(own); own_fit, own_gate = own[: len(own) // 2], own[len(own) // 2:]
cap = lambda t: t[:20000]
tr = [r for r in recs if r["split"] == "train"]; va = [r for r in recs if r["split"] == "validation"]; te = [r for r in recs if r["split"] == "test"]
pos = [cap(r["text"]) for r in tr if r["label"] == 1]; random.Random(1).shuffle(pos); pos = pos[:1500]
clean = [cap(r["text"]) for r in tr if r["label"] == 0] + [cap(o["text"]) for o in own_fit]
t = time.time(); g = synthesize_gene(pos, clean, max_phrases=NPH, min_support=3)
print("synth s", round(time.time() - t), "phrases", len(g["phrases"]) if g else 0, flush=True)
rx = re.compile(g["pattern"], re.I | re.M)
def rate(rs, lab): xs = [bool(rx.search(cap(r["text"]))) for r in rs if r["label"] == lab]; return round(sum(xs) / max(1, len(xs)), 3), len(xs)
res = {"phrases": g["phrases"], "val_recall": rate(va, 1), "val_fpr": rate(va, 0),
       "own_gate_fpr": round(sum(bool(rx.search(cap(o["text"]))) for o in own_gate) / len(own_gate), 3),
       "test_recall": rate(te, 1), "test_fpr": rate(te, 0),
       "test_by_source": {s: {"recall": rate([r for r in te if r["source"] == s], 1), "fpr": rate([r for r in te if r["source"] == s], 0)} for s in sorted({r["source"] for r in te})}}
print(json.dumps({k: v for k, v in res.items() if k != "phrases"})); print(res["phrases"][:25])
json.dump({**res, "pattern": g["pattern"]}, open(f"{SP}/eval/skills_genes_{SPLIT}_{NPH}.json", "w"), indent=1)
