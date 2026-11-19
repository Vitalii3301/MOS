"""Formula memes: MOS grows its own vocabulary under the gate.

Origin: the self-extension fork of MemeOS 2.9.0 supplied by the owner on 2026-11-18
(MemeOS-2.9.0-self-extension-experiment.zip, sha256 e3d60563c8c5f68a281137127b64f35da6008bc96da004b0349b378df611296b).
Its whitelist (validate/evaluate) and its grammar are kept; what changed in integration:
  * a new word is NOT a process-global operator: it is an ordinary meme whose genome is
    {"pipeline":[{"op":"formula","args":[<tree>]}]} — so it lives in the cell's own hash-chained memory,
    passes the normal gate, is exported in bundles, and is re-verified by every receiving cell;
  * other memes reach it through `call` and composition, like any skill.
A formula is DATA: a bounded JSON tree interpreted by a whitelist. No eval/exec, no I/O.
"""
NUMOPS = {"add", "sub", "mul", "mod", "floordiv"}
CMPOPS = {"eq", "ne", "lt", "le", "gt", "ge"}
MAX_NODES, MAX_DEPTH, CONST_RANGE, INPUT_RANGE, VALUE_RANGE = 12, 4, 100, 1_000_000, 10 ** 12

class FormulaError(ValueError):
    pass

def validate_formula(tree):
    count = [0]
    def walk(t, depth=0):
        count[0] += 1
        if count[0] > MAX_NODES or depth > MAX_DEPTH or not isinstance(t, dict): raise FormulaError("invalid_formula_shape")
        if set(t) == {"var"} and t["var"] is True: return "num"
        if set(t) == {"const"} and type(t["const"]) is int and -CONST_RANGE <= t["const"] <= CONST_RANGE: return "num"
        if set(t) == {"abs"} and walk(t["abs"], depth + 1) == "num": return "num"
        if set(t) == {"op", "a", "b"} and t["op"] in NUMOPS and walk(t["a"], depth + 1) == walk(t["b"], depth + 1) == "num": return "num"
        if set(t) == {"cmp", "a", "b"} and t["cmp"] in CMPOPS and walk(t["a"], depth + 1) == walk(t["b"], depth + 1) == "num": return "bool"
        raise FormulaError("invalid_formula_operator")
    if walk(tree) != "bool": raise FormulaError("formula_must_return_bool")
    return tree

def evaluate_formula(tree, value):
    if type(value) is not int or not -INPUT_RANGE <= value <= INPUT_RANGE: raise FormulaError("formula_input_range")
    def calc(t):
        if "var" in t: return value
        if "const" in t: return t["const"]
        if "abs" in t: return abs(calc(t["abs"]))
        a, b = calc(t["a"]), calc(t["b"])
        if "cmp" in t: return {"eq": a == b, "ne": a != b, "lt": a < b, "le": a <= b, "gt": a > b, "ge": a >= b}[t["cmp"]]
        op = t["op"]
        if op in ("mod", "floordiv") and b == 0: raise FormulaError("formula_division_by_zero")
        r = {"mod": lambda: a % b, "floordiv": lambda: a // b, "add": lambda: a + b, "sub": lambda: a - b, "mul": lambda: a * b}[op]()
        if not -VALUE_RANGE <= r <= VALUE_RANGE: raise FormulaError("formula_overflow")
        return r
    return calc(tree)

def proposed_formulas():
    """Generic grammar written by a human (from the fork): <term> <cmp> <const>, no task-specific templates."""
    x = {"var": True}; arith = [x, {"abs": x}]
    for k in (-4, -3, -2, -1, 0, 1, 2, 3, 4, 5, 6, 7, 8, 10):
        arith += [{"op": "add", "a": x, "b": {"const": k}}, {"op": "sub", "a": x, "b": {"const": k}}, {"op": "mul", "a": x, "b": {"const": k}}]
        if k > 0: arith += [{"op": "mod", "a": x, "b": {"const": k}}, {"op": "floordiv", "a": x, "b": {"const": k}}]
    for term in arith:
        for c in (-4, -3, -2, -1, 0, 1, 2, 3, 4, 5, 6):
            for cmp in ("eq", "ne", "lt", "le", "gt", "ge"):
                yield {"cmp": cmp, "a": term, "b": {"const": c}}

def applicable(cases):
    """Only int -> bool contracts are in this grammar's reach."""
    return bool(cases) and all(type(c["input"]) is int and type(c["output"]) is bool for c in cases)

def _exact(f, cases):
    try: return all(evaluate_formula(f, c["input"]) is c["output"] for c in cases)
    except FormulaError: return False

def synthesize(train, validation, cap=10000):
    """Selection on train, admission also requires validation; holdout is never consulted here."""
    n = train_perfect = rejected = 0
    for f in proposed_formulas():
        n += 1
        if n > cap: break
        if not _exact(f, train): continue
        train_perfect += 1
        if not _exact(f, validation): rejected += 1; continue
        return {"ok": True, "formula": validate_formula(f), "evaluated": n, "train_perfect": train_perfect, "rejected_by_validation": rejected}
    return {"ok": False, "evaluated": n, "train_perfect": train_perfect, "rejected_by_validation": rejected, "reason": "no_valid_formula_in_grammar"}

def show(t):
    if "var" in t: return "x"
    if "const" in t: return str(t["const"])
    if "abs" in t: return f"|{show(t['abs'])}|"
    sym = {"add": "+", "sub": "-", "mul": "*", "mod": "%", "floordiv": "//", "eq": "==", "ne": "!=", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}
    return f"({show(t['a'])} {sym[t.get('op') or t.get('cmp')]} {show(t['b'])})"
