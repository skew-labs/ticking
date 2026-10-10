/** Resumable bounded customer SSE. Keeps the prior frame through a reconnect. */
export function subscribeCustomerStream(path,{apiBase='https://api.ticking.dev',headers=()=>({}),onMessage,onState=()=>{}}={}){
 const base=new URL(apiBase);if(base.protocol!=='https:'&&!(base.protocol==='http:'&&['127.0.0.1','localhost'].includes(base.hostname)))throw Error('Use an HTTPS origin.');
 let stopped=false,controller,timer,cursor=null,failures=0;
 const stop=()=>{stopped=true;clearTimeout(timer);controller?.abort();};
 async function connect(){if(stopped)return;controller=new AbortController();let reader,idle,retry=500;
  const arm=()=>{clearTimeout(idle);idle=setTimeout(()=>controller.abort(),25000);};
  try{onState({state:'connecting',resuming:!!cursor});arm();const auth=await headers();if(stopped)return;const url=new URL(path,base);if(cursor)url.searchParams.set('after',cursor);
   const response=await fetch(url,{headers:{Accept:'text/event-stream',...auth},credentials:'omit',cache:'no-store',signal:controller.signal});
   if(!response.ok){retry=Math.max(1000,Number(response.headers.get('Retry-After')||5)*1000);if([401,403,402,422].includes(response.status)){stopped=true;onState({state:'blocked',status:response.status});}throw Error('Stream unavailable.');}
   if(!response.body||!response.headers.get('Content-Type')?.startsWith('text/event-stream'))throw Error('Invalid stream transport.');
   reader=response.body.getReader();const decoder=new TextDecoder();let buffer='';
   while(!stopped){const {value,done}=await reader.read();if(done)break;arm();buffer+=decoder.decode(value,{stream:true});if(buffer.length>1048576)throw Error('Stream frame too large.');let end;
    while((end=buffer.indexOf('\n\n'))>=0){const frame=buffer.slice(0,end);buffer=buffer.slice(end+2);const text=frame.split('\n').filter(s=>s.startsWith('data:')).map(s=>s.slice(5).trimStart()).join('\n');if(!text)continue;const event=JSON.parse(text);
     if(['reconnect','reauthorize','unavailable'].includes(event.type)){retry=500;throw Error('Refresh stream authorization.');}
     try{onMessage(event);}catch(error){cursor=null;throw error;}
     cursor=event.cursor||null;failures=0;onState({state:'streaming',cursor,publishedAt:event.publishedAt,receipt:event.receipt});
    }
   }
  }catch(error){if(!stopped)onState({state:'retrying',retained:true});}
  finally{clearTimeout(idle);controller.abort();try{await reader?.cancel();}catch{}if(!stopped)timer=setTimeout(connect,Math.max(retry,Math.min(15000,500*2**Math.min(failures++,5)))+Math.random()*250);}
 }
 connect();return stop;
}
