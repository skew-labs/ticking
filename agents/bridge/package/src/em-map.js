// Ticking Flow, Economic Machine edition (2D only). Chains are continents, wallet kinds are regions, protocol groups are
// districts, wallets are points, hubs are cities with their protocol artwork and money flows are roads. Layout: the Economic
// Machine territory build (300K most active wallets; 30K on phones), shipped as per-chain quantized tiles that load lazily.
// Every card reads /api/public/em through em-api.js. Keyboard: arrows pan, +/- zoom, 0 fits, Enter opens the nearest wallet.
import {em,esc,short,num,walletIcon,walletCard,protocolOf,glyph,cat,CHAINS,injectStyles,isAddress,normAddress,stateHTML,artwork,protocolArt} from './em-api.js';

const CHAIN_KEYS=['arbitrum','robinhood'];
const ICONS={search:'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',plus:'M12 5v14M5 12h14',minus:'M5 12h14',reset:'M20 7a9 9 0 1 0 1 8M20 2v6h-6',expand:'M8 3H3v5m13-5h5v5M3 16v5h5m8 0h5v-5'};
const icon=n=>`<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="${ICONS[n]}" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
const hex=h=>{const v=parseInt(h.slice(1),16);return [(v>>16)&255,(v>>8)&255,v&255];};
const GLYPH_PATH={},CELL=48,BASE='/assets/em/map/';

async function getBuffer(url,signal){const r=await fetch(url,{signal,credentials:'omit'});if(!r.ok)throw Error('Map data could not load ('+r.status+').');return r.arrayBuffer();}
function decodeDots(buf){
 const v=new DataView(buf);if(String.fromCharCode(v.getUint8(0),v.getUint8(1),v.getUint8(2),v.getUint8(3))!=='EMD1')throw Error('Unsupported map tile.');
 const n=v.getUint32(4,true),x0=v.getFloat32(8,true),y0=v.getFloat32(12,true),x1=v.getFloat32(16,true),y1=v.getFloat32(20,true),off=24;
 const qx=new Uint16Array(buf.slice(off,off+2*n)),qy=new Uint16Array(buf.slice(off+2*n,off+4*n)),S8=new Uint8Array(buf,off+4*n,n),CAT=new Uint8Array(buf,off+5*n,n);
 const X=new Float32Array(n),Y=new Float32Array(n),S=new Float32Array(n),sx=(x1-x0)/65535,sy=(y1-y0)/65535,grid=new Map();
 for(let i=0;i<n;i++){X[i]=x0+qx[i]*sx;Y[i]=y0+qy[i]*sy;S[i]=S8[i]/255;const key=Math.floor(X[i]/CELL)+':'+Math.floor(Y[i]/CELL);let a=grid.get(key);if(!a)grid.set(key,a=[]);a.push(i);}
 return {n,X,Y,S,CAT,grid,NODE:null,nodeIndex:null};
}

export async function mountEmMap(root,{signal:parentSignal}={}){
 injectStyles();injectMapStyles();performance.mark?.('em-map-start');
 const abort=new AbortController(),{signal}=abort;parentSignal?.addEventListener('abort',()=>abort.abort(),{once:true});
 const variant=Math.min(screen.width,screen.height)<600||(navigator.deviceMemory||8)<=4?'phone':'desktop';
 root.innerHTML=`<div class="em-map-boot">${stateHTML('loading','Opening the Economic Machine wallet map…')}</div>`;
 let T;
 try{[T]=await Promise.all([fetch(BASE+`terrain-${variant}.json`,{signal,credentials:'omit'}).then(r=>{if(!r.ok)throw Error('Map data could not load ('+r.status+').');return r.json();}),artwork().catch(()=>null)]);}
 catch(e){abort.abort();if(parentSignal?.aborted)return()=>{};throw e;}   // flow-home renders its own "Try again" state
 if(signal.aborted)return()=>{};
 const catKeys=T.categories,catColor=catKeys.map(k=>hex(cat(k)[2])),D=[null,null],loading=[null,null];
 const poly=p=>{const path=new Path2D();p.forEach(([x,y],i)=>i?path.lineTo(x,y):path.moveTo(x,y));path.closePath();return path;};
 for(const o of [...T.continents,...T.regions,...T.districts])o.path=poly(o.poly);
 const districtCity=new Map();for(const c of T.cities)if(c.rank===0)districtCity.set(c.district,c);
 for(const c of T.cities){c.proto=protocolOf(c.name||T.districts[c.district]?.name||'');c.catKey=catKeys[T.regions[T.districts[c.district]?.region]?.cat]||'unknown';}
 const box=o=>{let x0=Infinity,y0=Infinity,x1=-Infinity,y1=-Infinity;for(const [x,y] of o.poly){x0=Math.min(x0,x);y0=Math.min(y0,y);x1=Math.max(x1,x);y1=Math.max(y1,y);}return {x0,y0,x1,y1};};
 const contBox=T.continents.map(box);
 let chainIx=1,cam={x:0,y:0,k:1},target=null,selected=null,hover=null,dragging=null,pinch=null,frame=0,settle=0,detail=null,overview=[null,null];
 const logoCache=new Map(),frames=[];
 const logo=c=>{const art=protocolArt(c.proto?.key);if(!art)return null;if(logoCache.has(art))return logoCache.get(art);const img=new Image();img.decoding='async';img.src=art;img.onload=()=>draw();logoCache.set(art,img);return img;};

 root.innerHTML=`<header class="map-chrome"><div class="map-brand"><span class="map-plain-mark" aria-hidden="true"><i></i><i></i><i></i></span><span>ticking <b>Flow</b></span></div>
 <div class="map-chains" role="tablist" aria-label="Map network"><button type="button" role="tab" data-map-chain="solana" aria-selected="false" aria-disabled="true" tabindex="-1" class="em-chain-soon" title="Solana wallets are not mapped yet"><img class="map-chain-logo" src="/assets/ecosystem/solana.svg" alt="" width="24" height="24"><span>Solana</span><small>Soon</small></button>${CHAIN_KEYS.map((k,i)=>`<button type="button" role="tab" data-map-chain="${k}" aria-selected="${i===chainIx}" tabindex="${i===chainIx?0:-1}"><img class="map-chain-logo map-chain-logo--${k}" src="${CHAINS[k].logo}" alt="" width="24" height="24"><span>${CHAINS[k].name}</span></button>`).join('')}</div>
 <span class="em-map-badge">Economic Machine map</span><button type="button" class="map-icon-button map-expand" aria-label="Expand wallet map" aria-pressed="false" data-em-expand>${icon('expand')}</button></header>
 <div class="map-workspace"><aside class="map-addresses" aria-label="Hubs and search"><form class="map-search" role="search"><label class="sr-only" for="em-map-search">Find a wallet, protocol or hub</label>${icon('search')}<input id="em-map-search" placeholder="Find a wallet or protocol" autocomplete="off" spellcheck="false" maxlength="64"><button type="submit" aria-label="Find">↵</button></form><div class="map-search-status" role="status" aria-live="polite"></div><div class="map-list-title"><span data-em-list-title>Hubs</span><b data-em-list-count></b></div><div class="em-hub-list" role="list"></div><div class="map-scope-note"><span class="map-record-dot"></span><span>${num(T.n)} most active wallets drawn · 145M mapped</span></div></aside>
 <div class="map-center em-map-center"><div class="map-view-label"><span class="map-record-dot"></span><span data-em-label></span><span class="map-view-sub">Wallet map · 2D</span></div><canvas class="em-map-canvas" role="application" aria-roledescription="wallet map" aria-label="Wallet map. Arrow keys move, plus and minus zoom, 0 fits the chain, Enter opens the wallet nearest the centre." tabindex="0"></canvas><div class="em-map-tip" hidden></div><div class="em-map-loading" hidden>${stateHTML('loading','Loading wallets…')}</div>
 <div class="em-map-controls"><button type="button" class="em-zoom" data-em-zoom="in" aria-label="Zoom in">${icon('plus')}</button><button type="button" class="em-zoom" data-em-zoom="out" aria-label="Zoom out">${icon('minus')}</button><button type="button" class="em-zoom" data-em-zoom="reset" aria-label="Fit the chain">${icon('reset')}</button></div>
 <div class="em-map-legend" aria-hidden="true">${catKeys.filter(k=>!['nft_contract','unknown','contract_other','cex_deposit'].includes(k)).map(k=>`<span><i style="background:${cat(k)[2]}"></i>${esc(cat(k)[0])}</span>`).join('')}</div></div>
 <aside class="map-inspector" aria-label="Selected wallet" aria-live="polite"><div class="map-inspector-content"></div></aside></div>`;
 const $=s=>root.querySelector(s),canvas=$('.em-map-canvas'),ctx=canvas.getContext('2d'),tip=$('.em-map-tip'),list=$('.em-hub-list'),inspector=$('.map-inspector-content'),status=$('.map-search-status');
 let W=0,H=0,dpr=1;
 function resize(){const r=canvas.getBoundingClientRect();dpr=Math.min(2,devicePixelRatio||1);W=Math.max(1,r.width);H=Math.max(1,r.height);canvas.width=Math.round(W*dpr);canvas.height=Math.round(H*dpr);overview=[null,null];detail=null;if(D[chainIx])overview[chainIx]=renderDots(fitCam(chainIx),chainIx);draw();}
 const fitCam=ix=>{const b=contBox[ix],pad=W<520?1.08:1.16;return {x:(b.x0+b.x1)/2,y:(b.y0+b.y1)/2,k:Math.min(W/((b.x1-b.x0)*pad),H/((b.y1-b.y0)*pad))};};
 const toScreen=(x,y,c=cam)=>[(x-c.x)*c.k+W/2,(y-c.y)*c.k+H/2],toWorld=(sx,sy)=>[(sx-W/2)/cam.k+cam.x,(sy-H/2)/cam.k+cam.y];
 const fitK=()=>fitCam(chainIx).k;

 async function loadChain(ix){
  if(D[ix])return D[ix];if(!loading[ix]){$('.em-map-loading').hidden=false;loading[ix]=getBuffer(BASE+`dots-${variant}-${ix}.bin`,signal).then(decodeDots).then(d=>{D[ix]=d;return d;}).finally(()=>{$('.em-map-loading').hidden=true;});}
  try{return await loading[ix];}catch(e){loading[ix]=null;if(!signal.aborted)status.textContent=e.message;throw e;}
 }
 async function loadNodes(ix){const d=await loadChain(ix);if(d.NODE)return d;if(!d._nodes)d._nodes=getBuffer(BASE+`nodes-${variant}-${ix}.bin`,signal).then(b=>{d.NODE=new Uint32Array(b);d.nodeIndex=new Map();for(let i=0;i<d.n;i++)d.nodeIndex.set(d.NODE[i],i);return d;}).catch(e=>{d._nodes=null;throw e;});return d._nodes;}

 // ---- dots: a cached bitmap per camera, re-rendered crisply once the camera settles ----
 function renderDots(c,ix){
  const d=D[ix];if(!d)return null;const cv=document.createElement('canvas');cv.width=canvas.width;cv.height=canvas.height;const g=cv.getContext('2d');
  g.setTransform(c.k*dpr,0,0,c.k*dpr,(W/2-c.x*c.k)*dpr,(H/2-c.y*c.k)*dpr);
  const wx0=(0-W/2)/c.k+c.x,wy0=(0-H/2)/c.k+c.y,wx1=(W-W/2)/c.k+c.x,wy1=(H-H/2)/c.k+c.y;
  const paths=catKeys.map(()=>new Path2D()),zoomPx=Math.min(3.2,Math.max(.55,c.k/fitCam(ix).k*.55));
  const visit=i=>{const x=d.X[i],y=d.Y[i];if(x<wx0||x>wx1||y<wy0||y>wy1)return;const rp=(.55+1.35*d.S[i])*zoomPx,r=rp/c.k;
   if(rp<1.1)paths[d.CAT[i]].rect(x-r,y-r,2*r,2*r);else{paths[d.CAT[i]].moveTo(x+r,y);paths[d.CAT[i]].arc(x,y,r,0,6.2832);}};
  if((wx1-wx0)*(wy1-wy0)/(CELL*CELL)>d.grid.size*.6){for(let i=0;i<d.n;i++)visit(i);}
  else for(let gx=Math.floor(wx0/CELL);gx<=Math.floor(wx1/CELL);gx++)for(let gy=Math.floor(wy0/CELL);gy<=Math.floor(wy1/CELL);gy++){const a=d.grid.get(gx+':'+gy);if(a)for(const i of a)visit(i);}
  const dark=document.documentElement.dataset.theme==='dark';
  paths.forEach((p,k)=>{const [r,gg,b]=catColor[k]||[160,164,184];g.fillStyle=`rgba(${r},${gg},${b},${dark?.82:.7})`;g.fill(p);});
  return {canvas:cv,cam:{...c},chain:ix};
 }
 function blit(cache){const f=cam.k/cache.cam.k,ox=(cache.cam.x-cam.x)*cam.k+W/2-W/2*f,oy=(cache.cam.y-cam.y)*cam.k+H/2-H/2*f;ctx.drawImage(cache.canvas,ox*dpr,oy*dpr,canvas.width*f,canvas.height*f);}

 function draw(){if(!frame)frame=requestAnimationFrame(paint);}
 function paint(now){
  frame=0;if(!W)return;const t0=performance.now();const dark=document.documentElement.dataset.theme==='dark',rel=cam.k/fitK();
  ctx.setTransform(1,0,0,1,0,0);ctx.clearRect(0,0,canvas.width,canvas.height);
  ctx.setTransform(cam.k*dpr,0,0,cam.k*dpr,(W/2-cam.x*cam.k)*dpr,(H/2-cam.y*cam.k)*dpr);
  const px=1/cam.k;
  for(const c of T.continents){ctx.fillStyle=dark?'#1f2026':'#ffffff';ctx.shadowColor=dark?'#0008':'#5658e81f';ctx.shadowBlur=28*cam.k*dpr;ctx.fill(c.path);ctx.shadowBlur=0;ctx.lineWidth=1.2*px;ctx.strokeStyle=dark?'#34363d':'#e4e1f7';ctx.stroke(c.path);}
  for(const r of T.regions){const [cr,cg,cb]=catColor[r.cat]||[160,164,184];ctx.fillStyle=`rgba(${cr},${cg},${cb},${dark?.09:.075})`;ctx.fill(r.path);ctx.lineWidth=2.2*px;ctx.strokeStyle=dark?'#2a2c33':'#ffffff';ctx.stroke(r.path);}
  if(rel>1.6){ctx.lineWidth=1*px;ctx.strokeStyle=dark?'#ffffff12':'#5658e81a';for(const d of T.districts)ctx.stroke(d.path);}
  ctx.setTransform(1,0,0,1,0,0);
  const fresh=detail&&detail.chain===chainIx&&!detail.stale;
  if(!fresh&&overview[chainIx])blit(overview[chainIx]);if(detail&&detail.chain===chainIx)blit(detail);
  ctx.setTransform(cam.k*dpr,0,0,cam.k*dpr,(W/2-cam.x*cam.k)*dpr,(H/2-cam.y*cam.k)*dpr);
  const show=Math.max(0,Math.min(1,(rel-.75)/1.1));
  if(show){const maxU=Math.max(1,...T.roads.map(r=>r.usd||0)),limit=Math.round(16+40*Math.min(1,(rel-.75)/5)),per=[0,0];
   for(const r of T.roads){if(per[r.chain]++>=limit)continue;const a=districtCity.get(r.a)||T.districts[r.a],b=districtCity.get(r.b)||T.districts[r.b];if(!a||!b)continue;
    const ax=a.x??a.c[0],ay=a.y??a.c[1],bx=b.x??b.c[0],by=b.y??b.c[1],mx=(ax+bx)/2-(by-ay)*.18,my=(ay+by)/2+(bx-ax)*.18;
    ctx.beginPath();ctx.moveTo(ax,ay);ctx.quadraticCurveTo(mx,my,bx,by);ctx.lineWidth=(.6+2.2*Math.sqrt((r.usd||0)/maxU))*px;ctx.strokeStyle=`rgba(86,88,232,${(dark?.32:.22)*show})`;ctx.stroke();}}
  ctx.setTransform(1,0,0,1,0,0);ctx.scale(dpr,dpr);
  const placed=[];const free=(x,y,w,h)=>{if(x<4||y<4||x+w>W-4||y+h>H-4)return false;for(const p of placed)if(x<p[0]+p[2]&&x+w>p[0]&&y<p[1]+p[3]&&y+h>p[1])return false;placed.push([x,y,w,h]);return true;};
  ctx.textBaseline='middle';
  if(rel<1.15)for(const [i,c] of T.continents.entries()){const [sx,sy]=toScreen((contBox[i].x0+contBox[i].x1)/2,contBox[i].y0);ctx.font='600 15px "DM Sans",sans-serif';const w=ctx.measureText(c.name).width;if(free(sx-w/2,sy-34,w,20)){ctx.fillStyle=dark?'#eceef1':'#1c2233';ctx.fillText(c.name,sx-w/2,sy-24);}}
  if(rel<3.2)for(const r of [...T.regions].sort((a,b)=>b.count-a.count)){const [sx,sy]=toScreen(r.c[0],r.c[1]);if(sx<0||sy<0||sx>W||sy>H)continue;
   ctx.font='600 11px "DM Sans",sans-serif';const t=r.name.toUpperCase(),tw=ctx.measureText(t).width,c=rel>1.1?num(r.count):'';ctx.font='500 10.5px "Space Grotesk",sans-serif';const cw=c?ctx.measureText(c).width+6:0,w=tw+cw;
   if(free(sx-w/2,sy-9,w,18)){ctx.globalAlpha=.88;ctx.font='600 11px "DM Sans",sans-serif';ctx.fillStyle=dark?'#c9cbe0':'#4b4f6a';ctx.fillText(t,sx-w/2,sy);if(c){ctx.font='500 10.5px "Space Grotesk",sans-serif';ctx.fillStyle=dark?'#8d90a8':'#8a8fa3';ctx.fillText(c,sx-w/2+tw+6,sy);}ctx.globalAlpha=1;}}
  const hubs=T.cities.filter(c=>c.chain===chainIx&&(c.rank===0?rel>.95:c.rank===1?rel>2.4:rel>4.2)).sort((a,b)=>b.degree-a.degree);
  for(const c of hubs){const [sx,sy]=toScreen(c.x,c.y);if(sx<-20||sy<-20||sx>W+20||sy>H+20)continue;const label=c.name||short(c.addr);ctx.font='500 11px "DM Sans",sans-serif';const tw=ctx.measureText(label).width;
   if(!free(sx-14,sy-14,28+tw+16,28))continue;
   ctx.save();ctx.shadowColor='#2b245033';ctx.shadowBlur=10;ctx.shadowOffsetY=3;ctx.beginPath();ctx.arc(sx,sy,13,0,6.2832);ctx.fillStyle=dark?'#25262c':'#fff';ctx.fill();ctx.restore();
   const img=logo(c);if(img?.complete&&img.naturalWidth){ctx.save();ctx.beginPath();ctx.arc(sx,sy,10.5,0,6.2832);ctx.clip();ctx.drawImage(img,sx-10.5,sy-10.5,21,21);ctx.restore();}
   else drawGlyph(cat(c.catKey)[1],sx,sy,13,cat(c.catKey)[2]);
   ctx.fillStyle=dark?'#25262ccc':'#ffffffdd';roundRect(sx+17,sy-10,tw+12,20,7);ctx.fill();ctx.fillStyle=dark?'#e6e7ee':'#1c2233';ctx.fillText(label,sx+23,sy);}
  const d=D[chainIx];
  if(d&&selected?.chain===chainIx&&selected.i!=null){const [sx,sy]=toScreen(d.X[selected.i],d.Y[selected.i]);ctx.beginPath();ctx.arc(sx,sy,9,0,6.2832);ctx.strokeStyle='#5658e8';ctx.lineWidth=2.5;ctx.stroke();ctx.beginPath();ctx.arc(sx,sy,15,0,6.2832);ctx.strokeStyle='#5658e855';ctx.lineWidth=1.5;ctx.stroke();
   if(selected.address){const t=short(selected.address);ctx.font='600 11px "Space Grotesk",sans-serif';const tw=ctx.measureText(t).width;ctx.fillStyle='#5658e8';roundRect(sx-tw/2-8,sy-36,tw+16,20,7);ctx.fill();ctx.fillStyle='#fff';ctx.fillText(t,sx-tw/2,sy-26);}}
  if(d&&hover!=null&&hover!==selected?.i){const [sx,sy]=toScreen(d.X[hover],d.Y[hover]);ctx.beginPath();ctx.arc(sx,sy,7,0,6.2832);ctx.strokeStyle='#5658e8aa';ctx.lineWidth=2;ctx.stroke();}
  ctx.setTransform(1,0,0,1,0,0);
  $('[data-em-label]').textContent=CHAINS[CHAIN_KEYS[chainIx]].name;
  frames.push(performance.now()-t0);if(frames.length>240)frames.shift();
 }
 function roundRect(x,y,w,h,r){ctx.beginPath();ctx.moveTo(x+r,y);ctx.arcTo(x+w,y,x+w,y+h,r);ctx.arcTo(x+w,y+h,x,y+h,r);ctx.arcTo(x,y+h,x,y,r);ctx.arcTo(x,y,x+w,y,r);ctx.closePath();}
 function drawGlyph(name,x,y,size,color){const p=GLYPH_PATH[name]||(GLYPH_PATH[name]=new Path2D((glyph(name).match(/d="([^"]+)"/)||[])[1]||''));ctx.save();ctx.translate(x-size/2,y-size/2);ctx.scale(size/24,size/24);ctx.strokeStyle=color;ctx.lineWidth=1.8;ctx.lineCap='round';ctx.lineJoin='round';ctx.stroke(p);ctx.restore();}

 function settleSoon(){clearTimeout(settle);settle=setTimeout(()=>{if(signal.aborted||!D[chainIx])return;detail=renderDots(cam,chainIx);draw();},140);}
 const reduce=matchMedia('(prefers-reduced-motion: reduce)');
 function flyTo(t,ms=650){if(reduce.matches)ms=1;const from={...cam},start=performance.now();target=t;
  const step=now=>{if(target!==t||signal.aborted)return;const p=Math.min(1,(now-start)/ms),e=p<.5?4*p*p*p:1-Math.pow(-2*p+2,3)/2,lk=Math.log(from.k)+(Math.log(t.k)-Math.log(from.k))*e;
   cam={x:from.x+(t.x-from.x)*e,y:from.y+(t.y-from.y)*e,k:Math.exp(lk)};if(detail)detail.stale=true;paint();if(p<1)requestAnimationFrame(step);else{target=null;settleSoon();}};requestAnimationFrame(step);}
 function zoomAt(f,sx=W/2,sy=H/2){const [wx,wy]=toWorld(sx,sy),k=Math.min(fitK()*60,Math.max(fitK()*.6,cam.k*f));cam={k,x:wx-(sx-W/2)/k,y:wy-(sy-H/2)/k};if(detail)detail.stale=true;draw();settleSoon();}
 function pan(dx,dy){cam={...cam,x:cam.x+dx/cam.k,y:cam.y+dy/cam.k};if(detail)detail.stale=true;draw();settleSoon();}
 async function setChain(ix,fly=true){chainIx=ix;root.querySelectorAll('[data-map-chain]').forEach(b=>{const on=b.dataset.mapChain===CHAIN_KEYS[ix];if(b.dataset.mapChain!=='solana'){b.setAttribute('aria-selected',String(on));b.tabIndex=on?0:-1;}});
  detail=null;renderHubs();renderSummary();if(fly)flyTo(fitCam(ix));else{cam=fitCam(ix);draw();}
  try{await loadChain(ix);if(chainIx!==ix)return;if(!overview[ix])overview[ix]=renderDots(fitCam(ix),ix);draw();settleSoon();}catch{}}

 function hubArt(c,size){const art=protocolArt(c.proto?.key);return art?`<img class="em-logo" src="${art}" width="${size}" height="${size}" alt="">`:walletIcon(c.category||c.catKey||'unknown',size);}
 function renderHubs(rows){const hubs=rows||T.cities.filter(c=>c.chain===chainIx&&c.rank===0).sort((a,b)=>b.degree-a.degree).slice(0,40);
  $('[data-em-list-title]').textContent=rows?'Search results':'Hubs';$('[data-em-list-count]').textContent=hubs.length;
  list.innerHTML=hubs.map((c,i)=>`<button type="button" class="em-hub" role="listitem" data-hub="${i}" data-address="${esc(normAddress(c.addr||c.address||'')||'')}" aria-label="${esc((c.name||short(c.addr||c.address||''))+(c.degree!=null?', '+num(c.degree)+' connections':''))}"><span class="em-hub-art">${hubArt(c,28)}</span><span class="em-hub-text"><b>${esc(c.name||short(c.addr||c.address||''))}</b><small>${c.degree!=null?num(c.degree)+' connections':esc(c.categoryName||'')}</small></span></button>`).join('')||'<p class="em-muted" style="padding:8px">No matches. Try a protocol name or a full 0x address.</p>';
  list._rows=hubs;}
 function renderSummary(){const ch=CHAIN_KEYS[chainIx],regs=T.regions.filter(r=>r.chain===chainIx).sort((a,b)=>b.count-a.count),tot=regs.reduce((s,r)=>s+r.count,0);
  inspector.innerHTML=`<div class="em-summary"><span class="em-chain"><img src="${CHAINS[ch].logo}" width="14" height="14" alt="">${CHAINS[ch].name}</span><h3>${num(tot)} wallets drawn</h3><p class="em-muted">The most active wallets, placed by what they are. Select a point or a hub to read its balance sheet.</p><ul>${regs.slice(0,9).map(r=>`<li><i style="background:${cat(catKeys[r.cat])[2]}"></i><span>${esc(r.name)}</span><b>${num(r.count)}</b></li>`).join('')}</ul></div>`;}

 let cardReq=null;
 async function selectAddress(address,chain,hint=null){
  const a=normAddress(address);if(!a){status.textContent='Enter a full 0x address (40 hex characters).';return;}
  selected={address:a,chain:hint?.chain??null,i:hint?.i??null};draw();cardReq?.abort();cardReq=new AbortController();
  inspector.innerHTML=stateHTML('loading','Reading '+short(a)+'…');
  try{const q=new URLSearchParams({address:a});if(chain)q.set('chain',chain);const card=await em('tools/wallet?'+q,{signal:cardReq.signal});if(selected?.address!==a)return;
   inspector.innerHTML=walletCard(card,{compact:true});status.textContent=card.inGalaxy?'':'This wallet is in the full map but not among the drawn wallets.';
   if(card.nodeId!=null&&card.inGalaxy){const ix=CHAIN_KEYS.indexOf(card.chain);if(ix<0)return;if(ix!==chainIx)await setChain(ix,false);
    try{const d=await loadNodes(ix),j=d.nodeIndex.get(card.nodeId);if(j!=null&&selected?.address===a){selected={address:a,chain:ix,i:j};flyTo({x:d.X[j],y:d.Y[j],k:Math.max(cam.k,fitK()*7)});}}catch{}}}
  catch(e){if(e.name!=='AbortError'){inspector.innerHTML=stateHTML('error',e.message,{retry:true});inspector.querySelector('[data-em-retry]')?.addEventListener('click',()=>selectAddress(a,chain,hint),{once:true});}}
 }
 async function selectPoint(i){const ix=chainIx;selected={chain:ix,i};draw();inspector.innerHTML=stateHTML('loading','Reading wallet…');
  try{const d=await loadNodes(ix);const n=await em(`g/node/${CHAIN_KEYS[ix]}/${d.NODE[i]}?brief=1`,{signal});if(selected?.i===i&&isAddress(n.address))selectAddress(n.address,CHAIN_KEYS[ix],{chain:ix,i});}
  catch(e){if(e.name!=='AbortError')inspector.innerHTML=stateHTML('error',e.message);}}

 const pointers=new Map();
 function nearest(sx,sy,r=9){const d=D[chainIx];if(!d)return null;const [wx,wy]=toWorld(sx,sy),rw=r/cam.k;let best=null,bd=rw*rw;for(let gx=Math.floor((wx-rw)/CELL);gx<=Math.floor((wx+rw)/CELL);gx++)for(let gy=Math.floor((wy-rw)/CELL);gy<=Math.floor((wy+rw)/CELL);gy++){const a=d.grid.get(gx+':'+gy);if(a)for(const i of a){const q=(d.X[i]-wx)**2+(d.Y[i]-wy)**2;if(q<bd){bd=q;best=i;}}}return best;}
 function hubAt(sx,sy){for(const c of T.cities){if(c.chain!==chainIx)continue;const [x,y]=toScreen(c.x,c.y);if((x-sx)**2+(y-sy)**2<15*15)return c;}return null;}
 canvas.addEventListener('pointerdown',e=>{canvas.setPointerCapture(e.pointerId);pointers.set(e.pointerId,{x:e.offsetX,y:e.offsetY});if(pointers.size===1)dragging={x:e.offsetX,y:e.offsetY,cx:cam.x,cy:cam.y,moved:0};else if(pointers.size===2){const [a,b]=[...pointers.values()];pinch={d:Math.hypot(a.x-b.x,a.y-b.y),k:cam.k};dragging=null;}target=null;},{signal});
 canvas.addEventListener('pointermove',e=>{if(pointers.has(e.pointerId))pointers.set(e.pointerId,{x:e.offsetX,y:e.offsetY});
  if(pinch&&pointers.size===2){const [a,b]=[...pointers.values()],dd=Math.hypot(a.x-b.x,a.y-b.y);zoomAt(pinch.k*dd/pinch.d/cam.k,(a.x+b.x)/2,(a.y+b.y)/2);return;}
  if(dragging){const dx=e.offsetX-dragging.x,dy=e.offsetY-dragging.y;dragging.moved=Math.max(dragging.moved,Math.abs(dx)+Math.abs(dy));cam={...cam,x:dragging.cx-dx/cam.k,y:dragging.cy-dy/cam.k};if(detail)detail.stale=true;draw();return;}
  if(e.pointerType!=='mouse')return;const i=nearest(e.offsetX,e.offsetY);if(i!==hover){hover=i;draw();}
  if(i!=null){const [nm]=cat(catKeys[D[chainIx].CAT[i]]);tip.hidden=false;tip.style.left=e.offsetX+14+'px';tip.style.top=e.offsetY+12+'px';tip.innerHTML=`${walletIcon(catKeys[D[chainIx].CAT[i]],20)}<span>${esc(nm)}</span>`;}else tip.hidden=true;},{signal});
 const up=e=>{pointers.delete(e.pointerId);if(pointers.size<2)pinch=null;if(dragging&&pointers.size===0){const moved=dragging.moved;dragging=null;if(moved<4){const h=hubAt(e.offsetX,e.offsetY);if(h&&isAddress(normAddress(h.addr)))selectAddress(h.addr,CHAIN_KEYS[h.chain]);else{const i=nearest(e.offsetX,e.offsetY,e.pointerType==='touch'?16:10);if(i!=null)selectPoint(i);}}else settleSoon();}};
 canvas.addEventListener('pointerup',up,{signal});canvas.addEventListener('pointercancel',up,{signal});canvas.addEventListener('pointerleave',()=>{hover=null;tip.hidden=true;draw();},{signal});
 canvas.addEventListener('wheel',e=>{e.preventDefault();target=null;zoomAt(Math.exp(-e.deltaY*.0016),e.offsetX,e.offsetY);},{passive:false,signal});
 canvas.addEventListener('keydown',e=>{const step=e.shiftKey?160:60;const k={ArrowLeft:[-step,0],ArrowRight:[step,0],ArrowUp:[0,-step],ArrowDown:[0,step]}[e.key];
  if(k){e.preventDefault();pan(...k);}else if(e.key==='+'||e.key==='='){e.preventDefault();zoomAt(1.4);}else if(e.key==='-'||e.key==='_'){e.preventDefault();zoomAt(1/1.4);}else if(e.key==='0'){e.preventDefault();flyTo(fitCam(chainIx));}
  else if(e.key==='Enter'||e.key===' '){e.preventDefault();const i=nearest(W/2,H/2,40);if(i!=null)selectPoint(i);else status.textContent='Zoom in, then press Enter to open the wallet nearest the centre.';}},{signal});
 root.addEventListener('click',e=>{
  const z=e.target.closest('[data-em-zoom]');if(z){z.dataset.emZoom==='reset'?flyTo(fitCam(chainIx)):zoomAt(z.dataset.emZoom==='in'?1.6:1/1.6);return;}
  const c=e.target.closest('[data-map-chain]');if(c){const ix=CHAIN_KEYS.indexOf(c.dataset.mapChain);if(ix<0){status.textContent='Solana wallets are not in the Economic Machine map yet.';return;}status.textContent='';setChain(ix);return;}
  const h=e.target.closest('.em-hub');if(h){const row=list._rows?.[+h.dataset.hub];if(!row)return;if(row.x!=null&&row.chain===chainIx)flyTo({x:row.x,y:row.y,k:Math.max(cam.k,fitK()*6)});if(isAddress(h.dataset.address))selectAddress(h.dataset.address,row.chainKey||CHAIN_KEYS[row.chain??chainIx]);return;}
  if(e.target.closest('[data-em-expand]')){const on=root.classList.toggle('map-expanded');e.target.closest('[data-em-expand]').setAttribute('aria-pressed',String(on));requestAnimationFrame(resize);return;}
  const m=e.target.closest('a[data-em-map]');if(m&&isAddress(m.dataset.emMap)){e.preventDefault();selectAddress(m.dataset.emMap,m.dataset.emChain);}
 },{signal});
 root.querySelector('.map-chains').addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight'].includes(e.key))return;e.preventDefault();const ix=(chainIx+(e.key==='ArrowRight'?1:-1)+2)%2;setChain(ix);root.querySelector(`[data-map-chain="${CHAIN_KEYS[ix]}"]`)?.focus();},{signal});
 $('.map-search').addEventListener('submit',async e=>{e.preventDefault();const q=$('.map-search input').value.trim();if(!q){renderHubs();status.textContent='';return;}
  if(/^0x/i.test(q)){if(normAddress(q)){status.textContent='';selectAddress(q,null);}else status.textContent='Enter a full 0x address (40 hex characters).';return;}
  if(!/^[\w .:\-]{1,64}$/.test(q)){status.textContent='Use letters, digits, spaces or . : - _';return;}
  status.textContent='Searching…';const local=T.cities.filter(c=>(c.name||'').toLowerCase().includes(q.toLowerCase())).sort((a,b)=>b.degree-a.degree).slice(0,30);
  try{const d=await em('g/search?'+new URLSearchParams({q,limit:20}),{signal});const remote=(d.results||[]).filter(r=>isAddress(r.address)).map(r=>({name:r.entity_name?r.entity_name.replace(/_/g,' '):null,addr:r.address,chainKey:r.chain,chain:CHAIN_KEYS.indexOf(r.chain),category:r.category,categoryName:cat(r.category)[0]}));
   renderHubs([...local,...remote.filter(r=>!local.some(l=>l.addr===r.addr))].slice(0,40));status.textContent=local.length||remote.length?'':'No matches.';}
  catch(err){if(err.name!=='AbortError'){renderHubs(local);status.textContent=local.length?'':err.message;}}
 },{signal});
 const ro=new ResizeObserver(resize);ro.observe(canvas);
 const themeObserver=new MutationObserver(()=>{overview=[null,null];detail=null;if(D[chainIx])overview[chainIx]=renderDots(fitCam(chainIx),chainIx);draw();});themeObserver.observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
 resize();await setChain(chainIx,false);
 performance.mark?.('em-map-ready');root.dataset.emReady='true';
 root.__emMapStats=()=>({frames:[...frames],variant,loaded:D.map(Boolean)});
 return ()=>{abort.abort();ro.disconnect();themeObserver.disconnect();cancelAnimationFrame(frame);clearTimeout(settle);cardReq?.abort();};
}

let mapStyled=false;
function injectMapStyles(){if(mapStyled)return;mapStyled=true;const s=document.createElement('style');s.id='em-map-styles';s.textContent=`
.flow-desk:has(.em-map-center),.flow-desk:has(.em-map-boot){background:#f7f6fd;color:#1c2233;border-color:#e4e1f7;box-shadow:0 35px 90px -40px #4b3fb040,0 0 0 7px #ffffffb0}
html[data-theme=dark] .flow-desk:has(.em-map-center),html[data-theme=dark] .flow-desk:has(.em-map-boot){background:#17181c;color:#e6e7ee;border-color:#2c2e34}
.em-map-boot{display:grid;place-items:center;min-height:560px}
.flow-desk:has(.em-map-center) .map-chrome{background:#ffffffcc;border-bottom:1px solid #ebe9f7}html[data-theme=dark] .flow-desk:has(.em-map-center) .map-chrome{background:#1c1d22;border-color:#2c2e34}
.flow-desk:has(.em-map-center) .map-addresses,.flow-desk:has(.em-map-center) .map-inspector{background:#ffffff;border-color:#ebe9f7;color:inherit;overflow:auto}
html[data-theme=dark] .flow-desk:has(.em-map-center) .map-addresses,html[data-theme=dark] .flow-desk:has(.em-map-center) .map-inspector{background:#1b1c21;border-color:#2c2e34}
.em-map-center{position:relative;background:radial-gradient(ellipse at 50% 45%,#ecebff,transparent 70%),radial-gradient(#5658e81c .7px,transparent .7px) 0 0/18px 18px,#f4f3fd!important}
html[data-theme=dark] .em-map-center{background:radial-gradient(ellipse at 50% 45%,#2a2840,transparent 70%),radial-gradient(#ffffff10 .7px,transparent .7px) 0 0/18px 18px,#15161a!important}
.em-map-canvas{display:block;width:100%;height:100%;min-height:560px;cursor:grab;touch-action:none;outline:none}.em-map-canvas:active{cursor:grabbing}.em-map-canvas:focus-visible{box-shadow:inset 0 0 0 2px #5658e8}
.em-map-loading{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);background:#ffffffe6;border-radius:12px}html[data-theme=dark] .em-map-loading{background:#1b1c21e6}
.em-map-badge{font:500 11.5px "DM Sans",sans-serif;padding:5px 10px;border-radius:999px;background:#ecebfd;color:#5658e8;white-space:nowrap}html[data-theme=dark] .em-map-badge{background:#2a2840;color:#bcadff}
.em-chain-soon{opacity:.55;cursor:not-allowed}.em-chain-soon small{font-size:10px;margin-left:4px;padding:1px 6px;border-radius:999px;background:#0000000d}
.em-map-tip{position:absolute;z-index:3;display:flex;align-items:center;gap:7px;padding:5px 9px 5px 5px;border-radius:10px;background:#fff;border:1px solid #ebe9f7;box-shadow:0 10px 24px -14px #2b2450;font:12px "DM Sans",sans-serif;pointer-events:none;color:#1c2233}
html[data-theme=dark] .em-map-tip{background:#222329;border-color:#33353c;color:#e6e7ee}
.em-map-controls{position:absolute;right:14px;bottom:14px;display:flex;flex-direction:column;gap:6px;z-index:2}
.em-zoom{display:grid;place-items:center;width:34px;height:34px;border-radius:10px;border:1px solid #e4e1f7;background:#fff;color:#4b4f6a;cursor:pointer;box-shadow:0 6px 16px -10px #2b245066}.em-zoom svg{width:16px;height:16px}.em-zoom:hover{color:#5658e8;border-color:#cfcaf5}
html[data-theme=dark] .em-zoom{background:#222329;border-color:#33353c;color:#c9cbe0}
.em-map-legend{position:absolute;left:14px;bottom:12px;right:70px;display:flex;flex-wrap:wrap;gap:4px 12px;font:11px "DM Sans",sans-serif;color:#6b7088;pointer-events:none}.em-map-legend span{display:inline-flex;align-items:center;gap:5px}.em-map-legend i{width:7px;height:7px;border-radius:50%}
.em-hub-list{display:grid;gap:4px;overflow:auto;align-content:start}.em-hub{display:flex;align-items:center;gap:10px;width:100%;padding:7px 8px;border-radius:10px;border:0;background:transparent;text-align:left;cursor:pointer;color:inherit;font:inherit}
.em-hub:hover{background:#f2f1fd}html[data-theme=dark] .em-hub:hover{background:#26272d}.em-hub-art{flex:none;display:grid}.em-hub-text{display:grid;min-width:0}.em-hub-text b{font-weight:500;font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.em-hub-text small{font-size:11px;color:#8a8fa3}
.em-summary{display:grid;gap:10px;font:13px/1.45 "DM Sans",sans-serif}.em-summary h3{margin:0;font:500 22px/1.1 "Space Grotesk",sans-serif;letter-spacing:-.02em}.em-summary .em-chain{justify-self:start}
.em-summary ul{list-style:none;margin:0;padding:0;display:grid;gap:2px}.em-summary li{display:grid;grid-template-columns:10px 1fr auto;gap:8px;align-items:center;padding:6px 0;border-top:1px solid #ebe9f7;font-size:12.5px}html[data-theme=dark] .em-summary li{border-color:#2c2e34}
.em-summary li i{width:8px;height:8px;border-radius:50%}.em-summary li b{font:500 12.5px "Space Grotesk",sans-serif}
.map-inspector .em-card{border:0;padding:0;background:transparent}
@media (max-width:720px){.em-map-canvas{min-height:420px}.em-map-legend{display:none}.em-map-boot{min-height:420px}}
`;document.head.append(s);}
