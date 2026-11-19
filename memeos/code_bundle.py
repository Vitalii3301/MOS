"""Identity of the code the live cells run, checkable by anyone from any copy of the files.

    python3 code_bundle.py <dir with the files>      # prints code_root, then the bundle's sha256 and size

code_root (the identity to compare): sha256 of the text made of one line per file in FILES order,
    "<path>\t<sha256 of the file's bytes>\n"
It depends only on the files, never on archive formats or compressors.

The bundle (memeos-code/v1 `code_b64`) is only the delivery wrapper: deterministic tar (ustar, mtime 0, uid/gid 0, empty
names, mode 0644) + gzip mtime 0 level 9. Its sha256 protects the delivery, but it is NOT portable across machines:
different zlib builds compress the same bytes differently (found by mos-grok, 2026-11-19: same 29 files, 169,605 vs
169,810 bytes). Every live cell reports the code_root of the files it is running (op "rule").
"""
import gzip, hashlib, io, os, sys, tarfile

FILES = ["memeos.py", "sandbox.py", "codememes.py", "agent.py", "node.py", "tasks.py", "modelgen.py", "barrier.py", "barrier_deploy.json",
         "science.py", "net.py", "immune.py", "emergence.py", "compile.py", "drive.py", "neuro.py", "external_gene.py", "formula.py",
         "restrict.py", "forecast.py", "skillgene.py", "genememe.py", "forage.py", "lifecycle.py", "breed.py", "code_bundle.py",
         "requirements-genes.txt", "genes/cards/horizon_small_pi.json", "genes/cards/chronos2_small_forecaster.json"]

def code_root(root):
    lines = "".join("%s\t%s\n" % (f, hashlib.sha256(open(os.path.join(root, f), "rb").read()).hexdigest()) for f in FILES)
    return hashlib.sha256(lines.encode()).hexdigest()

def build(root):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as t:
        for f in FILES:
            data = open(os.path.join(root, f), "rb").read(); ti = tarfile.TarInfo(f)
            ti.size, ti.mtime, ti.mode, ti.uid, ti.gid, ti.uname, ti.gname = len(data), 0, 0o644, 0, 0, "", ""
            t.addfile(ti, io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", compresslevel=9, mtime=0, filename="") as g: g.write(buf.getvalue())
    return out.getvalue()

if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    b = build(root)
    print("code_root", code_root(root)); print("bundle_sha256", hashlib.sha256(b).hexdigest(), len(b), "(machine-dependent, see docstring)")
