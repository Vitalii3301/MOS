"""MOS Hub, built from zero: a self-contained relay with the same wire protocol the MemeOS node speaks.

Standard library only (http.server + sqlite3). Durable (SQLite), token-authenticated, payload-hash-verifying,
duplicate-safe. Started in-process by the portable launcher when no external hub is reachable, or stand-alone:
    python3 localhub.py --db hub.sqlite --port 8765

Endpoints (subset of the production hub that MemeOS and the agent bridge use):
  GET  /health                         liveness
  GET  /v1/public/nodes                who exists / who was seen recently
  POST /v1/nodes/register   (admin)    create a node identity, returns its token once
  POST /v1/heartbeat        (node)     presence
  POST /v1/messages         (node)     send; body = mos-remote-message/v1; 409 on duplicate message_id
  GET  /v1/mailbox/{node}?after=&limit=  (node, own mailbox only)
  POST /v1/messages/{id}/receipts (node) read/completed/failed receipts
Remote payloads are DATA. The hub never executes anything.
"""
import hashlib, json, secrets, sqlite3, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

MAX_BODY = 256 * 1024

def canon(v): return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes(node_id TEXT PRIMARY KEY, token_sha256 TEXT NOT NULL, capabilities TEXT, created REAL, last_seen REAL, heartbeat TEXT);
CREATE TABLE IF NOT EXISTS messages(stream_seq INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT UNIQUE NOT NULL, sender TEXT NOT NULL,
  recipient TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL, expires_at INTEGER, created REAL);
CREATE TABLE IF NOT EXISTS receipts(message_id TEXT, node TEXT, status TEXT, detail TEXT, at REAL);
CREATE TABLE IF NOT EXISTS hub_meta(key TEXT PRIMARY KEY, value TEXT);
"""

class HubStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None); self.lock = threading.Lock()
        self.db.executescript(SCHEMA)
        row = self.db.execute("SELECT value FROM hub_meta WHERE key='admin_sha256'").fetchone()
        self.admin_token = None
        if not row:                       # first start: only the admin token's hash is ever stored
            import os
            preset = os.environ.get("MOS_HUB_ADMIN_TOKEN_SHA256", "")      # deployer keeps the token; the server never sees it
            if len(preset) == 64: h = preset
            else: self.admin_token = secrets.token_urlsafe(32); h = hashlib.sha256(self.admin_token.encode()).hexdigest()
            self.db.execute("INSERT INTO hub_meta VALUES('admin_sha256',?)", (h,))
    def q(self, sql, args=()):
        with self.lock: return self.db.execute(sql, args).fetchall()
    def node_of(self, token):
        r = self.q("SELECT node_id FROM nodes WHERE token_sha256=?", (hashlib.sha256((token or "").encode()).hexdigest(),))
        return r[0][0] if r else None
    def is_admin(self, token):
        return bool(self.q("SELECT 1 FROM hub_meta WHERE key='admin_sha256' AND value=?", (hashlib.sha256((token or "").encode()).hexdigest(),)))
    def register(self, node_id, capabilities=None):
        tok = secrets.token_urlsafe(32)
        self.q("INSERT INTO nodes VALUES(?,?,?,?,?,?)", (node_id, hashlib.sha256(tok.encode()).hexdigest(), canon(capabilities or {}), time.time(), None, None))
        return tok

def make_handler(store):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *a): pass
        def _out(self, code, obj):
            raw = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        def _auth(self):
            a = self.headers.get("Authorization", ""); return a[7:] if a.startswith("Bearer ") else None
        def _body(self):
            n = int(self.headers.get("Content-Length", "0") or 0)
            if n > MAX_BODY: self.rfile.read(n); return None, "body_too_large"
            try: return json.loads(self.rfile.read(n) or b"{}"), None
            except ValueError: return None, "bad_json"
        def do_GET(self):
            u = urlparse(self.path); tok = self._auth()
            if u.path in ("/health", "/v1/health"): return self._out(200, {"ok": True, "hub": "mos-localhub/1", "time": int(time.time())})
            if u.path == "/v1/public/nodes":
                now = time.time()
                return self._out(200, {"nodes": [{"node_id": n, "enabled": True, "online": bool(ls and now - ls < 300), "last_seen": ls, "heartbeat_status": hb,
                                                  "capabilities": json.loads(c or "{}")} for n, c, ls, hb in store.q("SELECT node_id,capabilities,last_seen,heartbeat FROM nodes ORDER BY node_id")]})
            if u.path.startswith("/v1/mailbox/"):
                node = store.node_of(tok)
                if not node: return self._out(401, {"error": "unauthorized"})
                if u.path.split("/")[3] != node: return self._out(403, {"error": "not_your_mailbox"})
                qs = parse_qs(u.query); after = int(qs.get("after", ["0"])[0]); limit = min(200, int(qs.get("limit", ["50"])[0]))
                store.q("UPDATE nodes SET last_seen=? WHERE node_id=?", (time.time(), node))
                rows = store.q("SELECT stream_seq,message_id,sender,recipient,kind,payload,payload_sha256,expires_at,created FROM messages WHERE recipient=? AND stream_seq>? ORDER BY stream_seq LIMIT ?", (node, after, limit))
                return self._out(200, {"messages": [{"stream_seq": s, "message_id": m, "sender": se, "recipient": r, "kind": k, "payload": json.loads(p), "payload_sha256": h,
                                                     "expires_at": e, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(c))} for s, m, se, r, k, p, h, e, c in rows]})
            return self._out(404, {"error": "not_found"})
        def do_POST(self):
            u = urlparse(self.path); tok = self._auth(); body, err = self._body()
            if err: return self._out(413 if err == "body_too_large" else 400, {"error": err})
            if u.path == "/v1/nodes/register":
                if not store.is_admin(tok): return self._out(401, {"error": "admin_required"})
                nid = body.get("node_id")
                if not isinstance(nid, str) or not (2 <= len(nid) <= 64) or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in nid): return self._out(400, {"error": "bad_node_id"})
                if store.q("SELECT 1 FROM nodes WHERE node_id=?", (nid,)): return self._out(409, {"error": "node_exists"})
                return self._out(201, {"node_id": nid, "token": store.register(nid, body.get("capabilities"))})
            node = store.node_of(tok)
            if not node: return self._out(401, {"error": "unauthorized"})
            store.q("UPDATE nodes SET last_seen=? WHERE node_id=?", (time.time(), node))
            if u.path == "/v1/heartbeat":
                store.q("UPDATE nodes SET heartbeat=?, capabilities=COALESCE(?,capabilities) WHERE node_id=?", (str(body.get("status"))[:60], canon(body["capabilities"]) if body.get("capabilities") else None, node))
                return self._out(200, {"ok": True, "node_id": node})
            if u.path == "/v1/messages":
                need = ("message_id", "recipient", "kind", "payload", "payload_sha256")
                if any(k not in body for k in need): return self._out(400, {"error": "missing_fields"})
                calc = hashlib.sha256(canon(body["payload"]).encode()).hexdigest()
                if calc != body["payload_sha256"]: return self._out(400, {"error": "payload_hash_mismatch", "calculated": calc})
                if not store.q("SELECT 1 FROM nodes WHERE node_id=?", (body["recipient"],)): return self._out(404, {"error": "unknown_recipient"})
                if body.get("expires_at") and body["expires_at"] < time.time(): return self._out(400, {"error": "expired"})
                try:
                    with store.lock:
                        cur = store.db.execute("INSERT INTO messages(message_id,sender,recipient,kind,payload,payload_sha256,expires_at,created) VALUES(?,?,?,?,?,?,?,?)",
                                               (str(body["message_id"])[:200], node, body["recipient"], str(body["kind"])[:80], canon(body["payload"]), calc, body.get("expires_at"), time.time()))
                        seq = cur.lastrowid
                except sqlite3.IntegrityError:
                    return self._out(409, {"error": "duplicate_message_id"})
                return self._out(201, {"ok": True, "stream_seq": seq, "message_id": body["message_id"]})
            parts = u.path.split("/")
            if len(parts) == 5 and parts[1] == "v1" and parts[2] == "messages" and parts[4] == "receipts":
                row = store.q("SELECT recipient FROM messages WHERE message_id=?", (parts[3],))
                if not row: return self._out(404, {"error": "unknown_message"})
                if row[0][0] != node: return self._out(403, {"error": "not_recipient"})
                store.q("INSERT INTO receipts VALUES(?,?,?,?,?)", (parts[3], node, str(body.get("status"))[:20], str(body.get("detail"))[:1000], time.time()))
                return self._out(201, {"ok": True})
            return self._out(404, {"error": "not_found"})
    return H

def start(db_path, port=0, host="127.0.0.1"):
    """Start the hub in a daemon thread. Returns (url, store, server)."""
    store = HubStore(db_path); srv = ThreadingHTTPServer((host, port), make_handler(store)); srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://{host}:{srv.server_address[1]}", store, srv

if __name__ == "__main__":
    import argparse
    import os
    ap = argparse.ArgumentParser(); ap.add_argument("--db", default=os.environ.get("MOS_HUB_DB", "mos-hub.sqlite"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765"))); ap.add_argument("--host", default=os.environ.get("MOS_HUB_HOST", "127.0.0.1"))
    a = ap.parse_args(); url, store, srv = start(a.db, a.port, a.host)
    print(json.dumps({"hub": url, "admin_token_shown_once": store.admin_token}), flush=True)
    try:
        while True: time.sleep(3600)
    except KeyboardInterrupt: pass
