// Economic Machine inside the Pro workspace: the Wallets data tab (holders and money flow around the open market), the
// Wallet side tab (dossier next to Assistant | Market details) and that wallet's buys, sells, collateral and borrows drawn
// on the chart, with a legend, keyboard-reachable markers and tooltips.
import {em,esc,short,usd,num,day,walletIcon,walletCard,glyph,cat,injectStyles,isEmMarket,chainOfMarket,isAddress,normAddress,isChain,isMarket,emitWallet,stateHTML} from './em-api.js';

const ACTION={buy:['Bought','#17856d','B'],sell:['Sold','#c76678','A'],swap:['Swapped','#5658e8',''],supply:['Supplied','#2c9c8c',''],collateral:['Posted collateral','#3b6fd6',''],
 supply_collateral:['Posted collateral','#3b6fd6',''],withdraw:['Withdrew','#d97706',''],withdraw_collateral:['Withdrew collateral','#d97706',''],borrow:['Borrowed','#b7791f',''],repay:['Repaid','#2c9c8c',''],
 lp_add:['Added liquidity','#2fb3c9',''],lp_remove:['Removed liquidity','#b7791f',''],bridge_in:['Bridged in','#8b5cf6',''],bridge_out:['Bridged out','#8b5cf6',''],liquidation:['Liquidated','#c76678','']};
const GLYPH={buy:'route',sell:'route',swap:'route',supply:'bank',collateral:'shield',supply_collateral:'shield',withdraw:'inbox',withdraw_collateral:'inbox',borrow:'bank',repay:'bank',lp_add:'pool',lp_remove:'pool',bridge_in:'bridge',bridge_out:'bridge',liquidation:'bolt'};
const kindOf=it=>it.side==='B'?'buy':it.side==='A'?'sell':ACTION[it.action]?it.action:'swap';
const FLOW={swap:'Swaps',lp_add:'Liquidity added',lp_remove:'Liquidity removed',supply:'Supplied',collateral:'Posted as collateral',supply_collateral:'Posted as collateral',withdraw:'Withdrawn',withdraw_collateral:'Collateral withdrawn',borrow:'Borrowed',repay:'Repaid',bridge_in:'Bridged in',bridge_out:'Bridged out',liquidation:'Liquidated'};

export function attachWorkspace({root,insights,sideTabs,getToken,getChart,openSide}){
 injectStyles();
 const abort=new AbortController(),{signal}=abort;
 const btn=document.createElement('button');btn.type='button';btn.dataset.sideView='wallet';btn.setAttribute('aria-pressed','false');btn.hidden=true;btn.innerHTML=glyph('wallet',14)+' Wallet';sideTabs.append(btn);
 const panel=document.createElement('section');panel.className='em-side-dossier';panel.hidden=true;panel.setAttribute('aria-label','Wallet dossier');panel.tabIndex=-1;insights.append(panel);
 let current=null,request=null,overlay=null;
 function show(on){panel.hidden=!on;btn.setAttribute('aria-pressed',String(on));if(on)insights.dataset.emWallet='open';else delete insights.dataset.emWallet;if(on&&root.dataset.sideOpen!=='true')openSide('market');}
 const head='<header><span>Wallet</span><button type="button" class="em-btn em-btn-quiet" data-em-close aria-label="Close wallet dossier">Close</button></header>';
 async function open(address,chain){
  const a=normAddress(address);if(!a)return;current={address:a,chain:isChain(chain)?chain:null};btn.hidden=false;show(true);
  request?.abort();request=new AbortController();panel.innerHTML=head+stateHTML('loading','Reading '+short(a)+' from the Economic Machine…');
  try{const q=new URLSearchParams({address:a});if(current.chain)q.set('chain',current.chain);const card=await em('tools/wallet?'+q,{signal:request.signal});if(current?.address!==a)return;
   panel.innerHTML=head+walletCard(card);current.chain=card.chain;}
  catch(e){if(e.name!=='AbortError'&&current?.address===a){panel.innerHTML=head+stateHTML('error',e.message,{retry:true});panel.querySelector('[data-em-retry]')?.addEventListener('click',()=>open(a,chain),{once:true});}}
 }
 async function markers(address){
  overlay?.destroy();overlay=null;const token=getToken(),chart=getChart(),a=normAddress(address);
  if(!isEmMarket(token)||!chart||!a)return;
  try{const d=await em('tools/trades?'+new URLSearchParams({address:a,market:token.id}),{signal});if(getToken()?.id!==token.id||current?.address!==a)return;overlay=chartMarkers(chart,d,a);}
  catch(e){if(e.name!=='AbortError'&&chart.container)overlay=chartMarkers(chart,{items:[],error:e.message},a);}
 }
 const pick=(address,chain)=>{open(address,chain||chainOfMarket(getToken()));markers(address);};
 sideTabs.addEventListener('click',e=>{const b=e.target.closest('button[data-side-view]');if(!b)return;if(b===btn){if(current)show(panel.hidden);}else show(false);},{signal});
 panel.addEventListener('click',e=>{if(e.target.closest('[data-em-close]')){show(false);btn.hidden=true;current=null;overlay?.destroy();overlay=null;}},{signal});
 // Picking a market data view (tab or the compact view menu) returns from the dossier to that view
 root.addEventListener('change',e=>{if(e.target.closest('select')&&!panel.hidden&&!panel.contains(e.target))show(false);},{signal});
 root.addEventListener('click',e=>{
  if(e.target.closest('[data-pro=tab]')&&!panel.hidden){show(false);return;}
  const a=e.target.closest('a[data-em-chart]');if(a){const token=getToken();if(a.dataset.emChart&&a.dataset.emChart===token?.id&&isAddress(a.dataset.emAddress)){e.preventDefault();pick(a.dataset.emAddress,a.dataset.emChain);}return;}
  const w=e.target.closest('[data-em-wallet]');if(w&&isAddress(w.dataset.emWallet)){e.preventDefault();pick(w.dataset.emWallet,w.dataset.emChain);}
 },{signal});
 window.addEventListener('emwallet',e=>{const d=e.detail||{},a=normAddress(d.address);if(!a)return;const token=getToken();
  if(isMarket(d.market)&&d.market!==token?.id){location.hash=`/console/workspace/${encodeURIComponent(d.market)}?panel=wallets&wallet=${a}`;return;}
  open(a,d.chain||chainOfMarket(token));if(d.markers!==false)markers(a);},{signal});
 const wallet=normAddress(new URLSearchParams(location.hash.split('?')[1]||'').get('wallet')||'');
 if(wallet){let tries=0;const wait=()=>{if(signal.aborted)return;if(getChart()?.view||tries++>40)pick(wallet);else setTimeout(wait,250);};wait();}
 return {destroy(){abort.abort();request?.abort();overlay?.destroy();btn.remove();panel.remove();}};
}

// One wallet's economic actions on the open market, pinned to the candle they happened in
function chartMarkers(chart,data,address){
 const host=chart.container,layer=document.createElement('div'),chip=document.createElement('div'),legend=document.createElement('div'),tip=document.createElement('div');
 layer.className='em-marker-layer';chip.className='em-chart-chip';legend.className='em-marker-legend';tip.className='em-tip';tip.hidden=true;tip.setAttribute('role','tooltip');tip.id='em-marker-tip';
 const items=(data.items||[]).filter(i=>Number.isFinite(i.time)&&Number.isFinite(i.price)&&i.price>0).sort((a,b)=>a.time-b.time);
 const kinds=[...new Set(items.map(kindOf))];
 chip.setAttribute('role','status');
 chip.innerHTML=`${walletIcon('eoa_user',18)}<span><b class="em-mono">${esc(short(address))}</b> · <span data-em-chip-text>${data.error?esc(data.error):items.length?items.length+' action'+(items.length===1?'':'s'):'No recorded actions on this market'}</span></span><button type="button" class="em-btn em-btn-quiet" data-em-wide hidden style="padding:2px 8px">Show on 1d</button><button type="button" data-em-remove aria-label="Remove wallet markers">×</button>`;
 legend.innerHTML=kinds.map(k=>`<span><i style="--c:${ACTION[k][1]}"></i>${esc(ACTION[k][0])}</span>`).join('')+(data.source==='galaxy'?'<span title="Swap direction is not recorded for this wallet">· direction not recorded</span>':'');
 legend.hidden=!kinds.length;
 layer.innerHTML=items.map((it,i)=>{const k=kindOf(it),[label,color,side]=ACTION[k];const text=`${label} ${num(it.amount)} ${data.symbol||''} · ${usd(it.usd)} · ${day(it.time/1000)}`;
  return `<button type="button" class="em-marker" data-i="${i}" data-side="${side}" style="--em-c:${color}" aria-label="${esc(text)}" aria-describedby="em-marker-tip">${glyph(GLYPH[k]||'dot',12)}</button>`;}).join('');
 host.append(layer,chip,legend,tip);
 let frame=0;
 function paint(){frame=0;const v=chart.view,cv=chart.canvas;if(!v||!cv){layer.hidden=true;return;}layer.hidden=false;
  const w=v.width||cv.clientWidth,h=v.height||cv.clientHeight;layer.style.left=cv.offsetLeft+'px';layer.style.top=cv.offsetTop+'px';layer.style.width=w+'px';layer.style.height=h+'px';
  const els=layer.children;let shown=0;for(let i=0;i<items.length;i++){let p=null;try{p=v.screen({time:items[i].time,price:items[i].price});}catch{}const el=els[i];
   if(!p||!Number.isFinite(p.x)||!Number.isFinite(p.y)||p.x<0||p.x>w||p.y<0||p.y>h){el.hidden=true;continue;}el.hidden=false;shown++;el.style.left=p.x+'px';el.style.top=p.y+'px';}
  if(items.length){const first=items[0].time,last=items.at(-1).time,span=day(first/1000)===day(last/1000)?day(first/1000):day(first/1000)+' – '+day(last/1000);
   chip.querySelector('[data-em-chip-text]').textContent=shown?`${shown} of ${items.length} action${items.length===1?'':'s'} in view · ${span}`:`${items.length} action${items.length===1?'':'s'} outside this view · ${span}`;
   chip.querySelector('[data-em-wide]').hidden=!!shown||chart.options?.interval==='1d';}}
 const schedule=()=>{if(!frame)frame=requestAnimationFrame(paint);};
 const showTip=el=>{const it=items[+el.dataset.i];if(!it)return;const k=kindOf(it),[label]=ACTION[k];tip.innerHTML=`<b>${esc(label)} ${esc(num(it.amount))} ${esc(data.symbol||'')}</b><small>${esc(usd(it.usd))} at ${esc(num(it.price))} USD · ${esc(new Date(it.time).toISOString().slice(0,16).replace('T',' '))} UTC</small>${it.text?`<small>${esc(it.text)}</small>`:''}`;
  tip.hidden=false;const r=el.getBoundingClientRect(),hr=host.getBoundingClientRect();tip.style.left=Math.min(hr.width-270,Math.max(4,r.left-hr.left-20))+'px';tip.style.top=(r.top-hr.top+28)+'px';};
 layer.addEventListener('pointerover',e=>{const el=e.target.closest('.em-marker');if(el)showTip(el);});layer.addEventListener('pointerout',()=>{tip.hidden=true;});
 layer.addEventListener('focusin',e=>{const el=e.target.closest('.em-marker');if(el)showTip(el);});layer.addEventListener('focusout',()=>{tip.hidden=true;});
 layer.addEventListener('click',e=>{const el=e.target.closest('.em-marker');const it=el&&items[+el.dataset.i];if(it?.tx){const ex=data.explorer||(chart?.engineData?.identity?.chain==='arbitrum'?'https://arbiscan.io':'https://robinhoodchain.blockscout.com');window.open(`${ex}/tx/${it.tx}`,'_blank','noopener,noreferrer');}});
 host.addEventListener('chartframe',schedule);const ro=new ResizeObserver(schedule);ro.observe(host);schedule();
 chip.querySelector('[data-em-remove]').addEventListener('click',()=>destroy());
 chip.querySelector('[data-em-wide]').addEventListener('click',()=>{const panel=host.closest('.pro-chart-panel')||document;const b=[...panel.querySelectorAll('button')].find(x=>x.textContent.trim()==='1d');b?.click();});
 function destroy(){cancelAnimationFrame(frame);host.removeEventListener('chartframe',schedule);ro.disconnect();layer.remove();chip.remove();legend.remove();tip.remove();}
 return {destroy};
}

// Wallets data tab: who holds (or moves) the open market and where its money goes
export function mountWalletsTab(view,{token}){
 injectStyles();const abort=new AbortController();
 if(!isEmMarket(token)){
  view.innerHTML=`<div class="em-empty"><b>Wallet intelligence covers Arbitrum One and Robinhood Chain markets.</b><br>Open <a href="#/console/workspace/robinhood%3A0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec?panel=wallets">NVDA on Robinhood Chain</a>, <a href="#/console/workspace/robinhood%3A0x322f0929c4625ed5bad873c95208d54e1c003b2d?panel=wallets">TSLA</a>, <a href="#/console/workspace/arbitrum%3A0x912ce59144191c1204e64559fe8253a0e49e6548?panel=wallets">ARB</a> or <a href="#/console/workspace/arbitrum%3A0x82af49447d8a07e3bd95bd0d56f35241523fbab1?panel=wallets">WETH on Arbitrum</a>, or explore the <a href="#/?view=map&amp;chain=robinhood">wallet map</a>.</div>`;
  return {token:token?.id,destroy(){abort.abort();}};
 }
 const chain=chainOfMarket(token),narrow=view.clientWidth&&view.clientWidth<640;
 view.innerHTML=`<div class="em-wallets${narrow?' em-narrow':''}"><div><h3>Money around ${esc(token.symbol)}</h3><div class="em-constellation" aria-label="${esc(token.symbol)} holders map">${stateHTML('loading','Reading holders…')}</div><div class="em-flowbars" aria-label="Economic actions in ${esc(token.symbol)}"></div></div><div><h3 data-em-list-title>Top holders</h3><div class="em-holders-wrap">${stateHTML('loading','Reading holders…')}</div></div></div>`;
 const $=s=>view.querySelector(s);
 const load=()=>em('tools/holders?'+new URLSearchParams({market:token.id}),{signal:abort.signal}).then(d=>{
  if(abort.signal.aborted)return;
  const holders=(d.holders||[]).filter(h=>isAddress(h.address)),movers=(d.movers||[]).filter(h=>isAddress(h.address)),useMovers=!holders.length&&movers.length;
  const rows=useMovers?movers:holders;$('[data-em-list-title]').textContent=useMovers?'Wallets moving the most '+token.symbol:'Top holders';
  const max=Math.max(...rows.map(h=>useMovers?h.usd||0:h.share||0),1);
  $('.em-holders-wrap').innerHTML=rows.length?`<table class="em-holders"><caption class="sr-only">${useMovers?'Wallets with the largest decoded flow':'Largest holders by share of supply'}</caption><thead><tr><th scope="col">Wallet</th><th scope="col">Kind</th><th scope="col" style="text-align:right">${useMovers?'Decoded flow':'Share of supply'}</th></tr></thead><tbody>${rows.map(h=>`<tr data-address="${h.address}" tabindex="0" aria-label="${esc(short(h.address)+', '+(h.categoryName||cat(h.category)[0]))}"><td><span class="em-who">${walletIcon(h.category,28)}<div><b class="em-mono">${esc(short(h.address))}</b><small>${esc(h.entity||'')}</small></div></span></td><td><small>${esc(h.categoryName||cat(h.category)[0])}</small></td><td><span class="em-share"><b class="em-mono">${useMovers?usd(h.usd):Number.isFinite(h.share)?h.share.toFixed(2)+'%':'—'}</b><i style="width:${Math.max(4,(useMovers?h.usd||0:h.share||0)/max*90)}px"></i></span></td></tr>`).join('')}</tbody></table><p class="em-muted" style="margin-top:8px;font-size:11.5px">${d.holderCount?num(d.holderCount)+' holders · ':''}${useMovers?'decoded economic actions':'balances'} as of ${day(d.asOf)} · Economic Machine</p>`
   :stateHTML('empty',d.ready===false?'Market summaries are still being prepared. Try again in a minute.':'No holder or flow data for this market yet.');
  $('.em-constellation').innerHTML=rows.length?constellation(token,chain,rows,useMovers):'';
  const flows=(d.flows||[]).filter(f=>Number.isFinite(f.usd)&&f.usd>0),top=Math.max(...flows.map(f=>f.usd),1);
  $('.em-flowbars').innerHTML=flows.map(f=>`<div><span>${esc(FLOW[f.action]||String(f.action).replace(/_/g,' '))}</span><span class="em-track" role="img" aria-label="${esc(usd(f.usd))}"><i style="width:${(f.usd/top*100).toFixed(1)}%;background:${(ACTION[f.action]||ACTION.swap)[1]}"></i></span><b>${usd(f.usd)}</b></div>`).join('');
 }).catch(e=>{if(!abort.signal.aborted){$('.em-holders-wrap').innerHTML=stateHTML('error',e.message,{retry:true});$('.em-constellation').innerHTML='';}});
 load();
 const pick=address=>{view.querySelectorAll('tr[data-address]').forEach(r=>r.setAttribute('aria-selected',String(r.dataset.address===address)));emitWallet({address,chain,market:token.id});};
 view.addEventListener('click',e=>{if(e.target.closest('[data-em-retry]')){$('.em-holders-wrap').innerHTML=stateHTML('loading','Reading holders…');load();return;}const t=e.target.closest('[data-address]');if(t&&isAddress(t.dataset.address))pick(t.dataset.address);},{signal:abort.signal});
 view.addEventListener('keydown',e=>{const t=e.target.closest('[data-address]');if(t&&(e.key==='Enter'||e.key===' ')){e.preventDefault();pick(t.dataset.address);}},{signal:abort.signal});
 return {token:token.id,destroy(){abort.abort();}};
}

function constellation(token,chain,rows,movers){
 const W=520,H=270,cx=W/2,cy=H/2,n=Math.min(rows.length,12),val=h=>movers?h.usd||0:h.share||0,max=Math.max(...rows.map(val),1);
 const nodes=rows.slice(0,n).map((h,i)=>{const a=-Math.PI/2+i/n*Math.PI*2,r=82+(i%2)*26;return {...h,x:cx+Math.cos(a)*r*1.55,y:cy+Math.sin(a)*r,s:9+Math.sqrt(val(h)/max)*13};});
 const icon=/^\/(assets\/em\/tokens\/(arbitrum|robinhood)\/0x[0-9a-f]{40}-[0-9a-f]{12}\.(webp|svg)|api\/public\/em\/icon\/(arbitrum|robinhood)\/0x[0-9a-f]{40})$/.test(token.icon||'')?token.icon:'';
 return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(token.symbol)} ${movers?'largest movers':'largest holders'}">
 ${nodes.map(p=>`<line x1="${cx}" y1="${cy}" x2="${p.x.toFixed(1)}" y2="${p.y.toFixed(1)}" stroke="${cat(p.category)[2]}" stroke-opacity=".35" stroke-width="${(1+p.s/9).toFixed(1)}"/>`).join('')}
 <circle cx="${cx}" cy="${cy}" r="30" fill="var(--em-paper)" stroke="var(--em-accent)" stroke-opacity=".35" stroke-width="6"/>
 ${icon?`<clipPath id="em-token-clip"><circle cx="${cx}" cy="${cy}" r="22"/></clipPath><image href="${icon}" x="${cx-22}" y="${cy-22}" width="44" height="44" clip-path="url(#em-token-clip)"/>`:''}
 ${nodes.map(p=>`<g class="em-node" data-address="${p.address}" tabindex="0" role="button" aria-label="${esc(short(p.address)+' '+(movers?usd(p.usd):Number.isFinite(p.share)?p.share.toFixed(1)+'%':''))}"><circle class="em-ring" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${p.s.toFixed(1)}" fill="var(--em-paper)" stroke="${cat(p.category)[2]}" stroke-width="1.5"/><circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${(p.s*.45).toFixed(1)}" fill="${cat(p.category)[2]}" fill-opacity=".8"/><text x="${p.x.toFixed(1)}" y="${(p.y+p.s+12).toFixed(1)}" text-anchor="middle" font-size="10" fill="var(--em-muted)" font-family="Space Grotesk,DM Sans,sans-serif">${esc(short(p.address))} · ${movers?esc(usd(p.usd)):Number.isFinite(p.share)?p.share.toFixed(1)+'%':''}</text></g>`).join('')}
 </svg>`;
}
