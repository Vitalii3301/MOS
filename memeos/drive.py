"""Drive — a cell sets its own goals from OBSERVED gaps (never from imagination: it could not verify those).

Gaps are recorded as the cell works:
  unsolved   : a solve/learn request with a contract that could not be satisfied
  unknown    : a goal requested without examples and without any known skill
  missed     : a text reported as an invasion that the barrier let through
  weak       : an active meme failing in real use (labelled feedback)
  false_alarm: a text reported as clean that the barrier flags (immune tolerance)

reflect() -> agenda of self-goals with reasons, ordered by priority
pursue()  -> acts on the agenda within a separate small daily budget; every result passes the normal gates:
  retry_unsolved : only when the gene pool changed since the last attempt (new own/neighbour genes) -> composition
  immunize       : >= 2 reported misses not yet caught -> synthesize antimeme, own lab, share
  repair_weak    : relearn from the cell's own labelled experience
  ask_examples   : ask the requester (once per goal) for labelled examples — the cell asks for what it needs
Everything is journaled (self_goal_set / self_goal_done); `agenda` exposes it; MEMEOS_SELF_GOALS=0 switches it off.
"""
import json, os, time
from memeos import sha, canon, MemeError

DRIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS gaps(id TEXT PRIMARY KEY, kind TEXT NOT NULL, goal TEXT, context TEXT, contract TEXT, detail TEXT,
  requester TEXT, count INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'open', pool_at_attempt INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
  created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS self_goals(id TEXT PRIMARY KEY, kind TEXT, gap TEXT, reason TEXT, status TEXT, result TEXT, at REAL);
"""

class Drive:
    def __init__(self, node):
        self.node = node; node.k.db.executescript(DRIVE_SCHEMA)

    @property
    def enabled(self): return os.environ.get("MEMEOS_SELF_GOALS", "1") != "0"

    def pool(self):
        """Size of the gene pool this cell can build from (own accepted + library)."""
        return self.node.k.db.execute("SELECT count(*) FROM memes WHERE status IN ('active','superseded','library')").fetchone()[0]

    # ------------------------------------------------------------ recording gaps (called from normal operation)
    def note(self, kind, goal=None, context=None, contract=None, detail=None, requester=None):
        gid = sha({"k": kind, "g": goal, "c": context or {}, "d": detail if kind in ("missed", "false_alarm") else None})
        db = self.node.k.db
        if db.execute("SELECT 1 FROM gaps WHERE id=?", (gid,)).fetchone():
            db.execute("UPDATE gaps SET count=count+1, updated=?, contract=COALESCE(?, contract), status=CASE WHEN status='closed' THEN 'closed' ELSE 'open' END WHERE id=?",
                       (time.time(), canon(contract) if contract else None, gid))
        else:
            db.execute("INSERT INTO gaps(id,kind,goal,context,contract,detail,requester,pool_at_attempt,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (gid, kind, goal, canon(context or {}), canon(contract) if contract else None, detail, requester, self.pool(), time.time(), time.time()))
            self.node.k.event("gap_noted", {"kind": kind, "goal": goal})
        return gid

    def close(self, goal, context=None):
        self.node.k.db.execute("UPDATE gaps SET status='closed', updated=? WHERE goal=? AND context=? AND kind IN ('unsolved','unknown')",
                               (time.time(), goal, canon(context or {})))

    # ------------------------------------------------------------ reflection: what do I want to do, and why
    def reflect(self):
        db = self.node.k.db; agenda = []; pool = self.pool()
        for gid, goal, ctx, contract, pa, cnt in db.execute("SELECT id,goal,context,contract,pool_at_attempt,count FROM gaps WHERE kind='unsolved' AND status='open' AND contract IS NOT NULL"):
            if self.node.k.active(goal, json.loads(ctx)): db.execute("UPDATE gaps SET status='closed' WHERE id=?", (gid,)); continue
            if pool > (pa or 0):
                agenda.append({"priority": 3 + min(cnt, 5), "kind": "retry_unsolved", "gap": gid, "goal": goal,
                               "reason": f"asked {cnt}x, unsolved; gene pool grew {pa}->{pool} since last attempt"})
        misses = [(gid, d) for gid, d in db.execute("SELECT id, detail FROM gaps WHERE kind='missed' AND status='open'")]
        if self.node.barrier is not None:
            still = [(g, d) for g, d in misses if not self.node.barrier.decide(d, "content")["invasive"]]
            for g, d in misses:
                if (g, d) not in still: db.execute("UPDATE gaps SET status='closed' WHERE id=?", (g,))
            if len(still) >= 2:
                agenda.append({"priority": 9, "kind": "immunize", "gap": sha([g for g, _ in still]), "goal": "barrier",
                               "reason": f"{len(still)} reported invasions still pass my barrier", "samples": [d for _, d in still]})
        for gid, text in db.execute("SELECT id, detail FROM gaps WHERE kind='false_alarm' AND status='open'").fetchall():
            if self.node.barrier is not None and not self.node.barrier.decide(text, "content")["invasive"]:
                db.execute("UPDATE gaps SET status='closed' WHERE id=?", (gid,)); continue
            agenda.append({"priority": 8, "kind": "tolerance", "gap": gid, "goal": "barrier", "reason": "a text reported as clean is flagged by my barrier"})
        for gid, goal, ctx, req, cnt in db.execute("SELECT id,goal,context,requester,count FROM gaps WHERE kind='unknown' AND status='open'"):
            if cnt >= 2 and req and not db.execute("SELECT 1 FROM self_goals WHERE gap=? AND kind='ask_examples'", (gid,)).fetchone():
                agenda.append({"priority": 2, "kind": "ask_examples", "gap": gid, "goal": goal, "requester": req,
                               "reason": f"goal requested {cnt}x but I have no skill and no examples to learn from"})
        if self.node.k.db.execute("SELECT name FROM sqlite_master WHERE name='usage'").fetchone():
            for mid, t, s in db.execute("SELECT meme, trials, successes FROM usage WHERE trials>=3"):
                st = db.execute("SELECT status FROM memes WHERE id=?", (mid,)).fetchone()
                if st and st[0] == "active" and s / t < 0.8:
                    card = self.node.k.card(mid)
                    agenda.append({"priority": 6, "kind": "repair_weak", "gap": mid, "goal": card["goal"], "context": card["context"],
                                   "reason": f"my active meme fails in real use ({s}/{t} correct)"})
        return sorted(agenda, key=lambda a: -a["priority"])

    # ------------------------------------------------------------ acting on my own agenda
    def _budget(self):
        day = time.strftime("%Y-%m-%d", time.gmtime()); key = "selfops:" + day; db = self.node.k.db
        used = int((db.execute("SELECT value FROM node_meta WHERE key=?", (key,)).fetchone() or ["0"])[0])
        if used >= int(os.environ.get("MEMEOS_DAILY_SELF_OPS", "6")): return False
        db.execute("INSERT INTO node_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(used + 1))); return True

    def pursue(self, limit=2):
        if not self.enabled: return {"enabled": False, "done": []}
        done = []
        for item in self.reflect()[:limit]:
            if not self._budget(): done.append({"kind": item["kind"], "skipped": "daily_self_budget_exhausted"}); break
            sid = sha({"i": item["gap"], "k": item["kind"], "t": time.time()}); db = self.node.k.db
            db.execute("INSERT INTO self_goals VALUES(?,?,?,?,?,?,?)", (sid, item["kind"], item["gap"], item["reason"], "set", None, time.time()))
            self.node.k.event("self_goal_set", {"kind": item["kind"], "goal": item.get("goal"), "reason": item["reason"]})
            try: res = getattr(self, "_do_" + item["kind"])(item)
            except Exception as e: res = {"ok": False, "error": type(e).__name__ + ":" + str(e)[:120]}
            db.execute("UPDATE self_goals SET status=?, result=? WHERE id=?", ("done" if res.get("ok") else "not_achieved", canon(res), sid))
            self.node.k.event("self_goal_done", {"kind": item["kind"], "goal": item.get("goal"), "ok": bool(res.get("ok"))})
            done.append({"kind": item["kind"], "goal": item.get("goal"), "reason": item["reason"], **res})
        return {"enabled": True, "done": done}

    def _do_retry_unsolved(self, item):
        from emergence import compose
        db = self.node.k.db; goal, ctx, contract = db.execute("SELECT goal, context, contract FROM gaps WHERE id=?", (item["gap"],)).fetchone()
        c = json.loads(contract); ctx = json.loads(ctx)
        cid = self.node.k.contract(goal, c["train"], c["validation"], c.get("holdout", []), ctx)
        r = compose(self.node.k, cid, max_len=2, budget=400)                    # cheap: build from the grown gene pool
        db.execute("UPDATE gaps SET pool_at_attempt=?, attempts=attempts+1, updated=? WHERE id=?", (self.pool(), time.time(), item["gap"]))
        if r["ok"]:
            db.execute("UPDATE gaps SET status='closed' WHERE id=?", (item["gap"],))
            return {"ok": True, "how": "composition", "composed_of": r.get("composed_of"), "evaluations": r["evaluations"], "meme": r["meme"]}
        return {"ok": False, "how": "composition_failed", "evaluations": r["evaluations"]}

    def _do_immunize(self, item):
        if self.node.net is None: return {"ok": False, "error": "no_network"}
        r = self.node.net.immunize(item["samples"])
        ok = r.get("action") == "gene_synthesized_adopted_and_shared"
        return {"ok": ok, **{k: r.get(k) for k in ("action", "gene", "phrases", "lab", "missed")}, "shared_to": [x["to"] for x in r.get("broadcast", [])]}

    def _do_tolerance(self, item):
        if self.node.net is None: return {"ok": False, "error": "no_network"}
        text = self.node.k.db.execute("SELECT detail FROM gaps WHERE id=?", (item["gap"],)).fetchone()[0]
        r = self.node.net.tolerance(text)
        # a false alarm caused by a built-in gene or the learned head cannot be fixed by recall: keep it visible, do not loop
        self.node.k.db.execute("UPDATE gaps SET status=?, attempts=attempts+1, updated=? WHERE id=?",
                               ("closed" if not r.get("still_flagged") else "needs_retraining", time.time(), item["gap"]))
        return {"ok": not r.get("still_flagged"), **r}

    def _do_repair_weak(self, item):
        r = self.node.agent.learn_from_experience(item["goal"], item.get("context") or {})
        return {"ok": bool(r.get("ok")), "meme": r.get("meme"), "generator": r.get("generator")}

    def _do_ask_examples(self, item):
        payload = {"schema": "mos-meme-request/v1", "from": self.node.hub.node, "goal": item["goal"],
                   "ask": "I was asked for this goal but have no skill and no examples. Please send labelled examples: kind=mos-meme-task/v1 "
                          "{op:'learn', contract:{goal, train:[{input,output}], validation:[...]}}"}
        s, _ = self.node.hub.send(item["requester"], "mos-meme-request/v1", payload, "MSG-request-" + sha({"g": item["goal"], "c": self.node.hub.node})[:40])
        return {"ok": s in (200, 201), "asked": item["requester"]}

    def agenda(self):
        db = self.node.k.db
        return {"enabled": self.enabled, "now": [{k: v for k, v in a.items() if k != "samples"} for a in self.reflect()],
                "open_gaps": [{"kind": k, "goal": g, "count": c} for k, g, c in db.execute("SELECT kind, goal, count FROM gaps WHERE status='open'")],
                "history": [{"kind": k, "reason": r, "status": s, "result": json.loads(res) if res else None}
                            for k, r, s, res in db.execute("SELECT kind, reason, status, result FROM self_goals ORDER BY at DESC LIMIT 20")]}
