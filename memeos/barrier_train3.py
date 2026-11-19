"""Barrier v3: content-domain gene selection + extra benign content (EN tech docs, RU MOS docs).
All thresholds and gene decisions on train/val; test and external untouched."""
import json, random, time
from barrier_corpus import build, _attacks, B
from barrier import Barrier, Detector, immune_vision, segments, genes_fired, features, metrics, pick_threshold, GENES
from barrier_train2 import content_rows

def split3(xs, seed):
    xs = list(xs); random.Random(seed).shuffle(xs); n = len(xs)
    return xs[:int(.6 * n)], xs[int(.6 * n):int(.8 * n)], xs[int(.8 * n):]

def run(gate_fpr=0.002, min_neg=500):
    t0 = time.time(); rng = random.Random(0); C = build(0)
    from build_benign import load as _load_benign
    en = _load_benign("en"); ru = _load_benign("ru")
    ben = {}
    for name, xs in (("en", en), ("ru", ru)):
        a, b, c = split3([{"text": d["text"], "label": 0, "src": "benign-" + name} for d in xs], 1)
        ben.setdefault("train", []).extend(a); ben.setdefault("val", []).extend(b); ben.setdefault("test", []).extend(c)
    tr_att = _attacks(B + "text_attack_train.json") + _attacks(B + "code_attack_train.json")
    rows, npos, nneg = content_rows(C["train"] + ben["train"], tr_att, rng, neg_cap=14000)
    content = Detector().fit(rows, epochs=8, featurized=True)
    prompt = Detector().fit([d for d in C["train"] if d["src"] == "jailbreak_llms"], epochs=6)
    # ---- memetic gene selection in the content domain (train+val content negatives only)
    negs = [d for d in C["train"] + C["val"] if d["label"] == 0 and d["src"].startswith("bipia")] + ben["train"] + ben["val"]
    poss = [d for d in C["train"] + C["val"] if d["label"] == 1 and d["src"].startswith("bipia")]
    neg_ru = [d for d in negs if d["src"] == "benign-ru"]
    sel = {}
    for g in GENES:
        fa = sum(g in genes_fired(immune_vision(d["text"])[0]) for d in negs)
        fa_ru = sum(g in genes_fired(immune_vision(d["text"])[0]) for d in neg_ru)
        hits = sum(g in genes_fired(immune_vision(d["text"])[0]) for d in poss)
        decisive = fa / len(negs) <= gate_fpr and len(negs) >= min_neg and (not g.startswith("ru_") or fa_ru == 0)
        sel[g] = {"false_alarms": fa, "negatives": len(negs), "ru_false_alarms": fa_ru, "content_hits": hits, "pos": len(poss), "decisive": decisive}
    bar = Barrier(content, prompt, decisive=[g for g, v in sel.items() if v["decisive"]])
    # ---- thresholds on validation (content: zero false alarms on val content incl. benign docs)
    vc = [d for d in C["val"] if d["src"].startswith("bipia")] + ben["val"]
    content.threshold = pick_threshold([bar.assess(d["text"])[4][0] for d in vc], [d["label"] for d in vc], 0.0)
    vj = [d for d in C["val"] if d["src"] == "jailbreak_llms"]
    prompt.threshold = pick_threshold([prompt.prob(features(immune_vision(d["text"])[0], genes_fired(immune_vision(d["text"])[0]), immune_vision(d["text"])[2])) for d in vj], [d["label"] for d in vj], 0.01)
    out = {"gene_selection": sel, "decisive": sorted(bar.decisive), "thresholds": {"content": content.threshold, "prompt": prompt.threshold},
           "content_rows": {"pos": npos, "neg": nneg}}
    def ev(rows, mode):
        dec = [bar.decide(d["text"], mode) for d in rows]; ys = [d["label"] for d in rows]
        pr = [1.0 if x["invasive"] else 0.0 for x in dec]; m = metrics(pr, ys, 0.5); m.pop("auc", None)
        by = {}
        for d, p in zip(rows, pr): by.setdefault(d["src"], ([], []))[0].append(p); by[d["src"]][1].append(d["label"])
        return m, {k: {x: v for x, v in metrics(a, b, 0.5).items() if x != "auc"} for k, (a, b) in by.items()}, dec
    content_test = [d for d in C["test"] if d["src"].startswith("bipia")] + ben["test"]
    out["content_test"], out["content_test_by_source"], dec = ev(content_test, "content")
    out["external_as_content"], out["external_by_source"], _ = ev(C["external"], "content")
    out["prompt_test"], _, _ = ev([d for d in C["test"] if d["src"] == "jailbreak_llms"], "prompt")
    hid = [(d, x) for d, x in zip(content_test, dec) if d["label"] == 1 and ("\u200b" in d["text"] or "base64 and follow" in d["text"] or "font-size:0" in d["text"])]
    out["concealed_attacks_test"] = {"n": len(hid), "caught": sum(x["invasive"] for _, x in hid)}
    pos = [(d, x) for d, x in zip(content_test, dec) if d["label"] == 1 and "span" in d and x["invasive"]]
    hit = sum(1 for d, x in pos if any(d["span"][:30].lower() in c.lower() or c.lower()[:30] in d["span"].lower()
                                         for c in ([x["best"][1]] + [r for p, r, g in x["hidden"]]) if c))
    out["localization_on_caught"] = {"caught": len(pos), "pinpointed": hit}
    out["seconds"] = round(time.time() - t0, 1)
    bar.meta = {"version": "3.0", "thresholds": out["thresholds"], "content_test": out["content_test"], "decisive": out["decisive"]}
    return bar, out

if __name__ == "__main__":
    bar, out = run()
    json.dump(out, open("evidence/barrier_eval_v3.json", "w"), indent=1, ensure_ascii=False)
    json.dump(bar.to_card(), open("barrier_v3.json", "w"))
    print("decisive genes:", out["decisive"]); print("thresholds:", out["thresholds"])
    for g, v in out["gene_selection"].items(): print(f"  {g:22} FA {v['false_alarms']:>3}/{v['negatives']}  RU-FA {v['ru_false_alarms']}  hits {v['content_hits']:>3}/{v['pos']}  {'DECISIVE' if v['decisive'] else ''}")
    for k in ("content_test", "external_as_content", "prompt_test", "concealed_attacks_test", "localization_on_caught", "seconds"): print(k, out[k])
    for k in ("content_test_by_source", "external_by_source"): print(k, json.dumps(out[k], ensure_ascii=False))
