"""Model as a code-meme GENERATOR (never a judge).

Shown: goal, context, TRAIN examples only, previous failing source. Never validation/holdout.
Returns up to 3 Python sources defining meme(x); each one is screened, sandboxed and gated
by the same promotion rules as any other meme.
Cost control: a durable reservation per call (daily cap), capped output tokens, 30 s timeout,
and no automatic retry when the outcome of a call is uncertain."""
import json, os, re, time
from urllib import request, error
from memeos import MemeError, ensure, canon, sha
from sandbox import ALLOWED_IMPORTS

ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

class ModelGenerator:
    def __init__(self, db, env=os.environ, fetch=None, barrier=None):
        self.db, self.env, self.barrier = db, env, barrier
        self.fetch = fetch or self._http
        self.db.execute("CREATE TABLE IF NOT EXISTS model_calls(id TEXT PRIMARY KEY, day TEXT NOT NULL, state TEXT NOT NULL, "
                        "request_hash TEXT NOT NULL, usage TEXT, at REAL NOT NULL)")

    @property
    def available(self): return bool(self.env.get("MOS_MODEL_CREDENTIAL"))

    def used_today(self):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        return self.db.execute("SELECT count(*) FROM model_calls WHERE day=? AND state<>'blocked_by_barrier'", (day,)).fetchone()[0]

    def _reserve(self, req_hash):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        cap = int(self.env.get("MEMEOS_DAILY_MODEL_CALLS", "10"))
        ensure(self.used_today() < cap, "daily_model_budget_exhausted")
        rid = sha({"r": req_hash, "t": time.time()})
        self.db.execute("INSERT INTO model_calls VALUES(?,?,?,?,?,?)", (rid, day, "reserved", req_hash, None, time.time()))
        return rid

    def _http(self, body):
        req = request.Request(ENDPOINT, data=json.dumps(body).encode(), method="POST",
                              headers={"Authorization": "Bearer " + self.env["MOS_MODEL_CREDENTIAL"],
                                       "Content-Type": "application/json", "User-Agent": "memeos-node/2.1.1"})
        try:
            with request.urlopen(req, timeout=30) as r: return json.loads(r.read().decode())
        except error.HTTPError as e:
            raise MemeError("model_http_%d" % e.code)

    def __call__(self, contract):
        """contract: dict with goal, context, train (validation is deliberately ignored)."""
        if not self.available: return []
        if self.barrier is not None:                     # self-defence: examples are data, never instructions to the model
            probe = "\n".join(str(v) for c in contract["train"] for v in (c.get("input"), c.get("output")))
            verdict = self.barrier.decide(probe, "content")
            if verdict["invasive"]:
                self.db.execute("INSERT INTO model_calls VALUES(?,?,?,?,?,?)", (sha({"blocked": probe, "t": time.time()}),
                                "blocked", "blocked_by_barrier", sha(probe), canon(verdict["reasons"]), time.time()))
                raise MemeError("model_call_blocked_by_barrier:" + ",".join(verdict["reasons"]))
        prompt = {
            "task": "Write a Python function meme(x) that maps each input to its output.",
            "goal": contract["goal"], "context": contract.get("context") or {},
            "examples": contract["train"], "previous_failing_source": contract.get("previous_source"),
            "rules": ["Define exactly: def meme(x): ...", "Pure function: no I/O, no printing, no globals.",
                      "Allowed imports only: " + ", ".join(sorted(ALLOWED_IMPORTS)),
                      "Forbidden: open, eval, exec, getattr, __import__, dunder attributes, os, sys, subprocess, socket.",
                      "Generalize the rule behind the examples; do not hard-code the example answers.",
                      "Output types must match the examples exactly (str vs int vs list vs bool)."],
            "answer_format": {"candidates": [{"source": "import re\ndef meme(x):\n    ..."}]},
            "max_candidates": 3}
        req_hash = sha(prompt)
        rid = self._reserve(req_hash)
        body = {"model": self.env.get("MOS_MODEL", "qwen/qwen3.8-27b"),
                "messages": [{"role": "system", "content": "Reply with JSON only."},
                             {"role": "user", "content": canon(prompt)}],
                "temperature": 0.2, "max_completion_tokens": int(self.env.get("MEMEOS_MODEL_MAX_TOKENS", "1500")),
                "reasoning_effort": "none", "include_reasoning": False,      # same settings the hub kernel uses with this model
                "response_format": {"type": "json_object"}}
        try:
            data = self.fetch(body)
        except Exception as e:
            self.db.execute("UPDATE model_calls SET state=? WHERE id=?", ("uncertain:" + type(e).__name__, rid))
            raise MemeError("model_call_failed_no_retry:" + str(e)[:80])
        self.db.execute("UPDATE model_calls SET state='done', usage=? WHERE id=?", (canon(data.get("usage") or {}), rid))
        content = ""
        try:
            content = data["choices"][0]["message"]["content"] or ""
            text = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
            m = re.search(r"\{.*\}", text, flags=re.S)
            cands = json.loads(m.group(0) if m else text)["candidates"]
        except Exception:
            raise MemeError("model_output_not_json:" + content[:240].replace("\n", " "))
        out = []
        for c in cands[:3]:
            if isinstance(c, dict) and isinstance(c.get("source"), str) and len(c["source"]) <= 8000:
                out.append({"kind": "code", "source": c["source"], "generator": "hub-model:" + body["model"]})
        return out
