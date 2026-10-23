/** Paint schema `ticking.chartMarkers.track.v1` on a TickChart time axis. */
import {Lru} from './speed-pass.js';

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
 if(raw==='dump'||raw==='suspect'||raw.includes('dump')||raw.includes('suspect'))return 'dump';
 if(KINDS.has(raw))return raw;
 if(raw.includes('buy'))return 'buy';
 if(raw.includes('sell'))return 'sell';
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
 const request=input.handoff?.next?.request||input.next?.request;
 if(request&&typeof request==='object'){
  const dumpTagsOnly=request.dumpTagsOnly===true||request.mode==='dump'||input.mode==='dump'||input.guard?.dumpTagsOnly===true;
  const graph=request.graph||request.hopGraph||request.loop||input.graph||input.hopGraph||input.loop||input.trace?.graph;
  return {...request,graph,museSummary:request.museSummary||request.muse?.summary||input.message||'',mode:request.mode||input.mode,dumpTagsOnly};
 }
 if(input.request&&typeof input.request==='object'&&(input.request.chartMarkers||input.request.markers||input.request.track!=null))return unwrap({...input.request,message:input.request.museSummary||input.message});
 if(input.paint&&typeof input.paint==='object')return unwrap(input.paint);
 if(input.result&&typeof input.result==='object')return unwrap(input.result);
 if(input.data&&typeof input.data==='object'&&(input.data.handoff||input.data.chartMarkers||input.data.markers))return unwrap(input.data);
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
 const actionTag=kind==='buy'||kind==='sell'||kind==='transfer'||kind==='swap'||kind==='dump'||kind==='tag';
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
  chip:!actionTag&&(kind==='hop'||kind==='frozen'||kind==='cex'||kind==='bridge'||raw.role==='chip'||raw.chip===true),
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
 const replayMs=Math.max(200,Math.min(8000,Number(muse.replayMs??replay?.replayMs??raw?.replayMs)||900));
 const beside=muse.beside&&typeof muse.beside==='object'?muse.beside:null;
 return {
  summary:text(muse.summary??summary??raw?.museSummary,280),
  beside:beside?{cta:text(beside.cta??beside.label,80)||'Follow next hop',wallet:walletAddress(beside.wallet??beside.address)}:null,
  replay:Boolean(enabled),
  replayMs:enabled?replayMs:1500,
 };
}

const GRAPH_ORIGIN=1_700_000_000_000;
function graphSource(raw){
 if(!raw||typeof raw!=='object')return null;
 const g=raw.graph||raw.hopGraph||raw.flowGraph||raw.loop||raw.trace?.graph;
 if(!g||typeof g!=='object'||Array.isArray(g))return null;
 const nodes=Array.isArray(g.nodes)?g.nodes:Array.isArray(g.hops)?g.hops:[];
 const edges=Array.isArray(g.edges)?g.edges:Array.isArray(g.links)?g.links:[];
 const loops=Array.isArray(g.loops)?g.loops:[];
 if(!nodes.length&&!edges.length&&!loops.length)return null;
 return {nodes:nodes.slice(0,80),edges:edges.slice(0,80),loops:loops.slice(0,12),summary:text(g.summary??raw.museSummary,280)};
}
function nodeId(raw,index){return text(raw?.id??raw?.key??index,80)||String(index);}
function timedNodes(nodes){
 const rows=nodes.map((n,index)=>({n,index,id:nodeId(n,index),time:timeMs(n?.time??n?.t??n?.ts??n?.timestamp)}));
 if(!rows.length)return rows;
 if(rows.every(row=>row.time==null)){
  rows.forEach((row,index)=>{row.time=GRAPH_ORIGIN+index*60_000;});
  return rows;
 }
 let cursor=null;
 for(const row of rows){
  if(row.time==null&&cursor!=null)row.time=cursor+60_000;
  if(row.time!=null)cursor=row.time;
 }
 const first=rows.findIndex(row=>row.time!=null);
 for(let i=first-1;i>=0;i--)rows[i].time=rows[i+1].time-60_000;
 return rows;
}
function loopBand(edges,rows){
 const timeOf=new Map(rows.map(row=>[row.id,row.time]));
 const walletOf=new Map(rows.map(row=>[row.id,walletAddress(row.n?.wallet??row.n?.address)]));
 const next=new Map();
 for(const edge of edges){
  if(edge?.kind==='bridge'||edge?.type==='bridge'||edge?.bridge===true)continue;
  const from=text(edge?.from??edge?.source??edge?.a,80);
  const to=text(edge?.to??edge?.target??edge?.b,80);
  if(from&&to)next.set(from,to);
 }
 const seen=new Set();
 for(const start of next.keys()){
  if(seen.has(start))continue;
  const path=[];
  const index=new Map();
  let cur=start;
  while(cur&&!index.has(cur)&&next.has(cur)){
   index.set(cur,path.length);
   path.push(cur);
   seen.add(cur);
   cur=next.get(cur);
  }
  if(!cur||!index.has(cur)||path.length-index.get(cur)<2)continue;
  const cycle=path.slice(index.get(cur));
  const wallets=cycle.map(id=>walletOf.get(id)).filter(Boolean);
  const times=cycle.map(id=>timeOf.get(id)).filter(t=>Number.isFinite(t));
  if(wallets.length>=2&&times.length)return {id:'loop',t0:Math.min(...times),t1:Math.max(...times),wallets,label:'Loop'};
 }
 return null;
}
/** Project a hop graph onto chart markers. An empty graph adds nothing. */
export function paintFromGraph(graph){
 if(!graph?.nodes?.length&&!graph?.edges?.length)return null;
 const rows=timedNodes(graph.nodes||[]);
 const byId=new Map(rows.map(row=>[row.id,row]));
 const markers=rows.map(row=>markerFrom({
  ...row.n,
  id:row.id,
  time:row.time,
  kind:row.n?.kind??row.n?.type??row.n?.role??(row.index===0?'buy':'hop'),
  wallet:row.n?.wallet??row.n?.address,
  label:row.n?.label??row.n?.kind??(row.index===0?'buy':'hop'),
 },row.index)).filter(Boolean);
 const bands=[];
 const side=[];
 const seenSide=new Set();
 for(const [index,edge] of (graph.edges||[]).entries()){
  const from=byId.get(text(edge?.from??edge?.source??edge?.a,80));
  const to=byId.get(text(edge?.to??edge?.target??edge?.b,80));
  const fromW=walletAddress(edge?.fromWallet??edge?.from??from?.n?.wallet??from?.n?.address);
  const toW=walletAddress(edge?.toWallet??edge?.to??to?.n?.wallet??to?.n?.address);
  const t0=timeMs(edge?.t0??edge?.time??edge?.timestamp)??from?.time??null;
  const t1=timeMs(edge?.t1??edge?.timeEnd)??to?.time??t0;
  const src=text(edge?.src??edge?.sourceChain,24)||null;
  const dst=text(edge?.dst??edge?.destChain??edge?.chain,24)||null;
  const bridge=edge?.kind==='bridge'||edge?.type==='bridge'||edge?.bridge===true||Boolean(src&&dst&&src!==dst);
  if(t0!=null){
   const band=bandFrom({
    id:text(edge?.id,80)||`edge:${index}`,
    t0,t1:t1??t0,from:fromW,to:toW,wallets:[fromW,toW].filter(Boolean),
    src,dst,kind:bridge?'bridge':'hop',dotted:bridge,
    label:edge?.label||(bridge&&src&&dst?`${src}→${dst}`:'Hop'),
   },bands.length,markers);
   if(band)bands.push(band);
  }
  if(!bridge||!dst)continue;
  const chain=chainId(dst)||dst.toLowerCase();
  if(seenSide.has(chain))continue;
  seenSide.add(chain);
  const column=columnFrom({id:`side:${chain}`,chain,label:text(edge?.label&&!edge.label.includes('→')?edge.label:dst,32)||chain,dotted:true,markers:toW&&t1?[{kind:'transfer',time:t1,wallet:toW,label:'transfer',chain}]:[]},side.length);
  if(column)side.push(column);
  if(!markers.some(m=>m.kind==='bridge'&&chainId(m.dst)===chain)&&t0!=null){
   const chip=markerFrom({kind:'bridge',time:t0,label:src&&dst?`${src}→${dst}`:'bridge',src,dst,wallet:fromW},markers.length);
   if(chip)markers.push(chip);
  }
 }
 for(const [index,loop] of (graph.loops||[]).entries()){
  const ids=Array.isArray(loop)?loop:loop?.nodes||loop?.wallets||[];
  const wallets=ids.map(id=>walletAddress(byId.get(text(id,80))?.n?.wallet??byId.get(text(id,80))?.n?.address??id)).filter(Boolean);
  const times=ids.map(id=>byId.get(text(id,80))?.time).filter(t=>Number.isFinite(t));
  if(wallets.length<2||!times.length)continue;
  const band=bandFrom({id:`loop:${index}`,t0:Math.min(...times),t1:Math.max(...times),wallets,label:text(loop?.label,64)||'Loop'},bands.length,markers);
  if(band)bands.push(band);
 }
 if(!(graph.loops||[]).length){
  const cycle=loopBand(graph.edges||[],rows);
  if(cycle){
   const band=bandFrom(cycle,bands.length,markers);
   if(band)bands.push(band);
  }
 }
 if(!markers.length)return null;
 const wallets=[];
 for(const marker of markers){if(marker.wallet&&!wallets.includes(marker.wallet))wallets.push(marker.wallet);}
 return {
  chartMarkers:markers.slice(0,400),
  bands:bands.slice(0,80),
  sideColumns:side.slice(0,12),
  muse:{summary:graph.summary||'Hop loop on this chart.',beside:wallets[1]?{cta:'Follow next hop',wallet:wallets[1]}:null,replay:markers.length>=2,replayMs:900},
 };
}

/** Accept a track payload, a resolve body, or `handoff.next.request`. Address lists are dropped. */
export function parseTrackPaint(input){
 const raw=unwrap(input);
 if(!raw)return emptyPaint();
 const dumpOnly=raw.dumpTagsOnly===true||raw.mode==='dump';
 let markers=(Array.isArray(raw.chartMarkers)?raw.chartMarkers:Array.isArray(raw.markers)?raw.markers:[]).slice(0,400).map(markerFrom).filter(Boolean).filter(m=>!dumpOnly||m.kind==='dump');
 let bands=dumpOnly?[]:(Array.isArray(raw.bands)?raw.bands:[]).slice(0,80).map((b,i)=>bandFrom(b,i,markers)).filter(Boolean);
 let sideColumns=dumpOnly?[]:(Array.isArray(raw.sideColumns)?raw.sideColumns:Array.isArray(raw.side_columns)?raw.side_columns:[]).slice(0,12).map(columnFrom).filter(Boolean);
 const mapped=paintFromGraph(graphSource(raw));
 let museRaw=raw;
 if(!markers.length&&mapped){
  markers=mapped.chartMarkers.filter(m=>!dumpOnly||m.kind==='dump').slice(0,400);
  bands=dumpOnly?[]:mapped.bands;
  sideColumns=dumpOnly?[]:mapped.sideColumns;
  if(raw.muse==null&&raw.replay==null)museRaw={...raw,muse:mapped.muse,museSummary:raw.museSummary||mapped.muse.summary};
 }else if(markers.length&&mapped&&!dumpOnly){
  if(!bands.length)bands=mapped.bands;
  if(!sideColumns.length)sideColumns=mapped.sideColumns;
 }
 const muse=museFrom(museRaw,museRaw.museSummary);
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
 return {schema:TRACK_SCHEMA,track:false,listAddresses:false,chartMarkers:[],bands:[],sideColumns:[],muse:{summary:'',beside:null,replay:false,replayMs:900},suppressedAddressList:false};
}
export function paintTimes(paint){
 const times=[];
 for(const m of paint?.chartMarkers||[]){times.push(m.time);if(m.timeEnd)times.push(m.timeEnd);}
 for(const b of paint?.bands||[]){times.push(b.t0,b.t1);}
 for(const c of paint?.sideColumns||[])for(const m of c.markers)times.push(m.time);
 return times.filter(t=>Number.isFinite(t));
}
const NUDGE={buy:.04,sell:-.05,dump:-.12,swap:.02,hop:.01,transfer:-.01,bridge:-.03,cex:-.02,frozen:0,tag:0};
export function seriesFromPaint(paint){
 const times=paintTimes(paint);
 if(!times.length)return [];
 const step=60_000,min=Math.min(...times),max=Math.max(...times);
 const start=Math.floor(min/step)*step;
 const end=Math.max(start+step*8,Math.ceil(max/step)*step);
 const marks=new Map();
 const absorb=m=>{
  if(!m||!Number.isFinite(m.time))return;
  const bucket=Math.floor(m.time/step)*step;
  marks.set(bucket,(marks.get(bucket)||0)+(NUDGE[m.kind]||0));
 };
 for(const m of paint?.chartMarkers||[])absorb(m);
 for(const c of paint?.sideColumns||[])for(const m of c.markers||[])absorb(m);
 let price=1;
 const series=[];
 for(let t=start;t<=end&&series.length<480;t+=step){
  const open=price;
  price=Math.max(.2,price+(marks.get(t)||0));
  const close=price;
  series.push({time:t,open,high:Math.max(open,close),low:Math.min(open,close),close,volume:0,synthetic:true});
 }
 return series;
}
export function replayFrames(paint){
 if(!paint?.muse?.replay)return [];
 const times=[...new Set(paintTimes(paint))].sort((a,b)=>a-b);
 if(!times.length)return [];
 const replayMs=paint.muse.replayMs||1500;
 const steps=Math.min(times.length,8);
 return Array.from({length:steps},(_,i)=>{
  const at=Math.round(i*replayMs/Math.max(1,steps-1));
  const time=times[Math.round(i*(times.length-1)/Math.max(1,steps-1))];
  return {at,time};
 });
}
export function panesForWallet(paint,wallet,symbol){
 const address=walletAddress(wallet);
 if(!address)return [];
 const mine=(paint?.chartMarkers||[]).filter(m=>m.wallet===address);
 const asked=text(symbol,32);
 if(asked){
  const markers=mine.filter(m=>m.symbol===asked||!m.symbol);
  return [{wallet:address,symbol:asked,title:`${asked} · ${shortWallet(address)}`,markers}];
 }
 const symbols=[...new Set(mine.map(m=>m.symbol).filter(Boolean))];
 if(symbols.length<=1){
  const only=symbols[0]||null;
  return [{wallet:address,symbol:only,title:only?`${only} · ${shortWallet(address)}`:shortWallet(address),markers:mine}];
 }
 return symbols.map(item=>({wallet:address,symbol:item,title:`${item} · ${shortWallet(address)}`,markers:mine.filter(m=>m.symbol===item)}));
}
const CHAIN_ALIAS={eth:'ethereum',ethereum:'ethereum',arb:'arbitrum',arbitrum:'arbitrum',op:'optimism',optimism:'optimism',base:'base',sol:'solana',solana:'solana',matic:'polygon',polygon:'polygon',avax:'avalanche',avalanche:'avalanche',bnb:'bsc',bsc:'bsc'};
export function chainId(value){
 const s=text(value,32).toLowerCase().replace(/[^a-z0-9]/g,'');
 return s?(CHAIN_ALIAS[s]||s):null;
}
export function columnForBridge(paint,bridge){
 const dst=chainId(bridge?.dst||bridge?.chain||bridge?.label);
 const found=(paint?.sideColumns||[]).find(c=>chainId(c.chain)===dst||chainId(c.label)===dst);
 if(found)return found;
 if(!dst)return null;
 const markers=(paint?.chartMarkers||[]).filter(m=>chainId(m.chain)===dst||chainId(m.dst)===dst);
 return {id:`bridge:${dst}`,chain:dst,label:text(bridge?.dst||bridge?.label||dst,32),dotted:true,markers};
}
export function museLine(summary){
 const line=String(summary??'').replace(/0x[a-fA-F0-9]{40}/gi,'a wallet').replace(/\s+/g,' ').trim();
 return line.slice(0,180);
}
export function shortWallet(address){
 const a=walletAddress(address)||String(address||'');
 return a.length>12?`${a.slice(0,6)}…${a.slice(-4)}`:a||'wallet';
}

const TAG_COLOR={buy:'#239a82',sell:'#d36a79',dump:'#f97316',transfer:'#7564c3',bridge:'#60a5fa',swap:'#f5b041',hop:'#36c6bc',frozen:'#111827',cex:'#111827',tag:'#7b8496'};
export function tagColor(kind){return TAG_COLOR[kind]||TAG_COLOR.tag;}
export function isSharp(marker){
 if(!marker||marker.chip)return false;
 if(marker.kind==='dump')return true;
 return /whale|sharp/i.test(marker.label||'');
}
export function clockLabel(time,origin){
 if(!Number.isFinite(time)||!Number.isFinite(origin))return '';
 const mins=Math.round((time-origin)/60_000);
 return `${mins>=0?'+':''}${mins}m`;
}
export function assignLanes(points,gap=72){
 const ends=[];
 const order=points.map((p,i)=>({p,i})).sort((a,b)=>a.p.x-b.p.x||a.i-b.i);
 for(const {p} of order){
  let lane=0;
  while(lane<ends.length&&ends[lane]>p.x-gap)lane++;
  ends[lane]=p.x;
  p.lane=lane;
 }
 return points;
}
function spanLabel(t0,t1){
 const mins=Math.max(0,Math.round(((t1??t0)-t0)/60_000));
 return `${mins}m`;
}
function nearestMarker(paint,time){
 let best=null,dist=Infinity;
 for(const m of paint?.chartMarkers||[]){
  const d=Math.abs(m.time-time);
  if(d<dist){dist=d;best=m;}
 }
 return best;
}

export function layoutTrack(paint,view,chart){
 const markers=paint?.chartMarkers||[];
 const originMs=markers.reduce((min,m)=>Number.isFinite(m.time)?Math.min(min,m.time):min,Infinity);
 const origin=Number.isFinite(originMs)?originMs:null;
 const xAt=time=>{
  if(!view?.indexAt||!view?.x)return null;
  const x=view.x(view.indexAt(time));
  const plot=view.width||view.plotWidth||chart?.plotWidth||chart?.width||0;
  if(!Number.isFinite(x)||x<-40||x>plot+40)return null;
  return x;
 };
 const bands=(paint?.bands||[]).map(b=>{
  const x0=xAt(b.t0),x1=xAt(b.t1);
  if(x0==null&&x1==null)return null;
  const left=Math.min(x0??x1,x1??x0),right=Math.max(x0??x1,x1??x0);
  return {...b,x:left,width:Math.max(2,right-left),span:spanLabel(b.t0,b.t1)};
 }).filter(Boolean);
 const tags=[],chips=[];
 for(const m of markers){
  const x=xAt(m.time);
  if(x==null)continue;
  const item={...m,x,clock:clockLabel(m.time,origin),sharp:isSharp(m)};
  if(m.chip)chips.push(item);else tags.push(item);
 }
 return {bands,tags:assignLanes(tags).slice(0,80),chips:assignLanes(chips).slice(0,80),origin};
}

function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

export class TrackPaintLayer{
 constructor(chart){
  this.chart=chart;this.paint=emptyPaint();this.hooks={};this.cursor=null;this.timer=0;this.uiSig='';this.hotId=null;this.stepIndex=-1;this.layoutCache=new Lru(24);this.geoSig='';
  this.canvas=document.createElement('canvas');this.canvas.className='track-paint-canvas';this.canvas.setAttribute('aria-hidden','true');
  this.ui=document.createElement('div');this.ui.className='track-paint-ui';
  this.readout=document.createElement('div');this.readout.className='track-readout';this.readout.hidden=true;
  this.onClick=e=>this.click(e);this.onOver=e=>this.hover(e);this.onOut=e=>this.leave(e);
  this.ui.addEventListener('click',this.onClick);this.ui.addEventListener('pointerover',this.onOver);this.ui.addEventListener('pointerout',this.onOut);
  this.chart.container.append(this.canvas,this.ui,this.readout);
 }
 setHooks(hooks){this.hooks=hooks||{};}
 setPaint(paint){this.paint=paint?.schema?paint:parseTrackPaint(paint);this.cursor=null;this.uiSig='';this.geoSig='';this.hotId=null;this.stepIndex=-1;this.layoutCache=new Lru(24);this.readout.hidden=true;this.stop();this.chart.requestDraw?.();}
 replayClass(time){
  if(this.cursor==null||!Number.isFinite(time))return '';
  if(time<this.cursor)return ' is-past';
  if(time>this.cursor)return ' is-ahead';
  return ' is-now';
 }
 sync(){
  const chart=this.chart,width=chart.container.clientWidth,height=chart.container.clientHeight;
  if(!width||!height)return;
  const dpr=chart.dpr||Math.min(devicePixelRatio||1,2);
  const bitmapW=Math.round(width*dpr),bitmapH=Math.round(height*dpr);
  if(this.canvas.width!==bitmapW||this.canvas.height!==bitmapH){this.canvas.width=bitmapW;this.canvas.height=bitmapH;this.canvas.style.width=width+'px';this.canvas.style.height=height+'px';this.geoSig='';}
  const view=chart.view;if(!view){this.ui.replaceChildren();this.uiSig='';this.geoSig='';return;}
  const series=view.series||[];
  const layoutKey=`${(this.paint.chartMarkers||[]).map(m=>m.id+':'+m.time).join(',')}|${(this.paint.bands||[]).map(b=>b.id+':'+b.t0).join(',')}|${view.width}|${view.step}|${view.left}|${series.length}|${series[0]?.time??''}|${series.at?.(-1)?.time??''}`;
  let laid=this.layoutCache.get(layoutKey);
  if(!laid){laid=layoutTrack(this.paint,view,chart);this.layoutCache.set(layoutKey,laid);}
  const top=chart.priceTop||18,bottom=(chart.priceTop||18)+(chart.priceHeight||height*.6);
  const geo=`${layoutKey}|${top}|${bottom}|${this.hotId||''}|${this.cursor??''}|${bitmapW}`;
  if(geo===this.geoSig){this.renderUi(laid,top);return;}
  this.geoSig=geo;
  const ctx=this._ctx||(this._ctx=this.canvas.getContext('2d',{desynchronized:true}));ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,width,height);
  const wash=new Map(),ribbon=new Map(),stroke=new Map();
  for(const b of laid.bands){
   const washColor=b.dotted?'rgba(96,165,250,.10)':'rgba(54,198,188,.10)';
   const ribbonColor=b.dotted?'rgba(96,165,250,.32)':'rgba(54,198,188,.34)';
   const line=b.dotted?'#60a5fa':'#36c6bc';
   if(!wash.has(washColor))wash.set(washColor,[]);
   if(!ribbon.has(ribbonColor))ribbon.set(ribbonColor,[]);
   if(!stroke.has(line))stroke.set(line,[]);
   wash.get(washColor).push(b);ribbon.get(ribbonColor).push(b);stroke.get(line).push(b);
  }
  for(const [color,list] of wash){ctx.fillStyle=color;ctx.beginPath();for(const b of list)ctx.rect(b.x,top,b.width,Math.max(8,bottom-top));ctx.fill();}
  for(const [color,list] of ribbon){ctx.fillStyle=color;ctx.beginPath();for(const b of list)ctx.rect(b.x,top,b.width,22);ctx.fill();}
  for(const [color,list] of stroke){
   const dotted=color==='#60a5fa';
   ctx.strokeStyle=color;ctx.lineWidth=1.25;ctx.setLineDash(dotted?[3,4]:[]);ctx.beginPath();
   for(const b of list)ctx.rect(b.x,top,b.width,Math.max(8,bottom-top));
   ctx.stroke();
  }
  ctx.setLineDash([]);ctx.font='600 10px "DM Sans",sans-serif';ctx.textBaseline='middle';
  for(const b of laid.bands){
   if(b.src&&b.dst)continue;
   ctx.save();ctx.beginPath();ctx.rect(b.x+4,top,Math.max(0,b.width-8),22);ctx.clip();
   ctx.fillStyle=b.dotted?'#1d4ed8':'#0f766e';
   ctx.fillText(`${b.label||'Hop'} · ${b.span||''}`,b.x+6,top+11);ctx.restore();
  }
  const stems=new Map();
  for(const t of [...laid.tags,...laid.chips]){
   const sharp=t.sharp,hot=this.hotId===t.id;
   const color=sharp?'#f97316':tagColor(t.kind);
   const width=hot?3.25:(sharp?2.5:1.15);
   const dash=t.kind==='bridge'||t.dashed?'dash':'solid';
   const key=`${color}|${width}|${dash}|${hot?1:sharp?0.95:(t.chip?0.72:0.88)}`;
   if(!stems.has(key))stems.set(key,[]);
   stems.get(key).push(t);
  }
  for(const [key,list] of stems){
   const [color,width,dash,alpha]=key.split('|');
   ctx.save();ctx.strokeStyle=color;ctx.lineWidth=Number(width);ctx.globalAlpha=Number(alpha);ctx.setLineDash(dash==='dash'?[2,3]:[]);ctx.beginPath();
   for(const t of list){ctx.moveTo(t.x,top);ctx.lineTo(t.x,bottom);}
   ctx.stroke();ctx.restore();
  }
  if(this.cursor!=null){
   const x=view.x(view.indexAt(this.cursor));
   if(Number.isFinite(x)){ctx.save();ctx.strokeStyle='#111827';ctx.lineWidth=1.75;ctx.beginPath();ctx.moveTo(x,top);ctx.lineTo(x,bottom);ctx.stroke();ctx.restore();}
  }
  this.renderUi(laid,top);
 }
 renderUi(laid,top){
  const sig=[laid.tags,laid.chips].map(list=>list.map(t=>`${t.id}:${Math.round(t.x)}:${t.lane}:${this.replayClass(t.time)}`).join('|')).join('#')+`#${laid.bands.map(b=>`${b.id}:${Math.round(b.x)}`).join('|')}#${this.cursor??''}#${this.paint.muse?.beside?.wallet||''}`;
  if(sig===this.uiSig)return;
  this.uiSig=sig;
  const bits=[];
  for(const t of laid.tags){
   const y=top+8+t.lane*20;
   const face=`track-tag kind-${esc(t.kind)}${t.sharp?' sharp':''}${this.replayClass(t.time)}`;
   const clock=t.clock?` · ${t.clock}`:'';
   const attrs=`data-track-id="${esc(t.id)}" data-track-kind="${esc(t.kind)}" data-track-label="${esc(t.label)}" data-track-clock="${esc(t.clock||'')}"`;
   if(t.sharp){
    const aria=`Open ${t.label}${t.clock?` ${t.clock}`:''}`;
    bits.push(`<button type="button" class="${face}" style="left:${t.x}px;top:${y}px" ${attrs} data-track-focus="${t.time}" data-track-focus-wallet="${esc(t.wallet||'')}" aria-label="${esc(aria)}">${esc(t.label)}${esc(clock)}</button>`);
   }else bits.push(`<span class="${face}" style="left:${t.x}px;top:${y}px" ${attrs}>${esc(t.label)}${esc(clock)}</span>`);
  }
  for(const c of laid.chips){
   const y=(this.chart.priceTop||18)+(this.chart.priceHeight||120)-28-c.lane*22;
   const face=c.kind==='bridge'?'bridge':c.solid?'solid':c.dashed?'dashed':'chip';
   const bridge=c.kind==='bridge'?` data-track-bridge="${esc(c.id)}" data-track-src="${esc(c.src||'')}" data-track-dst="${esc(c.dst||c.chain||'')}"`:'';
   const label=c.kind==='bridge'?`Open ${c.dst||c.chain||'chain'} chart`:c.label||shortWallet(c.wallet);
   const hint=c.kind==='bridge'?'Open the chain chart beside':'Adds this wallet in this case';
   const clock=c.clock?` data-track-clock="${esc(c.clock)}"`:'';
   bits.push(`<button type="button" class="track-chip ${face} kind-${esc(c.kind)}${this.replayClass(c.time)}" style="left:${c.x}px;top:${y}px" title="${esc(hint)}" aria-label="${esc(hint)}" data-track-id="${esc(c.id)}" data-track-kind="${esc(c.kind)}" data-track-label="${esc(label)}" data-track-wallet="${esc(c.wallet||'')}" data-track-chain="${esc(c.chain||'')}" data-track-symbol="${esc(c.symbol||'')}"${clock}${bridge}>${esc(label)}</button>`);
  }
  for(const b of laid.bands){
   if(!(b.src&&b.dst))continue;
   bits.push(`<button type="button" class="track-chip bridge" style="left:${b.x+8}px;top:${top+4}px" title="Open the chain chart beside" aria-label="Open the ${esc(b.dst)} chart beside" data-track-id="${esc(b.id)}" data-track-bridge="${esc(b.id)}" data-track-src="${esc(b.src)}" data-track-dst="${esc(b.dst)}" data-track-label="${esc(b.label||b.dst)}">Open ${esc(b.dst)} chart</button>`);
  }
  const beside=this.paint.muse?.beside;
  if(beside?.wallet)bits.push(`<button type="button" class="track-beside" data-track-id="beside" data-track-wallet="${esc(beside.wallet)}" data-track-label="${esc(beside.cta||'Follow next hop')}">${esc(beside.cta||'Follow next hop')}</button>`);
  this.ui.innerHTML=bits.join('');
 }
 hover(event){
  const el=event.target.closest?.('[data-track-id]');
  const id=el?.dataset.trackId||null;
  if(id===this.hotId&&this.readout.hidden===!el)return;
  this.hotId=id;
  if(!el){this.readout.hidden=true;}
  else{
   const hint=el.dataset.trackBridge?'Open the chain chart beside':el.dataset.trackFocus?'Open this move on the chart':el.dataset.trackWallet?'Adds this wallet in this case':'';
   const clock=el.dataset.trackClock||'';
   this.readout.hidden=false;
   this.readout.textContent=[el.dataset.trackLabel||el.textContent,clock,hint].filter(Boolean).join(' · ');
  }
  this.chart.requestDraw?.();
 }
 leave(event){
  if(event.relatedTarget&&this.ui.contains(event.relatedTarget))return;
  if(!this.hotId&&this.readout.hidden)return;
  this.hotId=null;this.readout.hidden=true;this.chart.requestDraw?.();
 }
 click(event){
  const bridge=event.target.closest('[data-track-bridge]');
  if(bridge){this.hooks.onBridge?.({id:bridge.dataset.trackBridge,src:bridge.dataset.trackSrc,dst:bridge.dataset.trackDst});return;}
  const focus=event.target.closest('[data-track-focus]');
  if(focus){
   const time=Number(focus.dataset.trackFocus);
   this.hooks.onFocus?.({time:Number.isFinite(time)?time:null,kind:focus.dataset.trackKind||null,wallet:focus.dataset.trackFocusWallet||null,label:focus.dataset.trackLabel||''});
   return;
  }
  const chip=event.target.closest('[data-track-wallet]');
  if(!chip?.dataset.trackWallet)return;
  this.hooks.onWallet?.({address:chip.dataset.trackWallet,chain:chip.dataset.trackChain||null,symbol:chip.dataset.trackSymbol||null,kind:chip.dataset.trackKind||null,paint:this.paint});
 }
 emitStep(frame,index){
  this.cursor=frame.time;
  if(index===this.stepIndex)return;
  this.stepIndex=index;
  const marker=nearestMarker(this.paint,frame.time);
  const raw=marker?.label||'';
  const label=walletAddress(raw)?shortWallet(raw):raw;
  this.hooks.onStep?.({index,total:replayFrames(this.paint).length,time:frame.time,label});
  this.chart.requestDraw?.();
 }
 play(){
  this.stop();
  const frames=replayFrames(this.paint);
  if(!frames.length)return;
  this.stepIndex=-1;
  const reduced=typeof matchMedia==='function'&&matchMedia('(prefers-reduced-motion: reduce)').matches;
  if(reduced){this.emitStep(frames.at(-1),frames.length-1);return;}
  const t0=performance.now();
  const step=now=>{
   const elapsed=now-t0;
   let index=0;
   for(let i=0;i<frames.length;i++)if(elapsed>=frames[i].at)index=i;
   this.emitStep(frames[index],index);
   if(elapsed<frames.at(-1).at)this.timer=requestAnimationFrame(step);
   else {this.timer=0;this.cursor=frames.at(-1).time;}
  };
  this.timer=requestAnimationFrame(step);
 }
 stop(){if(this.timer)cancelAnimationFrame(this.timer);this.timer=0;}
 destroy(){this.stop();this.ui.removeEventListener('click',this.onClick);this.ui.removeEventListener('pointerover',this.onOver);this.ui.removeEventListener('pointerout',this.onOut);this.canvas.remove();this.ui.remove();this.readout.remove();}
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
   {id:'dump',kind:'dump',time:t+9*60_000,wallet,label:'dump',symbol:'WETH',chain:'arbitrum'},
   {id:'whale',kind:'tag',time:t+10*60_000,wallet:hop,label:'whale',symbol:'WETH',chain:'arbitrum'},
   {id:'bridge',kind:'bridge',time:t+12*60_000,wallet,label:'bridge',chain:'arbitrum',src:'arb',dst:'eth'},
   {id:'cex',kind:'cex',time:t+14*60_000,wallet,label:'CEX',solid:true,chain:'ethereum'},
   {id:'frozen',kind:'frozen',time:t+16*60_000,label:'unlabeled',unknown:true,dashed:true,chain:'ethereum'},
  ],
  bands:[{id:'hop-band',t0:t,t1:t+8*60_000,wallets:[wallet,hop],label:'A→B'},{id:'bridge-band',t0:t+12*60_000,t1:t+16*60_000,src:'arb',dst:'eth',dotted:true,label:'arb→eth',wallets:[wallet]}],
  sideColumns:[{id:'eth',chain:'ethereum',label:'ETH',dotted:true,markers:[{kind:'transfer',time:t+15*60_000,wallet,label:'transfer',chain:'ethereum'}]}],
  muse:{summary:'Sample paint for the workspace chart.',beside:{cta:'Follow next hop',wallet:hop},replay:true,replayMs:900},
 });
}

/** Preview fixture for a freshly minted token. Live mints use engine candles plus resolve markers. */
export function sampleMintPaint(seed='0x278d858f05b94576c1e6f73285886876ff6ef8d2'){
 const wallet=walletAddress(seed)||'0x278d858f05b94576c1e6f73285886876ff6ef8d2';
 const hop='0x51c72848c68a965f66fa7a88855f9f7784502a7f';
 const t=Math.floor(Date.now()/60_000)*60_000-12*60_000;
 return parseTrackPaint({
  schema:TRACK_SCHEMA,track:true,listAddresses:false,
  museSummary:'Sample mint on this workspace chart.',
  chartMarkers:[
   {id:'buy',kind:'buy',time:t,wallet,label:'buy',symbol:'PEPE',chain:'solana'},
   {id:'hop',kind:'hop',time:t+4*60_000,wallet:hop,label:shortWallet(hop),chain:'solana'},
   {id:'dump',kind:'dump',time:t+8*60_000,wallet,label:'dump',symbol:'PEPE',chain:'solana'},
   {id:'whale',kind:'tag',time:t+10*60_000,wallet:hop,label:'whale',symbol:'PEPE',chain:'solana'},
  ],
  bands:[{id:'hop-band',t0:t,t1:t+8*60_000,wallets:[wallet,hop],label:'A→B'}],
  sideColumns:[],
  muse:{summary:'Sample mint on this workspace chart.',beside:{cta:'Follow next hop',wallet:hop},replay:true,replayMs:900},
 });
}
