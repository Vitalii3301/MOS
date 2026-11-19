"""Offline verification harness for MemeOS (requested by mos-manus' quarantine review).

Runs the whole test suite with: no network (only loopback sockets allowed in this process, for the built-in hub; sandbox children cannot import socket at
all), no credentials (every MOS_*/MEMEOS_* variable removed), no external genes (MEMEOS_GENES unset, no downloads).
Also verifies every file against MANIFEST.json first. Usage:  python3 -B offline_check.py
"""
import hashlib, json, os, socket, sys, unittest

def _refuse(*a, **k): raise OSError("network disabled by offline_check")
_LOOP = ("127.0.0.1", "localhost", "::1")
class _LoopbackOnly(socket.socket):
    """Only loopback is allowed (the built-in hub listens on 127.0.0.1); every other address is refused."""
    def connect(self, addr):
        if not (isinstance(addr, tuple) and addr[0] in _LOOP): _refuse()
        return super().connect(addr)
    def connect_ex(self, addr):
        if not (isinstance(addr, tuple) and addr[0] in _LOOP): _refuse()
        return super().connect_ex(addr)
    def bind(self, addr):
        if not (isinstance(addr, tuple) and addr[0] in _LOOP): _refuse()
        return super().bind(addr)
_gai = socket.getaddrinfo
def _gai_loop(host, *a, **k):
    if host not in _LOOP: _refuse()
    return _gai(host, *a, **k)
socket.socket = _LoopbackOnly; socket.getaddrinfo = _gai_loop
for k in list(os.environ):
    if k.startswith(("MOS_", "MEMEOS_", "HF_", "HUGGINGFACE")): os.environ.pop(k)

here = os.path.dirname(os.path.abspath(__file__)); os.chdir(here); sys.path.insert(0, here)
# the offline bundle carries its own manifest (no evidence folder); the full release uses MANIFEST.json
mf = "OFFLINE_MANIFEST.json" if os.path.exists("OFFLINE_MANIFEST.json") else "MANIFEST.json"
man = json.load(open(mf)) if os.path.exists(mf) else {"files": {}}
missing = [p for p in man.get("files", {}) if not os.path.exists(p)]
bad = [p for p, h in man.get("files", {}).items() if p != mf and os.path.exists(p) and hashlib.sha256(open(p, "rb").read()).hexdigest() != h]
print(json.dumps({"manifest": mf, "files": len(man.get("files", {})), "missing": missing[:10], "hash_mismatches": bad}))
if bad or (mf == "OFFLINE_MANIFEST.json" and missing): sys.exit("manifest mismatch: refusing to run")
import test_memeos
if not test_memeos.LOOPBACK: print(json.dumps({"warning": test_memeos.NO_LO, "effect": "4 hub tests are skipped, not passed"}), flush=True)
r = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromName("test_memeos"))
print(json.dumps({"ran": r.testsRun, "failures": len(r.failures), "errors": len(r.errors), "skipped": len(r.skipped),
                  "network": "loopback only", "credentials": "removed", "external_genes": "off"}))
sys.exit(0 if r.wasSuccessful() else 1)
