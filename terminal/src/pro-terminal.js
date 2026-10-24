import {tokenImage} from './token-icons.js';
import {reviewedMarket,verificationBadge} from './market-verification.js';
import {TickChart} from './chart.js';
import {mountChartControls} from './chart-controls.js';
import {bindDataFeed} from '../sdk/data-feed.js';
import {subscribeStream} from '../sdk/stream.js';
import {MARKET_INTERVALS,parseInterval} from './intervals.js';
import {enhanceSelect} from './select.js';
import {categoryChips,compareMarkets,normalizeSubtags,resolveMarketCategory,workspaceHash} from './market-categories.js';

const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const finite=x=>typeof x==='number'&&Number.isFinite(x);
const short=s=>s?`${s.slice(0,5)}…${s.slice(-5)}`:'—';
const num=(v,compact=false)=>!finite(v)?'—':v!==0&&Math.abs(v)<1e-8?v.toExponential(5):v.toLocaleString('en-US',{notation:compact&&Math.abs(v)>=1?'compact':'standard',...(compact&&Math.abs(v)<1?{maximumSignificantDigits:4}:{maximumFractionDigits:compact?2:Math.abs(v)>=1?5:10})});
const usd=(v,compact=false)=>finite(v)?'$'+num(v,compact):'—';
const pct=v=>finite(v)?`${v>0?'+':''}${v.toFixed(2)}%`:'—';
const tone=v=>finite(v)?v>=0?'up':'down':'';
const date=t=>finite(t)&&t>0?new Date(t*1000).toISOString().replace('T',' ').slice(0,19):'—';
const age=t=>!finite(t)?'Awaiting observations':`${Math.max(0,Math.floor(Date.now()/1000-t))<60?Math.max(0,Math.floor(Date.now()/1000-t))+'s':Math.floor(Math.max(0,Date.now()/1000-t)/60)+'m'} ago`;
const icon=name=>`<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${{search:'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',star:'m12 3 2.8 5.8 6.4.9-4.6 4.5 1.1 6.3-5.7-3-5.7 3 1.1-6.3-4.6-4.5 6.4-.9z',pause:'M8 4v16M16 4v16',play:'m6 3 14 9-14 9z',export:'M12 3v12m-5-5 5 5 5-5M4 17v4h16v-4',close:'m6 6 12 12M6 18 18 6',grid:'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z',copy:'M9 9h12v12H9zM15 5V2H2v13h3'}[name]}"/></svg>`;
const assetIcon=t=>tokenImage(t,'pro-initial',28);
const external=(url,label)=>{try{const u=new URL(url);if(u.protocol!=='https:'||u.username||u.password)throw Error();return `<a href="${esc(u.href)}" target="_blank" rel="noopener noreferrer">${esc(label)} ↗</a>`;}catch{return `<span>${esc(label)}</span>`;}};
const explorer=(type,address,label)=>/^[1-9A-HJ-NP-Za-km-z]{32,90}$/.test(address||'')?external('https://explorer.solana.com/'+type+'/'+address,label):esc(label);
const MAX_MARKETS=4096,ASSET_PAGE_SIZE=100,CATALOG_BYTES=4*1024*1024;
const tabs=[['executions','Executions'],['book','Order book'],['pools','Pools'],['candles','Candles'],['research','Research'],['status','Feed status']];
function saved(){try{const d=JSON.parse(localStorage.getItem('tick-watchlist')||'["SOL","JUP","BONK"]');return Array.isArray(d)?d:[];}catch{return [];}}
export function terminalPage(){return `<section class="pro-terminal" aria-label="ticking Pro market terminal">
 <header class="pro-market-bar"><button class="pro-icon-button pro-browse" data-pro="browse" aria-label="Browse assets" aria-expanded="false">${icon('grid')}</button><div class="pro-selected"><span class="pro-skeleton"></span><div>Opening market…<small>Solana</small></div></div><div class="pro-market-metrics"></div><div class="pro-mode"><a class="pro-standard" href="#/token/SOL">Standard</a><span aria-current="page">Pro</span></div></header>
 <div class="pro-workspace"><aside class="pro-assets" aria-label="Market browser"><header><h2>Markets <small class="pro-catalog-count">—</small></h2><button class="pro-icon-button pro-close-assets" data-pro="browse" aria-label="Close market browser">${icon('close')}</button></header><label class="pro-search">${icon('search')}<input type="search" placeholder="Token or mint" aria-label="Search terminal markets"></label><div class="pro-market-filters"><button data-pro="assets" data-filter="all" aria-pressed="true">All assets</button><button data-pro="assets" data-filter="watch" aria-pressed="false">Watchlist</button></div><div class="pro-category-filters" role="group" aria-label="Asset category" hidden></div><div class="pro-assets-label"><span>Asset</span><span>Price / 24h</span></div><div class="pro-asset-list"><p class="pro-empty">Loading reviewed assets…</p></div><footer><div class="pro-asset-pagination"><button data-pro="asset-page" data-direction="-1" aria-label="Previous asset page" disabled>‹</button><span class="pro-page-number">—</span><button data-pro="asset-page" data-direction="1" aria-label="Next asset page" disabled>›</button></div><span class="pro-asset-scope">Reviewed crypto markets</span><br>Attributed overview prices</footer></aside>
 <div class="pro-center"><section class="pro-chart-panel chart-card" aria-label="Market chart"><header class="pro-chart-heading"><div><strong class="pro-chart-pair">Recorded market</strong><span class="pro-chart-close">—</span></div><label class="pro-scope"><span>Chart source</span><select id="pro-chart-source" aria-label="Chart source"><option value="">All retained pools</option></select></label></header><div class="chart-toolbar"></div><div class="pro-chart-frame"><div class="drawing-tools"></div><div class="pro-price-chart" aria-label="Recorded price and volume chart"></div></div><div class="pro-chart-source" role="status">Loading recorded history…</div></section>
 <section class="pro-data-panel" aria-label="Market data"><header><div class="pro-data-tabs" role="tablist" aria-label="Market data views">${tabs.map(([id,label],i)=>`<button id="pro-tab-${id}" role="tab" aria-selected="${i===0}" aria-controls="pro-data-view" tabindex="${i===0?0:-1}" data-pro="tab" data-tab="${id}">${label}</button>`).join('')}</div><div class="pro-data-actions"><label class="pro-row-limit"><span>Rows</span><select aria-label="Execution row count"><option value="50">50</option><option value="100">100</option></select></label><button data-pro="export" class="pro-export">${icon('export')}<span>Export CSV</span></button></div></header><div class="pro-table-caption">Retained physical pool executions · canonical order</div><div class="pro-data-view" id="pro-data-view" role="tabpanel" aria-labelledby="pro-tab-executions" tabindex="0"><p class="pro-empty">Fetching retained executions…</p></div></section></div>
 <aside class="pro-insights"><section class="pro-profile"><header><h2>Execution profile</h2><span class="pro-profile-unit">—</span></header><div class="pro-profile-columns"><span>Price</span><span>Base flow</span><span>Legs</span></div><div class="pro-price-bins"><p class="pro-empty">Loading execution sample…</p></div><p class="pro-profile-note">Executed quantities at sampled prices. Not resting bids or asks.</p></section><section class="pro-inspector"><header><h2>Market details</h2><button class="pro-icon-button" data-pro="watch" aria-label="Add market to watchlist" aria-pressed="false">${icon('star')}</button></header><div class="pro-inspector-body"><p class="pro-empty">Loading verified identity…</p></div></section><section class="pro-coverage"><h2>Observation coverage</h2><div class="pro-coverage-body">Waiting for chart data.</div><a href="#/console">Use ticking in your product ↗</a></section></aside></div>
 <footer class="pro-status-bar"><div><span class="pro-status-dot"></span><span class="pro-refresh-status" role="status">Reading retained observations</span><button class="pro-pause" data-pro="pause">${icon('pause')}<span>Pause updates</span></button><button data-pro="retry" class="pro-retry" hidden>Retry</button></div><span>Read-only <i>·</i> Partial coverage <i>·</i> UTC</span></footer></section>`;}

/** Public GETs only. All refresh work and drawing listeners belong to this route. */
export function mountTerminal(root,{ident='SOL',pool:requestedPool=null}={}){
 const abort=new AbortController(),on={signal:abort.signal},requests=new Set();
 let tokens=[],token=null,chart=null,controls=null,feedDispose=null,selectDispose=null,tape=null,tapeError='',chartState=null,chartError='',detailError='',pool=null,tab='executions',filter='all',assetPage=0,query='',rowLimit=50,paused=false,disposed=false,tapeTimer=0,detailTimer=0,clockTimer=0,run=0,tapeBusy=false,detailBusy=false,retryUntil=0,detailRetryUntil=0,tabKey='',profileKey='';
 const el=s=>root.querySelector(s),active=()=>!disposed&&!abort.signal.aborted;
 let catalogStream=null,tapeStream=null,bookStream=null,book=null,connection='connecting';
 const isHyperliquid=()=>token?.source==='hyperliquid_public';
 const selection=()=>token?.id+':'+(pool||'reference');
 const quoteName=mint=>mint==='EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'?'USDC':mint==='Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB'?'USDT':mint==='So11111111111111111111111111111111111111112'?'SOL':tokens.find(t=>t.mint===mint)?.symbol||short(mint);
 function cancel(){for(const c of requests)c.abort();requests.clear();}
 async function read(path,maxBytes=1048576){
  const c=new AbortController();requests.add(c);const timeout=setTimeout(()=>c.abort(),12000);
  try{const r=await fetch('/api/public/'+path,{signal:c.signal,credentials:'omit',cache:'no-store'});if(!r.ok){const value=r.headers.get('Retry-After'),n=value&&/^\d+$/.test(value)?Number(value)*1000:value?Date.parse(value)-Date.now():r.status===429?60000:0;const e=Error(`Public data read failed (${r.status}).`);e.retryAfterMs=Math.min(2147483647,Math.max(0,Number.isFinite(n)?n:0));throw e;}const text=await r.text();if(text.length>maxBytes)throw Error('Response exceeds the terminal memory bound.');return JSON.parse(text);}finally{clearTimeout(timeout);requests.delete(c);}
 }
 async function catalogRead(){
  return parseCatalog(await read('markets?view=table',CATALOG_BYTES));
 }
 function parseCatalog(d){
  if(!Array.isArray(d.tokens)||!d.tokens.length||d.tokens.length>MAX_MARKETS)throw Error('The market catalog exceeds the supported bound.');
  const reviewed=d.tokens.filter(reviewedMarket).sort(compareMarkets);if(!reviewed.length)throw Error("No reviewed markets are available.");const seen=new Set();return reviewed.map(t=>{const identity=t.network==='exchange'?t.mint===null&&(t.source==='hyperliquid_public'?t.id==='HL-'+t.market&&/^(?:[a-z][a-z0-9]{0,15}:)?[A-Za-z0-9._-]{1,40}$/.test(t.market):/^[A-Z0-9]{2,30}$/.test(t.market)&&['binance_spot','binance_usdm'].includes(t.source)):(/^[1-9A-HJ-NP-Za-km-z]{32,44}$/).test(t.mint);if(typeof t.id!=='string'||typeof t.symbol!=='string'||typeof t.name!=='string'||!identity||seen.has(t.id))throw Error('Invalid market catalog identity.');seen.add(t.id);return Object.fromEntries(['id','symbol','name','mint','icon','category','kind','subtags','price','change','volume','cap','updated_at','stale','verification','ranking_eligible','network','market','marketType','source','venue','quote','volumeScope','venueBaseVolume24h','volumeUpdatedAt','volumeStale','capSource','capUpdatedAt','capStale','priceBasis','sources'].map(k=>[k,t[k]]));});
 }
 function renderCategoryChips(){
  const box=el('.pro-category-filters');if(!box)return;const chips=categoryChips(tokens,{enumOnly:true});const sig=chips.map(c=>c.key+':'+c.count).join('|');
  if(box.dataset.sig!==sig){box.dataset.sig=sig;box.hidden=!chips.length;box.innerHTML=chips.map(c=>`<button data-pro="assets" data-filter="${esc(c.key)}" aria-pressed="false">${esc(c.label)} <small>${c.count}</small></button>`).join('');}
  box.querySelectorAll('[data-pro="assets"]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.filter===filter)));
 }
 function renderAssets(){
  renderCategoryChips();
  const list=el('.pro-asset-list'),scroll=list.scrollTop,watched=saved(),visible=tokens.filter(t=>{if(filter==='watch'&&!watched.includes(t.id))return false;if(filter!=='all'&&filter!=='watch'&&resolveMarketCategory(t).key!==filter)return false;return `${t.symbol} ${t.name} ${t.mint} ${Array.isArray(t.subtags)?t.subtags.join(' '):''}`.toLowerCase().includes(query.toLowerCase());});
  const pages=Math.max(1,Math.ceil(visible.length/ASSET_PAGE_SIZE));assetPage=Math.max(0,Math.min(assetPage,pages-1));const displayed=visible.slice(assetPage*ASSET_PAGE_SIZE,(assetPage+1)*ASSET_PAGE_SIZE);
  list.innerHTML=displayed.map(t=>`<a href="${esc(workspaceHash(t))}" class="pro-asset-row ${t.id===token?.id?'selected':''}" ${t.id===token?.id?'aria-current="page"':''}>${assetIcon(t)}<span class="pro-asset-name" title="${esc(t.name)}"><b><span class="pro-symbol">${esc(t.symbol)}</span>${verificationBadge(t)}</b><small>${esc(t.name)} · ${esc(resolveMarketCategory(t).label)}</small></span><span class="pro-asset-quote" title="${esc(t.network==='exchange'?num(t.price)+' '+t.quote:usd(t.price))}">${t.network==='exchange'?num(t.price):usd(t.price)}<small class="${tone(t.change)}">${pct(t.change)}${t.stale?' · Older':''}</small></span></a>`).join('')||'<p class="pro-empty">No matching assets.</p>';list.scrollTop=scroll;el('.pro-catalog-count').textContent=tokens.length;el('.pro-page-number').textContent=(assetPage+1)+' / '+pages;el('.pro-asset-scope').textContent=visible.length+' of '+tokens.length+' reviewed assets';root.querySelectorAll('[data-pro=asset-page]').forEach(b=>b.disabled=Number(b.dataset.direction)<0?assetPage===0:assetPage===pages-1);
 }
 function renderMarket(){
  el('.pro-selected').innerHTML=`${assetIcon(token)}<div><strong>${esc(token.symbol)}${verificationBadge(token)}</strong><small>${esc(token.name)} / ${esc(token.network==='exchange'?token.venue:'Solana')} · ${esc(resolveMarketCategory(token).label)}</small></div>`;
  el('.pro-standard').href='#/token/'+encodeURIComponent(token.id);el('.pro-market-metrics').innerHTML=`${[['Displayed price',chartState?.candles?.length?num(chartState.candles.at(-1).close)+' '+(chartState.candles.at(-1).quoteCurrency||chartState.quote):(token.network==='exchange'?num(token.price)+' '+token.quote:usd(token.price))],['24h change',`<span class="${tone(token.change)}">${pct(token.change)}</span>`],[isHyperliquid()?'Venue base volume (24h)':'Solana volume (24h)',isHyperliquid()?num(token.venueBaseVolume24h,true):usd(token.volume,true)],['Market cap',usd(token.cap,true)]].map(([k,v])=>`<div><small>${k}</small><strong>${v}</strong></div>`).join('')}`;
  const w=el('[data-pro="watch"]'),watched=saved().includes(token.id);w.setAttribute('aria-pressed',String(watched));w.setAttribute('aria-label',`${watched?'Remove':'Add'} ${token.symbol} ${watched?'from':'to'} watchlist`);
  el('.pro-inspector-body').innerHTML=`<dl><div><dt>Network</dt><dd>Solana</dd></div><div><dt>Mint</dt><dd><button data-pro="copy-mint" title="${esc(token.mint)}">${esc(short(token.mint))} ${icon('copy')}</button></dd></div><div><dt>Category</dt><dd>${esc(resolveMarketCategory(token).label)}</dd></div>${normalizeSubtags(token).length?`<div><dt>Subtags</dt><dd>${esc(normalizeSubtags(token).join(' · '))}</dd></div>`:''}<div><dt>Overview fetched</dt><dd>${date(token.updated_at).slice(11)} UTC</dd></div><div><dt>Overview state</dt><dd>${token.stale?'Older observation':'Current observation'}</dd></div></dl><div class="pro-official-links">${explorer('address',token.mint,'Mint')}${token.website?external(token.website,'Website'):''}${token.x?external(token.x,'X'):''}</div><p>USD overview is provider-attributed. Chart and execution prices use their displayed quote units.</p><span class="pro-copy-status" role="status"></span>`;
  if(token.network==='exchange')el('.pro-inspector-body').innerHTML='<dl><div><dt>Venue</dt><dd>'+esc(token.venue)+'</dd></div><div><dt>Instrument</dt><dd>'+esc(token.market)+'</dd></div><div><dt>Type</dt><dd>'+esc(token.marketType)+'</dd></div><div><dt>Category</dt><dd>'+esc(resolveMarketCategory(token).label)+'</dd></div>'+(normalizeSubtags(token).length?'<div><dt>Subtags</dt><dd>'+esc(normalizeSubtags(token).join(' · '))+'</dd></div>':'')+'</dl><p>'+esc(isHyperliquid()?'Hyperliquid perpetual. Trading this instrument does not represent ownership of its underlying asset.':'Multi-source asset price reference. Global market cap and observed Solana pool volume are separate metrics.')+'</p><span class="pro-copy-status" role="status"></span>';
  renderAssets();if(tab==='research'||tab==='pools'||tab==='status')renderTab(true);
 }
 function configureSource(){
  selectDispose?.();el('.pro-scope .tick-select')?.remove();const select=el('.pro-scope select');select.innerHTML=(token.network==='exchange'?'<option value="">'+esc(token.venue)+' · '+esc(token.market)+'</option>':'<option value="">All retained pools · reference</option>')+(token.pools?.items||[]).map(p=>`<option value="${esc(p.address)}">${esc(p.venue)} · ${esc(p.base)} / ${esc(p.quote)} · ${esc(short(p.address))}</option>`).join('');select.value=pool||'';selectDispose=enhanceSelect(select,option=>'<span>'+esc(option.textContent)+'</span>');el('.pro-scope .select-trigger').setAttribute('aria-label','Chart source');
 }
 function status(){
  const last=chartState?.updated_at,tradeTime=token?.network==='exchange'?null:tape?.trades?.[0]?.time,error=tapeError||chartError||detailError;el('.pro-refresh-status').textContent=paused?'Updates paused':error?'Read interrupted · last good data retained':`${connection==='streaming'?'Streaming updates':connection==='retrying'?'Reconnecting · retained data':'Connecting'} · Chart ${age(last)}${tradeTime?' · Latest execution '+age(tradeTime):''}`;el('.pro-status-dot').classList.toggle('paused',paused||!!error);el('.pro-retry').hidden=!error;el('.pro-pause span').textContent=paused?'Resume updates':'Pause updates';el('.pro-pause').setAttribute('aria-pressed',String(paused));el('.pro-pause svg').outerHTML=icon(paused?'play':'pause');
 }
 function sourceStatus(){
  const d=chartState;if(!d){el('.pro-chart-pair').textContent=token.symbol+' / '+(pool?quoteName(token.pools?.items?.find(p=>p.address===pool)?.quoteMint):'reference');el('.pro-chart-close').textContent='—';return;}
  const b=d.candles?.at(-1),unit=b?.quoteCurrency||d.quote||quoteName(d.quoteMint),label=isHyperliquid()?'Venue executions':d.identity.network==='exchange'?'Asset reference':d.aggregation==='token'?'Token reference':'Pool executions';el('.pro-chart-pair').textContent=`${token.symbol} / ${unit} · ${label}`;el('.pro-chart-close').textContent=b?num(b.close):'—';
  if(b){const price=el('.pro-market-metrics>div:first-child strong');if(price)price.textContent=num(b.close)+' '+unit;}

  el('.pro-chart-source').textContent=chartError?`${d.interval} frame retained · ${chartError}`:`${d.interval} · ${d.stale?'Older records':'Updated '+date(d.updated_at)+' UTC'}${d.identity.network==='exchange'?' · Ticking · '+(d.sources||[]).join(' + '):d.history?' · Archive '+d.history.archiveQuote+' / Solana '+d.history.currentQuote+'; no historical quote conversion':''}`;
  el('.pro-coverage-body').innerHTML=`<dl><div><dt>Chart unit</dt><dd>${esc(unit)} / ${esc(token.symbol)}</dd></div><div><dt>Loaded candles</dt><dd>${num(d.candles?.length)}</dd></div><div><dt>Carried closes</dt><dd>${num(d.carriedBars||0)}</dd></div>${d.aggregation==='token'?`<div><dt>Priced / observed pools</dt><dd>${num(d.coverage?.pricedPoolCount)} / ${num(d.coverage?.observedPoolCount)}</dd></div>`:`<div><dt>${d.identity.network==='exchange'?'Market':'Pool'}</dt><dd>${esc(d.identity.network==='exchange'?d.identity.market:short(d.pool))}</dd></div>`}<div><dt>Complete coverage</dt><dd>${d.complete===true?'Reported by source':'No'}</dd></div></dl><p>Carried closes have zero recorded flow; they do not prove an empty market.</p>`;
 }
 function startFeed(){
  feedDispose?.();feedDispose=null;if(!chart||paused||document.hidden)return;
  const key=selection();feedDispose=bindDataFeed(chart,{...(token.network==='exchange'?{market:token.id,aggregation:'exchange'}:{mint:token.mint,pool:pool||undefined,aggregation:pool?'pool':'token'}),apiBase:location.origin,onConnection:s=>{connection=s.state;if(active())status();},onStatus:d=>{
   if(!active()||selection()!==key)return;
   if(d.loading){el('.pro-chart-source').textContent=`Loading ${chart.options.interval}${chartState?' · previous '+chartState.interval+' frame remains displayed':''}`;return;}
   if(d.error){chartError=d.error;el('.pro-chart-source').textContent=`${d.retained?'Previous '+d.displayedInterval+' frame retained · ':''}${d.error}`;status();return;}
   chartError='';chartState=d;sourceStatus();renderProfile();if(tab==='candles'||tab==='status')renderTab(true);status();
  }});
 }
 function selectPool(address,updateUrl=true){
  if(address&&!(token.pools?.items||[]).some(p=>p.address===address))return;
  run++;cancel();clearTimeout(tapeTimer);tapeBusy=false;tapeStream?.();tapeStream=null;feedDispose?.();controls?.();feedDispose=null;pool=address||null;tape=null;tapeError='';chartState=null;chartError='';profileKey='';tabKey='';
  chart.engineData=null;chart.set({series:[],emptyMessage:paused?'Updates paused; resume to load this selection.':'Loading selected history…',interval:pool?chart.options.interval:MARKET_INTERVALS.includes(chart.options.interval)?chart.options.interval:'1m'});chart.offset=0;chart.manualScale=null;chart.drawingLayer?.setMarket('pending:'+selection());
  controls=mountChartControls(chart,el('.pro-chart-panel'),{intervals:pool?null:MARKET_INTERVALS,quickIntervals:pool?['1s','3s','5s','15s','30s','45s','1m','3m']:['1m','3m','5m','15m','1h','1d'],customIntervals:!!pool});configureSource();
  el('.pro-chart-pair').textContent=token.symbol+' / '+(pool?token.pools.items.find(p=>p.address===pool)?.quote||'native quote':'reference');el('.pro-chart-close').textContent='—';el('.pro-chart-source').textContent=paused?'Updates paused. Resume to load this source.':'Loading selected history…';el('.pro-coverage-body').textContent='Waiting for selected chart data.';
  if(updateUrl)history.replaceState(null,'',location.pathname+location.search+'#/terminal/'+encodeURIComponent(token.id)+(pool?'?pool='+encodeURIComponent(pool):''));renderTab(true);renderProfile();startFeed();startTape();pollTape();
 }
 function receipt(t){if(/^[1-9A-HJ-NP-Za-km-z]{64,90}$/.test(t.signature||''))return t.signature;return String(t.identity||'').split(':').find(p=>/^[1-9A-HJ-NP-Za-km-z]{64,90}$/.test(p))||'';}
 function acceptTape(d){
   if((token.network==='exchange'?d.identity?.network!=='exchange'||d.identity?.market!==token.market||d.identity?.venue!==token.source:d.identity?.network!=='solana'||d.identity?.baseMint!==token.mint)||!Array.isArray(d.trades)||d.trades.length>100||pool&&d.identity?.pool!==pool)throw Error('Execution identity does not match this market.');
   const ids=new Set(),fields=isHyperliquid()?['time','tradeId','price','base_volume','quote_volume']:['time','slot','tx_order','event_order','price','base_volume','quote_volume'];d.trades=d.trades.filter(t=>{if(typeof t.identity!=='string'||!fields.every(k=>finite(t[k]))||t.price<=0||t.base_volume<0||t.quote_volume<0||pool&&t.pool!==pool||ids.has(t.identity))return false;ids.add(t.identity);return true;}).sort((a,b)=>isHyperliquid()?b.time-a.time||b.tradeId-a.tradeId:b.slot-a.slot||b.tx_order-a.tx_order||b.event_order-a.event_order);
   tape=d;tapeError='';renderProfile();if(tab==='executions'||tab==='status')renderTab();status();
 }
 function startTape(){
  tapeStream?.();tapeStream=null;
  bookStream?.();bookStream=null;book=null;
  if(!token||token.network==='exchange'&&!isHyperliquid()||paused||document.hidden)return;
  const key=selection(),q=new URLSearchParams(isHyperliquid()?{market:token.id,aggregation:'exchange'}:{mint:token.mint,aggregation:pool?'pool':'token'});if(pool)q.set('pool',pool);
  tapeStream=subscribeStream('/api/public/stream/trades?'+q,{onMessage:message=>{if(active()&&!paused&&selection()===key&&message.type==='snapshot')acceptTape(message.data);}});
  if(isHyperliquid())bookStream=subscribeStream('/api/public/stream/book?'+new URLSearchParams({market:token.id}),{onMessage:message=>{const d=message.data;if(active()&&!paused&&selection()===key&&message.type==='snapshot'&&d?.identity?.market===token.market&&d.identity.venue===token.source){book=d;if(tab==='book')renderTab(true);}}});
 }
 function startCatalog(){
  catalogStream?.();catalogStream=null;if(paused||document.hidden)return;
  catalogStream=subscribeStream('/api/public/stream/markets',{onMessage:message=>{
   if(!active()||paused||message.type!=='snapshot')return;
   tokens=parseCatalog(message.data);const latest=tokens.find(t=>t.id===token?.id);
   if(latest){token={...token,...latest};renderMarket();}else renderAssets();
  }});
 }
 async function pollTape(){
  clearTimeout(tapeTimer);if(!active()||paused||document.hidden||!token||tapeBusy)return;
  if(Date.now()<retryUntil){tapeTimer=setTimeout(pollTape,retryUntil-Date.now());return;}
  tapeBusy=true;const epoch=run,key=selection();
  try{const q=new URLSearchParams(token.network==='exchange'?{market:token.id,aggregation:'exchange',limit:'100'}:{mint:token.mint,aggregation:pool?'pool':'token',limit:'100'});if(pool)q.set('pool',pool);const d=await read('engine/trades?'+q);
   if(!active()||epoch!==run||key!==selection())return;
   acceptTape(d);
  }catch(e){if(active()&&epoch===run&&e.name!=='AbortError'){tapeError=e.message;retryUntil=Date.now()+Math.max(15000,e.retryAfterMs||0);renderProfile();if(tab==='executions'||tab==='status')renderTab(true);status();}}
  finally{if(epoch===run){tapeBusy=false;if(active()&&!paused&&!document.hidden)tapeTimer=setTimeout(pollTape,Math.max(tapeStream?30000:3000,retryUntil-Date.now()));}}
 }
 async function pollDetail(refreshCatalog=true){
  clearTimeout(detailTimer);if(!active()||paused||document.hidden||!token||detailBusy)return;
  if(Date.now()<detailRetryUntil){detailTimer=setTimeout(pollDetail,detailRetryUntil-Date.now());return;}
  detailBusy=true;
  try{const [d,catalog]=await Promise.all([read('markets/'+encodeURIComponent(token.id)),refreshCatalog?catalogRead():Promise.resolve(tokens)]);if(!active()||d.id!==token.id||d.mint!==token.mint)return;tokens=catalog;token={...d,...tokens.find(t=>t.id===d.id)};detailError='';renderMarket();configureSource();
   if(requestedPool){const address=requestedPool;requestedPool=null;selectPool((token.pools?.items||[]).some(p=>p.address===address)?address:null,false);}
  }
  catch(e){if(active()&&e.name!=='AbortError'){detailError=e.message;detailRetryUntil=Date.now()+Math.max(30000,e.retryAfterMs||0);status();}}
  finally{detailBusy=false;if(active()&&!paused&&!document.hidden)detailTimer=setTimeout(pollDetail,Math.max(30000,detailRetryUntil-Date.now()));}
 }
 function sample(){return (tape?.trades||[]).slice(0,rowLimit);}
 function renderProfile(){if(token?.network==='exchange'&&!isHyperliquid()){el('.pro-price-bins').innerHTML='<p class="pro-empty">Individual trade tape is not collected for this exchange feed.</p>';el('.pro-profile-note').textContent='Live OHLCV candles and source trade counts are available in the Candles tab.';return;}
  const rows=sample(),unit=isHyperliquid()?token.quote:pool?quoteName(tape?.identity?.quoteMint):chartState?.quoteMint?quoteName(chartState.quoteMint):'Awaiting quote';
  const eligible=(pool||chartState?.quoteMint?rows:[]).map(t=>({...t,p:isHyperliquid()||pool?t.price:t.reference_eligible&&finite(t.reference_price)?t.reference_price:null})).filter(t=>finite(t.p)&&t.p>0&&t.base_volume>0);
  const key=JSON.stringify([unit,tapeError,rows.length,eligible.map(t=>[t.identity,t.p,t.base_volume])]);if(profileKey===key)return;profileKey=key;el('.pro-profile-unit').textContent=unit;
  if(!eligible.length){el('.pro-price-bins').innerHTML='<p class="pro-empty">'+(tapeError?'Sample unavailable. Retry the public feed.':tape?'No priced execution sample in this view.':'Loading execution sample…')+'</p>';el('.pro-profile-note').textContent=`${rows.length} retained legs${tape?' · No eligible converted prices':''}. This is not resting order depth.`;return;}
  const min=Math.min(...eligible.map(t=>t.p)),max=Math.max(...eligible.map(t=>t.p)),n=min===max?1:10,step=(max-min)/n||1,bins=Array.from({length:n},(_,i)=>({low:min+i*step,high:min===max?max:min+(i+1)*step,flow:0,count:0}));
  for(const t of eligible){const b=bins[Math.min(n-1,Math.floor((t.p-min)/step))];b.flow+=t.base_volume;b.count++;}const peak=Math.max(...bins.map(b=>b.flow));
  el('.pro-price-bins').innerHTML=bins.reverse().map(b=>`<div class="pro-price-bin" title="${esc(num(b.low)+' – '+num(b.high)+' '+unit)}"><i style="width:${Math.max(0,b.flow/peak*100).toFixed(2)}%"></i><span>${num((b.low+b.high)/2)}</span><b>${num(b.flow,true)}</b><small>${b.count}</small></div>`).join('')+`<div class="pro-profile-total"><span>${eligible.length} priced legs</span><b>${num(eligible.reduce((a,t)=>a+t.base_volume,0),true)} ${esc(token.symbol)}</b></div>`;
  el('.pro-profile-note').textContent=`${rows.length} sampled ${isHyperliquid()?'Hyperliquid trades':'physical pool legs'} · ${rows.length-eligible.length} unpriced or zero-flow excluded. ${tapeError?'Previous sample retained. ':''}Base flow, not resting order depth.`;
 }
 const table=(headers,rows,empty)=>`<table class="pro-table"><thead><tr>${headers.map(h=>`<th scope="col">${h}</th>`).join('')}</tr></thead><tbody>${rows.join('')||`<tr><td colspan="${headers.length}" class="pro-empty">${empty}</td></tr>`}</tbody></table>`;
 function renderTab(force=false){
  const rows=sample(),key=JSON.stringify([tab,rowLimit,pool,tapeError,tape?.trades,chartState?.revision,chartState?.candles?.length,token?.research?.version]);if(!force&&key===tabKey)return;tabKey=key;const view=el('.pro-data-view'),scroll=[view.scrollTop,view.scrollLeft];let html='',caption='';
  if(tab==='executions'){
   caption=`${rows.length} / ${tape?.trades?.length||0} retained physical pool legs · raw native quotes · slot / transaction / event order${tapeError?' · Previous sample retained: '+tapeError:''}`;
   html=table(['Time (UTC)','Canonical order','Price / quote',`Base qty (${esc(token?.symbol)})`,'Quote qty','Pool','Receipt','Commitment'],rows.map(t=>{const q=quoteName(t.quote_mint||tape.identity.quoteMint),p=token.pools?.items?.find(p=>p.address===t.pool),sig=receipt(t);return `<tr><td>${date(t.time).slice(5)}</td><td class="pro-canonical">${num(t.slot)}<small>${num(t.tx_order)} / ${num(t.event_order)}</small></td><td>${num(t.price)}<small title="${esc(t.quote_mint||tape.identity.quoteMint)}">${esc(q)}</small></td><td>${num(t.base_volume)}</td><td>${num(t.quote_volume)}<small>${esc(q)}</small></td><td title="${esc(t.pool)}">${explorer('address',t.pool,p?.venue||short(t.pool))}</td><td>${sig?explorer('tx',sig,short(sig)):'—'}</td><td>${esc(t.commitment==='processed_observation'?'Processed':t.commitment==='finalized'?'Finalized':t.commitment||'Not reported')}</td></tr>`;}),token.network==='exchange'?'This exchange feed provides candles. Individual executions are not collected.':'No retained executions in this selection.');
  }else if(tab==='book'){
   caption=isHyperliquid()?`Hyperliquid · 20 price levels per side · ${book?.stale?'Older snapshot':book?.book?date(book.book.time/1000)+' UTC':'Waiting for snapshot'}`:'Order-book coverage';
   const levels=book?.book?.levels||[[],[]];
   html=isHyperliquid()?table(['Bid size','Bid price','Ask price','Ask size'],Array.from({length:Math.max(levels[0].length,levels[1].length)},(_,i)=>`<tr><td class="positive">${num(Number(levels[0][i]?.sz))}</td><td class="positive">${num(Number(levels[0][i]?.px))}</td><td class="negative">${num(Number(levels[1][i]?.px))}</td><td class="negative">${num(Number(levels[1][i]?.sz))}</td></tr>`),'Waiting for retained order-book snapshots.'):'<p class="pro-empty">Order-book snapshots are available for Hyperliquid instruments. Pool observations are not a resting order book.</p>';
  }else if(tab==='pools'){
   caption=`${token?.pools?.items?.length||0} verified pool identities · USD observations where available · ${token?.pools?.stale?'Older observation':date(token?.pools?.updated_at)+' UTC'}`;
   html=table(['Venue / pair','USD price','Liquidity (USD)','24h vol (USD)','Retained 1m bars','Pool address','Chart'],(token?.pools?.items||[]).map(p=>`<tr class="${pool===p.address?'pro-selected-pool':''}"><td>${esc(p.venue)}<small>${esc(p.base)} / ${esc(p.quote)}</small></td><td>${usd(p.price)}</td><td>${usd(p.liquidity,true)}</td><td>${usd(p.volume,true)}</td><td>${num(p.history_candles)}</td><td>${explorer('address',p.address,short(p.address))}</td><td><button data-pro="pool" data-pool="${esc(p.address)}" aria-pressed="${pool===p.address}">${pool===p.address?'Selected':'Open pool'}</button></td></tr>`),'No verified pools returned.');
  }else if(tab==='candles'){
   caption=`Loaded chart window · ${chartState?.interval||'—'} · source quote preserved · carried closes have zero recorded flow${chartError?' · Previous frame retained':''}`;
   html=table(['Open time (UTC)','Open','High','Low','Close',`Base flow (${esc(token?.symbol)})`,'Legs / trades','Quote / source'],[...(chartState?.candles||[])].reverse().map(b=>`<tr class="${b.synthetic?'pro-carried':''}"><td>${date(b.time/1000).slice(5)}</td>${['open','high','low','close'].map(k=>`<td>${num(b[k])}</td>`).join('')}<td>${num(b.baseVolume)}</td><td>${num(b.count)}</td><td>${esc(b.quoteCurrency||chartState.quote)}<small>${b.synthetic?'Carried close':esc((b.sources||[chartState.aggregation==='token'?'reference':'pool execution']).join(', '))}${b.mixedSource?' · mixed sources':''}</small></td></tr>`),'Select a source to load candles.');
  }else if(tab==='research'){
   const r=token?.research;caption=`TICKING RESEARCH / ${token?.symbol||''} · ${r?.reviewed_at?'Reviewed '+r.reviewed_at.slice(0,10):'Review pending'}${r?.version?' · v'+r.version:''}`;
   html=r?.status==='published'?`<article class="pro-research"><p class="pro-research-role">${esc(r.role)}</p><p class="pro-research-summary">${esc(r.summary)}</p><div>${(r.mechanics||[]).map(m=>`<section><h3>${esc(m.title)}</h3><p>${esc(m.text)} ${(m.sources||[]).map(i=>r.sources?.[i]?external(r.sources[i].url,'['+(i+1)+']'):'').join(' ')}</p></section>`).join('')}</div><section><h3>What we’re watching</h3><ul>${(r.watch||[]).map(w=>`<li>${esc(w)}</li>`).join('')}</ul></section><details><summary>Sources and review coverage</summary><ol>${(r.sources||[]).map(s=>`<li>${external(s.url,s.title)}</li>`).join('')}</ol><p>${esc(r.coverage)}</p></details></article>`:'<p class="pro-empty">A verified research profile is not available for this asset.</p>';
  }else{
   const d=chartState,c=d?.coverage;caption='Source boundaries and observation state · no live latency SLA';
   html=`<div class="pro-feed-report"><section><h3>Chart identity</h3><dl>${[['Asset mint',token?.mint],['Scope',pool||'All retained supported pools'],['Current quote',d?.quote||'Awaiting data'],['Displayed interval',d?.interval||'Awaiting data'],['Source',d?.source||'Awaiting data'],['Chart updated',date(d?.updated_at)+' UTC'],['Observation window',c?.scopeWindowStart?date(c.scopeWindowStart)+' to '+date(c.scopeWindowEnd)+' UTC':'Not reported'],['Selected market chart',token?.chart?.ready?'Retained history available':'Not ready']].map(([k,v])=>`<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join('')}</dl></section><section><h3>How to read this data</h3><p>${esc(c?.priceSampling||'The selected pool reports actual executions in its native quote.')}</p><p>${esc(c?.volumeDefinition||'Volumes are physical pool legs, not wallet net swaps.')}</p><p>${esc(d?.gapDisclosure||'Missing coverage is not evidence of no trading.')}</p>${d?.history?`<p>Archive ${esc(d.history.market)} in ${esc(d.history.archiveQuote)} precedes Solana reference in ${esc(d.history.currentQuote)}. No historical quote conversion. Transition: ${date(d.history.sourceTransition)} UTC.</p>`:''}<p>${esc(tape?.commitment||'Execution commitment not reported')} — token tape rows without commitment are not marked finalized.</p><p>Streaming retained updates with reconnect snapshots. Whole-Solana completeness and a production latency SLA are not claimed.</p>${[chartError,tapeError,detailError].filter(Boolean).map(e=>`<p class="pro-read-error">${esc(e)}</p>`).join('')}</section></div>`;
  }
  if(token?.network==='exchange'){
   if(tab==='executions')caption='Individual exchange executions are not collected';
   if(tab==='pools'){caption='Exchange instrument';html='<p class="pro-empty">'+esc(token.venue+' · '+token.market+' · '+token.marketType)+'</p>';}
   if(tab==='status'){caption='Exchange candle source';html='<div class="pro-feed-report"><section><h3>'+esc(token.venue+' · '+token.market)+'</h3><p>'+esc(token.marketType)+' last-traded-price candles in '+esc(token.quote)+'. Current candles use attributed composite references; archive components keep their source identity.</p><p>Retained history is paged separately from current updates. Missing source records remain unavailable.</p><p>Individual executions and mark prices are not provided by this candle feed.</p></section></div>';}
  }
  if(isHyperliquid()){
   if(tab==='executions'){caption=`${rows.length} retained Hyperliquid executions · ${esc(token.base)} / ${esc(token.quote)} · reconnect completeness unverified`;html=table(['Time (UTC)','Side','Price',`Size (${esc(token.base)})`,`Notional (${esc(token.quote)})`,'Trade ID'],rows.map(t=>`<tr><td>${date(t.time).slice(5)}</td><td class="${t.side==='B'?'positive':'negative'}">${t.side==='B'?'Buy':'Sell'}</td><td>${num(t.price)}</td><td>${num(t.base_volume)}</td><td>${num(t.quote_volume)}</td><td>${esc(t.tradeId)}</td></tr>`),'No retained executions in this window.');}
   if(tab==='status'){caption='Hyperliquid venue data';html='<div class="pro-feed-report"><section><h3>'+esc(token.market+' / '+token.quote)+'</h3><p>Last-traded-price candles from this exact Hyperliquid perpetual. Oracle and mark prices are not substituted. Other exchanges are not blended into these candles.</p><p>Public API history is limited to the latest 5,000 candles per supported interval. Earlier retained pages remain stored. Missing trades across reconnects are not claimed as recovered.</p><p>Live executions and 20-level order-book snapshots are collected by the native C++ receiver. This is a public feed, not a locally executing HyperCore node.</p></section></div>';}
  }
  el('.pro-table-caption').textContent=caption;view.innerHTML=html;view.scrollTop=scroll[0];view.scrollLeft=scroll[1];el('.pro-export').disabled=!token||tab==='research'||tab==='status'||tab==='book';el('.pro-row-limit').hidden=tab!=='executions'||token?.network==='exchange'&&!isHyperliquid();
 }
 function exportCsv(){
  let headers,rows,unit=chartState?.quote||'';
  if(tab==='executions'&&isHyperliquid()){headers=['time_utc','trade_id','market','side','price','base_size','quote_notional','source'];rows=sample().map(t=>[date(t.time),t.tradeId,token.market,t.side,t.price,t.base_volume,t.quote_volume,'hyperliquid_public']);}
  else if(tab==='executions'){headers=['time_utc','slot','transaction_order','event_order','base_mint','base_quantity','quote_mint','native_quote_price','quote_quantity','pool','signature','commitment','reference_price','reference_eligible'];rows=sample().map(t=>[date(t.time),t.slot,t.tx_order,t.event_order,token.mint,t.base_volume,t.quote_mint||tape.identity.quoteMint,t.price,t.quote_volume,t.pool,receipt(t),t.commitment||'',t.reference_price??'',t.reference_eligible??'']);}
  else if(tab==='pools'){headers=['venue','base','quote','usd_price','usd_liquidity','usd_volume_24h','retained_minute_bars','pool','fetched_utc'];rows=(token.pools?.items||[]).map(p=>[p.venue,p.base,p.quote,p.price??'',p.liquidity??'',p.volume??'',p.history_candles,p.address,date(token.pools.updated_at)]);}
  else if(tab==='candles'){headers=['time_utc','interval','open','high','low','close','base_quantity','count','quote','sources','synthetic','source_time_utc','pool'];rows=(chartState?.candles||[]).map(b=>[date(b.time/1000),chartState.interval,b.open,b.high,b.low,b.close,b.baseVolume,b.count,b.quoteCurrency||unit,(b.sources||[]).join(';'),!!b.synthetic,date(b.sourceTime/1000),chartState.pool||'reference']);}
  else return;
  const cell=v=>'"'+String(v??'').replace(/^[\s]*[=+\-@]/,"'$&").replaceAll('"','""')+'"',blob=new Blob(['\uFEFF'+[headers,...rows].map(row=>row.map(cell).join(',')).join('\r\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`ticking-${token.id}-${tab}-sample.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
 }
 function activateTab(id){if(!tabs.some(([t])=>t===id))return;tab=id;root.querySelectorAll('[role="tab"]').forEach(b=>{const selected=b.dataset.tab===tab;b.setAttribute('aria-selected',String(selected));b.tabIndex=selected?0:-1;});el('.pro-data-view').setAttribute('aria-labelledby','pro-tab-'+id);renderTab(true);}
 root.addEventListener('click',async e=>{const b=e.target.closest('[data-pro]');if(!b)return;
  switch(b.dataset.pro){case 'browse':{const open=root.classList.toggle('pro-assets-open');el('.pro-browse').setAttribute('aria-expanded',String(open));if(open)el('.pro-search input').focus();else el('.pro-browse').focus();break;}
   case 'assets':filter=b.dataset.filter;assetPage=0;root.querySelectorAll('[data-pro="assets"]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));renderAssets();break;
   case 'asset-page':assetPage+=Number(b.dataset.direction)>0?1:-1;renderAssets();el('.pro-asset-list').scrollTop=0;break;
   case 'tab':activateTab(b.dataset.tab);break;
   case 'pool':selectPool(b.dataset.pool);break;
   case 'export':exportCsv();break;
   case 'watch':{if(!token)return;let w=saved();w=w.includes(token.id)?w.filter(id=>id!==token.id):[...w,token.id];try{localStorage.setItem('tick-watchlist',JSON.stringify(w));renderMarket();}catch{el('.pro-copy-status').textContent='This browser cannot save preferences.';}break;}
   case 'copy-mint':try{await navigator.clipboard.writeText(token.mint);if(active())el('.pro-copy-status').textContent='Mint copied.';}catch{if(active())el('.pro-copy-status').textContent=token.mint;}break;
   case 'pause':paused=!paused;if(paused){catalogStream?.();catalogStream=null;tapeStream?.();tapeStream=null;bookStream?.();bookStream=null;run++;clearTimeout(tapeTimer);clearTimeout(detailTimer);cancel();tapeBusy=false;feedDispose?.();feedDispose=null;}else{startFeed();startTape();startCatalog();pollTape();pollDetail();}status();break;
   case 'retry':if(!token){location.reload();break;}if(!paused){startFeed();pollTape();pollDetail();}break;
  }
 },on);
 root.addEventListener('input',e=>{if(e.target.matches('.pro-search input')){query=e.target.value;assetPage=0;renderAssets();}},on);
 root.addEventListener('change',e=>{if(e.target.matches('.pro-scope select'))selectPool(e.target.value);if(e.target.matches('.pro-row-limit select')){rowLimit=Number(e.target.value)===100?100:50;renderTab(true);renderProfile();}},on);
 root.addEventListener('intervalchange',()=>{if(paused&&chartState&&chartState.interval!==chart.options.interval)el('.pro-chart-source').textContent='Updates paused · previous '+chartState.interval+' frame remains displayed. Resume to load '+chart.options.interval+'.';},on);
 root.addEventListener('keydown',e=>{if(e.key==='Escape'&&root.classList.contains('pro-assets-open')){root.classList.remove('pro-assets-open');el('.pro-browse').setAttribute('aria-expanded','false');el('.pro-browse').focus();}if(e.target.matches('[role="tab"]')&&['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();const i=tabs.findIndex(([id])=>id===tab),next=e.key==='Home'?0:e.key==='End'?tabs.length-1:(i+(e.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;activateTab(tabs[next][0]);el('#pro-tab-'+tab).focus();}},on);
 document.addEventListener('visibilitychange',()=>{if(document.hidden){catalogStream?.();catalogStream=null;tapeStream?.();tapeStream=null;bookStream?.();bookStream=null;run++;cancel();tapeBusy=false;clearTimeout(tapeTimer);clearTimeout(detailTimer);feedDispose?.();feedDispose=null;}else if(!paused){startFeed();startTape();startCatalog();pollTape();pollDetail();}},on);
 window.addEventListener('storage',e=>{if(e.key==='tick-watchlist'){renderAssets();if(token)renderMarket();}},on);
 (async()=>{try{
  const catalog=await catalogRead();
  if(!active())return;tokens=catalog;token=catalog.find(t=>t.id===ident);renderAssets();if(!token)throw Error('Choose a listed market from the market browser.');assetPage=Math.floor(tokens.findIndex(t=>t.id===token.id)/ASSET_PAGE_SIZE);renderMarket();
  // The verified catalog is enough to open the reference chart. Pool and
  // research details must not hold the asset list or chart behind their read.
  chart=new TickChart(el('.pro-price-chart'),{symbol:token.symbol,market:'pending:'+token.mint},{editable:true,interval:'1m',series:[],storageScope:'terminal',emptyMessage:'Loading recorded history…'});if(!requestedPool)selectPool(null,false);pollDetail(false);startCatalog();clockTimer=setInterval(()=>{if(active()&&!document.hidden)status();},10000);
 }catch(e){if(!active())return;el('.pro-selected').textContent='Market unavailable';el('.pro-asset-list').innerHTML='<p class="pro-empty">'+esc(e.message)+'<br><a href="#/markets">Open markets</a></p>';el('.pro-chart-source').textContent=e.message;el('.pro-refresh-status').textContent=e.message;el('.pro-retry').hidden=false;el('.pro-pause').disabled=true;}})();
 return ()=>{disposed=true;catalogStream?.();tapeStream?.();bookStream?.();run++;abort.abort();cancel();clearTimeout(tapeTimer);clearTimeout(detailTimer);clearInterval(clockTimer);feedDispose?.();controls?.();selectDispose?.();chart?.destroy();};
}
