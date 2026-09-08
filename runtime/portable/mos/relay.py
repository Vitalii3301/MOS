from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from .storage import SQLiteStore, SCHEMA_VERSION
from .util import atomic_write_json, sha256_json, stable_id, utcnow

# Tables that constitute transferable cognitive/runtime continuity.
# snapshots is intentionally excluded to avoid recursively embedding snapshots in snapshots.
FULL_TABLES = (
    "state", "memory", "hypotheses", "strategies", "strategy_stats", "strategy_evolution",
    "memes", "meme_versions", "episodes", "meta_memory", "self_model", "events",
)

DELETE_ORDER = (
    "strategy_evolution", "strategy_stats", "meme_versions", "strategies", "memes", "memory", "hypotheses",
    "episodes", "meta_memory", "self_model", "events", "state",
)
INSERT_ORDER = (
    "state", "memory", "hypotheses", "strategies", "strategy_stats", "strategy_evolution", "memes",
    "meme_versions", "episodes", "meta_memory", "self_model", "events",
)

class StateRelay:
    """Portable, hash-verified full-state relay with optimistic conflict protection."""
    def __init__(self, store: SQLiteStore):
        self.store = store

    def _dump_table(self, table: str) -> list[dict[str, Any]]:
        order = ""
        if table == "events":
            order = " ORDER BY seq"
        elif table == "meme_versions":
            order = " ORDER BY meme_id,version"
        else:
            cols = [r["name"] for r in self.store.conn.execute(f"PRAGMA table_info({table})")]
            if cols:
                order = f" ORDER BY {cols[0]}"
        return [dict(r) for r in self.store.conn.execute(f"SELECT * FROM {table}{order}")]

    def _full_payload(self, revision: int, parent_snapshot: str | None) -> dict[str, Any]:
        tables = {t: self._dump_table(t) for t in FULL_TABLES}
        table_hashes = {t: sha256_json(rows) for t, rows in tables.items()}
        head = self.store.conn.execute("SELECT event_hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        return {
            "schema": "mos-relay-snapshot/v4",
            "schema_version": SCHEMA_VERSION,
            "revision": revision,
            "tables": tables,
            "table_hashes": table_hashes,
            "table_counts": {t: len(rows) for t, rows in tables.items()},
            "event_head": head["event_hash"] if head else None,
            "parent_snapshot": parent_snapshot,
            "created_at": utcnow(),
            "boundaries": {
                "hidden_cross_chat_memory": False,
                "base_model_weight_change": False,
                "snapshot_catalog_recursively_embedded": False,
            },
        }

    def export(self, path: str | Path, parent_snapshot: str | None = None) -> dict[str, Any]:
        revision = int(self.store.get_state("relay_revision", 0)) + 1
        payload = self._full_payload(revision, parent_snapshot)
        state_hash = sha256_json(payload)
        sid = stable_id("SNAP", {"revision": revision, "hash": state_hash})
        envelope = {"snapshot_id": sid, "state_hash": state_hash, "payload": payload}
        atomic_write_json(path, envelope)
        self.store.set_state("relay_revision", revision)
        with self.store.tx() as c:
            c.execute(
                "INSERT INTO snapshots(snapshot_id,revision,state_hash,payload,created_at,parent_snapshot) VALUES(?,?,?,?,?,?)",
                (sid, revision, state_hash, json.dumps(payload, ensure_ascii=False, sort_keys=True), utcnow(), parent_snapshot),
            )
        self.store.append_event(f"{sid}:export", "relay_export", {
            "snapshot_id": sid, "revision": revision, "state_hash": state_hash,
            "table_counts": payload["table_counts"],
        })
        return envelope

    @staticmethod
    def _validate_envelope(env: dict[str, Any]) -> None:
        if env.get("payload", {}).get("schema") != "mos-relay-snapshot/v4":
            raise ValueError("unsupported relay snapshot schema")
        if int(env.get("payload", {}).get("schema_version", -1)) != SCHEMA_VERSION:
            raise ValueError("relay snapshot database schema version mismatch")
        if sha256_json(env["payload"]) != env.get("state_hash"):
            raise ValueError("snapshot hash mismatch")
        tables = env["payload"].get("tables", {})
        hashes = env["payload"].get("table_hashes", {})
        counts = env["payload"].get("table_counts", {})
        for table in FULL_TABLES:
            if table not in tables:
                raise ValueError(f"snapshot missing table: {table}")
            if sha256_json(tables[table]) != hashes.get(table):
                raise ValueError(f"snapshot table hash mismatch: {table}")
            if len(tables[table]) != counts.get(table):
                raise ValueError(f"snapshot table count mismatch: {table}")

    def import_snapshot(
        self,
        path: str | Path,
        expected_revision: int | None = None,
        *,
        create_backup: bool = True,
    ) -> dict[str, Any]:
        env = json.loads(Path(path).read_text(encoding="utf-8"))
        self._validate_envelope(env)
        current = int(self.store.get_state("relay_revision", 0))
        if expected_revision is not None and current != expected_revision:
            raise RuntimeError(f"relay conflict current={current} expected={expected_revision}")

        backup_path = None
        if create_backup and self.store.path.exists():
            backup_path = self.store.path.with_suffix(self.store.path.suffix + ".pre_restore.bak")
            self.store.backup(backup_path)

        tables = env["payload"]["tables"]
        try:
            with self.store.tx() as c:
                for table in DELETE_ORDER:
                    c.execute(f"DELETE FROM {table}")
                for table in INSERT_ORDER:
                    rows = tables[table]
                    if not rows:
                        continue
                    cols = list(rows[0].keys())
                    q = f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' for _ in cols)})"
                    for row in rows:
                        c.execute(q, tuple(row[col] for col in cols))
            # Snapshot revision is authoritative after restoration, even though
            # the exported state table contains the pre-export relay_revision.
            self.store.set_state("relay_revision", int(env["payload"]["revision"]))
            self.store.append_event(f"{env['snapshot_id']}:import:{utcnow()}", "relay_import", {
                "snapshot_id": env["snapshot_id"],
                "revision": env["payload"]["revision"],
                "restored_table_counts": env["payload"]["table_counts"],
                "backup_path": str(backup_path) if backup_path else None,
            })
            if not self.store.verify_event_chain():
                raise RuntimeError("event chain invalid after relay restore")
        except Exception:
            # Transaction failures automatically roll back. A durable pre-restore
            # backup is retained for recovery from non-transactional host failures.
            raise

        # Keep an audit record of the imported snapshot without recursively
        # embedding the existing snapshot catalog in future exports.
        with self.store.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO snapshots(snapshot_id,revision,state_hash,payload,created_at,parent_snapshot) VALUES(?,?,?,?,?,?)",
                (env["snapshot_id"], int(env["payload"]["revision"]), env["state_hash"],
                 json.dumps(env["payload"], ensure_ascii=False, sort_keys=True), utcnow(), env["payload"].get("parent_snapshot")),
            )
        return {**env, "backup_path": str(backup_path) if backup_path else None}
