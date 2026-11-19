import hashlib, json, pathlib, sys
root = pathlib.Path(__file__).resolve().parent
m = json.loads((root / "MANIFEST.json").read_text()); errors = []
for f, h in m["files"].items():
    p = root / f
    if not p.exists(): errors.append("missing:" + f)
    elif hashlib.sha256(p.read_bytes()).hexdigest() != h: errors.append("hash:" + f)
print(json.dumps({"ok": not errors, "version": m["version"], "files": len(m["files"]), "errors": errors}, indent=2))
sys.exit(1 if errors else 0)
