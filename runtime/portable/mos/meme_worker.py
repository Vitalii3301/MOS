from __future__ import annotations
import json, sys
from .security import CodePolicy

MAX_RESULT_BYTES = 1_000_000

def _limits():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
        max_mem = 768 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (max_mem, max_mem))
        resource.setrlimit(resource.RLIMIT_NOFILE, (16, 16))
    except Exception:
        pass

def main():
    _limits()
    try:
        req = json.load(sys.stdin)
        code = req["code"]
        input_data = req.get("input_data")
        policy = CodePolicy()
        sec = policy.scan(code)
        if not sec.allowed:
            print(json.dumps({"ok": False, "error": "security", "reasons": sec.reasons}, ensure_ascii=False))
            return
        env = {"__builtins__": policy.allowed_builtins, "input_data": input_data}
        exec(compile(code, "<meme>", "exec"), env, env)
        out = env["meme_main"](input_data) if "meme_main" in env and callable(env["meme_main"]) else env.get("result")
        raw = json.dumps(out, ensure_ascii=False, allow_nan=False)
        if len(raw.encode("utf-8")) > MAX_RESULT_BYTES:
            print(json.dumps({"ok": False, "error": "result_too_large"}))
            return
        print(json.dumps({"ok": True, "result": out}, ensure_ascii=False, allow_nan=False))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))

if __name__ == "__main__":
    main()
