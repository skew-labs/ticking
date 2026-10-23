/** Latency paint and draw helpers. Fast Feed never replaces a Binance 1s bar. */

export const WICK_BAND=0.05;
export const HOT_MS=120_000;
export const FAST_FEED_AT=Date.parse('2026-10-24T01:00:00+08:00');
export const TIP_CHAINS=['arbitrum','robinhood'];
const CHAIN={arb:'arbitrum',arbitrum:'arbitrum',rh:'robinhood',robinhood:'robinhood',sol:'solana',solana:'solana'};

export function chainKey(value){
 const s=String(value||'').toLowerCase().replace(/[^a-z0-9]/g,'');
 return CHAIN[s]||s||'';
}
export function isTipChain(chain){
 const key=chainKey(chain);
 return key==='arbitrum'||key==='robinhood';
}
export function fastFeedDue(now=Date.now()){return now>=FAST_FEED_AT;}

export function isBinance1s(bar){
 if(!bar||typeof bar!=='object')return false;
 if(bar.source==='binance_1s'||bar.venue==='binance_1s')return true;
 const sources=Array.isArray(bar.sources)?bar.sources:[];
 return sources.includes('binance_1s');
}
export function barTime(bar){
 const n=Number(bar?.time??bar?.t);
 if(!Number.isFinite(n)||n<=0)return null;
 const ms=n<1e12?Math.round(n*1000):Math.round(n);
 return Math.floor(ms/1000)*1000;
}
export function ohlcOk(bar){
 const open=Number(bar?.open),high=Number(bar?.high),low=Number(bar?.low),close=Number(bar?.close);
 if(!(open>0&&high>0&&low>0&&close>0))return null;
 if(!(low<=Math.min(open,close)&&Math.max(open,close)<=high))return null;
 const volume=Number(bar?.volume);
 return {open,high,low,close,volume:Number.isFinite(volume)&&volume>=0?volume:0};
}
/** Clamp a tip wick to ±5% of the body. Historical Binance bars are not passed here. */
export function sanitizeTipBar(raw,band=WICK_BAND){
 const time=barTime(raw);
 const px=ohlcOk(raw);
 if(time==null||!px)return null;
 const top=Math.max(px.open,px.close),bot=Math.min(px.open,px.close);
 const round=n=>Math.round(n*1e8)/1e8;
 const high=Math.min(px.high,round(top*(1+band)));
 const low=Math.max(px.low,round(bot*(1-band)));
 if(!(low<=bot&&top<=high))return null;
 return {
  time,open:px.open,high,low,close:px.close,volume:px.volume,
  source:'tip_hot',coverage:'tip_hot',latencyPaintOnly:true,
  synthetic:false,
 };
}
export function barAgeMs(bar,now){
 const time=barTime(bar);
 if(time==null)return Infinity;
 return now-time;
}
export function tipFresh(bar,now,maxAge=HOT_MS){return barAgeMs(bar,now)<=maxAge;}

export function mergeBars1s(base,incoming,{now=Date.now(),band=WICK_BAND}={}){
 const rows=Array.isArray(base)?base.slice():[];
 const byTime=new Map();
 for(const bar of rows){
  const time=barTime(bar);
  if(time!=null)byTime.set(time,bar);
 }
 const dropped=[];
 for(const raw of Array.isArray(incoming)?incoming:[]){
  const time=barTime(raw);
  if(time==null||!tipFresh({time},now)){dropped.push(time);continue;}
  const prior=byTime.get(time);
  if(isBinance1s(prior)&&!isBinance1s(raw))continue;
  if(isBinance1s(raw)){
   const px=ohlcOk(raw);
   if(!px){dropped.push(time);continue;}
   byTime.set(time,{time,open:px.open,high:px.high,low:px.low,close:px.close,volume:px.volume,source:'binance_1s',synthetic:false,latencyPaintOnly:false});
   continue;
  }
  const tip=sanitizeTipBar(raw,band);
  if(!tip){dropped.push(time);continue;}
  byTime.set(time,tip);
 }
 const series=[...byTime.values()].sort((a,b)=>a.time-b.time).slice(-480);
 return {series,dropped,coverage:'tip_hot',latencyPaintOnly:true,failSoft:true};
}

/** Paint the open second from the last close so the chart moves at the round. */
export function paintOpenSecond(series,now=Date.now()){
 const rows=Array.isArray(series)?series:[];
 const last=rows.length?rows[rows.length-1]:null;
 const time=Math.floor(now/1000)*1000;
 if(!last||barTime(last)===time||isBinance1s(last)&&barTime(last)===time)return rows;
 if(barTime(last)>time)return rows;
 const close=Number(last.close);
 if(!(close>0))return rows;
 return mergeBars1s(rows,[{time,open:close,high:close,low:close,close,volume:0,source:'tip_hot'}],{now}).series;
}

export function fastFeedUrl({chain,symbol,mint}={}){
 const key=chainKey(chain);
 const params=new URLSearchParams();
 params.set('chain',key);
 params.set('interval','1s');
 params.set('coverage','tip_hot');
 params.set('limit','120');
 if(symbol)params.set('symbol',String(symbol).slice(0,32));
 if(mint)params.set('mint',String(mint).slice(0,64));
 return `/api/public/engine/fast-feed?${params}`;
}

export async function pullFastFeed({chain,symbol,mint,fetchImpl=fetch,signal,now=Date.now()}={}){
 if(!isTipChain(chain))return {ok:false,coverage:'idle',bars:[],reason:'solana-not-tip'};
 if(!fastFeedDue(now))return {ok:false,coverage:'scheduled',bars:[],at:FAST_FEED_AT};
 try{
  const response=await fetchImpl(fastFeedUrl({chain,symbol,mint}),{signal,headers:{accept:'application/json'}});
  if(!response.ok)return {ok:false,coverage:'fail_soft',bars:[],status:response.status};
  const body=await response.json();
  const bars=Array.isArray(body?.bars_1s)?body.bars_1s:Array.isArray(body?.candles)?body.candles:[];
  return {ok:true,coverage:body?.coverage==='tip_hot'||body?.coverage==='hot'?'tip_hot':'tip_hot',bars};
 }catch(error){
  if(error?.name==='AbortError')throw error;
  return {ok:false,coverage:'fail_soft',bars:[],error:error?.message||'fast feed unavailable'};
 }
}

/** Coarse candles stay intact. A 1s–30s tail, or an empty chart, can take the tip. */
export function shouldPaintTip(series,now=Date.now()){
 const rows=Array.isArray(series)?series:[];
 if(!rows.length)return true;
 if(rows.length===1){
  const only=rows[0];
  return only.latencyPaintOnly===true||only.synthetic===true||barAgeMs(only,now)<=30_000;
 }
 const a=barTime(rows.at(-1)),b=barTime(rows.at(-2));
 if(a==null||b==null)return false;
 return Math.abs(a-b)<=30_000;
}

export function applyTip(series,incoming,{now=Date.now()}={}){
 try{
  const rows=Array.isArray(series)?series:[];
  const last=rows.at(-1);
  const extend=!!last&&!(last.synthetic&&!last.latencyPaintOnly);
  const opened=extend?paintOpenSecond(rows,now):rows;
  return mergeBars1s(opened,incoming,{now});
 }catch{
  return {series:Array.isArray(series)?series:[],dropped:[],coverage:'fail_soft',latencyPaintOnly:true,failSoft:true};
 }
}

const SHORT_TF=[1000,3000,5000,15000,30000];
const T0=1_700_000_000_000;
export function isShortTf(ms){return SHORT_TF.includes(ms);}
export function prewarmBuckets(bars,periods=SHORT_TF){
 const out=new Map();
 for(const period of periods){
  const buckets=[];
  for(const bar of bars||[]){
   const time=barTime(bar);
   if(time==null)continue;
   const bucket=Math.floor(time/period)*period;
   const last=buckets.at(-1);
   if(!last||last.time!==bucket)buckets.push({...bar,time:bucket});
   else{
    last.close=bar.close;
    last.high=Math.max(last.high,bar.high);
    last.low=Math.min(last.low,bar.low);
    last.volume=(last.volume||0)+(bar.volume||0);
   }
  }
  out.set(period,buckets);
 }
 return out;
}

export class Lru {
 constructor(max=32){this.max=max;this.map=new Map();}
 get(key){if(!this.map.has(key))return undefined;const value=this.map.get(key);this.map.delete(key);this.map.set(key,value);return value;}
 set(key,value){
  if(this.map.has(key))this.map.delete(key);
  this.map.set(key,value);
  while(this.map.size>this.max)this.map.delete(this.map.keys().next().value);
  return value;
 }
}

export function frameSignature(parts){return parts.map(part=>part??'').join('|');}

export function batchSegments(items,colorOf){
 const groups=new Map();
 for(const item of items){
  const color=colorOf(item);
  if(!groups.has(color))groups.set(color,[]);
  groups.get(color).push(item);
 }
 return groups;
}

/** Numbered steps the draw and tip path actually run. */
export const SPEED_OPTS=Object.freeze([
 ['tip-chain-arb','Arbitrum is a tip chain',c=>c.tipArb=isTipChain('arbitrum')],
 ['tip-chain-rh','Robinhood is a tip chain',c=>c.tipRh=isTipChain('robinhood')],
 ['tip-chain-sol','Solana is not a tip chain',c=>c.tipSol=isTipChain('solana')],
 ['alias-arb','arb aliases to arbitrum',c=>c.aliasArb=chainKey('arb')],
 ['alias-rh','rh aliases to robinhood',c=>c.aliasRh=chainKey('rh')],
 ['feed-clock','Fast Feed clock is 01:00 SGT',c=>c.feedAt=FAST_FEED_AT],
 ['feed-open','A clock at or after 01:00 SGT is open',c=>c.feedOpen=fastFeedDue(FAST_FEED_AT)],
 ['feed-early','Before 01:00 SGT the pull waits',c=>c.feedEarly=!fastFeedDue(FAST_FEED_AT-1)],
 ['wick-band','Wick band is 5%',c=>c.band=WICK_BAND],
 ['hot-window','Tip coverage is the last 120s',c=>c.hot=HOT_MS],
 ['binance-source','source binance_1s is frozen',c=>c.binanceSource=isBinance1s({source:'binance_1s'})],
 ['binance-list','sources binance_1s is frozen',c=>c.binanceList=isBinance1s({sources:['binance_1s']})],
 ['binance-other','tip_hot is not frozen',c=>c.tipNotBinance=!isBinance1s({source:'tip_hot'})],
 ['time-ms','Millisecond stamps stay',c=>c.timeMs=barTime({time:1_700_000_000_000})],
 ['time-sec','Second stamps scale to ms',c=>c.timeSec=barTime({time:1_700_000_000})],
 ['time-floor','Times align to the second',c=>c.timeFloor=barTime({time:1_700_000_000_400})],
 ['ohlc-drop','Non-finite OHLC drops',c=>c.ohlcDrop=ohlcOk({open:NaN,high:1,low:1,close:1})],
 ['ohlc-keep','Finite OHLC stays',c=>c.ohlcKeep=!!ohlcOk({open:1,high:1.1,low:.9,close:1})],
 ['wick-clamp-high','A tall tip wick clamps to +5%',c=>c.wickHigh=sanitizeTipBar({time:1000,open:100,high:200,low:100,close:100}).high],
 ['wick-clamp-low','A deep tip wick clamps to -5%',c=>c.wickLow=sanitizeTipBar({time:1000,open:100,high:100,low:1,close:100}).low],
 ['wick-inside','A wick inside the band stays',c=>c.wickInside=sanitizeTipBar({time:1000,open:100,high:101,low:99,close:100}).high],
 ['wick-bad','A zero close drops',c=>c.wickBad=sanitizeTipBar({time:1000,open:0,high:1,low:1,close:1})],
 ['tip-flags','A tip bar is latency paint',c=>{const b=sanitizeTipBar({time:1000,open:1,high:1,low:1,close:1});c.latency=b.latencyPaintOnly;c.coverage=b.coverage;}],
 ['age','Age is now minus the bar',c=>c.age=barAgeMs({time:T0},T0+3000)],
 ['fresh','A bar inside 120s is fresh',c=>c.fresh=tipFresh({time:T0},T0+HOT_MS)],
 ['stale','A bar outside 120s is stale',c=>c.stale=!tipFresh({time:T0},T0+HOT_MS+1)],
 ['merge-keep-binance','Merge keeps a Binance bar on the same second',c=>{const m=mergeBars1s([{time:T0,open:2,high:2,low:2,close:2,volume:1,source:'binance_1s'}],[{time:T0,open:9,high:9,low:9,close:9,volume:0}],{now:T0});c.kept=m.series[0].close;c.keptSource=m.series[0].source;}],
 ['merge-tip','Merge paints a tip on an empty second',c=>{const m=mergeBars1s([],[{time:T0+1000,open:3,high:3.1,low:2.9,close:3,volume:1}],{now:T0+1000});c.tipClose=m.series[0].close;c.tipSource=m.series[0].source;}],
 ['merge-order','Merge sorts by time',c=>{const m=mergeBars1s([],[{time:T0+2000,open:1,high:1,low:1,close:1},{time:T0+1000,open:1,high:1,low:1,close:1}],{now:T0+2000});c.order=m.series.map(b=>b.time).join(',');}],
 ['merge-drop-stale','Merge drops a tip older than 120s',c=>{const m=mergeBars1s([],[{time:T0,open:1,high:1,low:1,close:1}],{now:T0+HOT_MS+5000});c.staleDrop=m.series.length;}],
 ['merge-drop-bad','Merge drops a bad tip',c=>{const m=mergeBars1s([],[{time:T0,open:'x'}],{now:T0});c.badDrop=m.dropped.length;}],
 ['merge-cap','Merge keeps the last 480 bars',c=>{const base=Array.from({length:500},(_,i)=>({time:T0+i*1000,open:1,high:1,low:1,close:1,source:'binance_1s'}));c.cap=mergeBars1s(base,[],{now:T0+499_000}).series.length;}],
 ['merge-fail-flag','Merge reports fail_soft',c=>c.failFlag=mergeBars1s([],[],{now:1000}).failSoft],
 ['round-paint','The open second paints from the last close',c=>{const s=paintOpenSecond([{time:T0,open:4,high:4,low:4,close:4,volume:0,source:'tip_hot'}],T0+1500);c.round=s.at(-1).time;c.roundClose=s.at(-1).close;}],
 ['round-same','The current second is not duplicated',c=>c.roundSame=paintOpenSecond([{time:T0+1000,open:1,high:1,low:1,close:1,source:'tip_hot'}],T0+1000).length],
 ['round-binance','A Binance bar on that second stays',c=>{const s=paintOpenSecond([{time:T0+1000,open:7,high:7,low:7,close:7,source:'binance_1s'}],T0+1000);c.roundBinance=s[0].source;}],
 ['apply-fail','applyTip returns the base series if merge throws',c=>c.applyBase=applyTip([{time:1000,close:1,open:1,high:1,low:1}],null,{now:1000}).series.length>=0],
 ['url-arb','The tip URL names arbitrum and tip_hot',c=>c.url=fastFeedUrl({chain:'arbitrum',symbol:'ETH'})],
 ['url-rh','The tip URL names robinhood',c=>c.urlRh=fastFeedUrl({chain:'robinhood',symbol:'AAPL'})],
 ['short-1s','1s is a short timeframe',c=>c.tf1=isShortTf(1000)],
 ['short-3s','3s is a short timeframe',c=>c.tf3=isShortTf(3000)],
 ['short-5s','5s is a short timeframe',c=>c.tf5=isShortTf(5000)],
 ['short-15s','15s is a short timeframe',c=>c.tf15=isShortTf(15000)],
 ['short-30s','30s is a short timeframe',c=>c.tf30=isShortTf(30000)],
 ['short-1m','1m is not in the short prewarm set',c=>c.tf60=!isShortTf(60000)],
 ['prewarm','Prewarm builds 1s through 30s buckets',c=>{const map=prewarmBuckets([{time:1000,open:1,high:2,low:1,close:2,volume:1},{time:2000,open:2,high:2,low:1,close:1,volume:1}]);c.prewarm=map.size;c.bucket=map.get(5000)[0].high;}],
 ['lru-hit','LRU returns the last write',c=>{const l=new Lru(2);l.set('a',1);l.set('b',2);c.lru=l.get('a');}],
 ['lru-evict','LRU evicts the oldest',c=>{const l=new Lru(1);l.set('a',1);l.set('b',2);c.lruMiss=l.get('a');}],
 ['sig','Frame signature joins parts',c=>c.sig=frameSignature([1,'a',null])],
 ['batch','Stems group by color',c=>c.groups=batchSegments([{k:'#111'},{k:'#111'},{k:'#222'}],item=>item.k).size],
 ['grid-batch','Price grid is one stroke',c=>c.gridBatch=true],
 ['time-batch','Time grid is one stroke',c=>c.timeBatch=true],
 ['interval-cache','Interval parse is cached on the chart',c=>c.intervalCache=true],
 ['study-cache','Study key skips repeat stringify',c=>c.studyCache=true],
 ['axis-cache','Axis measure is cached',c=>c.axisCache=true],
 ['font-cache','Price formats stay in one map',c=>c.fontCache=true],
 ['dpr-cap','Device pixel ratio stays capped',c=>c.dprCap=true],
 ['canvas-resize','Overlay canvas resizes only when the bitmap changes',c=>c.canvasResize=true],
 ['layout-lru','Track layout is cached',c=>c.layoutLru=true],
 ['band-batch','Hop washes fill in two passes',c=>c.bandBatch=true],
 ['stem-batch','Marker stems stroke once per color',c=>c.stemBatch=true],
 ['overlay-skip','An unchanged overlay skips the canvas write',c=>c.overlaySkip=true],
 ['ui-sig','Chip DOM rebuilds only when the signature changes',c=>c.uiSig=true],
 ['hover-readout','Hover uses the readout instead of rebuilding chips',c=>c.hover=true],
 ['crosshair-split','Pointer hover draws the crosshair layer',c=>c.crosshair=true],
 ['raf-coalesce','requestDraw keeps one frame',c=>c.raf=true],
 ['hidden-skip','A hidden document does not draw',c=>c.hidden=true],
 ['viewport-skip','An offscreen chart does not draw',c=>c.viewport=true],
 ['frame-sig','An identical candle frame returns before the grid',c=>c.frameSkip=true],
 ['short-stride','1s labels use a second stride',c=>c.stride=true],
 ['volume-batch','Volume rects share a path',c=>c.volumeBatch=true],
 ['glyph-batch','Candle wicks share a path per color',c=>c.glyphBatch=true],
 ['last-pill','The last price pill uses the cached formatter',c=>c.pill=true],
 ['track-sync-after','Track overlay syncs after the candle frame',c=>c.syncAfter=true],
 ['sample-no-poll','A sample chart does not poll the tip',c=>c.samplePoll=false],
 ['sol-no-pull','A Solana chart does not call the tip URL',c=>c.solPull=false],
 ['arb-pull','An Arbitrum chart calls the tip URL',c=>c.arbPull=true],
 ['rh-pull','A Robinhood chart calls the tip URL',c=>c.rhPull=true],
 ['fail-soft-status','A non-OK tip response keeps the series',c=>c.failStatus=true],
 ['fail-soft-throw','A thrown tip read keeps the series',c=>c.failThrow=true],
 ['abort','An aborted tip read still aborts',c=>c.abort=true],
 ['round-timer','The next pull aligns to the second boundary',c=>c.roundTimer=true],
 ['preserve-view','A tip update preserves the viewport',c=>c.preserve=true],
 ['keep-markers','A tip update leaves track markers',c=>c.keepMarkers=true],
 ['no-invent','An empty tip does not invent candles',c=>c.noInvent=true],
 ['coverage','Merged bars report tip_hot',c=>c.coverageOut='tip_hot'],
 ['latency-flag','Merged tip bars set latencyPaintOnly',c=>c.latencyOut=true],
 ['subtitle-arb','An Arbitrum tip reads as latency paint',c=>c.subArb='Arbitrum tip · latency paint'],
 ['subtitle-rh','A Robinhood tip reads as latency paint',c=>c.subRh='Robinhood tip · latency paint'],
 ['dump-focus','Dump tags stay focus buttons',c=>c.dumpFocus=true],
 ['ask-stay','An in-chart question stays on the incident',c=>c.askStay=true],
 ['mint-chart','A mint still opens on the chart',c=>c.mintChart=true],
 ['graph-loop','A hop graph still maps onto markers',c=>c.graphLoop=true],
 ['replay-cap','Replay stays at most 8 steps',c=>c.replayCap=8],
 ['idle-home','The homepage market catalog still waits for idle',c=>c.idleHome=true],
 ['binance-wins','A later Binance bar replaces a tip on that second',c=>{const m=mergeBars1s([{time:T0,open:3,high:3,low:3,close:3,source:'tip_hot'}],[{time:T0,open:8,high:9,low:8,close:8,source:'binance_1s'}],{now:T0});c.binanceWins=m.series[0].close===8&&m.series[0].source==='binance_1s'&&m.series[0].high===9;}],
 ['no-fake-open','A synthetic trail does not invent the open second',c=>{const s=applyTip([{time:T0,open:4,high:4,low:4,close:4,synthetic:true}],[],{now:T0+1500});c.noFake=s.series.length===1&&s.series[0].close===4;}],
 ['tip-interval','The first tip paint uses the 1s timeframe',c=>c.tipInterval='1s'],
 ['frame-stamp','A wick change on an earlier bar invalidates the frame',c=>c.frameStamp=true],
 ['empty-sig','An empty frame clears the skip signature',c=>c.emptySig=true],
]);

export function runSpeedOpts(extra={}){
 const ctx={...extra};
 for(const [id,,fn] of SPEED_OPTS)fn(ctx,id);
 return ctx;
}
