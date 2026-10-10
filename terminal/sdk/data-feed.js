/** Real retained Solana executions. Server keys never enter this browser SDK. */
import {subscribeStream,applyStreamMessage} from './stream.js';
import {subscribeCustomerStream} from './customer-stream.js';
import {parseInterval,intervalStart} from '../src/intervals.js';
// Preserve this venue's Thursday-based API weeks; do not shift native OHLC.
function dataIntervalStart(time,period,identity){return identity?.venue==='hyperliquid_public'&&identity?.historyPolicy==='hyperliquid_retained_v1'?Math.floor(time/period)*period:intervalStart(time,period);}
const candleFields=['time','open','high','low','close','volume','baseVolume','count','synthetic','estimated','referenceOnly','estimateMethod','sourceTime','quoteCurrency','mixedSource','receipt','conversionReceipt','sourceMarket','sourceBaseVolume','sourceQuoteVolume'];
const candleArrays=['sources','quoteCurrencies','referenceQualities','anchors'];
const qualityFields=['basis','observedMinutes','referenceMinutes','estimatedMinutes','carriedMinutes'];
function sameCandle(a,b){
 if(a===b)return true;
 if(!b||!candleFields.every(k=>a[k]===b[k])||!qualityFields.every(k=>a.quality?.[k]===b.quality?.[k]))return false;
 for(const k of candleArrays){const x=a[k]||[],y=b[k]||[];if(x===y)continue;if(x.length!==y.length)return false;for(let i=0;i<x.length;i++)if(x[i]!==y[i]&&(!(x[i]&&y[i]&&typeof x[i]==='object'&&typeof y[i]==='object')||JSON.stringify(x[i])!==JSON.stringify(y[i])))return false;}
 return true;
}
function validateQuality(bar,period){
 const q=bar.quality;if(q==null)return;
 const fields=['observedMinutes','referenceMinutes','estimatedMinutes','carriedMinutes'];
 if(!fields.every(k=>Number.isSafeInteger(q[k])&&q[k]>=0)||fields.reduce((n,k)=>n+q[k],0)<1||fields.reduce((n,k)=>n+q[k],0)>period/60000)throw Error('Invalid candle provenance counts.');
 const present=fields.filter(k=>q[k]>0),basis=present.length>1?'mixed':{observedMinutes:'observed',referenceMinutes:'reference',estimatedMinutes:'estimated',carriedMinutes:'carried'}[present[0]];
 if(q.basis!==basis||Boolean(bar.estimated)!==Boolean(q.estimatedMinutes)||Boolean(bar.referenceOnly)!==(q.observedMinutes===0))throw Error('Inconsistent candle provenance.');
 if(q.observedMinutes===0&&(bar.count!==0||bar.volume!==0||bar.baseVolume!==0||Number(bar.quoteVolume||0)!==0))throw Error('Reference candles cannot claim execution volume.');
}
export function createDataFeed({mint,pool,market='SOL',aggregation='pool',history='full',tokenProvider,apiBase='https://ticking.dev',onStatus=()=>{}}={}){
 if(!['pool','token','exchange'].includes(aggregation)||pool&&aggregation!=='pool'||aggregation==='exchange'&&mint)throw Error('Use an explicit exchange market, token reference or pool.');
 const url=new URL(apiBase);if(url.username||url.password||url.search||url.hash||url.protocol!=='https:'&&!(url.protocol==='http:'&&['localhost','127.0.0.1'].includes(url.hostname)))throw Error('Use an HTTPS API origin.');
 let disposed=false,grant=null,grantPending=null,pinned=null,referenceIdentity=null,retryUntil=0;const active=new Set();
 function checkGrant(){
  if(disposed)throw new DOMException('Disposed','AbortError');
  if(!Number.isFinite(grant?.expires)||grant.expires*1000<=Date.now())throw Error('The display token has expired.');
  if(!grant?.token?.startsWith('td_'))throw Error('Provide a short-lived display token from your backend. Keep the server API key on your server.');
  if(grant.aggregation&&grant.aggregation!==aggregation)throw Error('Display token aggregation changed.');
 }
 async function authorize(){
  if(!tokenProvider)return;
  if(grant?.expires*1000>Date.now()+15000){checkGrant();return;}
  if(grantPending)return grantPending;
  grantPending=(async()=>{
   const timeout=new AbortController();active.add(timeout);let expiry;
   try{grant=await Promise.race([tokenProvider({mint,...(aggregation==='exchange'?{market}:{}),pool:pinned||pool,aggregation,origin:location.origin,signal:timeout.signal}),new Promise((_,reject)=>{expiry=setTimeout(()=>{timeout.abort();reject(Error('Display token request timed out.'));},12000);timeout.signal.addEventListener('abort',()=>reject(new DOMException('Disposed','AbortError')),{once:true});})]);}
   finally{clearTimeout(expiry);active.delete(timeout);}
   if(disposed)throw new DOMException('Disposed','AbortError');
   if(typeof grant==='string')grant={token:grant,expires:Date.now()/1000+60};
   checkGrant();
  })().finally(()=>{grantPending=null;});
  return grantPending;
 }
 async function request(kind,query={}){
  if(disposed)throw new DOMException('Disposed','AbortError');
  if(Date.now()<retryUntil){const error=Error('Chart requests are cooling down. The last frame is retained.');error.retryAfterMs=retryUntil-Date.now();throw error;}
  let endpoint,headers={};
  if(tokenProvider){
   await authorize();
   endpoint='/api/v1/display/'+kind;headers.Authorization='Bearer '+grant.token;
  }else endpoint='/api/public/engine/'+kind;
  const controller=new AbortController();active.add(controller);const timer=setTimeout(()=>controller.abort(),12000);
  try{
   const q=new URLSearchParams(query);
   if(!tokenProvider){q.set('aggregation',aggregation);if(pinned)q.set('pool',pinned);if(mint)q.set('mint',mint);else if(!pool)q.set('market',market);if(pool)q.set('pool',pool);}
   const r=await fetch(new URL(endpoint+'?'+q,url),{headers,signal:controller.signal,credentials:'omit',cache:'no-store'});
   if(!r.ok){let d;try{d=await r.json();}catch{}const error=Error(typeof d?.detail==='string'?d.detail:d?.detail?.message||`Chart request failed (${r.status}).`),value=r.headers.get('Retry-After');let delay=value==null?(r.status===429?60000:0):/^\d+$/.test(value)?Number(value)*1000:Date.parse(value)-Date.now();if(Number.isFinite(delay)&&delay>0){error.retryAfterMs=Math.min(2147483647,Math.max(1000,delay));retryUntil=Date.now()+error.retryAfterMs;}throw error;}
   const bytes=await r.text();if(bytes.length>1048576)throw Error('Chart page exceeds the memory bound.');const data=JSON.parse(bytes);
   if(disposed)throw new DOMException('Disposed','AbortError');
   return data;
  }finally{clearTimeout(timer);active.delete(controller);}
 }
 function validate(d,interval){
   const exchange=aggregation==='exchange';
   if(!(exchange?d.mode==='exchange_history'&&d.identity?.network==='exchange'&&d.aggregation==='exchange'&&(d.market===market||d.identity.market===market):['executed_history','asset_history'].includes(d.mode)&&d.identity?.network==='solana')||!Array.isArray(d.candles)||d.candles.length>600)throw Error('Invalid execution history response.');
   if(aggregation==='token'&&(d.aggregation!=='token'||d.pool!==null||d.identity.series!=='reference'||d.identity.baseMint!==d.mint||mint&&d.mint!==mint))throw Error('Token reference identity changed.');
   // A deployed reference policy can change while a tab remains open. Rebase
   // the complete series after validation; never merge prices across identities.
   const composite=exchange&&d.identity.series==='reference';
   if(composite&&(d.identity.venue!=='ticking'||d.identity.asset!==d.market||d.identity.historyPolicy!=='median_venue_ohlc_v1'))throw Error('Unrecognized composite reference identity.');
   const nextReference=exchange?[d.identity.venue+':'+d.identity.market,d.identity.quoteMint,d.identity.aggregation,d.identity.historyPolicy]:aggregation==='token'?[d.identity.baseMint,d.identity.quoteMint,d.identity.aggregation,d.identity.historyPolicy]:null;
   const migration=composite&&referenceIdentity&&['binance_spot:','binance_usdm:'].some(prefix=>referenceIdentity[0]===prefix+d.identity.market);
   if(referenceIdentity&&nextReference[0]!==referenceIdentity[0]&&!migration)throw Error('Token reference mint changed.');
   if(pinned&&d.pool!==pinned)throw Error('Chart pool identity changed.');pinned=d.pool;
   const period=parseInterval(d.interval).milliseconds;if(period!==parseInterval(interval).milliseconds)throw Error('Response interval changed.');let last=0;for(const b of d.candles){if(!['time','open','high','low','close','volume','baseVolume','count'].every(k=>Number.isFinite(b[k]))||b.time<=last||dataIntervalStart(b.time,period,d.identity)!==b.time||b.low<=0||b.high<Math.max(b.open,b.close)||b.low>Math.min(b.open,b.close)||b.volume<0||b.baseVolume<0||b.count<0||d.fillPolicy==='carry'&&last&&b.time-last!==period)throw Error('Invalid candle sequence.');if(b.synthetic&&(b.gapKind!=='unverified_no_record'||b.count!==0||b.volume!==0||b.baseVolume!==0||b.open!==b.high||b.open!==b.low||b.open!==b.close||!Number.isFinite(b.sourceTime)||b.sourceTime>=b.time))throw Error('Invalid carried close.');last=b.time;}
   for(const bar of d.candles)validateQuality(bar,period);
   if(nextReference){d.identityReset=!!referenceIdentity&&JSON.stringify(referenceIdentity)!==JSON.stringify(nextReference);referenceIdentity=nextReference;}
   onStatus(d);return d;
 }
 const subscriptions=new Set();
 return {
  adopt(initial,interval){
   if(disposed)throw new DOMException('Disposed','AbortError');
   if(!initial||initial.history!==history)throw Error('Initial chart history changed.');
   return validate(initial.data,interval);
  },
  subscribe(interval,onData,onState=()=>{}){
   let current=null;const query=new URLSearchParams({interval,history});
   if(!tokenProvider){query.set('aggregation',aggregation);if(mint)query.set('mint',mint);else if(!pool)query.set('market',market);if(pool)query.set('pool',pool);}
   const stop=(tokenProvider?subscribeCustomerStream:subscribeStream)((tokenProvider?'/api/v1/display/stream':'/api/public/stream/candles')+'?'+query,{apiBase,headers:async()=>{
    if(tokenProvider)await authorize();
    return tokenProvider?{Authorization:'Bearer '+grant.token}:{};
   },onMessage:message=>{current=applyStreamMessage(current,message);onData({...validate(current.data,interval),transport:'stream',publishedAt:message.publishedAt});},onState:state=>{if(state.state==='connecting'&&!tokenProvider)current=null;onState(state);}});
   subscriptions.add(stop);return ()=>{subscriptions.delete(stop);stop();};
  },
  async history(interval='1m',before=null){const d=await request('candles',{interval,gap_policy:'carry',limit:'600',history:aggregation==='token'?history:'executions',...(before?{before:String(before)}:{})});
   return validate(d,interval);
  },
  trades(){return request('trades',{limit:'50'});},
  cancelPending(){for(const c of active)c.abort();active.clear();},
  destroy(){disposed=true;for(const stop of subscriptions)stop();subscriptions.clear();for(const c of active)c.abort();active.clear();grant=null;},
 };
}

/** Bounded paged windows. A failed read never erases the last good frame. */
export function bindDataFeed(chart,options={}){
 let active=true,timer=0,epoch=0,paging=false,stopStream=null,streamInterval=null,streamVersion=0,lastGood=chart.engineData||null,lastPageKey='',retryUntil=0;
 const pages=new Map(),feed=options.feed||createDataFeed({...options,onStatus:()=>{}}),root=chart.container.closest('.chart-card,.frame')||chart.container.parentElement,status=options.onStatus||(()=>{}),MAX=2400;
 const remember=data=>{pages.delete(data.interval);pages.set(data.interval,data);while(pages.size>8)pages.delete(pages.keys().next().value);};
 function apply(data,previous,preserve=true){
  if(data.identityReset){pages.clear();lastPageKey='';previous=null;preserve=false;}
  let bars=data.candles;
  const same=previous?.pool===data.pool&&previous.interval===data.interval&&previous.identity?.network===data.identity.network&&previous.identity?.venue===data.identity.venue&&previous.identity?.market===data.identity.market&&previous.identity?.baseMint===data.identity.baseMint&&previous.identity?.quoteMint===data.identity.quoteMint&&previous.identity?.aggregation===data.identity.aggregation&&previous.identity?.historyPolicy===data.identity.historyPolicy;
  if(same&&bars.length&&previous.candles.length){const first=bars[0].time,last=bars.at(-1).time,period=parseInterval(data.interval).milliseconds;
   // The next real page can lie across a long unavailable interval. Preserve
   // both sides of that gap instead of replacing the current viewport.
   bars=[...previous.candles.filter(b=>b.time<first),...bars,...previous.candles.filter(b=>b.time>last)];}
  const anchor=preserve&&same&&chart.offset>0?chart.visible?.at(-1)?.time:null;
  if(bars.length>MAX){const index=anchor==null?bars.length-1:bars.findIndex(b=>b.time>=anchor),start=Math.max(0,Math.min(bars.length-MAX,index-Math.floor(MAX*.75)));bars=bars.slice(start,start+MAX);}
  const result={...data,candles:bars,nextBefore:bars[0]?.time??null,hasMore:same&&bars[0]?.time===previous.candles[0]?.time?previous.hasMore:data.hasMore,carriedBars:bars.filter(b=>b.synthetic).length};
  // Delta transport preserves untouched bar objects. Compare only changed
  // rows; independently decoded snapshots still use every provenance field.
  const unchanged=same&&chart.engineData?.interval===data.interval&&chart.options.series?.length===bars.length&&bars.every((bar,i)=>sameCandle(bar,chart.options.series[i]));
  lastGood=result;remember(result);chart.engineData=result;if(!unchanged)chart.setSeries(bars,{preserve:preserve&&same});const identity=data.identity.network==='exchange'?`exchange:${data.identity.venue}:${data.identity.market}`:data.aggregation==='token'?`solana:token:${data.identity.baseMint}:${data.identity.quoteMint}:${data.identity.aggregation}`:`solana:${data.pool}:${data.identity.baseMint}:${data.identity.quoteMint}`;chart.token={...chart.token,market:identity,symbol:data.symbol||chart.token.symbol};chart.drawingLayer?.setMarket(identity);status(result);
 }
 function connectStream(){
  if(!feed.subscribe||!active||document.hidden)return;
  if(stopStream&&streamInterval===chart.options.interval)return;
  stopStream?.();streamInterval=chart.options.interval;const selected=streamInterval;
  stopStream=feed.subscribe(selected,data=>{
   if(!active||document.hidden||chart.options.interval!==selected||paging||chart.offset>30)return;
   if(!data.candles.length&&lastGood?.candles.length)return;
   streamVersion++;apply(data,pages.get(selected),true);options.onUpdate?.(data);
  },state=>{options.onConnection?.(state);});
 }
 async function refresh(){
  const run=++epoch,seenVersion=streamVersion;clearTimeout(timer);feed.cancelPending();paging=false;
  if(Date.now()<retryUntil){timer=setTimeout(refresh,retryUntil-Date.now());return;}
  const interval=chart.options.interval,old=pages.get(interval),period=chart.view?.period;
  const before=old&&chart.offset>30&&chart.engineData?.interval===interval?chart.visible?.at(-1)?.time+period:null;
  try{const data=await feed.history(interval,before);if(!active||run!==epoch)return;
   if(!data.candles.length&&lastGood?.candles.length){status({error:'No records returned; retaining the previous frame.',retained:true,displayedInterval:lastGood.interval});return;}
   if(streamVersion===seenVersion)apply(data,pages.get(interval)||old,!!old);connectStream();
  }catch(e){if(active&&run===epoch&&e.name!=='AbortError'){if(e.retryAfterMs)retryUntil=Date.now()+e.retryAfterMs;if(!lastGood)chart.set({series:[],emptyMessage:e.message});status({error:e.message,retained:!!lastGood,displayedInterval:lastGood?.interval});}}
  finally{if(active&&run===epoch){const delay=feed.subscribe?30000:(options.refreshMs??(chart.offset>30?30000:((lastGood?.refresh_seconds*1000)||30000)));timer=setTimeout(()=>{if(!document.hidden)refresh();},Math.max(retryUntil-Date.now(),1000,Math.min(60000,delay)));}}
 }
 async function pageHistory(event){
  const current=pages.get(chart.options.interval);if(!current||paging||chart.engineData!==current||Date.now()<retryUntil)return;
  const {begin,end,total}=event.detail,older=begin<40&&current.hasMore,newer=total-end<40&&current.candles.at(-1)?.time<dataIntervalStart((current.updated_at||0)*1000,chart.view.period,current.identity);
  if(!older&&!newer)return;
  const before=older?current.candles[0].time:current.candles.at(-1).time+chart.view.period*601,key=current.interval+':'+before;
  if(lastPageKey===key)return;lastPageKey=key;paging=true;const run=epoch;
  status({...current,loadingHistory:true});
  try{const data=await feed.history(current.interval,before);if(!active||run!==epoch)return;
   if(!data.candles.length){if(older){current.hasMore=false;remember(current);}status(current);return;}
   apply(data,current,true);
  }catch(e){if(active&&run===epoch&&e.name!=='AbortError'){lastPageKey='';if(e.retryAfterMs)retryUntil=Date.now()+e.retryAfterMs;status({error:e.message,retained:true,displayedInterval:current.interval});}}
  finally{if(run===epoch){paging=false;chart.requestDraw();}}
 }
 const visibility=()=>{if(document.hidden){clearTimeout(timer);stopStream?.();stopStream=null;}else refresh();};
 const interval=()=>{stopStream?.();stopStream=null;lastPageKey='';const saved=pages.get(chart.options.interval);if(saved){lastGood=saved;chart.engineData=saved;chart.setSeries(saved.candles,{preserve:false});status(saved);}else status({loading:true,displayedInterval:lastGood?.interval});refresh();};
 const latest=()=>{lastPageKey='';refresh();};
 chart.container.addEventListener('latestrequest',latest);
 root.addEventListener('intervalchange',interval);chart.container.addEventListener('viewchange',pageHistory);document.addEventListener('visibilitychange',visibility);
 let seeded=false;
 if(options.initialData&&feed.adopt){
  try{const data=feed.adopt(options.initialData,chart.options.interval);if(data.candles.length){apply(data,null,false);connectStream();timer=setTimeout(refresh,30000);seeded=true;}}
  catch{} // Validate the ordinary live read if an older/mismatched window is supplied.
 }
 if(!seeded)refresh();return ()=>{active=false;epoch++;clearTimeout(timer);stopStream?.();feed.destroy();root.removeEventListener('intervalchange',interval);chart.container.removeEventListener('viewchange',pageHistory);chart.container.removeEventListener('latestrequest',latest);document.removeEventListener('visibilitychange',visibility);pages.clear();};
}
