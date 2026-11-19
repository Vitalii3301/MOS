#!/usr/bin/env python3
"""MemeOS 1.0 — Memetic Operating System kernel (stdlib only).

Meme = executable genome in a bounded language (no eval, no host access),
with goal, context, parents, origin; id = SHA-256 of canonical card.

Mechanisms (each tested in test_memeos.py, measured in experiment.py):
  replication+mutation   point/insert/delete/param mutations of genomes
  recombination          one-point crossover; explicit hybridize(a, b)
  selection              population evolution, tournament, elitism, parsimony
  promotion gate         exact on train+validation, no regression on prior
                         contracts of the same goal, improvement over active
  anti-memes             static policy + immune signatures; quarantine+rollback
  emergence              motif mining from promoted memes -> reusable genes
                         that feed back into later searches (measured effect)
  composition            'call' pins dependency hash
  memory                 SQLite, hash-chained event journal, reopen-safe
  transfer               bundle export/import; receiver re-verifies, never
                         trusts sender fitness; import never auto-activates
"""
from __future__ import annotations
import difflib, hashlib, json, random, sqlite3, time, re
from dataclasses import dataclass

VERSION = "2.23.0"

def canon(v) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def sha(v) -> str:
    return hashlib.sha256((v if isinstance(v, str) else canon(v)).encode()).hexdigest()

class MemeError(Exception):
    pass

def ensure(c, msg):
    if not c:
        raise MemeError(msg)

# ------------------------------------------------------------------ DSL
# op name -> (param kinds); kinds: 's' string, 'i' int, 'n' number, 'p' program, 'h' meme hash
OPS = {
    "trim": (), "lower": (), "upper": (), "title": (), "collapse_ws": (),
    "reverse": (), "sort": (), "unique": (), "length": (), "first": (), "last": (),
    "number": (), "abs": (), "keys": (), "filter_nonempty": (), "filter_digits": (),
    "sum": (), "to_str": (),
    "replace": ("s", "s"), "split": ("s",), "join": ("s",), "prefix": ("s",),
    "suffix": ("s",), "strip_chars": ("s",), "count": ("s",),
    "add": ("n",), "scale": ("n",), "round": ("i",), "take": ("i",), "drop": ("i",),
    "pluck": ("s",), "map": ("p",), "call": ("h",), "formula": ("f",),
}
MAX_STEPS, MAX_LEN, MAX_LIST, MAX_DEPTH, MAX_PIPE = 5000, 20000, 2000, 4, 12

def validate(prog, depth=0):
    ensure(isinstance(prog, dict) and set(prog) == {"pipeline"}, "invalid_program")
    pipe = prog["pipeline"]
    ensure(isinstance(pipe, list) and len(pipe) <= MAX_PIPE, "invalid_pipeline")
    ensure(depth <= MAX_DEPTH, "program_too_deep")
    for st in pipe:
        ensure(isinstance(st, dict) and st.get("op") in OPS, "unknown_op")
        kinds = OPS[st["op"]]
        args = st.get("args", [])
        ensure(set(st) <= {"op", "args"} and isinstance(args, list) and len(args) == len(kinds), "bad_args")
        for k, a in zip(kinds, args):
            if k == "s": ensure(isinstance(a, str) and len(a) <= 64, "bad_str_arg")
            elif k == "i": ensure(isinstance(a, int) and not isinstance(a, bool) and -1000 <= a <= 1000, "bad_int_arg")
            elif k == "n": ensure(isinstance(a, (int, float)) and not isinstance(a, bool) and abs(a) <= 1e9, "bad_num_arg")
            elif k == "p": validate(a, depth + 1)
            elif k == "h": ensure(isinstance(a, str) and re.fullmatch(r"[0-9a-f]{64}", a), "bad_hash_arg")
            elif k == "f":
                from formula import validate_formula, FormulaError
                try: validate_formula(a)
                except FormulaError as e: raise MemeError("bad_formula_arg:" + str(e))
    return prog

def deps(prog):
    out = []
    for st in prog["pipeline"]:
        if st["op"] == "call": out.append(st["args"][0])
        if st["op"] == "map": out += deps(st["args"][0])
    return out

class Budget:
    def __init__(self): self.steps = 0
    def tick(self):
        self.steps += 1
        ensure(self.steps <= MAX_STEPS, "step_limit")

def _num(x):
    ensure(isinstance(x, (int, float)) and not isinstance(x, bool), "type_number")
    return x

def _guard(v):
    if isinstance(v, str): ensure(len(v) <= MAX_LEN, "string_too_long")
    if isinstance(v, list): ensure(len(v) <= MAX_LIST, "list_too_long")
    if isinstance(v, float): ensure(v == v and abs(v) != float("inf"), "bad_float")
    return v

def run(prog, value, resolve=None, budget=None, depth=0):
    """Execute a program. resolve(hash)->program for 'call'."""
    budget = budget or Budget()
    ensure(depth <= MAX_DEPTH, "call_too_deep")
    for st in prog["pipeline"]:
        budget.tick()
        op, a = st["op"], st.get("args", [])
        v = value
        if op == "trim": ensure(isinstance(v, str), "type_str"); v = v.strip()
        elif op == "lower": ensure(isinstance(v, str), "type_str"); v = v.lower()
        elif op == "upper": ensure(isinstance(v, str), "type_str"); v = v.upper()
        elif op == "title": ensure(isinstance(v, str), "type_str"); v = v.title()
        elif op == "collapse_ws": ensure(isinstance(v, str), "type_str"); v = " ".join(v.split()) if v.strip() else v.strip()
        elif op == "reverse": ensure(isinstance(v, (str, list)), "type_seq"); v = v[::-1]
        elif op == "sort":
            ensure(isinstance(v, list), "type_list")
            ensure(all(isinstance(x, str) for x in v) or all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v), "mixed_sort")
            v = sorted(v)
        elif op == "unique":
            ensure(isinstance(v, list), "type_list"); seen = []; [seen.append(x) for x in v if x not in seen]; v = seen
        elif op == "length": ensure(isinstance(v, (str, list)), "type_seq"); v = len(v)
        elif op == "first": ensure(isinstance(v, (str, list)) and len(v) > 0, "empty"); v = v[0]
        elif op == "last": ensure(isinstance(v, (str, list)) and len(v) > 0, "empty"); v = v[-1]
        elif op == "number":
            ensure(isinstance(v, str), "type_str")
            try: f = float(v.strip().replace(",", "."))
            except ValueError: raise MemeError("not_a_number")
            v = int(f) if f.is_integer() else f
        elif op == "abs": v = abs(_num(v))
        elif op == "keys": ensure(isinstance(v, dict), "type_obj"); v = sorted(v.keys())
        elif op == "filter_nonempty": ensure(isinstance(v, list), "type_list"); v = [x for x in v if x not in ("", None, [])]
        elif op == "filter_digits": ensure(isinstance(v, list), "type_list"); v = [x for x in v if isinstance(x, str) and x.isdigit()]
        elif op == "sum": ensure(isinstance(v, list), "type_list"); v = sum(_num(x) for x in v)
        elif op == "to_str":
            ensure(isinstance(v, (int, float, str)) and not isinstance(v, bool), "type_scalar")
            v = v if isinstance(v, str) else (str(int(v)) if isinstance(v, float) and v.is_integer() else str(v))
        elif op == "replace": ensure(isinstance(v, str) and a[0] != "", "type_str"); v = v.replace(a[0], a[1])
        elif op == "split": ensure(isinstance(v, str), "type_str"); v = v.split(a[0]) if a[0] else v.split()
        elif op == "join": ensure(isinstance(v, list) and all(isinstance(x, str) for x in v), "type_strlist"); v = a[0].join(v)
        elif op == "prefix": ensure(isinstance(v, str), "type_str"); v = a[0] + v
        elif op == "suffix": ensure(isinstance(v, str), "type_str"); v = v + a[0]
        elif op == "strip_chars": ensure(isinstance(v, str), "type_str"); v = v.strip(a[0])
        elif op == "count": ensure(isinstance(v, str) and a[0] != "", "type_str"); v = v.count(a[0])
        elif op == "add": v = _num(v) + a[0]
        elif op == "scale": v = _num(v) * a[0]
        elif op == "round": v = round(_num(v), max(0, a[0])); v = int(v) if a[0] <= 0 else v
        elif op == "take": ensure(isinstance(v, (str, list)), "type_seq"); v = v[:max(0, a[0])]
        elif op == "drop": ensure(isinstance(v, (str, list)), "type_seq"); v = v[max(0, a[0]):]
        elif op == "pluck": ensure(isinstance(v, dict) and a[0] in v, "missing_key"); v = v[a[0]]
        elif op == "map":
            ensure(isinstance(v, list), "type_list"); v = [run(a[0], x, resolve, budget, depth + 1) for x in v]
        elif op == "call":
            ensure(resolve is not None, "no_resolver"); v = run(resolve(a[0]), v, resolve, budget, depth + 1)
        elif op == "formula":
            from formula import evaluate_formula, FormulaError
            try: v = evaluate_formula(a[0], v)
            except FormulaError as e: raise MemeError(str(e))
        value = _guard(v)
    return value

# ------------------------------------------------------------------ Meme card
def meme_card(program, goal, context=None, parents=(), origin=None, name=""):
    validate(program)
    return {"schema": "mos-meme/v1", "name": name or goal, "goal": goal, "context": context or {},
            "parents": sorted(parents), "origin": origin or {"kind": "authored"}, "genome": program}

def meme_id(card) -> str:
    return sha(card)

# ------------------------------------------------------------------ fitness
def score_case(out, expected):
    if out == expected and type(out) == type(expected): return 1.0
    if isinstance(out, str) and isinstance(expected, str):
        return 0.9 * difflib.SequenceMatcher(None, out, expected).ratio()
    if isinstance(out, (int, float)) and isinstance(expected, (int, float)) and not isinstance(out, bool):
        d = abs(out - expected); return 0.9 / (1 + d)
    if isinstance(out, list) and isinstance(expected, list):
        if not expected and not out: return 1.0
        hits = sum(1 for x, y in zip(out, expected) if x == y)
        return 0.9 * hits / max(len(out), len(expected))
    return 0.0

def evaluate(prog, cases, resolve=None):
    """returns (mean graded fitness, exact passes, total)"""
    tot, exact = 0.0, 0
    for c in cases:
        try:
            out = run(prog, c["input"], resolve)
            s = score_case(out, c["output"])
        except MemeError:
            s = 0.0
        tot += s; exact += s == 1.0
    return tot / max(1, len(cases)), exact, len(cases)

# ------------------------------------------------------------------ variation operators
NULLARY = [k for k, v in OPS.items() if v == ()]

def constant_pool(cases):
    """Constants are harvested from the task data, not hard-coded answers."""
    chars, tokens, ints = set(), set(), {0, 1, 2, 3, -1}
    for c in cases:
        for v in (c["input"], c["output"]):
            for s in _strings(v):
                chars.update(ch for ch in s if not ch.isalnum())
                for t in re.findall(r"[^\W\d_]+|\d+", s):
                    if len(t) <= 12: tokens.add(t)
                for d in re.findall(r"-?\d+", s):
                    if abs(int(d)) <= 1000: ints.add(int(d))
            if isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) <= 1000: ints.add(int(v))
    chars |= {" ", "-", "_", ",", ""}
    return {"s": sorted(chars) + sorted(tokens)[:30], "i": sorted(ints)[:30], "n": sorted(ints)[:30] + [0.5, 10, 100]}

def _strings(v):
    if isinstance(v, str): yield v
    elif isinstance(v, list):
        for x in v: yield from _strings(x)
    elif isinstance(v, dict):
        for k, x in v.items(): yield k; yield from _strings(x)

def random_step(rng, pool, motifs=None, allow_map=True):
    op = rng.choice(list(OPS))
    kinds = OPS[op]
    if "h" in kinds or "f" in kinds: op, kinds = rng.choice(NULLARY), ()
    if "p" in kinds and not allow_map: op, kinds = rng.choice(NULLARY), ()
    args = []
    for k in kinds:
        if k == "s": args.append(rng.choice(pool["s"]))
        elif k == "i": args.append(rng.choice(pool["i"]))
        elif k == "n": args.append(rng.choice(pool["n"]))
        elif k == "p": args.append({"pipeline": [random_step(rng, pool, None, False) for _ in range(rng.randint(1, 2))]})
    st = {"op": op}
    if args: st["args"] = args
    return st

DEFAULT_CONFIG = {"schema": "mos-search-config/v1",
    "w": {"motif": 0.25, "insert": 0.25, "delete": 0.15, "inner_map": 0.07, "lift_map": 0.08, "param": 0.12, "point": 0.05, "transpose": 0.03},
    "pop": 80, "elite": 4, "tournament": 3, "crossover": 0.45, "max_mutations": 2, "random_init": 0.7}

def mutate(prog, rng, pool, motifs=None, w=None):
    w = dict(w or DEFAULT_CONFIG["w"])
    if not motifs: w["motif"] = 0
    p = json.loads(canon(prog)); pipe = p["pipeline"]
    if not any(st["op"] == "map" for st in pipe): w["inner_map"] = 0
    if not pipe: w = {"insert": 1}
    kinds = [k for k in w if w[k] > 0]
    kind = rng.choices(kinds, [w[k] for k in kinds])[0]
    if kind == "motif":
        m = rng.choice(motifs); i = rng.randint(0, len(pipe)); pipe[i:i] = json.loads(canon(m))
    elif kind == "insert": pipe.insert(rng.randint(0, len(pipe)), random_step(rng, pool))
    elif kind == "delete": pipe.pop(rng.randrange(len(pipe)))
    elif kind == "inner_map":
        i = rng.choice([k for k, st in enumerate(pipe) if st["op"] == "map"])
        inner = mutate(pipe[i]["args"][0], rng, pool, None, w)
        if inner["pipeline"]: pipe[i]["args"][0] = inner
    elif kind == "lift_map":
        i = rng.randrange(len(pipe)); body = pipe[i:i + rng.randint(1, 2)]
        if all(st["op"] != "map" for st in body):
            pipe[i:i + len(body)] = [{"op": "map", "args": [{"pipeline": json.loads(canon(body))}]}]
    elif kind == "param":
        idx = [i for i, s in enumerate(pipe) if s.get("args") and OPS[s["op"]][0] in "sin"]
        if idx:
            i = rng.choice(idx); k = rng.randrange(len(pipe[i]["args"])); kd = OPS[pipe[i]["op"]][k]
            if kd in "sin": pipe[i]["args"][k] = rng.choice(pool[kd])
    elif kind == "point": pipe[rng.randrange(len(pipe))] = random_step(rng, pool)
    elif kind == "transpose" and len(pipe) > 1:
        i, j = rng.sample(range(len(pipe)), 2); pipe[i], pipe[j] = pipe[j], pipe[i]
    p["pipeline"] = pipe[:MAX_PIPE]
    try: return validate(p)
    except MemeError: return prog

def crossover(a, b, rng):
    pa, pb = a["pipeline"], b["pipeline"]
    i, j = rng.randint(0, len(pa)), rng.randint(0, len(pb))
    child = {"pipeline": json.loads(canon(pa[:i] + pb[j:]))[:MAX_PIPE]}
    try: return validate(child)
    except MemeError: return a


# ------------------------------------------------------------------ enumerative synthesis (observational equivalence)
def step_library(pool, macros=()):
    """All single genes over the task's constant pool, plus map-lifted genes and emergent macro genes."""
    lib = [[{"op": o}] for o in NULLARY]
    chars = [c for c in pool["s"] if len(c) <= 1][:14]
    toks = [t for t in pool["s"] if len(t) > 1][:10]
    for c in chars:
        lib += [[{"op": "split", "args": [c]}], [{"op": "join", "args": [c]}], [{"op": "strip_chars", "args": [c or " "]}]]
        if c: lib.append([{"op": "count", "args": [c]}])
        for d in chars:
            if c and c != d: lib.append([{"op": "replace", "args": [c, d]}])
    for t in chars + toks:
        if t: lib += [[{"op": "prefix", "args": [t]}], [{"op": "suffix", "args": [t]}]]
    for i in pool["i"][:12]:
        lib += [[{"op": "take", "args": [i]}], [{"op": "drop", "args": [i]}], [{"op": "add", "args": [i]}],
                [{"op": "scale", "args": [i]}], [{"op": "round", "args": [i]}]]
    for t in toks: lib.append([{"op": "pluck", "args": [t]}])
    inner = [g for g in lib if g[0]["op"] not in ("map",)]
    lib += [[{"op": "map", "args": [{"pipeline": g}]}] for g in inner[:len(NULLARY) + 40]]
    for m in macros: lib.append(json.loads(canon(m)))
    out, seen = [], set()
    for g in lib:
        try: validate({"pipeline": g})
        except MemeError: continue
        k = canon(g)
        if k not in seen: seen.add(k); out.append(g)
    return out

def potential(v, e):
    """Beam ranking only (never acceptance): how close the CONTENT of an intermediate
    state is to the answer, ignoring type and case."""
    flat = lambda x: x if isinstance(x, str) else "".join(flat(y) for y in x) if isinstance(x, list) else str(x)
    a, b = flat(v), flat(e)
    if not a and not b: return 1.0
    r = difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()
    return max(score_case(v, e), 0.8 * r)

def _apply(genes, value, resolve):
    return run({"pipeline": genes}, value, resolve)

def enumerate_programs(train, pool, macros=(), max_depth=5, beam=3000, resolve=None, counter=None):
    """Breadth-first over pipelines; states with identical outputs on all train inputs are merged.
    Yields train-perfect programs in order of discovery (shortest first)."""
    lib = step_library(pool, macros)
    ins = [c["input"] for c in train]; exp = [c["output"] for c in train]
    sig = lambda vals: canon([[type(v).__name__, v] for v in vals])
    start = {"pipeline": []}; seen = {sig(ins)}; frontier = [(ins, start)]
    if [ (v == e and type(v) == type(e)) for v, e in zip(ins, exp)] == [True] * len(exp): yield start
    for depth in range(max_depth):
        nxt = []
        for vals, prog in frontier:
            for g in lib:
                if len(prog["pipeline"]) + len(g) > MAX_PIPE: continue
                if counter is not None: counter[0] += 1
                try: new = [_apply(g, v, resolve) for v in vals]
                except MemeError: continue
                p = {"pipeline": prog["pipeline"] + json.loads(canon(g))}
                if all(v == e and type(v) == type(e) for v, e in zip(new, exp)):
                    # goal states are NOT merged: train-equivalent programs can generalize
                    # differently, and only the held-out validation may tell them apart
                    yield p
                    continue
                k = sig(new)
                if k in seen: continue
                seen.add(k)
                nxt.append((new, p))
        if not nxt: return
        # type-diverse beam: intermediate states of another type (e.g. a list on the way to a string)
        # score ~0 against the answer, so each type-shape gets its own share of the beam
        groups = {}
        for t in nxt:
            groups.setdefault(tuple(type(v).__name__ for v in t[0]), []).append(t)
        share = max(200, beam // max(1, len(groups)))
        frontier = []
        for g in groups.values():
            g.sort(key=lambda t: (-sum(potential(v, e) for v, e in zip(t[0], exp)), len(t[1]["pipeline"])))
            frontier += g[:share]

def prune(prog, cases, resolve=None):
    """Remove genes that do not change behaviour on the cases (anti-bloat)."""
    p = json.loads(canon(prog)); n = len(cases)
    i = 0
    while i < len(p["pipeline"]):
        q = {"pipeline": p["pipeline"][:i] + p["pipeline"][i + 1:]}
        if evaluate(q, cases, resolve)[1] == n: p = q
        else: i += 1
    return p

# ------------------------------------------------------------------ anti-memes (immune system)
STATIC_POLICY = {"max_pipeline": MAX_PIPE, "max_depth": MAX_DEPTH, "forbidden_ops": []}

def fragments(prog, k_max=3):
    pipe = prog["pipeline"]
    for k in range(1, k_max + 1):
        for i in range(len(pipe) - k + 1):
            yield sha({"frag": pipe[i:i + k]})

# ------------------------------------------------------------------ persistent store
SCHEMA = """
PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS memes(id TEXT PRIMARY KEY, card TEXT NOT NULL, created REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'candidate');
CREATE TABLE IF NOT EXISTS contracts(id TEXT PRIMARY KEY, goal TEXT NOT NULL, context TEXT NOT NULL,
  train TEXT NOT NULL, validation TEXT NOT NULL, holdout TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS active(goal_key TEXT PRIMARY KEY, meme_id TEXT NOT NULL REFERENCES memes(id),
  previous TEXT);
CREATE TABLE IF NOT EXISTS antimemes(signature TEXT PRIMARY KEY, reason TEXT NOT NULL, source TEXT, created REAL);
CREATE TABLE IF NOT EXISTS motifs(id TEXT PRIMARY KEY, genes TEXT NOT NULL, support INTEGER NOT NULL,
  sources TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS foreign_memes(id TEXT PRIMARY KEY, root TEXT, at REAL);
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, data TEXT NOT NULL,
  prev TEXT NOT NULL, hash TEXT NOT NULL, at REAL NOT NULL);
"""

def goal_key(goal, context): return sha({"goal": goal, "context": context or {}})

class MemeOS:
    def __init__(self, path=":memory:", seed=0):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.executescript(SCHEMA)
        self.rng = random.Random(seed)

    # ---- journal
    def event(self, kind, data):
        row = self.db.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        prev = row[0] if row else "0" * 64
        at = time.time(); body = canon(data)
        h = sha(prev + "|" + kind + "|" + body + "|" + repr(at))
        self.db.execute("INSERT INTO events(kind,data,prev,hash,at) VALUES(?,?,?,?,?)", (kind, body, prev, h, at))
        return h

    def integrity(self):
        prev, bad, n = "0" * 64, [], 0
        for seq, kind, data, p, h, at in self.db.execute("SELECT seq,kind,data,prev,hash,at FROM events ORDER BY seq"):
            n += 1
            if p != prev or h != sha(prev + "|" + kind + "|" + data + "|" + repr(at)): bad.append(seq)
            prev = h
        for mid, card in self.db.execute("SELECT id,card FROM memes"):
            if meme_id(json.loads(card)) != mid: bad.append("meme:" + mid)
        return {"ok": not bad, "events": n, "problems": bad, "head": prev}

    # ---- memes
    def resolve(self, mid):
        row = self.db.execute("SELECT card,status FROM memes WHERE id=?", (mid,)).fetchone()
        ensure(row, "unknown_dependency")
        ensure(row[1] != "quarantined", "dependency_quarantined")
        return json.loads(row[0])["genome"]

    def screen(self, card):
        """Anti-meme screening. Returns list of violations (empty = clean)."""
        v = []
        if card.get("schema") == "mos-neuro-meme/v1":
            from neuro import validate_card
            v += validate_card(card)
            r = self.db.execute("SELECT reason FROM antimemes WHERE signature=?", (meme_id(card),)).fetchone()
            if r: v.append("antimeme:" + r[0])
            return v
        if card.get("schema") == "mos-code-meme/v1":
            from sandbox import scan
            v += ["static:" + x for x in scan(card.get("source", ""))]
            for sig in (meme_id(card), sha({"source": card.get("source", "")})):
                r = self.db.execute("SELECT reason FROM antimemes WHERE signature=?", (sig,)).fetchone()
                if r: v.append("antimeme:" + r[0])
            return v
        try: validate(card["genome"])
        except MemeError as e: v.append("static:" + str(e))
        else:
            for f in fragments(card["genome"]):
                r = self.db.execute("SELECT reason FROM antimemes WHERE signature=?", (f,)).fetchone()
                if r: v.append("antimeme:" + r[0])
            whole = self.db.execute("SELECT reason FROM antimemes WHERE signature=?", (meme_id(card),)).fetchone()
            if whole: v.append("antimeme:" + whole[0])
            for d in deps(card["genome"]):
                row = self.db.execute("SELECT status FROM memes WHERE id=?", (d,)).fetchone()
                if not row: v.append("missing_dependency")
                elif row[0] == "quarantined": v.append("quarantined_dependency")
        return v

    def add(self, card):
        bad = self.screen(card)
        ensure(not bad, "rejected_by_antimeme:" + ",".join(bad))
        mid = meme_id(card)
        if not self.db.execute("SELECT 1 FROM memes WHERE id=?", (mid,)).fetchone():
            self.db.execute("INSERT INTO memes(id,card,created) VALUES(?,?,?)", (mid, canon(card), time.time()))
            self.event("meme_recorded", {"id": mid, "parents": card["parents"], "origin": card["origin"]["kind"]})
        return mid

    def card(self, mid): return json.loads(self.db.execute("SELECT card FROM memes WHERE id=?", (mid,)).fetchone()[0])

    # ---- contracts
    def contract(self, goal, train, validation, holdout=(), context=None):
        ins = [sha(c["input"]) for c in list(train) + list(validation) + list(holdout)]
        ensure(len(ins) == len(set(ins)), "split_overlap")
        ensure(len(train) >= 1 and len(validation) >= 1, "need_train_and_validation")
        body = {"goal": goal, "context": context or {}, "train": list(train), "validation": list(validation),
                "holdout": list(holdout)}
        cid = sha(body)
        if not self.db.execute("SELECT 1 FROM contracts WHERE id=?", (cid,)).fetchone():
            self.db.execute("INSERT INTO contracts VALUES(?,?,?,?,?,?,?)", (cid, goal, canon(context or {}),
                            canon(body["train"]), canon(body["validation"]), canon(body["holdout"]), time.time()))
            self.event("contract_frozen", {"id": cid, "train": len(train), "validation": len(validation),
                                           "holdout": len(holdout)})
        return cid

    def _contract(self, cid):
        g, ctx, tr, va, ho = self.db.execute("SELECT goal,context,train,validation,holdout FROM contracts WHERE id=?",
                                             (cid,)).fetchone()
        return {"id": cid, "goal": g, "context": json.loads(ctx), "train": json.loads(tr),
                "validation": json.loads(va), "holdout": json.loads(ho)}

    # ---- promotion gate
    def promote(self, mid, cid):
        c = self._contract(cid); card = self.card(mid)
        ensure(card["goal"] == c["goal"] and card["context"] == c["context"], "goal_or_context_mismatch")
        ensure("genome" in card, "not_mpl_meme")
        from restrict import tainted
        if tainted(self, mid):           # foreign behaviour is admitted only through restrict.adopt_restricted
            self.event("rejected", {"id": mid, "contract": cid, "reason": "foreign_unrestricted"})
            return {"accepted": False, "reason": "foreign_unrestricted", "train": None, "validation": None, "holdout": None}
        g = card["genome"]
        _, tr, tn = evaluate(g, c["train"], self.resolve)
        _, va, vn = evaluate(g, c["validation"], self.resolve)
        key = goal_key(c["goal"], c["context"])
        prior_cs = [r[0] for r in self.db.execute("SELECT id FROM contracts WHERE goal=? AND context=? AND id<>?",
                                                     (c["goal"], canon(c["context"]), cid))]
        regress = []
        for pc in prior_cs:
            p = self._contract(pc)
            if evaluate(g, p["train"] + p["validation"], self.resolve)[1] < len(p["train"]) + len(p["validation"]):
                regress.append(pc)
        act = self.db.execute("SELECT meme_id FROM active WHERE goal_key=?", (key,)).fetchone()
        base = evaluate(self.resolve(act[0]), c["train"] + c["validation"], self.resolve)[1] if act else -1
        perfect = tr == tn and va == vn
        improves = act is None or act[0] == mid or tr + va > base
        ok = perfect and not regress and improves
        reason = "accepted" if ok else ("test_failure" if not perfect else "regression" if regress else "no_improvement")
        holdout = evaluate(g, c["holdout"], self.resolve)[1:] if c["holdout"] else None
        if ok:
            if act and act[0] != mid: self.db.execute("UPDATE memes SET status='superseded' WHERE id=?", (act[0],))
            self.db.execute("INSERT INTO active VALUES(?,?,?) ON CONFLICT(goal_key) DO UPDATE SET meme_id=excluded.meme_id, previous=active.meme_id WHERE active.meme_id<>excluded.meme_id",
                            (key, mid, None))
            self.db.execute("UPDATE memes SET status='active' WHERE id=?", (mid,))
        self.event("promoted" if ok else "rejected", {"id": mid, "contract": cid, "reason": reason,
                                                      "train": [tr, tn], "validation": [va, vn],
                                                      "holdout_report_only": holdout, "regressions": regress})
        return {"accepted": ok, "reason": reason, "train": (tr, tn), "validation": (va, vn), "holdout": holdout}

    def active(self, goal, context=None):
        r = self.db.execute("SELECT meme_id FROM active WHERE goal_key=?", (goal_key(goal, context),)).fetchone()
        return r[0] if r else None

    def execute(self, goal, value, context=None):
        mid = self.active(goal, context); ensure(mid, "no_active_meme")
        out = run(self.resolve(mid), value, self.resolve)
        self.event("executed", {"id": mid, "input": sha(value), "output": sha(out)})
        return out, mid

    # ---- evolution
    def motifs(self, min_support=1):
        return [json.loads(g) for (g,) in self.db.execute("SELECT genes FROM motifs WHERE support>=? ORDER BY support DESC, id LIMIT 64", (min_support,))]

    def evolve(self, cid, *, pop=80, generations=150, seeds=(), use_motifs=True, rng=None, restarts=3,
               enumerate_first=True, enum_depth=4, config=None):
        """Population search on TRAIN only; validation is never shown to the search."""
        rng = rng or self.rng
        cfg = config or self.search_config()
        pop = cfg["pop"] if pop == 80 else pop
        c = self._contract(cid); train = c["train"]; pool = constant_pool(train)
        motifs = self.motifs() if use_motifs else []
        act = self.active(c["goal"], c["context"])
        seed_progs = [s for s in seeds] + ([self.resolve(act)] if act else [])
        evaluations = 0
        cache = {}
        def fit(p):
            nonlocal evaluations
            k = canon(p)
            if k not in cache:
                evaluations += 1
                g, ex, n = evaluate(p, train, self.resolve)
                cache[k] = (g - 0.002 * len(p["pipeline"]), ex, n)
            return cache[k]
        tried = set()
        counter = [0]
        if enumerate_first:
            for p in enumerate_programs(train, pool, motifs, max_depth=enum_depth, resolve=self.resolve, counter=counter):
                p = prune(p, train, self.resolve)
                if canon(p) in tried: continue
                tried.add(canon(p))
                verdict = yield {"program": p, "generation": 0, "restart": -1, "phase": "enumeration",
                                 "evaluations": evaluations + counter[0], "train_exact": [len(train), len(train)]}
                if verdict: return
        evaluations += counter[0]
        for restart in range(restarts):
            P = [json.loads(canon(s)) for s in seed_progs]
            while len(P) < pop:
                n = rng.randint(1, 3)
                P.append(validate({"pipeline": [random_step(rng, pool) for _ in range(n)]}) if rng.random() < cfg["random_init"] or not motifs
                         else validate({"pipeline": json.loads(canon(rng.choice(motifs)))}))
            for gen in range(generations):
                ranked = sorted(P, key=lambda p: (-fit(p)[0], len(p["pipeline"])))
                for p in ranked[:8]:
                    f, ex, n = fit(p)
                    if ex == n:
                        p = prune(p, train, self.resolve)
                        if canon(p) in tried: continue
                        tried.add(canon(p))
                        verdict = yield {"program": p, "generation": gen, "restart": restart, "phase": "evolution",
                                         "evaluations": evaluations, "train_exact": [ex, n]}
                        if verdict: return
                elite = ranked[:cfg["elite"]]
                nxt = [json.loads(canon(e)) for e in elite]
                while len(nxt) < pop:
                    t = lambda: max(rng.sample(P, min(len(P), cfg["tournament"])), key=lambda p: fit(p)[0])
                    child = crossover(t(), t(), rng) if rng.random() < cfg["crossover"] else json.loads(canon(t()))
                    for _ in range(rng.randint(1, cfg["max_mutations"])): child = mutate(child, rng, pool, motifs, cfg["w"])
                    nxt.append(child)
                P = nxt
        yield {"program": None, "evaluations": evaluations}

    def learn(self, cid, max_candidates=12, compose_first=True, formula_first=True, **kw):
        """evolve (train only) -> record meme with lineage -> validation gate -> mine motifs.
        Up to max_candidates train-perfect genomes are gated; each attempt is journaled.
        Holdout is never used for selection, only reported."""
        c = self._contract(cid)
        act = self.active(c["goal"], c["context"])
        if compose_first and not act:
            from emergence import compose
            cr = compose(self, cid, max_len=2, budget=400)
            if cr["ok"]:
                self.event("learned_by_composition", {"contract": cid, "meme": cr["meme"], "of": cr["composed_of"]})
                return {"ok": True, "meme": cr["meme"], "gate": cr["gate"], "program": self.card(cr["meme"]).get("genome") or {"python": True}, "phase": "composition",
                        "evaluations": cr["evaluations"], "generation": 0, "rejected": [], "composed_of": cr["composed_of"]}
        if formula_first and not act:
            from formula import applicable, synthesize
            if applicable(c["train"]) and applicable(c["validation"]):
                fs = synthesize(c["train"], c["validation"])
                if fs["ok"]:
                    card = meme_card({"pipeline": [{"op": "formula", "args": [fs["formula"]]}]}, c["goal"], c["context"],
                                     origin={"kind": "formula_synthesis", "evaluated": fs["evaluated"]})
                    mid = self.add(card); gate = self.promote(mid, cid)
                    self.event("learned_by_formula", {"contract": cid, "meme": mid, "accepted": gate["accepted"]})
                    if gate["accepted"]:
                        return {"ok": True, "meme": mid, "gate": gate, "program": card["genome"], "phase": "formula",
                                "evaluations": fs["evaluated"], "generation": 0, "rejected": []}
        gen = self.evolve(cid, **kw); res = next(gen); rejected = []
        while True:
            if not res["program"]:
                self.event("learn_failed", {"contract": cid, "evaluations": res["evaluations"], "rejected": rejected})
                return {"ok": False, "reason": "no_solution_within_budget" if not rejected else "overfit_candidates_rejected",
                        "evaluations": res["evaluations"], "rejected": rejected}
            card = meme_card(res["program"], c["goal"], c["context"], parents=[act] if act else [],
                             origin={"kind": "evolved", "phase": res.get("phase"), "generation": res["generation"], "evaluations": res["evaluations"]})
            try:
                mid = self.add(card); gate = self.promote(mid, cid)
            except MemeError as e:
                mid, gate = None, {"accepted": False, "reason": str(e)}
            if gate["accepted"]:
                try: gen.send(True)
                except StopIteration: pass
                self.mine(mid)
                return {"ok": True, "meme": mid, "gate": gate, "program": res["program"], "phase": res.get("phase"),
                        "evaluations": res["evaluations"], "generation": res["generation"], "rejected": rejected}
            rejected.append({"meme": mid, "reason": gate["reason"]})
            if len(rejected) >= max_candidates:
                gen.close()
                self.event("learn_failed", {"contract": cid, "evaluations": res["evaluations"], "rejected": rejected})
                return {"ok": False, "reason": "overfit_candidates_rejected", "evaluations": res["evaluations"], "rejected": rejected}
            res = gen.send(False)

    # ---- emergence: motif mining
    def mine(self, mid):
        pipe = self.card(mid)["genome"]["pipeline"]
        for k in (2, 3):
            for i in range(len(pipe) - k + 1):
                genes = pipe[i:i + k]
                if any(s["op"] == "call" for s in genes): continue
                gid = sha({"motif": genes})
                row = self.db.execute("SELECT sources FROM motifs WHERE id=?", (gid,)).fetchone()
                if row:
                    src = set(json.loads(row[0])) | {mid}
                    self.db.execute("UPDATE motifs SET support=?, sources=? WHERE id=?", (len(src), canon(sorted(src)), gid))
                else:
                    self.db.execute("INSERT INTO motifs VALUES(?,?,?,?)", (gid, canon(genes), 1, canon([mid])))
        self.event("motifs_mined", {"from": mid})

    def emergent_report(self):
        rows = self.db.execute("SELECT genes,support,sources FROM motifs ORDER BY support DESC, id LIMIT 10").fetchall()
        return [{"genes": json.loads(g), "support": s, "shared_by_goals": sorted({self.card(m)["goal"] for m in json.loads(src)})}
                for g, s, src in rows]

    # ---- hybridization
    def hybridize(self, a, b, cid, rng=None):
        """Explicit recombination of two existing memes into a child judged by the gate."""
        rng = rng or self.rng
        ga, gb = self.card(a)["genome"], self.card(b)["genome"]
        c = self._contract(cid)
        cands = [{"pipeline": (ga["pipeline"] + gb["pipeline"])[:MAX_PIPE]},
                 {"pipeline": (gb["pipeline"] + ga["pipeline"])[:MAX_PIPE]},
                 {"pipeline": [{"op": "call", "args": [a]}, {"op": "call", "args": [b]}]},
                 {"pipeline": [{"op": "call", "args": [b]}, {"op": "call", "args": [a]}]}]
        for _ in range(40): cands.append(crossover(ga, gb, rng)); cands.append(crossover(gb, ga, rng))
        best = max(cands, key=lambda p: (evaluate(p, c["train"], self.resolve)[1], -len(p["pipeline"])))
        card = meme_card(best, c["goal"], c["context"], parents=[a, b], origin={"kind": "hybrid"})
        mid = self.add(card)
        return {"meme": mid, "program": best, "gate": self.promote(mid, cid)}

    def pin(self, mid, goal, context=None, reason="operator_override"):
        """Operator override: activate a version without the gate (journaled). revalidate() still applies."""
        card = self.card(mid); ensure(card["goal"] == goal and card["context"] == (context or {}), "goal_or_context_mismatch")
        key = goal_key(goal, context)
        self.db.execute("INSERT INTO active VALUES(?,?,NULL) ON CONFLICT(goal_key) DO UPDATE SET previous=active.meme_id, meme_id=excluded.meme_id", (key, mid))
        self.db.execute("UPDATE memes SET status='active' WHERE id=?", (mid,))
        self.event("pinned", {"id": mid, "reason": reason})

    # ---- self-optimization: the search engine's own configuration is under selection
    def search_config(self):
        r = self.db.execute("SELECT m.card FROM active a JOIN memes m ON m.id=a.meme_id WHERE a.goal_key=?",
                            (goal_key("mos.search_config", {}),)).fetchone()
        return json.loads(r[0])["config"] if r else json.loads(canon(DEFAULT_CONFIG))

    def adopt_config(self, config, evidence, parent=None):
        card = {"schema": "mos-config-meme/v1", "name": "search config", "goal": "mos.search_config", "context": {},
                "parents": [parent] if parent else [], "origin": {"kind": "meta_evolution"}, "config": config, "evidence": evidence}
        mid = meme_id(card)
        self.db.execute("INSERT OR IGNORE INTO memes(id,card,created,status) VALUES(?,?,?,'active')", (mid, canon(card), time.time()))
        self.db.execute("INSERT INTO active VALUES(?,?,NULL) ON CONFLICT(goal_key) DO UPDATE SET previous=active.meme_id, meme_id=excluded.meme_id",
                        (goal_key("mos.search_config", {}), mid))
        self.event("config_adopted", {"id": mid, "evidence": evidence})
        return mid

    # ---- anti-meme immune response
    def register_antimeme(self, signature, reason, source=None):
        self.db.execute("INSERT OR IGNORE INTO antimemes VALUES(?,?,?,?)", (signature, reason, source, time.time()))
        self.event("antimeme_registered", {"signature": signature, "reason": reason})

    def revalidate(self, goal, context=None):
        """Re-check the active meme against all contracts; on failure quarantine,
        immunize against its distinctive fragment, and roll back to a parent that passes."""
        key = goal_key(goal, context)
        row = self.db.execute("SELECT meme_id FROM active WHERE goal_key=?", (key,)).fetchone()
        if not row: return {"ok": True, "active": None}
        mid = row[0]
        cs = [r[0] for r in self.db.execute("SELECT id FROM contracts WHERE goal=? AND context=?", (goal, canon(context or {})))]
        def passes(m):
            try: g = self.resolve(m)
            except MemeError: return False
            return all(evaluate(g, self._contract(x)["train"] + self._contract(x)["validation"], self.resolve)[1]
                       == len(self._contract(x)["train"]) + len(self._contract(x)["validation"]) for x in cs)
        if passes(mid): return {"ok": True, "active": mid}
        self.db.execute("UPDATE memes SET status='quarantined' WHERE id=?", (mid,))
        self.register_antimeme(mid, "failed_revalidation", mid)
        lineage = list(self.card(mid)["parents"]); restored = None
        while lineage:
            p = lineage.pop(0)
            st = self.db.execute("SELECT status FROM memes WHERE id=?", (p,)).fetchone()
            if st and st[0] not in ("quarantined",) and passes(p): restored = p; break
            if st: lineage += self.card(p)["parents"]
        if restored:
            self.db.execute("UPDATE active SET meme_id=?, previous=? WHERE goal_key=?", (restored, mid, key))
            self.db.execute("UPDATE memes SET status='active' WHERE id=?", (restored,))
        else:
            self.db.execute("DELETE FROM active WHERE goal_key=?", (key,))
        self.event("rollback", {"failed": mid, "restored": restored})
        return {"ok": False, "quarantined": mid, "restored": restored}

    # ---- transfer
    def export(self, mid):
        seen, order = set(), []
        def walk(m):
            if m in seen: return
            seen.add(m); card = self.card(m)
            for d in (deps(card["genome"]) if "genome" in card else []): walk(d)
            order.append({"id": m, "card": card})
        walk(mid)
        body = {"schema": "mos-meme-bundle/v1", "root": mid, "memes": order}
        return {**body, "sha256": sha(body)}

    def import_bundle(self, bundle):
        """Never trusts sender: verifies hashes, screens with own antimemes, records as candidates."""
        body = {k: bundle[k] for k in ("schema", "root", "memes")}
        ensure(bundle.get("schema") == "mos-meme-bundle/v1" and sha(body) == bundle.get("sha256"), "bundle_tampered")
        for item in bundle["memes"]: ensure(meme_id(item["card"]) == item["id"], "card_hash_mismatch")
        for item in bundle["memes"]:
            dd = deps(item["card"]["genome"]) if "genome" in item["card"] else []
            bad = self.screen(item["card"]) if not any(d in [i["id"] for i in bundle["memes"]] for d in dd) else \
                [x for x in self.screen(item["card"]) if x != "missing_dependency"]
            ensure(not bad, "rejected_by_antimeme:" + ",".join(bad))
        ids = []
        self.db.execute("BEGIN")
        try:
            for item in bundle["memes"]:
                if not self.db.execute("SELECT 1 FROM memes WHERE id=?", (item["id"],)).fetchone():
                    self.db.execute("INSERT INTO memes(id,card,created) VALUES(?,?,?)", (item["id"], canon(item["card"]), time.time()))
                    self.db.execute("INSERT OR IGNORE INTO foreign_memes VALUES(?,?,?)", (item["id"], bundle["root"], time.time()))
                ids.append(item["id"])
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK"); raise
        self.event("bundle_imported", {"root": bundle["root"], "memes": ids, "activated": False})
        return {"imported": ids, "activated": False}

    def state(self):
        q = lambda s: self.db.execute(s).fetchone()[0]
        return {"version": VERSION, "memes": q("SELECT count(*) FROM memes"),
                "active": q("SELECT count(*) FROM active"), "quarantined": q("SELECT count(*) FROM memes WHERE status='quarantined'"),
                "antimemes": q("SELECT count(*) FROM antimemes"), "motifs": q("SELECT count(*) FROM motifs"),
                "contracts": q("SELECT count(*) FROM contracts"), "integrity": self.integrity()}
