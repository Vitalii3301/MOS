"""Build the single-file portable MOS:  python3 build_portable.py <out.zip>"""
import hashlib, json, os, sys, zipfile
here = os.path.dirname(os.path.abspath(__file__)); out = sys.argv[1]
SKIP_DIRS = {"evidence", "__pycache__", "portable"}; SKIP = {"MANIFEST.json", "OFFLINE_MANIFEST.json", "verify_release.py"}
files = {}
for root, dirs, names in os.walk(here):
    dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
    for n in sorted(names):
        rel = os.path.relpath(os.path.join(root, n), here)
        if n in SKIP or n.endswith((".pyc", ".sqlite", ".sqlite-wal", ".sqlite-shm")): continue
        files[rel] = open(os.path.join(root, n), "rb").read()
import memeos
man = {"schema": "mos-portable/v1", "version": memeos.VERSION, "files": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}}
files["OFFLINE_MANIFEST.json"] = json.dumps({"version": memeos.VERSION, "bundle": "portable", "files": dict(man["files"])}, indent=1).encode()
man["files"]["OFFLINE_MANIFEST.json"] = hashlib.sha256(files["OFFLINE_MANIFEST.json"]).hexdigest()
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    def put(name, data):
        zi = zipfile.ZipInfo(name, date_time=(2026, 11, 19, 0, 0, 0)); zi.compress_type = zipfile.ZIP_DEFLATED; zi.external_attr = 0o644 << 16; z.writestr(zi, data)
    put("__main__.py", open(os.path.join(here, "portable", "__main__.py"), "rb").read())
    put("START_HERE.md", open(os.path.join(here, "portable", "START_HERE.md"), "rb").read())
    put("PORTABLE_MANIFEST.json", json.dumps(man, indent=1).encode())
    for k in sorted(files): put("app/" + k, files[k])
print(json.dumps({"out": out, "files": len(files), "bytes": os.path.getsize(out), "sha256": hashlib.sha256(open(out, "rb").read()).hexdigest()}))
