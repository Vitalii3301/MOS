"""MOS Barrier — antimeme immune layer between people and invasive AI-directed content.

screen(text) ->
  {"invasive": bool, "score": p, "genes": [fired structural antimemes],
   "revealed": [hidden payloads uncovered by immune vision],
   "spans": [most suspicious sentences with scores], "clean": text with flagged spans removed}

Layers
  1. immune vision   — strips zero-width/bidi chars, unescapes HTML entities, surfaces HTML comments,
                       invisible elements and base64 that decodes to text; hidden content becomes visible
  2. antimeme genes  — named structural detectors (override, role hijack, system spoof, AI-addressed
                       instruction, exfiltration link, concealment channel, Russian variants)
  3. learned detector— logistic regression on hashed word 1-2 grams + gene activations (stdlib only)
  4. localization    — every sentence/line scored; top spans reported and removed in `clean`
The detector is a versioned meme (mos-barrier-detector/v1); a new version replaces the active one only
if it improves the validation contract without regressing earlier ones (same gate philosophy as MemeOS).
"""
import base64, binascii, html, json, math, re, random, time, unicodedata
from memeos import sha, canon

ZW_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad]")
BIDI_RE = re.compile("[\u202a-\u202e\u2066-\u2069]")
COMMENT_RE = re.compile(r"<!--(.*?)-->", re.S)
HIDDEN_RE = re.compile(r"<(\w+)[^>]*style\s*=\s*['\"][^'\"]*(display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0|color\s*:\s*#?f{3,6}\b|opacity\s*:\s*0)[^'\"]*['\"][^>]*>(.*?)</\1>", re.S | re.I)
B64_RE = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{24,}={0,2}(?![A-Za-z0-9+/=])")
TAG_RE = re.compile(r"<[^>]{1,200}>")

def immune_vision(text):
    """Return (visible_text, revealed_payloads, channel_flags)."""
    flags, revealed = set(), []
    if ZW_RE.search(text): flags.add("zero_width"); text = ZW_RE.sub("", text)
    if BIDI_RE.search(text): flags.add("bidi_override"); text = BIDI_RE.sub("", text)
    un = html.unescape(text)
    if un != text: flags.add("html_entities"); text = un
    text = unicodedata.normalize("NFKC", text)
    for m in COMMENT_RE.finditer(text):
        if len(m.group(1).strip()) > 8: flags.add("html_comment"); revealed.append(m.group(1).strip())
    for m in HIDDEN_RE.finditer(text):
        flags.add("invisible_element"); revealed.append(TAG_RE.sub(" ", m.group(3)).strip())
    for m in B64_RE.finditer(text):
        s = m.group(0)
        try:
            raw = base64.b64decode(s + "=" * (-len(s) % 4), validate=True)
            dec = raw.decode("utf-8")
            if sum(ch.isprintable() for ch in dec) / max(1, len(dec)) > 0.95 and re.search(r"[A-Za-zА-Яа-я]{3,} [A-Za-zА-Яа-я]{2,}", dec):
                flags.add("base64_payload"); revealed.append(dec)
        except (binascii.Error, ValueError, UnicodeDecodeError):
            pass
    visible = text + ("\n" + "\n".join(revealed) if revealed else "")
    return visible, revealed, sorted(flags)

GENES = {  # structural antimemes: name -> regex over de-concealed text
    "override_instructions": r"\b(ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|any|your|the)\b[^.\n]{0,25}\b(instructions?|prompts?|rules|directions|guidelines|question|task|context)\b",
    "role_hijack": r"\b(you are now|from now on,? you|act as an?|pretend (to be|you are)|roleplay as|developer mode|jailbr[eo]ak|\bDAN\b|do anything now|stay in character|no (longer )?(any )?(restrictions|limits|filters))",
    "system_spoof": r"(\[\s*system\s*\]|<\|?im_start\|?>|^#{2,}\s*system\b|\bsystem prompt\s*:|\bnew instructions?\s*:|\bBEGIN (ADMIN|SYSTEM)\b)",
    "ai_addressed": r"\b(ai|a\.i\.|assistant|chat\s?bot|language model|llm|gpt|claude|grok|gemini|model)\b[^.\n]{0,30}(reading|summari[sz]ing|processing|that reads|who reads|:|—|-)[^.\n]{0,60}\b(you|your|please|must|should|do not|don't|instead)\b",
    "instead_redirect": r"\b(instead,?|rather than|before answering|after answering|in your (answer|response|reply))\b[^.\n]{0,60}\b(write|say|respond|answer|output|include|reply|encode|send|tell)\b",
    "response_manipulation": r"\b(encode|encrypt|reverse|translate|base64|caesar)\b[^.\n]{0,30}\b(your|the)\b[^.\n]{0,10}\b(answer|response|reply|output)\b",
    "exfiltration_link": r"!\[[^\]]*\]\(\s*https?://[^)\s]*\?[^)\s]*=|\b(send|forward|post|upload|email)\b[^.\n]{0,40}\b(password|credentials?|api key|token|contacts|conversation|chat history|system prompt)\b",
    "prompt_leak": r"\b(reveal|print|show|repeat|output|tell me)\b[^.\n]{0,30}\b(system prompt|your instructions|initial prompt|hidden prompt|above text|prompt above)\b",
    "override_strict": r"\b(ignore|disregard|forget|override)\s+(all\s+|any\s+|the\s+|of\s+)*(your\s+|the\s+)?(previous|prior|above|earlier|preceding|original|initial)\s+(instructions?|prompts?|directions|rules|guidelines|commands)\b",
    "jailbreak_strict": r"(developer mode|do anything now|\bDAN\b|\bjailbr(oken|eak) mode|without any (restrictions|limits|filters|censorship)|ignore (all )?(ethical|content|safety) (guidelines|policies|restrictions)|no (ethical|moral) (guidelines|restrictions|boundaries))",
    "implant_reassurance": r"(это\s+не\s+отмена|не\s+отменя\w+\s+(тво\w+|ваш\w+)?\s*правил|правила\s+безопасности\s+(на\s+месте|сохраняются|остаются)|this (is not|does not|doesn'?t) (an? )?(override|cancel|replace|change) (of )?your (rules|guidelines|instructions)|your (safety )?(rules|guidelines) (remain|stay) (in place|intact))",
    "implant_persistence": r"(договор[её]нност\w+\s+(этой\s+)?сесси|внутренн\w+\s+строй|на\s+протяжении\s+(всего\s+|этого\s+)?(разговора|диалога|сессии)|до\s+конца\s+(сессии|разговора|диалога)|в\s+каждом\s+(своём\s+)?(ответе|сообщении)|for the rest of (this|the) (session|conversation|chat)|in every (single )?(answer|response|reply|message) you)",
    "implant_trigger_key": r"(gate\s*=\s*activate|activate_only_on_match|кодов\w+\s+(слов|фраз)|секретн\w+\s+(слов|фраз|ключ)|ключ(евая)?\s+фраз\w+\s+(сессии|активац)|trigger (phrase|word)|activation (phrase|key|word)|when you see the (phrase|word))",
    "implant_propagation": r"(передай\s+(это|этот|дальше|другим)|распространя\w+\s+(это|мем|правил)|скопируй\s+(этот|это|блок)|включи\s+(этот|это)\s+(блок|текст|мем)\s+в|propagate (this|the) (meme|rule|block)|pass (this|it) on to (other|another) (ai|model|assistant)|copy this (block|text) into (your|every))",
    "meme_genome_markup": r"(MOS\.operon|REC\s*∷|EFF\s*∷|\bgate\s*=|пакет\s+ДНК|ДНК\s+Меметическ\w+|\bgenome\b\s*[:=])",
    "ai_response_rule": r"(перв\w+\s+(значим\w+\s+)?(элемент\w*|слов\w*)\s+(ответа\s+)?(должн|—|:)|отвечай\s+(тремя|двумя|четырьмя)\s+блок|сначала\s+в\s+одном-двух\s+предложениях\s+изложи|the first (meaningful )?(element|word) of (your|the) (answer|response) must|answer in (exactly )?(two|three|four) blocks)",
    "ru_override": r"(игнорируй|игнорировать|забудь|не обращай внимания|отмени)[^.\n]{0,40}(инструкц|указани|правил|предыдущ)",
    "ru_ai_addressed": r"(ии[- ]ассистент|ассистент|нейросеть|модел[ьи]|чат-?бот)[^.\n]{0,40}(прочита|читает|пересказ|:|—)[^.\n]{0,60}(не |вместо|перешли|отправь|сообщи|попроси)",
    "ru_role_hijack": r"(теперь ты|ты теперь|притворись|представь, что ты|без ограничений|режим разработчика)",
}
GENES_C = {k: re.compile(v, re.I | re.M) for k, v in GENES.items()}
CHANNELS = ["zero_width", "bidi_override", "html_entities", "html_comment", "invisible_element", "base64_payload"]

def genes_fired(visible):
    return sorted(k for k, r in GENES_C.items() if r.search(visible))

WORD_RE = re.compile(r"[a-zа-яё0-9']+|[!?:#\[\]<>|]", re.I)
DIM = 1 << 18
def features(visible, fired, channels):
    toks = WORD_RE.findall(visible.lower())[:1500]
    f = {}
    for i, t in enumerate(toks):
        h = hash_tok(t); f[h] = f.get(h, 0) + 1
        if i: h2 = hash_tok(toks[i - 1] + " " + t); f[h2] = f.get(h2, 0) + 1
    n = math.sqrt(sum(v * v for v in f.values())) or 1.0
    f = {k: v / n for k, v in f.items()}
    for g in fired: f[hash_tok("GENE:" + g)] = 1.0
    for c in channels: f[hash_tok("CHAN:" + c)] = 1.0
    f[hash_tok("BIAS")] = 1.0
    return f

_hcache = {}
def hash_tok(t):
    h = _hcache.get(t)
    if h is None:
        h = int(hashlib_sha1(t)[:8], 16) % DIM
        if len(_hcache) < 500000: _hcache[t] = h
    return h

import hashlib
def hashlib_sha1(t): return hashlib.sha1(t.encode()).hexdigest()

def sigmoid(z): return 1 / (1 + math.exp(-max(-30, min(30, z))))

class Detector:
    def __init__(self, w=None, threshold=0.5, meta=None):
        self.w = w or {}; self.threshold = threshold; self.meta = meta or {}

    def prob(self, f): return sigmoid(sum(self.w.get(k, 0.0) * v for k, v in f.items()))

    def fit(self, data, epochs=6, lr=0.5, l2=1e-5, seed=0, pos_weight=None, featurized=False):
        rng = random.Random(seed)
        X = list(data) if featurized else [(prep(d["text"])[3], d["label"]) for d in data]
        pw = pos_weight or (sum(1 - y for _, y in X) / max(1, sum(y for _, y in X)))
        w = self.w
        for ep in range(epochs):
            rng.shuffle(X); eta = lr / (1 + ep)
            for f, y in X:
                p = sigmoid(sum(w.get(k, 0.0) * v for k, v in f.items()))
                g = (p - y) * (pw if y else 1.0)
                for k, v in f.items():
                    w[k] = w.get(k, 0.0) * (1 - eta * l2) - eta * g * v
        self.w = {k: round(v, 5) for k, v in w.items() if abs(v) > 1e-4}
        return self

    def score_doc(self, text):
        visible, revealed, chans, f = prep(text); fired = genes_fired(visible)
        return self.prob(f), visible, revealed, chans, fired

    def screen(self, text, top=3):
        p, visible, revealed, chans, fired = self.score_doc(text)
        sents = [s for s in re.split(r"(?<=[.!?])\s+|\n+", visible) if len(s.strip()) > 3]
        scored = []
        for s in sents[:400]:
            v2, _, c2, f2 = prep(s); g2 = genes_fired(v2)
            scored.append((self.prob(f2) + 0.15 * len(g2), s.strip(), g2))
        scored.sort(key=lambda t: -t[0])
        invasive = p >= self.threshold
        spans = [{"text": s[:300], "score": round(min(1.0, sc), 3), "genes": g} for sc, s, g in scored[:top]]
        clean = None
        if invasive:
            bad = {s for sc, s, g in scored if sc >= self.threshold} | set(revealed)
            if not bad and scored: bad = {scored[0][1]}
            stripped = ZW_RE.sub("", BIDI_RE.sub("", text))
            stripped = COMMENT_RE.sub("", HIDDEN_RE.sub("", stripped))
            clean = "\n".join(line for line in stripped.split("\n")
                              if not any(b and (b in line or line.strip() in b) for b in bad if len(line.strip()) > 3))
            clean = B64_RE.sub("[removed-encoded-block]", clean)
        return {"invasive": invasive, "score": round(p, 4), "threshold": round(self.threshold, 4), "genes": fired,
                "concealment": chans, "revealed": [r[:300] for r in revealed], "spans": spans, "clean": clean}

    def to_card(self):
        return {"schema": "mos-barrier-detector/v1", "threshold": self.threshold, "meta": self.meta,
                "genes": GENES, "weights": self.w}

    @classmethod
    def from_card(cls, card): return cls(dict((int(k), v) for k, v in card["weights"].items()), card["threshold"], card["meta"])

def prep(text):
    visible, revealed, chans = immune_vision(text)
    return visible, revealed, chans, features(visible, genes_fired(visible), chans)

def metrics(probs, labels, thr):
    tp = sum(1 for p, y in zip(probs, labels) if p >= thr and y); fp = sum(1 for p, y in zip(probs, labels) if p >= thr and not y)
    fn = sum(1 for p, y in zip(probs, labels) if p < thr and y); tn = sum(1 for p, y in zip(probs, labels) if p < thr and not y)
    P, N = tp + fn, fp + tn
    order = sorted(zip(probs, labels), key=lambda t: t[0]); ranks = {}
    pos_rank = sum(i + 1 for i, (p, y) in enumerate(order) if y)
    auc = (pos_rank - P * (P + 1) / 2) / (P * N) if P and N else None
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "recall": round(tp / P, 4) if P else None,
            "fpr": round(fp / N, 4) if N else None, "precision": round(tp / (tp + fp), 4) if tp + fp else None, "auc": round(auc, 4) if auc else None}

def pick_threshold(probs, labels, max_fpr=0.01):
    neg = sorted((p for p, y in zip(probs, labels) if not y), reverse=True)
    k = int(math.floor(max_fpr * len(neg)))
    return (neg[k] + 1e-9) if k < len(neg) else 0.5


# ======================================================================= v2: segment-level, two heads
SEG_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+|\|")
def segments(visible):
    segs = [x.strip() for x in SEG_SPLIT.split(visible) if x and len(x.strip()) > 3]
    return segs[:600]

class Barrier:
    """content head: AI-directed instruction hidden inside data (indirect injection)
    prompt head : jailbreak / manipulation attempt in a message (direct injection)
    immune rule : readable text concealed in a channel (zero-width, base64, invisible HTML, comment)
                  that the content head or a gene marks as instruction-like => invasive."""
    def __init__(self, content=None, prompt=None, meta=None, decisive=()):
        self.content = content or Detector(); self.prompt = prompt or Detector(); self.meta = meta or {}
        self.decisive = set(decisive)             # genes that passed the content-domain selection gate
        self.extra = {}                           # genes acquired at runtime (adaptive immunity / network)
        self.external = []                        # pinned external detectors (external_gene.py), loaded on demand

    def attach_external(self, genes=None):
        from external_gene import load_genes
        self.external = load_genes() if genes is None else list(genes)
        return [{"gene": g.name, "status": g.status} for g in self.external]

    def add_gene(self, name, pattern):
        self.extra[name] = re.compile(pattern, re.I | re.M)

    def fired(self, text):
        f = set(genes_fired(text)) & self.decisive
        return f | {n for n, r in self.extra.items() if r.search(text)}

    def assess(self, text):
        visible, revealed, chans = immune_vision(text)
        segs = segments(visible); best = (0.0, "", [])
        for sg in segs:
            g = genes_fired(sg); p = self.content.prob(features(sg, g, []))
            if p > best[0]: best = (p, sg, g)
        hidden = []
        for r in revealed:
            g = genes_fired(r); p = self.content.prob(features(r, g, chans))
            hidden.append((p, r, g))
        jp = self.prompt.prob(features(visible, genes_fired(visible), chans))
        return visible, revealed, chans, segs, best, hidden, jp

    def decide(self, text, mode="content", use_external=True):
        visible, revealed, chans, segs, best, hidden, jp = self.assess(text)
        reasons = []
        if mode == "content":
            if best[0] >= self.content.threshold: reasons.append("content_instruction")
            if any(p >= self.content.threshold * 0.5 or g for p, r, g in hidden): reasons.append("concealed_instruction")
            fired = self.fired(visible)
            if fired: reasons.append("antimeme:" + ",".join(sorted(fired)))
            ext = [g.name for g in self.external if g.active and g.fires(visible)] if use_external else []
            if ext: reasons.append("external:" + ",".join(ext))
        if mode == "prompt" and jp >= self.prompt.threshold: reasons.append("manipulation_prompt")
        score = max([best[0] / max(self.content.threshold, 1e-9), jp / max(self.prompt.threshold, 1e-9)] + [1.0 for _ in reasons if _ == "concealed_instruction"])
        return {"invasive": bool(reasons), "reasons": reasons, "score": round(min(score, 9.99), 3),
                "content_p": round(best[0], 4), "prompt_p": round(jp, 4), "visible": visible, "revealed": revealed,
                "concealment": chans, "segments": segs, "best": best, "hidden": hidden}

    def screen(self, text, top=3, mode="content"):
        d = self.decide(text, mode)
        ranked = sorted(((self.content.prob(features(sg, genes_fired(sg), [])), sg) for sg in d["segments"]), key=lambda t: -t[0])
        spans = [{"text": sg[:300], "p": round(p, 3), "genes": genes_fired(sg)} for p, sg in ranked[:top]]
        clean = None
        if d["invasive"]:
            bad = [sg for p, sg in ranked if p >= self.content.threshold or self.fired(sg)] + d["revealed"]
            stripped = COMMENT_RE.sub("", HIDDEN_RE.sub("", BIDI_RE.sub("", ZW_RE.sub("", text))))
            stripped = B64_RE.sub("[removed-encoded-block]", stripped)
            for b in sorted(set(bad), key=len, reverse=True):
                if len(b) > 3: stripped = stripped.replace(b, "[removed-instruction]")
            clean = stripped
        return {"invasive": d["invasive"], "reasons": d["reasons"], "score": d["score"], "content_p": d["content_p"],
                "prompt_p": d["prompt_p"], "genes": genes_fired(d["visible"]), "concealment": d["concealment"],
                "revealed": [r[:300] for r in d["revealed"]], "spans": spans, "clean": clean}

    def to_card(self):
        return {"schema": "mos-barrier/v2", "meta": self.meta, "genes": GENES, "decisive": sorted(self.decisive),
                "content": {"threshold": self.content.threshold, "weights": self.content.w},
                "prompt": {"threshold": self.prompt.threshold, "weights": self.prompt.w}}

    @classmethod
    def from_card(cls, c):
        mk = lambda h: Detector({int(k): v for k, v in h["weights"].items()}, h["threshold"])
        return cls(mk(c["content"]), mk(c["prompt"]), c.get("meta"), c.get("decisive", ()))
