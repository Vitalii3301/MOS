"""Compile MPL genomes to Python source defining meme(x) — a common genetic code for both substrates.
Semantics mirror memeos.run exactly (verified by property tests against random evolved programs).
`call` dependencies are inlined (their genomes are compiled into nested helper functions)."""
import json

PRE = '''
def _ensure(c):
    if not c: raise ValueError("meme_failed")
def _num(v):
    _ensure(isinstance(v, (int, float)) and not isinstance(v, bool)); return v
def _frng(r):
    _ensure(-10**12 <= r <= 10**12); return r
def _fdiv(a, b, mod):
    _ensure(b != 0); return _frng(a % b if mod else a // b)
def _guard(v):
    if isinstance(v, str): _ensure(len(v) <= 20000)
    if isinstance(v, list): _ensure(len(v) <= 2000)
    if isinstance(v, float): _ensure(v == v and abs(v) != float("inf"))
    return v
'''

def _step(st, fn_for, v="v"):
    op, a = st["op"], st.get("args", [])
    S = lambda s: json.dumps(s, ensure_ascii=False)
    T = {
        "trim": f"_ensure(isinstance({v}, str)); {v} = {v}.strip()",
        "lower": f"_ensure(isinstance({v}, str)); {v} = {v}.lower()",
        "upper": f"_ensure(isinstance({v}, str)); {v} = {v}.upper()",
        "title": f"_ensure(isinstance({v}, str)); {v} = {v}.title()",
        "collapse_ws": f"_ensure(isinstance({v}, str)); {v} = ' '.join({v}.split()) if {v}.strip() else {v}.strip()",
        "reverse": f"_ensure(isinstance({v}, (str, list))); {v} = {v}[::-1]",
        "sort": f"_ensure(isinstance({v}, list)); _ensure(all(isinstance(x, str) for x in {v}) or all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in {v})); {v} = sorted({v})",
        "unique": f"_ensure(isinstance({v}, list)); _u = []\n    for _x in {v}:\n        if _x not in _u: _u.append(_x)\n    {v} = _u",
        "length": f"_ensure(isinstance({v}, (str, list))); {v} = len({v})",
        "first": f"_ensure(isinstance({v}, (str, list)) and len({v}) > 0); {v} = {v}[0]",
        "last": f"_ensure(isinstance({v}, (str, list)) and len({v}) > 0); {v} = {v}[-1]",
        "number": f"_ensure(isinstance({v}, str))\n    try: _f = float({v}.strip().replace(',', '.'))\n    except ValueError: raise ValueError('meme_failed')\n    {v} = int(_f) if _f.is_integer() else _f",
        "abs": f"{v} = abs(_num({v}))",
        "keys": f"_ensure(isinstance({v}, dict)); {v} = sorted({v}.keys())",
        "filter_nonempty": f"_ensure(isinstance({v}, list)); {v} = [x for x in {v} if x not in ('', None, [])]",
        "filter_digits": f"_ensure(isinstance({v}, list)); {v} = [x for x in {v} if isinstance(x, str) and x.isdigit()]",
        "sum": f"_ensure(isinstance({v}, list)); {v} = sum(_num(x) for x in {v})",
        "to_str": f"_ensure(isinstance({v}, (int, float, str)) and not isinstance({v}, bool)); {v} = {v} if isinstance({v}, str) else (str(int({v})) if isinstance({v}, float) and {v}.is_integer() else str({v}))",
    }
    if op in T: return T[op]
    if op == "replace": return f"_ensure(isinstance({v}, str) and {S(a[0])} != ''); {v} = {v}.replace({S(a[0])}, {S(a[1])})"
    if op == "split": return f"_ensure(isinstance({v}, str)); {v} = {v}.split({S(a[0])}) if {S(a[0])} else {v}.split()"
    if op == "join": return f"_ensure(isinstance({v}, list) and all(isinstance(x, str) for x in {v})); {v} = {S(a[0])}.join({v})"
    if op == "prefix": return f"_ensure(isinstance({v}, str)); {v} = {S(a[0])} + {v}"
    if op == "suffix": return f"_ensure(isinstance({v}, str)); {v} = {v} + {S(a[0])}"
    if op == "strip_chars": return f"_ensure(isinstance({v}, str)); {v} = {v}.strip({S(a[0])})"
    if op == "count": return f"_ensure(isinstance({v}, str) and {S(a[0])} != ''); {v} = {v}.count({S(a[0])})"
    if op == "add": return f"{v} = _num({v}) + {a[0]!r}"
    if op == "scale": return f"{v} = _num({v}) * {a[0]!r}"
    if op == "round": return f"{v} = round(_num({v}), {max(0, a[0])}); {v} = int({v}) if {a[0]} <= 0 else {v}"
    if op == "take": return f"_ensure(isinstance({v}, (str, list))); {v} = {v}[:{max(0, a[0])}]"
    if op == "drop": return f"_ensure(isinstance({v}, (str, list))); {v} = {v}[{max(0, a[0])}:]"
    if op == "pluck": return f"_ensure(isinstance({v}, dict) and {S(a[0])} in {v}); {v} = {v}[{S(a[0])}]"
    if op == "map": return f"_ensure(isinstance({v}, list)); {v} = [{fn_for(a[0])}(x) for x in {v}]"
    if op == "call": return f"{v} = {fn_for(('call', a[0]))}({v})"
    if op == "formula":
        from formula import validate_formula
        validate_formula(a[0])
        def ex(t):
            if "var" in t: return "_x"
            if "const" in t: return repr(int(t["const"]))
            if "abs" in t: return f"abs({ex(t['abs'])})"
            sym = {"add": "+", "sub": "-", "mul": "*", "eq": "==", "ne": "!=", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}
            if t.get("op") in ("mod", "floordiv"):
                return f"_fdiv({ex(t['a'])}, {ex(t['b'])}, {t['op'] == 'mod'})"
            if "op" in t: return f"_frng({ex(t['a'])} {sym[t['op']]} {ex(t['b'])})"
            return f"({ex(t['a'])} {sym[t['cmp']]} {ex(t['b'])})"
        return (f"_ensure(isinstance({v}, int) and not isinstance({v}, bool) and -1000000 <= {v} <= 1000000); _x = {v}; {v} = {ex(a[0])}")
    raise ValueError("unsupported op " + op)

def compile_genome(genome, resolve=None, name="meme", prefix="_g"):
    fns, counter = [], [0]
    def fn_for(obj):
        counter[0] += 1; fname = f"{prefix}{counter[0]}"
        prog = resolve(obj[1]) if isinstance(obj, tuple) else obj
        emit(prog, fname); return fname
    def emit(prog, fname):
        body = [_step(st, fn_for) + "\n    v = _guard(v)" for st in prog["pipeline"]]
        fns.append(f"def {fname}(v):\n    " + "\n    ".join(body or ["pass"]) + "\n    return v\n")
    emit(genome, prefix + "root")
    wrapper = f"\ndef {name}(x):\n    return {prefix}root(x)\n"
    return PRE + "\n".join(fns) + wrapper
