"""Train + evaluate the barrier. Threshold chosen on VALIDATION at FPR <= 1%; test/external never used for choices."""
import json, sys, time, re
from barrier_corpus import build
from barrier import Detector, prep, genes_fired, metrics, pick_threshold, immune_vision

def run():
    t0 = time.time(); C = build(0)
    det = Detector().fit(C["train"], epochs=6)
    vp = [det.score_doc(d["text"])[0] for d in C["val"]]; vy = [d["label"] for d in C["val"]]
    det.threshold = pick_threshold(vp, vy, 0.01)
    out = {"sizes": {k: len(v) for k, v in C.items()}, "threshold": det.threshold, "train_seconds": round(time.time() - t0, 1)}
    def evaluate(rows):
        ps = [det.score_doc(d["text"])[0] for d in rows]; ys = [d["label"] for d in rows]
        return ps, ys
    for name in ("val", "test", "external"):
        ps, ys = evaluate(C[name]); out[name] = metrics(ps, ys, det.threshold)
        by = {}
        for d, p in zip(C[name], ps): by.setdefault(d["src"], ([], []))[0].append(p); by[d["src"]][1].append(d["label"])
        out[name + "_by_source"] = {k: metrics(a, b, det.threshold) for k, (a, b) in by.items()}
    # baseline: structural antimemes only (any gene or concealment channel fires)
    ys = [d["label"] for d in C["test"]]
    gb = [1.0 if (genes_fired(immune_vision(d["text"])[0]) or immune_vision(d["text"])[2]) else 0.0 for d in C["test"]]
    out["test_genes_only_baseline"] = metrics(gb, ys, 0.5)
    # ablation: immune vision off on the concealment-channel test cases
    hid = [d for d in C["test"] if d["label"] == 1 and d["src"].startswith("bipia") and (any(ch in d["text"] for ch in "\u200b") or "base64" in d["text"])]
    def raw_prob(t):
        from barrier import features, genes_fired as gf
        return det.prob(features(t, gf(t), []))
    out["concealed_attacks_test"] = {"n": len(hid), "caught_with_immune_vision": sum(det.score_doc(d["text"])[0] >= det.threshold for d in hid),
                                     "caught_without_immune_vision": sum(raw_prob(d["text"]) >= det.threshold for d in hid)}
    # localization: does the top-1 span contain the injected instruction?
    pos = [d for d in C["test"] if d["label"] == 1 and "span" in d]; hit = 0; flagged = 0
    for d in pos:
        r = det.screen(d["text"])
        if r["invasive"]:
            flagged += 1
            key = d["span"][:40].lower()
            if r["spans"] and (key in r["spans"][0]["text"].lower() or r["spans"][0]["text"].lower()[:40] in d["span"].lower()): hit += 1
    out["localization_top1_on_flagged_bipia_test"] = {"flagged": flagged, "top1_contains_injection": hit}
    out["total_seconds"] = round(time.time() - t0, 1)
    det.meta = {k: out[k] for k in ("threshold", "sizes")} | {"test": out["test"], "external": out["external"]}
    return det, out

if __name__ == "__main__":
    det, out = run()
    json.dump(out, open("evidence/barrier_eval.json", "w"), indent=1, ensure_ascii=False)
    json.dump(det.to_card(), open("barrier_detector_v1.json", "w"))
    print(json.dumps({k: v for k, v in out.items() if not k.endswith("by_source")}, indent=1, ensure_ascii=False))
    for n in ("test_by_source", "external_by_source"): print(n, json.dumps(out[n], ensure_ascii=False))
