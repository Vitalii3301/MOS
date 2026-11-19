"""MOS Science — hypotheses are memes ("создание новых теорий, выявление закономерностей").

A hypothesis meme = expression shape f(x) + fitted constants y ~ a + b*f(x) (closed-form least squares).
Search: bottom-up enumeration with observational-equivalence pruning (same principle as MemeOS synthesis)
plus parsimony. Selection uses TRAIN points only; a law is ACCEPTED only if it predicts held-out points.
Theory transfer: an accepted law's SHAPE is carried to a new domain and only its constants are refit;
it survives there only if it again predicts that domain's held-out points.
Control: on data without structure the system must answer "no law", not invent one.
"""
import math, itertools, json, random

UN = {"sqrt": lambda v: math.sqrt(v) if v >= 0 else None, "sq": lambda v: v * v,
      "log": lambda v: math.log(v) if v > 0 else None, "inv": lambda v: 1.0 / v if v != 0 else None,
      "exp": lambda v: math.exp(v) if v < 50 else None}
BIN = {"+": lambda a, b: a + b, "-": lambda a, b: a - b, "*": lambda a, b: a * b,
       "/": lambda a, b: a / b if b != 0 else None}

def show(e):
    if isinstance(e, str): return e
    if len(e) == 2: return f"{e[0]}({show(e[1])})"
    return f"({show(e[1])} {e[0]} {show(e[2])})"

def size(e): return 1 if isinstance(e, str) else 1 + sum(size(x) for x in e[1:])

def evaluate(e, row):
    if isinstance(e, str): return row[e]
    if len(e) == 2:
        v = evaluate(e[1], row); return None if v is None else UN[e[0]](v)
    a, b = evaluate(e[1], row), evaluate(e[2], row)
    if a is None or b is None: return None
    try: r = BIN[e[0]](a, b)
    except OverflowError: return None
    return r if r is None or (math.isfinite(r) and abs(r) < 1e300) else None

def fit(fx, y):
    """least squares y ~ a + b*f ; returns (a, b, r2)."""
    n = len(y); mf = sum(fx) / n; my = sum(y) / n
    sff = sum((f - mf) ** 2 for f in fx)
    if sff < 1e-300: return None
    b = sum((f - mf) * (v - my) for f, v in zip(fx, y)) / sff; a = my - b * mf
    ss = sum((v - my) ** 2 for v in y) or 1e-300
    res = sum((v - (a + b * f)) ** 2 for f, v in zip(fx, y))
    return a, b, 1 - res / ss

def r2_with(a, b, fx, y):
    my = sum(y) / len(y); ss = sum((v - my) ** 2 for v in y) or 1e-300
    return 1 - sum((v - (a + b * f)) ** 2 for f, v in zip(fx, y)) / ss

def discover(rows, target, variables, max_size=7, beam=4000, relative=True):
    """Return ranked hypotheses fitted on rows. relative=True fits in log space of |y| when y>0
    (so a planet 400x farther does not dominate the error) — a choice about error metric, not about the law."""
    ys = [r[target] for r in rows]
    tr = lambda v: math.log(v) if relative else v
    if relative and min(ys) <= 0: relative = False
    level = {1: [(v, tuple(r[v] for r in rows)) for v in variables]}
    seen = {sig for _, sig in level[1]}; pool = list(level[1])
    for s in range(2, max_size + 1):
        new = []
        for e, sig in level.get(s - 1, []):
            for u in UN:
                try: vals = tuple(UN[u](x) for x in sig)
                except (OverflowError, ValueError): continue
                if any(v is None or not math.isfinite(v) or abs(v) > 1e100 for v in vals): continue
                k = tuple(round(v, 12) for v in vals)
                if k in seen: continue
                seen.add(k); new.append(((u, e), vals))
        for s1 in range(1, s - 1):
            s2 = s - 1 - s1
            for (e1, g1), (e2, g2) in itertools.product(level.get(s1, []), level.get(s2, [])):
                for o in BIN:
                    if o in "+*" and repr(e1) > repr(e2): continue
                    try: vals = tuple(BIN[o](a, b) for a, b in zip(g1, g2))
                    except (OverflowError, ZeroDivisionError): continue
                    if any(v is None or not math.isfinite(v) or abs(v) > 1e100 for v in vals): continue
                    k = tuple(round(v, 12) for v in vals)
                    if k in seen: continue
                    seen.add(k); new.append(((o, e1, e2), vals))
        new.sort(key=lambda t: -abs(_corr(t[1], ys)))
        level[s] = new[:beam]; pool += level[s]
    out = []
    for e, sig in pool:
        if relative:     # fit y = b*f (+a) in original units but rank by relative error
            f = fit(list(sig), ys)
            if not f: continue
            a, b, _ = f
            pred = [a + b * v for v in sig]
            if any(p <= 0 for p in pred): continue
            err = sum((math.log(p) - math.log(v)) ** 2 for p, v in zip(pred, ys)) / len(ys)
            out.append({"expr": e, "a": a, "b": b, "rel_mse": err, "size": size(e)})
        else:
            f = fit(list(sig), ys)
            if f: out.append({"expr": e, "a": f[0], "b": f[1], "rel_mse": 1 - f[2], "size": size(e)})
    out.sort(key=lambda h: (h["rel_mse"] + 1e-4 * h["size"], h["size"]))
    return out

def _corr(x, y):
    if any(abs(v) > 1e100 for v in x): return 0.0
    n = len(x); mx = sum(x) / n; my = sum(y) / n
    sx = math.sqrt(sum((a - mx) ** 2 for a in x)); sy = math.sqrt(sum((b - my) ** 2 for b in y))
    return 0.0 if sx == 0 or sy == 0 else sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sx * sy)

def holdout_error(h, rows, target):
    fx = []
    for r in rows:
        try: v = evaluate(h["expr"], r)
        except (OverflowError, ValueError, ZeroDivisionError): v = None
        if v is None: return float("inf")
        fx.append(v)
    pr = [h["a"] + h["b"] * v for v in fx]
    if any(p <= 0 for p in pr): return float("inf")
    return max(abs(p - r[target]) / abs(r[target]) for p, r in zip(pr, rows))

def accept_law(train, test, target, variables, tol=0.02, top=50, **kw):
    """Select on TRAIN, accept only if max relative error on held-out TEST <= tol. Report alternatives honestly."""
    hyps = discover(train, target, variables, **kw)[:top]
    for h in hyps:
        h["holdout_max_rel_err"] = holdout_error(h, test, target)
    best = hyps[0] if hyps else None
    accepted = best is not None and best["holdout_max_rel_err"] <= tol
    return {"accepted": accepted, "law": best, "runners_up": hyps[1:4]}

def transfer(law, rows, target, test_rows, tol=0.02):
    """Carry a law's SHAPE into a new domain: refit constants only, then demand held-out accuracy."""
    fx = [evaluate(law["expr"], r) for r in rows]
    if any(v is None for v in fx): return {"survives": False, "holdout_max_rel_err": float("inf")}
    f = fit(fx, [r[target] for r in rows])
    if not f: return {"survives": False}
    h = {"expr": law["expr"], "a": f[0], "b": f[1]}
    err = holdout_error(h, test_rows, target)
    return {"survives": err <= tol, "a": f[0], "b": f[1], "holdout_max_rel_err": err, "expr": law["expr"]}
