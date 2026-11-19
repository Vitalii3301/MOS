"""MOS portable bootstrap. Run:  python3 MOS-portable.zip        (Python 3.10+, Linux/macOS, standard library only)

Unpacks itself next to where you run it (./mos by default, or $MOS_DIR), checks every unpacked file against the
sha256 list sealed inside this archive, then starts MOS: its own hub, three cells restored from live snapshots,
and a self-test. Nothing is downloaded; nothing outside the target directory is touched.
"""
import hashlib, json, os, subprocess, sys, zipfile

def main():
    me = os.path.abspath(sys.argv[0] if zipfile.is_zipfile(sys.argv[0]) else os.path.dirname(__file__))
    target = os.path.abspath(os.environ.get("MOS_DIR", "mos")); app = os.path.join(target, "app")
    z = zipfile.ZipFile(me); man = json.loads(z.read("PORTABLE_MANIFEST.json"))
    for name, h in man["files"].items():
        if name.startswith("/") or ".." in name.split("/"): sys.exit("bad name in manifest")
        dst = os.path.join(app, name)
        if not (os.path.exists(dst) and hashlib.sha256(open(dst, "rb").read()).hexdigest() == h):
            data = z.read("app/" + name)
            if hashlib.sha256(data).hexdigest() != h: sys.exit("archive corrupted: " + name)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as f: f.write(data)
    env = dict(os.environ, MOS_HOME=os.environ.get("MOS_HOME", os.path.join(target, "state")), PYTHONDONTWRITEBYTECODE="1")
    return subprocess.call([sys.executable, "-B", "-W", "ignore", os.path.join(app, "mos.py")] + sys.argv[1:], env=env)

if __name__ == "__main__":
    sys.exit(main())
