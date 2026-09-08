from __future__ import annotations
import json, sqlite3, threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable
from .util import utcnow, canonical_bytes, sha256_bytes

SCHEMA_VERSION = 4

class SQLiteStore:
    """Transactional persistence for MOS state, memory, strategies, memes and evidence."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    @contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def _init_schema(self):
        ddl = [
            """CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS memory(
                memory_id TEXT PRIMARY KEY,content TEXT NOT NULL,kind TEXT NOT NULL,
                importance REAL NOT NULL,strength REAL NOT NULL,usage_count INTEGER NOT NULL,
                created_at TEXT NOT NULL,metadata TEXT NOT NULL,vector TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS hypotheses(
                hypothesis_id TEXT PRIMARY KEY,description TEXT NOT NULL,probability REAL NOT NULL,
                expected_outcome TEXT NOT NULL,success_count INTEGER NOT NULL,test_count INTEGER NOT NULL,
                created_at TEXT NOT NULL,status TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS strategies(
                strategy_id TEXT PRIMARY KEY,name TEXT UNIQUE NOT NULL,level INTEGER NOT NULL,
                triggers TEXT NOT NULL,action_kind TEXT NOT NULL,action_arg TEXT,priority REAL NOT NULL,
                parent_id TEXT,status TEXT NOT NULL,version INTEGER NOT NULL,learned_from TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS strategy_stats(
                strategy_id TEXT PRIMARY KEY,uses INTEGER NOT NULL,success INTEGER NOT NULL,fail INTEGER NOT NULL,
                last_used TEXT, FOREIGN KEY(strategy_id) REFERENCES strategies(strategy_id) ON DELETE CASCADE)""",
            """CREATE TABLE IF NOT EXISTS strategy_evolution(
                strategy_id TEXT PRIMARY KEY,last_evolved_uses INTEGER NOT NULL,last_evolved_success INTEGER NOT NULL,
                last_evolved_fail INTEGER NOT NULL,generation INTEGER NOT NULL,updated_at TEXT NOT NULL,
                FOREIGN KEY(strategy_id) REFERENCES strategies(strategy_id) ON DELETE CASCADE)""",
            """CREATE TABLE IF NOT EXISTS memes(
                meme_id TEXT PRIMARY KEY,name TEXT UNIQUE NOT NULL,description TEXT NOT NULL,
                current_version INTEGER NOT NULL,fitness REAL,status TEXT NOT NULL,created_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS meme_versions(
                meme_id TEXT NOT NULL,version INTEGER NOT NULL,code TEXT NOT NULL,parents TEXT NOT NULL,
                fitness REAL,created_at TEXT NOT NULL,change_note TEXT NOT NULL,
                PRIMARY KEY(meme_id,version), FOREIGN KEY(meme_id) REFERENCES memes(meme_id) ON DELETE CASCADE)""",
            """CREATE TABLE IF NOT EXISTS episodes(
                episode_id TEXT PRIMARY KEY,goal TEXT NOT NULL,hypothesis TEXT NOT NULL,evidence TEXT NOT NULL,
                decision TEXT,action TEXT,outcome TEXT,revision TEXT,status TEXT NOT NULL,
                created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS meta_memory(
                memory_id TEXT PRIMARY KEY,kind TEXT NOT NULL,statement TEXT NOT NULL,importance REAL NOT NULL,
                confidence REAL NOT NULL,verification_status TEXT NOT NULL,evidence_refs TEXT NOT NULL,
                review_status TEXT NOT NULL,review_when TEXT NOT NULL,metadata TEXT NOT NULL,updated_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS self_model(
                item_id TEXT PRIMARY KEY,kind TEXT NOT NULL,domain TEXT NOT NULL,statement TEXT NOT NULL,
                confidence REAL NOT NULL,status TEXT NOT NULL,evidence_refs TEXT NOT NULL,
                scope_limit TEXT NOT NULL,metadata TEXT NOT NULL,updated_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT UNIQUE NOT NULL,event_type TEXT NOT NULL,
                body TEXT NOT NULL,previous_hash TEXT,event_hash TEXT NOT NULL,created_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS snapshots(
                snapshot_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,state_hash TEXT NOT NULL,
                payload TEXT NOT NULL,created_at TEXT NOT NULL,parent_snapshot TEXT)""",
        ]
        with self.tx() as c:
            for q in ddl:
                c.execute(q)
            c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)",(str(SCHEMA_VERSION),))

    def close(self):
        self.conn.close()

    def set_state(self, key: str, value: Any):
        now = utcnow()
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True)
        with self.tx() as c:
            c.execute("""INSERT INTO state(key,value,updated_at) VALUES(?,?,?)
                         ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                      (key,raw,now))

    def get_state(self, key: str, default=None):
        r = self.conn.execute("SELECT value FROM state WHERE key=?",(key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def all_state(self) -> dict[str,Any]:
        return {r["key"]: json.loads(r["value"]) for r in self.conn.execute("SELECT key,value FROM state ORDER BY key")}

    def append_event(self, event_id: str, event_type: str, body: dict[str,Any]) -> str:
        # Previous hash lookup and insert must be in the same locked transaction;
        # otherwise concurrent cycles can fork the local hash chain.
        with self.tx() as c:
            prev = c.execute("SELECT event_hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            previous_hash = prev["event_hash"] if prev else None
            envelope = {"event_id":event_id,"event_type":event_type,"body":body,"previous_hash":previous_hash}
            digest = sha256_bytes(canonical_bytes(envelope))
            c.execute("INSERT INTO events(event_id,event_type,body,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?)",
                      (event_id,event_type,json.dumps(body,ensure_ascii=False,sort_keys=True),previous_hash,digest,utcnow()))
        return digest

    def verify_event_chain(self) -> bool:
        prev = None
        for r in self.conn.execute("SELECT event_id,event_type,body,previous_hash,event_hash FROM events ORDER BY seq"):
            body = json.loads(r["body"])
            env = {"event_id":r["event_id"],"event_type":r["event_type"],"body":body,"previous_hash":prev}
            if r["previous_hash"] != prev or sha256_bytes(canonical_bytes(env)) != r["event_hash"]:
                return False
            prev = r["event_hash"]
        return True

    def backup(self, target: str | Path):
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        other = sqlite3.connect(str(target))
        with other:
            self.conn.backup(other)
        other.close()
