"""Emergent analysis (законы МОС: «эмерджентный анализ — скрытые связи между мемами»).

behaviour(m)   : outputs of meme m on a shared probe set (all inputs from all contracts) — what it DOES
equivalents    : memes with different genomes but identical behaviour -> convergent evolution, found by the system
connections    : A -> B when A's outputs are valid inputs for B (B runs without error on most of them) — composability
compose(cid)   : before evolving from scratch, try pipelines of existing memes (call A ; call B [; call C]) on TRAIN;
                 the gate (validation, regressions) decides as for any meme. Parents = the composed memes.
"""
import itertools, json
from memeos import MemeError, run, canon, sha, meme_card, evaluate

def _active_genomes(k):
    """Only memes that PASSED a gate (active or superseded ancestors); rejected candidates are noise."""
    out = {}
    for mid, card in k.db.execute("SELECT id, card FROM memes WHERE status IN ('active','superseded')"):
        c = json.loads(card)
        if "genome" in c: out[mid] = c
    return out

def probes(k, limit=200):
    seen, xs = set(), []
    for tr, va in k.db.execute("SELECT train, validation FROM contracts"):
        for c in json.loads(tr) + json.loads(va):
            key = canon(c["input"])
            if key not in seen: seen.add(key); xs.append(c["input"])
    return xs[:limit]

def behaviour(k, genome, xs):
    sig = []
    for x in xs:
        try: sig.append(canon(run(genome, x, k.resolve)))
        except MemeError: sig.append(None)
    return sig

def analyse(k):
    xs = probes(k); memes = _active_genomes(k)
    beh = {m: behaviour(k, c["genome"], xs) for m, c in memes.items()}
    clusters = {}
    for m, b in beh.items():
        if sum(v is not None for v in b) >= 3: clusters.setdefault(sha(b), []).append(m)
    convergent = []
    for ms in clusters.values():
        gs = {canon(memes[m]["genome"]) for m in ms}
        goals = sorted({memes[m]["goal"] for m in ms})
        if len(gs) > 1:
            convergent.append({"memes": [m[:12] for m in ms], "distinct_genomes": len(gs), "goals": goals,
                               "genomes": [[s["op"] for s in memes[m]["genome"]["pipeline"]] for m in ms[:4]]})
    # hidden identities: an accepted meme C behaves exactly like A followed by B (both different from C)
    act = [m for m, in k.db.execute("SELECT meme_id FROM active") if m in memes]
    identities = []
    for a, b in itertools.permutations(act, 2):
        comp = [None if o is None else run_safe(k, memes[b]["genome"], o) for o in (run_safe(k, memes[a]["genome"], x) for x in xs)]
        valid = sum(v is not None for v in comp)
        if valid < 5: continue
        sig = [None if v is None else canon(v) for v in comp]
        for c in act:
            if c in (a, b): continue
            if beh[c] == sig and memes[c].get("origin", {}).get("kind") != "composition":
                identities.append({"identity": f"{memes[c]['goal']} = {memes[a]['goal']} ∘ {memes[b]['goal']}", "evidence_inputs": valid})
    cross = [cv for cv in convergent if len(cv["goals"]) > 1]
    conns = identities
    return {"memes": len(memes), "probe_inputs": len(xs), "behaviour_clusters": len(clusters),
            "convergent_evolution": convergent, "synonym_goals": cross, "hidden_identities": conns}

def run_safe(k, g, x):
    try: return run(g, x, k.resolve)
    except MemeError: return None

def compose(k, cid, max_len=3, budget=4000):
    """Search pipelines of existing ACTIVE memes; return gate result for the first train-perfect one that passes."""
    c = k._contract(cid)
    act = [m for m, in k.db.execute("SELECT meme_id FROM active") if json.loads(k.db.execute("SELECT card FROM memes WHERE id=?", (m,)).fetchone()[0]).get("genome")]
    act += [m for m, card in k.db.execute("SELECT id, card FROM memes WHERE status='library'")
            if m not in act and "genome" in json.loads(card)]                                     # MPL genes received from other cells
    tried, evals = 0, 0
    for n in range(1, max_len + 1):
        for combo in itertools.permutations(act, n):
            if tried >= budget: break
            tried += 1
            prog = {"pipeline": [{"op": "call", "args": [m]} for m in combo]}
            evals += 1
            if evaluate(prog, c["train"], k.resolve)[1] != len(c["train"]): continue
            try:
                from restrict import genome_tainted, adopt_restricted_genome
                if genome_tainted(k, prog):           # a neighbour's gene is involved: keep only what OUR evidence exercises
                    mid, gate, _ = adopt_restricted_genome(k, prog, cid, list(combo), "composition")
                else:
                    card = meme_card(prog, c["goal"], c["context"], parents=list(combo), origin={"kind": "composition", "of": list(combo)})
                    mid = k.add(card); gate = k.promote(mid, cid)
            except MemeError as e:
                continue
            if gate["accepted"]:
                return {"ok": True, "meme": mid, "composed_of": [json.loads(k.db.execute("SELECT card FROM memes WHERE id=?", (m,)).fetchone()[0])["goal"] for m in combo],
                        "evaluations": evals, "gate": gate}
    return compose_cross(k, cid, evals)

def _python_of(k, mid, idx):
    """Python source for any meme, renaming its entry point to _gene{idx}; MPL is compiled."""
    card = k.card(mid)
    if card.get("schema") == "mos-code-meme/v1":
        import re
        src = card["source"]
        return re.sub(r"\bdef meme\(", f"def _gene{idx}(", src).replace("meme(", f"_gene{idx}(") if "def meme(" in src else None
    from compile import compile_genome
    return compile_genome(card["genome"], k.resolve, name=f"_gene{idx}", prefix=f"_c{idx}_")

def tainted_single(k, m):
    from restrict import tainted
    return tainted(k, m)                             # foreign genes keep their restricted path (pairs below)

def compose_cross(k, cid, evals=0, max_pairs=160):
    """Cross-substrate composition: chains of MPL and Python memes as one Python code meme, gated in the sandbox."""
    from sandbox import evaluate_source
    from codememes import CodeMemes
    pool = [m for m, in k.db.execute("SELECT meme_id FROM active")] + [m for m, in k.db.execute("SELECT id FROM memes WHERE status='library'")]
    pool = [m for m in dict.fromkeys(pool) if k.card(m).get("schema") in ("mos-meme/v1", "mos-code-meme/v1")]   # executable skills only
    created = {m: k.db.execute("SELECT created FROM memes WHERE id=?", (m,)).fetchone()[0] for m in pool}
    pool.sort(key=lambda m: -created[m])                                         # newest genes first: they are why a retry may now succeed
    code = {m for m in pool if k.card(m).get("schema") == "mos-code-meme/v1"}
    if not code: return {"ok": False, "evaluations": evals}
    c = k._contract(cid); cm = CodeMemes(k); tried = 0
    for a in pool:                                   # a single existing code meme first: the same skill asked again, in other words
        if a not in code or tainted_single(k, a): continue
        tried += 1; evals += 1
        sa = _python_of(k, a, 1)
        if not sa: continue
        src = sa + "\n\ndef meme(x):\n    return _gene1(x)\n"
        if evaluate_source(src, c["train"])[0] != len(c["train"]): continue
        r = cm.propose(src, cid, "reuse", parents=[a])
        if r.get("accepted"):
            return {"ok": True, "meme": r["meme"], "composed_of": [k.card(a)["goal"]], "substrates": ["python"], "evaluations": evals, "gate": r}
    for a, b in itertools.permutations(pool, 2):
        if not ({a, b} & code) or tried >= max_pairs: continue
        tried += 1; evals += 1
        from restrict import tainted, restrict_python, foreign_python_gene
        foreign = tainted(k, a) or tainted(k, b)
        if foreign:
            ev = c["train"] + c["validation"]
            (sa, ra), (sb, rb) = foreign_python_gene(k, a, 1, ev), foreign_python_gene(k, b, 2, ev)
            if ra or rb: k.event("foreign_refused", {"composition_of": [a, b], "reason": ra or rb}); continue
        else:
            sa, sb = _python_of(k, a, 1), _python_of(k, b, 2)
        if not sa or not sb: continue
        src = sa + "\n" + sb + "\n\ndef meme(x):\n    return _gene2(_gene1(x))\n"
        if evaluate_source(src, c["train"])[0] != len(c["train"]): continue
        if foreign:                                  # foreign code: unexplained literals refused (per gene), unverified paths fail closed
            rr = restrict_python(src, c["train"] + c["validation"], check_literals=False)
            if not rr["ok"]:
                k.event("foreign_refused", {"composition_of": [a, b], "reason": rr["reason"]}); continue
            from codememes import code_card
            mid = cm.add(code_card(rr["source"], c["goal"], c["context"], parents=[a, b],
                                   origin={"kind": "restricted_foreign", "label": "cross_substrate_composition"}))
            r = {"meme": mid, **cm.promote(mid, cid)}
        else:
            r = cm.propose(src, cid, "cross_substrate_composition", parents=[a, b])
        if r.get("accepted"):
            return {"ok": True, "meme": r["meme"], "composed_of": [k.card(a)["goal"], k.card(b)["goal"]], "substrates":
                    ["python" if m in code else "mpl->python" for m in (a, b)], "evaluations": evals, "gate": r}
    return {"ok": False, "evaluations": evals}
