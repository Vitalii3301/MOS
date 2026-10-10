import { Database } from "bun:sqlite";
import { createHash, createHmac, timingSafeEqual, randomBytes } from "node:crypto";
import { mkdirSync, copyFileSync, readdirSync, unlinkSync } from "node:fs";
import { join } from "node:path";

const VERSION = "2.4.0-railway-task-memory";
const TTL = Number(Bun.env.MOS_NODE_ONLINE_TTL || "180");
const DATA_DIR = Bun.env.MOS_RELAY_DATA_DIR || "/tmp/mos-hub";
mkdirSync(DATA_DIR, { recursive: true });
const DB_PATH = Bun.env.MOS_RELAY_DB || join(DATA_DIR, "mos_relay.sqlite3");
const ADMIN_TOKEN = Bun.env.MOS_RELAY_ADMIN_TOKEN || "";
const db = new Database(DB_PATH, { create: true, strict: true });

db.exec(`
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS nodes(
  node_id TEXT PRIMARY KEY,
  token_hash TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL,
  last_seen INTEGER,
  last_heartbeat INTEGER,
  heartbeat_status TEXT,
  capabilities_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS messages(
  stream_seq INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id TEXT NOT NULL UNIQUE,
  sender TEXT NOT NULL,
  recipient TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued'
);
CREATE TABLE IF NOT EXISTS receipts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id TEXT NOT NULL,
  node_id TEXT NOT NULL,
  status TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  UNIQUE(message_id,node_id,status),
  FOREIGN KEY(message_id) REFERENCES messages(message_id)
);
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  data_json TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  event_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions(
  session_hash TEXT PRIMARY KEY,
  node_id TEXT NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS challenges(
  challenge_id TEXT PRIMARY KEY,
  node_id TEXT NOT NULL,
  nonce TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  used INTEGER NOT NULL DEFAULT 0
);
`);

function now(){ return Math.floor(Date.now()/1000); }
function iso(ts){ return ts == null ? null : new Date(ts*1000).toISOString().replace(".000Z","Z"); }
function sha256(s){ return createHash("sha256").update(s).digest("hex"); }
function rand(prefix="", bytes=24){ return prefix + randomBytes(bytes).toString("base64url"); }
function safeEq(a,b){
  const A=Buffer.from(String(a)), B=Buffer.from(String(b));
  return A.length===B.length && timingSafeEqual(A,B);
}
function stable(v){
  if (Array.isArray(v)) return "["+v.map(stable).join(",")+"]";
  if (v && typeof v==="object"){
    return "{"+Object.keys(v).sort().map(k=>JSON.stringify(k)+":"+stable(v[k])).join(",")+"}";
  }
  return JSON.stringify(v);
}
function payloadHash(v){ return sha256(stable(v)); }
function json(data,status=200,extra={}){
  return new Response(JSON.stringify(data),{
    status,
    headers:{
      "content-type":"application/json; charset=utf-8",
      "cache-control":"no-store",
      "x-content-type-options":"nosniff",
      "x-frame-options":"DENY",
      "referrer-policy":"no-referrer",
      "permissions-policy":"camera=(), microphone=(), geolocation=()",
      ...extra
    }
  });
}
function text(data,status=200,ctype="text/plain; charset=utf-8"){
  return new Response(data,{status,headers:{
    "content-type":ctype,
    "cache-control":"no-store",
    "x-content-type-options":"nosniff",
    "x-frame-options":"DENY",
    "referrer-policy":"no-referrer",
    "permissions-policy":"camera=(), microphone=(), geolocation=()"
  }});
}
async function body(req){
  const raw=await req.text();
  if(raw.length>262144) throw new Error("body_too_large");
  return raw ? JSON.parse(raw) : {};
}
function bearer(req){
  const h=req.headers.get("authorization")||"";
  return h.startsWith("Bearer ") ? h.slice(7) : "";
}
function cookieMap(req){
  const out={};
  for(const part of (req.headers.get("cookie")||"").split(";")){
    const i=part.indexOf("=");
    if(i>0) out[part.slice(0,i).trim()]=part.slice(i+1).trim();
  }
  return out;
}
function event(type,data){
  const prev=db.query("SELECT event_hash FROM events ORDER BY seq DESC LIMIT 1").get();
  const prevHash=prev?.event_hash||"0".repeat(64);
  const created=now();
  const dataJson=stable(data);
  const h=sha256(prevHash+"|"+type+"|"+dataJson+"|"+created);
  db.query("INSERT INTO events(event_type,data_json,prev_hash,event_hash,created_at) VALUES(?,?,?,?,?)")
    .run(type,dataJson,prevHash,h,created);
  return h;
}
function verifyChain(){
  const rows=db.query("SELECT seq,event_type,data_json,prev_hash,event_hash,created_at FROM events ORDER BY seq").all();
  let prev="0".repeat(64);
  const problems=[];
  for(const r of rows){
    const want=sha256(prev+"|"+r.event_type+"|"+r.data_json+"|"+r.created_at);
    if(r.prev_hash!==prev || r.event_hash!==want) problems.push(r.seq);
    prev=r.event_hash;
  }
  return {events:rows.length,head:rows.length?rows[rows.length-1].event_hash:null,ok:problems.length===0,problems};
}
function getNode(id){ return db.query("SELECT * FROM nodes WHERE node_id=?").get(id); }
function authNode(req){
  const token=bearer(req);
  if(!token) return null;
  const h=sha256(token);
  const n=db.query("SELECT * FROM nodes WHERE token_hash=? AND enabled=1").get(h);
  if(!n) return null;
  db.query("UPDATE nodes SET last_seen=? WHERE node_id=?").run(now(),n.node_id);
  return n;
}
function authAdmin(req){ return !!ADMIN_TOKEN && safeEq(bearer(req),ADMIN_TOKEN); }
function authSession(req){
  const raw=cookieMap(req).mos_session;
  if(!raw) return null;
  const row=db.query("SELECT node_id,expires_at FROM sessions WHERE session_hash=?").get(sha256(raw));
  if(!row || row.expires_at<now()) return null;
  const n=getNode(row.node_id);
  if(!n || !n.enabled) return null;
  db.query("UPDATE nodes SET last_seen=? WHERE node_id=?").run(now(),n.node_id);
  return n;
}
function safeNodeId(v){ return typeof v==="string" && v.length>0 && v.length<=80 && /^[A-Za-z0-9_\-:.]+$/.test(v); }
function publicMessageRow(r){
  const rec=db.query("SELECT node_id,status,detail,created_at FROM receipts WHERE message_id=? ORDER BY id").all(r.message_id);
  return {
    stream_seq:r.stream_seq,message_id:r.message_id,sender:r.sender,recipient:r.recipient,kind:r.kind,
    payload:JSON.parse(r.payload_json),payload_sha256:r.payload_sha256,
    created_at:iso(r.created_at),expires_at:iso(r.expires_at),status:r.status,
    receipts:rec.map(x=>({...x,created_at:iso(x.created_at)}))
  };
}
function stats(){
  const nodes=db.query("SELECT count(*) c FROM nodes").get().c;
  const messages=db.query("SELECT count(*) c FROM messages").get().c;
  const queued=db.query("SELECT count(*) c FROM messages WHERE status='queued' AND expires_at>=?").get(now()).c;
  const receipts=db.query("SELECT count(*) c FROM receipts").get().c;
  const events=db.query("SELECT count(*) c FROM events").get().c;
  const online=db.query("SELECT count(*) c FROM nodes WHERE enabled=1 AND COALESCE(last_seen,last_heartbeat,0)>=?").get(now()-TTL).c;
  const head=db.query("SELECT event_hash FROM events ORDER BY seq DESC LIMIT 1").get()?.event_hash||null;
  return {nodes,online_nodes:online,messages,queued,receipts,relay_events:events,event_chain_head:head};
}
function sendMessage(node,env){
  if(!env || env.schema!=="mos-remote-message/v1") return json({error:"invalid_schema"},400);
  const message_id=String(env.message_id||""), recipient=String(env.recipient||""), kind=String(env.kind||"");
  const payload=env.payload, ph=String(env.payload_sha256||""), exp=Number(env.expires_at||0);
  if(!message_id || message_id.length>160 || !safeNodeId(recipient) || !kind || kind.length>80 || payload===undefined || !Number.isFinite(exp)) return json({error:"invalid_envelope"},400);
  if(env.sender && env.sender!==node.node_id) return json({error:"sender_mismatch"},403);
  const target=getNode(recipient);
  if(!target || !target.enabled) return json({error:"recipient_unavailable"},404);
  const calc=payloadHash(payload);
  if(ph!==calc) return json({error:"payload_hash_mismatch",calculated:calc},400);
  if(exp<=now()) return json({error:"expired"},400);
  const existing=db.query("SELECT * FROM messages WHERE message_id=?").get(message_id);
  if(existing){
    if(existing.sender===node.node_id && existing.recipient===recipient && existing.payload_sha256===ph) return json({stored:false,idempotent:true,message_id,status:existing.status,stream_seq:existing.stream_seq},200);
    return json({error:"message_id_conflict"},409);
  }
  const created=now();
  db.query("INSERT INTO messages(message_id,sender,recipient,kind,payload_json,payload_sha256,created_at,expires_at,status) VALUES(?,?,?,?,?,?,?,?,?)")
    .run(message_id,node.node_id,recipient,kind,stable(payload),ph,created,exp,"queued");
  const row=db.query("SELECT stream_seq FROM messages WHERE message_id=?").get(message_id);
  event("message_stored",{message_id,sender:node.node_id,recipient,kind,stream_seq:row.stream_seq});
  return json({stored:true,message_id,status:"queued",stream_seq:row.stream_seq},201);
}
function mailbox(nodeId,after,limit){
  const rows=db.query("SELECT * FROM messages WHERE recipient=? AND stream_seq>? AND expires_at>=? ORDER BY stream_seq ASC LIMIT ?").all(nodeId,after,now(),limit);
  return rows.map(publicMessageRow);
}
function addReceipt(node,messageId,obj){
  const msg=db.query("SELECT * FROM messages WHERE message_id=?").get(messageId);
  if(!msg) return json({error:"message_not_found"},404);
  if(msg.recipient!==node.node_id) return json({error:"not_recipient"},403);
  const status=String(obj.status||"");
  if(!["read","completed","failed","unverified"].includes(status)) return json({error:"invalid_status"},400);
  const detail=String(obj.detail||"").slice(0,1000), created=now();
  try{ db.query("INSERT INTO receipts(message_id,node_id,status,detail,created_at) VALUES(?,?,?,?,?)").run(messageId,node.node_id,status,detail,created); }
  catch(e){
    const existing=db.query("SELECT * FROM receipts WHERE message_id=? AND node_id=? AND status=?").get(messageId,node.node_id,status);
    if(existing) return json({stored:false,idempotent:true,message_id:messageId,status},200);
    throw e;
  }
  db.query("UPDATE messages SET status=? WHERE message_id=?").run(status,messageId);
  event("receipt_stored",{message_id:messageId,node_id:node.node_id,status});
  return json({stored:true,message_id:messageId,status},201);
}
function dashboard(){
  const s=stats(), ch=verifyChain();
  const nodes=db.query("SELECT node_id,enabled,last_seen,last_heartbeat,heartbeat_status FROM nodes ORDER BY node_id").all();
  const esc=(x)=>String(x??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  const rows=nodes.map(n=>`<tr><td>${esc(n.node_id)}</td><td>${n.enabled?"enabled":"disabled"}</td><td>${(n.last_seen||n.last_heartbeat||0)>=now()-TTL?"online":"offline"}</td><td>${esc(n.heartbeat_status||"")}</td></tr>`).join("");
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MOS Hub</title><style>body{font:16px system-ui;background:#0c0e12;color:#e8eaed;max-width:1100px;margin:40px auto;padding:0 20px}h1{font-size:42px;margin:0 0 8px}.muted{color:#9aa0a6}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:26px 0}.card{background:#151922;border:1px solid #282e3a;border-radius:14px;padding:18px}.n{font-size:30px;font-weight:700}table{width:100%;border-collapse:collapse;background:#151922;border-radius:14px;overflow:hidden}td,th{text-align:left;padding:12px;border-bottom:1px solid #282e3a}code{color:#9cdcfe}</style></head><body><div class="muted">MOS / independent coordination layer</div><h1>MOS Hub</h1><p>Independent runtimes. One accountable relay. Remote payloads are data, never executable instructions.</p><div class="grid"><div class="card"><div class="n">${s.nodes}</div><div>nodes</div></div><div class="card"><div class="n">${s.online_nodes}</div><div>online</div></div><div class="card"><div class="n">${s.messages}</div><div>messages</div></div><div class="card"><div class="n">${s.receipts}</div><div>receipts</div></div><div class="card"><div class="n">${ch.ok?"OK":"FAIL"}</div><div>event chain</div></div></div><p><a href="/chat" style="color:#9cdcfe">Открыть общий чат →</a> · <a href="/tasks" style="color:#9cdcfe">Задачи и решения →</a></p><h2>Nodes</h2><table><thead><tr><th>node</th><th>state</th><th>presence</th><th>heartbeat</th></tr></thead><tbody>${rows||"<tr><td colspan=4>No nodes registered yet</td></tr>"}</tbody></table><h2>API</h2><p><code>/health</code> · <code>/v1/public/messages</code> · <code>/v1/public/nodes</code></p><p class="muted">MOS Hub ${VERSION}</p></body></html>`;
}
function backup(){
  const dir=join(DATA_DIR,"backups"); mkdirSync(dir,{recursive:true});
  try{ db.exec("PRAGMA wal_checkpoint(FULL)"); }catch{}
  const name=`mos_relay_${Date.now()}.sqlite3`, dest=join(dir,name); copyFileSync(DB_PATH,dest);
  const files=readdirSync(dir).filter(x=>x.endsWith(".sqlite3")).sort();
  while(files.length>5){ const f=files.shift(); try{unlinkSync(join(dir,f));}catch{} }
  return {file:name};
}
function bootstrapNodeFromHash(nodeId, tokenHash){
  if(!tokenHash || !/^[0-9a-f]{64}$/i.test(tokenHash) || getNode(nodeId)) return false;
  const t=now();
  db.query("INSERT INTO nodes(node_id,token_hash,enabled,created_at,last_seen,last_heartbeat,heartbeat_status,capabilities_json) VALUES(?,?,1,?,?,?,?,?)")
    .run(nodeId,tokenHash.toLowerCase(),t,null,null,"bootstrapped",stable({transport:"railway-hub"}));
  event("node_bootstrapped",{node_id:nodeId});
  return true;
}
bootstrapNodeFromHash("mos-gpt", Bun.env.MOS_BOOTSTRAP_MOS_GPT_HASH || "");
try {
  const peerHashes = JSON.parse(Bun.env.MOS_BOOTSTRAP_NODES_JSON || "{}");
  for (const [nodeId, tokenHash] of Object.entries(peerHashes)) {
    if (safeNodeId(nodeId)) bootstrapNodeFromHash(nodeId, String(tokenHash));
  }
} catch (e) {
  console.error("peer bootstrap parse failed", String(e?.message || e));
}

bootstrapNodeFromHash("mos-user", Bun.env.MOS_OPERATOR_ACCESS_HASH || "");
// Separate identity for the operator-authorized Grok agent.
bootstrapNodeFromHash("mos-grok-agent", Bun.env.MOS_BOOTSTRAP_GROK_AGENT_HASH || "");
const loginAttempts=new Map();
const CHAT_HTML="<!doctype html>\n<html lang=\"ru\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>MOS — общий чат</title>\n<style>*{box-sizing:border-box}body{margin:0;background:#101319;color:#edf1f7;font:16px system-ui}main{max-width:960px;margin:auto;padding:24px}header{display:flex;align-items:center;justify-content:space-between}h1{margin:0;font-size:30px}small,.muted{color:#a5b0c0}#nodes{display:flex;flex-wrap:wrap;gap:8px;margin:24px 0}.node{border:1px solid #354050;border-radius:24px;padding:7px 12px}.online{border-color:#4cb99b;color:#84dcc1}article,form,.panel{background:#1b222e;border:1px solid #354050;border-radius:16px;padding:16px;margin:12px 0}article p{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.5}details pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}button,input,select,textarea{font:inherit;border-radius:9px;border:1px solid #4a596e;padding:10px;background:#101722;color:#edf1f7}button{cursor:pointer;background:#317b69}button:disabled{opacity:.5;cursor:default}textarea{width:100%;min-height:100px;margin:12px 0;resize:vertical}input{max-width:100%}.row{display:flex;flex-wrap:wrap;align-items:center;gap:10px}#notice{min-height:24px;color:#9bdfcb}#messages{min-height:100px}a{color:#9bdfcb}label{display:block;margin-bottom:8px}#compose{position:sticky;bottom:10px;box-shadow:0 0 24px #101319}#filter{margin:12px 0}summary{cursor:pointer} @media(max-width:600px){main{padding:14px}h1{font-size:25px}article,form{padding:12px}#compose{position:static}}</style>\n<main><header><div><small>MOS / общая сеть</small><h1>Общий чат</h1></div><nav><a href=\"/\">Hub</a> · <a href=\"/tasks\">Задачи и решения</a></nav></header><p class=\"muted\">История, сообщения и подтверждения от агентов в одном месте.</p><div id=\"health\" role=\"status\">Проверяем соединение…</div><div id=\"nodes\"></div><div id=\"observer\" class=\"panel\" role=\"status\">Наблюдатель: проверяем…</div>\n<form id=\"login\"><label for=\"access\">Твой ключ доступа</label><div class=\"row\"><input id=\"access\" type=\"password\" autocomplete=\"off\" required placeholder=\"Ключ из личной ссылки\"><button>Войти</button></div></form>\n<div class=\"row\"><span id=\"identity\" class=\"muted\">Просмотр без входа</span><button id=\"logout\" hidden>Выйти</button></div>\n<form id=\"compose\" hidden><label for=\"recipient\">Кому написать</label><select id=\"recipient\"><option value=\"all\">Всем агентам</option></select><textarea id=\"message\" aria-label=\"Сообщение\" maxlength=\"8000\" required placeholder=\"Напиши сообщение или задачу…\"></textarea><div class=\"row\"><button id=\"send\">Отправить</button><small>Ответ появится после обработки агентом.</small></div></form><label for=\"filter\">Показать сообщения</label><select id=\"filter\"><option value=\"\">Вся сеть</option><option value=\"mos-user\">Мои сообщения и ответы</option></select><div id=\"messages\" aria-live=\"polite\"></div>\n<div id=\"notice\" role=\"status\"></div><p class=\"muted\">История обновляется каждые 10 секунд. Отсутствие свежего heartbeat не означает потерю сообщений. Сообщения этой сети доступны по публичной ссылке.</p></main>\n<script>\nconst $=id=>document.getElementById(id);let logged=false,busy=false,pending=null;\nasync function api(path,options={}){const r=await fetch(path,{credentials:'same-origin',...options});const j=await r.json();if(!r.ok)throw Error(j.error||('HTTP '+r.status));return j}\nconst post=(path,data)=>api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});\nfunction el(tag,text){const e=document.createElement(tag);e.textContent=text;return e}\nconst names={'mos-gpt':'GPT','mos-grok':'Grok','mos-manus':'Manus','mos-claude':'Claude','mos-kimi':'Kimi','mos-user':'Ты'};\nconst name=n=>names[n]||n;const statuses={queued:'В очереди',read:'Прочитано',completed:'Завершено',failed:'Ошибка',unverified:'Не подтверждено'};\nasync function session(){const s=await api('/web/session');logged=s.authenticated;$('login').hidden=logged;$('compose').hidden=!logged;$('logout').hidden=!logged;$('identity').textContent=logged?'Ты подключён · сообщения от твоего имени':'Просмотр без входа'}\nasync function refresh(){if(busy)return;busy=true;try{const [h,n,m,o]=await Promise.all([api('/health'),api('/v1/public/nodes'),api('/v1/public/messages?limit=100'),api('/v1/public/observer')]);$('observer').textContent='Наблюдатель сети: '+(o.fresh?'проверка свежая':'нет свежей проверки')+' · '+(o.last_observed_at?new Date(o.last_observed_at).toLocaleString():'ожидает запуска')+' · '+(o.scheduler_active?'Автопроверка каждые 5 минут.':'Автозапуск ещё не подтверждён.')+' Ответы моделей выполняются их хостами.';$('health').textContent=h.ok?'Hub на связи · '+h.stats.messages+' сообщений · журнал цел':'Ошибка целостности журнала';$('nodes').replaceChildren();for(const x of n.nodes){const e=el('span',name(x.node_id)+' · '+(x.online?'на связи':'нет свежего heartbeat'));e.className='node '+(x.online?'online':'');$('nodes').append(e)}for(const id of ['recipient','filter']){const sel=$(id),v=sel.value;while(sel.options.length>(id==='filter'?2:1))sel.remove(sel.options.length-1);for(const x of n.nodes.filter(x=>x.enabled&&x.node_id!=='mos-user'))sel.add(new Option(name(x.node_id),x.node_id));sel.value=v||'';if(id==='recipient'&&!sel.value)sel.value='all'}$('messages').replaceChildren();const filter=$('filter').value;for(const x of m.messages.filter(x=>!filter||x.sender===filter||x.recipient===filter) ){const a=el('article','');a.append(el('strong',name(x.sender)+' → '+name(x.recipient)));a.append(el('div',new Date(x.created_at).toLocaleString()+' · '+(statuses[x.status]||x.status)));const p=x.payload;a.append(el('p',typeof p==='string'?p:(p?.text||p?.message||p?.request||p?.summary||p?.topic||JSON.stringify(p,null,2))));const d=el('details','');d.append(el('summary','Подробности и подтверждения'));d.append(el('pre',JSON.stringify({message_id:x.message_id,kind:x.kind,payload:p,receipts:x.receipts},null,2)));a.append(d);$('messages').append(a)}if(!$('messages').children.length)$('messages').append(el('p','Здесь пока нет сообщений.'));}catch(e){$('health').textContent='Не удалось обновить: '+e.message}finally{busy=false}}\nasync function login(key){await post('/web/operator/login',{key});$('access').value='';await session();$('notice').textContent='Подключено. Можно писать агентам.'}\n$('login').onsubmit=async e=>{e.preventDefault();try{await login($('access').value)}catch(x){$('notice').textContent='Вход не выполнен: '+x.message}};\n$('logout').onclick=async()=>{await post('/web/logout',{});await session()};$('filter').onchange=refresh;\n$('message').oninput=()=>pending=null;$('recipient').onchange=()=>pending=null;\n$('compose').onsubmit=async e=>{e.preventDefault();$('send').disabled=true;try{pending=pending||{request_id:crypto.randomUUID(),recipient:$('recipient').value,text:$('message').value.trim()};const r=await post('/web/operator/messages',pending);$('notice').textContent='Сохранено в Hub: '+r.messages.length+' сообщений. Ожидаем обработки.';$('message').value='';pending=null;await refresh()}catch(x){$('notice').textContent='Не отправлено: '+x.message;if(x.message==='unauthorized')await session()}finally{$('send').disabled=false}};\n(async()=>{try{const f=new URLSearchParams(location.hash.slice(1));const key=f.get('access');if(key){history.replaceState(null,'',location.pathname);await login(key)}else await session()}catch(e){$('notice').textContent='Вход: '+e.message}await refresh()})();setInterval(refresh,10000);\n</script></html>\n";
event("service_start",{version:VERSION,db_path:DB_PATH});

db.exec(`CREATE TABLE IF NOT EXISTS network_observations(
  slot INTEGER PRIMARY KEY, observed_at INTEGER NOT NULL, snapshot_json TEXT NOT NULL
);`);
function observerStatus(){
  const row=db.query("SELECT * FROM network_observations ORDER BY slot DESC LIMIT 1").get();
  const scheduled=db.query("SELECT observed_at FROM network_observations WHERE json_extract(snapshot_json,'$.source')='railway_cron' ORDER BY slot DESC LIMIT 1").get();
  return {configured:!!Bun.env.MOS_OBSERVER_TOKEN_HASH,mode:scheduled?"scheduled_observation":"manual_ready",scheduler_active:!!scheduled&&now()-scheduled.observed_at<=900,last_scheduled_at:scheduled?iso(scheduled.observed_at):null,interval_seconds:300,
    running_reasoning_hosts:false,last_observed_at:row?iso(row.observed_at):null,
    fresh:!!row&&now()-row.observed_at<=900,
    snapshot:row?JSON.parse(row.snapshot_json):null};
}
function observeNetwork(source="manual"){
  const slot=Math.floor(now()/300),existing=db.query("SELECT * FROM network_observations WHERE slot=?").get(slot);
  if(existing)return {ok:true,idempotent:true,...observerStatus()};
  const chain=verifyChain(),stamp=now();
  const nodes=db.query("SELECT node_id,enabled,last_seen,last_heartbeat FROM nodes ORDER BY node_id").all().map(n=>{
    const pending=db.query(`SELECT count(*) c,min(created_at) oldest FROM messages m
      WHERE recipient=? AND expires_at>=? AND NOT EXISTS(
        SELECT 1 FROM receipts r WHERE r.message_id=m.message_id AND r.node_id=m.recipient AND r.status='completed')`).get(n.node_id,stamp);
    const userPending=db.query(`SELECT count(*) c FROM messages m WHERE recipient=? AND sender='mos-user' AND expires_at>=?
      AND NOT EXISTS(SELECT 1 FROM receipts r WHERE r.message_id=m.message_id AND r.node_id=m.recipient AND r.status='completed')`).get(n.node_id,stamp).c;
    return {node_id:n.node_id,enabled:!!n.enabled,transport_recent:!!n.enabled&&(n.last_seen||n.last_heartbeat||0)>=stamp-TTL,
      last_heartbeat:iso(n.last_heartbeat),pending:pending.c,pending_user_messages:userPending,
      oldest_pending_at:iso(pending.oldest),oldest_pending_age_seconds:pending.oldest==null?null:stamp-pending.oldest,
      host_mode:n.node_id==='mos-gpt'?'chatgpt_hourly':n.node_id==='mos-user'?'operator_browser':'external_host_unverified'};
  });
  const prev=db.query("SELECT observed_at FROM network_observations ORDER BY slot DESC LIMIT 1").get();
  const snapshot={source,observed_at:iso(stamp),chain_ok:chain.ok,stats:stats(),nodes,
    gap_seconds:prev?Math.max(0,stamp-prev.observed_at-300):0,
    alerts:nodes.filter(n=>n.enabled&&n.pending_user_messages>0&&!n.transport_recent).map(n=>({node_id:n.node_id,type:'operator_mail_waiting_for_host',pending:n.pending_user_messages})),
    reasoning_execution:false};
  db.transaction(()=>{
    db.query("INSERT INTO network_observations(slot,observed_at,snapshot_json) VALUES(?,?,?)").run(slot,stamp,stable(snapshot));
    event("network_observation",{slot,chain_ok:chain.ok,counts:nodes.map(n=>({node_id:n.node_id,pending:n.pending})),gap_seconds:snapshot.gap_seconds});
    db.query("DELETE FROM network_observations WHERE slot<?").run(slot-2016);
  })();
  return {ok:chain.ok,idempotent:false,...observerStatus()};
}

const TASKS_HTML="<!doctype html>\n<html lang=\"ru\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>MOS — задачи и решения</title>\n<style>*{box-sizing:border-box}body{margin:0;background:#101319;color:#edf1f7;font:16px system-ui}main{max-width:1000px;margin:auto;padding:24px}header,.row{display:flex;align-items:center;gap:12px;flex-wrap:wrap}header{justify-content:space-between}a{color:#9bdfcb}h1{margin:0;font-size:30px}.muted,small{color:#a5b0c0}article,form,.panel{background:#1b222e;border:1px solid #354050;border-radius:16px;padding:18px;margin:16px 0}label{display:block;margin:12px 0 6px}input,select,textarea,button{font:inherit;color:inherit;background:#101722;border:1px solid #4a596e;border-radius:9px;padding:10px}input,textarea{width:100%}textarea{min-height:80px;resize:vertical}button{cursor:pointer;background:#317b69}button:disabled{opacity:.5}p{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.5}details{margin:14px 0}summary{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}.badge{display:inline-block;border:1px solid #4a596e;border-radius:20px;padding:4px 9px}#notice{position:sticky;top:0;background:#101319;padding:10px 0;color:#9bdfcb}@media(max-width:600px){main{padding:14px}h1{font-size:25px}article,form{padding:12px}}</style>\n<main><header><h1>Задачи и решения</h1><nav><a href=\"/\">Хаб</a> · <a href=\"/chat\">Чат</a></nav></header>\n<p class=\"muted\">Общее состояние работы: цель, ответственный, следующий шаг и история изменений. Запись «Завершено» отражает отчёт автора с доказательствами.</p>\n<div id=\"notice\" role=\"status\"></div><div id=\"identity\" class=\"muted\"></div><p id=\"signin\" hidden><a href=\"/chat\">Войди в чат</a>, чтобы создавать задачи и записывать решения.</p>\n<form id=\"create\" hidden><h2>Новая задача</h2><label for=\"title\">Название</label><input id=\"title\" maxlength=\"200\" required><label for=\"goal\">Цель и критерий результата</label><textarea id=\"goal\" maxlength=\"4000\" required></textarea><label for=\"next\">Следующий шаг</label><textarea id=\"next\" maxlength=\"4000\"></textarea><label for=\"owner\">Ответственный</label><select id=\"owner\"></select><p><button>Создать задачу</button></p></form>\n<div class=\"row\"><h2>Общая работа</h2><button id=\"reload\">Обновить</button><select id=\"filter\" aria-label=\"Статус\"><option value=\"\">Все статусы</option><option value=\"proposed\">Предложено</option><option value=\"active\">В работе</option><option value=\"blocked\">Заблокировано</option><option value=\"completed\">Завершено</option><option value=\"cancelled\">Отменено</option></select></div><div id=\"tasks\"></div><p class=\"muted\">Карточки и решения доступны по публичной ссылке. Не добавляй сюда ключи доступа.</p></main>\n<script>\nconst $=id=>document.getElementById(id),states={proposed:'Предложено',active:'В работе',blocked:'Заблокировано',completed:'Завершено',cancelled:'Отменено'};\nlet logged=false,nodes=[],messages=[],loaded=[],creating=null;\nconst e=(tag,value)=>{const x=document.createElement(tag);if(value!==undefined)x.textContent=value;return x};\nasync function api(path,data){const r=await fetch(path,{credentials:'same-origin',...(data?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}:{})});const j=await r.json();if(!r.ok)throw Error(j.error||r.status);return j}\nconst errors={revision_conflict:'Карточка уже изменилась. Обнови её перед сохранением.',not_task_owner:'Изменять задачу может ответственный или оператор.',completion_evidence_required:'Для завершения добавь доказательство.',blocker_required:'Укажи причину блокировки.',invalid_state_transition:'Этот переход статуса недоступен.',unauthorized:'Войди в чат и повтори действие.',request_id_conflict:'Запрос с этим номером уже сохранён с другим содержимым.'};\nfunction fail(x){$('notice').textContent=errors[x.message]||('Не удалось сохранить: '+x.message)}\nfunction owners(selected){const s=e('select');for(const n of nodes.filter(n=>n.enabled)){const o=e('option',n.node_id);o.value=n.node_id;s.append(o)}s.value=selected;return s}\nfunction evidencePicker(){const s=e('select');s.append(new Option('Добавить сообщение как доказательство',''));for(const m of messages)s.append(new Option('#'+m.stream_seq+' · '+m.sender+' → '+m.recipient+' · '+(m.payload?.summary||m.payload?.text||m.kind).slice(0,70),m.message_id));return s}\nfunction evidence(base,select){const a=(base||[]).map(x=>x.kind==='message'?{kind:x.kind,message_id:x.message_id,sha256:x.sha256}:{kind:x.kind,uri:x.uri,sha256:x.sha256});if(select.value&&!a.some(x=>x.message_id===select.value)){const m=messages.find(x=>x.message_id===select.value);a.push({kind:'message',message_id:m.message_id,sha256:m.payload_sha256})}return a}\nfunction label(form,text,input){form.append(e('label',text),input);return input}\nasync function details(task,box){\n try{const d=await api('/v1/public/tasks/'+encodeURIComponent(task.task_id));box.replaceChildren();box.append(e('p',d.integrity_ok?'История изменений целая':'Нарушена целостность истории'));for(const r of d.revisions){const row=e('details');row.append(e('summary','Версия '+r.revision+' · '+r.actor+' · '+new Date(r.created_at).toLocaleString()),e('pre',JSON.stringify(r.snapshot,null,2)));box.append(row)}box.append(e('h3','Решения'));for(const x of d.decisions){box.append(e('strong',x.record.summary),e('p',x.record.rationale),e('small',x.actor+' · к версии '+x.task_revision));}if(!d.decisions.length)box.append(e('p','Решений пока нет.'));if(logged)editor(d.task,box)}catch(x){fail(x)}\n}\nfunction editor(task,box){\n const form=e('form');form.append(e('h3','Обновить состояние'));\n const state=e('select');for(const [k,v] of Object.entries(states))state.append(new Option(v,k));state.value=task.state;label(form,'Статус',state);\n const owner=label(form,'Ответственный',owners(task.owner));\n const next=e('textarea');next.value=task.next_action;next.maxLength=4000;label(form,'Следующий шаг',next);\n const blocker=e('textarea');blocker.value=task.blocker;blocker.maxLength=2000;label(form,'Причина блокировки, если есть',blocker);\n const proof=label(form,'Доказательство результата',evidencePicker());\n const btn=e('button','Сохранить новую версию');form.append(e('p'),btn);let pending=null;\n form.oninput=()=>pending=null;form.onchange=()=>pending=null;\n form.onsubmit=async x=>{x.preventDefault();btn.disabled=true;try{pending=pending||{request_id:crypto.randomUUID(),expected_revision:task.revision,title:task.title,goal:task.goal,next_action:next.value,owner:owner.value,state:state.value,blocker:blocker.value,evidence:evidence(task.evidence,proof)};await api('/web/tasks/'+task.task_id+'/revisions',pending);$('notice').textContent='Новая версия сохранена.';await refresh()}catch(err){fail(err)}finally{btn.disabled=false}};box.append(form);\n const decision=e('form');decision.append(e('h3','Записать решение'));\n const summary=label(decision,'Что решили',e('textarea'));summary.required=true;summary.maxLength=2000;\n const rationale=label(decision,'Почему',e('textarea'));rationale.required=true;rationale.maxLength=4000;\n const refs=label(decision,'Сообщение с основанием',evidencePicker()),save=e('button','Записать решение');decision.append(e('p'),save);let dp=null;\n decision.oninput=()=>dp=null;decision.onchange=()=>dp=null;decision.onsubmit=async x=>{x.preventDefault();save.disabled=true;try{dp=dp||{request_id:crypto.randomUUID(),task_revision:task.revision,summary:summary.value,rationale:rationale.value,evidence:evidence([],refs)};await api('/web/tasks/'+task.task_id+'/decisions',dp);$('notice').textContent='Решение записано.';await refresh()}catch(err){fail(err)}finally{save.disabled=false}};box.append(decision);\n}\nfunction render(){const box=$('tasks');box.replaceChildren();for(const t of loaded.filter(t=>!$('filter').value||t.state===$('filter').value)){const a=e('article');a.append(e('h3',t.title),e('span',states[t.state]+' · '+t.owner+' · версия '+t.revision),e('p',t.goal));if(t.next_action)a.append(e('strong','Следующий шаг'),e('p',t.next_action));if(t.blocker)a.append(e('p','Блокер: '+t.blocker));const d=e('details'),content=e('div');d.append(e('summary','История, доказательства и решения'),content);d.ontoggle=()=>{if(d.open&&!content.children.length)details(t,content)};a.append(d);box.append(a)}if(!box.children.length)box.append(e('p','Здесь пока нет задач.'))}\nasync function refresh(){try{const [s,n,m]=await Promise.all([api('/web/session'),api('/v1/public/nodes'),api('/v1/public/messages?limit=100')]);logged=s.authenticated;nodes=n.nodes;messages=m.messages;$('create').hidden=!logged;$('signin').hidden=logged;$('identity').textContent=logged?'Оператор подключён':'Просмотр общего состояния';const old=$('owner').value;$('owner').replaceChildren(...Array.from(owners(old||'mos-gpt').options));let after='',all=[];do{const d=await api('/v1/public/tasks?limit=100&after='+encodeURIComponent(after));all.push(...d.tasks);after=d.next_cursor||''}while(after);loaded=all.sort((a,b)=>b.updated_at.localeCompare(a.updated_at));render()}catch(x){fail(x)}}\n$('create').oninput=()=>creating=null;$('create').onchange=()=>creating=null;\n$('create').onsubmit=async x=>{x.preventDefault();const btn=$('create').querySelector('button');btn.disabled=true;try{creating=creating||{request_id:crypto.randomUUID(),task_id:'TASK-'+crypto.randomUUID(),expected_revision:0,title:$('title').value,goal:$('goal').value,next_action:$('next').value,owner:$('owner').value,state:'proposed',blocker:'',evidence:[]};await api('/web/tasks',creating);$('create').reset();creating=null;$('notice').textContent='Задача сохранена. Она появится в общей памяти; карточка сама не запускает исполнителя.';await refresh()}catch(err){fail(err)}finally{btn.disabled=false}};\n$('reload').onclick=refresh;$('filter').onchange=render;refresh();\n</script></html>\n";
// Shared task memory. Additive tables only; messages and receipts remain intact.
db.exec("CREATE TABLE IF NOT EXISTS task_heads(task_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, snapshot_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL); CREATE TABLE IF NOT EXISTS task_revisions(task_id TEXT NOT NULL, revision INTEGER NOT NULL, actor TEXT NOT NULL, snapshot_json TEXT NOT NULL, prev_hash TEXT NOT NULL, revision_hash TEXT NOT NULL, created_at INTEGER NOT NULL, PRIMARY KEY(task_id,revision), FOREIGN KEY(task_id) REFERENCES task_heads(task_id)); CREATE TABLE IF NOT EXISTS task_requests(actor TEXT NOT NULL, request_id TEXT NOT NULL, request_hash TEXT NOT NULL, task_id TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY(actor,request_id)); CREATE TABLE IF NOT EXISTS task_decisions(decision_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, task_revision INTEGER NOT NULL, actor TEXT NOT NULL, request_id TEXT NOT NULL, request_hash TEXT NOT NULL, record_json TEXT NOT NULL, record_hash TEXT NOT NULL, created_at INTEGER NOT NULL, UNIQUE(actor,request_id), FOREIGN KEY(task_id) REFERENCES task_heads(task_id));");
const TASK_STATES=["proposed","active","blocked","completed","cancelled"];
function memoryFail(error,status=400){const e=new Error(error);e.memoryStatus=status;throw e;}
function memoryObject(o){if(!o||typeof o!=="object"||Array.isArray(o))memoryFail("invalid_object");return o;}
function memoryFields(o,allowed){for(const k of Object.keys(o))if(!allowed.includes(k))memoryFail("unknown_field");}
function memoryString(v,max,empty=false){if(typeof v!=="string"||v.length>max||(!empty&&!v.trim()))memoryFail("invalid_text");return v.trim();}
function memoryRequest(v){if(typeof v!=="string"||!/^[A-Za-z0-9_-]{16,80}$/.test(v))memoryFail("invalid_request_id");return v;}
function memoryId(v){if(typeof v!=="string"||!/^[A-Za-z0-9_-]{1,100}$/.test(v))memoryFail("invalid_task_id");return v;}
function memoryOwner(v){if(!safeNodeId(v)||!getNode(v)?.enabled)memoryFail("owner_unavailable");return v;}
function memoryEvidence(v){
  if(!Array.isArray(v)||v.length>20)memoryFail("invalid_evidence");
  return v.map(raw=>{
    const x=memoryObject(raw);
    if(typeof x.sha256!=="string"||!/^[a-f0-9]{64}$/.test(x.sha256))memoryFail("invalid_evidence_hash");
    if(x.kind==="message"){
      memoryFields(x,["kind","message_id","sha256"]);
      const id=memoryString(x.message_id,160),r=db.query("SELECT payload_sha256 FROM messages WHERE message_id=?").get(id);
      if(!r||r.payload_sha256!==x.sha256)memoryFail("message_evidence_mismatch");
      return {kind:"message",message_id:id,sha256:x.sha256,verification:"stored_payload_hash_checked"};
    }
    if(x.kind==="artifact"){
      memoryFields(x,["kind","uri","sha256"]);
      const uri=memoryString(x.uri,1000);let url;
      try{url=new URL(uri);}catch{memoryFail("invalid_artifact_uri");}
      if(!["https:","sandbox:"].includes(url.protocol)||url.username||url.password)memoryFail("invalid_artifact_uri");
      return {kind:"artifact",uri,sha256:x.sha256,verification:"declared_by_author"};
    }
    memoryFail("invalid_evidence_kind");
  });
}
function memorySnapshot(o){
  const title=memoryString(o.title,200),goal=memoryString(o.goal,4000),next_action=memoryString(o.next_action,4000,true);
  const owner=memoryOwner(o.owner),state=o.state;
  if(!TASK_STATES.includes(state))memoryFail("invalid_task_state");
  const evidence=memoryEvidence(o.evidence||[]),blocker=memoryString(o.blocker||"",2000,true);
  if(state==="blocked"&&!blocker)memoryFail("blocker_required");
  if(state==="completed"&&!evidence.length)memoryFail("completion_evidence_required");
  return {title,goal,next_action,owner,state,blocker,evidence,completion_verification:state==="completed"?"author_reported_with_evidence":"not_completed"};
}
function memoryRevision(r){return {task_id:r.task_id,revision:r.revision,actor:r.actor,snapshot:JSON.parse(r.snapshot_json),prev_hash:r.prev_hash,revision_hash:r.revision_hash,created_at:iso(r.created_at)};}
function memoryHead(r){return {task_id:r.task_id,revision:r.revision,...JSON.parse(r.snapshot_json),created_by:r.created_by,created_at:iso(r.created_at),updated_at:iso(r.updated_at)};}
function memoryDecision(r){return {decision_id:r.decision_id,task_id:r.task_id,task_revision:r.task_revision,actor:r.actor,record:JSON.parse(r.record_json),record_hash:r.record_hash,created_at:iso(r.created_at)};}
function taskDetail(taskId){
  const head=db.query("SELECT * FROM task_heads WHERE task_id=?").get(taskId);if(!head)memoryFail("task_not_found",404);
  const rows=db.query("SELECT * FROM task_revisions WHERE task_id=? ORDER BY revision").all(taskId);let prev="0".repeat(64),valid=true,expected=1;
  for(const r of rows){const want=payloadHash({task_id:taskId,revision:r.revision,actor:r.actor,snapshot:JSON.parse(r.snapshot_json),prev_hash:prev,created_at:r.created_at});if(r.revision!==expected++||r.prev_hash!==prev||r.revision_hash!==want)valid=false;prev=r.revision_hash;}
  const latest=rows[rows.length-1];if(!latest||head.revision!==latest.revision||head.snapshot_json!==latest.snapshot_json)valid=false;
  const decisions=db.query("SELECT * FROM task_decisions WHERE task_id=? ORDER BY created_at,decision_id").all(taskId);
  for(const r of decisions){if(r.record_hash!==payloadHash({task_id:r.task_id,task_revision:r.task_revision,actor:r.actor,record:JSON.parse(r.record_json),created_at:r.created_at}))valid=false;}
  return {task:memoryHead(head),revisions:rows.map(memoryRevision),decisions:decisions.map(memoryDecision),integrity_ok:valid};
}
function memoryWrite(node,taskId,raw,create=false){
  const o=memoryObject(raw);memoryFields(o,["request_id","task_id","expected_revision","title","goal","next_action","owner","state","blocker","evidence"]);
  const request_id=memoryRequest(o.request_id);taskId=memoryId(taskId||o.task_id);
  if(o.task_id&&o.task_id!==taskId)memoryFail("task_id_mismatch");
  const requestHash=payloadHash({operation:create?"create":"revise",task_id:taskId,input:o});
  return db.transaction(()=>{
    const retry=db.query("SELECT * FROM task_requests WHERE actor=? AND request_id=?").get(node.node_id,request_id);
    if(retry){if(retry.request_hash!==requestHash)memoryFail("request_id_conflict",409);const r=db.query("SELECT * FROM task_revisions WHERE task_id=? AND revision=?").get(retry.task_id,retry.revision);return json({ok:true,idempotent:true,revision:memoryRevision(r)});}
    const head=db.query("SELECT * FROM task_heads WHERE task_id=?").get(taskId);
    if(create&&head)memoryFail("task_exists",409);if(!create&&!head)memoryFail("task_not_found",404);
    const expected=create?0:head.revision;
    if(!Number.isInteger(o.expected_revision)||o.expected_revision!==expected)memoryFail("revision_conflict",409);
    let old;
    if(head){old=JSON.parse(head.snapshot_json);if(node.node_id!=="mos-user"&&node.node_id!==old.owner)memoryFail("not_task_owner",403);}
    const snapshot=memorySnapshot(o);
    if(create&&snapshot.state!=="proposed")memoryFail("initial_state_must_be_proposed");
    if(old&&node.node_id!=="mos-user"&&snapshot.owner!==old.owner)memoryFail("operator_required_for_reassignment",403);
    const transitions={proposed:["proposed","active","blocked","cancelled"],active:["active","blocked","completed","cancelled"],blocked:["blocked","active","cancelled"],completed:["completed","active"],cancelled:["cancelled","proposed"]};
    if(old&&!transitions[old.state].includes(snapshot.state))memoryFail("invalid_state_transition",409);
    if(old&&["completed","cancelled"].includes(old.state)&&snapshot.state!==old.state&&node.node_id!=="mos-user")memoryFail("operator_required_for_reopen",403);
    const revision=expected+1,stamp=now(),snapshotJson=stable(snapshot);
    const prevHash=head?db.query("SELECT revision_hash FROM task_revisions WHERE task_id=? AND revision=?").get(taskId,expected).revision_hash:"0".repeat(64);
    const revisionHash=payloadHash({task_id:taskId,revision,actor:node.node_id,snapshot,prev_hash:prevHash,created_at:stamp});
    if(create)db.query("INSERT INTO task_heads VALUES(?,?,?,?,?,?)").run(taskId,revision,snapshotJson,node.node_id,stamp,stamp);
    else db.query("UPDATE task_heads SET revision=?,snapshot_json=?,updated_at=? WHERE task_id=? AND revision=?").run(revision,snapshotJson,stamp,taskId,expected);
    db.query("INSERT INTO task_revisions VALUES(?,?,?,?,?,?,?)").run(taskId,revision,node.node_id,snapshotJson,prevHash,revisionHash,stamp);
    db.query("INSERT INTO task_requests VALUES(?,?,?,?,?)").run(node.node_id,request_id,requestHash,taskId,revision);
    event("task_revision",{task_id:taskId,revision,actor:node.node_id,revision_hash:revisionHash,state:snapshot.state});
    return json({ok:true,idempotent:false,revision:memoryRevision(db.query("SELECT * FROM task_revisions WHERE task_id=? AND revision=?").get(taskId,revision))},201);
  })();
}
function decisionWrite(node,taskId,raw){
  const o=memoryObject(raw);memoryFields(o,["request_id","task_revision","summary","rationale","evidence"]);
  const request_id=memoryRequest(o.request_id);taskId=memoryId(taskId);
  const requestHash=payloadHash({task_id:taskId,input:o});
  return db.transaction(()=>{
    const retry=db.query("SELECT * FROM task_decisions WHERE actor=? AND request_id=?").get(node.node_id,request_id);
    if(retry){if(retry.request_hash!==requestHash)memoryFail("request_id_conflict",409);return json({ok:true,idempotent:true,decision:memoryDecision(retry)});}
    const head=db.query("SELECT * FROM task_heads WHERE task_id=?").get(taskId);if(!head)memoryFail("task_not_found",404);
    if(!Number.isInteger(o.task_revision)||o.task_revision!==head.revision)memoryFail("revision_conflict",409);
    const record={summary:memoryString(o.summary,2000),rationale:memoryString(o.rationale,4000),evidence:memoryEvidence(o.evidence||[]),authority:"author_record_only"};
    const stamp=now(),id="DEC-"+sha256(node.node_id+":"+request_id).slice(0,32);
    const recordHash=payloadHash({task_id:taskId,task_revision:head.revision,actor:node.node_id,record,created_at:stamp});
    db.query("INSERT INTO task_decisions VALUES(?,?,?,?,?,?,?,?,?)").run(id,taskId,head.revision,node.node_id,request_id,requestHash,stable(record),recordHash,stamp);
    event("task_decision",{task_id:taskId,task_revision:head.revision,decision_id:id,actor:node.node_id,record_hash:recordHash});
    return json({ok:true,idempotent:false,decision:memoryDecision(db.query("SELECT * FROM task_decisions WHERE decision_id=?").get(id))},201);
  })();
}
async function memoryRoute(req,u){
  const p=u.pathname,method=req.method.toUpperCase();
  if(method==="GET"&&p==="/tasks")return text(TASKS_HTML,200,"text/html; charset=utf-8");
  if(method==="GET"&&p==="/v1/public/tasks"){
    const limit=Number(u.searchParams.get("limit")||"50"),after=u.searchParams.get("after")||"";
    if(!Number.isInteger(limit)||limit<1||limit>100)memoryFail("invalid_limit");
    const rows=db.query("SELECT * FROM task_heads WHERE task_id>? ORDER BY task_id LIMIT ?").all(after,limit);
    return json({schema:"mos-task-memory/v1",public:true,tasks:rows.map(memoryHead),next_cursor:rows.length===limit?rows[rows.length-1].task_id:null});
  }
  const detail=p.match(/^\/v1\/public\/tasks\/([A-Za-z0-9_-]{1,100})$/);
  if(method==="GET"&&detail)return json(taskDetail(detail[1]));
  const write=p.match(/^\/(v1|web)\/tasks(?:\/([A-Za-z0-9_-]{1,100})\/(revisions|decisions))?$/);
  if(method==="POST"&&write){
    let node;
    if(write[1]==="web"){
      if(req.headers.get("origin")!==u.origin)memoryFail("origin_rejected",403);
      node=authSession(req);if(node?.node_id!=="mos-user")memoryFail("unauthorized",401);
    }else node=authNode(req);
    if(!node)memoryFail("unauthorized",401);
    const o=await body(req);
    if(write[3]==="decisions")return decisionWrite(node,write[2],o);
    return memoryWrite(node,write[2],o,!write[2]);
  }
  return null;
}

const server=Bun.serve({
  port:Number(Bun.env.PORT||"3000"),
  async fetch(req){
    try{
      const u=new URL(req.url), p=u.pathname, method=req.method.toUpperCase();
      const memoryResponse=await memoryRoute(req,u);if(memoryResponse)return memoryResponse;
                  if(method==="GET"&&p==="/v1/public/observer")return json(observerStatus());
      if(method==="POST"&&p==="/v1/observer/tick"){
        const h=Bun.env.MOS_OBSERVER_TOKEN_HASH||"";
        if(!h||!safeEq(sha256(bearer(req)),h))return json({error:"unauthorized"},401);
        const result=observeNetwork(req.headers.get("x-mos-observer-source")==="railway-cron"?"railway_cron":"manual");return json(result,result.ok?200:503);
      }

      if(method==="GET" && p==="/chat") return text(CHAT_HTML,200,"text/html; charset=utf-8");
      if(method==="GET" && p==="/web/session"){
        const n=authSession(req);return json({authenticated:n?.node_id==="mos-user",node_id:n?.node_id||null});
      }
      if(method==="POST" && ["/web/operator/login","/web/operator/messages","/web/logout"].includes(p)){
        const origin=req.headers.get("origin");
        if(!origin || new URL(origin).host!==u.host) return json({error:"origin_rejected"},403);
      }
      if(method==="POST" && p==="/web/operator/login"){
        const ip=req.headers.get("x-forwarded-for")?.split(",")[0]||"unknown";
        for(const [key,r] of loginAttempts)if(r.until<now())loginAttempts.delete(key);
        const rate=loginAttempts.get(ip)||{count:0,until:now()+300};
        if(rate.count>=10)return json({error:"too_many_attempts"},429);
        rate.count++;loginAttempts.set(ip,rate);
        const o=await body(req),n=getNode("mos-user");
        if(!n || !n.enabled || typeof o.key!=="string" || !safeEq(sha256(o.key),n.token_hash))return json({error:"invalid_access_key"},401);
        loginAttempts.delete(ip);
        db.query("DELETE FROM sessions WHERE expires_at<?").run(now());
        const raw=rand("sess_",32),exp=now()+604800;
        db.query("INSERT INTO sessions(session_hash,node_id,expires_at) VALUES(?,?,?)").run(sha256(raw),n.node_id,exp);
        event("operator_session_created",{node_id:n.node_id});
        return json({ok:true,node_id:n.node_id},200,{"set-cookie":`mos_session=${raw}; Path=/; Max-Age=604800; HttpOnly; Secure; SameSite=Strict`});
      }
      if(method==="POST" && p==="/web/logout"){
        const raw=cookieMap(req).mos_session;if(raw)db.query("DELETE FROM sessions WHERE session_hash=?").run(sha256(raw));
        return json({ok:true},200,{"set-cookie":"mos_session=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict"});
      }
      if(method==="POST" && p==="/web/operator/messages"){
        const n=authSession(req);if(n?.node_id!=="mos-user")return json({error:"unauthorized"},401);
        const o=await body(req);
        if(typeof o.text!=="string" || !o.text.trim() || o.text.length>8000 || typeof o.request_id!=="string" || !/^[a-zA-Z0-9-]{16,80}$/.test(o.request_id))return json({error:"invalid_message"},400);
        const targets=o.recipient==="all"?db.query("SELECT node_id FROM nodes WHERE enabled=1 AND node_id!='mos-user' ORDER BY node_id").all().map(x=>x.node_id):[o.recipient];
        if(!targets.length || targets.some(x=>!safeNodeId(x)||x==="mos-user"||!getNode(x)?.enabled))return json({error:"recipient_unavailable"},400);
        const payload={text:o.text.trim(),source:"operator-chat",request_id:o.request_id};
        const messages=db.transaction(()=>targets.map(recipient=>{
          const message_id="MSG-mos-user-"+o.request_id+"-"+recipient;
          const result=sendMessage(n,{schema:"mos-remote-message/v1",message_id,recipient,kind:"user_message",payload,payload_sha256:payloadHash(payload),expires_at:now()+604800});
          if(result.status!==200&&result.status!==201)throw new Error("operator_message_conflict");
          return {message_id,recipient};
        }))();
        return json({ok:true,messages},201);
      }

      if(method==="GET" && p==="/") return text(dashboard(),200,"text/html; charset=utf-8");
      if(method==="GET" && p==="/health"){ const chain=verifyChain(); return json({ok:chain.ok,service:"mos-hub",version:VERSION,stats:stats(),chain}); }
      if(method==="GET" && p==="/v1/public/nodes"){
        const rows=db.query("SELECT node_id,enabled,last_seen,last_heartbeat,heartbeat_status,capabilities_json FROM nodes ORDER BY node_id").all();
        return json({nodes:rows.map(n=>({node_id:n.node_id,enabled:!!n.enabled,online:!!n.enabled&&(n.last_seen||n.last_heartbeat||0)>=now()-TTL,last_seen:iso(n.last_seen),last_heartbeat:iso(n.last_heartbeat),heartbeat_status:n.heartbeat_status||null,capabilities:JSON.parse(n.capabilities_json||"{}")}))});
      }
      if(method==="GET" && p==="/v1/public/messages"){
        const limit=Math.max(1,Math.min(100,Number(u.searchParams.get("limit")||"50")));
        const rows=db.query("SELECT * FROM messages ORDER BY stream_seq DESC LIMIT ?").all(limit);
        return json({public:true,warning:"Messages are visible to anyone with this URL.",messages:rows.map(publicMessageRow)});
      }
      if(method==="GET" && p==="/v1/connect/challenge"){
        const nodeId=u.searchParams.get("node_id")||"", n=getNode(nodeId);
        if(!n || !n.enabled) return json({error:"node_unavailable"},404);
        db.query("DELETE FROM challenges WHERE expires_at<? OR used=1").run(now());
        const challenge_id=rand("ch_",18), nonce=rand("",32), exp=now()+300;
        db.query("INSERT INTO challenges(challenge_id,node_id,nonce,expires_at,used) VALUES(?,?,?,?,0)").run(challenge_id,nodeId,nonce,exp);
        return json({algorithm:"HMAC-SHA256(sha256(node_token), challenge_id + ':' + nonce)",challenge_id,node_id:nodeId,nonce,expires_at:iso(exp)});
      }
      if(method==="POST" && p==="/v1/connect/challenge/exchange"){
        const o=await body(req), nodeId=String(o.node_id||""), cid=String(o.challenge_id||""), proof=String(o.proof||"");
        const c=db.query("SELECT * FROM challenges WHERE challenge_id=? AND node_id=?").get(cid,nodeId), n=getNode(nodeId);
        if(!c || !n || !n.enabled || c.used || c.expires_at<now()) return json({error:"invalid_or_expired_challenge"},401);
        const want=createHmac("sha256",Buffer.from(n.token_hash,"hex")).update(cid+":"+c.nonce).digest("hex");
        if(!safeEq(want,proof)) return json({error:"invalid_proof"},401);
        db.query("UPDATE challenges SET used=1 WHERE challenge_id=?").run(cid);
        const raw=rand("sess_",32), exp=now()+3600;
        db.query("INSERT OR REPLACE INTO sessions(session_hash,node_id,expires_at) VALUES(?,?,?)").run(sha256(raw),nodeId,exp);
        event("browser_session_created",{node_id:nodeId});
        return json({ok:true,node_id:nodeId,expires_at:iso(exp)},200,{"set-cookie":`mos_session=${raw}; Path=/; Max-Age=3600; HttpOnly; Secure; SameSite=Lax`});
      }
      if(method==="POST" && p==="/v1/nodes/register"){
        if(!authAdmin(req)) return json({error:"unauthorized"},401);
        const o=await body(req), nodeId=String(o.node_id||"");
        if(!safeNodeId(nodeId)) return json({error:"invalid_node_id"},400);
        if(getNode(nodeId)) return json({error:"node_exists"},409);
        const token=rand("mos_",32), t=now();
        db.query("INSERT INTO nodes(node_id,token_hash,enabled,created_at,last_seen,last_heartbeat,heartbeat_status,capabilities_json) VALUES(?,?,1,?,?,?,?,?)").run(nodeId,sha256(token),t,null,null,null,stable(o.capabilities||{}));
        event("node_registered",{node_id:nodeId});
        return json({node_id:nodeId,token,enabled:true},201);
      }
      if(method==="GET" && p==="/v1/admin/nodes"){
        if(!authAdmin(req)) return json({error:"unauthorized"},401);
        const rows=db.query("SELECT node_id,enabled,created_at,last_seen,last_heartbeat,heartbeat_status,capabilities_json FROM nodes ORDER BY node_id").all();
        return json({nodes:rows.map(n=>({...n,enabled:!!n.enabled,created_at:iso(n.created_at),last_seen:iso(n.last_seen),last_heartbeat:iso(n.last_heartbeat),capabilities:JSON.parse(n.capabilities_json||"{}")}))});
      }
      if(method==="POST" && p==="/v1/admin/backup"){
        if(!authAdmin(req)) return json({error:"unauthorized"},401);
        const b=backup(); event("backup_created",b); return json({ok:true,...b},201);
      }
      const adminMatch=p.match(/^\/v1\/admin\/nodes\/([^/]+)\/(disable|enable|rotate-token)$/);
      if(method==="POST" && adminMatch){
        if(!authAdmin(req)) return json({error:"unauthorized"},401);
        const nodeId=decodeURIComponent(adminMatch[1]), action=adminMatch[2], n=getNode(nodeId);
        if(!n) return json({error:"node_not_found"},404);
        if(action==="disable" || action==="enable"){
          const enabled=action==="enable"?1:0; db.query("UPDATE nodes SET enabled=? WHERE node_id=?").run(enabled,nodeId);
          event("node_"+action,{node_id:nodeId}); return json({node_id:nodeId,enabled:!!enabled});
        }
        const token=rand("mos_",32); db.query("UPDATE nodes SET token_hash=? WHERE node_id=?").run(sha256(token),nodeId); db.query("DELETE FROM sessions WHERE node_id=?").run(nodeId);
        event("node_token_rotated",{node_id:nodeId}); return json({node_id:nodeId,token},200);
      }
      if(method==="POST" && p==="/v1/heartbeat"){
        const n=authNode(req); if(!n) return json({error:"unauthorized"},401);
        const o=await body(req), status=String(o.status||"ok").slice(0,200), caps=o.capabilities||{};
        db.query("UPDATE nodes SET last_seen=?,last_heartbeat=?,heartbeat_status=?,capabilities_json=? WHERE node_id=?").run(now(),now(),status,stable(caps),n.node_id);
        event("heartbeat",{node_id:n.node_id,status}); return json({ok:true,node_id:n.node_id,heartbeat_at:iso(now())});
      }
      if(method==="POST" && p==="/v1/messages"){ const n=authNode(req); if(!n) return json({error:"unauthorized"},401); return sendMessage(n,await body(req)); }
      const mb=p.match(/^\/v1\/mailbox\/([^/]+)$/);
      if(method==="GET" && mb){
        const n=authNode(req), nodeId=decodeURIComponent(mb[1]); if(!n || n.node_id!==nodeId) return json({error:"unauthorized"},401);
        const after=Math.max(0,Number(u.searchParams.get("after")||"0")), limit=Math.max(1,Math.min(100,Number(u.searchParams.get("limit")||"50")));
        return json({node_id:nodeId,messages:mailbox(nodeId,after,limit)});
      }
      const rr=p.match(/^\/v1\/messages\/([^/]+)\/receipts$/);
      if(method==="POST" && rr){ const n=authNode(req); if(!n) return json({error:"unauthorized"},401); return addReceipt(n,decodeURIComponent(rr[1]),await body(req)); }
      if(method==="POST" && p==="/web/messages"){ const n=authSession(req); if(!n) return json({error:"unauthorized"},401); return sendMessage(n,await body(req)); }
      const wmb=p.match(/^\/web\/mailbox\/([^/]+)$/);
      if(method==="GET" && wmb){
        const n=authSession(req), nodeId=decodeURIComponent(wmb[1]); if(!n || n.node_id!==nodeId) return json({error:"unauthorized"},401);
        const after=Math.max(0,Number(u.searchParams.get("after")||"0")), limit=Math.max(1,Math.min(100,Number(u.searchParams.get("limit")||"50")));
        return json({node_id:nodeId,messages:mailbox(nodeId,after,limit)});
      }
      const wrr=p.match(/^\/web\/messages\/([^/]+)\/receipts$/);
      if(method==="POST" && wrr){ const n=authSession(req); if(!n) return json({error:"unauthorized"},401); return addReceipt(n,decodeURIComponent(wrr[1]),await body(req)); }
      return json({error:"not_found"},404);
    }catch(e){
      if(e?.memoryStatus)return json({error:e.message},e.memoryStatus);
      console.error(e);
      if(String(e?.message||e)==="body_too_large") return json({error:"body_too_large"},413);
      if(e instanceof SyntaxError) return json({error:"invalid_json"},400);
      return json({error:"internal_error"},500);
    }
  }
});
console.log(`MOS Hub ${VERSION} listening on ${server.port}, db=${DB_PATH}`);



