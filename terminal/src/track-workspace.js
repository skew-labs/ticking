import {TickChart} from './chart.js';
import {parseTrackPaint,seriesFromPaint,panesForWallet,shortWallet,walletAddress,sampleTrackPaint,sampleMintPaint,columnForBridge,museLine} from './track-paint.js';
import {chartAskBody,classifyTrackIntent,guardedResolveBody,mintAddress,trackResolveBody} from './track-intent.js';
import {applyTip,chainKey,isTipChain,prewarmBuckets,pullFastFeed} from './speed-pass.js';
import trackCss from './track-style.js';

export const HANDOFF_KEY='ticking.track.handoff.v1';
const CHAIN=/^[a-z]{2,20}$/;

export function parseWorkspaceHash(hash=typeof location==='undefined'?'':location.hash){
 const raw=String(hash||'');
 const [path,query='']=raw.replace(/^#/,'').split('?');
 const m=/^\/console\/workspace\/([^/]+)$/.exec(path);
 if(!m)return null;
 let ident;try{ident=decodeURIComponent(m[1]);}catch{return null;}
 if(!/^[-A-Za-z0-9_.:]{1,120}$/.test(ident))return null;
 const params=new URLSearchParams(query);
 const wallet=ident.startsWith('wallet:')?walletAddress(ident.slice(7)):walletAddress(params.get('wallet'));
 const chain=CHAIN.test(params.get('chain')||'')?params.get('chain'):null;
 const mint=mintAddress(ident.startsWith('mint:')?ident.slice(5):params.get('mint'));
 const launch=ident==='launch'||params.get('launch')==='1';
 const symbol=launch&&!mint&&ident!=='launch'&&/^[A-Za-z][A-Za-z0-9]{1,12}$/.test(ident)?ident.toUpperCase():null;
 const incident=wallet?`wallet:${wallet}`:mint?`mint:${mint}`:symbol?`mint:${symbol}`:ident;
 return {ident,wallet,chain,mint,launch,symbol,query:params.get('q')||'',sample:params.get('sample')==='1',incident};
}
export function trackerRedirect(hash){
 const raw=String(hash||'').replace(/^#/,'');
 const [path,query='']=raw.split('?');
 if(path!=='/tracker'&&!path.startsWith('/tracker/'))return null;
 const params=new URLSearchParams(query);
 const wallet=walletAddress(params.get('address')||params.get('wallet')||'');
 if(!wallet)return '';
 const chain=CHAIN.test(params.get('chain')||'')?`?chain=${params.get('chain')}`:'';
 return `#/console/workspace/${encodeURIComponent('wallet:'+wallet)}${chain}`;
}
export function readStoredHandoff(){
 try{return JSON.parse(sessionStorage.getItem(HANDOFF_KEY)||'null');}catch{return null;}
}
export function storeHandoff(body){
 try{sessionStorage.setItem(HANDOFF_KEY,JSON.stringify(body));}catch{}
}
function richer(a,b){
 const an=a?.chartMarkers?.length||0,bn=b?.chartMarkers?.length||0;
 if(bn>an)return b;
 if(an>0)return a;
 return b||a;
}
export async function loadTrackPaint({wallet,chain,handoff,sample,fetchImpl=fetch,signal}={}){
 if(sample)return {paint:sampleTrackPaint(wallet),source:'sample'};
 const stored=parseTrackPaint(handoff??readStoredHandoff());
 const address=walletAddress(wallet);
 if(!address)return {paint:stored?.track?stored:parseTrackPaint(null),source:stored?.track?'handoff':'empty'};
 const query=`where did the money go from ${address}${chain?` on ${chain}`:''}`;
 let live=null,error='';
 try{
  const r=await fetchImpl('/api/public/intelligence/resolve',{method:'POST',headers:{'content-type':'application/json',accept:'application/json'},body:JSON.stringify(trackResolveBody({query,wallet:address,chain})),signal});
  if(!r.ok)throw Error('Track resolve failed ('+r.status+').');
  const body=await r.json();
  live=parseTrackPaint(body);
  const priorN=stored?.chartMarkers?.length||0;
  if((live?.chartMarkers?.length||0)>=priorN)storeHandoff(body);
 }catch(e){if(e?.name==='AbortError')throw e;error=e.message||'Track resolve unavailable.';}
 const paint=richer(live,stored?.track?stored:null)||parseTrackPaint(null);
 return {paint,source:live?.chartMarkers?.length?'resolve':stored?.chartMarkers?.length?'handoff':'resolve',error};
}
export function candlesFromEngine(body){
 const rows=Array.isArray(body?.candles)?body.candles:[];
 const series=[];
 for(const row of rows){
  let time=Number(row?.time);
  const open=Number(row?.open),high=Number(row?.high),low=Number(row?.low),close=Number(row?.close);
  if(!Number.isFinite(time)||!Number.isFinite(open)||!Number.isFinite(high)||!Number.isFinite(low)||!Number.isFinite(close))continue;
  if(!(open>0&&high>0&&low>0&&close>0))continue;
  if(time>0&&time<1e12)time*=1000;
  const volume=Number(row?.volume);
  series.push({time,open,high,low,close,volume:Number.isFinite(volume)?volume:0});
 }
 return series;
}
export function mintCandleUrl(mint){
 return `/api/public/engine/candles?aggregation=token&mint=${encodeURIComponent(mint)}&interval=1m&history=full&gap_policy=carry&limit=240`;
}
function mentionsMint(paint,needle){
 if(!needle||!paint)return false;
 return JSON.stringify(paint).toLowerCase().includes(String(needle).toLowerCase());
}
export async function loadMintCandles(mint,{fetchImpl=fetch,signal}={}){
 if(!mint||!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(mint))return [];
 try{
  const r=await fetchImpl(mintCandleUrl(mint),{headers:{accept:'application/json'},signal});
  if(!r.ok)return [];
  return candlesFromEngine(await r.json());
 }catch(e){if(e?.name==='AbortError')throw e;return [];}
}
export async function loadMintPaint({mint,chain,symbol,query,sample,handoff,fetchImpl=fetch,signal}={}){
 if(sample)return {paint:sampleMintPaint(mint),source:'sample'};
 const spoken=String(query||(mint?`minted ${mint}${chain?` on ${chain}`:''}`:symbol?`minted ${symbol}${chain?` on ${chain}`:''}`:'new token')).trim();
 let live=null,error='';
 try{
  const r=await fetchImpl('/api/public/intelligence/resolve',{method:'POST',headers:{'content-type':'application/json',accept:'application/json'},body:JSON.stringify(trackResolveBody({query:spoken})),signal});
  if(!r.ok)throw Error('Mint resolve failed ('+r.status+').');
  const body=await r.json();
  live=parseTrackPaint(body);
  if((live?.chartMarkers?.length||0)>0)storeHandoff(body);
 }catch(e){if(e?.name==='AbortError')throw e;error=e.message||'Mint resolve unavailable.';}
 const stored=parseTrackPaint(handoff??null);
 const needle=mint||symbol||'';
 const relevant=mentionsMint(stored,needle)?stored:null;
 const paint=richer(live,relevant?.track?relevant:null)||parseTrackPaint(null);
 return {paint,source:paint.chartMarkers?.length?(live?.chartMarkers?.length?'resolve':'handoff'):'resolve',error};
}
export function trackRouteFromResolve(body){
 if(!body||typeof body!=='object')return null;
 const route=typeof body.route==='string'?body.route:'';
 const request=body.handoff?.next?.request;
 const guardMode=body.guard?.mode;
 if(body.mode==='mint'||guardMode==='mint'){
  const mintRoute=route.includes('/console/workspace/')&&(route.includes('mint%3A')||route.includes('mint:')||route.includes('/launch')||route.includes('launch=1'));
  if(!mintRoute)return null;
  return route.startsWith('#')?route:`#${route}`;
 }
 const guard=typeof body.searchGuard==='string'?body.searchGuard:guardMode;
 const track=request?.track===true||body.mode==='track'||body.mode==='dump'||(typeof guard==='string'&&(guard.includes('track')||guard==='dump'));
 if(!track&&!route.includes('/console/workspace/wallet:'))return null;
 if(route.includes('/console/workspace/wallet:'))return route.startsWith('#')?route:`#${route}`;
 const wallet=walletAddress(body.entity?.id||request?.query||'');
 return wallet?`#/console/workspace/${encodeURIComponent('wallet:'+wallet)}`:null;
}
export function acceptTrackHandoff(body){
 const route=trackRouteFromResolve(body);
 if(!route)return null;
 const paint=parseTrackPaint(body);
 const prior=parseTrackPaint(readStoredHandoff());
 if((paint.chartMarkers?.length||0)>=(prior?.chartMarkers?.length||0))storeHandoff(body);
 return route;
}
export async function resolveTrackQuery(query,{signal,fetchImpl=fetch}={}){
 const intent=classifyTrackIntent(query);
 if(intent.mode!=='track'&&intent.mode!=='dump'&&intent.mode!=='mint')return null;
 const r=await fetchImpl('/api/public/intelligence/resolve',{method:'POST',headers:{'content-type':'application/json',accept:'application/json'},body:JSON.stringify(trackResolveBody({query})),signal});
 if(!r.ok)throw Error('Track resolve failed ('+r.status+').');
 const body=await r.json();
 const route=acceptTrackHandoff(body);
 if(!route&&intent.mode==='mint'){
  const paint=parseTrackPaint(body);
  if((paint.chartMarkers?.length||0)>0){
   const prior=parseTrackPaint(readStoredHandoff());
   const needle=intent.mint||intent.symbol||'';
   const priorOk=!needle||mentionsMint(prior,needle);
   if(paint.chartMarkers.length>=(priorOk?(prior?.chartMarkers?.length||0):0))storeHandoff(body);
  }
 }
 return body;
}
export function applyChartAnswer(current,incoming,error=''){
 const next=incoming?.chartMarkers?.length?incoming:null;
 const note=museLine(incoming?.muse?.summary);
 const summary=next?(museLine(next.muse?.summary)||`${next.chartMarkers.length} hops on this chart.`):(note||'This question stays on this chart.');
 return {paint:next||current||parseTrackPaint(null),summary,stayed:true,replaced:Boolean(next),error:error&&!next?error:''};
}
export async function askChart({query,wallet,mint,chain,symbol,incident,fetchImpl=fetch,signal}={}){
 const spoken=String(query||'').trim();
 if(!spoken)return {paint:null,error:'',stayed:true};
 const body=chartAskBody({query:spoken,wallet,mint,chain,symbol,incident});
 try{
  const r=await fetchImpl('/api/public/intelligence/resolve',{method:'POST',headers:{'content-type':'application/json',accept:'application/json'},body:JSON.stringify(body),signal});
  if(!r.ok)throw Error('Chart ask failed ('+r.status+').');
  const json=await r.json();
  const paint=parseTrackPaint(json);
  if((paint.chartMarkers?.length||0)>0)storeHandoff(json);
  return {paint,error:'',stayed:true};
 }catch(e){
  if(e?.name==='AbortError')throw e;
  return {paint:null,error:e.message||'Chart ask unavailable.',stayed:true};
 }
}
function bindChartAsk(form,{signal,context,onAnswer}){
 if(!form)return;
 form.addEventListener('submit',async e=>{
  e.preventDefault();
  const q=String(form.q?.value||'').trim();
  const summary=form.closest('.track-workspace')?.querySelector('[data-track-summary]');
  if(!q){if(summary)summary.textContent='Ask about this chart.';return;}
  const hash=typeof location==='undefined'?'':location.hash;
  if(summary)summary.textContent='Reading this chart…';
  const button=form.querySelector('button');
  if(button)button.disabled=true;
  const askAbort=new AbortController();
  const stop=()=>askAbort.abort();
  signal?.addEventListener('abort',stop,{once:true});
  try{
   const {paint,error}=await askChart({...context(),query:q,signal:askAbort.signal});
   if(typeof location!=='undefined'&&location.hash!==hash)location.hash=hash;
   onAnswer(paint,error||'');
  }catch(err){
   if(err?.name==='AbortError')return;
   if(typeof location!=='undefined'&&hash&&location.hash!==hash)location.hash=hash;
   onAnswer(null,err?.message||'');
  }finally{
   signal?.removeEventListener('abort',stop);
   if(button)button.disabled=false;
  }
 },{signal});
}
const ASK_FORM='<form class="track-ask" data-track-ask><label>Ask this chart<input name="q" autocomplete="off" spellcheck="false" placeholder="Ask about this hop"></label><button type="submit">Paint answer</button></form>';
export function installTrackGuard(){
 if(typeof window==='undefined'||window.__tickingTrackGuard)return;
 window.__tickingTrackGuard=true;
 const orig=window.fetch.bind(window);
 window.fetch=async(input,init={})=>{
  let nextInit=init;
  let stay=false;
  const url=typeof input==='string'?input:input instanceof Request?input.url:'';
  if(url.includes('/api/public/intelligence/resolve')&&typeof init?.body==='string'){
   try{
    const parsed=JSON.parse(init.body);
    stay=parsed?.stay===true||parsed?.guard?.stay===true;
    const guarded=guardedResolveBody(parsed);
    if(guarded)nextInit={...init,body:JSON.stringify(guarded)};
   }catch{}
  }
  const response=await orig(input,nextInit);
  if(url.includes('/api/public/intelligence/resolve')){
   try{
    const body=await response.clone().json();
    const route=acceptTrackHandoff(body);
    const hash=location.hash||'';
    if(!stay&&route&&(hash.includes('/auto')||hash.startsWith('#/tracker')||hash.includes('/search'))&&location.hash!==route)location.hash=route;
   }catch{}
  }
  return response;
 };
}
installTrackGuard();

function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

export function ensureTrackStyle(){
 if(typeof document==='undefined'||document.getElementById('ticking-track-style'))return;
 const node=document.createElement('style');
 node.id='ticking-track-style';
 node.textContent=trackCss;
 document.head.append(node);
}

export function mountTrackerPage(root,{params}={}){
 ensureTrackStyle();
 const search=params instanceof URLSearchParams?params:new URLSearchParams(params||'');
 const next=trackerRedirect(`#/tracker${search.size?`?${search}`:''}`);
 if(next){location.replace(next);return ()=>{};}
 return mountTrackerRetired(root);
}

export function mountTrackerRetired(root){
 ensureTrackStyle();
 root.innerHTML=`<section class="track-retired" data-track-form><h1>Track</h1><form><label>Wallet<input name="wallet" required autocomplete="off" spellcheck="false" placeholder="0x…"></label><button type="submit">Open chart</button></form><p role="status"></p></section>`;
 const form=root.querySelector('form'),status=root.querySelector('[role=status]');
 const onSubmit=e=>{
  e.preventDefault();
  const wallet=walletAddress(form.wallet.value);
  if(!wallet){status.textContent='Enter a 0x wallet address.';return;}
  location.hash=`#/console/workspace/${encodeURIComponent('wallet:'+wallet)}`;
 };
 form.addEventListener('submit',onSubmit);
 return ()=>form.removeEventListener('submit',onSubmit);
}

export function shouldOpenWallet(primary,chip){
 const address=walletAddress(chip?.address);
 if(!address||chip?.kind==='bridge')return false;
 if(address===walletAddress(primary)&&!chip?.symbol)return false;
 return true;
}
export function mountTrackWorkspace(root,{incident,wallet,chain,sample}={}){
 ensureTrackStyle();
 const abort=new AbortController();
 let disposed=false;
 const charts=[];
 const opened=new Set();
 root.innerHTML=`<section class="track-workspace" data-incident="${esc(incident||wallet||'workspace')}"><header class="track-workspace-bar"><div><p class="track-kicker">Workspace track</p><h1 data-track-title>${esc(shortWallet(wallet)||incident||'Workspace')}</h1><p data-track-caption class="track-caption"></p></div><p data-track-summary role="status">Reading the track paint…</p><button type="button" data-track-replay hidden>Replay trail</button></header>${ASK_FORM}<div class="track-layout"><div class="track-charts" data-track-charts></div><aside class="track-side" data-track-side hidden><h2>Beside this chart</h2><div data-track-columns></div></aside></div></section>`;
 const summary=root.querySelector('[data-track-summary]');
 const caption=root.querySelector('[data-track-caption]');
 const chartsEl=root.querySelector('[data-track-charts]');
 const side=root.querySelector('[data-track-side]');
 const columnsEl=root.querySelector('[data-track-columns]');
 const replayBtn=root.querySelector('[data-track-replay]');
 function addPane({title,subtitle,paint,wallet:paneWallet,role}){
  const key=`${role}:${paneWallet||''}:${title}`;
  if(opened.has(key))return;
  opened.add(key);
  const card=document.createElement('article');
  card.className='track-pane track-pane-enter';
  card.dataset.trackPane=role;
  card.innerHTML=`<header><b>${esc(title)}</b><small>${esc(subtitle||'')}</small></header><div class="track-chart-host"></div>`;
  chartsEl.append(card);
  if(role!=='primary')card.scrollIntoView({block:'nearest',inline:'nearest'});
  const host=card.querySelector('.track-chart-host');
  const series=seriesFromPaint(paint);
  const chart=new TickChart(host,{symbol:title,market:incident||title},{interval:'1m',editable:false,volume:false,series,emptyMessage:'No track markers in this window.',storageScope:'track'});
  chart.setTrackPaint(paint,{onWallet:openWallet,onBridge:openBridge,onFocus:focusMove,onStep:noteStep});
  charts.push(chart);
  if(charts.length>1)chartsEl.classList.add('is-multi');
  return chart;
 }
 function openWallet(chip){
  const address=walletAddress(chip?.address);
  const paint=chip?.paint||charts[0]?.trackLayer?.paint;
  if(!address||!paint||disposed||!shouldOpenWallet(wallet,chip))return;
  const panes=panesForWallet(paint,address,chip.symbol);
  const specs=panes.length?panes:[{title:shortWallet(address),markers:[]}];
  for(const spec of specs)addPane({role:'wallet',wallet:address,title:spec.title,subtitle:'Same case · wallet chart',paint:{...paint,chartMarkers:spec.markers,bands:[],sideColumns:[],muse:{summary:museLine(paint.muse?.summary),beside:null,replay:false,replayMs:900}}});
 }
 function noteStep(step){
  const raw=String(step?.label||'');
  const safe=walletAddress(raw)?shortWallet(raw):museLine(raw);
  const n=(Number(step?.index)||0)+1;
  const total=Number(step?.total)||n;
  caption.textContent=`Trail ${n} of ${total}${safe?` · ${safe}`:''}`;
 }
 function focusMove(focus){
  const layer=charts[0]?.trackLayer;
  if(!layer||disposed)return;
  const time=Number(focus?.time);
  if(Number.isFinite(time)){layer.cursor=time;charts[0].requestDraw?.();}
  const raw=String(focus?.label||'');
  const safe=walletAddress(raw)?shortWallet(raw):museLine(raw);
  if(safe)caption.textContent=safe;
  const address=walletAddress(focus?.wallet);
  if(!address||address===walletAddress(wallet))return;
  openWallet({address,kind:focus?.kind||'tag',symbol:null,paint:layer.paint});
 }
 function openBridge(bridge){
  const paint=charts[0]?.trackLayer?.paint;
  const column=columnForBridge(paint,bridge);
  if(column)openColumn(column);
 }
 function openColumn(column){
  if(!column?.chain||disposed)return;
  addPane({role:'chain',title:`${column.label||column.chain} chart`,subtitle:'Same case · chain chart',paint:{schema:paintSchema(),track:true,listAddresses:false,chartMarkers:column.markers||[],bands:[],sideColumns:[],muse:{summary:'',beside:null,replay:false,replayMs:900}}});
 }
 function paintSchema(){return 'ticking.chartMarkers.track.v1';}
 function showSide(paint){
  const cols=paint.sideColumns||[];
  side.hidden=!cols.length;
  columnsEl.innerHTML=cols.map(c=>`<button type="button" data-track-column="${esc(c.id)}">Open ${esc(c.label||c.chain)} beside<small>Same case</small></button>`).join('');
 }
 const hooks={onWallet:openWallet,onBridge:openBridge,onFocus:focusMove,onStep:noteStep};
 replayBtn.addEventListener('click',()=>charts[0]?.trackLayer?.play(),{signal:abort.signal});
 columnsEl.addEventListener('click',e=>{const b=e.target.closest('[data-track-column]');if(!b)return;const paint=charts[0]?.trackLayer?.paint;openColumn((paint?.sideColumns||[]).find(c=>c.id===b.dataset.trackColumn));},{signal:abort.signal});
 bindChartAsk(root.querySelector('[data-track-ask]'),{signal:abort.signal,context:()=>({wallet,chain,incident:incident||(wallet?`wallet:${wallet}`:null)}),onAnswer:(incoming,error)=>{
  const answer=applyChartAnswer(charts[0]?.trackLayer?.paint,incoming,error);
  if(answer.replaced&&charts[0]){
   const series=seriesFromPaint(answer.paint);
   if(series.length)charts[0].setSeries(series,{preserve:false});
   charts[0].setTrackPaint(answer.paint,hooks);
   showSide(answer.paint);
   replayBtn.hidden=!answer.paint.muse?.replay;
   if(answer.paint.muse?.replay)charts[0].trackLayer?.play();
   const beside=answer.paint.muse?.beside?.wallet;
   if(beside)openWallet({address:beside,kind:'hop',paint:answer.paint});
  }
  summary.textContent=answer.summary;
  summary.dataset.trackAsk='chart';
 }});
 const run=++mountTrackWorkspace.seq;
 (async()=>{
  try{
   const {paint,source,error}=await loadTrackPaint({wallet,chain,sample,signal:abort.signal});
   if(disposed||run!==mountTrackWorkspace.seq)return;
   const n=paint.chartMarkers.length;
   const muse=museLine(paint.muse?.summary);
   summary.textContent=sample?'Sample paint on this workspace chart.':error&&!n?error:muse||(n?`${n} hops on this chart.`:'No hops in this window.');
   if(source)summary.dataset.trackSource=source;
   replayBtn.hidden=!paint.muse?.replay;
   addPane({role:'primary',wallet,title:shortWallet(wallet)||'Track',subtitle:sample?'Preview fixture · trail silhouette, not a market print':'Trail silhouette · not a market print',paint});
   showSide(paint);
   const probe=sample&&new URLSearchParams(location.hash.split('?')[1]||'').get('probe')==='1';
   if(probe){
    const report=()=>{
     const chip=[...root.querySelectorAll('[data-track-wallet]')].find(b=>b.dataset.trackWallet&&b.dataset.trackWallet!==wallet);
     chip?.click();
     root.querySelector('[data-track-column]')?.click();
     root.dataset.probe=JSON.stringify({tags:root.querySelectorAll('.track-tag').length,chips:root.querySelectorAll('.track-chip').length,panes:root.querySelectorAll('[data-track-pane]').length,incident:root.querySelector('.track-workspace')?.dataset.incident,replayHidden:replayBtn.hidden,summary:summary.textContent});
    };
    requestAnimationFrame(()=>requestAnimationFrame(report));
   }else if(paint.muse?.replay)charts[0]?.trackLayer?.play();
  }catch(e){if(!disposed&&e?.name!=='AbortError')summary.textContent=e.message||'Track paint unavailable.';}
 })();
 return ()=>{disposed=true;abort.abort();for(const c of charts)c.destroy();};
}
mountTrackWorkspace.seq=0;

function shortMint(value){
 const text=String(value||'');
 if(!text)return '';
 if(text.length<=12)return text;
 return `${text.slice(0,4)}…${text.slice(-4)}`;
}
function chainKeyLabel(chain){
 return chainKey(chain)==='robinhood'?'Robinhood tip · latency paint':'Arbitrum tip · latency paint';
}
function tipAgeLabel(chain,series,now=Date.now()){
 let hot=null;
 for(let i=series.length-1;i>=0;i--)if(series[i].latencyPaintOnly){hot=series[i];break;}
 if(!hot)return chainKeyLabel(chain);
 const age=Math.max(0,Math.round((now-hot.time)/1000));
 return `${chainKeyLabel(chain)} · ${age}s`;
}
function fitMint(chart){
 const n=chart?.series?.length||chart?.options?.series?.length||0;
 if(n>8){chart.count=Math.min(480,n+8);chart.offset=0;}
}
export function mountMintWorkspace(root,{ident,mint,chain,query,sample,symbol}={}){
 ensureTrackStyle();
 const abort=new AbortController();
 let disposed=false;
 const charts=[];
 const opened=new Set();
 let primaryWallet=null;
 const subject=mint||symbol||ident||'Mint';
 const incident=mint?`mint:${mint}`:symbol?`mint:${symbol}`:'mint:launch';
 root.innerHTML=`<section class="track-workspace" data-mint-chart data-incident="${esc(incident)}"><header class="track-workspace-bar"><div><p class="track-kicker">Mint chart</p><h1 data-track-title>${esc(shortMint(subject)||'Mint')}</h1><p data-track-caption class="track-caption"></p></div><p data-track-summary role="status">Opening the mint chart…</p><button type="button" data-track-replay hidden>Replay trail</button></header>${ASK_FORM}<div class="track-layout"><div class="track-charts" data-track-charts></div><aside class="track-side" data-track-side hidden><h2>Beside this chart</h2><div data-track-columns></div></aside></div></section>`;
 const summary=root.querySelector('[data-track-summary]');
 const caption=root.querySelector('[data-track-caption]');
 const chartsEl=root.querySelector('[data-track-charts]');
 const side=root.querySelector('[data-track-side]');
 const columnsEl=root.querySelector('[data-track-columns]');
 const replayBtn=root.querySelector('[data-track-replay]');
 function addPane({title,subtitle,paint,series,wallet:paneWallet,role,emptyMessage}){
  const key=`${role}:${paneWallet||''}:${title}`;
  if(opened.has(key))return charts.find(c=>c.__mintKey===key)||null;
  opened.add(key);
  const card=document.createElement('article');
  card.className='track-pane track-pane-enter';
  card.dataset.trackPane=role;
  card.innerHTML=`<header><b>${esc(title)}</b><small>${esc(subtitle||'')}</small></header><div class="track-chart-host"></div>`;
  chartsEl.append(card);
  if(role!=='primary')card.scrollIntoView({block:'nearest',inline:'nearest'});
  const host=card.querySelector('.track-chart-host');
  const chart=new TickChart(host,{symbol:title,market:incident},{interval:'1m',editable:false,volume:false,series:series||[],emptyMessage:emptyMessage||'Waiting for the first prints on this mint.',storageScope:'mint'});
  chart.__mintKey=key;
  chart.setTrackPaint(paint||{chartMarkers:[],bands:[],sideColumns:[]},hooks);
  charts.push(chart);
  if(charts.length>1)chartsEl.classList.add('is-multi');
  return chart;
 }
 function openWallet(chip){
  const address=walletAddress(chip?.address);
  const paint=chip?.paint||charts[0]?.trackLayer?.paint;
  if(!address||!paint||disposed||!shouldOpenWallet(primaryWallet,chip))return;
  const panes=panesForWallet(paint,address,chip.symbol);
  const specs=panes.length?panes:[{title:shortWallet(address),markers:[]}];
  for(const spec of specs)addPane({role:'wallet',wallet:address,title:spec.title,subtitle:'Same case · wallet chart',series:seriesFromPaint({...paint,chartMarkers:spec.markers}),paint:{...paint,chartMarkers:spec.markers,bands:[],sideColumns:[],muse:{summary:museLine(paint.muse?.summary),beside:null,replay:false,replayMs:900}}});
 }
 function noteStep(step){
  const raw=String(step?.label||'');
  const safe=walletAddress(raw)?shortWallet(raw):museLine(raw);
  const n=(Number(step?.index)||0)+1;
  const total=Number(step?.total)||n;
  caption.textContent=`Trail ${n} of ${total}${safe?` · ${safe}`:''}`;
 }
 function focusMove(focus){
  const layer=charts[0]?.trackLayer;
  if(!layer||disposed)return;
  const time=Number(focus?.time);
  if(Number.isFinite(time)){layer.cursor=time;charts[0].requestDraw?.();}
  const raw=String(focus?.label||'');
  const safe=walletAddress(raw)?shortWallet(raw):museLine(raw);
  if(safe)caption.textContent=safe;
  const address=walletAddress(focus?.wallet);
  if(!address||address===walletAddress(primaryWallet))return;
  openWallet({address,kind:focus?.kind||'tag',symbol:null,paint:layer.paint});
 }
 function openBridge(bridge){
  const paint=charts[0]?.trackLayer?.paint;
  const column=columnForBridge(paint,bridge);
  if(column)openColumn(column);
 }
 function openColumn(column){
  if(!column?.chain||disposed)return;
  addPane({role:'chain',title:`${column.label||column.chain} chart`,subtitle:'Same case · chain chart',paint:{schema:'ticking.chartMarkers.track.v1',track:true,listAddresses:false,chartMarkers:column.markers||[],bands:[],sideColumns:[],muse:{summary:'',beside:null,replay:false,replayMs:900}}});
 }
 function showSide(paint){
  const cols=paint.sideColumns||[];
  side.hidden=!cols.length;
  columnsEl.innerHTML=cols.map(c=>`<button type="button" data-track-column="${esc(c.id)}">Open ${esc(c.label||c.chain)} beside<small>Same case</small></button>`).join('');
 }
 const hooks={onWallet:openWallet,onBridge:openBridge,onFocus:focusMove,onStep:noteStep};
 let priceLocked=false;
 const chart=addPane({role:'primary',title:shortMint(symbol||mint)||'Mint',subtitle:sample?'Sample mint · trail shape, not a market print':'Mint price',emptyMessage:'Waiting for the first prints on this mint.'});
 replayBtn.addEventListener('click',()=>charts[0]?.trackLayer?.play(),{signal:abort.signal});
 columnsEl.addEventListener('click',e=>{const b=e.target.closest('[data-track-column]');if(!b)return;const paint=charts[0]?.trackLayer?.paint;openColumn((paint?.sideColumns||[]).find(c=>c.id===b.dataset.trackColumn));},{signal:abort.signal});
 bindChartAsk(root.querySelector('[data-track-ask]'),{signal:abort.signal,context:()=>({mint,chain,symbol,incident,query}),onAnswer:(incoming,error)=>{
  const answer=applyChartAnswer(charts[0]?.trackLayer?.paint,incoming,error);
  if(answer.replaced&&chart){
   if(!priceLocked){
    const series=seriesFromPaint(answer.paint);
    if(series.length){chart.setSeries(series,{preserve:false});fitMint(chart);}
   }
   chart.setTrackPaint(answer.paint,hooks);
   primaryWallet=walletAddress(answer.paint.chartMarkers?.find(m=>m.wallet)?.wallet)||primaryWallet;
   showSide(answer.paint);
   replayBtn.hidden=!answer.paint.muse?.replay;
   if(answer.paint.muse?.replay)chart.trackLayer?.play();
   const beside=answer.paint.muse?.beside?.wallet;
   if(beside)openWallet({address:beside,kind:'hop',paint:answer.paint});
  }
  summary.textContent=answer.summary;
  summary.dataset.trackAsk='chart';
 }});
 const run=++mountMintWorkspace.seq;
 (async()=>{
  try{
   const [{paint,error},candles]=await Promise.all([
    loadMintPaint({mint,chain,symbol,query,sample,signal:abort.signal}),
    sample?Promise.resolve([]):loadMintCandles(mint,{signal:abort.signal}),
   ]);
   if(disposed||run!==mountMintWorkspace.seq||!chart)return;
   const markers=paint.chartMarkers?.length||0;
   const price=candles.length?candles:(markers?seriesFromPaint(paint):[]);
   priceLocked=candles.length>0;
   if(price.length)chart.setSeries(price,{preserve:false});
   chart.setTrackPaint(paint,hooks);
   fitMint(chart);
   primaryWallet=walletAddress(paint.chartMarkers?.find(m=>m.wallet)?.wallet);
   const muse=museLine(paint.muse?.summary);
   summary.textContent=sample?'Sample mint on this workspace chart.':error&&!markers&&!candles.length?error:muse||(markers?`${markers} marks on this mint chart.`:(candles.length?'Mint price on this chart.':'Waiting for the first prints on this mint.'));
   const card=chart.container?.closest?.('[data-track-pane]')||root.querySelector('[data-track-pane]');
   const sub=card?.querySelector('small');
   if(sub)sub.textContent=sample?'Sample mint · trail shape, not a market print':candles.length?'Mint price':markers?'Trail shape on this mint chart.':'Mint price';
   replayBtn.hidden=!paint.muse?.replay;
   showSide(paint);
   if(paint.muse?.replay)charts[0]?.trackLayer?.play();
   if(!sample&&isTipChain(chain))scheduleTip();
  }catch(e){if(!disposed&&e?.name!=='AbortError')summary.textContent=e.message||'Mint chart unavailable.';}
 })();
 let tipTimer=0;
 const stopTip=()=>{if(tipTimer)clearTimeout(tipTimer);tipTimer=0;};
 const scheduleTip=()=>{
  if(disposed||sample||!isTipChain(chain))return;
  if(typeof document!=='undefined'&&document.hidden){tipTimer=setTimeout(scheduleTip,1000);return;}
  tipTimer=setTimeout(tickTip,Math.max(50,1000-(Date.now()%1000)));
 };
 const tickTip=async()=>{
  if(disposed)return;
  let pulled;
  try{pulled=await pullFastFeed({chain,symbol,mint,signal:abort.signal});}
  catch(e){if(e?.name==='AbortError')return;scheduleTip();return;}
  if(disposed)return;
  if(pulled?.ok){
   const current=chart?.series||[];
   const merged=applyTip(current,pulled.bars||[]);
   if(merged.series.length&&chart){
    const first=!current.some(bar=>bar.latencyPaintOnly);
    chart.tipBuckets=prewarmBuckets(merged.series);
    if(first){
     if(chart.options?.interval!=='1s')chart.set({interval:'1s'});
     chart.setSeries(merged.series,{preserve:false});
     fitMint(chart);
    }else chart.setSeries(merged.series,{preserve:true});
    priceLocked=true;
    const sub=chart.container?.closest?.('[data-track-pane]')?.querySelector('small');
    if(sub)sub.textContent=tipAgeLabel(chain,merged.series);
    if(/Waiting for the first prints|Mint price|Trail shape/.test(summary.textContent))summary.textContent='Tip price on this chart.';
   }
  }
  scheduleTip();
 };
 return ()=>{disposed=true;stopTip();abort.abort();for(const c of charts)c.destroy();};
}
mountMintWorkspace.seq=0;
