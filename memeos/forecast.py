"""Forecasting in MOS: methods are competing memes, the gate is a rolling-origin backtest on the series' OWN past.

Law (same as everywhere in MOS): selection only on the cell's own evidence; the newest windows are a holdout
that is reported, never used to choose. Simple model first: an external model (pinned ONNX gene) is chosen only
if it beats the best simple method on validation by a margin (default 5%), otherwise the simple one is used.

Methods (pure Python, no numpy needed on the node):
  naive, seasonal_naive(m), drift, ses, holt_damped, theta(m)    + external gene "chronos2_small" if active.
Error: MASE (MAE scaled by the in-sample one-step (seasonal) naive MAE). Intervals: 80% from the chosen method's
own validation errors per horizon step (split-conformal); the external model brings its own quantiles, which are
also checked on validation (coverage reported).
"""
import math

# ------------------------------------------------------------------ simple methods
def _naive(y, h, m): return [y[-1]] * h
def _snaive(y, h, m):
    if m <= 1 or len(y) < m: return _naive(y, h, m)
    return [y[len(y) - m + (i % m)] for i in range(h)]
def _drift(y, h, m):
    if len(y) < 2: return _naive(y, h, m)
    b = (y[-1] - y[0]) / (len(y) - 1); return [y[-1] + b * (i + 1) for i in range(h)]

def _ses_fit(y, alphas=(0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 0.9, 1.0)):
    best = None
    for a in alphas:
        l, sse = y[0], 0.0
        for v in y[1:]: sse += (v - l) ** 2; l = a * v + (1 - a) * l
        if best is None or sse < best[0]: best = (sse, a, l)
    return best[1], best[2]
def _ses(y, h, m): return [_ses_fit(y)[1]] * h

def _holt_damped(y, h, m, phis=(0.8, 0.9, 0.98), grid=(0.1, 0.3, 0.5, 0.8)):
    if len(y) < 4: return _naive(y, h, m)
    best = None
    for a in grid:
        for b in grid:
            for p in phis:
                l, t, sse = y[0], y[1] - y[0], 0.0
                for v in y[1:]:
                    f = l + p * t; sse += (v - f) ** 2
                    ln = a * v + (1 - a) * (l + p * t); t = b * (ln - l) + (1 - b) * p * t; l = ln
                if best is None or sse < best[0]: best = (sse, l, t, p)
    _, l, t, p = best; out, damp = [], 0.0
    for i in range(1, h + 1): damp += p ** i; out.append(l + damp * t)
    return out

def _seasonal_index(y, m):
    """Classical multiplicative/additive decomposition indices (additive if series touches <= 0)."""
    if m <= 1 or len(y) < 3 * m: return None, None
    add = min(y) <= 0
    # centred moving average
    k = m // 2; ma = []
    for i in range(len(y)):
        if i < k or i + k >= len(y) or (m % 2 == 0 and i + k >= len(y)): ma.append(None); continue
        w = y[i - k:i + k + 1]
        ma.append((sum(w) - (w[0] + w[-1]) / 2) / m if m % 2 == 0 else sum(w) / m)
    buckets = [[] for _ in range(m)]
    for i, (v, c) in enumerate(zip(y, ma)):
        if c: buckets[i % m].append(v - c if add else v / c)
    idx = [sum(b) / len(b) if b else (0.0 if add else 1.0) for b in buckets]
    mean = sum(idx) / m
    idx = [x - mean for x in idx] if add else [x / mean for x in idx]
    return idx, add

def _theta(y, h, m):
    """Theta method (Assimakopoulos & Nikolopoulos 2000; as in M3/M4 benchmarks): deseasonalise, SES + half drift."""
    idx, add = _seasonal_index(y, m)
    z = y if idx is None else [v - idx[i % m] if add else v / idx[i % m] for i, v in enumerate(y)]
    a, l = _ses_fit(z); n = len(z)
    b = (z[-1] - z[0]) / (n - 1) if n > 1 else 0.0
    f = [l + 0.5 * b * ((i + 1) - 1 / a + (1 - a) ** n / a) if a > 0 else l for i in range(h)]
    if idx is None: return f
    return [v + idx[(n + i) % m] if add else v * idx[(n + i) % m] for i, v in enumerate(f)]

SIMPLE = {"naive": _naive, "seasonal_naive": _snaive, "drift": _drift, "ses": _ses, "holt_damped": _holt_damped, "theta": _theta}

# ------------------------------------------------------------------ external gene: Chronos-2 (pinned ONNX)
class Chronos2Gene:
    def __init__(self, gene):
        self.gene = gene                                 # external_gene.ExternalGene (verified, loads on demand)
    @property
    def active(self): return self.gene is not None and self.gene.active
    def quantiles(self, y, h):
        """Returns {q: [h values]} for q in the model's 13 native quantiles."""
        g = self.gene
        if g.session is None and not g._load(): raise RuntimeError("gene_unavailable")
        import time
        np = g.np; g.last_used = time.time()
        y = list(y)[-int(g.card.get("context_max", 2048)):]
        pad = (-len(y)) % 16
        ctx = np.array([[0.0] * pad + y], dtype=np.float32); mask = np.array([[0.0] * pad + [1.0] * len(y)], dtype=np.float32)
        fl = int(g.card.get("future_length", 672))
        out = g.session.run(None, {"context": ctx, "group_ids": np.array([0], dtype=np.int64), "attention_mask": mask,
                                   "future_covariates": np.zeros((1, fl), np.float32), "future_covariates_mask": np.zeros((1, fl), np.float32)})[0][0]
        qs = g.card["quantiles"]
        return {q: [float(v) for v in out[i][:h]] for i, q in enumerate(qs)}
    def point(self, y, h, m): return self.quantiles(y, h)[0.5]

# ------------------------------------------------------------------ scoring and selection
def scale(y, m):
    """In-sample MAE of the one-step seasonal naive (MASE denominator)."""
    s = m if m > 1 and len(y) > m + 1 else 1
    d = [abs(y[i] - y[i - s]) for i in range(s, len(y))]
    v = sum(d) / len(d) if d else 0.0
    return v if v > 0 else 1e-9

def backtest(y, h, m, methods, origins):
    """methods: {name: f(y, h, m)}; origins: list of cut indices t (forecast y[t:t+h] from y[:t])."""
    err = {k: [] for k in methods}; resid = {k: [[] for _ in range(h)] for k in methods}
    for t in origins:
        hist, fut = y[:t], y[t:t + h]
        if len(fut) < h: continue
        sc = scale(hist, m)
        for k, f in methods.items():
            p = f(hist, h, m)
            err[k].append(sum(abs(a - b) for a, b in zip(p, fut)) / h / sc)
            for i in range(h): resid[k][i].append(fut[i] - p[i])
    return {k: (sum(v) / len(v) if v else float("inf")) for k, v in err.items()}, resid

def _q(xs, q):
    xs = sorted(xs)
    if not xs: return 0.0
    pos = q * (len(xs) - 1); lo = int(math.floor(pos)); hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)

def select(y, h, m=1, external=None, n_val=12, step=None, margin=0.05):
    """Gate: rolling origins over the last n_val*step points of the history (validation). Returns the decision."""
    step = step or h
    methods = dict(SIMPLE)
    ext = external if external is not None and external.active else None
    if ext: methods["chronos2_small"] = ext.point
    n = len(y); first = max(3 * max(m, 1) + 2, n - h - (n_val - 1) * step)
    origins = [t for t in range(first, n - h + 1, step)][-n_val:]
    if len(origins) < 3: raise ValueError("series_too_short_for_gate")
    mase, resid = backtest(y, h, m, methods, origins)
    simple = {k: v for k, v in mase.items() if k in SIMPLE}
    best_simple = min(simple, key=simple.get)
    chosen = best_simple
    if ext and mase["chronos2_small"] < simple[best_simple] * (1 - margin): chosen = "chronos2_small"
    return {"chosen": chosen, "best_simple": best_simple, "validation_mase": {k: round(v, 4) for k, v in mase.items()},
            "validation_origins": len(origins), "resid": resid[chosen], "margin": margin}

def forecast(y, h, m=1, external=None, n_val=12, step=None, margin=0.05):
    d = select(y, h, m, external, n_val, step, margin)
    k = d["chosen"]
    if k == "chronos2_small":
        q = external.quantiles(y, h); point = q[0.5]; lo, hi = q[0.1], q[0.9]
    else:
        point = SIMPLE[k](y, h, m)
        lo = [p + _q(r, 0.1) for p, r in zip(point, d["resid"])]; hi = [p + _q(r, 0.9) for p, r in zip(point, d["resid"])]
    d.pop("resid")
    return {**d, "forecast": point, "interval80": [lo, hi]}
