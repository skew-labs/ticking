/** Bounded SSE over fetch: credentials remain headers, never URL parameters. */
export function streamOrigin(apiBase=location.origin){
 const url=new URL(apiBase);
 return ['ticking.dev','www.ticking.dev'].includes(url.hostname)?'https://api.ticking.dev':url.origin;
}
export function applyStreamMessage(previous,message){
 if(!Number.isSafeInteger(message.seq)||message.seq<1)throw Error('Invalid stream sequence.');
 if(message.type==='snapshot')return {seq:message.seq,data:message.data};
 if(message.type!=='delta'||!previous||message.base!==previous.seq||message.seq!==previous.seq+1)throw Error('Stream base changed; reconnecting.');
 if(!Array.isArray(message.times)||message.times.length>600||!Array.isArray(message.candles)||message.candles.length>600)throw Error('Stream window exceeded.');
 const rows=new Map(previous.data.candles.map(b=>[b.time,b]));
 for(const bar of message.candles)rows.set(bar.time,bar);
 const candles=message.times.map(t=>rows.get(t));
 if(candles.some(b=>!b)||new Set(message.times).size!==message.times.length)throw Error('Incomplete stream window.');
 return {seq:message.seq,data:{...message.meta,candles}};
}
export function subscribeStream(path,{apiBase=location.origin,headers=()=>({}),onMessage,onState=()=>{}}={}){
 let disposed=false,controller=null,timer=0,failures=0;
 const stop=()=>{disposed=true;clearTimeout(timer);controller?.abort();};
 async function connect(){
  if(disposed)return;
  controller=new AbortController();let idle=0,retry=0,reader=null;
  const arm=ms=>{clearTimeout(idle);idle=setTimeout(()=>controller?.abort(),ms);};
  try{
   onState({state:'connecting'});arm(15000);
   const auth=await headers();if(disposed)return;
   const response=await fetch(new URL(path,streamOrigin(apiBase)),{headers:{Accept:'text/event-stream',...auth},signal:controller.signal,credentials:'omit',cache:'no-store'});
   if(!response.ok){const n=response.headers.get('Retry-After');retry=/^\d+$/.test(n||'')?Number(n)*1000:0;throw Error('Live connection unavailable ('+response.status+').');}
   if(!response.headers.get('Content-Type')?.startsWith('text/event-stream')||!response.body)throw Error('Live transport is unavailable.');
   reader=response.body.getReader();const decoder=new TextDecoder();let buffer='';arm(25000);
   while(!disposed){
    const {done,value}=await reader.read();if(done)break;arm(25000);buffer+=decoder.decode(value,{stream:true});
    if(buffer.length>2*1024*1024)throw Error('Live message exceeds the memory bound.');
    let end;while((end=buffer.indexOf('\n\n'))>=0){
     const frame=buffer.slice(0,end);buffer=buffer.slice(end+2);
     const payload=frame.split('\n').filter(s=>s.startsWith('data:')).map(s=>s.slice(5).trimStart()).join('\n');if(!payload)continue;
     const message=JSON.parse(payload);
     if(['reconnect','reauthorize','unavailable'].includes(message.type)){retry=message.retryMs||250;throw Error('Refreshing live connection.');}
     onMessage(message);failures=0;onState({state:'streaming',publishedAt:message.publishedAt});
    }
   }
  }catch(error){if(!disposed)onState({state:'retrying',error:'Reconnecting; last data retained.'});}
  finally{
   clearTimeout(idle);controller?.abort();try{await reader?.cancel();}catch{}
   if(!disposed){const delay=Math.max(retry,Math.min(15000,500*2**Math.min(failures++,5)))+Math.floor(Math.random()*250);timer=setTimeout(connect,delay);}
  }
 }
 connect();return stop;
}
