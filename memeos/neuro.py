"""Neuro-memes (законы МОС: «гибридизация нейросетей и меметических алгоритмов», «AI-мем»).

A neuro-meme = small MLP classifier: genome (architecture + learning hyper-parameters) + learned weights +
input normalisation. Memetic algorithm in the exact sense: evolution of genomes + lifetime learning (SGD) +
inheritance of what was learned (child starts from the parent's weights; resized layers keep the overlap).
Training needs numpy; inference is pure Python so any cell can RUN and VERIFY a received neuro-meme.
Selection uses validation only; the test split is touched once, for the report.
"""
import json, math, random

try:
    import numpy as np
except Exception:                       # cells without numpy can still execute and verify neuro-memes
    np = None

DEFAULT = {"hidden": [16], "act": "tanh", "lr": 0.05, "l2": 1e-4, "batch": 32}

# ------------------------------------------------------------------ numpy training
def init_weights(sizes, rng):
    return [(rng.normal(0, math.sqrt(2.0 / (a + b)), (a, b)), np.zeros(b)) for a, b in zip(sizes[:-1], sizes[1:])]

def _act(z, kind): return np.tanh(z) if kind == "tanh" else np.maximum(z, 0)
def _dact(a, kind): return 1 - a * a if kind == "tanh" else (a > 0).astype(float)

def forward(W, X, act):
    acts = [X]
    for i, (w, b) in enumerate(W):
        z = acts[-1] @ w + b
        acts.append(z if i == len(W) - 1 else _act(z, act))
    z = acts[-1] - acts[-1].max(axis=1, keepdims=True); p = np.exp(z); p /= p.sum(axis=1, keepdims=True)
    return acts, p

def accuracy(W, X, y, act): return float((forward(W, X, act)[1].argmax(axis=1) == y).mean())

def train_epochs(W, g, X, y, epochs, rng):
    """Lifetime learning: minibatch SGD with L2. Returns new weights (W is not modified)."""
    W = [(w.copy(), b.copy()) for w, b in W]; n = len(X); k = W[-1][1].shape[0]; Y = np.eye(k)[y]
    for _ in range(epochs):
        idx = rng.permutation(n)
        for s in range(0, n, g["batch"]):
            bi = idx[s:s + g["batch"]]; acts, p = forward(W, X[bi], g["act"])
            delta = (p - Y[bi]) / len(bi)
            for i in range(len(W) - 1, -1, -1):
                gw = acts[i].T @ delta + g["l2"] * W[i][0]; gb = delta.sum(axis=0)
                if i: delta = (delta @ W[i][0].T) * _dact(acts[i], g["act"])
                W[i] = (W[i][0] - g["lr"] * gw, W[i][1] - g["lr"] * gb)
    return W

def inherit(parent_W, sizes, rng):
    """Lamarckian inheritance through an architecture change: keep the overlapping block of every layer."""
    W = init_weights(sizes, rng)
    for i, (w, b) in enumerate(W):
        if i < len(parent_W):
            pw, pb = parent_W[i]; r, c = min(w.shape[0], pw.shape[0]), min(w.shape[1], pw.shape[1])
            w[:r, :c] = pw[:r, :c]; b[:c] = pb[:c]
    return W

def mutate_genome(g, rng):
    g = json.loads(json.dumps(g)); r = rng.random()
    if r < 0.30 and g["hidden"]: i = rng.randrange(len(g["hidden"])); g["hidden"][i] = max(2, min(64, int(g["hidden"][i] * rng.choice([0.5, 0.75, 1.5, 2]))))
    elif r < 0.40 and len(g["hidden"]) < 2: g["hidden"].append(max(2, g["hidden"][-1] // 2) if g["hidden"] else 8)
    elif r < 0.50 and g["hidden"]: g["hidden"].pop()                       # simplification is a legal mutation, down to a linear model
    elif r < 0.75: g["lr"] = float(min(0.5, max(0.002, g["lr"] * rng.choice([0.3, 0.5, 2, 3]))))
    elif r < 0.87: g["l2"] = float(min(0.05, max(0.0, g["l2"] * rng.choice([0.1, 10]) if g["l2"] else 1e-4)))
    elif r < 0.94: g["act"] = "relu" if g["act"] == "tanh" else "tanh"
    else: g["batch"] = rng.choice([16, 32, 64])
    return g

def crossover_genome(a, b, rng):
    c = json.loads(json.dumps(a))
    for k in ("act", "lr", "l2", "batch"):
        if rng.random() < 0.5: c[k] = b[k]
    return c

def sizes_of(g, n_in, n_out): return [n_in] + list(g["hidden"]) + [n_out]

def memetic(Xtr, ytr, Xva, yva, n_out, seed=0, pop=8, gens=6, life=5, lamarck=True):
    """Evolution + lifetime learning. lamarck=False -> learned weights are NOT inherited (ablation)."""
    rng = np.random.default_rng(seed); pr = random.Random(seed); n_in = Xtr.shape[1]
    P = []
    for i in range(pop):
        g = dict(DEFAULT) if i == 0 else ({**DEFAULT, "hidden": []} if i == 1 else mutate_genome(mutate_genome(DEFAULT, pr), pr))
        P.append({"g": g, "W": init_weights(sizes_of(g, n_in, n_out), rng)})
    n_params = lambda W: sum(w.size + b.size for w, b in W)
    best = None; epochs_used = 0
    for gen in range(gens):
        for ind in P:
            ind["W"] = train_epochs(ind["W"], ind["g"], Xtr, ytr, life, rng); epochs_used += life
            ind["fit"] = accuracy(ind["W"], Xva, yva, ind["g"]["act"])
            if best is None or ind["fit"] > best["fit"] or (ind["fit"] == best["fit"] and n_params(ind["W"]) < n_params(best["W"])):
                best = {"g": ind["g"], "W": ind["W"], "fit": ind["fit"], "gen": gen}        # Occam: equal accuracy -> the simpler network
        if gen == gens - 1: break
        P.sort(key=lambda d: (-d["fit"], n_params(d["W"]))); elite = P[:2]; nxt = [dict(e) for e in elite]
        while len(nxt) < pop:
            a = max(pr.sample(P, 3), key=lambda d: d["fit"]); b = max(pr.sample(P, 3), key=lambda d: d["fit"])
            g = mutate_genome(crossover_genome(a["g"], b["g"], pr), pr)
            W = inherit(a["W"], sizes_of(g, n_in, n_out), rng) if lamarck else init_weights(sizes_of(g, n_in, n_out), rng)
            nxt.append({"g": g, "W": W})
        if not lamarck:
            for e in nxt[:2]: e["W"] = init_weights(sizes_of(e["g"], n_in, n_out), rng)
        P = nxt
    return {**best, "epochs": epochs_used}

def gradient_only(Xtr, ytr, Xva, yva, n_out, seed=0, epochs=240):
    """Same total training budget in one default network; best epoch chosen on validation (early stopping)."""
    rng = np.random.default_rng(seed); g = dict(DEFAULT); W = init_weights(sizes_of(g, Xtr.shape[1], n_out), rng); best = None
    for e in range(0, epochs, 5):
        W = train_epochs(W, g, Xtr, ytr, 5, rng); f = accuracy(W, Xva, yva, g["act"])
        if best is None or f > best["fit"]: best = {"g": g, "W": W, "fit": f}
    return {**best, "epochs": epochs}

def evolution_only(Xtr, ytr, Xva, yva, n_out, seed=0, pop=8, evals=720):
    """Neuro-evolution without gradients: Gaussian weight mutation, selection on TRAIN accuracy, report by validation."""
    rng = np.random.default_rng(seed); g = dict(DEFAULT); sz = sizes_of(g, Xtr.shape[1], n_out)
    P = [init_weights(sz, rng) for _ in range(pop)]; best = None; used = 0
    while used < evals:
        fits = [accuracy(W, Xtr, ytr, g["act"]) for W in P]; used += pop
        order = np.argsort(fits)[::-1]; top = [P[i] for i in order[:2]]
        v = accuracy(top[0], Xva, yva, g["act"])
        if best is None or v > best["fit"]: best = {"g": g, "W": top[0], "fit": v}
        P = list(top) + [[(w + rng.normal(0, 0.08, w.shape), b + rng.normal(0, 0.08, b.shape)) for w, b in top[int(rng.integers(2))]] for _ in range(pop - 2)]
    return {**best, "epochs": 0}

# ------------------------------------------------------------------ the meme: card + pure-Python inference
def neuro_card(goal, g, W, mean, std, classes, context=None, parents=(), origin=None):
    q = lambda a: [[round(float(v), 4) for v in row] for row in a]
    return {"schema": "mos-neuro-meme/v1", "name": goal, "goal": goal, "context": context or {}, "parents": sorted(parents),
            "origin": origin or {"kind": "memetic"}, "arch": {"hidden": list(g["hidden"]), "act": g["act"], "lr": g["lr"], "l2": g["l2"], "batch": g["batch"]},
            "norm": {"mean": [round(float(v), 5) for v in mean], "std": [round(float(v), 5) for v in std]},
            "weights": [{"w": q(w), "b": [round(float(v), 4) for v in b]} for w, b in W], "classes": list(classes)}

def predict(card, row):
    """Pure Python forward pass — no numpy needed to run or verify a neuro-meme."""
    x = [(v - m) / s for v, m, s in zip(row, card["norm"]["mean"], card["norm"]["std"])]
    L = card["weights"]
    for i, layer in enumerate(L):
        z = [sum(x[j] * layer["w"][j][k] for j in range(len(x))) + layer["b"][k] for k in range(len(layer["b"]))]
        x = z if i == len(L) - 1 else ([math.tanh(v) for v in z] if card["arch"]["act"] == "tanh" else [max(v, 0.0) for v in z])
    return card["classes"][max(range(len(x)), key=x.__getitem__)]

def card_accuracy(card, rows, labels):
    return sum(predict(card, r) == y for r, y in zip(rows, labels)) / max(1, len(rows))

def validate_card(card):
    """Structural antimeme screen for neuro-memes: bounded size, finite numbers, consistent shapes."""
    try:
        assert card["schema"] == "mos-neuro-meme/v1" and card["arch"]["act"] in ("tanh", "relu")
        n = len(card["norm"]["mean"]); assert 1 <= n <= 4096 and len(card["norm"]["std"]) == n and all(s > 0 for s in card["norm"]["std"])
        total = 0
        for layer in card["weights"]:
            assert len(layer["w"]) == n and all(len(r) == len(layer["b"]) for r in layer["w"]); n = len(layer["b"]); total += len(layer["w"]) * n
            assert all(isinstance(v, (int, float)) and math.isfinite(v) and abs(v) < 1e4 for r in layer["w"] for v in r)
        assert n == len(card["classes"]) and total <= 200000 and len(card["weights"]) <= 4
        return []
    except (AssertionError, KeyError, TypeError) as e:
        return ["static:invalid_neuro_meme"]


# ------------------------------------------------------------------ kernel integration: gates for learned models
class NeuroMemes:
    """Learned models as memes. Gate (decided by the negative result in evidence/neuro_experiment*.json):
    a neuro-meme is accepted only if, on VALIDATION, it beats BOTH simple references — the majority class and a
    linear model trained on the same data — and improves on the active version. Otherwise complexity is refused."""
    def __init__(self, kernel): self.k = kernel

    def _data(self, cid):
        c = self.k._contract(cid); classes = sorted({canon_label(x["output"]) for x in c["train"]})
        idx = {v: i for i, v in enumerate(classes)}
        arr = lambda cs: (np.array([x["input"] for x in cs], dtype=float), np.array([idx.get(canon_label(x["output"]), 0) for x in cs]))
        (Xtr, ytr), (Xva, yva) = arr(c["train"]), arr(c["validation"])
        mean, std = Xtr.mean(0), Xtr.std(0) + 1e-9
        return c, classes, (Xtr - mean) / std, ytr, (Xva - mean) / std, yva, mean, std

    def references(self, cid, seed=0):
        c, classes, Xtr, ytr, Xva, yva, mean, std = self._data(cid); rng = np.random.default_rng(seed)
        maj = float((yva == np.bincount(ytr).argmax()).mean())
        g = {**DEFAULT, "hidden": [], "lr": 0.1}; W = init_weights(sizes_of(g, Xtr.shape[1], len(classes)), rng); best = 0.0
        for _ in range(40):
            W = train_epochs(W, g, Xtr, ytr, 5, rng); best = max(best, accuracy(W, Xva, yva, g["act"]))
        return {"majority": round(maj, 4), "linear": round(best, 4)}

    def add(self, card):
        from memeos import ensure, meme_id
        import time
        bad = validate_card(card); ensure(not bad, "rejected_by_antimeme:" + ",".join(bad))
        mid = meme_id(card)
        if not self.k.db.execute("SELECT 1 FROM memes WHERE id=?", (mid,)).fetchone():
            self.k.db.execute("INSERT INTO memes(id,card,created) VALUES(?,?,?)", (mid, json.dumps(card, sort_keys=True, separators=(",", ":")), time.time()))
            self.k.event("neuro_meme_recorded", {"id": mid, "arch": card["arch"]["hidden"]})
        return mid

    def promote(self, mid, cid, refs=None):
        from memeos import goal_key
        card = self.k.card(mid); c = self.k._contract(cid)
        acc = lambda cases: round(card_accuracy(card, [x["input"] for x in cases], [canon_label(x["output"]) for x in cases]), 4)
        va = acc(c["validation"]); refs = refs or self.references(cid)
        if not card["arch"]["hidden"]: refs = {"majority": refs["majority"]}      # a linear model only has to beat the majority class
        key = goal_key(c["goal"], c["context"]); act = self.k.db.execute("SELECT meme_id FROM active WHERE goal_key=?", (key,)).fetchone()
        base = None
        if act and self.k.card(act[0]).get("schema") == "mos-neuro-meme/v1":
            ac = self.k.card(act[0]); base = round(card_accuracy(ac, [x["input"] for x in c["validation"]], [canon_label(x["output"]) for x in c["validation"]]), 4)
        beats = va > max(refs.values()); improves = base is None or va > base
        ok = beats and improves
        reason = "accepted" if ok else ("no_gain_over_simple_reference" if not beats else "no_improvement")
        if ok:
            if act and act[0] != mid: self.k.db.execute("UPDATE memes SET status='superseded' WHERE id=?", (act[0],))
            self.k.db.execute("INSERT INTO active VALUES(?,?,NULL) ON CONFLICT(goal_key) DO UPDATE SET previous=active.meme_id, meme_id=excluded.meme_id", (key, mid))
            self.k.db.execute("UPDATE memes SET status='active' WHERE id=?", (mid,))
        hold = acc(c["holdout"]) if c["holdout"] else None
        self.k.event("promoted" if ok else "rejected", {"id": mid, "kind": "neuro", "contract": cid, "reason": reason, "validation": va, "references": refs, "holdout_report_only": hold})
        return {"accepted": ok, "reason": reason, "validation": va, "references": refs, "holdout": hold}

    def learn(self, cid, seed=0, **kw):
        c, classes, Xtr, ytr, Xva, yva, mean, std = self._data(cid)
        res = memetic(Xtr, ytr, Xva, yva, len(classes), seed, **kw)
        card = neuro_card(c["goal"], res["g"], res["W"], mean, std, classes, c["context"], origin={"kind": "memetic", "generation": res["gen"], "epochs": res["epochs"]})
        refs = self.references(cid, seed); mid = self.add(card); r = self.promote(mid, cid, refs)
        out = {"meme": mid, "arch": res["g"]["hidden"], **r}
        if not r["accepted"] and r["reason"] == "no_gain_over_simple_reference" and res["g"]["hidden"]:
            # the complex model did not earn its complexity: adopt the simplest adequate model instead
            rng = np.random.default_rng(seed); g = {**DEFAULT, "hidden": [], "lr": 0.1}; W = init_weights(sizes_of(g, Xtr.shape[1], len(classes)), rng); best = (0.0, W)
            for _ in range(40):
                W = train_epochs(W, g, Xtr, ytr, 5, rng); a = accuracy(W, Xva, yva, g["act"])
                if a > best[0]: best = (a, W)
            lin = neuro_card(c["goal"], g, best[1], mean, std, classes, c["context"], origin={"kind": "simple_reference"})
            lid = self.add(lin); lr = self.promote(lid, cid, refs)
            out["fallback_linear"] = {"meme": lid, **lr}
        return out

def canon_label(v): return v
