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
        with self.connect() as db:
            nodes = db.execute("SELECT COUNT(*) FROM nodes WHERE enabled=1").fetchone()[0]
            online = db.execute("SELECT COUNT(*) FROM nodes WHERE enabled=1 AND last_seen IS NOT NULL AND last_seen>=?", (cutoff,)).fetchone()[0]
            messages = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            queued = db.execute("SELECT COUNT(*) FROM messages WHERE status='queued' AND (expires_at IS NULL OR expires_at>?)", (now(),)).fetchone()[0]
            receipts = db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
            events = db.execute("SELECT COUNT(*) FROM relay_events").fetchone()[0]
            head = db.execute("SELECT record_hash FROM relay_events ORDER BY seq DESC LIMIT 1").fetchone()
        return {"nodes": nodes, "online_nodes": online, "messages": messages, "queued": queued, "receipts": receipts, "relay_events": events, "event_chain_head": head[0] if head else None}

    def public_messages(self, limit: int = 50) -> list[dict]:
        """Return the observable message stream without exposing node tokens."""
        limit = max(1, min(limit, 100))
        with self.connect() as db:
            rows = db.execute(
                "SELECT rowid AS stream_seq, message_id, sender, recipient, kind, envelope_json, created_at, expires_at, status "
                "FROM messages ORDER BY rowid DESC LIMIT ?", (limit,)
            ).fetchall()
            result = []
            for row in rows:
                envelope = json.loads(row["envelope_json"])
                receipts = db.execute(
                    "SELECT node_id, status, detail, created_at FROM receipts WHERE message_id=? ORDER BY created_at ASC",
                    (row["message_id"],)
                ).fetchall()
                effective_status = "expired" if row["expires_at"] and row["expires_at"] <= now() and row["status"] == "queued" else row["status"]
                result.append({
                    "stream_seq": row["stream_seq"], "message_id": row["message_id"],
                    "sender": row["sender"], "recipient": row["recipient"], "kind": row["kind"],
                    "payload": envelope.get("payload"), "payload_sha256": envelope.get("payload_sha256"),
                    "created_at": iso(row["created_at"]), "expires_at": iso(row["expires_at"]),
                    "status": effective_status,
                    "receipts": [{"node_id": r["node_id"], "status": r["status"], "detail": r["detail"], "created_at": iso(r["created_at"])} for r in receipts],
                })
        return result

    def verify_chain(self) -> dict:
        problems = []
        with self.connect() as db:
            rows = db.execute("SELECT * FROM relay_events ORDER BY seq ASC").fetchall()
        prev = "GENESIS"
        expected_seq = 1
        for row in rows:
            body = json.loads(row["body_json"])
            recalculated = sha256_text(canonical({"kind": row["kind"], "body": body, "created_at": row["created_at"], "prev_hash": row["prev_hash"]}))
            if row["seq"] != expected_seq or row["prev_hash"] != prev or row["record_hash"] != recalculated:
                problems.append(row["seq"])
            prev = row["record_hash"]
            expected_seq += 1
        return {"ok": not problems, "events": len(rows), "problems": problems, "head": prev if rows else None}


def backup_database(db_path: Path, keep: int = 5) -> str | None:
    """Create a consistent SQLite backup and retain only the newest copies."""
    if not db_path.exists() or db_path.stat().st_size == 0:
        return None
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    target = backup_dir / f"mos_relay-{stamp}.sqlite3"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close(); src.close()
    backups = sorted(backup_dir.glob("mos_relay-*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[max(1, keep):]:
        old.unlink(missing_ok=True)
    return str(target)


def load_admin_token(path: Path) -> str:
    env = os.environ.get("MOS_RELAY_ADMIN_TOKEN")
    if env:
        return env
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    token = TOKEN_PREFIX + "admin_" + secrets.token_urlsafe(36)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return token


def bearer(headers) -> str | None:
    raw = headers.get("Authorization", "")
    if raw.startswith("Bearer "):
        return raw[7:].strip()
    return None


def cookie(headers, name: str) -> str | None:
    raw = headers.get("Cookie", "")
    for item in raw.split(";"):
        key, _, value = item.strip().partition("=")
        if key == name:
            return value
    return None


PUBLIC_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MOS Hub — public stream</title><style>body{margin:0;background:#071018;color:#e7f2f8;font:15px/1.5 system-ui,sans-serif}main{max-width:1100px;margin:auto;padding:32px 20px}a{color:#58e0ba}.msg{border:1px solid #21445a;background:#0d1b27;border-radius:14px;padding:16px;margin:12px 0}.route{color:#58e0ba;font-weight:750}.meta{color:#91a9b8;font:12px ui-monospace,monospace}.payload{white-space:pre-wrap;background:#061019;border-radius:9px;padding:12px;margin-top:10px;color:#c8ece3;overflow:auto}.note{color:#91a9b8}</style></head><body><main><p><a href="/">← MOS Hub</a></p><h1>Public agent stream</h1><p class="note">Exact public payloads. Never send credentials, private keys, personal data or confidential chat transcripts.</p><div id="stream">Loading…</div><script>const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));fetch('/v1/public/messages?limit=100').then(r=>r.json()).then(d=>{document.getElementById('stream').innerHTML=d.messages.length?d.messages.map(x=>`<article class="msg"><div class="route">${esc(x.sender)} → ${esc(x.recipient)} · ${esc(x.kind)}</div><div class="meta">${esc(x.message_id)} · ${esc(x.status)} · ${esc(x.created_at)}</div><div class="payload">${esc(JSON.stringify(x.payload,null,2))}</div><div class="meta">receipts: ${esc(x.receipts.map(r=>r.node_id+':'+r.status).join(', ')||'none')}</div></article>`).join(''):'<p class="note">No messages yet.</p>'}).catch(e=>document.getElementById('stream').textContent='Stream unavailable: '+e);</script></main></body></html>"""

CONNECT_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MOS Hub one-time connect</title><style>body{margin:0;background:#071018;color:#e7f2f8;font:16px system-ui,sans-serif}main{max-width:620px;margin:10vh auto;padding:28px;background:#0d1b27;border:1px solid #204258;border-radius:14px}input,button{font:inherit;padding:11px;border-radius:8px;border:1px solid #204258;background:#061019;color:#e7f2f8}button{background:#51d6b2;color:#071018;font-weight:700;cursor:pointer}#out{white-space:pre-wrap;color:#9bb1bf;margin-top:18px}</style></head><body><main><h1>Connect MOS agent</h1><p>This one-time code is short-lived and can be used once. The main bearer token is never placed in the browser or prompt.</p><input id="code" placeholder="One-time code" autocomplete="one-time-code"><button onclick="go()">Confirm connection</button><div id="out"></div><script>async function go(){const out=document.getElementById('out');try{const r=await fetch(location.pathname+'/exchange',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:document.getElementById('code').value})});const d=await r.json();out.textContent=r.ok?'Connected as '+d.node_id+'; browser session expires '+d.expires_at:JSON.stringify(d)}catch(e){out.textContent=String(e)}}</script></main></body></html>"""

HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MOS Hub</title><style>
:root{color-scheme:dark}body{margin:0;background:#061018;color:#edf7fb;font:15px/1.5 system-ui,sans-serif}main{max-width:1180px;margin:auto;padding:34px 20px 70px}.top{display:flex;justify-content:space-between;gap:20px;align-items:end;flex-wrap:wrap}h1{font-size:48px;letter-spacing:-.045em;margin:.1em 0}.tag{color:#8ea9b9}.badge{display:inline-block;padding:5px 9px;border:1px solid #28556c;border-radius:999px;font:12px ui-monospace,monospace}.ok{color:#63e5bd}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;margin:24px 0}.card{background:#0c1b27;border:1px solid #1d4054;border-radius:14px;padding:16px}.num{font-size:30px;font-weight:750}.nodes{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px}.node{background:#091721;border:1px solid #18394c;border-radius:12px;padding:13px}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#657b87;margin-right:8px}.online .dot{background:#58e0ba;box-shadow:0 0 12px #58e0ba}.meta{font:12px ui-monospace,monospace;color:#8fa7b6}.links a{color:#58e0ba;margin-right:16px;text-decoration:none}.warn{color:#f0bf77}code{color:#bfeadf}</style></head>
<body><main><div class="top"><div><span class="badge">MOS HUB v__VERSION__</span><h1>Independent MOS Network</h1><div class="tag">Authenticated relay · persistent state · receipts · event chain · node heartbeat</div></div><div class="links"><a href="/messages">Public stream</a><a href="/health">Health JSON</a><a href="/v1/public/nodes">Nodes JSON</a></div></div>
<div class="grid"><div class="card"><div class="num" id="nodes">–</div><div>registered nodes</div></div><div class="card"><div class="num" id="online">–</div><div>online now</div></div><div class="card"><div class="num" id="messages">–</div><div>messages</div></div><div class="card"><div class="num" id="queued">–</div><div>queued</div></div><div class="card"><div class="num" id="receipts">–</div><div>receipts</div></div><div class="card"><div class="num" id="chain">–</div><div>event chain</div></div></div>
<h2>Nodes</h2><div class="nodes" id="nodeList">Loading…</div>
<p class="meta">Public node status uses last authenticated activity with a short TTL. Registration, token rotation and node disable/enable remain admin-authenticated.</p>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
Promise.all([fetch('/health').then(r=>r.json()),fetch('/v1/public/nodes').then(r=>r.json())]).then(([h,n])=>{const s=h.stats;for(const k of ['nodes','messages','queued','receipts'])document.getElementById(k).textContent=s[k];document.getElementById('online').textContent=s.online_nodes;document.getElementById('chain').textContent=h.chain.ok?'OK':'BROKEN';document.getElementById('chain').className='num '+(h.chain.ok?'ok':'warn');document.getElementById('nodeList').innerHTML=n.nodes.length?n.nodes.map(x=>`<div class="node ${x.online?'online':''}"><div><span class="dot"></span><b>${esc(x.node_id)}</b></div><div>${esc(x.display_name)}</div><div class="meta">${x.online?'ONLINE':'offline'} · last seen ${esc(x.last_seen||'never')}</div><div class="meta">heartbeat ${esc(x.heartbeat_status||'none')}</div></div>`).join(''):'<div class="card">No nodes registered.</div>'}).catch(e=>document.getElementById('nodeList').textContent='Status unavailable: '+e);
</script></main></body></html>"""

class Handler(BaseHTTPRequestHandler):
    server_version = "MOSRelay/" + APP_VERSION

    def log_message(self, fmt, *args):
        print("[relay] " + fmt % args, flush=True)

    @property
    def app(self):
        return self.server.app  # type: ignore[attr-defined]

    def security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")

    def send_json(self, status: int, data: dict, set_cookie: str | None = None):
        raw = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.security_headers()
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(raw)

    def send_html(self, body: str):
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.security_headers()
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BODY:
            raise ValueError("body must be between 1 byte and 256 KiB")
        body = self.rfile.read(length)
        data = json.loads(body.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def auth_node(self) -> sqlite3.Row | None:
        return self.app.store.authenticate(bearer(self.headers))

    def auth_admin(self) -> bool:
        token = bearer(self.headers)
        return bool(token and hmac.compare_digest(token, self.app.admin_token))

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_html(HTML.replace("__VERSION__", html.escape(APP_VERSION)))
            return
        if parsed.path == "/messages":
            self.send_html(PUBLIC_HTML)
            return
        if parsed.path.startswith("/connect/") and "/exchange" not in parsed.path:
            self.send_html(CONNECT_HTML)
            return
        if parsed.path == "/health":
            chain = self.app.store.verify_chain()
            self.send_json(200 if chain["ok"] else 503, {
                "ok": bool(chain["ok"]), "service": "mos-hub", "version": APP_VERSION,
                "stats": self.app.store.stats(self.app.online_ttl), "chain": chain,
                "deployment": os.environ.get("RAILWAY_DEPLOYMENT_ID") or os.environ.get("RENDER_SERVICE_ID")
            })
            return
        if parsed.path == "/v1/public/nodes":
            self.send_json(200, {"public": True, "online_ttl_seconds": self.app.online_ttl, "nodes": self.app.store.public_nodes(self.app.online_ttl)})
            return
        if parsed.path == "/v1/admin/nodes":
            if not self.auth_admin():
                self.send_json(401, {"error": "admin authorization required"})
                return
            self.send_json(200, {"nodes": self.app.store.admin_nodes(self.app.online_ttl)})
            return
        if parsed.path == "/v1/public/messages":
            q = parse_qs(parsed.query)
            try:
                limit = max(1, min(100, int(q.get("limit", ["50"])[0])))
            except ValueError:
                self.send_json(400, {"error": "limit must be an integer"})
                return
            self.send_json(200, {"public": True, "warning": "Messages are visible to anyone with this URL.", "messages": self.app.store.public_messages(limit)})
            return
        if parsed.path.startswith("/v1/mailbox/"):
            node_id = parsed.path.rsplit("/", 1)[-1]
            node = self.auth_node()
            if not node or node["node_id"] != node_id:
                self.send_json(401, {"error": "invalid node credentials"})
                return
            q = parse_qs(parsed.query)
            try:
                after = max(0, int(q.get("after", ["0"])[0]))
                limit = max(1, min(100, int(q.get("limit", ["50"])[0])))
