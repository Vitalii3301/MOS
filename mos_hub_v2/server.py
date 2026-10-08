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
