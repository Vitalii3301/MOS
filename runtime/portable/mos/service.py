from __future__ import annotations
import argparse, json, os, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from .runtime import MOSRuntime

LOOPBACK_HOSTS={"127.0.0.1","localhost","::1"}

class RequestTooLarge(ValueError):
    pass

class MOSHTTPService:
    """HTTP control plane for the full runtime.

    Safety boundary: unauthenticated operation is allowed only on loopback.
    Binding a non-loopback address requires an explicit bearer token.
    """
    def __init__(self, db_path: str | Path, host: str = "127.0.0.1", port: int = 8765,
                 token: str | None = None, runtime: MOSRuntime | None = None,
                 max_body_bytes: int = 2 * 1024 * 1024):
        self.runtime = runtime or MOSRuntime(db_path)
        self.token = token if token is not None else os.environ.get("MOS_API_TOKEN")
        self.max_body_bytes=max(1024,int(max_body_bytes))
        if host not in LOOPBACK_HOSTS and not self.token:
            self.runtime.close()
            raise ValueError("non-loopback HTTP binding requires MOS_API_TOKEN/bearer token")
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "MOS-R4/4.1"
            def log_message(self, fmt, *args): return
            def _auth(self):
                if not outer.token: return True
                return self.headers.get("Authorization") == f"Bearer {outer.token}"
            def _send(self, status, obj):
                data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers(); self.wfile.write(data)
            def _body(self):
                raw_len=self.headers.get("Content-Length", "0")
                try:n=int(raw_len)
                except ValueError:raise ValueError("invalid Content-Length")
                if n<0:raise ValueError("negative Content-Length")
                if n>outer.max_body_bytes:raise RequestTooLarge(f"request body exceeds {outer.max_body_bytes} bytes")
                raw = self.rfile.read(n) if n else b"{}"
                if len(raw)>outer.max_body_bytes:raise RequestTooLarge(f"request body exceeds {outer.max_body_bytes} bytes")
                return json.loads(raw.decode("utf-8"))
            def do_GET(self):
                if not self._auth(): return self._send(401, {"error": "unauthorized"})
                path = urlparse(self.path).path
                if path == "/health": return self._send(200, {"status": "ok", "runtime_version": outer.runtime.VERSION, "verify": outer.runtime.verify()})
                if path == "/status": return self._send(200, {"state": outer.runtime.state(), "verify": outer.runtime.verify()})
                if path == "/ontology": return self._send(200, outer.runtime.memes.ontology())
                if path == "/self-model": return self._send(200, {"items": outer.runtime.meta.active_self()})
                if path == "/meta-memory": return self._send(200, {"items": outer.runtime.meta.active_meta(50)})
                return self._send(404, {"error": "not_found"})
            def do_POST(self):
                if not self._auth(): return self._send(401, {"error": "unauthorized"})
                try:
                    body = self._body(); path = urlparse(self.path).path
                except RequestTooLarge as exc:
                    return self._send(413, {"error": "request_too_large", "detail": str(exc)})
                except Exception as exc:
                    return self._send(400, {"error": "bad_json", "detail": str(exc)})
                try:
                    if path == "/ask": return self._send(200, outer.runtime.process(str(body["text"]), hypothesis=body.get("hypothesis"), decision=body.get("decision")).__dict__)
                    if path == "/goal": outer.runtime.set_goal(str(body["goal"])); return self._send(200, {"goal": body["goal"]})
                    if path == "/outcome": return self._send(200, outer.runtime.record_outcome(str(body["episode_id"]), str(body["outcome"]), body.get("revision"), body.get("success")))
                    if path == "/mpl": return self._send(200, {"results": outer.runtime.mpl.execute(str(body["script"]))})
                    if path == "/research": return self._send(200, outer.runtime.research.run(str(body["query"])))
                    if path == "/snapshot": return self._send(200, outer.runtime.relay.export(str(body["path"]), body.get("parent_snapshot")))
                    if path == "/restore": return self._send(200, outer.runtime.relay.import_snapshot(str(body["path"]), body.get("expected_revision"), create_backup=bool(body.get("create_backup", True))))
                    if path == "/backup": outer.runtime.store.backup(str(body["path"])); return self._send(200, {"backup": body["path"]})
                    if path == "/validate-weakness": return self._send(200, outer.runtime.validate_weakness(str(body["domain"]), passed=bool(body["passed"]), evidence_ref=str(body["evidence_ref"])))
                    if path == "/strategy/activate": return self._send(200, outer.runtime.activate_strategy_candidate(str(body["name"]), dict(body["evaluation"])))
                    if path == "/background":
                        enabled = bool(body.get("enabled", True))
                        if enabled: outer.runtime.start_background_reflection(float(body.get("interval", 10.0)))
                        else: outer.runtime.stop_background_reflection()
                        return self._send(200, {"observer_running": outer.runtime.observer.running})
                    if path == "/verify": return self._send(200, outer.runtime.verify())
                    return self._send(404, {"error": "not_found"})
                except KeyError as exc:
                    return self._send(400, {"error": "missing_field", "field": str(exc)})
                except (ValueError, RuntimeError, TypeError) as exc:
                    return self._send(409 if isinstance(exc, RuntimeError) else 400, {"error": type(exc).__name__, "detail": str(exc)})
                except Exception as exc:
                    return self._send(500, {"error": type(exc).__name__, "detail": str(exc)})

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.thread = None

    @property
    def address(self): return self.server.server_address
    def start_background(self):
        if self.thread and self.thread.is_alive(): return
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="mos-http")
        self.thread.start()
    def shutdown(self):
        self.server.shutdown(); self.server.server_close()
        if self.thread: self.thread.join(timeout=2)
        self.runtime.close()

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="mos.sqlite3"); p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765); p.add_argument("--token")
    p.add_argument("--max-body-bytes",type=int,default=2*1024*1024)
    args = p.parse_args(); s = MOSHTTPService(args.db, args.host, args.port, args.token,max_body_bytes=args.max_body_bytes)
    try: s.server.serve_forever()
    except KeyboardInterrupt: pass
    finally: s.shutdown()

if __name__ == "__main__": main()
