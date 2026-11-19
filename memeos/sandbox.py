"""Isolated execution of real-code memes (Python functions).

Each run: fresh `python3 -I -S` subprocess, CPU/memory/file/process rlimits, empty env,
temp cwd, no network (socket module blocked), restricted builtins (no open/exec/eval/import
except an allowlist), wall timeout. Input/output via JSON only. Static scan rejects
dangerous names before execution. This is defence in depth, not a formally secure jail.
"""
import ast, json, os, subprocess, sys, tempfile, textwrap

ALLOWED_IMPORTS = {"re", "math", "json", "string", "itertools", "collections", "functools",
                   "statistics", "datetime", "unicodedata", "hashlib", "decimal", "fractions", "heapq", "bisect"}
FORBIDDEN_NAMES = {"open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars",
                   "getattr", "setattr", "delattr", "input", "breakpoint", "help", "memoryview",
                   "os", "sys", "subprocess", "socket", "shutil", "pathlib", "ctypes", "importlib",
                   "builtins", "__builtins__", "__subclasses__", "__bases__", "__mro__", "__globals__",
                   "__code__", "__class__", "__dict__", "__loader__", "__spec__",
                   # ways to reach attributes by string, or host modules, through an allowed import
                   "operator", "attrgetter", "methodcaller", "modules", "codecs", "random", "copyreg"}

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
        elif isinstance(n, ast.Name) and n.id in FORBIDDEN_NAMES: v.append("name:" + n.id)
        elif isinstance(n, ast.Attribute) and (n.attr in FORBIDDEN_NAMES or n.attr.startswith("_")): v.append("attr:" + n.attr)   # private aliases (e.g. a module's `_sys`) lead to host modules
        elif isinstance(n, (ast.Global, ast.Nonlocal)): v.append("scope:" + type(n).__name__)
    for n in tree.body:
        if not isinstance(n, (ast.FunctionDef, ast.Import, ast.ImportFrom, ast.Assign, ast.Expr)): v.append("top_level:" + type(n).__name__)
        if isinstance(n, ast.Expr) and not (isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)): v.append("top_level_call")
    return sorted(set(v))

RUNNER = textwrap.dedent(r'''
import json, resource, sys
resource.setrlimit(resource.RLIMIT_CPU, (@@CPU@@, @@CPU@@))
resource.setrlimit(resource.RLIMIT_AS, (@@MEM@@, @@MEM@@))
resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
resource.setrlimit(resource.RLIMIT_NOFILE, (8, 8))
sys.modules["socket"] = None; sys.modules["_socket"] = None; sys.modules["subprocess"] = None
ALLOWED = @@ALLOWED@@
import builtins as _b
_real_import = _b.__import__
def _imp(name, globals=None, locals=None, fromlist=(), level=0):
    if name.split(".")[0] not in ALLOWED: raise ImportError("blocked:" + name)
    return _real_import(name, globals, locals, fromlist, level)
SAFE = {k: getattr(_b, k) for k in ("abs all any bool dict enumerate filter float frozenset int isinstance "
        "len list map max min next range reversed round set sorted str sum tuple zip ValueError TypeError "
        "KeyError IndexError Exception StopIteration ZeroDivisionError chr ord divmod pow repr hash iter "
        "callable slice format bin hex oct print True False None").split() if hasattr(_b, k)}
SAFE["__import__"] = _imp
class MOSUnverified(BaseException):
    """Raised by restricted foreign code on a path its cell never verified; not catchable by `except Exception`."""
SAFE["MOSUnverified"] = MOSUnverified
SAFE["__build_class__"] = _b.__build_class__
data = json.loads(sys.stdin.read())
g = {"__builtins__": SAFE, "__name__": "meme"}
exec(compile(data["source"], "<meme>", "exec"), g)
out = []
for x in data["inputs"]:
    try: out.append({"ok": True, "value": g["meme"](x)})
    except MOSUnverified: out.append({"ok": False, "error": "unverified_path"})
    except Exception as e: out.append({"ok": False, "error": type(e).__name__})
sys.stdout.write(json.dumps(out))
''')

def run_source(source, inputs, cpu_seconds=2, mem_mb=256, wall_seconds=5):
    bad = scan(source)
    if bad: return {"ok": False, "error": "antimeme_scan", "violations": bad}
    code = RUNNER.replace("@@CPU@@", str(int(cpu_seconds))).replace("@@MEM@@", str(mem_mb * 1024 * 1024)) \
                 .replace("@@ALLOWED@@", repr(ALLOWED_IMPORTS))
    with tempfile.TemporaryDirectory() as d:
        try:
            p = subprocess.run([sys.executable, "-I", "-S", "-c", code], input=json.dumps({"source": source, "inputs": inputs}),
                               capture_output=True, text=True, timeout=wall_seconds, cwd=d, env={})
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "wall_timeout"}
    if p.returncode != 0:
        return {"ok": False, "error": "process_failed", "code": p.returncode, "stderr": p.stderr[-300:]}
    try: return {"ok": True, "results": json.loads(p.stdout)}
    except ValueError: return {"ok": False, "error": "bad_output"}

def evaluate_source(source, cases):
    """(exact passes, total, details) — JSON round-trip makes output comparison type-faithful."""
    r = run_source(source, [c["input"] for c in cases])
    if not r["ok"]: return 0, len(cases), r
    ok = sum(1 for res, c in zip(r["results"], cases) if res["ok"] and res["value"] == c["output"]
             and type(res["value"]) == type(c["output"]))
    return ok, len(cases), r
