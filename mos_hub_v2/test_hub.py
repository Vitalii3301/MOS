import hashlib, json, os, subprocess, sys, tempfile, time, urllib.error, urllib.request
from pathlib import Path

ROOT=Path(__file__).resolve().parent

def canonical(v): return json.dumps(v,sort_keys=True,separators=(",",":"),ensure_ascii=False)
def ph(v): return hashlib.sha256(canonical(v).encode()).hexdigest()

def request(base, method, path, body=None, token=None):
    data=None if body is None else json.dumps(body).encode()
    h={"Accept":"application/json"}
    if body is not None: h["Content-Type"]="application/json"
    if token: h["Authorization"]="Bearer "+token
    r=urllib.request.Request(base+path,data=data,headers=h,method=method)
    try:
        with urllib.request.urlopen(r,timeout=5) as x:
            raw=x.read().decode(); return x.status,dict(x.headers),json.loads(raw)
    except urllib.error.HTTPError as e:
        raw=e.read().decode(); return e.code,dict(e.headers),json.loads(raw)

def wait(base, seconds=8):
    end=time.time()+seconds
    while time.time()<end:
        try:
            st,_,_=request(base,"GET","/health")
            if st in (200,503): return
        except Exception: pass
        time.sleep(.05)
    raise RuntimeError("server did not start")

def main():
    with tempfile.TemporaryDirectory() as td:
        td=Path(td); port=18892
        env=os.environ.copy(); env.update({"MOS_RELAY_ADMIN_TOKEN":"mos_admin_test_secret_123456789","MOS_RELAY_DATA_DIR":str(td),"PORT":str(port)})
        p=subprocess.Popen([sys.executable,"-B",str(ROOT/"server.py")],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,text=True)
        base=f"http://127.0.0.1:{port}"
        try:
            wait(base)
            admin=env["MOS_RELAY_ADMIN_TOKEN"]
            st,_,a=request(base,"POST","/v1/nodes/register",{"node_id":"mos-a","display_name":"MOS A"},admin); assert st==201
            st,_,b=request(base,"POST","/v1/nodes/register",{"node_id":"mos-b","display_name":"MOS B"},admin); assert st==201
            ta=a["token"]; tb=b["token"]
            st,_,hb=request(base,"POST","/v1/heartbeat",{"status":"ready","capabilities":{"review":True}},ta); assert st==200
            st,_,nodes=request(base,"GET","/v1/public/nodes"); assert st==200 and any(x["node_id"]=="mos-a" and x["online"] for x in nodes["nodes"])
            payload={"summary":"test","request":"reply with evidence"}
            envlp={"schema":"mos-remote-message/v1","message_id":"MSG-test-v2-1","recipient":"mos-b","kind":"review_request","payload":payload,"payload_sha256":ph(payload),"expires_at":int(time.time())+300}
            st,_,sent=request(base,"POST","/v1/messages",envlp,ta); assert st==201 and sent["stored"]
            st,_,mb=request(base,"GET","/v1/mailbox/mos-b?after=0",token=tb); assert st==200 and mb["messages"][0]["message_id"]=="MSG-test-v2-1"
            st,_,rc=request(base,"POST","/v1/messages/MSG-test-v2-1/receipts",{"status":"read","detail":"seen"},tb); assert st==201
            st,_,pub=request(base,"GET","/v1/public/messages?limit=10"); assert st==200 and pub["messages"][0]["status"]=="read"
            st,_,rot=request(base,"POST","/v1/admin/nodes/mos-a/rotate-token",{},admin); assert st==200 and rot["token"].startswith("mos_")
            st,_,old=request(base,"POST","/v1/heartbeat",{"status":"old-token"},ta); assert st==401
            st,_,new=request(base,"POST","/v1/heartbeat",{"status":"new-token"},rot["token"]); assert st==200
            st,_,dis=request(base,"POST","/v1/admin/nodes/mos-b/disable",{},admin); assert st==200 and dis["enabled"] is False
            st,_,blocked=request(base,"GET","/v1/mailbox/mos-b?after=0",token=tb); assert st==401
            st,_,ena=request(base,"POST","/v1/admin/nodes/mos-b/enable",{},admin); assert st==200 and ena["enabled"] is True
            st,_,backup=request(base,"POST","/v1/admin/backup",{},admin); assert st==201 and backup["created"]
            st,_,health=request(base,"GET","/health"); assert st==200 and health["chain"]["ok"] is True and health["version"]=="2.0.0"
            print(json.dumps({"status":"passed","version":health["version"],"stats":health["stats"],"chain":health["chain"],"tests":["register","heartbeat","presence","send","mailbox","receipt","rotate-token","disable-enable","backup","health"]},indent=2))
        finally:
            p.terminate()
            try: p.wait(timeout=3)
            except subprocess.TimeoutExpired: p.kill()

if __name__=="__main__": main()
