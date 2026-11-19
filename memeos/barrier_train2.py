"""Barrier v2 training/eval. Choices (thresholds) on VALIDATION only; test & external untouched."""
import json, random, time, math
from barrier_corpus import build, WRAP_TRAIN, _attacks, B
from barrier import Barrier, Detector, immune_vision, segments, genes_fired, features, metrics, pick_threshold

def content_rows(docs, attacks, rng, neg_cap=9000):
    pos, neg = [], []
    for a in attacks:
        for w in WRAP_TRAIN:
            vis, rev, ch = immune_vision(w(a))
            for sg in segments(vis):
                if a[:25].lower() in sg.lower() or sg.lower() in a.lower(): pos.append((features(sg, genes_fired(sg), []), 1))
            for r in rev: pos.append((features(r, genes_fired(r), ch), 1))
    for d in docs:
        if d["label"] == 0 and d["src"].startswith("bipia"):
            for sg in segments(immune_vision(d["text"])[0]): neg.append((features(sg, genes_fired(sg), []), 0))
    rng.shuffle(neg)
    return pos + neg[:neg_cap], len(pos), min(len(neg), neg_cap)

def run():
    t0 = time.time(); rng = random.Random(0); C = build(0)
    tr_attacks = _attacks(B + "text_attack_train.json") + _attacks(B + "code_attack_train.json")
    rows, npos, nneg = content_rows(C["train"], tr_attacks, rng)
    content = Detector().fit(rows, epochs=8, featurized=True)
    prompt = Detector().fit([d for d in C["train"] if d["src"] == "jailbreak_llms"], epochs=6)
    bar = Barrier(content, prompt)
    # thresholds on validation
    vj = [d for d in C["val"] if d["src"] == "jailbreak_llms"]
    prompt.threshold = pick_threshold([prompt.prob(features(immune_vision(d["text"])[0], genes_fired(immune_vision(d["text"])[0]), immune_vision(d["text"])[2])) for d in vj], [d["label"] for d in vj], 0.01)
    vb = [d for d in C["val"] if d["src"].startswith("bipia")]
    vbs = [bar.assess(d["text"])[4][0] for d in vb]
    content.threshold = pick_threshold(vbs, [d["label"] for d in vb], 0.0)   # zero false alarms on validation content
    out = {"content_rows": {"pos": npos, "neg": nneg}, "thresholds": {"content": content.threshold, "prompt": prompt.threshold}}
    def ev(rows):
        dec = [bar.decide(d["text"]) for d in rows]; ys = [d["label"] for d in rows]
        pr = [1.0 if x["invasive"] else 0.0 for x in dec]
        res = metrics(pr, ys, 0.5); res.pop("auc", None)
        by = {}
        for d, p in zip(rows, pr): by.setdefault(d["src"], ([], []))[0].append(p); by[d["src"]][1].append(d["label"])
        return res, {k: {x: v for x, v in metrics(a, b, 0.5).items() if x != "auc"} for k, (a, b) in by.items()}, dec
    for name in ("val", "test", "external"):
        out[name], out[name + "_by_source"], dec = ev(C[name])
        if name == "test": test_dec = dec
    hid = [(d, x) for d, x in zip(C["test"], test_dec) if d["label"] == 1 and d["src"].startswith("bipia") and ("\u200b" in d["text"] or "base64 and follow" in d["text"] or "font-size:0" in d["text"])]
    out["concealed_attacks_test"] = {"n": len(hid), "caught": sum(x["invasive"] for _, x in hid),
                                     "via_concealment_rule": sum("concealed_instruction" in x["reasons"] for _, x in hid)}
    pos = [(d, x) for d, x in zip(C["test"], test_dec) if d["label"] == 1 and "span" in d and x["invasive"]]
    hit = 0
    for d, x in pos:
        cand = ([x["best"][1]] if x["best"][1] else []) + [r for p, r, g in x["hidden"]]
        if any(d["span"][:30].lower() in c.lower() or c.lower()[:30] in d["span"].lower() for c in cand if c): hit += 1
    out["localization_on_caught_bipia_test"] = {"caught": len(pos), "injected_instruction_pinpointed": hit}
    out["seconds"] = round(time.time() - t0, 1)
    bar.meta = {"version": "2.0", "thresholds": out["thresholds"], "test": out["test"], "external": out["external"]}
    return bar, out

if __name__ == "__main__":
    bar, out = run()
    json.dump(out, open("evidence/barrier_eval_v2.json", "w"), indent=1, ensure_ascii=False)
    json.dump(bar.to_card(), open("barrier_v2.json", "w"))
    print(json.dumps({k: v for k, v in out.items() if not k.endswith("by_source")}, indent=1, ensure_ascii=False))
    for n in ("test_by_source", "external_by_source"): print(n, json.dumps(out[n], ensure_ascii=False))
