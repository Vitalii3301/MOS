"""MOS Agent: autonomous executor with its own memory and goals (MemeticOS whitepaper: 'Agent').

perform()  -> picks the active meme for the goal and runs it (MPL or sandboxed Python)
feedback() -> the world says what the right answer was; the episode becomes experience
learning   -> unknown goal or wrong answer => the agent builds a contract from its own labelled
              episodes, keeps every earlier contract as a regression guard, and evolves a new meme;
              optional external generators (hub model, chat, other nodes) only PROPOSE, the gate decides
ecosystem  -> real-use fitness per meme (Laplace), decay of unused memes (dormant), extinction of
              memes that fail in use (quarantine + rollback), motif support follows survivors
"""
import json, random, time
from memeos import MemeOS, MemeError, ensure, sha, canon, goal_key, validate
from codememes import CodeMemes

AGENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes(id TEXT PRIMARY KEY, agent TEXT NOT NULL, goal TEXT NOT NULL, context TEXT NOT NULL,
  input TEXT NOT NULL, output TEXT, meme TEXT, expected TEXT, outcome TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS usage(meme TEXT PRIMARY KEY, trials INTEGER NOT NULL, successes INTEGER NOT NULL, last_used REAL NOT NULL);
"""

def from_kernel_program(p):
    """Adapter: MOS Experience Kernel MPL (Railway) -> MemeOS MPL, so the hub model can act as a generator."""
    out = []
    for st in p["pipeline"]:
        op = st["op"]
        if op == "replace": out.append({"op": "replace", "args": [st["from"], st["to"]]})
        elif op in ("prefix", "suffix"): out.append({"op": op, "args": [st["text"]]})
        elif op in ("split", "join"): out.append({"op": op, "args": [st["sep"]]})
        elif op in ("add", "scale"): out.append({"op": op, "args": [st["value"]]})
        elif op == "round": out.append({"op": "round", "args": [0]})
        elif op == "pluck": out.append({"op": "pluck", "args": [st["key"]]})
        elif op == "map": out.append({"op": "map", "args": [from_kernel_program(st["program"])]})
        elif op == "call": raise MemeError("kernel_call_needs_bundle")
        else: out.append({"op": op})
    return validate({"pipeline": out})

class Agent:
    def __init__(self, kernel: MemeOS, name="mos-agent", generators=(), seed=0):
        self.k, self.name, self.cm = kernel, name, CodeMemes(kernel)
        self.k.db.executescript(AGENT_SCHEMA)
        self.generators = list(generators)      # callables(contract_dict) -> list of {"kind": "mpl"|"code", "genome"/"source", "generator"}
        self.rng = random.Random(seed)

    # ---------------------------------------------------------------- acting
    def perform(self, goal, value, context=None):
        context = context or {}
        eid = sha({"agent": self.name, "goal": goal, "context": context, "input": value, "t": time.time(), "r": self.rng.random()})
        mid = self.k.active(goal, context); out, outcome = None, "pending"
        if not mid: outcome = "no_meme"
        else:
            try:
                card = self.k.card(mid)
                out = self.cm.execute(goal, value, context)[0] if card["schema"] == "mos-code-meme/v1" else self.k.execute(goal, value, context)[0]
            except MemeError as e:
                outcome = "error:" + str(e)[:80]
        self.k.db.execute("INSERT INTO episodes VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (eid, self.name, goal, canon(context), canon(value), canon(out), mid, None, outcome, time.time()))
        if mid:
            self.k.db.execute("INSERT INTO usage VALUES(?,0,0,?) ON CONFLICT(meme) DO UPDATE SET last_used=excluded.last_used", (mid, time.time()))
        return {"episode": eid, "output": out, "meme": mid, "status": outcome}

    def feedback(self, episode, expected, learn=True):
        row = self.k.db.execute("SELECT goal,context,input,output,meme FROM episodes WHERE id=?", (episode,)).fetchone()
        ensure(row, "unknown_episode"); goal, ctx, inp, out, mid = row; ctx = json.loads(ctx)
        got = json.loads(out) if out is not None else None
        correct = mid is not None and got == expected and type(got) == type(expected)
        self.k.db.execute("UPDATE episodes SET expected=?, outcome=? WHERE id=?", (canon(expected), "correct" if correct else "wrong", episode))
        if mid:
            self.k.db.execute("UPDATE usage SET trials=trials+1, successes=successes+? WHERE meme=?", (int(correct), mid))
        self.k.event("feedback", {"episode": episode, "meme": mid, "correct": correct})
        result = {"correct": correct}
        if not correct and learn: result["learning"] = self.learn_from_experience(goal, ctx)
        return result

    # ---------------------------------------------------------------- autonomous learning
    def experience_contract(self, goal, context):
        rows = self.k.db.execute("SELECT input, expected FROM episodes WHERE goal=? AND context=? AND expected IS NOT NULL ORDER BY at",
                                 (goal, canon(context))).fetchall()
        cases, seen = [], set()
        for i, e in rows:
            if i not in seen: seen.add(i); cases.append({"input": json.loads(i), "output": json.loads(e)})
        # accumulated knowledge of this goal: examples of every earlier contract join the experience
        for tr, va in self.k.db.execute("SELECT train, validation FROM contracts WHERE goal=? AND context=?", (goal, canon(context))).fetchall():
            for c in json.loads(tr) + json.loads(va):
                k = canon(c["input"])
                if k not in seen: seen.add(k); cases.append(c)
        ensure(len(cases) >= 2, "need_more_experience")
        # deterministic split by content hash: ~2/3 train, ~1/3 validation, both non-empty
        tr = [c for c in cases if int(sha(c["input"])[:8], 16) % 3 != 0]; va = [c for c in cases if c not in tr]
        if not va: va = [tr.pop()]
        if not tr: tr = [va.pop()]
        return self.k.contract(goal, tr, va, context=context)

    def learn_from_experience(self, goal, context):
        try: cid = self.experience_contract(goal, context)
        except MemeError as e: return {"ok": False, "reason": str(e)}
        c = self.k._contract(cid); attempts = []
        r = self.k.learn(cid, rng=random.Random(self.rng.random()))
        attempts.append({"generator": "memeos-evolution", "ok": r["ok"], "reason": r.get("reason") or r["gate"]["reason"]})
        if r["ok"]: return {"ok": True, "meme": r["meme"], "generator": "memeos-evolution", "attempts": attempts}
        for gen in self.generators:
            for prop in gen(c) or []:
                try:
                    if prop["kind"] == "code":
                        res = self.cm.propose(prop["source"], cid, prop.get("generator", "external"))
                    else:
                        act = self.k.active(goal, context)
                        mid = self.k.add(__import__("memeos").meme_card(prop["genome"], goal, context, parents=[act] if act else [],
                                         origin={"kind": "proposed", "generator": prop.get("generator", "external")}))
                        res = {"meme": mid, **self.k.promote(mid, cid)}
                except MemeError as e:
                    res = {"accepted": False, "reason": str(e)}
                attempts.append({"generator": prop.get("generator"), "ok": res.get("accepted"), "reason": res.get("reason")})
                if res.get("accepted"): return {"ok": True, "meme": res["meme"], "generator": prop.get("generator"), "attempts": attempts}
        return {"ok": False, "attempts": attempts}

    # ---------------------------------------------------------------- ecosystem
    def fitness(self, mid):
        r = self.k.db.execute("SELECT trials, successes FROM usage WHERE meme=?", (mid,)).fetchone()
        t, s = r if r else (0, 0)
        return (s + 1) / (t + 2), t

    def ecosystem_tick(self, dormant_after=7 * 86400, min_trials=5, extinction_below=0.5, now=None):
        now = now or time.time(); report = {"extinct": [], "dormant": [], "restored": []}
        for mid, status in self.k.db.execute("SELECT id, status FROM memes WHERE status IN ('active','candidate')").fetchall():
            f, t = self.fitness(mid)
            if t >= min_trials and f < extinction_below and status == "active":
                card = self.k.card(mid)
                self.k.db.execute("UPDATE memes SET status='quarantined' WHERE id=?", (mid,))
                self.k.register_antimeme(mid, "failed_in_use", self.name)
                r = self.k.revalidate(card["goal"], card["context"])
                report["extinct"].append(mid); report["restored"].append(r.get("restored"))
                continue
            last = self.k.db.execute("SELECT last_used FROM usage WHERE meme=?", (mid,)).fetchone()
            created = self.k.db.execute("SELECT created FROM memes WHERE id=?", (mid,)).fetchone()[0]
            if status == "candidate" and now - (last[0] if last else created) > dormant_after:
                self.k.db.execute("UPDATE memes SET status='dormant' WHERE id=?", (mid,)); report["dormant"].append(mid)
        self.k.event("ecosystem_tick", {k: len(v) for k, v in report.items()})
        return report

    def census(self):
        rows = self.k.db.execute("SELECT status, count(*) FROM memes GROUP BY status").fetchall()
        live = [{"meme": m, "goal": self.k.card(m)["goal"], "fitness": round(self.fitness(m)[0], 3), "trials": self.fitness(m)[1]}
                for (m,) in self.k.db.execute("SELECT id FROM memes WHERE status='active'")]
        return {"by_status": dict(rows), "active": live}
