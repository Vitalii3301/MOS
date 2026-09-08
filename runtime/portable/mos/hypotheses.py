from __future__ import annotations
from typing import Any
from .storage import SQLiteStore
from .util import stable_id, utcnow

class HypothesisTracker:
    """Persistent, evidence-linked hypothesis state separate from episode history."""
    def __init__(self, store: SQLiteStore):
        self.store = store

    def propose(self, description: str, expected_outcome: str = "progress", probability: float = 0.5, *, reuse_active: bool = True) -> str:
        if reuse_active:
            row=self.store.conn.execute(
                """SELECT hypothesis_id FROM hypotheses WHERE description=? AND expected_outcome=?
                   AND status IN ('active','supported','weakened') ORDER BY created_at DESC LIMIT 1""",
                (description,expected_outcome),
            ).fetchone()
            if row:
                hid=row["hypothesis_id"]
                self.store.append_event(f"{hid}:reuse:{stable_id('R',{'t':utcnow()})}","hypothesis_reused",{
                    "hypothesis_id":hid,"description":description,"expected_outcome":expected_outcome,
                })
                return hid
        created = utcnow()
        hid = stable_id("HYP", {"description": description, "created_at": created})
        with self.store.tx() as c:
            c.execute(
                """INSERT INTO hypotheses(hypothesis_id,description,probability,expected_outcome,success_count,test_count,created_at,status)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (hid, description, float(probability), expected_outcome, 0, 0, created, "active"),
            )
        self.store.append_event(f"{hid}:propose", "hypothesis_proposed", {
            "hypothesis_id": hid, "description": description, "expected_outcome": expected_outcome,
            "probability": float(probability),
        })
        return hid

    def update(self, hypothesis_id: str, success: bool) -> dict[str, Any]:
        row = self.store.conn.execute("SELECT * FROM hypotheses WHERE hypothesis_id=?", (hypothesis_id,)).fetchone()
        if not row:
            raise KeyError(hypothesis_id)
        success_count = int(row["success_count"]) + (1 if success else 0)
        test_count = int(row["test_count"]) + 1
        # Laplace-smoothed empirical probability avoids unjustified 0/1 certainty after one test.
        probability = (success_count + 1.0) / (test_count + 2.0)
        status = "supported" if test_count >= 3 and probability >= 0.7 else "weakened" if test_count >= 3 and probability <= 0.3 else "active"
        with self.store.tx() as c:
            c.execute(
                "UPDATE hypotheses SET success_count=?,test_count=?,probability=?,status=? WHERE hypothesis_id=?",
                (success_count, test_count, probability, status, hypothesis_id),
            )
        body = {"hypothesis_id": hypothesis_id, "success": bool(success), "success_count": success_count,
                "test_count": test_count, "probability": probability, "status": status}
        self.store.append_event(f"{hypothesis_id}:test:{test_count}", "hypothesis_test", body)
        return body

    def active(self, limit: int = 8) -> list[dict[str, Any]]:
        rows = self.store.conn.execute(
            """SELECT * FROM hypotheses WHERE status IN ('active','supported','weakened')
               ORDER BY test_count DESC, probability DESC, created_at DESC LIMIT ?""", (limit,)
        )
        return [dict(r) for r in rows]

    def get(self, hypothesis_id: str) -> dict[str, Any]:
        row = self.store.conn.execute("SELECT * FROM hypotheses WHERE hypothesis_id=?", (hypothesis_id,)).fetchone()
        if not row:
            raise KeyError(hypothesis_id)
        return dict(row)
