/** Paint schema `ticking.chartMarkers.track.v1` on a TickChart time axis. */

export const TRACK_SCHEMA='ticking.chartMarkers.track.v1';
const ADDR=/0x[a-fA-F0-9]{40}/;
const KINDS=new Set(['buy','sell','transfer','bridge','swap','hop','frozen','cex','dump','tag']);

export function walletAddress(value){
 const m=String(value??'').match(ADDR);
 return m?m[0].toLowerCase():null;
}
export function timeMs(value){
 const n=Number(value);
 if(!Number.isFinite(n)||n<=0)return null;
 return n<1e12?Math.round(n*1000):Math.round(n);
}
function text(value,max=80){
 const s=String(value??'').replace(/\s+/g,' ').trim();
 return s?s.slice(0,max):'';
}
function kindOf(value){
 const raw=text(value,32).toLowerCase();
 if(KINDS.has(raw))return raw;
 if(raw.includes('buy'))return 'buy';
 if(raw.includes('sell')||raw.includes('dump'))return raw.includes('dump')?'dump':'sell';
 if(raw.includes('bridge'))return 'bridge';
 if(raw.includes('swap'))return 'swap';
 if(raw.includes('transfer'))return 'transfer';
 if(raw.includes('frozen'))return 'frozen';
 if(raw.includes('cex'))return 'cex';
 if(raw.includes('hop'))return 'hop';
 return 'tag';
}
function unwrap(input){
 if(!input||typeof input!=='object')return null;
 if(input.handoff?.next?.request)return input.handoff.next.request;
 if(input.next?.request)return input.next.request;
 if(input.request&&typeof input.request==='object')return input.request;
 if(input.paint&&typeof input.paint==='object')return unwrap(input.paint);
 return input;
}
function markerFrom(raw,index){
 if(!raw||typeof raw!=='object')return null;
 const time=timeMs(raw.time??raw.t??raw.ts??raw.timestamp??raw.at);
 if(time==null)return null;
 const kind=kindOf(raw.kind??raw.type??raw.tag??raw.role);
 const wallet=walletAddress(raw.wallet??raw.address??raw.trader??raw.from);
 const label=text(raw.label??raw.text??raw.name??raw.entity??(kind==='tag'?'':kind),64);
 const labeled=Boolean(label)&&raw.unknown!==true&&label!==wallet;
 const frozenCex=kind==='frozen'||kind==='cex';
 const solid=raw.solid===true||(frozenCex&&labeled&&raw.dashed!==true);
 const dashed=raw.dashed===true||raw.unknown===true||(frozenCex&&!solid);
 return {
  id:text(raw.id,80)||`${kind}:${time}:${wallet||index}`,
  kind,time,timeEnd:timeMs(raw.timeEnd??raw.t1??raw.end),
  label:label||kind,wallet,
  chain:text(raw.chain??raw.network,24).toLowerCase()||null,
  symbol:text(raw.symbol??raw.token??raw.mint,32)||null,
  src:text(raw.src??raw.bridge?.src,24)||null,
  dst:text(raw.dst??raw.bridge?.dst,24)||null,
  bandId:text(raw.bandId??raw.band,80)||null,
  solid,dashed,labeled,
  chip:kind==='hop'||kind==='frozen'||kind==='cex'||raw.role==='chip'||raw.chip===true,
 };
}
function bandFrom(raw,index,markers){
 if(!raw||typeof raw!=='object')return null;
 let t0=timeMs(raw.t0??raw.start??raw.fromTime??raw.time);
 let t1=timeMs(raw.t1??raw.end??raw.toTime??raw.timeEnd);
 const wallets=[];
 const push=value=>{const a=walletAddress(value);if(a&&!wallets.includes(a))wallets.push(a);};
 if(Array.isArray(raw.wallets))for(const w of raw.wallets)push(typeof w==='object'?w.wallet??w.address:w);
 push(raw.from);push(raw.to);push(raw.a);push(raw.b);push(raw.srcWallet);push(raw.dstWallet);
 if(Number.isInteger(raw.from)&&markers[raw.from])push(markers[raw.from].wallet);
 if(Number.isInteger(raw.to)&&markers[raw.to])push(markers[raw.to].wallet);
 if(t0==null&&wallets.length){
  const related=markers.filter(m=>m.wallet&&wallets.includes(m.wallet));
  if(related.length){t0=Math.min(...related.map(m=>m.time));t1=Math.max(...related.map(m=>m.timeEnd||m.time));}
 }
 if(t0==null)return null;
 if(t1==null||t1<t0)t1=t0;
 const src=text(raw.src??raw.bridge?.src??raw.sourceChain,24)||null;
 const dst=text(raw.dst??raw.bridge?.dst??raw.destChain,24)||null;
 const bridge=raw.bridge===true||raw.kind==='bridge'||raw.style==='dotted'||raw.dotted===true||Boolean(src&&dst&&src!==dst);
 return {
  id:text(raw.id,80)||`band:${index}:${t0}`,
  t0,t1,wallets,src,dst,bridge,dotted:bridge||raw.dotted===true||raw.style==='dotted',
  label:text(raw.label,64)||(src&&dst?`${src}→${dst}`:wallets.length>1?'Hop':'Hop'),
 };
}
function columnFrom(raw,index){
 if(!raw||typeof raw!=='object')return null;
 const chain=text(raw.chain??raw.network??raw.id,24).toLowerCase();
 if(!chain)return null;
 const markers=(Array.isArray(raw.markers)?raw.markers:Array.isArray(raw.chartMarkers)?raw.chartMarkers:[]).map(markerFrom).filter(Boolean).slice(0,80);
 return {
  id:text(raw.id,80)||`side:${chain}:${index}`,
  chain,label:text(raw.label??raw.title??chain,32),
  dotted:raw.dotted===true||raw.bridge===true,
  markers,
 };
}
function museFrom(raw,summary){
 const muse=raw?.muse&&typeof raw.muse==='object'?raw.muse:{};
 const replay=muse.replay??raw?.replay;
 const enabled=replay===true||(replay&&typeof replay==='object'&&replay.enabled!==false);
 const replayMs=Math.max(200,Math.min(8000,Number(muse.replayMs??replay?.replayMs??raw?.replayMs)||1500));
 const beside=muse.beside&&typeof muse.beside==='object'?muse.beside:null;
 return {
  summary:text(muse.summary??summary??raw?.museSummary,280),
  beside:beside?{cta:text(beside.cta??beside.label,80)||'Follow next hop',wallet:walletAddress(beside.wallet??beside.address)}:null,
  replay:Boolean(enabled),
  replayMs:enabled?replayMs:1500,
 };
}

/** Accept a track payload, a resolve body, or `handoff.next.request`. Address lists are dropped. */
export function parseTrackPaint(input){
 const raw=unwrap(input);
 if(!raw)return emptyPaint();
 const markers=(Array.isArray(raw.chartMarkers)?raw.chartMarkers:Array.isArray(raw.markers)?raw.markers:[]).slice(0,400).map(markerFrom).filter(Boolean);
 const bands=(Array.isArray(raw.bands)?raw.bands:[]).slice(0,80).map((b,i)=>bandFrom(b,i,markers)).filter(Boolean);
 const sideColumns=(Array.isArray(raw.sideColumns)?raw.sideColumns:Array.isArray(raw.side_columns)?raw.side_columns:[]).slice(0,12).map(columnFrom).filter(Boolean);
 const muse=museFrom(raw,raw.museSummary);
 return {
  schema:TRACK_SCHEMA,
  track:raw.track!==false,
  listAddresses:false,
  chartMarkers:markers,
  bands,sideColumns,muse,
  suppressedAddressList:raw.listAddresses===true||Array.isArray(raw.addresses)||Array.isArray(raw.addressList),
 };
}
function emptyPaint(){
 return {schema:TRACK_SCHEMA,track:false,listAddresses:false,chartMarkers:[],bands:[],sideColumns:[],muse:{summary:'',beside:null,replay:false,replayMs:1500},suppressedAddressList:false};
}
export function paintTimes(paint){
 const times=[];
 for(const m of paint?.chartMarkers||[]){times.push(m.time);if(m.timeEnd)times.push(m.timeEnd);}
 for(const b of paint?.bands||[]){times.push(b.t0,b.t1);}
 for(const c of paint?.sideColumns||[])for(const m of c.markers)times.push(m.time);
 return times.filter(t=>Number.isFinite(t));
}
export function seriesFromPaint(paint){
 const times=paintTimes(paint);
 if(!times.length)return [];
 const step=60_000,min=Math.min(...times),max=Math.max(...times);
 const start=Math.floor(min/step)*step;
 const end=Math.max(start+step*8,Math.ceil(max/step)*step);
 const series=[];
 for(let t=start;t<=end&&series.length<480;t+=step)series.push({time:t,open:1,high:1,low:1,close:1,volume:0,synthetic:true});
 return series;
}
export function replayFrames(paint){
 if(!paint?.muse?.replay)return [];
 const times=[...new Set(paintTimes(paint))].sort((a,b)=>a-b);
 if(!times.length)return [];
 const replayMs=paint.muse.replayMs||1500;
 const steps=Math.min(times.length,12);
 return Array.from({length:steps},(_,i)=>{
  const at=Math.round(i*replayMs/Math.max(1,steps-1));
  const time=times[Math.round(i*(times.length-1)/Math.max(1,steps-1))];
  return {at,time};
 });
}
export function panesForWallet(paint,wallet){
 const address=walletAddress(wallet);
 if(!address)return [];
 const mine=(paint?.chartMarkers||[]).filter(m=>m.wallet===address);
 const symbols=[...new Set(mine.map(m=>m.symbol).filter(Boolean))];
 if(!symbols.length)return [{wallet:address,symbol:null,title:shortWallet(address),markers:mine}];
 return symbols.map((symbol,i)=>({wallet:address,symbol,title:`${symbol} · ${shortWallet(address)}`,markers:mine.filter(m=>m.symbol===symbol||(!m.symbol&&i===0))}));
}
export function shortWallet(address){
 const a=walletAddress(address)||String(address||'');
 return a.length>12?`${a.slice(0,6)}…${a.slice(-4)}`:a||'wallet';
}

const TAG_COLOR={buy:'#239a82',sell:'#d36a79',dump:'#f97316',transfer:'#7564c3',bridge:'#60a5fa',swap:'#f5b041',hop:'#36c6bc',frozen:'#111827',cex:'#111827',tag:'#7b8496'};
export function tagColor(kind){return TAG_COLOR[kind]||TAG_COLOR.tag;}

export function layoutTrack(paint,view){
 const markers=paint?.chartMarkers||[];
 const xAt=time=>{
  if(!view?.indexAt||!view?.x)return null;
  const x=view.x(view.indexAt(time));
  const plot=view.width||view.plotWidth||0;
  if(!Number.isFinite(x)||x<-40||x>plot+40)return null;
  return x;
 };
 const bands=(paint?.bands||[]).map(b=>{
  const x0=xAt(b.t0),x1=xAt(b.t1);
  if(x0==null&&x1==null)return null;
  const left=Math.min(x0??x1,x1??x0),right=Math.max(x0??x1,x1??x0);
  return {...b,x:left,width:Math.max(2,right-left)};
 }).filter(Boolean);
 const tags=[],chips=[];
 markers.forEach((m,i)=>{
  const x=xAt(m.time);
  if(x==null)return;
  if(m.chip)chips.push({...m,x,lane:chips.length%3});
  else tags.push({...m,x,lane:i%3});
 });
 return {bands,tags:tags.slice(0,80),chips:chips.slice(0,80)};
}

function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

export class TrackPaintLayer{
 constructor(chart){this.chart=chart;this.paint=emptyPaint();this.hooks={};this.cursor=null;this.timer=0;this.canvas=document.createElement('canvas');this.canvas.className='track-paint-canvas';this.canvas.setAttribute('aria-hidden','true');this.ui=document.createElement('div');this.ui.className='track-paint-ui';this.onClick=e=>this.click(e);this.ui.addEventListener('click',this.onClick);this.chart.container.append(this.canvas,this.ui);}
 setHooks(hooks){this.hooks=hooks||{};}
 setPaint(paint){this.paint=paint?.schema?paint:parseTrackPaint(paint);this.cursor=null;this.stop();this.chart.requestDraw?.();}
 sync(){
  const chart=this.chart,width=chart.container.clientWidth,height=chart.container.clientHeight;
  if(!width||!height)return;
  const dpr=chart.dpr||Math.min(devicePixelRatio||1,2);
  this.canvas.width=Math.round(width*dpr);this.canvas.height=Math.round(height*dpr);this.canvas.style.width=width+'px';this.canvas.style.height=height+'px';
  const ctx=this.canvas.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,width,height);
  const view=chart.view;if(!view){this.ui.replaceChildren();return;}
  const laid=layoutTrack(this.paint,view);
  const top=chart.priceTop||18,bottom=(chart.priceTop||18)+(chart.priceHeight||height*.6);
  for(const b of laid.bands){
   ctx.save();ctx.fillStyle=b.dotted?'rgba(96,165,250,.13)':'rgba(54,198,188,.14)';ctx.fillRect(b.x,top,b.width,Math.max(8,bottom-top));
   ctx.strokeStyle=b.dotted?'#60a5fa':'#36c6bc';ctx.lineWidth=1.25;ctx.setLineDash(b.dotted?[3,4]:[]);ctx.strokeRect(b.x,top,b.width,Math.max(8,bottom-top));ctx.restore();
  }
  for(const t of laid.tags){
   ctx.save();ctx.strokeStyle=tagColor(t.kind);ctx.globalAlpha=.85;ctx.setLineDash([3,3]);ctx.beginPath();ctx.moveTo(t.x,top);ctx.lineTo(t.x,bottom);ctx.stroke();ctx.restore();
  }
  if(this.cursor!=null){
   const x=view.x(view.indexAt(this.cursor));
   if(Number.isFinite(x)){ctx.save();ctx.strokeStyle='#111827';ctx.lineWidth=1.5;ctx.beginPath();ctx.moveTo(x,top);ctx.lineTo(x,bottom);ctx.stroke();ctx.restore();}
  }
  this.renderUi(laid,top);
 }
 renderUi(laid,top){
  const bits=[];
  laid.tags.forEach(t=>{
   const y=top+8+t.lane*18;
   bits.push(`<span class="track-tag kind-${esc(t.kind)}" style="left:${t.x}px;top:${y}px">${esc(t.label)}</span>`);
  });
  laid.chips.forEach(c=>{
   const y=(this.chart.priceTop||18)+(this.chart.priceHeight||120)-28-c.lane*22;
   const face=c.solid?'solid':c.dashed?'dashed':'chip';
   bits.push(`<button type="button" class="track-chip ${face} kind-${esc(c.kind)}" style="left:${c.x}px;top:${y}px" data-track-wallet="${esc(c.wallet||'')}" data-track-chain="${esc(c.chain||'')}" data-track-symbol="${esc(c.symbol||'')}">${esc(c.label||shortWallet(c.wallet))}</button>`);
  });
  for(const b of laid.bands){
   if(!(b.src&&b.dst))continue;
   bits.push(`<button type="button" class="track-chip bridge" style="left:${b.x+8}px;top:${top+6}px" data-track-bridge="${esc(b.id)}" data-track-src="${esc(b.src)}" data-track-dst="${esc(b.dst)}">${esc(b.src)}→${esc(b.dst)}</button>`);
  }
  const beside=this.paint.muse?.beside;
  if(beside?.wallet)bits.push(`<button type="button" class="track-beside" data-track-wallet="${esc(beside.wallet)}">${esc(beside.cta||'Follow next hop')}</button>`);
  this.ui.innerHTML=bits.join('');
 }
 click(event){
  const bridge=event.target.closest('[data-track-bridge]');
  if(bridge){this.hooks.onBridge?.({id:bridge.dataset.trackBridge,src:bridge.dataset.trackSrc,dst:bridge.dataset.trackDst});return;}
  const chip=event.target.closest('[data-track-wallet]');
  if(!chip?.dataset.trackWallet)return;
  this.hooks.onWallet?.({address:chip.dataset.trackWallet,chain:chip.dataset.trackChain||null,symbol:chip.dataset.trackSymbol||null});
 }
 play(){
  this.stop();
  const frames=replayFrames(this.paint);
  if(!frames.length)return;
  if(matchMedia('(prefers-reduced-motion: reduce)').matches){this.cursor=frames.at(-1).time;this.chart.requestDraw();return;}
  const t0=performance.now();
  const step=now=>{
   const elapsed=now-t0;
   const frame=frames.reduce((a,f)=>elapsed>=f.at?f:a,frames[0]);
   this.cursor=frame.time;this.chart.requestDraw();
   if(elapsed<frames.at(-1).at)this.timer=requestAnimationFrame(step);
   else this.timer=0;
  };
  this.timer=requestAnimationFrame(step);
 }
 stop(){if(this.timer)cancelAnimationFrame(this.timer);this.timer=0;}
 destroy(){this.stop();this.ui.removeEventListener('click',this.onClick);this.canvas.remove();this.ui.remove();}
}

/** Preview fixture. Live routes use handoff or the track API. */
export function sampleTrackPaint(seed='0x278d858f05b94576c1e6f73285886876ff6ef8d2'){
 const wallet=walletAddress(seed)||'0x278d858f05b94576c1e6f73285886876ff6ef8d2';
 const hop='0x51c72848c68a965f66fa7a88855f9f7784502a7f';
 const t=Math.floor(Date.now()/60_000)*60_000-18*60_000;
 return parseTrackPaint({
  schema:TRACK_SCHEMA,track:true,listAddresses:false,
  museSummary:'Sample paint for the workspace chart.',
  chartMarkers:[
   {id:'buy',kind:'buy',time:t,wallet,label:'buy',symbol:'WETH',chain:'arbitrum'},
   {id:'hop',kind:'hop',time:t+4*60_000,wallet:hop,label:shortWallet(hop),chain:'arbitrum'},
   {id:'sell',kind:'sell',time:t+8*60_000,wallet:hop,label:'sell',symbol:'WETH',chain:'arbitrum'},
   {id:'bridge',kind:'bridge',time:t+12*60_000,wallet,label:'bridge',chain:'arbitrum',src:'arb',dst:'eth'},
   {id:'cex',kind:'cex',time:t+14*60_000,wallet,label:'CEX',solid:true,chain:'ethereum'},
   {id:'frozen',kind:'frozen',time:t+16*60_000,label:'unlabeled',unknown:true,dashed:true,chain:'ethereum'},
  ],
  bands:[{id:'hop-band',t0:t,t1:t+8*60_000,wallets:[wallet,hop],label:'A→B'},{id:'bridge-band',t0:t+12*60_000,t1:t+16*60_000,src:'arb',dst:'eth',dotted:true,label:'arb→eth',wallets:[wallet]}],
  sideColumns:[{id:'eth',chain:'ethereum',label:'ETH',dotted:true,markers:[{kind:'transfer',time:t+15*60_000,wallet,label:'transfer',chain:'ethereum'}]}],
  muse:{summary:'Sample paint for the workspace chart.',beside:{cta:'Follow next hop',wallet:hop},replay:true,replayMs:1500},
 });
}
