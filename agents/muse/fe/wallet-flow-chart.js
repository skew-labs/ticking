// Flow chart — inflow/outflow/holdings analysis (NOT a price TickChart).
// Muse/Ask answers + Investigate/Tracker detail share this surface.
// APIs: GET /api/public/em/wallet-flow/{addr} | /asset-flow/{mint}
import {esc} from './explorer-common.js';

const money=n=>{const v=Number(n); if(!Number.isFinite(v)) return '$0'; return '$'+Intl.NumberFormat('en',{notation:'compact',maximumFractionDigits:2}).format(v);};
const signedMoney=n=>{const v=Number(n)||0; return (v>=0?'+':'−')+money(Math.abs(v));};
const COLORS=['#36c6bc','#f5b041','#fb7185','#a5a0ff','#34d399','#60a5fa','#f472b6','#94a3b8'];
const CAT_TONE={tokenized:'tok',crypto:'cry',stable:'stb'};
const _nz=v=>{const n=Number(v);return Number.isFinite(n)?n:0;};

export async function fetchWalletFlow(addr,{chain='ethereum',interval='1h',limit=168,mapped=true,signal}={}){
  const q=new URLSearchParams({chain,interval,limit:String(limit),mapped:mapped?'1':'0'});
  const r=await fetch('/api/public/em/wallet-flow/'+encodeURIComponent(addr)+'?'+q,{credentials:'omit',signal,headers:{Accept:'application/json'}});
  if(!r.ok)throw Error('Wallet flow unavailable ('+r.status+').');
  const d=await r.json();
  if(d.schema!=='ticking.wallet-flow.v1')throw Error('Invalid wallet-flow response.');
  return d;
}

export async function fetchAssetFlow(mint,{chain='arbitrum',interval='1h',limit=168,signal}={}){
  const q=new URLSearchParams({chain,interval,limit:String(limit)});
  const r=await fetch('/api/public/em/asset-flow/'+encodeURIComponent(mint)+'?'+q,{credentials:'omit',signal,headers:{Accept:'application/json'}});
  if(!r.ok)throw Error('Asset flow unavailable ('+r.status+').');
  const d=await r.json();
  if(d.schema!=='ticking.asset-flow.v1'&&d.schema!=='ticking.wallet-flow.v1')throw Error('Invalid asset-flow response.');
  return d;
}

function stubSeries(totalUsd,interval='1h',n=48){
  const period={'1m':6e4,'5m':3e5,'15m':9e5,'1h':36e5,'4h':144e5,'1d':864e5}[interval]||36e5;
  const end=Math.floor(Date.now()/period)*period,out=[];
  for(let i=n-1;i>=0;i--){const t=end-i*period,v=Math.max(0,totalUsd||0);out.push({time:t,open:v,high:v,low:v,close:v,volume:0,netInUsd:0,netOutUsd:0,totalUsd:v,synthetic:true});}
  return out;
}

function toBars(series){
  return (series||[]).map(b=>({
    time:b.time,
    close:_nz(b.close??b.totalUsd),
    totalUsd:_nz(b.totalUsd??b.close),
    volume:_nz(b.volume??((b.netInUsd||0)+(b.netOutUsd||0))),
    netInUsd:_nz(b.netInUsd),netOutUsd:_nz(b.netOutUsd),
    synthetic:!!b.synthetic
  }));
}

function summaryOf(d){
  const s=d.flowSummary||{};
  if(Number.isFinite(Number(s.netInUsd))||Number.isFinite(Number(s.netOutUsd)))return{
    netIn:_nz(s.netInUsd),netOut:_nz(s.netOutUsd),netDelta:_nz(s.netDeltaUsd),
    marketsN:_nz(s.marketsTokenCount),marketsUsd:_nz(s.marketsHoldingsUsd)
  };
  const series=d.series||d.primary?.series||[];
  let netIn=0,netOut=0;
  for(const b of series){netIn+=_nz(b.netInUsd);netOut+=_nz(b.netOutUsd);}
  const tokens=d.primary?.tokens||d.tokens||(d.asset?[d.asset]:[]);
  const tokList=Array.isArray(tokens)?tokens:[];
  return{
    netIn,netOut,netDelta:netIn-netOut,
    marketsN:tokList.filter(t=>t.onMarkets).length,
    marketsUsd:tokList.filter(t=>t.onMarkets).reduce((a,t)=>a+_nz(t.usd),0)
  };
}

function shellHTML(title,interval,compact,kind){
  return `<div class="wf-chart${compact?' is-compact':''}" data-wf-kind="${esc(kind)}">
    <div class="wf-badge" aria-hidden="true"><span>FLOW</span><small>In / Out · holdings USD — not a price chart</small></div>
    <div class="wf-head"><div class="wf-title"><b>${esc(title)}</b><small data-wf-sub>Loading…</small></div>
      <div class="wf-stats">
        <span class="wf-flow-pair"><small>In</small><b data-wf-in class="up">$0</b></span>
        <span class="wf-flow-pair"><small>Out</small><b data-wf-out class="down">$0</b></span>
        <span><small>Net Δ</small><b data-wf-net>$0</b></span>
        <span><small>${kind==='asset'?'Net flow USD':'Holdings'}</small><b data-wf-total>$0</b></span>
        <span><small>Unrealized</small><b data-wf-upnl>$0</b></span>
        <span><small>Realized</small><b data-wf-rpnl>$0</b></span>
        <span data-wf-map-slot><small>On chart</small><b data-wf-mapn>0</b></span>
      </div>
      <div class="wf-ivals" role="group" aria-label="Interval">${[['1h','1H'],['4h','4H'],['1d','1D']].map(([id,t])=>`<button type="button" data-wf-iv="${id}"${id===interval?' aria-pressed="true"':''}>${t}</button>`).join('')}</div>
    </div>
    <div class="wf-flowbar" data-wf-flowbar><i data-wf-inbar></i><i data-wf-outbar></i></div>
    <div class="wf-legend" data-wf-legend hidden></div>
    <div class="wf-canvas-wrap"><canvas class="wf-canvas" data-wf-canvas role="img" aria-label="Flow chart: green inflow, red outflow, holdings line"></canvas></div>
    <div class="wf-tokens" data-wf-tokens hidden></div>
    <p class="wf-dumps" data-wf-dumps hidden></p>
    <p class="wf-note" data-wf-note></p>
  </div>`;
}

/** Draw dump/who-sold tags on the flow time axis. */
function paintDumpMarkers(ctx,markers,bars,{padL,padT,w,h,holdH,flowTop,flowH,cssW,cssH,xAt,compact}){
  if(!markers?.length||!bars?.length)return;
  const t0=bars[0].time,t1=bars.at(-1).time||t0+1;
  const inRange=m=>{
    const tm=m.time>1e12?m.time:m.time*1000;
    const b0=t0>1e12?t0:t0*1000,b1=t1>1e12?t1:t1*1000;
    // compare in same units as bars.time
    const mt=m.time, lo=Math.min(t0,t1), hi=Math.max(t0,t1);
    return mt>=lo && mt<=hi;
  };
  const list=(markers||[]).filter(inRange).slice(0,6);
  list.forEach((m,i)=>{
    const x=xAt(m.time);
    if(x<padL||x>padL+w)return;
    // vertical dump line through flow band
    ctx.save();
    ctx.strokeStyle='color-mix(in srgb, #f97316 70%, transparent)';
    ctx.lineWidth=1.2;ctx.setLineDash([4,3]);
    ctx.beginPath();ctx.moveTo(x,flowTop);ctx.lineTo(x,flowTop+flowH);ctx.stroke();
    ctx.setLineDash([]);
    // tag pill
    const label=String(m.label||'Dump').slice(0,42);
    ctx.font='700 9px DM Sans,sans-serif';
    const tw=Math.min(ctx.measureText(label).width+10, Math.max(80,w*0.42));
    const th=16;
    // alternate above/below mid to reduce overlap
    const midY=flowTop+flowH/2;
    const y= (i%2===0) ? (flowTop+4) : (flowTop+flowH-th-4);
    let bx=Math.min(Math.max(x-tw/2,padL+2), padL+w-tw-2);
    ctx.fillStyle='color-mix(in srgb, #f97316 22%, var(--tk-bg-sub,#0b0f19))';
    ctx.strokeStyle='color-mix(in srgb, #f97316 55%, var(--tk-line,#30384b))';
    ctx.lineWidth=1;
    const r=5;
    ctx.beginPath();
    ctx.moveTo(bx+r,y);ctx.arcTo(bx+tw,y,bx+tw,y+th,r);ctx.arcTo(bx+tw,y+th,bx,y+th,r);
    ctx.arcTo(bx,y+th,bx,y,r);ctx.arcTo(bx,y,bx+tw,y,r);ctx.closePath();
    ctx.fill();ctx.stroke();
    ctx.fillStyle='#fdba74';
    ctx.textAlign='left';ctx.textBaseline='middle';
    ctx.fillText(label, bx+5, y+th/2, tw-8);
    // caret toward bar
    ctx.beginPath();ctx.fillStyle='#f97316';
    ctx.moveTo(x, midY-3);ctx.lineTo(x-3,midY+2);ctx.lineTo(x+3,midY+2);ctx.closePath();ctx.fill();
    ctx.restore();
  });
}

/** Draw Flow: green In bars up, red Out bars down from mid; holdings line on top band. */
function paintFlowCanvas(canvas,bars,overlays,visible,{compact=false,markers=[]}={}){
  const dpr=window.devicePixelRatio||1;
  const cssW=Math.max(280,canvas.clientWidth||canvas.parentElement?.clientWidth||640);
  const cssH=compact?200:280;
  canvas.width=Math.floor(cssW*dpr);canvas.height=Math.floor(cssH*dpr);
  canvas.style.width=cssW+'px';canvas.style.height=cssH+'px';
  const ctx=canvas.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,cssW,cssH);
  const padL=8,padR=52,padT=10,padB=22,w=cssW-padL-padR,h=cssH-padT-padB;
  if(!bars.length){ctx.fillStyle='var(--tk-muted,#94a3b8)';ctx.font='13px DM Sans,sans-serif';ctx.fillText('No flow in this window',padL+12,cssH/2);return;}
  const holdH=Math.round(h*0.38), flowH=h-holdH-10, holdTop=padT, flowTop=padT+holdH+10, midY=flowTop+flowH/2;
  const t0=bars[0].time,t1=bars.at(-1).time||t0+1,span=Math.max(1,t1-t0);
  const xAt=t=>padL+((t-t0)/span)*w;
  const maxHold=Math.max(1,...bars.map(b=>b.totalUsd||b.close||0),...overlays.flatMap(o=>(o.bars||[]).map(b=>b.totalUsd||b.close||0)));
  const maxFlow=Math.max(1,...bars.map(b=>Math.max(b.netInUsd||0,b.netOutUsd||0)));
  // bands
  ctx.fillStyle='color-mix(in srgb, var(--tk-bg-sub,#0b0f19) 88%, transparent)';
  ctx.fillRect(padL,holdTop,w,holdH);
  ctx.fillRect(padL,flowTop,w,flowH);
  // zero line + labels
  ctx.strokeStyle='color-mix(in srgb, var(--tk-line,#30384b) 80%, transparent)';ctx.lineWidth=1;ctx.setLineDash([3,4]);
  ctx.beginPath();ctx.moveTo(padL,midY);ctx.lineTo(padL+w,midY);ctx.stroke();ctx.setLineDash([]);
  ctx.fillStyle='var(--tk-muted,#94a3b8)';ctx.font='600 9px DM Sans,sans-serif';
  ctx.fillText('IN',padL+4,flowTop+11);ctx.fillText('OUT',padL+4,flowTop+flowH-4);
  ctx.fillText('HOLDINGS',padL+4,holdTop+11);
  // In/Out bars (primary)
  const n=bars.length, bw=Math.max(2,Math.min(14,(w/n)*0.55));
  for(const b of bars){
    const x=xAt(b.time);
    const ih=((b.netInUsd||0)/maxFlow)*(flowH/2-4);
    const oh=((b.netOutUsd||0)/maxFlow)*(flowH/2-4);
    if(ih>0.5){ctx.fillStyle='color-mix(in srgb, var(--tk-up,#22c55e) 82%, transparent)';ctx.fillRect(x-bw/2,midY-ih,bw,ih);}
    if(oh>0.5){ctx.fillStyle='color-mix(in srgb, var(--tk-down,#ef4444) 82%, transparent)';ctx.fillRect(x-bw/2,midY,bw,oh);}
  }
  // Holdings line (secondary)
  ctx.beginPath();ctx.strokeStyle=COLORS[0];ctx.lineWidth=1.8;ctx.globalAlpha=.95;
  let started=false;
  for(const b of bars){
    const x=xAt(b.time), y=holdTop+holdH-(((b.totalUsd||b.close||0)/maxHold)*(holdH-8)+4);
    if(!started){ctx.moveTo(x,y);started=true;}else ctx.lineTo(x,y);
  }
  if(started)ctx.stroke();
  // soft fill under holdings
  if(started){
    ctx.lineTo(xAt(bars.at(-1).time),holdTop+holdH);ctx.lineTo(xAt(bars[0].time),holdTop+holdH);ctx.closePath();
    const g=ctx.createLinearGradient(0,holdTop,0,holdTop+holdH);
    g.addColorStop(0,'color-mix(in srgb, #36c6bc 28%, transparent)');g.addColorStop(1,'transparent');
    ctx.fillStyle=g;ctx.globalAlpha=.55;ctx.fill();
  }
  ctx.globalAlpha=1;
  // mapped/related overlays on holdings band
  overlays.forEach((o,i)=>{
    if(!o.bars||o.bars.length<2)return;
    ctx.beginPath();ctx.strokeStyle=COLORS[(i+1)%COLORS.length];ctx.lineWidth=1.4;ctx.globalAlpha=.8;
    let s2=false;
    for(const b of o.bars){
      const x=xAt(b.time), y=holdTop+holdH-(((b.totalUsd||b.close||0)/maxHold)*(holdH-8)+4);
      if(x<padL||x>padL+w)continue;
      if(!s2){ctx.moveTo(x,y);s2=true;}else ctx.lineTo(x,y);
    }
    if(s2)ctx.stroke();
  });
  ctx.globalAlpha=1;
  // right-axis labels
  ctx.fillStyle='var(--tk-muted,#94a3b8)';ctx.font='10px DM Sans,sans-serif';ctx.textAlign='left';
  ctx.fillText(money(maxHold),padL+w+6,holdTop+12);
  ctx.fillText(money(maxFlow),padL+w+6,flowTop+12);
  ctx.fillText('−'+money(maxFlow).replace('$',''),padL+w+6,flowTop+flowH);
  // time ticks
  ctx.textAlign='center';
  const ticks=[bars[0],bars[Math.floor(n/2)],bars.at(-1)].filter(Boolean);
  for(const b of ticks){
    const d=new Date((b.time>1e12?b.time:b.time*1000));
    ctx.fillText(d.toISOString().slice(5,16).replace('T',' '),xAt(b.time),cssH-6);
  }
  // dump / who-sold tags
  paintDumpMarkers(ctx,markers,bars,{padL,padT,w,h,holdH,flowTop,flowH,cssW,cssH,xAt,compact});
}

/** Mount Flow chart for wallet or Markets asset. */
export async function mountWalletFlowChart(host,{address,mint=null,chain='ethereum',interval='1h',limit=168,compact=false,signal,label=null,kind=null,preloaded=null}={}){
  if(!host)return null;
  const isAsset=!!(kind==='asset'||mint||(preloaded&&String(preloaded.schema||'').includes('asset-flow')));
  const subject=isAsset?(mint||address||preloaded?.mint||preloaded?.address):(address||preloaded?.address);
  const title=typeof label==='string'?label:(label?.label?.text||label?.text||label?.name||(isAsset?'Asset Flow':'Wallet Flow'));
  host.innerHTML='';
  styles();
  host.insertAdjacentHTML('beforeend',shellHTML(title,interval,compact,isAsset?'asset':'wallet'));
  const wrap=host.querySelector('.wf-chart');
  const canvas=wrap.querySelector('[data-wf-canvas]');
  const legend=wrap.querySelector('[data-wf-legend]');
  let alive=true,curIv=interval,data=null,visible=new Set(['primary','flowvol']);

  function renderLegend(d){
    const groups=(d.groups||[]).filter(g=>(g.totalUsd||0)>0 || (g.tokenCount||0)>0);
    const agg=!!(d.aggregateMode || ((d.mapped||[]).length>0 && Number(d.totalUsd)>Number(d.primary?.totalUsd||0)+1));
    const rows=[];
    if(isAsset){
      rows.push({id:'primary',label:d.asset?.symbol||d.primary?.label||'Asset net flow',usd:d.totalUsd??d.primary?.totalUsd??0,color:COLORS[0],locked:true});
    }else if(agg){
      rows.push({id:'entity',label:d.entityName?`${d.entityName} (entity)`:'Entity holdings',usd:d.totalUsd??0,color:COLORS[0],locked:true});
      rows.push({id:'primary',label:d.primary?.label||'This wallet',usd:d.primary?.totalUsd??d.primaryTotalUsd??0,color:COLORS[1]});
    }else{
      rows.push({id:'primary',label:d.primary?.label||'This wallet',usd:d.primary?.totalUsd??d.totalUsd??0,color:COLORS[0]});
    }
    rows.push({id:'flowvol',label:'In / Out bars',usd:null,color:'linear-gradient(90deg,#22c55e,#ef4444)',grade:'flow'});
    if((d.markers||d.dumpTags||[]).length){
      rows.push({id:'dumps',label:`Dump tags (${(d.markers||d.dumpTags).length})`,usd:null,color:'#f97316',grade:'dump',locked:true});
    }
    rows.push(...groups.map((g,i)=>({id:'grp:'+g.id,label:g.label||g.id,usd:g.totalUsd??0,color:COLORS[(i+(agg?2:1))%COLORS.length],grade:g.category})));
    rows.push(...(d.mapped||[]).map((m,i)=>({id:m.address,label:m.label||m.address.slice(0,8),usd:m.totalUsd??0,color:COLORS[(i+(agg?2:1)+groups.length)%COLORS.length],grade:m.grade})));
    legend.hidden=false;
    legend.innerHTML='<span class="wf-leg-k">On this chart</span>'+rows.map(r=>{
      const swatch=String(r.color).startsWith('linear')?`background:${r.color}`:`background:${r.color}`;
      const usd=r.usd==null?'':`<small>${money(r.usd)}${r.grade?' · '+esc(r.grade):''}</small>`;
      return `<label class="wf-leg"><input type="checkbox" data-wf-tog="${esc(r.id)}" ${visible.has(r.id)?'checked':''}${r.locked?' disabled':''}/><i style="${swatch}"></i><b>${esc(r.label)}</b>${usd}</label>`;
    }).join('');
  }

  function paintTokens(tokens){
    const tok=wrap.querySelector('[data-wf-tokens]');
    if(!tokens?.length){tok.hidden=true;tok.innerHTML='';return;}
    tok.hidden=false;
    tok.innerHTML=tokens.slice(0,14).map((t,i)=>{
      const cat=CAT_TONE[t.category]||'cry';
      const mkt=t.onMarkets?'<em class="wf-mkt" title="Listed on Markets">Markets</em>':'';
      return `<span class="wf-chip wf-chip-${cat}" style="--i:${i}" data-cat="${esc(t.category||'crypto')}"><b>${esc(t.symbol)}</b> ${money(t.usd??0)}${t.category?` · ${esc(t.category)}`:''}${mkt}</span>`;
    }).join('');
  }

  function paint(d){
    data=d;
    const agg=!!(d.aggregateMode || ((d.mapped||[]).length>0));
    const main=toBars(d.series||d.primary?.series||stubSeries(d.totalUsd,curIv));
    const last=main.at(-1);
    const headline=Number.isFinite(Number(d.totalUsd))?Number(d.totalUsd):(last?.totalUsd||last?.close||0);
    const sum=summaryOf(d);
    wrap.querySelector('[data-wf-total]').textContent=money(headline);
    const inEl=wrap.querySelector('[data-wf-in]'); if(inEl) inEl.textContent=money(sum.netIn);
    const outEl=wrap.querySelector('[data-wf-out]'); if(outEl) outEl.textContent=money(sum.netOut);
    const netEl=wrap.querySelector('[data-wf-net]');netEl.textContent=signedMoney(sum.netDelta);netEl.className=sum.netDelta>=0?'up':'down';
    wrap.querySelector('[data-wf-mapn]').textContent=String((d.mapped||[]).length);
    const upnl=_nz(d.unrealizedPnl??d.primary?.unrealizedPnl), rpnl=_nz(d.realizedPnl??d.primary?.realizedPnl);
    const upEl=wrap.querySelector('[data-wf-upnl]'); if(upEl){upEl.textContent=signedMoney(upnl); upEl.className=upnl>=0?'up':'down';}
    const rpEl=wrap.querySelector('[data-wf-rpnl]'); if(rpEl){rpEl.textContent=signedMoney(rpnl); rpEl.className=rpnl>=0?'up':'down';}
    const totFlow=Math.max(sum.netIn+sum.netOut,1);
    const inPct=Math.round(100*sum.netIn/totFlow), outPct=100-inPct;
    const inBar=wrap.querySelector('[data-wf-inbar]'), outBar=wrap.querySelector('[data-wf-outbar]');
    if(inBar&&outBar){inBar.style.flexGrow=String(Math.max(inPct,2));outBar.style.flexGrow=String(Math.max(outPct,2));wrap.querySelector('[data-wf-flowbar]').title=`In ${money(sum.netIn)} · Out ${money(sum.netOut)}`;}
    const pnlTag=d.pnlStatus||d.primary?.pnlStatus||'estimated';
    const who=isAsset?(d.asset?.symbol||'asset')+' · Markets flow':(agg?(d.entityName||'entity')+' · this wallet + '+(d.mapped||[]).length+' related':'this wallet');
    const cacheTag=d.cacheHit?` · ${Number(d.latencyMs||0).toFixed(1)}ms`:'';
    const mktTag=sum.marketsN?` · ${sum.marketsN} Markets assets`:'';
    const addr=String(d.address||d.mint||subject||'');
    wrap.querySelector('[data-wf-sub]').textContent=`${d.chain||chain} · ${curIv} · ${who} · PnL ${pnlTag}${mktTag}${cacheTag} · ${addr.slice(0,6)}…${addr.slice(-4)}`;
    wrap.querySelector('[data-wf-note]').textContent=d.framingNote||d.mappedNote||d.depth?.note||'Green = inflow USD · Red = outflow USD · Line = holdings / net flow — not token price.';
    const tokens=isAsset?(d.asset?[d.asset]:[]):(d.primary?.tokens||d.tokens||[]);
    paintTokens(tokens);
    visible=new Set([
      ...(agg&&!isAsset?['entity','primary']:['primary']),
      'flowvol',
      ...((d.groups||[]).filter(g=>g.visibleDefault).map(g=>'grp:'+g.id)),
      ...((d.mapped||[]).filter(m=>m.visibleDefault!==false).map(m=>m.address)),
    ]);
    renderLegend(d);
    const overlays=[];
    if(agg&&visible.has('primary')&&d.primary?.series)overlays.push({bars:toBars(d.primary.series)});
    for(const g of (d.groups||[]))if(visible.has('grp:'+g.id))overlays.push({bars:toBars(g.series)});
    for(const m of (d.mapped||[]))if(visible.has(m.address))overlays.push({bars:toBars(m.series)});
    const dumpMarkers=d.markers||d.dumpTags||[];
    paintFlowCanvas(canvas, visible.has('primary')||visible.has('entity')?main:[], overlays, visible, {compact,markers:dumpMarkers});
    const dumpNote=wrap.querySelector('[data-wf-dumps]');
    if(dumpNote){
      if(dumpMarkers.length){
        dumpNote.hidden=false;
        dumpNote.textContent=dumpMarkers.slice(0,4).map(m=>m.label).join(' · ');
      }else{
        dumpNote.hidden=true;dumpNote.textContent='';
      }
    }
    wrap.classList.remove('wf-pulse');void wrap.offsetWidth;wrap.classList.add('wf-pulse');
  }

  async function load(iv=curIv){
    curIv=iv;wrap.querySelectorAll('[data-wf-iv]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.wfIv===iv)));
    try{
      let d=preloaded&&iv===interval?preloaded:null;
      if(!d){
        d=isAsset
          ?await fetchAssetFlow(subject,{chain,interval:iv,limit,signal})
          :await fetchWalletFlow(subject,{chain,interval:iv,limit,signal});
      }
      if(!alive||signal?.aborted)return;paint(d);
    }catch(e){
      if(!alive||signal?.aborted)return;
      paint({schema:isAsset?'ticking.asset-flow.v1':'ticking.wallet-flow.v1',address:subject,mint:subject,chain,interval:iv,series:stubSeries(0,iv),primary:{series:stubSeries(0,iv),tokens:[]},mapped:[],alerts:[],depth:{note:String(e.message||e)},totalUsd:0,realizedPnl:0,unrealizedPnl:0,pnlStatus:'unavailable',framingNote:'Flow API unavailable — stub chart.',flowSummary:{netInUsd:0,netOutUsd:0,netDeltaUsd:0,marketsTokenCount:0,marketsHoldingsUsd:0}});
    }
  }

  wrap.addEventListener('click',e=>{
    const iv=e.target.closest('[data-wf-iv]');if(iv)return void load(iv.dataset.wfIv);
  });
  wrap.addEventListener('change',e=>{
    const t=e.target.closest('[data-wf-tog]');if(!t)return;
    if(t.checked)visible.add(t.dataset.wfTog);else visible.delete(t.dataset.wfTog);
    if(t.dataset.wfTog==='entity'){t.checked=true;visible.add('entity');}
    if(data)paint(data);
  });
  const ro=typeof ResizeObserver!=='undefined'?new ResizeObserver(()=>{if(data)paint(data);}):null;
  ro?.observe(wrap);
  await load(curIv);
  return {
    destroy(){alive=false;ro?.disconnect();host.innerHTML='';},
    data:()=>data,reload:()=>load(curIv)
  };
}

export const mountAssetFlowChart=(host,opts)=>mountWalletFlowChart(host,{...opts,kind:'asset',mint:opts.mint||opts.address});

/** Compact answer-slot mount for Muse/Ask HTML hosts (`[data-wf-answer]`). */
export async function hydrateFlowAnswers(root,{signal}={}){
  const nodes=[...(root?.querySelectorAll?.('[data-wf-answer]:not([data-wf-ready])')||[])];
  for(const el of nodes){
    el.dataset.wfReady='1';
    let payload=null;
    try{payload=JSON.parse(el.getAttribute('data-wf-payload')||'null');}catch{payload=null;}
    const kind=el.dataset.wfKind||payload?.kind||'wallet';
    const address=el.dataset.wfAddress||payload?.address||payload?.mint;
    const chain=el.dataset.wfChain||payload?.chain||'ethereum';
    const label=el.dataset.wfLabel||payload?.label||(kind==='asset'?'Asset Flow':kind==='crosschain'?'Cross-chain Flow':'Wallet Flow');
    try{
      const mountKind=kind==='crosschain'?'wallet':kind;
      await mountWalletFlowChart(el,{address,mint:kind==='asset'?address:null,chain,kind:mountKind,label,compact:true,limit:96,interval:'1h',signal,preloaded:payload?.data||null});
    }catch(e){el.innerHTML=`<p class="wf-note">Flow chart unavailable. ${esc(String(e.message||e))}</p>`;}
  }
}

export function flowAnswerMarkup({kind='wallet',address,chain,label,summary,data}){
  const sum=summary||summaryOf(data||{});
  const payload=esc(JSON.stringify({kind,address,chain,label,data:data?{schema:data.schema,address:data.address,mint:data.mint,chain:data.chain,interval:data.interval,series:data.series||data.primary?.series,primary:data.primary,mapped:(data.mapped||[]).slice(0,6),groups:data.groups,totalUsd:data.totalUsd,flowSummary:data.flowSummary,asset:data.asset,aggregateMode:data.aggregateMode,entityName:data.entityName,framingNote:data.framingNote,markers:data.markers||data.dumpTags||[],dumpTags:data.dumpTags||data.markers||[],alerts:(data.alerts||[]).slice(0,12)}:null}).slice(0,120000));
  return `<section class="wf-answer" data-wf-answer data-wf-kind="${esc(kind)}" data-wf-address="${esc(address||'')}" data-wf-chain="${esc(chain||'')}" data-wf-label="${esc(label||'')}" data-wf-payload='${payload}'>
    <div class="wf-answer-sum"><b>${esc(label||(kind==='asset'?'Asset Flow':'Wallet Flow'))}</b>
      <span class="up">In ${money(sum.netIn)}</span>
      <span class="down">Out ${money(sum.netOut)}</span>
      <span>Net ${signedMoney(sum.netDelta)}</span>
    </div>
    <p class="wf-note">Loading Flow chart…</p>
  </section>`;
}

export function mountFlowAlerts(host,{items=[],onOpen}={}){
  styles();
  let root=host.querySelector('[data-wf-toasts]');
  if(!root){root=document.createElement('div');root.className='wf-toasts';root.setAttribute('data-wf-toasts','');host.append(root);}
  function push(a){
    const el=document.createElement('button');el.type='button';el.className='wf-toast '+(a.kind==='in'?'in':'out');
    el.innerHTML=`<span class="wf-toast-dot"></span><span><b>${esc(a.label||'Large flow')}</b><small>${money(a.usd??0)} · ${esc(a.chain||'')}</small></span>`;
    el.onclick=()=>onOpen?.(a);
    root.prepend(el);
    while(root.children.length>6)root.lastChild.remove();
    setTimeout(()=>el.classList.add('on'),20);
    setTimeout(()=>{el.classList.remove('on');setTimeout(()=>el.remove(),400);},9000);
  }
  for(const a of items.slice(0,3))push(a);
  return {push,destroy(){root.remove();}};
}

let styled=false;
function styles(){if(styled)return;styled=true;const s=document.createElement('style');s.id='wallet-flow-style';s.textContent=`
.wf-chart{display:grid;gap:10px;padding:14px 16px 12px;background:var(--tk-surface);border:1px solid var(--tk-line);border-radius:14px;color:var(--tk-ink);position:relative}
.wf-chart.is-compact{padding:10px 12px}
.wf-badge{display:flex;align-items:center;gap:8px}
.wf-badge>span{font:800 10px/1 var(--tk-font);letter-spacing:.08em;padding:4px 8px;border-radius:999px;background:color-mix(in srgb,var(--tk-accent) 18%,transparent);color:var(--tk-accent);border:1px solid color-mix(in srgb,var(--tk-accent) 40%,var(--tk-line))}
.wf-badge>small{color:var(--tk-muted);font-size:11px}
.wf-head{display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap}
.wf-title{min-width:0;flex:1}.wf-title b{font:650 15px var(--tk-display);display:block}.wf-title small{color:var(--tk-muted);font-size:11px}
.wf-stats{display:flex;gap:14px;flex-wrap:wrap}.wf-stats span{display:grid;gap:2px}.wf-stats small{font-size:10px;color:var(--tk-muted);text-transform:uppercase;letter-spacing:.03em}.wf-stats b{font:650 14px var(--tk-display)}.wf-stats b.up{color:var(--tk-up)}.wf-stats b.down{color:var(--tk-down)}
.wf-flow-pair b{font-variant-numeric:tabular-nums}
.wf-flowbar{display:flex;height:8px;border-radius:999px;overflow:hidden;background:var(--tk-bg-sub);gap:2px}
.wf-flowbar i{display:block;min-width:4px;transition:flex-grow .45s ease}
.wf-flowbar [data-wf-inbar]{background:linear-gradient(90deg,color-mix(in srgb,var(--tk-up) 70%,transparent),var(--tk-up))}
.wf-flowbar [data-wf-outbar]{background:linear-gradient(90deg,var(--tk-down),color-mix(in srgb,var(--tk-down) 70%,transparent))}
.wf-ivals{display:inline-flex;gap:4px;background:var(--tk-surface-2);padding:3px;border-radius:9px;margin-left:auto}
.wf-ivals button{border:0;background:transparent;height:26px;padding:0 10px;border-radius:7px;font:600 11px var(--tk-font);color:var(--tk-muted);cursor:pointer}
.wf-ivals button[aria-pressed=true]{background:var(--tk-surface);color:var(--tk-ink);box-shadow:var(--tk-shadow)}
.wf-legend{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;font-size:11px}.wf-leg-k{color:var(--tk-muted);font-weight:700;letter-spacing:.04em;text-transform:uppercase;font-size:10px;margin-right:4px}
.wf-leg{display:inline-flex;align-items:center;gap:6px;cursor:pointer;user-select:none}.wf-leg input{accent-color:var(--tk-accent)}.wf-leg i{width:8px;height:8px;border-radius:50%;flex:none}.wf-leg b{font-weight:600}.wf-leg small{color:var(--tk-muted)}
.wf-canvas-wrap{border-radius:10px;overflow:hidden;background:var(--tk-bg-sub);border:1px solid color-mix(in srgb,var(--tk-line) 80%,transparent)}
.wf-canvas{display:block;width:100%;height:280px}
.wf-chart.is-compact .wf-canvas{height:200px}
.wf-tokens{display:flex;gap:8px;flex-wrap:wrap;font-size:11px;color:var(--tk-muted)}
.wf-chip{padding:4px 8px;border-radius:999px;background:var(--tk-bg-sub);border:1px solid var(--tk-line);opacity:0;transform:translateY(6px);animation:wf-chip-in .35s ease forwards;animation-delay:calc(var(--i,0)*.04s)}
.wf-chip b{color:var(--tk-ink);margin-right:4px}
.wf-chip-tok{border-color:color-mix(in srgb,#f5b041 45%,var(--tk-line))}
.wf-chip-stb{border-color:color-mix(in srgb,#36c6bc 45%,var(--tk-line))}
.wf-chip-cry{border-color:color-mix(in srgb,#60a5fa 35%,var(--tk-line))}
.wf-mkt{display:inline-block;margin-left:6px;padding:1px 6px;border-radius:999px;font:700 9px/1.4 var(--tk-font);letter-spacing:.04em;text-transform:uppercase;color:var(--tk-accent);background:color-mix(in srgb,var(--tk-accent) 14%,transparent);border:1px solid color-mix(in srgb,var(--tk-accent) 35%,var(--tk-line));font-style:normal}
@keyframes wf-chip-in{to{opacity:1;transform:none}}
.wf-pulse .wf-stats b{animation:wf-flash .55s ease}
@keyframes wf-flash{0%{filter:brightness(1.35)}100%{filter:none}}
.wf-note{margin:0;font-size:11px;color:var(--tk-faint)}
.wf-dumps{margin:0;font-size:11px;color:#fdba74;line-height:1.45}
.wf-answer{margin:10px 0;padding:0}
.wf-answer-sum{display:flex;flex-wrap:wrap;gap:10px 14px;align-items:baseline;margin-bottom:8px;font-size:12px}
.wf-answer-sum b{font:650 13px var(--tk-display)}.wf-answer-sum .up{color:var(--tk-up)}.wf-answer-sum .down{color:var(--tk-down)}
.wf-toasts{position:fixed;right:18px;bottom:22px;z-index:80;display:grid;gap:8px;width:min(340px,calc(100vw - 32px));pointer-events:none}
.wf-toast{pointer-events:auto;display:flex;gap:10px;align-items:center;text-align:left;border:1px solid var(--tk-line);background:color-mix(in srgb,var(--tk-surface) 92%,transparent);backdrop-filter:blur(10px);border-radius:12px;padding:12px 14px;color:var(--tk-ink);box-shadow:var(--tk-shadow-pop);transform:translateY(12px);opacity:0;transition:transform .35s ease,opacity .35s ease;cursor:pointer;font:inherit}
.wf-toast.on{transform:none;opacity:1}.wf-toast.in{border-color:color-mix(in srgb,var(--tk-up) 45%,var(--tk-line))}.wf-toast.out{border-color:color-mix(in srgb,var(--tk-down) 45%,var(--tk-line))}
.wf-toast-dot{width:8px;height:8px;border-radius:50%;background:var(--tk-accent);box-shadow:0 0 10px #6566ea66;flex:none}.wf-toast.in .wf-toast-dot{background:var(--tk-up)}.wf-toast.out .wf-toast-dot{background:var(--tk-down)}
.wf-toast b{display:block;font-size:13px}.wf-toast small{color:var(--tk-muted);font-size:11px}
.wf-featured{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin:18px 0 8px}
.wt-flow-slot{margin:8px 0 22px}
@media(max-width:900px){.wf-featured{grid-template-columns:1fr}.wf-stats{width:100%}.wf-ivals{margin-left:0}}
`;document.head.append(s);}
