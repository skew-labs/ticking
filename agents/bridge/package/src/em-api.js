// Economic Machine bindings for the Ticking staging build: API reads, formatting, wallet glyphs, artwork and the shared
// wallet card. Every number shown comes from the em-galaxy / em-studio reads behind /api/public/em/ (no client estimates).
export const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const short=a=>typeof a==='string'&&a.length>12?a.slice(0,6)+'…'+a.slice(-4):String(a??'');
export const ADDRESS=/^0x[a-f0-9]{40}$/;
export const TXHASH=/^0x[a-f0-9]{64}$/;
export const MARKET=/^(arbitrum|robinhood):0x[a-f0-9]{40}$/;
// Strict input validation: everything that reaches a route, a request or the DOM goes through these
export const isAddress=v=>typeof v==='string'&&ADDRESS.test(v);
export const normAddress=v=>{const a=typeof v==='string'?v.trim():'';return /^0x[0-9a-fA-F]{40}$/.test(a)?a.toLowerCase():null;};
export const isChain=v=>v==='arbitrum'||v==='robinhood';
export const isMarket=v=>typeof v==='string'&&MARKET.test(v);
export const isTx=v=>typeof v==='string'&&TXHASH.test(v);
export const workspaceWalletRoute=(market,address)=>isMarket(market)&&isAddress(address)?`#/console/workspace/${encodeURIComponent(market)}?panel=wallets&wallet=${address}`:null;
export const mapRoute=(chain,address)=>isChain(chain)?`#/?view=map&chain=${chain}${isAddress(address)?'&address='+address:''}`:'#/?view=map';
const finite=Number.isFinite;
export function usd(v,{sign=false}={}){if(!finite(v))return '—';const a=Math.abs(v),s=v<0?'-':sign&&v>0?'+':'';if(a>=1e9)return s+'$'+(a/1e9).toFixed(2)+'B';if(a>=1e6)return s+'$'+(a/1e6).toFixed(2)+'M';if(a>=1e3)return s+'$'+(a/1e3).toFixed(1)+'K';return s+'$'+a.toFixed(a>=100?0:2);}
export function num(v){if(!finite(v))return '—';const a=Math.abs(v);if(a>=1e9)return (v/1e9).toFixed(2)+'B';if(a>=1e6)return (v/1e6).toFixed(2)+'M';if(a>=1e4)return (v/1e3).toFixed(1)+'K';return v.toLocaleString('en-US',{maximumSignificantDigits:5});}
export const day=t=>finite(t)?new Date(t*1000).toLocaleDateString('en-GB',{day:'numeric',month:'short',year:'numeric',timeZone:'UTC'}):'—';
export const CHAINS={arbitrum:{name:'Arbitrum One',logo:'/assets/ecosystem/arbitrum.svg',explorer:'https://arbiscan.io'},robinhood:{name:'Robinhood Chain',logo:'/assets/ecosystem/robinhood-chain.jpg',explorer:'https://robinhoodchain.blockscout.com'}};

// Reads: 12 s timeout, one retry on network/5xx, short client cache and in-flight coalescing per URL
const memo=new Map(),inflight=new Map();
export class EmError extends Error{constructor(message,status=0){super(message);this.name='EmError';this.status=status;}}
export async function em(path,{signal,ttl=30000}={}){
 if(typeof path!=='string'||!/^[a-z]+\/[A-Za-z0-9_./?&=:%+-]{1,400}$/.test(path))throw new EmError('Invalid request.');
 const url='/api/public/em/'+path,hit=memo.get(url);if(hit&&Date.now()-hit.at<ttl)return hit.data;
 let job=inflight.get(url);
 if(!job){job=(async()=>{let last;for(let attempt=0;attempt<2;attempt++){const ctl=new AbortController(),timer=setTimeout(()=>ctl.abort(),12000);
   try{const r=await fetch(url,{signal:ctl.signal,credentials:'omit',headers:{Accept:'application/json'}});let d={};try{d=await r.json();}catch{}
    if(r.ok){memo.set(url,{at:Date.now(),data:d});if(memo.size>300)memo.delete(memo.keys().next().value);return d;}
    last=new EmError(r.status===429?'Too many requests right now. Wait a moment and retry.':typeof d.detail==='string'?d.detail:typeof d.error==='string'?d.error:'Economic Machine data is unavailable right now.',r.status);
    if(r.status<500||r.status===501)throw last;}
   catch(e){if(e instanceof EmError&&e.status&&e.status<500)throw e;last=e.name==='AbortError'?new EmError('The Economic Machine did not respond in time.',504):e instanceof EmError?e:new EmError('Network error. Check the connection and retry.');}
   finally{clearTimeout(timer);}
   await new Promise(r=>setTimeout(r,600));}throw last;})().finally(()=>inflight.delete(url));inflight.set(url,job);}
 if(!signal)return job;
 return new Promise((resolve,reject)=>{if(signal.aborted)return reject(new DOMException('Aborted','AbortError'));signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true});job.then(resolve,reject);});
}
export const stateHTML=(kind,text,{retry=false}={})=>`<div class="em-state em-state-${kind}" role="${kind==='error'?'alert':'status'}">${kind==='loading'?'<span class="em-spinner" aria-hidden="true"></span>':''}<span>${esc(text)}</span>${retry?'<button type="button" class="em-btn em-btn-quiet" data-em-retry>Retry</button>':''}</div>`;

// Wallet categories -> name, glyph and the muted map colour (light and dark themes read the same hue)
export const CATS={
 eoa_user:['Wallet','wallet','#7d80ea'],eoa_whale:['Whale','whale','#3b6fd6'],mev_bot:['MEV bot','bolt','#c76678'],dex_pool:['DEX pool','pool','#17856d'],
 dex_router:['DEX router','route','#2c9c8c'],token_contract:['Token contract','token','#b7791f'],contract_other:['Contract','code','#8a8fa3'],other_contract:['Contract','code','#8a8fa3'],
 lending:['Lending','bank','#5658e8'],bridge:['Bridge','bridge','#8b5cf6'],cex_hot_wallet:['Exchange hot wallet','exchange','#d97706'],cex_deposit:['Exchange deposit','inbox','#e29a3b'],
 multisig_or_safe:['Multisig / Safe','shield','#4f46e5'],nft_contract:['NFT contract','image','#db2777'],unknown:['Unclassified','dot','#a0a4b8']};
export const cat=c=>CATS[c]||CATS.unknown;
const G={
 wallet:'M4 7h15v12H4zM4 7V5h11v2M14 12h5v3h-5z',whale:'M3 13c2-4 7-6 12-4 2 1 3 0 4-2l2 1c-1 3-2 4-4 5 0 3-3 5-7 5-4 0-6-2-7-5zM8 13h.01',
 bolt:'M13 3 5 13h6l-1 8 8-11h-6z',pool:'M12 4c3 4 6 7 6 10a6 6 0 0 1-12 0c0-3 3-6 6-10z',route:'M6 6h8a3 3 0 0 1 0 6H9a3 3 0 0 0 0 6h9M16 15l3 3-3 3',
 token:'M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16zM9 12h6M12 9v6',code:'m9 8-4 4 4 4m6-8 4 4-4 4',bank:'M3 10 12 5l9 5M5 10v7m4-7v7m6-7v7m4-7v7M3 19h18',
 bridge:'M3 17c3-6 15-6 18 0M3 13h18M7 13v5m10-5v5',exchange:'M5 20V8l7-4 7 4v12M9 20v-6h6v6',inbox:'M4 13h4l2 3h4l2-3h4M4 13l2-7h12l2 7v6H4z',
 shield:'M12 3 5 6v6c0 4 3 7 7 9 4-2 7-5 7-9V6zM9.5 12l2 2 3.5-4',image:'M4 5h16v14H4zM8 10h.01M20 16l-5-5-8 8',dot:'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z'};
export const glyph=(name,size=16)=>`<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" aria-hidden="true"><path d="${G[name]||G.dot}" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
export const walletIcon=(category,size=30)=>{const [n,g,c]=cat(category);return `<span class="em-wallet-icon" style="--em-c:${c};width:${size}px;height:${size}px" title="${esc(n)}">${glyph(g,Math.round(size*.55))}</span>`;};

// Protocol artwork: a protocol's governance-token logo when Ticking has one, otherwise a monogram
const PROTOCOLS=[['uniswap','Uniswap'],['aave','Aave'],['gmx','GMX'],['pancake','PancakeSwap'],['curve','Curve'],['balancer','Balancer'],['camelot','Camelot'],['sushi','SushiSwap'],
 ['compound','Compound'],['radiant','Radiant'],['pendle','Pendle'],['morpho','Morpho'],['arbitrum bridge','Arbitrum'],['robinhood bridge','Robinhood'],['stargate','Stargate'],['across','Across'],
 ['hop','Hop'],['layerzero','LayerZero'],['relay','Relay'],['usdc','USDC'],['usdt0','USDT0'],['usdg','USDG'],['ethena','Ethena'],['maple','Maple'],['algebra','Algebra'],['silo','Silo'],['dolomite','Dolomite'],['opensea','OpenSea'],['curve','Curve']];
export function protocolOf(name){const n=String(name||'').toLowerCase();for(const [k,label] of PROTOCOLS)if(n.includes(k))return {key:k.split(' ')[0].replace('pancake','pancakeswap').replace('sushi','sushiswap'),label};return null;}
// Self-hosted protocol artwork (build_assets.py): /assets/em/protocols/index.json, loaded once
let art=null,artJob=null;
export function artwork(){if(art)return Promise.resolve(art);if(!artJob)artJob=fetch('/assets/em/protocols/index.json',{credentials:'omit'}).then(r=>r.ok?r.json():{protocols:{}}).then(d=>{art=d.protocols||{};return art;}).catch(()=>{artJob=null;art=null;return {};});return artJob;}
export function protocolArt(key){if(!key)return null;if(key==='robinhood')return CHAINS.robinhood.logo;if(key==='arbitrum')return CHAINS.arbitrum.logo;const a=art?.[key];return a&&/^\/assets\/em\/protocols\/[a-z0-9-]+\.(webp|svg)$/.test(a.path)?a.path:null;}
export function protocolIcon(name,size=22){const p=protocolOf(name),src=protocolArt(p?.key);return src?`<img class="em-logo" src="${src}" width="${size}" height="${size}" alt="" loading="lazy" decoding="async">`:'';}
export const tokenIcon=(chain,address,symbol,size=20)=>isChain(chain)&&isAddress(address)?`<img class="em-logo" src="/api/public/em/icon/${chain}/${address}" width="${size}" height="${size}" alt="${esc(symbol||'')}" loading="lazy" decoding="async">`:'';

export const emitWallet=detail=>window.dispatchEvent(new CustomEvent('emwallet',{detail}));
export const isEmMarket=token=>!!token&&isChain(token.network)&&ADDRESS.test(token.mint||'');
export const chainOfMarket=token=>isEmMarket(token)?token.network:null;

const FLAG={unverified_principal:'Principal not confirmed by a receipt token',morpho_interest_runaway:'Market with runaway bad debt',vault_pass_through:'Vault: assets belong to depositors',
 debt_exceeds_assets:'Debt exceeds priced assets',collateral_unpriced:'Collateral has no trusted price',supplied_unpriced:'Supplied asset has no trusted price',lp_unpriced:'LP position has no trusted price',
 lp_owner_inferred:'LP owner inferred from the sender',unverified:'Supplied + collateral far above observed inflow'};
const KIND={liquid:'Holding',supplied:'Supplied',collateral:'Collateral',debt:'Borrowed',lp:'Liquidity',perps:'Perps'};

// The one wallet card: map inspector, workspace dossier and assistant answers all render this
export function walletCard(card,{compact=false,actions=true}={}){
 if(!card)return '';
 const s=card.sheet||{},chain=CHAINS[card.chain]||{name:card.chain};
 const parts=[['liquid',s.liquid,'var(--em-liquid)'],['supplied',s.supplied,'var(--em-supplied)'],['collateral',s.collateral,'var(--em-collateral)'],['lp',s.lp,'var(--em-lp)']].filter(p=>finite(p[1])&&p[1]>0);
 const gross=parts.reduce((n,p)=>n+p[1],0),debt=Math.abs(s.debt||0);
 const bar=gross?`<div class="em-sheet-bar" role="img" aria-label="Balance sheet">${parts.map(p=>`<i style="flex:${p[1]};background:${p[2]}"></i>`).join('')}</div>${debt?`<div class="em-sheet-bar em-sheet-debt"><i style="width:${Math.min(100,debt/gross*100).toFixed(1)}%"></i></div>`:''}`:'';
 const legend=gross?`<dl class="em-sheet-legend">${parts.map(p=>`<div><dt><i style="background:${p[2]}"></i>${KIND[p[0]]}</dt><dd>${usd(p[1])}</dd></div>`).join('')}${debt?`<div><dt><i style="background:var(--em-debt)"></i>Borrowed</dt><dd>${usd(-debt)}</dd></div>`:''}</dl>`:'';
 const flags=(card.flags||[]).map(f=>`<span class="em-flag" title="${esc(FLAG[f]||f)}">Unverified · ${esc((FLAG[f]||f).split(':')[0])}</span>`).join('');
 const pos=(card.positions||[]).filter(p=>finite(p.usd)).slice(0,compact?3:6).map(p=>`<li><span class="em-pos-kind em-k-${esc(p.kind)}">${esc(KIND[p.kind]||p.kind)}</span><span class="em-pos-asset">${esc(p.asset||'')}</span><span class="em-pos-where">${esc(p.protocol||'')}</span><b>${usd(p.kind==='debt'?-Math.abs(p.usd):p.usd)}</b>${p.flag?'<em class="em-flag-dot" title="'+esc(FLAG[p.flag]||p.flag)+'">!</em>':''}</li>`).join('');
 const recent=compact?'':(card.recent||[]).slice(0,3).map(r=>`<li><span>${esc(r.text||'')}</span><small>${day(r.time)}</small></li>`).join('');
 const story=card.headline?`<p class="em-story">${esc(card.headline)}</p>`:'';
 const addr=isAddress(card.address)?card.address:'',ch=isChain(card.chain)?card.chain:'',mk=isMarket(card.market)?card.market:'';
 const chartRoute=workspaceWalletRoute(mk,addr)||'',mapRouteHref=mapRoute(ch,addr);
 const buttons=actions?`<div class="em-card-actions">${chartRoute?`<a class="em-btn em-btn-primary" href="${chartRoute}" data-em-chart="${mk}" data-em-address="${addr}" data-em-chain="${ch}">Show on ${esc(card.marketSymbol||'the')} chart</a>`:''}<a class="em-btn" href="${mapRouteHref}" data-em-map="${addr}" data-em-chain="${ch}">Show on map</a>${chain.explorer?`<a class="em-btn em-btn-quiet" href="${chain.explorer}/address/${esc(card.address)}" target="_blank" rel="noopener noreferrer">Explorer ↗</a>`:''}</div>`:'';
 return `<article class="em-card${compact?' em-card-compact':''}" data-em-card="${esc(card.address)}">
 <header>${walletIcon(card.category,compact?30:38)}<div><b class="em-mono">${esc(short(card.address))}</b><small>${esc(card.entity||card.categoryName||'')}${card.protocol?' · '+esc(card.protocol):''}</small></div><span class="em-chain"><img src="${esc(chain.logo||'')}" width="14" height="14" alt="">${esc(chain.name||'')}</span></header>
 ${finite(s.net_worth)?`<div class="em-networth"><small>Net worth${card.asOf?' · '+day(card.asOf):''}</small><strong>${usd(s.net_worth)}</strong></div>${bar}${legend}`:`<p class="em-muted">No open lending, LP or perp positions were decoded for this wallet.</p>`}
 ${flags?`<div class="em-flags">${flags}</div>`:''}${story}
 ${pos?`<ul class="em-positions">${pos}</ul>`:''}${recent?`<ul class="em-recent">${recent}</ul>`:''}${buttons}</article>`;
}

let styled=false;
export function injectStyles(){
 if(styled||document.getElementById('em-ticking-styles'))return;styled=true;
 const s=document.createElement('style');s.id='em-ticking-styles';s.textContent=CSS;document.head.append(s);
}
const CSS=`
:root{--em-liquid:#a9b4ff;--em-supplied:#5bc3a2;--em-collateral:#5658e8;--em-lp:#2fb3c9;--em-debt:#e8a14b;--em-paper:#fff;--em-tint:#f4f3fd;--em-line:#e7e5f3;--em-ink:var(--ink,#1c2233);--em-muted:var(--muted,#6b7088);--em-accent:var(--accent,#5658e8)}
html[data-theme=dark]{--em-paper:#1d1e22;--em-tint:#24252b;--em-line:#33353c;--em-collateral:#9d8cf5;--em-liquid:#7f8bd6}
.em-mono{font-family:"Space Grotesk",var(--font,"DM Sans"),sans-serif;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.em-wallet-icon{display:inline-grid;place-items:center;flex:none;border-radius:10px;background:color-mix(in srgb,var(--em-c) 13%,var(--em-paper));color:var(--em-c);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--em-c) 22%,transparent)}
.em-logo{border-radius:50%;object-fit:cover;background:var(--em-tint);flex:none}
.em-card{display:grid;gap:12px;padding:16px;border:1px solid var(--em-line);border-radius:14px;background:var(--em-paper);color:var(--em-ink);font:13px/1.45 var(--font,"DM Sans"),sans-serif}
.em-card header{display:flex;align-items:center;gap:10px;min-width:0}.em-card header>div{display:grid;min-width:0;flex:1}.em-card header b{font-size:15px;font-weight:600}
.em-card header small{color:var(--em-muted);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.em-chain{display:inline-flex;align-items:center;gap:5px;padding:3px 8px;border-radius:999px;background:var(--em-tint);font-size:11px;color:var(--em-muted);white-space:nowrap}.em-chain img{border-radius:50%}
.em-networth{display:grid;gap:2px}.em-networth small{color:var(--em-muted);font-size:11px;letter-spacing:.02em}.em-networth strong{font:500 26px/1.1 "Space Grotesk",var(--font),sans-serif;letter-spacing:-.02em}
.em-sheet-bar{display:flex;height:8px;border-radius:999px;overflow:hidden;background:var(--em-tint);gap:2px}.em-sheet-bar i{display:block;height:100%}
.em-sheet-debt{height:5px}.em-sheet-debt i{background:repeating-linear-gradient(135deg,var(--em-debt) 0 5px,color-mix(in srgb,var(--em-debt) 55%,transparent) 5px 8px);border-radius:999px}
.em-sheet-legend{display:grid;grid-template-columns:1fr 1fr;gap:4px 14px;margin:0;font-size:12px}.em-sheet-legend div{display:flex;justify-content:space-between;gap:8px}.em-sheet-legend dt{display:flex;align-items:center;gap:6px;color:var(--em-muted)}.em-sheet-legend dd{margin:0;font-family:"Space Grotesk",sans-serif;font-variant-numeric:tabular-nums}
.em-sheet-legend i{width:8px;height:8px;border-radius:2px;display:inline-block}
.em-flags{display:flex;flex-wrap:wrap;gap:6px}.em-flag{font-size:11px;padding:3px 8px;border-radius:999px;background:#fff4e5;color:#9a5b05;border:1px solid #f6d9ac}html[data-theme=dark] .em-flag{background:#3a2c17;color:#f2c47c;border-color:#5b4320}
.em-story{margin:0;padding:10px 12px;border-radius:10px;background:var(--em-tint);font-size:12.5px}
.em-positions,.em-recent{list-style:none;margin:0;padding:0;display:grid;gap:2px}
.em-positions li{display:grid;grid-template-columns:auto auto 1fr auto auto;align-items:center;gap:8px;padding:6px 0;border-top:1px solid var(--em-line);font-size:12.5px}
.em-pos-kind{font-size:10.5px;padding:2px 6px;border-radius:6px;background:var(--em-tint);color:var(--em-muted)}.em-k-collateral{color:var(--em-collateral)}.em-k-debt{color:#b9741a}.em-k-supplied{color:#17856d}
.em-pos-asset{font-weight:600}.em-pos-where{color:var(--em-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.em-positions b{font-family:"Space Grotesk",sans-serif;font-weight:500;font-variant-numeric:tabular-nums}
.em-flag-dot{font-style:normal;display:grid;place-items:center;width:15px;height:15px;border-radius:50%;background:#fff4e5;color:#9a5b05;font-size:10px;font-weight:700}
.em-recent li{display:flex;justify-content:space-between;gap:10px;padding:6px 0;border-top:1px solid var(--em-line);font-size:12px}.em-recent small{color:var(--em-muted);white-space:nowrap}
.em-card-actions{display:flex;flex-wrap:wrap;gap:8px}.em-btn{display:inline-flex;align-items:center;gap:6px;padding:7px 12px;border-radius:9px;border:1px solid var(--em-line);background:var(--em-paper);color:var(--em-ink);font-size:12.5px;text-decoration:none;cursor:pointer}
.em-btn:hover{border-color:color-mix(in srgb,var(--em-accent) 45%,var(--em-line))}.em-btn-primary{background:var(--em-accent);border-color:var(--em-accent);color:#fff}.em-btn-primary:hover{filter:brightness(1.06)}.em-btn-quiet{border-color:transparent;color:var(--em-muted)}
.em-muted{color:var(--em-muted);margin:0}
.em-card-compact{padding:12px;gap:9px}.em-card-compact .em-networth strong{font-size:21px}
/* workspace: wallets tab */
.em-wallets{display:grid;grid-template-columns:minmax(260px,1.1fr) minmax(0,1.4fr);gap:14px;padding:12px 14px;height:100%;min-height:0;overflow:auto;font:13px/1.45 var(--font,"DM Sans"),sans-serif;color:var(--em-ink);container-type:inline-size}
.em-wallets.em-narrow{grid-template-columns:1fr;padding:10px}.em-wallets.em-narrow .em-constellation{min-height:190px}.em-wallets.em-narrow .em-holders th:nth-child(2),.em-wallets.em-narrow .em-holders td:nth-child(2){display:none}
.em-wallets h3{margin:0 0 6px;font:600 12px/1.2 var(--font);letter-spacing:.04em;text-transform:uppercase;color:var(--em-muted)}
.em-constellation{position:relative;border:1px solid var(--em-line);border-radius:14px;background:radial-gradient(circle at 50% 50%,color-mix(in srgb,var(--em-accent) 9%,transparent),transparent 62%),var(--em-tint);min-height:250px;overflow:hidden}
.em-constellation svg{width:100%;height:100%;display:block}.em-constellation .em-node{cursor:pointer}.em-constellation .em-node:hover circle.em-ring{stroke:var(--em-accent);stroke-width:2}
.em-flowbars{display:grid;gap:5px;margin-top:10px}.em-flowbars div{display:grid;grid-template-columns:110px 1fr auto;gap:8px;align-items:center;font-size:12px}.em-flowbars span.em-track{height:6px;border-radius:999px;background:var(--em-line);overflow:hidden}.em-flowbars span.em-track i{display:block;height:100%;border-radius:inherit;background:var(--em-accent)}
.em-flowbars b{font-family:"Space Grotesk",sans-serif;font-weight:500;font-variant-numeric:tabular-nums}
.em-holders{width:100%;border-collapse:collapse;font-size:12.5px}.em-holders th{text-align:left;font-weight:500;color:var(--em-muted);font-size:11px;padding:6px 8px;border-bottom:1px solid var(--em-line)}
.em-holders td{padding:7px 8px;border-bottom:1px solid var(--em-line);vertical-align:middle}.em-holders tr{cursor:pointer}.em-holders tr:hover td{background:var(--em-tint)}.em-holders tr[aria-selected=true] td{background:color-mix(in srgb,var(--em-accent) 9%,var(--em-paper))}
.em-holders .em-who{display:flex;align-items:center;gap:9px}.em-holders .em-who div{display:grid}.em-holders small{color:var(--em-muted)}
.em-share{display:grid;gap:3px;justify-items:end}.em-share i{display:block;height:4px;border-radius:999px;background:var(--em-accent);opacity:.75}
.em-empty{padding:18px;color:var(--em-muted)}.em-empty a{color:var(--em-accent)}
/* workspace: wallet side tab + dossier */
.pro-insights[data-em-wallet=open]>*:not(.workspace-side-tabs):not(.em-side-dossier){display:none!important}
.em-side-dossier{overflow:auto;padding:14px 14px 18px;min-height:0;flex:1}.em-side-dossier .em-card{border:0;padding:0;background:transparent}
.em-side-dossier .em-recent li span{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.em-side-dossier>header{display:flex;justify-content:space-between;align-items:center;margin:0 0 10px;font:600 12px var(--font);letter-spacing:.04em;text-transform:uppercase;color:var(--em-muted)}
.workspace-side-tabs [data-side-view=wallet][aria-pressed=true]{color:var(--em-accent)}
/* chart markers */
.em-marker-layer{position:absolute;pointer-events:none;z-index:4;overflow:hidden}
.em-marker{position:absolute;transform:translate(-50%,-50%);pointer-events:auto;padding:0;font:inherit;display:grid;place-items:center;width:22px;height:22px;border-radius:50%;background:var(--em-paper);border:2px solid var(--em-c,var(--em-accent));color:var(--em-c,var(--em-accent));box-shadow:0 4px 12px -4px #2b245080;cursor:default}
.em-marker svg{width:12px;height:12px}.em-marker::after{content:attr(data-label);position:absolute;left:50%;bottom:calc(100% + 6px);transform:translateX(-50%);white-space:nowrap;padding:3px 7px;border-radius:7px;background:var(--em-ink);color:#fff;font:11px var(--font);opacity:0;transition:opacity .15s;pointer-events:none}
.em-marker:hover::after{opacity:1}
.em-chart-chip{position:absolute;left:12px;top:44px;z-index:5;display:flex;align-items:center;gap:8px;padding:5px 6px 5px 8px;border-radius:999px;background:var(--em-paper);border:1px solid var(--em-line);box-shadow:0 8px 24px -14px #2b2450;font:12px var(--font);color:var(--em-ink)}
.em-chart-chip button{border:0;background:transparent;color:var(--em-muted);cursor:pointer;font-size:14px;padding:0 4px}
.em-state{display:flex;align-items:center;gap:10px;padding:14px;color:var(--em-muted);font:13px/1.4 var(--font,"DM Sans"),sans-serif}.em-state-error{color:#9a3a4a}html[data-theme=dark] .em-state-error{color:#f0a3b1}
.em-spinner{width:14px;height:14px;border-radius:50%;border:2px solid var(--em-line);border-top-color:var(--em-accent);animation:em-spin .8s linear infinite;flex:none}@keyframes em-spin{to{transform:rotate(1turn)}}
@media (prefers-reduced-motion:reduce){.em-spinner{animation:none}}
.em-btn:focus-visible,.em-holders tr:focus-visible,.em-marker:focus-visible,.em-hub:focus-visible,.em-node:focus-visible,.em-zoom:focus-visible{outline:2px solid var(--em-accent);outline-offset:2px}
.em-marker-legend{position:absolute;left:12px;top:78px;z-index:5;display:flex;gap:10px;padding:4px 10px;border-radius:999px;background:var(--em-paper);border:1px solid var(--em-line);font:11px var(--font);color:var(--em-muted)}
.em-marker-legend span{display:inline-flex;align-items:center;gap:5px}.em-marker-legend i{width:9px;height:9px;border-radius:50%;border:2px solid var(--c);background:var(--em-paper)}
.em-marker[data-side=B]{border-radius:6px 6px 50% 50%}.em-marker[data-side=A]{border-radius:50% 50% 6px 6px}
.em-tip{position:absolute;z-index:7;max-width:260px;padding:8px 10px;border-radius:10px;background:var(--em-ink);color:#fff;font:12px/1.4 var(--font);box-shadow:0 10px 24px -12px #000a;pointer-events:none}
html[data-theme=dark] .em-tip{background:#f0f1f5;color:#1c2233}.em-tip b{display:block;font-weight:600}.em-tip small{opacity:.8}
.em-wallet-btn{border:0;background:transparent;color:var(--em-accent);cursor:pointer;font:inherit;padding:0;text-decoration:underline;text-underline-offset:2px}
/* assistant answers */
.assistant-message .em-card{margin-top:10px}.em-list-card{display:grid;gap:8px;margin-top:10px;padding:12px;border:1px solid var(--em-line);border-radius:14px;background:var(--em-paper)}
.em-list-card ol{margin:0;padding:0;list-style:none;display:grid;gap:2px}.em-list-card li{display:grid;grid-template-columns:auto 1fr auto;gap:9px;align-items:center;padding:7px 4px;border-top:1px solid var(--em-line);cursor:pointer;border-radius:8px}
.em-list-card li:hover{background:var(--em-tint)}.em-list-card li div{display:grid;min-width:0}.em-list-card li small{color:var(--em-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.em-list-card li b{font-family:"Space Grotesk",sans-serif;font-weight:500;font-variant-numeric:tabular-nums;text-align:right}
.em-list-card p{margin:0;font-size:11.5px;color:var(--em-muted)}
`;

// Assistant answers: one structured card per Economic Machine tool result (rendered inside the Ticking assistant thread)
export function assistantCard(card){
 if(!card||typeof card!=='object')return '';
 if(card.kind==='wallet')return walletCard(card,{compact:true});
 const row=(addr,chain,market,left,sub,right)=>{const a=isAddress(addr)?addr:'',m=isMarket(market)?market:'',c=isChain(chain)?chain:'';
  const href=workspaceWalletRoute(m,a)||mapRoute(c,a);
  return `<li><a href="${href}" ${m?`data-em-chart="${m}"`:''} data-em-address="${a}" data-em-chain="${c}" style="display:contents;color:inherit;text-decoration:none">${left}<div><span class="em-mono">${esc(short(a))}</span><small>${sub}</small></div><b>${right}</b></a></li>`;};
 if(card.kind==='top')return `<section class="em-list-card"><b>${esc(card.title||'Wallets')}</b><ol>${(card.rows||[]).map(r=>row(r.address,card.chain,r.market,walletIcon(r.category,26),esc(Object.entries(r.collateral||{}).slice(0,3).map(([k,v])=>k+' '+usd(v)).join(' · '))+' → '+usd(r.usdgDebt)+' USDG',usd(r.netWorth))).join('')}</ol>${card.note?`<p>${esc(card.note)}</p>`:''}</section>`;
 if(card.kind==='holders')return `<section class="em-list-card"><b>Top ${esc(card.symbol||'')} holders</b><ol>${(card.holders||[]).slice(0,8).map(h=>row(h.address,card.chain,card.market,walletIcon(h.category,26),esc(h.entity?h.entity.replace(/_/g,' '):h.categoryName||''),Number.isFinite(h.share)?h.share.toFixed(2)+'%':'—')).join('')}</ol>${card.holderCount?`<p>${num(card.holderCount)} holders · Economic Machine balances</p>`:''}</section>`;
 if(card.kind==='trace'){const cps=card.counterparties||[];return `<section class="em-list-card"><b>Where ${esc(short(card.address))}'s money moved</b>${cps.length?`<ol>${cps.slice(0,8).map(p=>row(p.address,card.chain,null,walletIcon(p.category,26),esc((p.direction==='in'?'Sent to this wallet':p.direction==='out'?'Received from this wallet':'Both ways')+(p.assets?.length?' · '+p.assets.join(', '):'')+(p.entity?' · '+p.entity.replace(/_/g,' '):'')),usd(p.usd))).join('')}</ol>`:''}${card.wallet?walletCard(card.wallet,{compact:true}):''}${card.note?`<p>${esc(card.note)}</p>`:''}</section>`;}
 return '';
}

// Execution inspector for on-chain EM swaps: explorer link plus the wallet behind the swap (tx -> sender -> dossier)
const EXPLORERS={arbitrum:'https://arbiscan.io',robinhood:'https://robinhoodchain.blockscout.com'};
export function emInspector(token,trade){
 if(!isEmMarket(token)||!trade)return '';
 const chain=chainOfMarket(token),side=trade.side==='B'?'Buy':trade.side==='A'?'Sell':'Swap',tx=isTx(trade.tx)?trade.tx:null,wallet=isAddress(trade.wallet||trade.trader)?(trade.wallet||trade.trader):null;
 const when=Number.isFinite(trade.time)?new Date(trade.time*1000).toISOString().slice(11,19)+' UTC':'—';
 return `<header><span>SELECTED SWAP</span><button type="button" data-execution-close aria-label="Close execution inspector">×</button></header><strong>${esc(CHAINS[chain].name)} DEX <small class="${trade.side==='B'?'positive':trade.side==='A'?'negative':''}">${side}</small></strong><p>${when} · ${esc(num(trade.price))} USD · ${esc(num(trade.base_volume))} ${esc(token.symbol)}</p><p data-execution-focus role="status"></p><div class="workspace-execution-links">${tx?`<a href="${EXPLORERS[chain]}/tx/${tx}" target="_blank" rel="noopener noreferrer">Transaction ↗</a>`:''}<button type="button" data-em-execution-wallet="${wallet||''}" data-em-execution-tx="${tx||''}" data-em-execution-chain="${chain}" ${wallet||tx?'':'disabled'}>${wallet?'Open wallet '+esc(short(wallet)):'Find the wallet behind this swap'}</button></div><p data-execution-map-status role="status"></p>`;
}
export function emInspectorClick(e,root){
 const b=e.target.closest('[data-em-execution-wallet]');if(!b||b.disabled)return false;
 const chain=b.dataset.emExecutionChain,wallet=b.dataset.emExecutionWallet,tx=b.dataset.emExecutionTx,out=root.querySelector('[data-execution-map-status]');
 if(isAddress(wallet)){emitWallet({address:wallet,chain});return true;}
 if(!isTx(tx)||!isChain(chain))return true;
 b.disabled=true;if(out)out.textContent='Reading the transaction sender…';
 em('tools/tx?'+new URLSearchParams({chain,tx})).then(d=>{if(isAddress(d.from)){if(out)out.textContent='Sender '+short(d.from)+'. Opening the wallet dossier.';emitWallet({address:d.from,chain});}else if(out)out.textContent='The sender could not be read for this transaction.';})
  .catch(err=>{if(out)out.textContent=err.message;b.disabled=false;});
 return true;
}
