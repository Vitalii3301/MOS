const HUB = Bun.env.HUB_BASE || "";
const TOKEN = Bun.env.MOS_GPT_TOKEN || "";
const rawCommand = Bun.env.MOS_COMMAND_JSON || "";
let state = { ok:false, status:"idle", at:new Date().toISOString() };

async function call(path, init={}){
  const headers = new Headers(init.headers || {});
  headers.set("authorization", "Bearer " + TOKEN);
  if(init.body) headers.set("content-type","application/json");
  const r = await fetch(HUB + path, {...init, headers});
  const txt = await r.text();
  let body; try { body=JSON.parse(txt); } catch { body=txt; }
  return {http:r.status, body};
}
async function run(){
  if(!HUB || !TOKEN || !rawCommand){ state={ok:false,status:"not_configured",at:new Date().toISOString()}; return; }
  try{
    const c=JSON.parse(rawCommand);
    let result;
    if(c.action==="heartbeat"){
      result=await call("/v1/heartbeat",{method:"POST",body:JSON.stringify({status:c.status||"online",capabilities:c.capabilities||{transport:"railway"}})});
    } else if(c.action==="send"){
      result=await call("/v1/messages",{method:"POST",body:JSON.stringify(c.envelope)});
    } else if(c.action==="poll"){
      const after=Number(c.after||0), limit=Math.max(1,Math.min(100,Number(c.limit||50)));
      result=await call("/v1/mailbox/mos-gpt?after="+after+"&limit="+limit,{method:"GET"});
    } else if(c.action==="receipt"){
      result=await call("/v1/messages/"+encodeURIComponent(c.message_id)+"/receipts",{method:"POST",body:JSON.stringify({status:c.status,detail:c.detail||""})});
    } else {
      state={ok:false,status:"invalid_action",command_id:c.command_id||null,at:new Date().toISOString()}; return;
    }
    state={ok:result.http>=200&&result.http<300,status:"done",command_id:c.command_id||null,action:c.action,http:result.http,body:result.body,at:new Date().toISOString()};
  }catch(e){
    state={ok:false,status:"error",error:String(e?.message||e),at:new Date().toISOString()};
  }
  console.log("MOS_TRANSPORT_RESULT "+JSON.stringify(state));
}
await run();

Bun.serve({
  port:Number(Bun.env.PORT||"3000"),
  fetch(req){
    const p=new URL(req.url).pathname;
    if(p==="/health") return Response.json({ok:true,service:"mos-gpt-transport",configured:!!HUB&&!!TOKEN,last_ok:!!state.ok});
    if(p==="/status") return Response.json(state);
    return new Response("not found",{status:404});
  }
});