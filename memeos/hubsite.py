"""Where can a MOS hub live from HERE? Discovery of the environment, a ranked plan, and deployment.

MOS does not assume Railway (or anything else). It looks at what this environment actually offers and either
  - joins a hub that already exists (credentials given, or a hub already listening nearby),
  - builds its own hub server from zero on this machine (loopback, or all interfaces so others can reach it),
  - or places the same server on a target it found: Docker, an SSH host, a tunnel, Railway / Fly CLIs.
Anything that costs money, uses the owner's cloud credentials or exposes a port to the internet is NEVER done
silently: it is listed in the plan and runs only on an explicit `hub deploy <target> --yes`.
Secrets are detected by NAME only; their values are never read into reports.
"""
import hashlib, json, os, secrets, shutil, socket, subprocess, sys, time
from urllib import request

APP = os.path.dirname(os.path.abspath(__file__))
PROD_HUB = "https://mos-hub-live-production.up.railway.app"
TOOLS = ["docker", "podman", "ssh", "scp", "railway", "flyctl", "fly", "cloudflared", "ngrok", "kubectl", "gcloud", "heroku"]
TOKEN_NAMES = ["RAILWAY_TOKEN", "RAILWAY_API_TOKEN", "FLY_API_TOKEN", "CLOUDFLARE_API_TOKEN", "NGROK_AUTHTOKEN", "HEROKU_API_KEY", "RENDER_API_KEY"]

def _http_ok(url, timeout=5):
    try:
        with request.urlopen(url, timeout=timeout) as r: return r.status == 200, r.read(2000).decode(errors="replace")
    except Exception as e: return False, type(e).__name__

def _can_bind(host):
    try:
        s = socket.socket(); s.bind((host, 0)); s.close(); return True
    except OSError: return False

def _addresses():
    out = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("10.255.255.255", 1)); out.add(s.getsockname()[0]); s.close()
    except OSError: pass
    try: out |= {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError: pass
    return sorted(a for a in out if not a.startswith("127."))

def _ssh_hosts():
    hosts = []
    try:
        for l in open(os.path.expanduser("~/.ssh/config")):
            l = l.strip()
            if l.lower().startswith("host ") and "*" not in l: hosts += l.split()[1:]
    except OSError: pass
    return hosts[:20]

def discover(home, credentials=None):
    d = {"python": sys.version.split()[0], "platform": sys.platform, "home_writable": os.access(home, os.W_OK) if os.path.exists(home) else os.access(os.path.dirname(home) or ".", os.W_OK),
         "bind_loopback": _can_bind("127.0.0.1"), "bind_all_interfaces": _can_bind("0.0.0.0"), "addresses": _addresses(),
         "tools": {t: bool(shutil.which(t)) for t in TOOLS}, "token_names_present": [n for n in TOKEN_NAMES if os.environ.get(n)],
         "ssh_hosts": _ssh_hosts(), "background_processes": hasattr(os, "fork")}
    d["container_daemon"] = False
    for tool in ("docker", "podman"):                       # a client binary without a running daemon is not a deploy target
        if d["tools"][tool]:
            try: d["container_daemon"] = d["container_daemon"] or subprocess.run([tool, "info"], capture_output=True, timeout=15).returncode == 0
            except Exception: pass
    ok, _ = _http_ok(PROD_HUB + "/v1/public/nodes", 6); d["owner_hub_reachable"] = ok
    d["internet"] = ok or _http_ok("https://pypi.org/simple/pip/", 6)[0]
    d["credentials_for_external_hub"] = bool(credentials)
    near = []
    for port in (8765, 8080, 8000):                         # a hub that is already running next to us
        ok, body = _http_ok(f"http://127.0.0.1:{port}/health", 1)
        if ok and "mos-localhub" in body: near.append(f"http://127.0.0.1:{port}")
    d["hubs_already_listening"] = near
    return d

def plan(d):
    """Ranked options: status is ready (MOS can do it now, free, private), needs_owner (possible, but spends money /
    uses credentials / exposes a port: explicit command), or unavailable (with the reason = what to connect)."""
    t = d["tools"]; P = []
    def add(target, status, what, how, note=None): P.append({"target": target, "status": status, "what": what, "how": how, **({"note": note} if note else {})})
    if d["credentials_for_external_hub"]:
        add("join", "ready" if d["owner_hub_reachable"] or True else "unavailable", "join the existing hub with the credentials provided", "automatic on start")
    else:
        add("join", "unavailable", "join the owner's existing hub", "put mos_hub_config.json next to the file (node id + token from the owner)",
            "owner hub is reachable from here" if d["owner_hub_reachable"] else "owner hub is NOT reachable from here (no outbound network to it)")
    for u in d["hubs_already_listening"]:
        add("adopt-nearby", "needs_owner", f"a MOS hub is already running at {u}", f"hub adopt {u} <admin token>")
    add("local", "ready" if d["bind_loopback"] else "unavailable", "hub built from zero inside this process, loopback only (private to this sandbox)", "automatic on start")
    add("server", "ready" if d["bind_all_interfaces"] and d["background_processes"] else "unavailable",
        "hub built from zero as a separate long-running server on this machine, all interfaces" + (f" (reachable at {', '.join(d['addresses'])})" if d["addresses"] else ""),
        "hub deploy server", "other machines can connect only if this machine's network allows inbound connections")
    cont = d.get("container_daemon")
    add("docker", "needs_owner" if cont else "unavailable", "hub server in a container with a persistent volume",
        "hub deploy docker --yes" if cont else "connect an environment with a running Docker/Podman daemon" + (" (client found here, but no daemon)" if t["docker"] or t["podman"] else ""))
    add("ssh", "needs_owner" if t["ssh"] and t["scp"] else "unavailable", "hub server on a machine you can reach over SSH" + (f" (configured hosts: {', '.join(d['ssh_hosts'])})" if d["ssh_hosts"] else ""),
        "hub deploy ssh user@host --yes" if t["ssh"] else "connect an environment with ssh access to a server")
    tun = "cloudflared" if t["cloudflared"] else "ngrok" if t["ngrok"] else None
    add("tunnel", "needs_owner" if tun and d["internet"] else "unavailable", "public HTTPS address for the hub server on this machine (exposes it to the internet; token-protected)",
        "hub deploy tunnel --yes" if tun else "install cloudflared or ngrok here")
    rw = t["railway"] and any(n.startswith("RAILWAY") for n in d["token_names_present"])
    add("railway", "needs_owner" if rw else "unavailable", "hub server as a Railway service (costs money on the owner's account)",
        "hub deploy railway --yes" if rw else "connect Railway (CLI + RAILWAY_TOKEN), or let an AI with a Railway connector deploy the kit from `hub kit`")
    fl = (t["flyctl"] or t["fly"]) and "FLY_API_TOKEN" in d["token_names_present"]
    add("fly", "needs_owner" if fl else "unavailable", "hub server on Fly.io (costs money)", "hub kit <dir>, then `fly launch` in it" if fl else "connect Fly.io (flyctl + FLY_API_TOKEN)")
    add("kit", "ready", "write a self-contained hub server kit (server, Dockerfile, Procfile, instructions) to hand to any host or any AI with a deploy tool", "hub kit <dir>")
    best = next((p["target"] for p in P if p["status"] == "ready" and p["target"] in ("join",)), None) or "local"
    return {"chosen_automatically": best, "options": P,
            "to_get_a_shared_hub": [p["how"] for p in P if p["status"] != "ready" and p["target"] in ("join", "ssh", "docker", "railway", "tunnel")]}

# ------------------------------------------------------------------ the kit: a hub server anyone can host
DOCKERFILE = "FROM {base}\nWORKDIR /app\nCOPY localhub.py .\nENV MOS_HUB_HOST=0.0.0.0 MOS_HUB_DB=/data/mos-hub.sqlite PORT=8765\nVOLUME /data\nEXPOSE 8765\nCMD [\"python3\", \"-u\", \"localhub.py\"]\n"
KIT_README = """# MOS hub server kit

One file, standard library only: `localhub.py`.

Run anywhere with Python 3.10+:

    MOS_HUB_HOST=0.0.0.0 PORT=8765 MOS_HUB_DB=/data/mos-hub.sqlite MOS_HUB_ADMIN_TOKEN_SHA256=<sha256 of your admin token> python3 -u localhub.py

- Keep the admin token to yourself; the server only ever sees its sha256. Without the variable the server mints a token
  and prints it ONCE on first start.
- Persist the SQLite file (volume) or the hub forgets its nodes and messages.
- Put TLS in front of it (the platform's HTTPS, a reverse proxy or a tunnel) before exposing it to the internet.
- Docker: `docker build -t mos-hub . && docker run -d -p 8765:8765 -v mos-hub-data:/data -e MOS_HUB_ADMIN_TOKEN_SHA256=... mos-hub`
- Railway / Render / Fly / Heroku-style platforms: deploy this folder; start command is in `Procfile`; they provide PORT.

Then, from MOS:  `hub adopt https://<address> <admin token>`  registers the cells there and moves MOS onto that hub;
`hub invite <node-id>` creates an identity + config file for another AI.
"""
def write_kit(dst):
    os.makedirs(dst, exist_ok=True)
    shutil.copy(os.path.join(APP, "localhub.py"), os.path.join(dst, "localhub.py"))
    # MOS_HUB_BASE_IMAGE: any image with python3 on PATH, for hosts that cannot pull from Docker Hub (private mirror, local image)
    open(os.path.join(dst, "Dockerfile"), "w").write(DOCKERFILE.replace("{base}", os.environ.get("MOS_HUB_BASE_IMAGE") or "python:3.12-slim"))
    open(os.path.join(dst, "Procfile"), "w").write("web: MOS_HUB_HOST=0.0.0.0 python -u localhub.py\n")
    open(os.path.join(dst, "README.md"), "w").write(KIT_README)
    return {"kit": dst, "files": sorted(os.listdir(dst)), "localhub_sha256": hashlib.sha256(open(os.path.join(dst, "localhub.py"), "rb").read()).hexdigest()}

def _admin(home):
    """Mint an admin token locally; only its sha256 goes to the server."""
    tok = secrets.token_urlsafe(32)
    return tok, hashlib.sha256(tok.encode()).hexdigest()

def _wait(url, secs=30):
    t = time.time()
    while time.time() - t < secs:
        if _http_ok(url + "/health", 2)[0]: return True
        time.sleep(0.5)
    return False

def deploy(target, home, args=(), host="0.0.0.0"):
    """Returns {"url","admin_token"} on success. Only `server` runs without --yes."""
    kit = os.path.join(home, "hub-kit"); write_kit(kit); tok, h = _admin(home)
    if target == "server":
        s = socket.socket(); s.bind((host, 0)); port = s.getsockname()[1]; s.close()
        env = dict(os.environ, MOS_HUB_HOST=host, PORT=str(port), MOS_HUB_DB=os.path.join(home, "hub-server.sqlite"), MOS_HUB_ADMIN_TOKEN_SHA256=h)
        log = open(os.path.join(home, "hub-server.log"), "a")
        p = subprocess.Popen([sys.executable, "-u", os.path.join(kit, "localhub.py")], env=env, stdout=log, stderr=log, start_new_session=True)
        url = f"http://127.0.0.1:{port}"
        if not _wait(url): return {"ok": False, "error": "server_did_not_start"}
        return {"ok": True, "url": url, "admin_token": tok, "pid": p.pid, "also_reachable_at": [f"http://{a}:{port}" for a in _addresses()] if host == "0.0.0.0" else []}
    if "--yes" not in args:
        return {"ok": False, "error": "needs_explicit_consent", "say": f"hub deploy {target} --yes", "why": "this target uses credentials, money or a public port"}
    run = lambda cmd, **k: subprocess.run(cmd, capture_output=True, text=True, timeout=600, **k)
    if target == "docker":
        tool = "docker" if shutil.which("docker") else "podman"
        r = run([tool, "build", "-t", "mos-hub", kit])
        if r.returncode:
            out = {"ok": False, "error": r.stderr[-300:]}
            if "registry" in r.stderr or "failed to resolve" in r.stderr or "pull" in r.stderr.lower():
                out["hint"] = "the base image could not be pulled from here; set MOS_HUB_BASE_IMAGE to an image this daemon already has or can pull (any image with python3)"
            return out
        r = run([tool, "run", "-d", "--name", "mos-hub", "-p", "8765:8765", "-v", "mos-hub-data:/data", "-e", "MOS_HUB_ADMIN_TOKEN_SHA256=" + h, "mos-hub"])
        if r.returncode: return {"ok": False, "error": r.stderr[-300:]}
        url = "http://127.0.0.1:8765"; return {"ok": _wait(url), "url": url, "admin_token": tok}
    if target == "ssh":
        host = next((a for a in args if not a.startswith("--")), None)
        if not host: return {"ok": False, "error": "usage: hub deploy ssh user@host --yes"}
        r = run(["scp", "-q", os.path.join(kit, "localhub.py"), host + ":mos-localhub.py"])
        if r.returncode: return {"ok": False, "error": r.stderr[-300:]}
        r = run(["ssh", host, f"MOS_HUB_HOST=0.0.0.0 PORT=8765 MOS_HUB_DB=$HOME/mos-hub.sqlite MOS_HUB_ADMIN_TOKEN_SHA256={h} nohup python3 -u mos-localhub.py > mos-hub.log 2>&1 < /dev/null & sleep 2; echo started"])
        if r.returncode: return {"ok": False, "error": r.stderr[-300:]}
        name = host.split("@")[-1]
        try:   # an alias from ~/.ssh/config is not a DNS name: ask ssh what it actually connects to
            g = run(["ssh", "-G", host]).stdout; name = next((l.split()[1] for l in g.splitlines() if l.startswith("hostname ")), name)
        except Exception: pass
        url = "http://" + name + ":8765"; return {"ok": _wait(url, 15), "url": url, "admin_token": tok, "note": "plain HTTP: put TLS in front before real use; started with nohup, so it does not come back after the host reboots (run it under systemd or docker there for that)"}
    if target == "tunnel":
        srv = deploy("server", home)
        if not srv.get("ok"): return srv
        port = srv["url"].rsplit(":", 1)[1]; log = os.path.join(home, "tunnel.log")
        cmd = ["cloudflared", "tunnel", "--url", srv["url"]] if shutil.which("cloudflared") else ["ngrok", "http", port, "--log", "stdout"]
        subprocess.Popen(cmd, stdout=open(log, "a"), stderr=subprocess.STDOUT, start_new_session=True)
        import re
        t = time.time(); pub = None
        while time.time() - t < 40 and not pub:
            m = re.search(r"https://[a-z0-9-]+\.(?:trycloudflare\.com|ngrok[a-z.-]*\.(?:app|io|dev))", open(log).read()); pub = m and m.group(0); time.sleep(1)
        return {"ok": bool(pub), "url": srv["url"], "public_url": pub, "admin_token": srv["admin_token"]}
    if target == "railway":
        r = run(["railway", "up", "--detach"], cwd=kit)
        return {"ok": r.returncode == 0, "output": (r.stdout + r.stderr)[-400:], "admin_token": tok,
                "then": "set variable MOS_HUB_ADMIN_TOKEN_SHA256=" + h + " and MOS_HUB_HOST=0.0.0.0, add a volume at /data with MOS_HUB_DB=/data/mos-hub.sqlite, generate a domain, then: hub adopt https://<domain> <admin token>"}
    return {"ok": False, "error": "unknown_target"}

def register(url, admin_token, node_id, capabilities=None):
    req = request.Request(url.rstrip("/") + "/v1/nodes/register", method="POST", data=json.dumps({"node_id": node_id, "capabilities": capabilities or {}}).encode(),
                          headers={"Authorization": "Bearer " + admin_token, "Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=20) as r: return json.loads(r.read())["token"]
    except Exception as e:
        return None
