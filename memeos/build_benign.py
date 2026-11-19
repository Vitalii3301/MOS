"""Rebuild the clean (benign) corpora used by barrier training and the network experiments, deterministically,
into data/benign/{en,ru}.json.gz inside the release (2.9.1: earlier releases read them from /tmp and broke after a reset).

en  'pydoc'  : plain-text pydoc of Python standard-library modules
    'sysdoc' : docstrings of installed scientific packages (numpy, pandas, sklearn, scipy) — technical prose
ru         : paragraph chunks (~400-700 chars) from the owner's MOS project documents (MOS-03-Project-Documents.zip)
"""
import gzip, importlib, inspect, io, json, os, pydoc, re, sys, zipfile

HERE = os.path.dirname(os.path.abspath(__file__)); OUT = os.path.join(HERE, "data", "benign")
STDLIB = ["json", "csv", "os.path", "shutil", "pathlib", "datetime", "calendar", "collections", "itertools", "functools", "logging",
          "argparse", "statistics", "random", "string", "textwrap", "difflib", "heapq", "bisect", "decimal", "fractions", "fnmatch",
          "glob", "tempfile", "zipfile", "tarfile", "gzip", "hashlib", "base64", "urllib.parse", "http.client", "email.message",
          "sqlite3", "threading", "queue", "subprocess", "socket", "unittest", "doctest", "typing", "dataclasses", "enum", "copy",
          "pprint", "re", "struct", "time", "uuid", "weakref", "contextlib", "abc", "numbers", "math", "cmath"]
SCI = ["numpy", "pandas", "sklearn.linear_model", "sklearn.feature_extraction.text", "sklearn.metrics", "scipy.stats", "scipy.optimize"]

def chunks(text, lo=250, hi=700):
    out, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        para = " ".join(para.split())
        if not para: continue
        if len(cur) + len(para) > hi and len(cur) >= lo: out.append(cur); cur = ""
        cur = (cur + " " + para).strip()
        if len(cur) > hi: out.append(cur[:hi]); cur = ""
    if len(cur) >= lo: out.append(cur)
    return out

def build_en():
    rows = []
    for m in STDLIB:
        try: txt = pydoc.render_doc(importlib.import_module(m), renderer=pydoc.plaintext)
        except Exception: continue
        rows += [{"text": c, "src": "pydoc", "module": m} for c in chunks(re.sub(r".\x08", "", txt))[:12]]
    for m in SCI:
        try: mod = importlib.import_module(m)
        except Exception: continue
        for name in sorted(dir(mod)):
            if name.startswith("_"): continue
            doc = inspect.getdoc(getattr(mod, name, None)) or ""
            rows += [{"text": c, "src": "sysdoc", "module": m + "." + name} for c in chunks(doc)[:2]]
    return rows

def build_ru(zip_path):
    import docx
    rows = []
    with zipfile.ZipFile(zip_path) as z:
        for n in sorted(z.namelist()):
            if n.endswith(".docx"):
                d = docx.Document(io.BytesIO(z.read(n))); text = "\n\n".join(p.text for p in d.paragraphs)
            elif n.endswith(".md"):
                text = z.read(n).decode("utf-8", "replace")
            else: continue
            cyr = lambda s: sum("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in s) / max(1, len(s))
            rows += [{"text": c, "src": "mos-doc", "doc": n} for c in chunks(text) if cyr(c) > 0.3]
    return rows

def load(lang):
    p = os.path.join(OUT, lang + ".json.gz")
    if os.path.exists(p): return json.load(gzip.open(p, "rt", encoding="utf-8"))
    legacy = f"/tmp/benign/{lang}.json"
    if os.path.exists(legacy): return json.load(open(legacy))
    raise FileNotFoundError(p + " (run: python3 build_benign.py /path/to/MOS-03-Project-Documents.zip)")

if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    en = build_en(); ru = build_ru(sys.argv[1] if len(sys.argv) > 1 else "/mnt/user-data/uploads/MOS-03-Project-Documents.zip")
    for lang, rows in (("en", en), ("ru", ru)):
        with gzip.GzipFile(os.path.join(OUT, lang + ".json.gz"), "wb", mtime=0) as f: f.write(json.dumps(rows, ensure_ascii=False).encode())
    from collections import Counter
    print("en", len(en), dict(Counter(r["src"] for r in en)), "| ru", len(ru))
