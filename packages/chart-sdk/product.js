import {createChartSDK,connectData} from './client.js';
import {validateDiscovery} from './discovery.js';
export {getSolanaCoverage} from './discovery.js';

export class SolanaResolutionError extends Error{
  constructor(state,discovery){super('Solana chart is not ready: '+state);this.name='SolanaResolutionError';this.state=state;this.discovery=discovery}
}

const address=value=>/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(value||'');
/** First-party data + chart adapter. Private mgl_ keys belong on the backend. */
export async function createSolanaDataFeed({pool,token,quoteMint,interval=1,baseURL=globalThis.location?.origin,accessToken,fetcher=fetch,onError=()=>{},onCoverage=()=>{},onStatus=()=>{},onDiscovery=()=>{},streamIdleTimeoutMs=15000,resolutionTimeoutMs=30000,signal}={}){
  if(Boolean(pool)===Boolean(token)||!address(pool||token)||(quoteMint&&!address(quoteMint)))throw Error('Select one Solana pool or token, and an optional quoteMint');
  if(![1,5,15,60,240,1440].includes(interval)||typeof accessToken!=='function')throw Error('Provide a supported interval and browser data entitlement callback');
  if(!Number.isInteger(streamIdleTimeoutMs)||streamIdleTimeoutMs<1000||streamIdleTimeoutMs>60000)throw Error('Stream idle deadline must be 1000–60000 ms');
  if(!Number.isInteger(resolutionTimeoutMs)||resolutionTimeoutMs<1000||resolutionTimeoutMs>120000)throw Error('Resolution deadline must be 1000–120000 ms');
  signal?.throwIfAborted();
  const endpoint=new URL(baseURL);
  if(endpoint.username||endpoint.password||endpoint.search||endpoint.hash||(endpoint.protocol!=='https:'&&!(endpoint.protocol==='http:'&&['localhost','127.0.0.1'].includes(endpoint.hostname))))throw Error('Use a trusted HTTPS chart data endpoint');
  const url=(path,params={})=>{const value=new URL('/market/sdk/'+path,endpoint);for(const [k,v] of Object.entries({pool,token,quoteMint,interval,...params}))if(v!==undefined&&v!==null)value.searchParams.set(k,String(v));return value};
  const headers=async()=>{const value=await accessToken();if(typeof value!=='string'||value.startsWith('mgl_')||value.length>8192||value.split('.').length!==3)throw Error('Use an origin-bound browser entitlement, never a server API key');return {Authorization:'Bearer '+value,...(globalThis.location?.origin?{'X-Slotline-Origin':globalThis.location.origin}:{})}};
  const lifecycle=new AbortController(),resolutionSignal=AbortSignal.any([lifecycle.signal,AbortSignal.timeout(resolutionTimeoutMs)]);
  const externalAbort=()=>lifecycle.abort(signal.reason);signal?.addEventListener('abort',externalAbort,{once:true});
  const abortable=(promise,requestSignal)=>new Promise((resolve,reject)=>{requestSignal.throwIfAborted();const finish=(callback,value)=>{requestSignal.removeEventListener('abort',aborted);callback(value)},aborted=()=>finish(reject,requestSignal.reason);requestSignal.addEventListener('abort',aborted,{once:true});Promise.resolve(promise).then(value=>finish(resolve,value),error=>finish(reject,error))});
  const get=async(path,params,requestSignal=lifecycle.signal)=>{const bounded=AbortSignal.any([requestSignal,AbortSignal.timeout(15000)]);bounded.throwIfAborted();const authorization=await abortable(headers(),bounded);bounded.throwIfAborted();const response=await abortable(fetcher(url(path,params),{headers:authorization,credentials:'omit',signal:bounded}),bounded);if(!response.ok){const error=Error('Chart data request failed ('+response.status+')');error.status=response.status;throw error}const value=await abortable(response.json(),bounded);bounded.throwIfAborted();return {value,status:response.status}};
  const wait=(ms)=>new Promise((resolve,reject)=>{resolutionSignal.throwIfAborted();const aborted=()=>{clearTimeout(timer);reject(resolutionSignal.reason)},timer=setTimeout(()=>{resolutionSignal.removeEventListener('abort',aborted);resolve()},ms);resolutionSignal.addEventListener('abort',aborted,{once:true})});
  let resolved,lastDiscovery,resolutionAttempt=0;
  try{
    while(!resolved){
      let response;
      try{response=await get('resolve',undefined,resolutionSignal)}catch(error){if(resolutionSignal.aborted)throw error;if(![429,503].includes(error.status))throw error;await wait(Math.min(5000,1000*2**Math.min(resolutionAttempt++,3)));continue}
      if(response.status!==202){if(response.value.ready===false)throw Error('Invalid Solana resolution response');resolved=response.value;break}
      const pending=validateDiscovery(response.value,pool||token);
      if(pending.ready!==false||pending.state==='registered')throw Error('Invalid pending Solana resolution');
      if(lastDiscovery?.state!==pending.state){try{onDiscovery(structuredClone(pending))}catch(error){try{onError(error)}catch{}}}lastDiscovery=pending;
      if(['adapter_required','quote_unavailable','capacity_limited'].includes(pending.state))throw new SolanaResolutionError(pending.state,pending);
      const retry=Number.isInteger(pending.retryAfterMs)?Math.max(250,Math.min(5000,pending.retryAfterMs)):1000;
      await wait(retry*(.9+Math.random()*.2));
    }
  }catch(error){signal?.removeEventListener('abort',externalAbort);if(resolutionSignal.aborted&&!signal?.aborted)throw new SolanaResolutionError('resolution_timeout',lastDiscovery);throw error}
  if(resolved.identity?.network!=='solana'||resolved.identity?.series!=='trade'||!address(resolved.identity.quoteMint)||(pool&&resolved.identity.pool!==pool)||(token&&resolved.identity.baseMint!==token)||(quoteMint&&resolved.identity.quoteMint!==quoteMint)){signal?.removeEventListener('abort',externalAbort);throw Error('Resolved Solana identity mismatch')}
  if(lifecycle.signal.aborted){signal?.removeEventListener('abort',externalAbort);lifecycle.signal.throwIfAborted()}
  // Default quote selection is pinned once, even when new pools are discovered.
  quoteMint=resolved.identity.quoteMint;
  const listeners=new Set(),tradeListeners=new Set(),seenTimes=new Set(),abandoned=new Set();
  const tape=new TradeTapeModel(200);let tapeReady=false;
  let closed=false,controller=null,timer=null,renewTimer=null,renewRequest=null,attempt=0,epoch=null,offset=0,maximum=0,cursor=0,streamGeneration=0,lastStatus=null;
  const stamp=page=>{
    if(!page||!Number.isSafeInteger(page.sequence)||page.sequence<0||typeof page.epoch!=='string'||!Array.isArray(page.candles))throw Error('Invalid executed-data snapshot');
    if(page.identity&&['network','baseMint','quoteMint','series','pool'].some(k=>page.identity[k]!==resolved.identity[k]))throw Error('Executed-data identity changed');
    if(abandoned.has(page.epoch))throw Error('A previous engine epoch cannot replace recovered data');
    if(epoch!==page.epoch){if(epoch){abandoned.add(epoch);offset=maximum+1;if(abandoned.size>8)abandoned.delete(abandoned.values().next().value)}epoch=page.epoch;cursor=0}
    const sequence=offset+page.sequence;if(!Number.isSafeInteger(sequence))throw Error('Data cursor exceeded the SDK range');maximum=Math.max(maximum,sequence);cursor=Math.max(cursor,page.sequence);return {...page,sequence};
  };
  const pageWithCorrections=(raw,before)=>{
    const page=stamp(raw),times=new Set(page.candles.map(c=>c.time));let deleted=[];
    delivery(page.delivery);
    if(page.historyReset&&page.sequence>=maximum&&!before)seenTimes.clear();
    if(page.coverage&&page.sequence>=maximum){resolved.coverage=page.coverage;try{onCoverage(page.coverage)}catch(error){report(error)}}
    if(!before&&page.sequence>=maximum){const first=page.candles[0]?.time??0;deleted=[...seenTimes].filter(t=>(!first||t>=first)&&!times.has(t));for(const t of deleted)seenTimes.delete(t)}
    for(const t of times)seenTimes.add(t);if(seenTimes.size>6000)for(const t of [...seenTimes].sort((a,b)=>a-b).slice(0,seenTimes.size-6000))seenTimes.delete(t);
    return {...page,deleted};
  };
  const report=error=>{try{onError(error instanceof Error?error:Error(String(error)))}catch{}};
  const delivery=(value,heartbeat=false)=>{
    if(!value)return;
    if(!['live','recovering','stale','unavailable','unverified_source'].includes(value.state)||value.live!==(value.state==='live'))throw Error('Invalid execution delivery state');
    resolved.delivery={...value};
    if(heartbeat||value.state!==lastStatus){lastStatus=value.state;try{onStatus({...value})}catch(error){report(error)}}
  };
  delivery(resolved.delivery);
  const deliver=(set,value)=>{for(const listener of set)try{listener(value)}catch(error){report(error)}};
  const deliverTrades=value=>{if(Array.isArray(value.trades)&&tape.apply(value)){tapeReady=true;deliver(tradeListeners,value)}};
  const handle=(name,value)=>{
    if(name==='access')throw Error(value.error||'Chart data entitlement expired');
    if(name==='status'){if(value.delivery)delivery(value.delivery,true);else report(Error(value.message||'Execution stream delayed'));return}
    if(name==='snapshot'||name==='candles'){
      const page=pageWithCorrections(value);deliver(listeners,{candles:page.candles,deleted:page.deleted,sequence:page.sequence,recovery:name==='snapshot',historyReset:page.historyReset===true});
      if(name==='snapshot')deliverTrades({trades:value.trades,deleted:[],snapshot:true,sequence:page.sequence});
    }else if(name==='trades'){
      if(value.epoch!==epoch)throw Error('Recover a snapshot before a new execution epoch');
      if(!Array.isArray(value.trades)||!Array.isArray(value.deleted)||!Number.isSafeInteger(value.sequence))throw Error('Invalid execution tape update');
      if(value.sequence<cursor)return;delivery(value.delivery);cursor=value.sequence;maximum=Math.max(maximum,offset+cursor);deliverTrades({...value,sequence:offset+cursor});
    }
  };
  const active=()=>!closed&&!lifecycle.signal.aborted&&(listeners.size||tradeListeners.size)&&!globalThis.document?.hidden;
  const stop=()=>{streamGeneration++;controller?.abort();controller=null;renewRequest?.abort();renewRequest=null;clearTimeout(timer);clearTimeout(renewTimer);timer=renewTimer=null};
  const renewSession=(session,generation,abort)=>{
    if(typeof session?.id!=='string'||!/^[a-f0-9]{32}$/.test(session.id)||!Number.isSafeInteger(session.expiresAt)||session.expiresAt<=Date.now()/1000||session.expiresAt>Date.now()/1000+3660)throw Error('Invalid stream session');
    clearTimeout(renewTimer);
    const remaining=session.expiresAt*1000-Date.now();
    renewTimer=setTimeout(async()=>{
      if(!active()||generation!==streamGeneration||abort.signal.aborted)return;
      const request=new AbortController();renewRequest=request;let deadline;
      try{
        const auth=await Promise.race([headers(),new Promise((_,reject)=>{deadline=setTimeout(()=>reject(Error('Stream entitlement deadline exceeded')),5000)})]);clearTimeout(deadline);
        if(!active()||generation!==streamGeneration||abort.signal.aborted)return;
        const response=await fetcher(new URL('/market/sdk/stream/renew',endpoint),{method:'POST',headers:{...auth,'X-Slotline-Stream':session.id},credentials:'omit',signal:AbortSignal.any([request.signal,abort.signal,lifecycle.signal,AbortSignal.timeout(5000)])});
        if(!response.ok){if([401,402,403].includes(response.status)){abort.abort();throw Error('Stream entitlement was rejected')}throw Error('Stream entitlement renewal delayed ('+response.status+')')}
        const renewed=await response.json();if(renewed?.renewed!==true)throw Error('Invalid stream renewal response');
        if(generation===streamGeneration&&!abort.signal.aborted)renewSession({...session,expiresAt:renewed.expiresAt},generation,abort);
      }catch(error){
        if(active()&&generation===streamGeneration&&!lifecycle.signal.aborted){report(error);if(!abort.signal.aborted&&session.expiresAt>Date.now()/1000+1)renewTimer=setTimeout(()=>renewSession(session,generation,abort),1000)}
      }finally{clearTimeout(deadline);if(renewRequest===request)renewRequest=null}
    },Math.min(30000,Math.max(1000,remaining-10000)));
  };
  const open=async()=>{
    if(!active()||controller)return;const generation=++streamGeneration,abort=new AbortController();controller=abort;
    try{
      const response=await fetcher(url('stream',{limit:300}),{headers:await headers(),credentials:'omit',signal:AbortSignal.any([abort.signal,lifecycle.signal])});
      if(!response.ok||!response.body)throw Error('Execution stream unavailable ('+response.status+')');attempt=0;
      const reader=response.body.getReader(),decoder=new TextDecoder();let pending='';
      const read=async()=>{let idle;try{return await Promise.race([reader.read(),new Promise((_,reject)=>{idle=setTimeout(()=>reject(Error('Execution stream idle deadline exceeded')),streamIdleTimeoutMs)})])}finally{clearTimeout(idle)}};
      try{while(active()&&generation===streamGeneration){const {done,value}=await read();if(done)throw Error('Execution stream closed before recovery');pending+=decoder.decode(value,{stream:true});if(pending.length>2_000_000)throw Error('Execution stream frame exceeds the bounded SDK buffer');let end;
        while((end=pending.indexOf('\n\n'))>=0){const frame=pending.slice(0,end);pending=pending.slice(end+2);let name='message';const data=[];for(const line of frame.split('\n')){if(line.startsWith('event:'))name=line.slice(6).trim();if(line.startsWith('data:'))data.push(line.slice(5).trimStart())}if(data.length){const value=JSON.parse(data.join('\n'));if(name==='session')renewSession(value,generation,abort);else handle(name,value)}}
      }}finally{await reader.cancel().catch(()=>{});reader.releaseLock()}
    }catch(error){if(!abort.signal.aborted&&generation===streamGeneration){delivery({state:'unavailable',live:false,inputAgeMs:null,freshnessLimitMs:5000,recoveryRequired:true,executionToSDKLatencyMs:null},true);report(error)}}
    finally{if(generation===streamGeneration){controller=null;clearTimeout(renewTimer);renewRequest?.abort();renewRequest=null;if(active())timer=setTimeout(open,Math.min(30000,1000*2**Math.min(attempt++,5))*(.8+Math.random()*.4))}}
  };
  const visibility=()=>{stop();if(active())open()};globalThis.document?.addEventListener('visibilitychange',visibility);
  const destroy=()=>{if(closed)return;closed=true;stop();lifecycle.abort();listeners.clear();tradeListeners.clear();seenTimes.clear();signal?.removeEventListener('abort',externalAbort);lifecycle.signal.removeEventListener('abort',destroy);globalThis.document?.removeEventListener('visibilitychange',visibility)};
  lifecycle.signal.addEventListener('abort',destroy,{once:true});
  return {identity:resolved.identity,selection:resolved,async history(before,limit=300){if(closed)throw Error('Data feed closed');if(!Number.isInteger(limit)||limit<1||limit>600||(before!==undefined&&(!Number.isSafeInteger(before)||before<=0)))throw Error('Invalid bounded history page');return pageWithCorrections((await get('candles',{before,limit})).value,before)},
    subscribe(callback){if(closed)throw Error('Data feed closed');listeners.add(callback);open();return ()=>{listeners.delete(callback);if(!listeners.size&&!tradeListeners.size)stop()}},
    subscribeTrades(callback){if(closed)throw Error('Data feed closed');tradeListeners.add(callback);if(tapeReady)deliver(new Set([callback]),{trades:tape.values(),deleted:[],snapshot:true,sequence:tape.sequence});open();return ()=>{tradeListeners.delete(callback);if(!listeners.size&&!tradeListeners.size)stop()}},
    destroy
  };
}

export class TradeTapeModel{
  constructor(limit=100){if(!Number.isInteger(limit)||limit<1||limit>200)throw Error('Trade tape limit must be 1–200');this.limit=limit;this.rows=new Map();this.sequence=-1}
  apply(update){
    if(!Number.isSafeInteger(update.sequence)||update.sequence<this.sequence)return false;
    if(!Array.isArray(update.trades)||update.trades.length>2048||!Array.isArray(update.deleted)||update.deleted.length>4096||update.deleted.some(id=>typeof id!=='string'||id.length>120))throw Error('Invalid execution tape');
    for(const row of update.trades)if(typeof row.id!=='string'||row.id.length>120||!address(row.pool)||![row.price,row.baseVolume,row.quoteVolume].every(v=>Number.isFinite(v)&&v>0)||![row.slot,row.time,row.transactionIndex,row.eventIndex].every(Number.isSafeInteger)||row.slot<=0||row.time<=0||row.transactionIndex<0||row.eventIndex<0||row.executionStatus!=='executed')throw Error('Only verified normalized executions enter the tape');
    if(update.snapshot)this.rows.clear();for(const row of update.trades)this.rows.set(row.id,{...row});for(const id of update.deleted)this.rows.delete(id);this.sequence=update.sequence;
    const ordered=this.values();for(const row of ordered.slice(this.limit))this.rows.delete(row.id);return true;
  }
  values(){const descending=(a,b)=>a<b?1:a>b?-1:0;return [...this.rows.values()].sort((a,b)=>b.slot-a.slot||b.transactionIndex-a.transactionIndex||b.eventIndex-a.eventIndex||descending(a.pool,b.pool)||descending(a.id,b.id))}
}

/** Data/engine convenience only. The consuming application owns every UI. */
export async function createSolanaChart(container,{licenseProvider,publicKey,onTrades,chart={},...options}={}){
  options.signal?.throwIfAborted();const attach=new AbortController();
  const deadline=options.resolutionTimeoutMs??30000;
  if(!Number.isInteger(deadline)||deadline<1000||deadline>120000)throw Error('Resolution deadline must be 1000–120000 ms');
  let sdk,feed,engine,stopData=()=>{},stopTrades=()=>{},closed=false,timer;
  const destroy=()=>{if(closed)return;closed=true;clearTimeout(timer);options.signal?.removeEventListener('abort',destroy);attach.abort();stopTrades();stopData();feed?.destroy();sdk?.destroy()};
  options.signal?.addEventListener('abort',destroy,{once:true});
  timer=setTimeout(()=>{attach.abort(new SolanaResolutionError('resolution_timeout'));destroy()},deadline);
  try{
    const pendingSDK=createChartSDK({licenseProvider,publicKey,onLicenseError:error=>{
      try{options.onError?.(error)}catch{}
      if(!error.retryable||!sdk||sdk.entitlement.exp<=Date.now()/1000)destroy();
    }}).then(value=>{if(closed){value.destroy();throw attach.signal.reason}return value});
    sdk=await new Promise((resolve,reject)=>{
      const canceled=()=>{attach.signal.removeEventListener('abort',canceled);reject(attach.signal.reason)};
      attach.signal.addEventListener('abort',canceled,{once:true});
      pendingSDK.then(value=>{attach.signal.removeEventListener('abort',canceled);resolve(value)},error=>{attach.signal.removeEventListener('abort',canceled);reject(error)});
    });
    feed=await createSolanaDataFeed({...options,signal:options.signal?AbortSignal.any([options.signal,attach.signal]):attach.signal,accessToken:()=>sdk.dataToken()});
    engine=sdk.createChart(container,{...chart,interval:options.interval||1,quote:feed.identity.quoteSymbol,volumeUnit:feed.identity.quoteSymbol});
    stopData=await connectData(engine,feed);
    if(onTrades)stopTrades=feed.subscribeTrades(onTrades);engine.on('destroy',destroy);clearTimeout(timer);
    return {chart:engine,feed,identity:feed.identity,get coverage(){return feed.selection.coverage},destroy};
  }catch(error){destroy();throw error}
}
