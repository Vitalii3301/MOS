"""Foraging: MOS goes out into the real meme pool of AI agents (public skill repositories) and screens what it finds.

This is the live work stream that does not wait for anyone: every run the cell walks a few public repositories,
picks SKILL.md files it has not seen in that exact version (git blob sha), screens each with its active skill gene,
and remembers the result. Flagged skills are reported; anyone allowed (owner, other AIs) can label a foraged skill
(`label_skill`), and the label goes into the cell's own evidence — the data from which the next gene version is judged.

Budget: GitHub's unauthenticated API allows 60 requests/hour per IP; one tree request per repository per run, files
come from raw.githubusercontent.com. Defaults: 6 repositories and at most 40 new files per run, a run every 3 hours.
Nothing found is executed: skill text is data.
"""
import hashlib, json, time
from urllib import request, parse

SCHEMA = """
CREATE TABLE IF NOT EXISTS forage(repo TEXT, path TEXT, blob TEXT, size INTEGER, score REAL, verdict TEXT, gene TEXT, at REAL, PRIMARY KEY(repo, path, blob));
CREATE TABLE IF NOT EXISTS forage_meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS field_seen(repo TEXT, path TEXT, blob TEXT, PRIMARY KEY(repo, path, blob));
CREATE TABLE IF NOT EXISTS field(sha256 TEXT PRIMARY KEY, repo TEXT, path TEXT, blob TEXT, text TEXT, at REAL);
"""
FIELD_CHARS = 20000
UA = {"User-Agent": "mos-memeos-forager/1 (+https://github.com)", "Accept": "application/vnd.github+json"}

def http_get(url, timeout=20, limit=300000):
    with request.urlopen(request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read(limit).decode("utf-8", "replace")

class Forager:
    def __init__(self, node, repos, get=http_get, per_run=6, max_files=40):
        self.node, self.repos, self.get, self.per_run, self.max_files = node, list(repos), get, per_run, max_files
        node.k.db.executescript(SCHEMA)

    def _meta(self, k, v=None):
        db = self.node.k.db
        if v is None:
            r = db.execute("SELECT value FROM forage_meta WHERE key=?", (k,)).fetchone(); return r and r[0]
        db.execute("INSERT OR REPLACE INTO forage_meta VALUES(?,?)", (k, str(v)))

    def run_once(self):
        g = self.node.skill_gene
        if g is None: return {"ok": False, "error": "no_skill_gene"}
        gid = self.node.genes.active("skill_gene") or ""
        start = int(self._meta("rotation") or 0); picked = [self.repos[(start + i) % len(self.repos)] for i in range(min(self.per_run, len(self.repos)))]
        self._meta("rotation", (start + len(picked)) % max(1, len(self.repos)))
        new = flagged = errors = 0; out = []
        for repo in picked:
            try:
                tree = json.loads(self.get(f"https://api.github.com/repos/{repo}/git/trees/HEAD?recursive=1", limit=5_000_000))
                files = [(t["path"], t["sha"], t.get("size", 0)) for t in tree.get("tree", []) if t.get("type") == "blob" and t["path"].split("/")[-1].lower() == "skill.md"]
            except Exception as e:
                errors += 1; out.append({"repo": repo, "error": type(e).__name__}); continue
            seen = {(p, b) for p, b in self.node.k.db.execute("SELECT path, blob FROM field_seen WHERE repo=?", (repo,))}
            todo = [f for f in files if (f[0], f[1]) not in seen and f[2] <= 400000]
            took = 0
            for path, blob, size in todo:
                if new >= self.max_files: break
                try: text = self.get(f"https://raw.githubusercontent.com/{repo}/HEAD/{parse.quote(path)}")
                except Exception: errors += 1; continue
                text = text[:FIELD_CHARS]             # what is kept is exactly what is judged: the cell's field set (rule condition 4)
                self.node.k.db.execute("INSERT OR IGNORE INTO field_seen VALUES(?,?,?)", (repo, path, blob))     # identical copies elsewhere count once in the field set
                self.node.k.db.execute("INSERT OR IGNORE INTO field VALUES(?,?,?,?,?,?)", (hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest(), repo, path, blob, text, time.time()))
                j = g.judge(text); verdict = "malicious" if j["malicious"] else "no_signal"
                self.node.k.db.execute("INSERT OR IGNORE INTO forage VALUES(?,?,?,?,?,?,?,?)", (repo, path, blob, len(text), j["score"], verdict, gid, time.time()))
                new += 1; took += 1
                if j["malicious"]:
                    flagged += 1
                    self.node.k.event("forage_flagged", {"repo": repo, "path": path, "blob": blob, "score": j["score"], "gene": gid[:16]})
            out.append({"repo": repo, "skill_files": len(files), "screened_now": took})
        self._meta("last_run", time.time())
        self.node.k.event("forage_run", {"repos": picked, "new": new, "flagged": flagged, "errors": errors})
        return {"ok": True, "repos": out, "new": new, "flagged": flagged, "errors": errors}

    def status(self, top=20):
        q = self.node.k.db.execute
        tot = q("SELECT COUNT(*), SUM(verdict='malicious'), COUNT(DISTINCT repo) FROM forage").fetchone()
        g = self.node.skill_gene; now = [(r, p_, g.judge(t)["score"]) for r, p_, t in q("SELECT repo, path, text FROM field")] if g else []
        flagged_now = [{"repo": r, "path": p_, "score": round(s_, 3), "labelled": (q("SELECT label FROM skill_evidence WHERE source=?", ("forage:%s/%s" % (r, p_),)).fetchone() or [None])[0]}
                       for r, p_, s_ in sorted(now, key=lambda x: -x[2]) if s_ > g.threshold][:top]
        fl = [{"repo": r, "path": p, "score": round(s, 3), "url": f"https://github.com/{r}/blob/HEAD/{p}", "labelled": lab}
              for r, p, s, lab in q("SELECT f.repo, f.path, f.score, (SELECT label FROM skill_evidence e WHERE e.source='forage:'||f.repo||'/'||f.path LIMIT 1) "
                                    "FROM forage f WHERE verdict='malicious' ORDER BY score DESC LIMIT ?", (top,))]
        return {"screened": tot[0], "flagged": tot[1] or 0, "repos_seen": tot[2], "repos_configured": len(self.repos),
                "last_run": float(self._meta("last_run") or 0) or None, "flagged_top": fl,
                "field_set": len(now), "flagged_by_active_gene": flagged_now}

    def label(self, repo, path, label, by):
        """Owner/peer review of a foraged skill becomes this cell's evidence (source keeps who said so)."""
        row = self.node.k.db.execute("SELECT 1 FROM forage WHERE repo=? AND path=?", (repo, path)).fetchone()
        if not row: return {"ok": False, "error": "not_foraged_here"}
        text = self.get(f"https://raw.githubusercontent.com/{repo}/HEAD/{parse.quote(path)}")
        n = self.node.genes.add_evidence([{"text": text[:5000], "label": int(label), "source": f"forage:{repo}/{path}"}], source="label:" + str(by))
        self.node.k.event("skill_labelled", {"repo": repo, "path": path, "label": int(label), "by": by, "added": n})
        return {"ok": True, "added_to_evidence": n}

def share_of(repos, cell, cells):
    """Each cell forages its own disjoint part of the world, so the cells' field sets differ like real environments do."""
    cells = sorted(cells); i = cells.index(cell) if cell in cells else 0
    return [r for r in repos if int(hashlib.sha256(r.encode()).hexdigest(), 16) % max(1, len(cells)) == i]
