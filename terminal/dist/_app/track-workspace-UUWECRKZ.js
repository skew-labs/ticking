import {TickChart} from '/terminal/src/chart.js';
import {parseTrackPaint,seriesFromPaint,panesForWallet,shortWallet,walletAddress,sampleTrackPaint,columnForBridge,museLine} from './track-paint-I57ED4YC.js';
import {classifyTrackIntent,guardedResolveBody,trackResolveBody} from './track-intent-4WDFIVLB.js';
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
 return {ident,wallet,chain,sample:params.get('sample')==='1',incident:wallet?`wallet:${wallet}`:ident};
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
export function trackRouteFromResolve(body){
 if(!body||typeof body!=='object')return null;
 const route=typeof body.route==='string'?body.route:'';
 const request=body.handoff?.next?.request;
 const guard=typeof body.searchGuard==='string'?body.searchGuard:body.guard?.mode;
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
 if(intent.mode!=='track'&&intent.mode!=='dump')return null;
 const r=await fetchImpl('/api/public/intelligence/resolve',{method:'POST',headers:{'content-type':'application/json',accept:'application/json'},body:JSON.stringify(trackResolveBody({query})),signal});
 if(!r.ok)throw Error('Track resolve failed ('+r.status+').');
 const body=await r.json();
 acceptTrackHandoff(body);
 return body;
}
export function installTrackGuard(){
 if(typeof window==='undefined'||window.__tickingTrackGuard)return;
 window.__tickingTrackGuard=true;
 const orig=window.fetch.bind(window);
 window.fetch=async(input,init={})=>{
  let nextInit=init;
  const url=typeof input==='string'?input:input instanceof Request?input.url:'';
  if(url.includes('/api/public/intelligence/resolve')&&typeof init?.body==='string'){
   try{
    const parsed=JSON.parse(init.body);
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
    if(route&&(hash.includes('/auto')||hash.startsWith('#/tracker')||hash.includes('/search'))&&location.hash!==route)location.hash=route;
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
 root.innerHTML=`<section class="track-workspace" data-incident="${esc(incident||wallet||'workspace')}"><header class="track-workspace-bar"><div><p class="track-kicker">Workspace track</p><h1 data-track-title>${esc(shortWallet(wallet)||incident||'Workspace')}</h1><p data-track-caption class="track-caption"></p></div><p data-track-summary role="status">Reading the track paint…</p><button type="button" data-track-replay hidden>Replay trail</button></header><div class="track-layout"><div class="track-charts" data-track-charts></div><aside class="track-side" data-track-side hidden><h2>Beside this chart</h2><div data-track-columns></div></aside></div></section>`;
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
 replayBtn.addEventListener('click',()=>charts[0]?.trackLayer?.play(),{signal:abort.signal});
 columnsEl.addEventListener('click',e=>{const b=e.target.closest('[data-track-column]');if(!b)return;const paint=charts[0]?.trackLayer?.paint;openColumn((paint?.sideColumns||[]).find(c=>c.id===b.dataset.trackColumn));},{signal:abort.signal});
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
