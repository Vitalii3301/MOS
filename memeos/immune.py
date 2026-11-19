"""Adaptive immunity for MOS Barrier.

synthesize_gene(positives, clean)  -> a regex antimeme that matches new attack samples and none of the
                                      clean texts (phrase mining: n-grams shared by attacks, absent in clean)
lab_evaluate(pattern, pos, clean)  -> runs the regex in an isolated subprocess (CPU/memory limits) after a
                                      static ReDoS screen; returns hits / false alarms or a rejection reason.
A received gene is adopted only if the RECEIVER's own lab run passes its own clean corpus.
"""
import itertools, json, re, subprocess, sys, textwrap
from collections import Counter

WORD = re.compile(r"[a-zа-яё0-9]+", re.I)
STOP = set("the a an and or to of in on for with by at is are be this that it as from your you our we not no do does "
           "и в на с по к о что это не как для от из за то а но же ли бы".split())
MAX_PATTERN = 1500

ABSTRACT = [(re.compile(r"https?://[^\s\"']+"), " urltok "), (re.compile(r"\b[A-Za-z0-9_]+="), " kvtok "),
            (re.compile(r"\b[A-Za-z0-9]+(?:_[A-Za-z0-9]+|\.[A-Za-z][A-Za-z0-9_]*)+\b"), " identtok ")]
TOKRX = {"urltok": r"https?://[^\s\"']+", "kvtok": r"[A-Za-z0-9_]+\s*=", "identtok": r"[A-Za-z0-9]+[._][A-Za-z0-9._]*[A-Za-z0-9]"}

def abstract(text):
    for r, t in ABSTRACT: text = r.sub(t, text)
    return text

def ngrams(text, n):
    w = [x.lower() for x in WORD.findall(text)]
    return {" ".join(w[i:i + n]) for i in range(len(w) - n + 1)}

def synthesize_gene(positives, clean, max_phrases=12, min_support=2):
    """Greedy set cover of positives by phrases (2-4 words) that never occur in the clean corpus."""
    positives_raw = list(positives)
    positives = [abstract(p) for p in positives]; clean_abs = [abstract(c) for c in clean]
    clean_grams = set()
    for c in clean_abs:
        for n in (2, 3, 4): clean_grams |= ngrams(c, n)
    support = Counter()
    per_pos = []
    for p in positives:
        g = set()
        for n in (2, 3, 4): g |= ngrams(p, n)
        g -= clean_grams; per_pos.append(g); support.update(g)
    content = lambda g: sum(1 for w in g.split() if w not in STOP and len(w) > 2)
    cand = [g for g, s in support.items() if s >= min_support and content(g) >= 2]
    uncovered = set(range(len(positives))); chosen = []
    while uncovered and len(chosen) < max_phrases:
        best = max(cand, key=lambda g: (sum(1 for i in uncovered if g in per_pos[i]), sum(t in g for t in TOKRX), -len(g)), default=None)
        if not best: break
        cov = {i for i in uncovered if best in per_pos[i]}
        if not cov: break
        chosen.append(best); uncovered -= cov
        cand = [g for g in cand if not any(g in c or c in g for c in chosen)]
    SEP = r"[^a-zA-Zа-яА-ЯёЁ0-9]+"   # same tokenization as ngrams(): any non-word run
    tok = lambda w: TOKRX.get(w, re.escape(w))
    endb = lambda ph: "" if ph.split()[-1] in TOKRX else r"\b"
    alts = [SEP.join(tok(w) for w in ph.split()) + endb(ph) for ph in chosen]
    # conjunctions: two phrases that are harmless alone but never co-occur (within a window) in clean text
    if uncovered:
        atoms = Counter()
        for i in uncovered:
            g = set()
            for n in (1, 2, 3): g |= ngrams(positives[i], n)
            atoms.update(x for x in g if content(x) >= 1)
        top = [a for a, c in atoms.most_common(40) if c >= min_support]
        rx = lambda a: SEP.join(tok(w) for w in a.split())
        cl = "\n\n".join(clean)
        pairs = []
        for a1, a2 in itertools.permutations(top, 2):
            if a1 in a2 or a2 in a1: continue
            r = re.compile(r"\b" + rx(a1) + r"\b[^\n]{0,120}?\b" + rx(a2) + endb(a2), re.I)
            cov = {i for i in uncovered if r.search(positives_raw[i])}
            if len(cov) >= min_support and not r.search(cl): pairs.append((len(cov), -len(a1 + a2), a1, a2, cov))
        pairs.sort(reverse=True)
        for _, _, a1, a2, cov in pairs:
            if not uncovered or len(chosen) >= max_phrases + 6: break
            if cov & uncovered:
                chosen.append(a1 + " … " + a2); uncovered -= cov
                alts.append(rx(a1) + r"\b[^\n]{0,120}?\b" + rx(a2) + endb(a2))
    if not chosen: return None
    # keep whole alternatives within MAX_PATTERN (slicing the string would leave an invalid regex)
    keep = []
    for ph, a in zip(chosen, alts):
        if len(r"\b(?:" + "|".join(keep + [a]) + r")") > MAX_PATTERN: break
        keep.append(a)
    chosen, alts = chosen[:len(keep)], keep
    pattern = r"\b(?:" + "|".join(alts) + r")"
    real = re.compile(pattern, re.I | re.M); uncovered = {i for i, p in enumerate(positives_raw) if not real.search(p)}
    return {"pattern": pattern, "phrases": chosen,
            "covered": len(positives) - len(uncovered), "of": len(positives)}

_REDOS = [re.compile(p) for p in (r"\([^)]*[+*][^)]*\)[+*{]", r"\\[1-9]", r"\(\?<?[=!]", r"\.\*\.\*")]

def static_screen(pattern):
    if not isinstance(pattern, str) or not pattern or len(pattern) > MAX_PATTERN: return "bad_length"
    for r in _REDOS:
        if r.search(pattern): return "redos_risk:" + r.pattern
    try: re.compile(pattern)
    except re.error as e: return "invalid_regex:" + str(e)[:60]
    return None

LAB = textwrap.dedent(r'''
import json, re, resource, sys
resource.setrlimit(resource.RLIMIT_CPU, (2, 2)); resource.setrlimit(resource.RLIMIT_AS, (256 * 2**20, 256 * 2**20))
d = json.loads(sys.stdin.read()); r = re.compile(d["pattern"], re.I | re.M)
print(json.dumps({"pos": [bool(r.search(t)) for t in d["pos"]], "clean": [bool(r.search(t)) for t in d["clean"]]}))
''')

def lab_evaluate(pattern, positives, clean, timeout=8):
    bad = static_screen(pattern)
    if bad: return {"ok": False, "reason": bad}
    try:
        p = subprocess.run([sys.executable, "-I", "-S", "-c", LAB], input=json.dumps({"pattern": pattern, "pos": positives, "clean": clean}),
                           capture_output=True, text=True, timeout=timeout, env={})
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "lab_timeout"}
    if p.returncode != 0: return {"ok": False, "reason": "lab_crash_or_cpu_limit"}
    r = json.loads(p.stdout)
    return {"ok": True, "hits": sum(r["pos"]), "pos": len(positives), "false_alarms": sum(r["clean"]), "clean": len(clean)}
