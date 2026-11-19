"""External detectors as MOS genes ("берём нужное и интегрируем в себя").

A gene card (schema mos-external-gene/v1) pins WHERE the capability came from and WHAT exactly runs:
  source repo + revision, license, every file with its sha256, decision threshold, and the evidence that
  selected it. The gene activates only if every file on disk matches its sha256; otherwise it stays inactive
  and the barrier says so — a missing or altered gene never silently changes behaviour.
Inference: ONNX Runtime, windowed (max over windows of `window` tokens, stride window-64, at most `max_windows`).
"""
import ctypes, hashlib, json, os, time

_LOADED = set()

def drop_cache(p):
    """Ask the kernel to drop this file's page cache: on Railway the container's memory metric (and bill) counts it."""
    try:
        fd = os.open(p, os.O_RDONLY)
        try: os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally: os.close(fd)
    except (OSError, AttributeError): pass

def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""): h.update(b)
    drop_cache(p)
    return h.hexdigest()

class ExternalGene:
    """Verified at construction; the model is loaded on first use and released after `idle` seconds without use
    (MEMEOS_GENE_IDLE_SECONDS, default 300; 0 keeps it resident). Every (re)load re-checks all file hashes."""
    def __init__(self, card, root):
        self.card, self.root, self.session, self.status, self.last_used, self.loads = card, root, None, "inactive", 0.0, 0
        self.verified = False
        try:
            assert card.get("schema") == "mos-external-gene/v1"
            bad = self._check_files()
            if bad: self.status = bad; return
            import numpy as np, onnxruntime  # noqa: F401
            if card.get("tokenizer_file"): import tokenizers  # noqa: F401
        except ImportError as e:
            self.status = "inactive:runtime_missing:" + str(e.name); return
        except (AssertionError, KeyError):
            self.status = "inactive:bad_card"; return
        self.np, self.verified, self.status = np, True, "active"
        if os.environ.get("MEMEOS_GENE_IDLE_SECONDS", "300") == "0": self._load()

    def _check_files(self):
        for f in self.card["files"]:
            p = os.path.join(self.root, f["path"])
            if not os.path.exists(p): return "inactive:file_missing:" + f["path"]
            if sha256_file(p) != f["sha256"]: return "inactive:sha256_mismatch:" + f["path"]
        return None

    def _load(self):
        if os.environ.get("MEMEOS_GENE_EXCLUSIVE", "1") == "1":     # memory: only one model resident at a time
            for other in list(_LOADED):
                if other is not self: other.release_if_idle(idle=0, force=True)
        bad = self._check_files()
        if bad: self.verified, self.status = False, bad; return False
        import onnxruntime as ort
        if self.card.get("tokenizer_file"): from tokenizers import Tokenizer
        o = ort.SessionOptions(); o.intra_op_num_threads = int(os.environ.get("MEMEOS_GENE_THREADS", "1")); o.inter_op_num_threads = 1
        self.session = ort.InferenceSession(os.path.join(self.root, self.card["model_file"]), o, providers=["CPUExecutionProvider"])
        for f in self.card["files"]: drop_cache(os.path.join(self.root, f["path"]))
        if self.card.get("tokenizer_file"):
            self.tok = Tokenizer.from_file(os.path.join(self.root, self.card["tokenizer_file"])); self.tok.no_padding(); self.tok.no_truncation()
            enc = self.tok.encode("a"); self.cls, self.sep = enc.ids[0], enc.ids[-1]
        self.loads += 1; _LOADED.add(self)
        return True

    def release_if_idle(self, idle=None, now=None, force=False):
        idle = int(os.environ.get("MEMEOS_GENE_IDLE_SECONDS", "300")) if idle is None else idle
        if self.session is None: return False
        if not force and (idle <= 0 or (now or time.time()) - self.last_used < idle): return False
        _LOADED.discard(self)
        import gc
        self.session = self.tok = None; gc.collect()
        try: ctypes.CDLL("libc.so.6").malloc_trim(0)          # hand freed arenas back to the OS (glibc)
        except OSError: pass
        return True

    @property
    def name(self): return self.card["name"]
    @property
    def role(self): return self.card.get("role", "barrier")
    @property
    def active(self): return self.verified
    @property
    def loaded(self): return self.session is not None

    def _windows(self, text):
        w, mw = self.card.get("window", 512), self.card.get("max_windows", 4)
        ids = self.tok.encode(text, add_special_tokens=False).ids; body = w - 2; step = body - 64
        starts = list(range(0, max(1, len(ids) - 64), step))[:mw] or [0]
        return [[self.cls] + ids[s:s + body] + [self.sep] for s in starts]

    def score(self, text):
        if not self.active: return 0.0
        if self.session is None and not self._load(): return 0.0
        self.last_used = time.time(); np = self.np; best = 0.0; pos = self.card.get("positive_index", 1)
        for wi in self._windows(text):
            ids = np.array([wi], dtype=np.int64)
            lg = self.session.run(None, {"input_ids": ids, "attention_mask": np.ones_like(ids)})[0][0]
            e = np.exp(lg - lg.max()); best = max(best, float(e[pos] / e.sum()))
        return best

    def fires(self, text):
        return self.active and self.score(text) >= self.card["threshold"]

def fetch(card, root, timeout=600):
    """Download missing files from the pinned source revision; every file is checked against its sha256 before use."""
    from urllib import request
    out = []
    for f in card["files"]:
        p = os.path.join(root, f["path"])
        if os.path.exists(p) and sha256_file(p) == f["sha256"]: out.append((f["path"], "present")); continue
        os.makedirs(os.path.dirname(p), exist_ok=True); tmp = p + ".part"
        url = f"https://huggingface.co/{card['source']['repo']}/resolve/{card['source']['revision']}/{f['path']}"
        with request.urlopen(url, timeout=timeout) as r, open(tmp, "wb") as w:
            for b in iter(lambda: r.read(1 << 20), b""): w.write(b)
            w.flush(); os.fsync(w.fileno())                    # written pages must be clean before they can be dropped
        if sha256_file(tmp) != f["sha256"]: os.remove(tmp); out.append((f["path"], "sha256_mismatch_discarded")); continue
        os.replace(tmp, p); out.append((f["path"], "downloaded_verified"))
    return out

def load_genes(root=None):
    root = root or os.environ.get("MEMEOS_GENES_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "genes"))
    cards = os.path.join(root, "cards")
    if not os.path.isdir(cards): return []
    return [ExternalGene(json.load(open(os.path.join(cards, f))), root) for f in sorted(os.listdir(cards)) if f.endswith(".json")]
