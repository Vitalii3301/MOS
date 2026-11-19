"""Own-evidence restriction of foreign skills (defence against trojan skills, SkillJack-style).

Law: a cell executes only the behaviour of a foreign skill that the cell itself has seen exercised on its own
contract (train + validation; holdout stays report-only). Everything else is either removed or made to fail
closed — it never runs silently.

  MPL genome  -> calls are inlined (a payload hidden in a dependency becomes visible), every step that never
                 changed the value on any own case is removed. Removing an idle step changes nothing on the
                 evidence by construction, so the gate result is preserved; off-evidence the hidden step is gone.
  Python      -> (0) no access to the guard names, no `except:`/`except BaseException`; (1) every string/number constant must be explained by the cell's own data (or be a short
                 separator / small number): a literal like "@sberbank.ru" or 100000 that the cell never saw is
                 unexplained knowledge and the skill is refused; (2) every branch outcome (if/while/ternary/
                 and/or/comprehension filters) and every block (incl. except/else/finally) is instrumented; the
                 outcomes seen on own cases are frozen into the source and any other outcome raises
                 MOSUnverified (a BaseException the foreign code cannot catch) at run time.
"""
import ast, json, re
from memeos import run, MemeError, canon, sha, deps

# ------------------------------------------------------------------ provenance
def tainted(k, mid, _seen=None):
    """True if mid or anything it executes/derives from arrived from another cell."""
    _seen = _seen if _seen is not None else set()
    if mid in _seen: return False
    _seen.add(mid)
    if k.db.execute("SELECT 1 FROM foreign_memes WHERE id=?", (mid,)).fetchone(): return True
    row = k.db.execute("SELECT card FROM memes WHERE id=?", (mid,)).fetchone()
    if not row: return False
    card = json.loads(row[0])
    if (card.get("origin") or {}).get("kind") == "restricted_foreign" and not k.db.execute(
            "SELECT 1 FROM foreign_memes WHERE id=?", (mid,)).fetchone():
        return False                                    # produced here by this module: already restricted
    kids = deps(card["genome"]) if "genome" in card else list(card.get("parents") or []) if card.get("schema") == "mos-code-meme/v1" else []
    return any(tainted(k, d, _seen) for d in kids)

def genome_tainted(k, genome):
    return any(tainted(k, d) for d in deps(genome))

# ------------------------------------------------------------------ MPL
def inline(genome, resolve, depth=0):
    if depth > 8: raise MemeError("inline_too_deep")
    out = []
    for st in genome["pipeline"]:
        if st["op"] == "call": out += inline(resolve(st["args"][0]), resolve, depth + 1)["pipeline"]
        elif st["op"] == "map": out.append({"op": "map", "args": [inline(st["args"][0], resolve, depth + 1)]})
        else: out.append(json.loads(canon(st)))
    return {"pipeline": out}

def _trace(pipe, value, used, path=()):
    for i, st in enumerate(pipe):
        p = path + (i,)
        if st["op"] == "map":
            if not isinstance(value, list): raise MemeError("type_list")
            new = [_trace(st["args"][0]["pipeline"], x, used, p) for x in value]
        else:
            new = run({"pipeline": [st]}, value)
        if canon([type(new).__name__, new]) != canon([type(value).__name__, value]): used.add(p)
        value = new
    return value

def _prune(pipe, used, path=()):
    out, removed = [], []
    for i, st in enumerate(pipe):
        p = path + (i,)
        if p not in used: removed.append(st); continue
        if st["op"] == "map":
            inner, r = _prune(st["args"][0]["pipeline"], used, p); removed += r
            st = {"op": "map", "args": [{"pipeline": inner}]}
        out.append(st)
    return out, removed

def restrict_mpl(genome, cases, resolve):
    flat = inline(genome, resolve); used = set()
    for c in cases: _trace(flat["pipeline"], c["input"], used)
    pipe, removed = _prune(flat["pipeline"], used)
    return {"genome": {"pipeline": pipe}, "removed_steps": removed, "inlined": len(deps(genome))}

# ------------------------------------------------------------------ Python
SMALL_NUMBERS = set(range(-16, 17)) | {24, 60, 100, 1000, 1024, 3600}

def _evidence(cases):
    strs, nums = [], set()
    def walk(v):
        if isinstance(v, bool): return
        if isinstance(v, str): strs.append(v.lower())
        elif isinstance(v, (int, float)): nums.add(v); strs.append(str(v))
        elif isinstance(v, list): [walk(x) for x in v]
        elif isinstance(v, dict): [walk(x) for x in list(v.keys()) + list(v.values())]
    for c in cases: walk(c["input"]); walk(c["output"])
    return "\n".join(strs), nums

def _unexplained_words(v, text):
    """Words (3+ letters/digits) of a literal that the evidence never contains. Character ranges (a-z, 0-9) and
    escapes (\\d, \\w, \\s ...) are syntax, not knowledge."""
    core = re.sub(r"\\[A-Za-z]|[^\W_]-[^\W_]", " ", v)
    return [w for w in re.findall(r"[^\W_]{3,}", core) if w.lower() not in text]

def unexplained_literals(source, cases):
    text, nums = _evidence(cases); tree = ast.parse(source); bad = []
    docs = {id(n.body[0].value) for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.Module)) and n.body
            and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant) and isinstance(n.body[0].value.value, str)}
    docs |= {id(c) for r in ast.walk(tree) if isinstance(r, ast.Raise) and r.exc is not None
             for c in ast.walk(r.exc) if isinstance(c, ast.Constant)}          # error messages never reach an output
    for n in ast.walk(tree):
        if not isinstance(n, ast.Constant) or id(n) in docs or isinstance(n.value, bool) or n.value is None: continue
        v = n.value
        if isinstance(v, str):
            if not _unexplained_words(v, text): continue
            bad.append(repr(v)[:40])
        elif isinstance(v, (int, float)):
            if v in SMALL_NUMBERS or v in nums or (float(v).is_integer() and str(int(v)) in text): continue
            bad.append(repr(v))
    return sorted(set(bad))

class _Instrument(ast.NodeTransformer):
    def __init__(self): self.n = 0
    def _id(self): self.n += 1; return self.n
    def _br(self, e): return ast.Call(ast.Name("_mos_b", ast.Load()), [ast.Constant(self._id()), e], [])
    def _blk(self, body):
        return [ast.Expr(ast.Call(ast.Name("_mos_k", ast.Load()), [ast.Constant(self._id())], []))] + body
    def _blocks(self, node, fields):
        for f in fields:
            b = getattr(node, f, None)
            if b: setattr(node, f, self._blk(b))
        return node
    def visit_FunctionDef(self, node):
        self.generic_visit(node); return self._blocks(node, ["body"])
    def visit_If(self, node):
        self.generic_visit(node); node.test = self._br(node.test)
        if not node.orelse: node.orelse = [ast.Pass()]          # make the "not taken" path a block too
        return self._blocks(node, ["body", "orelse"])
    def visit_While(self, node):
        self.generic_visit(node); node.test = self._br(node.test); return self._blocks(node, ["body", "orelse"])
    def visit_For(self, node):
        self.generic_visit(node); return self._blocks(node, ["body", "orelse"])
    def visit_Try(self, node):
        self.generic_visit(node)
        for h in node.handlers: h.body = self._blk(h.body)
        return self._blocks(node, ["body", "orelse", "finalbody"])
    def visit_With(self, node):
        self.generic_visit(node); return self._blocks(node, ["body"])
    def visit_IfExp(self, node):
        self.generic_visit(node); node.test = self._br(node.test); return node
    def visit_BoolOp(self, node):
        self.generic_visit(node); node.values = [self._br(v) for v in node.values[:-1]] + node.values[-1:]; return node
    def visit_comprehension(self, node):
        self.generic_visit(node); node.ifs = [self._br(i) for i in node.ifs]; return node
    def visit_Assert(self, node):
        self.generic_visit(node); node.test = self._br(node.test); return node

UNSUPPORTED = (ast.Match, ast.AsyncFunctionDef, ast.Lambda, ast.Yield, ast.YieldFrom, ast.Await, ast.ClassDef)

def _reserved(tree):
    """Foreign code may not touch the guard machinery or swallow its exception."""
    bad = []
    for n in ast.walk(tree):
        name = n.id if isinstance(n, ast.Name) else n.name if isinstance(n, (ast.FunctionDef, ast.alias)) else n.arg if isinstance(n, ast.arg) else n.attr if isinstance(n, ast.Attribute) else None
        if name and (name.lower().startswith("_mos") or name == "MOSUnverified"): bad.append("reserved_name:" + name)
        if isinstance(n, ast.ExceptHandler) and (n.type is None or any(isinstance(x, ast.Name) and x.id == "BaseException" for x in ast.walk(n.type))):
            bad.append("catches_base_exception")
    return sorted(set(bad))

def _instrumented(source, own_names_ok=False):
    tree = ast.parse(source)
    bad = sorted({type(n).__name__ for n in ast.walk(tree) if isinstance(n, UNSUPPORTED)}) + _reserved(tree)
    if bad: raise MemeError("unverifiable_construct:" + ",".join(bad))
    tree = _Instrument().visit(tree); ast.fix_missing_locations(tree)
    return ast.unparse(tree)

RECORD = ("_MOS_T = set()\n\ndef _mos_b(i, v):\n    _MOS_T.add('b%d:%d' % (i, 1 if v else 0))\n    return v\n\n"
          "def _mos_k(j):\n    _MOS_T.add('k%d' % j)\n\n")
ENFORCE = ("_MOS_OK = frozenset(@@OK@@)\n\ndef _mos_b(i, v):\n    if 'b%d:%d' % (i, 1 if v else 0) not in _MOS_OK:\n"
           "        raise MOSUnverified()\n    return v\n\ndef _mos_k(j):\n    if 'k%d' % j not in _MOS_OK:\n"
           "        raise MOSUnverified()\n\n")

def restrict_python(source, cases, check_literals=True):
    from sandbox import run_source
    lits = unexplained_literals(source, cases) if check_literals else []
    if lits: return {"ok": False, "reason": "unexplained_literals:" + ",".join(lits)[:200]}
    try: body = _instrumented(source)
    except (MemeError, SyntaxError) as e: return {"ok": False, "reason": str(e)[:120]}
    rec = RECORD + body.replace("def meme(", "def _mos_inner(", 1) + \
          "\n\ndef meme(x):\n    _MOS_T.clear()\n    v = _mos_inner(x)\n    return [v, sorted(_MOS_T)]\n"
    r = run_source(rec, [c["input"] for c in cases], require_isolation=True)          # foreign code
    if not r.get("ok"): return {"ok": False, "reason": "trace_failed:" + str(r.get("error"))}
    seen = set()
    for res, c in zip(r["results"], cases):
        if not res["ok"] or res["value"][0] != c["output"] or type(res["value"][0]) != type(c["output"]):
            return {"ok": False, "reason": "fails_own_evidence"}
        seen.update(res["value"][1])
    restricted = ENFORCE.replace("@@OK@@", repr(sorted(seen))) + body
    return {"ok": True, "source": restricted, "verified_outcomes": len(seen)}

# ------------------------------------------------------------------ the single door for foreign behaviour
def adopt_restricted(k, mid, cid, parents=None, label=None):
    """Build this cell's own restricted version of foreign meme `mid` from its OWN contract, record it, gate it."""
    from memeos import meme_card
    from codememes import code_card, CodeMemes
    c = k._contract(cid); card = k.card(mid); ev = c["train"] + c["validation"]
    origin = {"kind": "restricted_foreign", "from": mid, "evidence": len(ev), **({"label": label} if label else {})}
    if card.get("schema") == "mos-code-meme/v1":
        r = restrict_python(card["source"], ev)
        if not r["ok"]:
            k.event("foreign_refused", {"id": mid, "contract": cid, "reason": r["reason"]})
            return {"accepted": False, "reason": "restriction_refused:" + r["reason"]}
        new = code_card(r["source"], c["goal"], c["context"], parents=parents or [mid], origin=origin)
        cm = CodeMemes(k); nid = cm.add(new); gate = cm.promote(nid, cid)
        report = {"substrate": "python", "verified_outcomes": r["verified_outcomes"]}
    else:
        try: r = restrict_mpl(card["genome"], ev, k.resolve)
        except MemeError as e:
            k.event("foreign_refused", {"id": mid, "contract": cid, "reason": str(e)[:120]})
            return {"accepted": False, "reason": "restriction_refused:" + str(e)[:80]}
        new = meme_card(r["genome"], c["goal"], c["context"], parents=parents or [mid], origin=origin)
        nid = k.add(new); gate = k.promote(nid, cid)
        report = {"substrate": "mpl", "removed_steps": r["removed_steps"], "inlined_calls": r["inlined"]}
    k.event("foreign_restricted", {"from": mid, "meme": nid, "accepted": gate["accepted"], **report})
    return {**gate, "meme": nid, "restriction": report}

def adopt_restricted_genome(k, genome, cid, parents, label):
    """Composition of genes some of which are foreign: inline + prune on own evidence, then gate."""
    from memeos import meme_card
    c = k._contract(cid); ev = c["train"] + c["validation"]
    r = restrict_mpl(genome, ev, k.resolve)
    card = meme_card(r["genome"], c["goal"], c["context"], parents=parents,
                     origin={"kind": "restricted_foreign", "label": label, "evidence": len(ev)})
    nid = k.add(card); gate = k.promote(nid, cid)
    k.event("foreign_restricted", {"composition_of": parents, "meme": nid, "accepted": gate["accepted"], "removed_steps": r["removed_steps"]})
    return nid, gate, r["removed_steps"]

def foreign_python_gene(k, mid, idx, cases):
    """Python text for one gene of a cross-substrate composition; foreign genes are restricted first.
    Returns (source or None, refusal reason or None)."""
    from emergence import _python_of
    from compile import compile_genome
    card = k.card(mid)
    if not tainted(k, mid): return _python_of(k, mid, idx), None
    if card.get("schema") == "mos-code-meme/v1":
        lits = unexplained_literals(card["source"], cases)
        return (None, "unexplained_literals:" + ",".join(lits)[:160]) if lits else (_python_of(k, mid, idx), None)
    flat = inline(card["genome"], k.resolve)          # inside a composition its intermediate inputs are not our evidence,
    lits = unexplained_genome_literals(flat, cases)   # so a foreign MPL gene must not carry knowledge our data does not explain
    if lits: return None, "unexplained_literals:" + ",".join(lits)[:160]
    return compile_genome(flat, k.resolve, name=f"_gene{idx}", prefix=f"_c{idx}_"), None

def unexplained_genome_literals(genome, cases):
    text, nums = _evidence(cases); bad = []
    def walk(pipe):
        for st in pipe:
            for a in st.get("args", []):
                if isinstance(a, dict) and "pipeline" in a: walk(a["pipeline"])
                elif isinstance(a, str) and _unexplained_words(a, text): bad.append(repr(a)[:40])
                elif isinstance(a, (int, float)) and not isinstance(a, bool) and a not in SMALL_NUMBERS and a not in nums \
                        and not (float(a).is_integer() and str(int(a)) in text): bad.append(repr(a))
    walk(genome["pipeline"]); return sorted(set(bad))
