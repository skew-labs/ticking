import {TickChart} from './chart.js';
import {parseTrackPaint,seriesFromPaint,panesForWallet,shortWallet,walletAddress,sampleTrackPaint} from './track-paint.js';
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
  const r=await fetchImpl('/api/public/intelligence/resolve',{method:'POST',headers:{'content-type':'application/json',accept:'application/json'},body:JSON.stringify({query}),signal});
  if(!r.ok)throw Error('Track resolve failed ('+r.status+').');
  const body=await r.json();
  storeHandoff(body);
  live=parseTrackPaint(body);
 }catch(e){if(e?.name==='AbortError')throw e;error=e.message||'Track resolve unavailable.';}
 const paint=richer(live,stored?.track?stored:null)||parseTrackPaint(null);
 return {paint,source:live?.chartMarkers?.length?'resolve':stored?.chartMarkers?.length?'handoff':'resolve',error};
}

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
 root.innerHTML=`<section class="track-retired"><h1>Tracking opens on the workspace chart</h1><p>One incident stays on one workspace. Paste a wallet to open its chart.</p><form><label>Wallet<input name="wallet" required autocomplete="off" spellcheck="false" placeholder="0x…"></label><button type="submit">Open workspace</button></form><p role="status"></p></section>`;
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

export function mountTrackWorkspace(root,{incident,wallet,chain,sample}={}){
 ensureTrackStyle();
 const abort=new AbortController();
 let disposed=false;
 const charts=[];
 const opened=new Set();
 root.innerHTML=`<section class="track-workspace" data-incident="${esc(incident||wallet||'workspace')}"><header class="track-workspace-bar"><div><p class="track-kicker">Workspace track</p><h1 data-track-title>${esc(shortWallet(wallet)||incident||'Workspace')}</h1></div><p data-track-summary role="status">Reading the track paint…</p><button type="button" data-track-replay hidden>Replay</button></header><div class="track-layout"><div class="track-charts" data-track-charts></div><aside class="track-side" data-track-side hidden><h2>Bridge columns</h2><div data-track-columns></div></aside></div></section>`;
 const summary=root.querySelector('[data-track-summary]');
 const chartsEl=root.querySelector('[data-track-charts]');
 const side=root.querySelector('[data-track-side]');
 const columnsEl=root.querySelector('[data-track-columns]');
 const replayBtn=root.querySelector('[data-track-replay]');
 function addPane({title,subtitle,paint,wallet:paneWallet,role}){
  const key=`${role}:${paneWallet||''}:${title}`;
  if(opened.has(key))return;
  opened.add(key);
  const card=document.createElement('article');
  card.className='track-pane';
  card.dataset.trackPane=role;
  card.innerHTML=`<header><b>${esc(title)}</b><small>${esc(subtitle||'')}</small></header><div class="track-chart-host"></div>`;
  chartsEl.append(card);
  const host=card.querySelector('.track-chart-host');
  const series=seriesFromPaint(paint);
  const chart=new TickChart(host,{symbol:title,market:incident||title},{interval:'1m',editable:false,volume:false,series,emptyMessage:'No track markers in this window.',storageScope:'track'});
  chart.setTrackPaint(paint,{onWallet:openWallet,onBridge:openBridge});
  charts.push(chart);
  if(charts.length>1)chartsEl.classList.add('is-multi');
  return chart;
 }
 function openWallet(chip){
  const address=walletAddress(chip?.address);
  const paint=charts[0]?.trackLayer?.paint;
  if(!address||!paint||disposed)return;
  const panes=panesForWallet(paint,address);
  const specs=panes.length?panes:[{title:shortWallet(address),markers:[]}];
  for(const spec of specs)addPane({role:'wallet',wallet:address,title:spec.title,subtitle:'Same workspace · wallet chart',paint:{...paint,chartMarkers:spec.markers,bands:[],sideColumns:[],muse:{summary:paint.muse?.summary||'',beside:null,replay:false,replayMs:1500}}});
 }
 function openBridge(bridge){
  const paint=charts[0]?.trackLayer?.paint;
  const column=(paint?.sideColumns||[]).find(c=>c.chain===String(bridge.dst||'').toLowerCase())||{chain:bridge.dst,label:bridge.dst,markers:[],dotted:true};
  openColumn(column);
 }
 function openColumn(column){
  if(!column?.chain||disposed)return;
  addPane({role:'chain',title:`${column.label||column.chain} chart`,subtitle:'Same workspace · bridge column',paint:{schema:paintSchema(),track:true,listAddresses:false,chartMarkers:column.markers||[],bands:[],sideColumns:[],muse:{summary:'',beside:null,replay:false,replayMs:1500}}});
 }
 function paintSchema(){return 'ticking.chartMarkers.track.v1';}
 function showSide(paint){
  const cols=paint.sideColumns||[];
  side.hidden=!cols.length;
  columnsEl.innerHTML=cols.map(c=>`<button type="button" data-track-column="${esc(c.id)}">${esc(c.label||c.chain)}<small>${c.dotted?'Dotted bridge column':'Column'}</small></button>`).join('');
 }
 replayBtn.addEventListener('click',()=>charts[0]?.trackLayer?.play(),{signal:abort.signal});
 columnsEl.addEventListener('click',e=>{const b=e.target.closest('[data-track-column]');if(!b)return;const paint=charts[0]?.trackLayer?.paint;openColumn((paint?.sideColumns||[]).find(c=>c.id===b.dataset.trackColumn));},{signal:abort.signal});
 const run=++mountTrackWorkspace.seq;
 (async()=>{
  try{
   const {paint,source,error}=await loadTrackPaint({wallet,chain,sample,signal:abort.signal});
   if(disposed||run!==mountTrackWorkspace.seq)return;
   const n=paint.chartMarkers.length;
   const muse=paint.muse?.summary||'';
   summary.textContent=sample?'Sample paint on this workspace chart.':error&&!n?error:muse||(n?`${n} markers on this workspace chart.`:'No track markers in this window.');
   if(source)summary.dataset.trackSource=source;
   replayBtn.hidden=!paint.muse?.replay;
   addPane({role:'primary',wallet,title:shortWallet(wallet)||'Track',subtitle:sample?'Preview fixture · time axis':'Track markers · time axis',paint});
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
