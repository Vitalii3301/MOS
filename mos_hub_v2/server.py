#!/usr/bin/env python3
"""MOS Relay Network: authenticated HTTPS mailbox relay.

Stdlib-only service for connecting independent MOS runtimes through a shared public
HTTPS endpoint. It stores messages and delivery receipts, validates payload hashes,
prevents duplicate message IDs, and records a tamper-evident relay event chain.

The service never executes message payloads and never treats remote message text as
system instructions. It is a transport and audit layer only.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

APP_VERSION = "2.0.0"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8791
MAX_BODY = 256 * 1024
MAX_PAYLOAD_BYTES = 64 * 1024
TOKEN_PREFIX = "mos_"


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_payload(payload: object) -> str:
    return sha256_text(canonical(payload))


def now() -> int:
    return int(time.time())


def iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def safe_node_id(value: str) -> bool:
    return bool(value) and len(value) <= 80 and all(c.isalnum() or c in "-_:." for c in value)


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS nodes (
  node_id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  created_at INTEGER NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  last_seen INTEGER,
  last_heartbeat INTEGER,
  heartbeat_status TEXT,
  capabilities_json TEXT
);
CREATE TABLE IF NOT EXISTS connect_requests (
  connect_id TEXT PRIMARY KEY,
  code_hash TEXT NOT NULL UNIQUE,
  node_id TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  used_at INTEGER,
  FOREIGN KEY(node_id) REFERENCES nodes(node_id)
);
CREATE TABLE IF NOT EXISTS web_sessions (
  session_hash TEXT PRIMARY KEY,
  node_id TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  FOREIGN KEY(node_id) REFERENCES nodes(node_id)
);
CREATE TABLE IF NOT EXISTS messages (
  message_id TEXT PRIMARY KEY,
  sender TEXT NOT NULL,
  recipient TEXT NOT NULL,
  kind TEXT NOT NULL,
  envelope_json TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER,
  status TEXT NOT NULL DEFAULT 'queued',
  FOREIGN KEY(sender) REFERENCES nodes(node_id),
  FOREIGN KEY(recipient) REFERENCES nodes(node_id)
);
CREATE INDEX IF NOT EXISTS messages_recipient_status_idx ON messages(recipient, status, created_at);
CREATE TABLE IF NOT EXISTS receipts (
  receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id TEXT NOT NULL,
  node_id TEXT NOT NULL,
  status TEXT NOT NULL,
  detail TEXT,
  created_at INTEGER NOT NULL,
  FOREIGN KEY(message_id) REFERENCES messages(message_id),
  FOREIGN KEY(node_id) REFERENCES nodes(node_id)
);
CREATE INDEX IF NOT EXISTS receipts_message_idx ON receipts(message_id, created_at);
CREATE TABLE IF NOT EXISTS relay_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  body_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  prev_hash TEXT NOT NULL,
  record_hash TEXT NOT NULL UNIQUE
);
"""


class RelayStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            db.commit()
        self.migrate()

    def migrate(self) -> None:
        """Apply additive schema upgrades so v1 SQLite databases boot on v2."""
        wanted = {
            "last_seen": "INTEGER",
            "last_heartbeat": "INTEGER",
            "heartbeat_status": "TEXT",
            "capabilities_json": "TEXT",
        }
        with self.lock, self.connect() as db:
            existing = {row[1] for row in db.execute("PRAGMA table_info(nodes)").fetchall()}
            for column, typ in wanted.items():
                if column not in existing:
                    db.execute(f"ALTER TABLE nodes ADD COLUMN {column} {typ}")
            db.commit()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=5, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def event(self, kind: str, body: dict) -> None:
        with self.lock, self.connect() as db:
            prev = db.execute("SELECT record_hash FROM relay_events ORDER BY seq DESC LIMIT 1").fetchone()
            prev_hash = prev[0] if prev else "GENESIS"
            created = now()
            body_json = canonical(body)
            record_hash = sha256_text(canonical({
                "kind": kind,
                "body": body,
                "created_at": created,
                "prev_hash": prev_hash,
            }))
            db.execute(
                "INSERT INTO relay_events(kind, body_json, created_at, prev_hash, record_hash) VALUES(?,?,?,?,?)",
                (kind, body_json, created, prev_hash, record_hash),
            )
            db.commit()

    def create_node(self, node_id: str, display_name: str) -> tuple[str, dict]:
        if not safe_node_id(node_id):
            raise ValueError("node_id must contain only letters, numbers, dash, underscore, colon, or dot")
        if not display_name or len(display_name) > 160:
            raise ValueError("display_name is required and must be at most 160 characters")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        token_hash = sha256_text(token)
        created = now()
        with self.lock, self.connect() as db:
            db.execute(
                "INSERT INTO nodes(node_id, display_name, token_hash, created_at) VALUES(?,?,?,?)",
                (node_id, display_name, token_hash, created),
            )
            db.commit()
        self.event("node_registered", {"node_id": node_id, "display_name": display_name})
        return token, {"node_id": node_id, "display_name": display_name, "created_at": iso(created)}

    def authenticate(self, token: str | None) -> sqlite3.Row | None:
        if not token:
            return None
        token_hash = sha256_text(token)
        seen = now()
        with self.lock, self.connect() as db:
            row = db.execute(
                "SELECT node_id, display_name, created_at, last_seen, last_heartbeat, heartbeat_status, capabilities_json "
                "FROM nodes WHERE token_hash=? AND enabled=1",
                (token_hash,),
            ).fetchone()
            if row:
                db.execute("UPDATE nodes SET last_seen=? WHERE node_id=?", (seen, row["node_id"]))
                db.commit()
                row = db.execute(
                    "SELECT node_id, display_name, created_at, last_seen, last_heartbeat, heartbeat_status, capabilities_json "
                    "FROM nodes WHERE node_id=?", (row["node_id"],)
                ).fetchone()
            return row

    def get_node(self, node_id: str, include_disabled: bool = False) -> sqlite3.Row | None:
        with self.connect() as db:
            if include_disabled:
                return db.execute("SELECT * FROM nodes WHERE node_id=?", (node_id,)).fetchone()
            return db.execute("SELECT * FROM nodes WHERE node_id=? AND enabled=1", (node_id,)).fetchone()

    def heartbeat(self, node_id: str, status: str | None, capabilities: object | None) -> dict:
        status = (status or "online").strip()[:80]
        caps_json = canonical(capabilities) if capabilities is not None else None
        if caps_json is not None and len(caps_json.encode("utf-8")) > 16 * 1024:
            raise ValueError("capabilities exceeds 16 KiB")
        ts = now()
        with self.lock, self.connect() as db:
            if caps_json is None:
                db.execute(
                    "UPDATE nodes SET last_seen=?, last_heartbeat=?, heartbeat_status=? WHERE node_id=? AND enabled=1",
                    (ts, ts, status, node_id),
                )
            else:
                db.execute(
                    "UPDATE nodes SET last_seen=?, last_heartbeat=?, heartbeat_status=?, capabilities_json=? WHERE node_id=? AND enabled=1",
                    (ts, ts, status, caps_json, node_id),
                )
            if db.total_changes == 0:
                raise ValueError("unknown or disabled node")
            db.commit()
        return {"node_id": node_id, "status": status, "last_seen": iso(ts), "recorded": True}

    def public_nodes(self, online_ttl: int = 180) -> list[dict]:
        cutoff = now() - max(30, min(int(online_ttl), 3600))
        with self.connect() as db:
            rows = db.execute(
                "SELECT node_id,display_name,created_at,enabled,last_seen,last_heartbeat,heartbeat_status,capabilities_json "
                "FROM nodes WHERE enabled=1 ORDER BY node_id"
            ).fetchall()
        result = []
        for row in rows:
            try:
                caps = json.loads(row["capabilities_json"]) if row["capabilities_json"] else None
            except Exception:
                caps = None
            result.append({
                "node_id": row["node_id"],
                "display_name": row["display_name"],
                "created_at": iso(row["created_at"]),
                "last_seen": iso(row["last_seen"]),
                "last_heartbeat": iso(row["last_heartbeat"]),
                "heartbeat_status": row["heartbeat_status"],
                "online": bool(row["last_seen"] and row["last_seen"] >= cutoff),
                "capabilities": caps,
            })
        return result

    def admin_nodes(self, online_ttl: int = 180) -> list[dict]:
        cutoff = now() - max(30, min(int(online_ttl), 3600))
        with self.connect() as db:
            rows = db.execute(
                "SELECT node_id,display_name,created_at,enabled,last_seen,last_heartbeat,heartbeat_status,capabilities_json "
                "FROM nodes ORDER BY node_id"
            ).fetchall()
        result=[]
        for row in rows:
            try:
                caps=json.loads(row["capabilities_json"]) if row["capabilities_json"] else None
            except Exception:
                caps=None
            result.append({
                "node_id":row["node_id"],"display_name":row["display_name"],
                "created_at":iso(row["created_at"]),"enabled":bool(row["enabled"]),
                "last_seen":iso(row["last_seen"]),"last_heartbeat":iso(row["last_heartbeat"]),
                "heartbeat_status":row["heartbeat_status"],
                "online":bool(row["enabled"] and row["last_seen"] and row["last_seen"]>=cutoff),
                "capabilities":caps,
            })
        return result

    def set_node_enabled(self, node_id: str, enabled: bool) -> dict:
        with self.lock, self.connect() as db:
            row=db.execute("SELECT node_id FROM nodes WHERE node_id=?",(node_id,)).fetchone()
            if not row:
                raise ValueError("node not found")
            db.execute("UPDATE nodes SET enabled=? WHERE node_id=?",(1 if enabled else 0,node_id))
            if not enabled:
                db.execute("DELETE FROM web_sessions WHERE node_id=?",(node_id,))
            db.commit()
        self.event("node_enabled" if enabled else "node_disabled", {"node_id":node_id})
        return {"node_id":node_id,"enabled":enabled}

    def rotate_node_token(self, node_id: str) -> tuple[str, dict]:
        if not self.get_node(node_id, include_disabled=True):
            raise ValueError("node not found")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        token_hash = sha256_text(token)
        with self.lock, self.connect() as db:
            db.execute("UPDATE nodes SET token_hash=? WHERE node_id=?",(token_hash,node_id))
            db.execute("DELETE FROM web_sessions WHERE node_id=?",(node_id,))
            db.commit()
        self.event("node_token_rotated", {"node_id":node_id})
        return token, {"node_id":node_id,"rotated":True}

    def create_connect_request(self, node_id: str, ttl: int = 300) -> dict:
        if not self.get_node(node_id):
            raise ValueError("unknown or disabled node")
        connect_id = "cn_" + secrets.token_urlsafe(18)
        code = secrets.token_urlsafe(24)
        created = now()
        expires = created + max(60, min(ttl, 600))
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO connect_requests(connect_id,code_hash,node_id,created_at,expires_at) VALUES(?,?,?,?,?)", (connect_id, sha256_text(code), node_id, created, expires))
            db.commit()
        self.event("connect_requested", {"connect_id": connect_id, "node_id": node_id, "expires_at": expires})
        return {"connect_id": connect_id, "code": code, "node_id": node_id, "expires_at": iso(expires), "max_uses": 1}

    def exchange_connect(self, connect_id: str, code: str) -> tuple[str, sqlite3.Row] | None:
        with self.lock, self.connect() as db:
            row = db.execute("SELECT * FROM connect_requests WHERE connect_id=?", (connect_id,)).fetchone()
            if not row or row["used_at"] is not None or row["expires_at"] <= now() or not hmac.compare_digest(row["code_hash"], sha256_text(code)):
                return None
            session = "mos_sess_" + secrets.token_urlsafe(32)
            created = now()
            expires = created + 3600
            db.execute("UPDATE connect_requests SET used_at=? WHERE connect_id=?", (created, connect_id))
            db.execute("INSERT INTO web_sessions(session_hash,node_id,created_at,expires_at) VALUES(?,?,?,?)", (sha256_text(session), row["node_id"], created, expires))
            db.commit()
            node = db.execute("SELECT node_id, display_name, created_at FROM nodes WHERE node_id=? AND enabled=1", (row["node_id"],)).fetchone()
        if node:
            self.event("connect_exchanged", {"connect_id": connect_id, "node_id": node["node_id"], "expires_at": expires})
        return (session, node) if node else None

    def authenticate_session(self, session: str | None) -> sqlite3.Row | None:
        if not session:
            return None
        with self.connect() as db:
            row = db.execute("SELECT n.node_id, n.display_name, n.created_at FROM web_sessions s JOIN nodes n ON n.node_id=s.node_id WHERE s.session_hash=? AND s.expires_at>? AND n.enabled=1", (sha256_text(session), now())).fetchone()
        return row

    def add_message(self, sender: str, envelope: dict) -> dict:
        required = ["message_id", "recipient", "kind", "payload", "payload_sha256"]
        missing = [key for key in required if key not in envelope]
        if missing:
            raise ValueError("missing fields: " + ", ".join(missing))
        message_id = envelope["message_id"]
        recipient = envelope["recipient"]
        kind = envelope["kind"]
        payload = envelope["payload"]
        if not isinstance(message_id, str) or not message_id or len(message_id) > 160:
            raise ValueError("invalid message_id")
        if not safe_node_id(recipient):
            raise ValueError("invalid recipient")
        if not isinstance(kind, str) or kind not in {"start", "finding", "review_request", "review_response", "handoff", "ack", "stop"}:
            raise ValueError("unsupported message kind")
        payload_bytes = len(canonical(payload).encode("utf-8"))
        if payload_bytes > MAX_PAYLOAD_BYTES:
            raise ValueError("payload exceeds 64 KiB")
        expected_hash = sha256_payload(payload)
        if not hmac.compare_digest(str(envelope["payload_sha256"]), expected_hash):
            raise ValueError("payload_sha256 mismatch")
        expires_at = envelope.get("expires_at")
        if expires_at is not None:
            if not isinstance(expires_at, int) or expires_at <= now():
                raise ValueError("expires_at must be a future Unix timestamp")
        recipient_row = self.get_node(recipient)
        if not recipient_row:
            raise ValueError("recipient node is not registered or enabled")
        created = now()
        safe_envelope = {
            "schema": envelope.get("schema", "mos-remote-message/v1"),
            "message_id": message_id,
            "sender": sender,
            "recipient": recipient,
            "kind": kind,
            "payload": payload,
            "payload_sha256": expected_hash,
            "created_at": envelope.get("created_at") or iso(created),
            "expires_at": expires_at,
            "reply_to": envelope.get("reply_to"),
        }
        with self.lock, self.connect() as db:
            existing = db.execute("SELECT message_id, sender, recipient, status FROM messages WHERE message_id=?", (message_id,)).fetchone()
            if existing:
                if existing["sender"] == sender and existing["recipient"] == recipient:
                    return {"message_id": message_id, "status": "duplicate_accepted", "stored": False}
                raise ValueError("message_id already belongs to another envelope")
            db.execute(
                "INSERT INTO messages(message_id,sender,recipient,kind,envelope_json,payload_sha256,created_at,expires_at,status) VALUES(?,?,?,?,?,?,?,?,?)",
                (message_id, sender, recipient, kind, canonical(safe_envelope), expected_hash, created, expires_at, "queued"),
            )
            db.commit()
        self.event("message_queued", {"message_id": message_id, "sender": sender, "recipient": recipient, "kind": kind})
        return {"message_id": message_id, "status": "queued", "stored": True}

    def mailbox(self, node_id: str, after: int = 0, limit: int = 50) -> list[dict]:
        limit = max(1, min(limit, 100))
        with self.connect() as db:
            rows = db.execute(
                "SELECT rowid AS mailbox_seq, * FROM messages WHERE recipient=? AND rowid>? ORDER BY rowid ASC LIMIT ?",
                (node_id, after, limit),
            ).fetchall()
        output = []
        for row in rows:
            envelope = json.loads(row["envelope_json"])
            if row["expires_at"] and row["expires_at"] <= now():
                envelope["relay_status"] = "expired"
            else:
                envelope["relay_status"] = row["status"]
            envelope["mailbox_seq"] = row["mailbox_seq"]
            output.append(envelope)
        return output

    def receipt(self, node_id: str, message_id: str, status: str, detail: str | None) -> dict:
        allowed = {"delivered", "read", "processing", "completed", "failed", "expired", "unverified"}
        if status not in allowed:
            raise ValueError("unsupported receipt status")
        with self.lock, self.connect() as db:
            msg = db.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()
            if not msg:
                raise ValueError("message not found")
            if msg["recipient"] != node_id:
                raise PermissionError("only the recipient can create this receipt")
            db.execute("INSERT INTO receipts(message_id,node_id,status,detail,created_at) VALUES(?,?,?,?,?)", (message_id, node_id, status, detail, now()))
            db.execute("UPDATE messages SET status=? WHERE message_id=?", (status, message_id))
            db.commit()
        self.event("delivery_receipt", {"message_id": message_id, "node_id": node_id, "status": status})
        return {"message_id": message_id, "status": status, "recorded": True}

    def stats(self, online_ttl: int = 180) -> dict:
        cutoff = now() - max(30, min(int(online_ttl), 3600))
