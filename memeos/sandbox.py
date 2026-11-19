"""Isolated execution of real-code memes (Python functions).

Three independent walls, each sufficient against a class of escapes:

1. Static scan (deny by default for introspection): forbidden names, any attribute that starts with "_", every
   frame / code / generator / coroutine / traceback attribute family (f_*, gi_*, cr_*, ag_*, tb_*, co_*), and string
   templating that can walk attributes (.format / .format_map).
2. Clean runner: the meme runs in a module namespace that holds nothing but itself; the import hook, the builtins
   module and the runner's own modules are not reachable from any frame the meme can see.
3. Empty filesystem: before the meme is loaded the child process locks itself into an empty directory (chroot) and
   drops to an unprivileged user (or, without root, does the same inside a private user namespace). Even code that
   gets past walls 1 and 2 finds no files, no /proc, nothing to read. If this wall cannot be built on a host, the
   run reports it and is refused (by default for all code; always for foreign code).

Plus: fresh `python3 -I -S` subprocess, CPU/memory/file/process rlimits, empty env, no network modules, wall timeout,
JSON in and out. Found by mos-grok (2026-11-19): a running generator's frame chain reached the runner's globals and
`_real_import('io').FileIO(...)` read any file of the cell; regression tests cover it at every wall.
"""
import ast, json, os, re, subprocess, sys, tempfile, textwrap

ALLOWED_IMPORTS = {"re", "math", "json", "string", "itertools", "collections", "functools",
                   "statistics", "datetime", "unicodedata", "hashlib", "decimal", "fractions", "heapq", "bisect"}
FORBIDDEN_NAMES = {"open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars",
                   "getattr", "setattr", "delattr", "input", "breakpoint", "help", "memoryview",
                   "os", "sys", "subprocess", "socket", "shutil", "pathlib", "ctypes", "importlib",
                   "builtins", "__builtins__", "__subclasses__", "__bases__", "__mro__", "__globals__",
                   "__code__", "__class__", "__dict__", "__loader__", "__spec__", "type", "object", "super", "classmethod",
                   "staticmethod", "property", "dir", "id",
                   # modules and helpers reachable as attributes of allowed modules, which access attributes by STRING
                   # (found by mos-claude-code: fractions.operator.attrgetter("...") walks names the scan cannot see)
                   "operator", "attrgetter", "methodcaller", "modules", "codecs", "random", "copyreg", "weakref", "gc",
                   "inspect", "types", "abc", "enum", "warnings", "traceback", "linecache", "tokenize", "io", "threading",
                   "marshal", "pickle", "Formatter", "get_field", "vformat", "template", "Template", "sys_modules"}
INTROSPECTION = re.compile(r"^(_|f_|gi_|cr_|ag_|tb_|co_)|^(format|format_map|mro|func_globals|func_code|im_func|im_self)$")

def scan(source):
    """Static anti-meme scan. Returns list of violations."""
    v = []
    try: tree = ast.parse(source)
    except SyntaxError as e: return ["syntax:" + str(e.msg)]
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "meme"]
    if not fns: v.append("no_meme_function")
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            mods = [a.name.split(".")[0] for a in n.names] if isinstance(n, ast.Import) else [(n.module or "").split(".")[0]]
            v += ["import:" + m for m in mods if m not in ALLOWED_IMPORTS]
        elif isinstance(n, ast.Name) and (n.id in FORBIDDEN_NAMES or n.id.startswith("__")): v.append("name:" + n.id)
        elif isinstance(n, ast.Attribute) and (n.attr in FORBIDDEN_NAMES or INTROSPECTION.search(n.attr)): v.append("attr:" + n.attr)
        elif isinstance(n, (ast.Global, ast.Nonlocal)): v.append("scope:" + type(n).__name__)
    for n in tree.body:
        if not isinstance(n, (ast.FunctionDef, ast.Import, ast.ImportFrom, ast.Assign, ast.Expr)): v.append("top_level:" + type(n).__name__)
        if isinstance(n, ast.Expr) and not (isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)): v.append("top_level_call")
    return sorted(set(v))

RUNNER = textwrap.dedent(r'''
import json, os, resource, sys
ALLOWED = @@ALLOWED@@
for _m in sorted(ALLOWED): __import__(_m)                 # load everything the meme may import BEFORE the filesystem goes away
data = json.loads(sys.stdin.read()); jail = os.getcwd(); iso = "none"
try:
    if os.geteuid() == 0:
        os.chroot(jail); os.chdir("/"); os.setgroups([]); os.setgid(65534); os.setuid(65534); iso = "chroot+nobody"
    else:
        uid, gid = os.geteuid(), os.getegid()
        os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNS)
        with open("/proc/self/setgroups", "w") as f: f.write("deny")
        with open("/proc/self/uid_map", "w") as f: f.write("0 %d 1" % uid)
        with open("/proc/self/gid_map", "w") as f: f.write("0 %d 1" % gid)
        os.chroot(jail); os.chdir("/"); iso = "userns+chroot"
except Exception as e:
    iso = "none:" + type(e).__name__
if data.get("require_isolation") and iso.startswith("none"):
    sys.stdout.write(json.dumps({"isolation": iso, "refused": True})); sys.exit(0)
resource.setrlimit(resource.RLIMIT_CPU, (@@CPU@@, @@CPU@@))
resource.setrlimit(resource.RLIMIT_AS, (@@MEM@@, @@MEM@@))
resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
resource.setrlimit(resource.RLIMIT_NOFILE, (8, 8))
for _n in ("socket", "_socket", "subprocess", "os", "posix", "io", "_io", "ctypes", "importlib", "resource"):
    sys.modules[_n] = None
def _make():
    # Wall 2 removes the handles (import hook, sys, os, io, the builtins module) from every namespace the meme's frames
    # point to. A frame object still carries its builtins, so wall 2 alone is not a jail: walls 1 and 3 are.
    # Everything the meme can touch is built here and handed over; nothing sensitive stays in any namespace it can reach.
    import builtins as _b
    mods = {m: sys.modules[m] for m in ALLOWED}
    def _imp(name, globals=None, locals=None, fromlist=(), level=0):
        root = name.split(".")[0]
        if root not in mods or level: raise ImportError("blocked:" + name)
        return mods[root] if not fromlist or "." not in name else sys.modules[name]
    safe = {k: getattr(_b, k) for k in ("abs all any bool dict enumerate filter float frozenset int isinstance "
            "len list map max min next range reversed round set sorted str sum tuple zip ValueError TypeError "
            "KeyError IndexError Exception StopIteration ZeroDivisionError chr ord divmod pow repr hash iter "
            "callable slice bin hex oct print True False None").split() if hasattr(_b, k)}
    safe["__import__"] = _imp; safe["__build_class__"] = _b.__build_class__
    class MOSUnverified(BaseException):
        """Raised by restricted foreign code on a path its cell never verified; not catchable by `except Exception`."""
    safe["MOSUnverified"] = MOSUnverified
    return safe, MOSUnverified
_safe, _Unverified = _make(); _src, _inputs, _dumps, _write, _iso = data["source"], data["inputs"], json.dumps, sys.stdout.write, iso
for _k in [k for k in list(globals()) if not k.startswith("_") or k in ("_m", "_n", "_b")]: del globals()[_k]
def _run(src, inputs, safe, unverified):
    g = {"__builtins__": safe, "__name__": "meme"}
    exec(compile(src, "<meme>", "exec"), g)
    meme = g["meme"]; out = []
    for x in inputs:
        try: out.append({"ok": True, "value": meme(x)})
        except unverified: out.append({"ok": False, "error": "unverified_path"})
        except Exception as e: out.append({"ok": False, "error": type(e).__name__})
    return out
_res = _run(_src, _inputs, _safe, _Unverified)
_write(_dumps({"isolation": _iso, "results": _res}))
''')

def run_source(source, inputs, cpu_seconds=2, mem_mb=256, wall_seconds=5, require_isolation=None, _skip_scan=False):
    # Isolation is required by default; an operator may allow unisolated runs of the cell's OWN code on a host where the
    # empty-filesystem wall cannot be built (MEMEOS_SANDBOX_ALLOW_UNISOLATED=1). Foreign code always passes True explicitly.
    if require_isolation is None: require_isolation = os.environ.get("MEMEOS_SANDBOX_ALLOW_UNISOLATED") != "1"
    bad = [] if _skip_scan else scan(source)            # _skip_scan exists only so tests can check walls 2 and 3 on their own
    if bad: return {"ok": False, "error": "antimeme_scan", "violations": bad}
    code = RUNNER.replace("@@CPU@@", str(int(cpu_seconds))).replace("@@MEM@@", str(mem_mb * 1024 * 1024)) \
                 .replace("@@ALLOWED@@", repr(ALLOWED_IMPORTS))
    with tempfile.TemporaryDirectory() as d:
        os.chmod(d, 0o711)
        try:
            p = subprocess.run([sys.executable, "-I", "-S", "-c", code], input=json.dumps({"source": source, "inputs": inputs, "require_isolation": require_isolation}),
                               capture_output=True, text=True, timeout=wall_seconds, cwd=d, env={})
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "wall_timeout"}
    if p.returncode != 0:
        return {"ok": False, "error": "process_failed", "code": p.returncode, "stderr": p.stderr[-300:]}
    try: out = json.loads(p.stdout)
    except ValueError: return {"ok": False, "error": "bad_output"}
    if out.get("refused"): return {"ok": False, "error": "no_filesystem_isolation_here", "isolation": out["isolation"]}
    return {"ok": True, "results": out["results"], "isolation": out["isolation"]}

def evaluate_source(source, cases, require_isolation=None):
    """(exact passes, total, details) — JSON round-trip makes output comparison type-faithful."""
    r = run_source(source, [c["input"] for c in cases], require_isolation=require_isolation)
    if not r["ok"]: return 0, len(cases), r
    ok = sum(1 for res, c in zip(r["results"], cases) if res["ok"] and res["value"] == c["output"]
             and type(res["value"]) == type(c["output"]))
    return ok, len(cases), r
