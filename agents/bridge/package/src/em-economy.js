// Economic Machine for one Arbitrum One / Robinhood Chain market: where the supply sits, who is leveraged and where money
// moves (panel), liquidation levels and economic events on the chart (overlays), and the assistant's economy card.
// Every number comes from /api/public/em/tools/{economy,events}; each section states its source and as-of time.
import {em,esc,short,usd,num,emitWallet,stateHTML,isEmMarket,chainOfMarket,walletIcon,injectStyles,CHAINS} from './em-api.js';

const COLORS={lending_collateral:'#5658e8',dex_liquidity:'#17856d',exchanges:'#b7791f',whales:'#3b6fd6',retail:'#7d80ea',bridges:'#2fb3c9',
 vaults_contracts:'#9a7fd1',issuer:'#6b7088',unknown:'#a3a8b8',other:'#d8dbe6',burned:'#6b7088'};
const EVENT={whale_buy:['Whale buy','#17856d','▲'],whale_sell:['Whale sell','#c76678','▼'],collateral_in:['Collateral in','#5658e8','+'],
 collateral_out:['Collateral out','#b7791f','−'],borrow:['Borrow','#e8a14b','B'],repay:['Repay','#2fb3c9','R'],liquidation:['Liquidation','#d1435b','!'],
 bridge_in:['Bridge in','#3b6fd6','↘'],bridge_out:['Bridge out','#9a7fd1','↗'],lp_add:['Liquidity added','#2c9c8c','L'],lp_remove:['Liquidity removed','#b7791f','L']};
const CLASS={whale:'Whales',retail:'Retail wallets',mev:'MEV bots',exchange:'Exchanges',contract:'Contracts & vaults'};
const proto=p=>/^v\d$/i.test(String(p||''))?String(p).toUpperCase()+' pools':/^other$/i.test(String(p||''))?'Other pools':String(p||'').split(':')[0].replace(/_/g,' ').replace(/\b(v\d)\b/gi,m=>m.toUpperCase()).replace(/\b[a-z]/g,c=>c.toUpperCase());
const away=d=>Number.isFinite(d)?`${Math.abs(d).toFixed(1)}% ${d<0?'below':'above'}`:'—';
const pct=v=>Number.isFinite(v)?(v*100>=10?(v*100).toFixed(0):(v*100).toFixed(1))+'%':'—';
const signed=v=>Number.isFinite(v)?(v>0?'+':v<0?'−':'')+usd(Math.abs(v)):'—';
export function ago(t){if(!Number.isFinite(t))return 'time unknown';const s=Math.max(0,Date.now()/1000-t),r=s<90?'just now':s<5400?Math.round(s/60)+' min ago':s<172800?Math.round(s/3600)+' h ago':Math.round(s/86400)+' days ago';
 return `${r} · ${new Date(t*1000).toISOString().slice(0,16).replace('T',' ')} UTC`;}
const sourceChip=s=>`<span class="emx-chip emx-chip-${esc(s?.status||'pending')}">${s?.status==='live'?'Live analytics':s?.status==='snapshot'?'Map snapshot':'Pending'}</span>`;
const foot=s=>s&&s.status!=='pending'?`<div class="emx-foot"><p>${sourceChip(s)} ${esc(s.source||'')} · ${esc(ago(s.asOf))}${Number.isFinite(s.data?.coverage?.unpricedShare)?` · ${pct(s.data.coverage.unpricedShare)} of supply unpriced`:''}</p>${(s.data?.coverage?.notes||[]).length?`<details><summary>Method</summary>${s.data.coverage.notes.map(n=>'<p>'+esc(n)+'</p>').join('')}</details>`:''}</div>`:'';
const pending=text=>`<div class="emx-empty">${sourceChip({status:'pending'})}<p>${esc(text)}</p></div>`;
const wallet=(w,chain,label)=>/^0x[0-9a-f]{40}$/.test(w||'')?`<button type="button" class="emx-wallet" data-emx-wallet="${w}" data-emx-chain="${chain}" aria-label="Open wallet ${esc(w)}">${esc(label||short(w))}</button>`:'—';
const skeleton=`<div class="emx-skel" aria-hidden="true"><i style="height:22px;width:42%"></i><i style="height:14px"></i><i style="height:120px"></i><i style="height:120px"></i><i style="height:90px"></i></div>`;

function stateSection(s,token){
 const d=s?.data;if(!d?.buckets?.length)return `<section class="emx-card" aria-labelledby="emx-state"><h4 id="emx-state">Money state</h4>${pending('The supply breakdown for '+token.symbol+' is being computed.')}</section>`;
 const rows=d.buckets.filter(b=>Number.isFinite(b.share)&&b.share>=.001).sort((a,b)=>(a.key==='other')-(b.key==='other')||b.share-a.share),lead=rows.find(b=>b.key!=='other');
 rows.forEach(b=>{b.protocols=(b.protocols||[]).filter(p=>!(p.share<.005)).slice(0,3).map(p=>({...p,name:proto(p.name)}));});
 const bar=rows.map(b=>`<i style="flex:${Math.max(b.share,.004)};--c:${COLORS[b.key]||'#a3a8b8'}" title="${esc(b.label)} ${pct(b.share)}"></i>`).join('');
 const story=lead?`${pct(lead.share)} of ${esc(token.symbol)} supply ${lead.key==='lending_collateral'?'is posted as lending collateral':lead.key==='dex_liquidity'?'sits in DEX liquidity':lead.key==='exchanges'?'sits on exchanges':'is held by '+esc(lead.label.toLowerCase())}${lead.protocols?.[0]?` — mostly ${esc(lead.protocols[0].name)}`:''}.`:'';
 return `<section class="emx-card" aria-labelledby="emx-state"><h4 id="emx-state">Money state</h4><p class="emx-story">${story}</p>
  <div class="emx-stack" role="img" aria-label="${esc(rows.map(b=>b.label+' '+pct(b.share)).join(', '))}">${bar}</div>
  <ul class="emx-list">${rows.map(b=>`<li><i style="--c:${COLORS[b.key]||'#a3a8b8'}"></i><span>${esc(b.label)}${b.protocols?.length?`<small>${b.protocols.map(p=>esc(p.name)+' '+pct(p.share)).join(' · ')}</small>`:''}</span><b class="em-mono">${pct(b.share)}</b>${Number.isFinite(b.usd)?`<small class="em-mono">${usd(b.usd)}</small>`:''}${Number.isFinite(b.d24hUsd)?`<small class="em-mono ${b.d24hUsd>0?'positive':b.d24hUsd<0?'negative':''}">${signed(b.d24hUsd)} in 24h</small>`:Number.isFinite(b.d24h)?`<small class="em-mono">${b.d24h>0?'+':''}${num(b.d24h)} ${esc(token.symbol)} in 24h</small>`:''}</li>`).join('')}</ul>${foot(s)}</section>`;
}

function leverageSection(s,token,chain){
 const d=s?.data;
 if(d?.ladder?.length||d?.positions?.length){const max=Math.max(...(d.ladder||[]).map(r=>r.debtUsd||0),1),price=d.price;
  return `<section class="emx-card" aria-labelledby="emx-lev"><h4 id="emx-lev">Leverage & liquidations</h4><p class="emx-story">${num(d.totals?.positions)} positions borrow ${usd(d.totals?.debtUsd)} against ${esc(token.symbol)}.${Number.isFinite(price)?` Price now ${esc(num(price))} USD.`:''}</p>
   <div class="emx-ladder" role="img" aria-label="Liquidation ladder: debt that becomes liquidatable at each price">${(d.ladder||[]).slice(0,14).map(r=>`<div><span class="em-mono">${esc(num(r.price))}</span><i style="width:${Math.max(2,100*(r.debtUsd||0)/max)}%"></i><b class="em-mono">${usd(r.debtUsd)}</b></div>`).join('')}</div>
   <table class="emx-table"><caption class="sr-only">Nearest positions to liquidation</caption><thead><tr><th>Wallet</th><th>Protocol</th><th>Liquidation</th><th>Distance</th><th>Debt</th></tr></thead><tbody>${(d.positions||[]).slice(0,8).map(p=>`<tr><td>${wallet(p.wallet,chain)}</td><td>${esc(proto(p.protocol))}</td><td class="em-mono">${esc(num(p.liqPrice))}</td><td class="em-mono ${Math.abs(p.distancePct)<5?'negative':''}">${away(p.distancePct)}</td><td class="em-mono">${usd(p.debtUsd)}</td></tr>`).join('')}</tbody></table>${foot(s)}</section>`;}
 if(d?.borrowers?.length)return `<section class="emx-card" aria-labelledby="emx-lev"><h4 id="emx-lev">Leverage & liquidations</h4><p class="emx-story">${d.borrowers.length} wallet${d.borrowers.length===1?'':'s'} borrow ${usd(d.borrowers.reduce((a,b)=>a+(b.debtUsd||0),0))} USDG on Morpho Blue against ${esc(token.symbol)}.</p>
   <table class="emx-table"><caption class="sr-only">Borrowers using this token as collateral</caption><thead><tr><th>Wallet</th><th>Collateral</th><th>Debt</th><th>Net worth</th></tr></thead><tbody>${d.borrowers.slice(0,8).map(b=>`<tr><td>${wallet(b.wallet,chain)}</td><td class="em-mono">${usd(b.collateralUsd)}</td><td class="em-mono">${usd(b.debtUsd)}</td><td class="em-mono">${usd(b.netWorth)}</td></tr>`).join('')}</tbody></table>${foot(s)}</section>`;
 return `<section class="emx-card" aria-labelledby="emx-lev"><h4 id="emx-lev">Leverage & liquidations</h4>${pending('No lending positions use '+token.symbol+' as collateral in the current data, or liquidation levels are still being computed.')}</section>`;
}

function flowsSection(s,token,win){
 const d=s?.data,tabs=['1h','24h','7d'].map(w=>`<button type="button" role="radio" aria-checked="${w===win}" data-emx-window="${w}">${w}</button>`).join('');
 let body;
 if(d?.classes?.length){const max=Math.max(...d.classes.map(c=>Math.abs(c.netUsd||0)),1);
  body=`<ul class="emx-flows">${d.classes.map(c=>`<li><span>${esc(c.label||CLASS[c.class]||c.class)}</span><i class="${(c.netUsd||0)>=0?'in':'out'}" style="--w:${Math.max(2,50*Math.abs(c.netUsd||0)/max)}%"></i><b class="em-mono ${(c.netUsd||0)>0?'positive':(c.netUsd||0)<0?'negative':''}">${signed(c.netUsd)}</b><small class="em-mono">${usd(c.buyUsd)} in · ${usd(c.sellUsd)} out</small></li>`).join('')}</ul>`;}
 else if(d?.actions?.length){const max=Math.max(...d.actions.map(a=>a.usd||0),1);
  body=`<ul class="emx-flows emx-actions">${d.actions.slice(0,8).map(a=>`<li><span>${esc(String(a.action||'').replace(/_/g,' ').replace(/^./,c=>c.toUpperCase()))}</span><i class="in" style="--w:${Math.max(2,100*(a.usd||0)/max)}%"></i><b class="em-mono">${usd(a.usd)}</b><small class="em-mono">${num(a.n)} actions</small></li>`).join('')}</ul>`;}
 else body=pending('Flows by wallet class for this window are being computed.');
 return `<section class="emx-card" aria-labelledby="emx-flow"><div class="emx-row"><h4 id="emx-flow">Flows</h4><div class="emx-seg" role="radiogroup" aria-label="Flow window">${tabs}</div></div>${body}${foot(s)}</section>`;
}

function venuesSection(d){
 if(!d.venues?.length)return '';
 return `<section class="emx-card" aria-labelledby="emx-where"><h4 id="emx-where">Where it sits</h4><ul class="emx-list">${d.venues.map(v=>`<li><i style="--c:${COLORS[v.bucket]||(String(v.bucket).startsWith('lending')?COLORS.lending_collateral:'#a3a8b8')}"></i><span>${esc(v.name)}${v.contracts>1?`<small>${v.contracts} contracts</small>`:''}</span><b class="em-mono">${pct(v.share)}</b>${v.usd?`<small class="em-mono">${usd(v.usd)}</small>`:''}</li>`).join('')}</ul><p class="emx-foot">Protocol and venue contracts holding the token, from the largest holders.</p></section>`;
}

function walletsSection(d,chain){
 if(!d.wallets?.length)return '';
 return `<section class="emx-card" aria-labelledby="emx-who"><h4 id="emx-who">Notable wallets</h4><ul class="emx-who">${d.wallets.map(w=>`<li>${walletIcon(w.category||'unknown',26)}<span>${wallet(w.wallet,chain,w.entity||short(w.wallet))}<small>${esc(w.label||'Unlabeled holder')} · ${esc(w.role||'')}</small></span></li>`).join('')}</ul><p class="emx-foot">Holders, borrowers and the largest 24h traders, ranked by position size. Select one to open its dossier.</p></section>`;
}

/** Economy panel (Pro data tab `?panel=economy` and the token page). */
export function mountEconomyPanel(view,{token,compact=false,toggles=!compact}={}){
 injectStyles();injectEconomyStyles();const abort=new AbortController();let win='24h',data=null;
 if(!isEmMarket(token)){view.innerHTML=`<div class="emx-empty"><p>The Economic Machine covers Arbitrum One and Robinhood Chain markets. Open <a href="#/console/workspace/robinhood%3A0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec?panel=economy">NVDA on Robinhood Chain</a> or <a href="#/console/workspace/arbitrum%3A0x912ce59144191c1204e64559fe8253a0e49e6548?panel=economy">ARB on Arbitrum One</a>.</p></div>`;return {token:token?.id,destroy(){}};}
 const chain=chainOfMarket(token);
 view.innerHTML=`<div class="emx${compact?' emx-compact':''}" aria-busy="true"><header class="emx-head"><div><h3>Economic Machine · ${esc(token.symbol)}</h3><p>Where ${esc(token.symbol)} sits economically, who is leveraged on it and where money is moving on ${esc(CHAINS[chain]?.name||chain)}.</p></div>${!toggles?'':`<div class="emx-overlay-toggles" role="group" aria-label="Chart overlays"><button type="button" data-emx-overlay="events" aria-pressed="${overlayState.events}">Events on chart</button><button type="button" data-emx-overlay="liquidations" aria-pressed="${overlayState.liquidations}">Liquidation levels</button></div>`}</header><div class="emx-body">${skeleton}</div></div>`;
 const root=view.querySelector('.emx'),body=view.querySelector('.emx-body');
 const render=()=>{root.setAttribute('aria-busy','false');body.innerHTML=stateSection(data.state,token)+venuesSection(data)+leverageSection(data.leverage,token,chain)+flowsSection(data.flows,token,win)+walletsSection(data,chain);};
 const load=()=>em('tools/economy?'+new URLSearchParams({market:token.id,window:win}),{signal:abort.signal}).then(d=>{if(abort.signal.aborted)return;data=d;render();})
  .catch(e=>{if(e.name==='AbortError')return;root.setAttribute('aria-busy','false');body.innerHTML=stateHTML('error',e.message||'The Economic Machine is unavailable right now.',{retry:true});});
 view.addEventListener('click',e=>{const w=e.target.closest('[data-emx-wallet]');if(w){emitWallet({address:w.dataset.emxWallet,chain:w.dataset.emxChain,market:token.id});return;}
  const t=e.target.closest('[data-emx-window]');if(t&&t.dataset.emxWindow!==win){win=t.dataset.emxWindow;body.querySelectorAll('[data-emx-window]').forEach(b=>b.setAttribute('aria-checked',String(b.dataset.emxWindow===win)));load();return;}
  const o=e.target.closest('[data-emx-overlay]');if(o){setOverlay(o.dataset.emxOverlay,o.getAttribute('aria-pressed')!=='true');view.querySelectorAll('[data-emx-overlay]').forEach(b=>b.setAttribute('aria-pressed',String(overlayState[b.dataset.emxOverlay])));return;}
  if(e.target.closest('[data-em-retry]')){body.innerHTML=skeleton;load();}},{signal:abort.signal});
 view.addEventListener('keydown',e=>{const t=e.target.closest('[data-emx-window]');if(!t||!['ArrowLeft','ArrowRight'].includes(e.key))return;e.preventDefault();const all=[...body.querySelectorAll('[data-emx-window]')],n=all[(all.indexOf(t)+(e.key==='ArrowRight'?1:all.length-1))%all.length];n.focus();n.click();},{signal:abort.signal});
 load();
 return {token:token.id,destroy(){abort.abort();}};
}

// ---- chart overlays: economic events (on by default) and liquidation levels (off by default) ------------------------
const overlayState={events:true,liquidations:false};
try{const saved=JSON.parse(localStorage.getItem('ticking-em-overlays')||'null');if(saved&&typeof saved==='object')Object.assign(overlayState,{events:saved.events!==false,liquidations:saved.liquidations===true});}catch{}
function setOverlay(kind,on){overlayState[kind]=on;try{localStorage.setItem('ticking-em-overlays',JSON.stringify(overlayState));}catch{}document.dispatchEvent(new CustomEvent('emxoverlay'));}

/** Keeps the overlays on the workspace chart for the open EVM market; survives zoom, pan, interval and market changes. */
export function attachEconomyOverlays({getToken,getChart}){
 injectStyles();injectEconomyStyles();let current=null,abort=null,layer=null,bands=null,legend=null,tip=null,frame=0,events=[],ladder=[],host=null;
 const clear=()=>{abort?.abort();abort=null;[layer,bands,legend,tip].forEach(n=>n?.remove());layer=bands=legend=tip=null;events=[];ladder=[];host?.removeEventListener('chartframe',schedule);host=null;current=null;};
 function closeAt(t){const s=getChart()?.options?.series;if(!s?.length)return null;const ms=t*1000;let lo=0,hi=s.length-1;if(ms<s[0].time)return null;while(lo<hi){const m=(lo+hi+1)>>1;if(s[m].time<=ms)lo=m;else hi=m-1;}return s[lo].close;}
 function paint(){frame=0;const chart=getChart(),v=chart?.view,cv=chart?.canvas;if(!v||!cv||!layer)return;const w=v.width||cv.clientWidth,h=v.height||cv.clientHeight,last=chart.options.series?.at(-1);
  for(const el of [layer,bands]){el.style.left=cv.offsetLeft+'px';el.style.top=cv.offsetTop+'px';el.style.width=w+'px';el.style.height=h+'px';}
  layer.hidden=!overlayState.events;bands.hidden=!overlayState.liquidations||!ladder.length;
  let shown=0;[...layer.children].forEach((el,i)=>{const ev=events[i],price=closeAt(ev.time);let p=null;try{p=price&&v.screen({time:ev.time*1000,price});}catch{}
   if(!p||p.x<0||p.x>w||p.y<0||p.y>h){el.hidden=true;return;}el.hidden=false;shown++;el.style.left=p.x+'px';el.style.top=Math.max(8,p.y-14)+'px';});
  [...bands.children].forEach((el,i)=>{const r=ladder[i];let p=null;try{p=last&&v.screen({time:last.time,price:r.price});}catch{}if(!p||p.y<0||p.y>h){el.hidden=true;return;}el.hidden=false;el.style.top=p.y+'px';});
  legend.querySelector('[data-emx-count]').textContent=events.length?`${shown} of ${events.length} in view`:'none recorded';legend.querySelector('[data-emx-fit]').hidden=!events.length||shown===events.length||!overlayState.events;}
 function fit(){const chart=getChart();if(!events.length||!chart)return;const span=Date.now()/1000-Math.min(...events.map(x=>x.time)),bars=Math.max(60,chart.visible?.length||120);
  const choice=[['15m',900],['1h',3600],['4h',14400],['1d',86400]].find(([,sec])=>span<=bars*sec*.9)?.[0]||'1d',panel=chart.container.closest('.pro-chart-panel')||document;
  if(chart.options.interval!==choice)[...panel.querySelectorAll('button')].find(b=>b.textContent.trim()===choice)?.click();chart.container.closest('.pro-chart-panel')?.querySelector('[data-chart-latest],.chart-latest')?.click();setTimeout(schedule,1200);}
 function schedule(){if(!frame)frame=requestAnimationFrame(paint);}
 async function sync(){const token=getToken(),chart=getChart();if(!isEmMarket(token)||!chart){clear();return;}if(current===token.id&&host===chart.container){schedule();return;}
  clear();current=token.id;host=chart.container;abort=new AbortController();const signal=abort.signal;
  layer=document.createElement('div');layer.className='emx-events';bands=document.createElement('div');bands.className='emx-bands';legend=document.createElement('div');legend.className='emx-legend';tip=document.createElement('div');tip.className='emx-tip';tip.hidden=true;tip.id='emx-tip';tip.setAttribute('role','tooltip');
  legend.innerHTML=`<button type="button" data-emx-overlay="events" aria-pressed="${overlayState.events}">Economic events · <span data-emx-count>loading</span></button><button type="button" data-emx-fit hidden>Fit events</button><button type="button" data-emx-overlay="liquidations" aria-pressed="${overlayState.liquidations}">Liquidation levels</button>`;
  host.append(bands,layer,legend,tip);host.addEventListener('chartframe',schedule);
  try{const now=Math.floor(Date.now()/1000),[ev,eco]=await Promise.all([em('tools/events?'+new URLSearchParams({market:token.id,from:String(now-30*86400),to:String(now)}),{signal}),em('tools/economy?'+new URLSearchParams({market:token.id,window:'24h'}),{signal}).catch(()=>null)]);
   if(signal.aborted)return;events=(ev.events||[]).filter(x=>Number.isFinite(x.time)&&EVENT[x.kind]).slice(0,200);ladder=(eco?.leverage?.data?.ladder||[]).filter(r=>Number.isFinite(r.price)&&r.price>0&&r.debtUsd>0).slice(0,12);
   layer.innerHTML=events.map((x,i)=>{const [label,color,mark]=EVENT[x.kind];return `<button type="button" class="emx-ev" data-i="${i}" style="--c:${color}" aria-label="${esc(label+' '+usd(x.usd)+' · '+new Date(x.time*1000).toISOString().slice(0,16).replace('T',' ')+' UTC')}" aria-describedby="emx-tip">${mark}</button>`;}).join('');
   bands.innerHTML=ladder.map(r=>`<div class="emx-band"><span>${esc(usd(r.debtUsd))} liquidatable at ${esc(num(r.price))}</span></div>`).join('');
   legend.querySelector('[data-emx-overlay=liquidations]').disabled=!ladder.length;legend.querySelector('[data-emx-overlay=liquidations]').title=ladder.length?'':eco?.leverage?.status==='live'?'No liquidatable debt in the current ladder':'Liquidation levels arrive with live analytics';schedule();}
  catch(e){if(e.name!=='AbortError'&&legend)legend.querySelector('[data-emx-count]').textContent='unavailable';}}
 const show=el=>{const x=events[+el.dataset.i];if(!x||!tip)return;const [label]=EVENT[x.kind];tip.innerHTML=`<b>${esc(label)} · ${esc(usd(x.usd))}</b><small>${esc(x.text||'')}</small><small>${esc(new Date(x.time*1000).toISOString().slice(0,16).replace('T',' '))} UTC${x.protocol?' · '+esc(x.protocol):''}</small>`;
  tip.hidden=false;const r=el.getBoundingClientRect(),hr=host.getBoundingClientRect();tip.style.left=Math.min(hr.width-270,Math.max(4,r.left-hr.left-20))+'px';tip.style.top=(r.top-hr.top+26)+'px';};
 const onOver=e=>{const el=e.target.closest?.('.emx-ev');if(el)show(el);},onOut=e=>{if(e.target.closest?.('.emx-ev')&&tip)tip.hidden=true;},
  onClick=e=>{const el=e.target.closest?.('.emx-ev');if(el){const x=events[+el.dataset.i];if(x?.wallet)emitWallet({address:x.wallet,chain:chainOfMarket(getToken()),market:current});return;}
   if(e.target.closest?.('.emx-legend [data-emx-fit]')){fit();return;}
   const o=e.target.closest?.('.emx-legend [data-emx-overlay]');if(o){setOverlay(o.dataset.emxOverlay,o.getAttribute('aria-pressed')!=='true');}},
  onOverlay=()=>{legend?.querySelectorAll('[data-emx-overlay]').forEach(b=>b.setAttribute('aria-pressed',String(overlayState[b.dataset.emxOverlay])));schedule();};
 document.addEventListener('pointerover',onOver);document.addEventListener('pointerout',onOut);document.addEventListener('focusin',onOver);document.addEventListener('focusout',onOut);document.addEventListener('click',onClick);document.addEventListener('emxoverlay',onOverlay);const onFit=()=>setTimeout(fit,1500);document.addEventListener('emxfit',onFit);
 const timer=setInterval(sync,1500);sync();
 return {sync,destroy(){clearInterval(timer);clear();document.removeEventListener('pointerover',onOver);document.removeEventListener('pointerout',onOut);document.removeEventListener('focusin',onOver);document.removeEventListener('focusout',onOut);document.removeEventListener('click',onClick);document.removeEventListener('emxoverlay',onOverlay);document.removeEventListener('emxfit',onFit);}};
}

/** Assistant card for the economy intents. */
export function economyCard(c){
 injectEconomyStyles();const facts=(c.facts||[]).slice(0,6),chain=c.network||'robinhood';
 const route=`#/console/workspace/${encodeURIComponent(c.market||'')}?panel=economy`;
 return `<section class="em-card emx-assist"><header><div><b>${esc(c.title||'Economic Machine')}</b><small>${esc(c.symbol||'')} · ${esc(CHAINS[chain]?.name||chain)} · ${esc(ago(c.asOf))}</small></div></header>
  ${c.headline?`<p class="emx-story">${esc(c.headline)}</p>`:''}${facts.length?`<ul class="emx-facts">${facts.map(f=>`<li><span>${esc(f.label)}</span><b class="em-mono">${esc(f.value)}</b></li>`).join('')}</ul>`:''}
  ${c.note?`<p class="emx-foot">${esc(c.note)}</p>`:''}<div class="em-card-actions">${/^(arbitrum|robinhood):0x[0-9a-f]{40}$/.test(c.market||'')?`<a class="em-btn em-btn-primary" href="${route}">Open Economy panel</a><a class="em-btn" href="#/console/workspace/${encodeURIComponent(c.market)}" data-emx-show="${esc(c.overlay||'events')}">Show on chart</a>`:''}</div></section>`;
}
if(typeof document!=='undefined')document.addEventListener('click',e=>{const a=e.target.closest?.('[data-emx-show]');if(a){setOverlay(a.dataset.emxShow==='liquidations'?'liquidations':'events',true);if(a.dataset.emxShow!=='liquidations')document.dispatchEvent(new CustomEvent('emxfit'));}});

let styled=false;
function injectEconomyStyles(){if(styled||typeof document==='undefined'||document.getElementById('emx-styles'))return;styled=true;const s=document.createElement('style');s.id='emx-styles';s.textContent=`
.emx{display:grid;gap:14px;padding:4px 2px 12px;color:var(--em-ink);font:13px/1.5 var(--font,"DM Sans"),sans-serif}
.emx-head{display:flex;flex-wrap:wrap;align-items:flex-start;justify-content:space-between;gap:10px}.emx-head h3{margin:0;font:600 17px/1.3 "Space Grotesk",var(--font),sans-serif}.emx-head p{margin:4px 0 0;color:var(--em-muted);max-width:62ch}
.emx-overlay-toggles,.emx-legend{display:flex;gap:6px;flex-wrap:wrap}
.emx-overlay-toggles button,.emx-legend button,.emx-seg button{font:500 12px/1 var(--font,"DM Sans"),sans-serif;padding:7px 10px;border-radius:999px;border:1px solid var(--em-line);background:var(--em-paper);color:var(--em-ink);cursor:pointer;min-height:30px}
.emx-overlay-toggles button[aria-pressed=true],.emx-legend button[aria-pressed=true],.emx-seg button[aria-checked=true]{background:var(--em-accent);border-color:var(--em-accent);color:#fff}
.emx button:focus-visible,.emx-legend button:focus-visible,.emx-ev:focus-visible{outline:2px solid var(--em-accent);outline-offset:2px}
.emx-body{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}.emx-compact .emx-body{grid-template-columns:1fr}
.emx-card{display:grid;gap:10px;align-content:start;padding:14px 16px;border:1px solid var(--em-line);border-radius:14px;background:var(--em-paper);min-width:0}
.emx-card h4{margin:0;font:600 14px/1.3 "Space Grotesk",var(--font),sans-serif}.emx-row{display:flex;align-items:center;justify-content:space-between;gap:8px}
.emx-story{margin:0;font-size:14px}.emx-stack{display:flex;height:14px;border-radius:7px;overflow:hidden;gap:2px;background:var(--em-tint)}.emx-stack i{background:var(--c);min-width:3px}
.emx-list,.emx-flows,.emx-who,.emx-facts{list-style:none;margin:0;padding:0;display:grid;gap:6px}
.emx-list li{display:grid;grid-template-columns:10px 1fr auto;gap:2px 8px;align-items:baseline}.emx-list li>i{width:10px;height:10px;border-radius:3px;background:var(--c);align-self:center}
.emx-list li small{grid-column:2/-1;color:var(--em-muted);font-size:12px}.emx-list li span small{display:block;grid-column:auto}
.emx-flows li{display:grid;grid-template-columns:minmax(90px,1fr) 2fr auto;gap:2px 8px;align-items:center}.emx-flows li small{grid-column:1/-1;color:var(--em-muted);font-size:12px}
.emx-flows i{height:8px;border-radius:4px;width:var(--w);background:#17856d}.emx-flows i.out{background:#c76678}.emx-actions i{background:var(--em-accent)}
.emx-ladder{display:grid;gap:3px}.emx-ladder div{display:grid;grid-template-columns:80px 1fr 70px;gap:8px;align-items:center;font-size:12px}.emx-ladder i{height:10px;border-radius:3px;background:#d1435b;opacity:.75}
.emx-table{width:100%;border-collapse:collapse;font-size:12.5px}.emx-table th{text-align:left;color:var(--em-muted);font-weight:500;padding:4px 6px;border-bottom:1px solid var(--em-line)}.emx-table td{padding:6px;border-bottom:1px solid var(--em-line)}
.emx-wallet{font:500 12.5px/1.2 "Space Grotesk",var(--font),sans-serif;color:var(--em-accent);background:none;border:0;padding:2px 0;cursor:pointer;text-align:left}.emx-wallet:hover{text-decoration:underline}
.emx-who li{display:flex;gap:10px;align-items:center}.emx-who li span{display:grid;min-width:0}.emx-who small{color:var(--em-muted);font-size:12px}
.emx-foot{margin:0;color:var(--em-muted);font-size:11.5px;line-height:1.5}.emx-foot p{margin:0}.emx-foot details{margin-top:4px}.emx-foot summary{cursor:pointer;color:var(--em-accent);width:max-content}.emx-foot details p{margin:4px 0 0}.emx-chip{display:inline-block;padding:1px 7px;border-radius:999px;font:600 10.5px/1.6 var(--font),sans-serif;letter-spacing:.02em;margin-right:4px;background:var(--em-tint);color:var(--em-muted)}
.emx-chip-live{background:color-mix(in srgb,#17856d 15%,var(--em-paper));color:#17856d}.emx-chip-snapshot{background:color-mix(in srgb,#5658e8 12%,var(--em-paper));color:var(--em-accent)}
.emx-empty{display:grid;gap:6px;padding:10px 0;color:var(--em-muted)}.emx-empty p{margin:0}
.emx-skel{display:grid;gap:10px;grid-column:1/-1}.emx-skel i{display:block;border-radius:10px;background:linear-gradient(90deg,var(--em-tint),var(--em-line),var(--em-tint));background-size:200% 100%;animation:emx-sh 1.2s infinite linear}
@keyframes emx-sh{to{background-position:-200% 0}}@media (prefers-reduced-motion:reduce){.emx-skel i{animation:none}}
.emx-events,.emx-bands{position:absolute;pointer-events:none;z-index:4}.emx-events{z-index:5}
.emx-ev{position:absolute;transform:translate(-50%,-50%);pointer-events:auto;width:18px;height:18px;border-radius:50%;border:2px solid var(--em-paper);background:var(--c);color:#fff;font:700 10px/14px var(--font),sans-serif;padding:0;cursor:pointer;box-shadow:0 1px 3px rgba(20,22,40,.25)}
.emx-band{position:absolute;left:0;right:0;height:0;border-top:1px dashed #d1435b;opacity:.85}.emx-band span{position:absolute;right:62px;top:-18px;font:600 11px/1.4 "Space Grotesk",var(--font),sans-serif;color:#d1435b;background:var(--em-paper);padding:0 6px;border-radius:6px;white-space:nowrap}
.emx-legend{position:absolute;left:12px;bottom:38px;z-index:6}.emx-legend button{font-size:11.5px;padding:5px 9px;min-height:26px;box-shadow:0 1px 3px rgba(20,22,40,.12)}.emx-legend button[disabled]{opacity:.55;cursor:not-allowed}
.emx-tip{position:absolute;z-index:7;max-width:260px;padding:8px 10px;border-radius:10px;background:var(--em-ink);color:#fff;font:12px/1.4 var(--font,"DM Sans"),sans-serif;box-shadow:0 10px 24px -12px #000a;pointer-events:none}.emx-tip b{display:block;font-weight:600}.emx-tip small{display:block;opacity:.85}html[data-theme=dark] .emx-tip{background:#f0f1f5;color:#1c2233}
.emx-facts li{display:flex;justify-content:space-between;gap:12px;border-bottom:1px solid var(--em-line);padding:4px 0}.emx-facts li span{color:var(--em-muted)}
@media (max-width:640px){.emx-body{grid-template-columns:1fr}.emx-flows li{grid-template-columns:1fr 1fr auto}.emx-legend{bottom:44px;left:8px}.emx-table th:nth-child(2),.emx-table td:nth-child(2){display:none}}
`;document.head.append(s);}
