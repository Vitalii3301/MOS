# Start command of a cell on its own machine (memeos-cell-d). Railway runs it base64-encoded: python3 -c "exec(base64.b64decode(...))".
import os,json,base64,hashlib,io,tarfile,time,urllib.request as u
H=os.environ.get('MOS_HUB_URL','https://mos-hub-live-production.up.railway.app');N=os.environ['MOS_NODE_ID'];D=os.path.dirname(os.environ['MEMEOS_DB']);P=D+'/cells/'+N+'.token'
os.makedirs(D+'/cells',exist_ok=True)
if not os.path.exists(P):
 r=u.Request(H+'/v1/nodes/register',method='POST',data=json.dumps({'node_id':N,'capabilities':{'transport':'memeos-cell','machine':'second'}}).encode(),headers={'Authorization':'Bearer '+os.environ['MOS_HUB_ADMIN_TOKEN'],'Content-Type':'application/json'});t=json.load(u.urlopen(r,timeout=30))['token'];f=os.open(P,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.write(f,t.encode());os.close(f);print('registered',N,flush=True)
T=open(P).read().strip()
while not os.environ.get('MEMEOS_CODE_SEQ'):print('registered; waiting for code',flush=True);time.sleep(3600)
s=int(os.environ['MEMEOS_CODE_SEQ']);r=u.Request(H+'/v1/mailbox/'+N+'?after=%d&limit=1'%(s-1),headers={'Authorization':'Bearer '+T});m=json.load(u.urlopen(r,timeout=30))['messages'][0];b=base64.b64decode(m['payload']['code_b64']);h=hashlib.sha256(b).hexdigest()
assert m['stream_seq']==s and m['sender']=='mos-claude' and h==os.environ['MEMEOS_CODE_SHA256'],'code_hash_mismatch:'+h
os.makedirs('/app',exist_ok=True);tarfile.open(fileobj=io.BytesIO(b)).extractall('/app',filter='data');os.chdir('/app');print('memeos code verified',h,'from hub seq',s,flush=True)
os.execvp('python3',['python3','node.py','--loop',os.environ.get('MEMEOS_POLL_SECONDS','60')])
