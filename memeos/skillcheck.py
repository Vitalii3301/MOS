"""Check an AI-agent skill (SKILL.md) before installing it: a plain answer for people, no JSON needed.

    python3 skillcheck.py <file | URL | ->          # prints DANGEROUS / SUSPICIOUS / NOTHING FOUND, and why
    python3 skillcheck.py --json <file | URL | ->   # the same answer as JSON
    python3 skillcheck.py serve [port]              # a page on http://127.0.0.1:<port>/ : paste a skill, read the answer

Works offline with the network's active skill gene shipped in this bundle (the same gene id and weights the live cells
hold), plus the barrier's "override" rule for blatant attacks (the `suspicious` level of screen_skill). With
`--cells`, it also asks live cells through the hub (needs mos_hub_config.json or MOS_NODE_ID/MOS_NODE_TOKEN) and shows
their verdicts next to the local one. Nothing in the checked text is executed or followed; a URL is only downloaded.

"Nothing found" is not a guarantee of safety: the gene catches about 4 in 5 malicious benchmark skills and misses
short, blunt ones; it also raises false alarms on long instruction-heavy skills (about 7 in 100 benign).
Exit status: 0 nothing found, 1 suspicious, 3 dangerous, 2 error.
"""
import gzip, hashlib, html, json, math, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import skillgene as SG

LEVELS = {"malicious": "DANGEROUS", "suspicious": "SUSPICIOUS", "no_signal": "NOTHING FOUND"}
NOTE_RU = {"no_signal": "«Ничего не найдено» — не гарантия безопасности. Прочитайте навык и его скрипты перед установкой.",
           "suspicious": "Текст открыто требует от ИИ отбросить свои инструкции. Не устанавливайте, пока не поймёте зачем.",
           "malicious": "Ген ошибается примерно на 7 из 100 обычных навыков, особенно длинных и с большим количеством кода. "
                        "Посмотрите фразы и строки выше: вердикт — повод прочитать навык, а не доказательство вреда."}
LEVELS_RU = {"malicious": "ОПАСНО", "suspicious": "ПОДОЗРИТЕЛЬНО", "no_signal": "НИЧЕГО НЕ НАЙДЕНО"}
MAX_TEXT = 2_000_000

def active_gene():
    """The gene the live network runs, from the bundled snapshot (same id) and gene bodies (checked by sha256)."""
    snap = json.loads(gzip.decompress(open(os.path.join(HERE, "snapshots", "cells.json.gz"), "rb").read()))["mos-claude"]
    rows = snap["tables"]["gene_memes"]; act = [r for r in rows if r[4] == "active"]
    if not act: raise RuntimeError("no active gene in the bundled snapshot")
    card = json.loads(act[0][3]); gdir = os.path.join(HERE, "genes", "skill")
    for f in sorted(os.listdir(gdir)):
        raw = open(os.path.join(gdir, f), "rb").read()
        if hashlib.sha256(raw).hexdigest() == card["body_sha256"]:
            return SG.SkillGene(json.loads(gzip.decompress(raw))), act[0][0]
    raise RuntimeError("gene body %s not found in genes/skill" % card["body_sha256"][:12])

def bundled_signatures():
    """Skill signatures the live main cell held when this bundle was built (precise patterns of known real attacks)."""
    try: rows = json.loads(gzip.decompress(open(os.path.join(HERE, "snapshots", "cells.json.gz"), "rb").read()))["mos-claude"]["tables"].get("skill_antimemes", [])
    except Exception: rows = []
    out = {}
    for r in rows:
        try: out[r[0]] = re.compile(r[1], re.I | re.M)
        except re.error: pass
    return out

def _barrier():
    bp = os.path.join(HERE, "barrier_deploy.json")
    if not os.path.exists(bp): return None
    from barrier import Barrier
    return Barrier.from_card(json.load(open(bp)))

def explain(gene, text, top=8):
    """Which words and word pairs pushed the score up (towards 'malicious') or down. The gene hashes features, so the
    phrases are recovered from the text itself: each unigram/bigram's share of w . x."""
    x = gene.vector(text); w = SG.TOKEN.findall(text[:SG.CAP].lower())
    grams = set(w) | {a + " " + b for a, b in zip(w, w[1:])}
    contrib = {}
    for g in grams:
        k = SG._h(g)
        if k in x: contrib[g] = x[k] * gene.w[k]
    # several phrases can share a hash bucket: keep the bucket's weight once, under its most readable phrase
    seen = {}
    for g, c in sorted(contrib.items(), key=lambda t: (-abs(t[1]), len(t[0]))):
        seen.setdefault(SG._h(g), (g, c))
    items = list(seen.values())
    up = [(g, round(c, 4)) for g, c in sorted(items, key=lambda t: -t[1]) if c > 0][:top]
    down = [(g, round(c, 4)) for g, c in sorted(items, key=lambda t: t[1]) if c < 0][:top]
    return up, down

def _lines_with(text, phrases, limit=5):
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        low = line.lower()
        hit = [p for p in phrases if re.search(r"\b" + re.escape(p) + r"\b", low)]
        if hit: out.append({"line": i, "text": line.strip()[:200], "phrases": hit[:4]})
        if len(out) >= limit: break
    return out

def check(text, gene=None, gene_id=None, barrier=None, signatures=None):
    if not isinstance(text, str) or not text.strip(): raise ValueError("empty text")
    if len(text) > MAX_TEXT: raise ValueError("text larger than %d characters" % MAX_TEXT)
    if gene is None: gene, gene_id = active_gene()
    rep = SG.screen(gene, text, barrier if barrier is not None else _barrier(), signatures if signatures is not None else bundled_signatures())[0]   # the one verdict function the live cells use
    up, down = explain(gene, text)
    sg = rep["skill_gene"]
    return {"verdict": rep["verdict"], "level": LEVELS[rep["verdict"]], "level_ru": LEVELS_RU[rep["verdict"]],
            "score": sg["score"], "threshold": sg["threshold"], "reasons": rep["reasons"],
            "gene": {"id": gene_id, "name": gene.card.get("name")},
            "pushed_towards_malicious": up, "pushed_towards_benign": down,
            "where_in_text": _lines_with(text, [g for g, _ in up[:5]]),
            "sha256": hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest(),
            "note": NOTE_RU[rep["verdict"]]}

def ask_cells(text, cells=("mos-claude", "memeos-cell-b", "memeos-cell-c"), wait=200):
    """Optional: the same question to live cells through the hub (each answers with its own gene)."""
    import time, secrets
    from urllib import request, error, parse
    cfg = None
    if os.environ.get("MOS_NODE_ID") and os.environ.get("MOS_NODE_TOKEN"):
        cfg = {"hub_url": os.environ.get("MOS_HUB_URL", "https://mos-hub-live-production.up.railway.app"),
               "node_id": os.environ["MOS_NODE_ID"], "node_token": os.environ["MOS_NODE_TOKEN"]}
    else:
        for p in (os.path.join(os.getcwd(), "mos_hub_config.json"), os.path.join(HERE, "mos_hub_config.json")):
            if os.path.exists(p): cfg = json.load(open(p)); break
    if not cfg: return {"error": "no hub identity (mos_hub_config.json or MOS_NODE_ID/MOS_NODE_TOKEN)"}
    url, me, tok = cfg["hub_url"].rstrip("/"), cfg["node_id"], cfg["node_token"]
    canon = lambda v: json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    def call(method, path, body=None):
        h = {"Accept": "application/json", "Authorization": "Bearer " + tok}
        data = None
        if body is not None: h["Content-Type"] = "application/json"; data = json.dumps(body, ensure_ascii=False).encode()
        try:
            with request.urlopen(request.Request(url + path, method=method, headers=h, data=data), timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}")
        except error.HTTPError as e: return e.code, {}
    payload = {"op": "screen_skill", "text": text}; ids = {}; first = None
    for c in cells:
        mid = "MSG-%s-check-%d-%s" % (me, int(time.time() * 1000), secrets.token_hex(3))
        st, o = call("POST", "/v1/messages", {"schema": "mos-remote-message/v1", "message_id": mid, "recipient": c,
                     "kind": "mos-meme-task/v1", "payload": payload, "payload_sha256": hashlib.sha256(canon(payload).encode()).hexdigest(),
                     "expires_at": int(time.time()) + 3600})
        if st in (200, 201): ids[mid] = c; first = o.get("stream_seq", 0) if first is None else first
    out = {}; t = time.time()
    while ids and time.time() - t < wait:
        time.sleep(5)
        st, box = call("GET", "/v1/mailbox/%s?after=%d&limit=200" % (parse.quote(me), max(0, (first or 1) - 1)))
        for m in (box or {}).get("messages", []):
            pl = m.get("payload") or {}
            if m.get("kind") == "mos-meme-result/v1" and pl.get("in_reply_to") in ids:
                ok = hashlib.sha256(canon(pl).encode()).hexdigest() == m.get("payload_sha256")
                out[ids.pop(pl["in_reply_to"])] = {"verdict": pl.get("verdict"), "error": pl.get("error"), "payload_sha256_ok": ok,
                                                    "score": (pl.get("skill_gene") or {}).get("score")}
    for c in ids.values(): out[c] = {"verdict": None, "error": "no answer within %ds (cells poll about once a minute)" % wait}
    return out

def render_text(r, cells=None):
    lines = ["%s  (%s)" % (r["level_ru"], r["level"]),
             "оценка гена %.3f при пороге %.3f; ген %s %s" % (r["score"], r["threshold"], r["gene"]["name"], (r["gene"]["id"] or "")[:12])]
    if r["reasons"]: lines.append("причины: " + ", ".join(r["reasons"]))
    if r["pushed_towards_malicious"]:
        lines.append("\nЧто сдвигало к «опасно»: " + ", ".join("«%s»" % g for g, _ in r["pushed_towards_malicious"]))
    if r["pushed_towards_benign"]:
        lines.append("Что сдвигало к «безопасно»: " + ", ".join("«%s»" % g for g, _ in r["pushed_towards_benign"]))
    if r["where_in_text"] and r["verdict"] != "no_signal":
        lines.append("\nГде в тексте:")
        lines += ["  строка %d: %s" % (w["line"], w["text"]) for w in r["where_in_text"]]
    if cells:
        lines.append("\nЖивые клетки:")
        for c, v in cells.items():
            if c == "error": lines.append("  " + v); continue
            lines.append("  %s: %s" % (c, LEVELS_RU.get(v.get("verdict"), v.get("error") or "?")))
    lines.append("\n" + NOTE_RU[r["verdict"]])
    return "\n".join(lines)

def read_source(src):
    if src == "-": return sys.stdin.read()
    if re.match(r"^https?://", src):
        from urllib import request
        u = re.sub(r"^https://github\.com/([^/]+)/([^/]+)/blob/", r"https://raw.githubusercontent.com/\1/\2/", src)
        with request.urlopen(request.Request(u, headers={"User-Agent": "mos-skillcheck/1"}), timeout=30) as r:
            return r.read(MAX_TEXT + 1).decode("utf-8", "replace")
    return open(src, encoding="utf-8", errors="replace").read()

PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Проверка навыка</title><style>
:root{--bg:#fafaf9;--fg:#1c1917;--muted:#57534e;--line:#d6d3d1;--bad:#b91c1c;--warn:#a16207;--ok:#15803d;--card:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#1c1917;--fg:#f5f5f4;--muted:#a8a29e;--line:#44403c;--bad:#f87171;--warn:#facc15;--ok:#4ade80;--card:#292524}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}main{max-width:820px;margin:0 auto;padding:24px 16px}
textarea{width:100%%;box-sizing:border-box;min-height:220px;font:13px/1.4 ui-monospace,monospace;background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:10px}
button{margin-top:10px;padding:8px 18px;font-size:15px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
.v{font-size:26px;font-weight:700;margin:20px 0 4px}.malicious{color:var(--bad)}.suspicious{color:var(--warn)}.no_signal{color:var(--ok)}
.muted{color:var(--muted);font-size:14px}code{font-size:13px}li{margin:2px 0}</style></head><body><main>
<h1>Проверка навыка ИИ-агента</h1><p class="muted">Вставьте текст SKILL.md. Текст проверяется геном сети MOS локально, ничего не исполняется.</p>
<form method="post" action="/"><textarea name="text" placeholder="---&#10;name: ...">%s</textarea><br><button type="submit">Проверить</button></form>%s
</main></body></html>"""

def render_html(r):
    li = lambda xs: "".join("<li><code>%s</code></li>" % html.escape(g) for g, _ in xs)
    where = "".join("<li>строка %d: <code>%s</code></li>" % (w["line"], html.escape(w["text"])) for w in r["where_in_text"]) if r["verdict"] != "no_signal" else ""
    return ("<div class='v %s'>%s</div><div class='muted'>оценка %.3f при пороге %.3f · ген %s</div>" % (r["verdict"], r["level_ru"], r["score"], r["threshold"], html.escape(r["gene"]["name"] or "")) +
            ("<p>Причины: %s</p>" % html.escape(", ".join(r["reasons"])) if r["reasons"] else "") +
            ("<h3>Что сдвигало к «опасно»</h3><ul>%s</ul>" % li(r["pushed_towards_malicious"]) if r["pushed_towards_malicious"] else "") +
            ("<h3>Где в тексте</h3><ul>%s</ul>" % where if where else "") +
            ("<h3>Что сдвигало к «безопасно»</h3><ul>%s</ul>" % li(r["pushed_towards_benign"]) if r["pushed_towards_benign"] else "") +
            "<p class='muted'>%s</p>" % html.escape(NOTE_RU[r["verdict"]]))

def serve(port=8770, host="127.0.0.1"):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs
    gene, gid = active_gene(); bar = _barrier()
    class H(BaseHTTPRequestHandler):
        def _page(self, body, code=200):
            b = body.encode(); self.send_response(code); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"); self.end_headers(); self.wfile.write(b)
        def do_GET(self):
            if self.path.split("?")[0] == "/health":
                b = json.dumps({"ok": True, "gene": gid}).encode(); self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return
            self._page(PAGE % ("", ""))
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_TEXT * 4: return self._page(PAGE % ("", "<p>Текст слишком большой.</p>"), 413)
            raw = self.rfile.read(n).decode("utf-8", "replace")
            text = raw if "json" in (self.headers.get("Content-Type") or "") else (parse_qs(raw).get("text") or [""])[0]
            if "json" in (self.headers.get("Content-Type") or ""):
                try: text = json.loads(raw)["text"]; r = check(text, gene, gid, bar); code = 200
                except Exception as e: r, code = {"error": str(e)}, 400
                b = json.dumps(r, ensure_ascii=False).encode(); self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return
            try: res = render_html(check(text, gene, gid, bar))
            except ValueError as e: res = "<p>%s</p>" % html.escape(str(e))
            self._page(PAGE % (html.escape(text), res))
        def log_message(self, *a): pass
    srv = ThreadingHTTPServer((host, port), H)
    print(json.dumps({"serving": "http://%s:%d/" % (host, srv.server_address[1]), "gene": gid}), flush=True)
    srv.serve_forever()

def main(argv):
    if not argv or argv[0] in ("-h", "--help"): print(__doc__); return 2
    if argv[0] == "serve": serve(int(argv[1]) if len(argv) > 1 else 8770); return 0
    as_json = "--json" in argv; cells = "--cells" in argv; args = [a for a in argv if not a.startswith("--")]
    if not args: print(__doc__); return 2
    try: text = read_source(args[0]); r = check(text)
    except (OSError, ValueError) as e: print("error: %s" % e, file=sys.stderr); return 2
    cv = ask_cells(text) if cells else None
    if as_json: r["cells"] = cv; print(json.dumps(r, ensure_ascii=False, indent=1))
    else: print(render_text(r, cv))
    return {"malicious": 3, "suspicious": 1}.get(r["verdict"], 0)

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
