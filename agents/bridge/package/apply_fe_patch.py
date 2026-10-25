"""First-class Arbitrum / Robinhood networks in the Ticking frontend and SDK, plus the 2D wallet map layer.

usage: apply_fe_patch.py <release_dir> [--assets <em-assets dir>] [--check]
<release_dir> is a copy of the current public release (.inputs/). Anchored, all-or-nothing edits.
  * SDK: createDataFeed/agent clients accept `network` (solana default) and exact 0x addresses; responses must carry
    the requested identity.network; chart identity keys become `${network}:…` (Solana keys unchanged).
  * Terminal, Markets, preview and preload pass `network` for EVM rows; explorer links, receipts, tapes, venue logos
    and execution snapshots are network-aware; the Markets source filter lists Arbitrum One and Robinhood Chain.
  * Wallet layer (unchanged design from Option B): 2D map, Wallets tab, wallet dossier, chart markers, assistant cards.
New modules: src/networks.js, src/em-api.js, src/em-map.js, src/em-wallets.js. Assets: assets/em/{map,protocols}.
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from patchlib import apply  # noqa: E402

MODULES = ['networks.js', 'em-api.js', 'em-map.js', 'em-wallets.js', 'em-economy.js']
SEL_EVM = "...networkParams(token)"

PATCHES = {
    'sdk/data-feed.js': [
        ("export function createDataFeed({mint,pool,market='SOL',aggregation='pool',history='full',pageSize=600,gapPolicy='carry',tokenProvider,apiBase='https://ticking.dev',onStatus=()=>{}}={}){\n",
         "const NETWORKS=['solana','arbitrum','robinhood'];\n"
         "export function createDataFeed({mint,pool,market='SOL',network='solana',aggregation='pool',history='full',pageSize=600,gapPolicy='carry',tokenProvider,apiBase='https://ticking.dev',onStatus=()=>{}}={}){\n"
         " if(!NETWORKS.includes(network)||network!=='solana'&&(aggregation==='exchange'||!(/^0x[0-9a-f]{40}$/.test(mint||'')||/^0x(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(pool||''))))throw Error('Use an exact token address on a supported network.');\n"),
        ("JSON.stringify([url.origin,url.pathname,mint||null,pool||null,market,aggregation,history,gapPolicy,interval]);",
         "JSON.stringify([url.origin,url.pathname,mint||null,pool||null,market,aggregation,history,gapPolicy,interval,...(network==='solana'?[]:[network])]);"),
        ("try{grant=await Promise.race([tokenProvider({mint,",
         "try{grant=await Promise.race([tokenProvider({mint,...(network==='solana'?{}:{network}),"),
        ("if(!tokenProvider){q.set('aggregation',aggregation);if(pinned)q.set('pool',pinned);if(mint)q.set('mint',mint);else if(!pool)q.set('market',market);if(pool)q.set('pool',pool);}",
         "if(!tokenProvider){q.set('aggregation',aggregation);if(pinned)q.set('pool',pinned);if(mint)q.set('mint',mint);else if(!pool)q.set('market',market);if(pool)q.set('pool',pool);if(network!=='solana')q.set('network',network);}"),
        ("d.identity?.network==='solana')||!Array.isArray(d.candles)", "d.identity?.network===network)||!Array.isArray(d.candles)"),
        ("if(pinned||pool)query.set('pool',pinned||pool);}", "if(pinned||pool)query.set('pool',pinned||pool);if(network!=='solana')query.set('network',network);}"),
        (":data.aggregation==='token'?`solana:token:${data.identity.baseMint}:${data.identity.quoteMint}:${data.identity.aggregation}`:`solana:${data.pool}:",
         ":data.aggregation==='token'?`${data.identity.network}:token:${data.identity.baseMint}:${data.identity.quoteMint}:${data.identity.aggregation}`:`${data.identity.network}:${data.pool}:"),
    ],
    'sdk/agent-client.js': [
        ("async getSnapshot({market='SOL',mint,pool,aggregation='token',", "async getSnapshot({market='SOL',mint,pool,network='solana',aggregation='token',"),
        ("[mint,pool].some(v=>v!==undefined&&(typeof v!=='string'||!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(v))))throw Error('Use a bounded market ID or exact Solana address.');",
         "[mint,pool].some(v=>v!==undefined&&(typeof v!=='string'||!(network==='solana'?/^[1-9A-HJ-NP-Za-km-z]{32,44}$/:/^0x(?:[0-9a-f]{40}|[0-9a-f]{64})$/).test(v)))||!['solana','arbitrum','robinhood'].includes(network))throw Error('Use a bounded market ID or an exact address on a supported network.');"),
        ("const selection={aggregation,interval,limit:String(limit),history,gap_policy:'recorded',...(mint?{mint}:pool?{pool}:{market})};",
         "const selection={aggregation,interval,limit:String(limit),history,gap_policy:'recorded',...(mint?{mint}:pool?{pool}:{market}),...(network==='solana'?{}:{network})};"),
    ],
    'sdk/agent-live.js': [
        ("const {market='SOL',mint,pool,aggregation='token',interval='1m',limit=300}=selection;",
         "const {market='SOL',mint,pool,network='solana',aggregation='token',interval='1m',limit=300}=selection;"),
        ("[mint,pool].some(v=>v!==undefined&&!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(v))",
         "[mint,pool].some(v=>v!==undefined&&!(network==='solana'?/^[1-9A-HJ-NP-Za-km-z]{32,44}$/:/^0x(?:[0-9a-f]{40}|[0-9a-f]{64})$/).test(v))||!['solana','arbitrum','robinhood'].includes(network)"),
        ("const path='/api/v1/stream/candles?'+new URLSearchParams({aggregation,interval,limit:String(limit),...(mint?{mint}:pool?{pool}:{market})});",
         "const path='/api/v1/stream/candles?'+new URLSearchParams({aggregation,interval,limit:String(limit),...(mint?{mint}:pool?{pool}:{market}),...(network==='solana'?{}:{network})});"),
    ],
    'src/agent-analysis.js': [
        ("['solana','exchange'].includes(frame.identity.network)", "['solana','arbitrum','robinhood','exchange'].includes(frame.identity.network)"),
    ],
    'src/token-icons.js': [
        ("exchange-binance-(?:spot|usdm)-[a-z0-9]{2,30})-[a-f0-9]{16}", "exchange-binance-(?:spot|usdm)-[a-z0-9]{2,30}|(?:arbitrum|robinhood)-0x[0-9a-f]{40})-[a-f0-9]{16}"),
    ],
    'src/execution-model.js': [
        ("export function executionReceipt(row){\n if(signature.test(row.signature||''))return row.signature;",
         "const evmTx=/^0x[0-9a-f]{64}$/,evmPool=/^0x(?:[0-9a-f]{40}|[0-9a-f]{64})$/,evm=n=>n==='arbitrum'||n==='robinhood';\n"
         "export function executionReceipt(row){\n if(signature.test(row.signature||'')||evmTx.test(row.signature||''))return row.signature;"),
        ("if(!address.test(mint)||data?.identity?.network!=='solana'||data.identity.baseMint!==mint",
         "const net=data?.identity?.network,token=evm(net)?/^0x[0-9a-f]{40}$/:address,pools=evm(net)?evmPool:address,quoted=q=>evm(net)?q==='USD':address.test(q||'');\n"
         " if(!token.test(mint)||!(net==='solana'||evm(net))||data.identity.baseMint!==mint"),
        ("row.mint!==mint||!address.test(row.pool||'')||!address.test(row.quote_mint||'')||", "row.mint!==mint||!pools.test(row.pool||'')||!quoted(row.quote_mint)||"),
        (" So11111111111111111111111111111111111111112:'SOL'\n};", " So11111111111111111111111111111111111111112:'SOL',\n USD:'USD'\n};"),
    ],
    'src/execution-inspector.js': [
        ("import {parseInterval} from './intervals.js';", "import {parseInterval} from './intervals.js';\nimport {emInspector,emInspectorClick} from './em-api.js';"),
        ("root.innerHTML=`<header><span>SELECTED EXECUTION</span>", "root.innerHTML=emInspector(token,trade)||`<header><span>SELECTED EXECUTION</span>"),
        ("  const button=e.target.closest('[data-execution-map]');", "  if(emInspectorClick(e,root))return;\n  const button=e.target.closest('[data-execution-map]');"),
    ],
    'src/market-executions.js': [
        ("import {executionSnapshot,executionQuote,executionVenue,shortAddress} from './execution-model.js';",
         "import {executionSnapshot,executionQuote,executionVenue,shortAddress} from './execution-model.js';\nimport {chainOf,networkParams,explorerUrl,NETWORK_LOGOS,NETWORK_NAMES} from './networks.js';"),
        ("const logo=venue=>`<span class=\"execution-logo\"${venue.image?'':' title=\"Solana network\"'}><img src=\"${venue.image||'/assets/ecosystem/solana.svg'}\"",
         "const venueLogo=(venue,n='solana')=>`<span class=\"execution-logo\"${venue.image?'':` title=\"${NETWORK_NAMES[n]} network\"`}><img src=\"${venue.image||NETWORK_LOGOS[n]}\""),
        (" let token=initialToken,pools=new Map(),destroyed=false,visible=false,paused=false,hover=false,focused=false;\n",
         " let token=initialToken,pools=new Map(),destroyed=false,visible=false,paused=false,hover=false,focused=false;\n"
         " const logo=venue=>venueLogo(venue,chainOf(token)),external=(path,label,cls='',title='')=>{const [type,...rest]=path.split('/'),url=explorerUrl(chainOf(token),type,decodeURIComponent(rest.join('/')));return url?`<a class=\"${cls}\" href=\"${url}\" target=\"_blank\" rel=\"noopener noreferrer\"${title?` title=\"${esc(title)}\"`:''}>${label}</a>`:`<span class=\"${cls}\">${label}</span>`;};\n"),
        ("subscribeStream('/api/public/stream/trades?'+new URLSearchParams({mint:token.mint,aggregation:'token'}),",
         "subscribeStream('/api/public/stream/trades?'+new URLSearchParams({mint:token.mint,aggregation:'token',...networkParams(token)}),"),
        ("fetch('/api/public/engine/trades?'+new URLSearchParams({mint:token.mint,aggregation:'token',limit:'100'}),",
         "fetch('/api/public/engine/trades?'+new URLSearchParams({mint:token.mint,aggregation:'token',limit:'100',...networkParams(token)}),"),
    ],
    'src/market-snapshot.js': [
        ("fetch('/api/public/markets?view='+view,", "fetch('/api/public/markets?view='+view+'&networks=all',"),
    ],
    'src/market-page.js': [
        ("import {tokenImage} from './token-icons.js';", "import {tokenImage} from './token-icons.js';\nimport {networkParams,networkName,networkBadge} from './networks.js';"),
        ("</small></h1><p>Solana · ${esc(t.category)}</p>", "</small></h1><p>${networkBadge(t)||esc(networkName(t))} · ${esc(t.category)}</p>"),
        ("<small>${esc(t.symbol)}${t.stale?' <span class=\"market-stale-dot\"", "<small>${esc(t.symbol)}${networkBadge(t)}${t.stale?' <span class=\"market-stale-dot\""),
        ("</select></label>", "</select></label><div class=\"market-network-chips\" role=\"group\" aria-label=\"Network\">${[['all','All networks'],['solana','Solana'],['arbitrum','Arbitrum One'],['robinhood','Robinhood Chain']].map(([v,l])=>`<button type=\"button\" data-market-chip=\"${v}\" aria-pressed=\"false\">${l}</button>`).join('')}</div>"),
        ("let pageIndex=0;const pageSize=100;let explorer=null,assistant=null,network='all',watchOnly=watch;",
         "let pageIndex=0;const pageSize=100;let explorer=null,assistant=null,network=(n=>['solana','arbitrum','robinhood','hyperliquid','exchange'].includes(n)?n:'all')(new URLSearchParams(location.hash.split('?')[1]||'').get('network')),watchOnly=watch;"),
        ("root.addEventListener('change',e=>{if(e.target.matches('[data-market-network]')){network=e.target.value;pageIndex=0;renderTable(true);}},on);",
         "const syncNetwork=()=>{const s=root.querySelector('[data-market-network]');if(s&&s.value!==network)s.value=network;root.querySelectorAll('[data-market-chip]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.marketChip===network)));};\n"
         " root.addEventListener('change',e=>{if(e.target.matches('[data-market-network]')){network=e.target.value;pageIndex=0;syncNetwork();renderTable(true);}},on);\n"
         " root.addEventListener('click',e=>{const c=e.target.closest('[data-market-chip]');if(c){network=c.dataset.marketChip;pageIndex=0;syncNetwork();renderTable(true);}},on);"),
        ("const visible=filtered.slice(start,start+pageSize),ids=new Set(visible.map(t=>t.id)),tbody=table.querySelector('tbody');",
         "const visible=filtered.slice(start,start+pageSize),ids=new Set(visible.map(t=>t.id)),tbody=table.querySelector('tbody');syncNetwork();\n"
         "  // The preview follows the filtered list: a hidden selection is replaced by the first matching market.\n"
         "  clearTimeout(root._previewSync);if(explorer&&filtered.length&&!filtered.some(t=>t.id===explorer.getSelected()))root._previewSync=setTimeout(()=>{if(explorer&&!filtered.some(t=>t.id===explorer.getSelected()))explorer.select(filtered[0]);},220);"),
        ("  executions();\n }\n function syncChartHeader(data){",
         "  executions();\n"
         "  if(['arbitrum','robinhood'].includes(t.network)){let eco=body.querySelector('.em-economy-slot');if(!eco){eco=document.createElement('section');eco.className='em-economy-slot public-stat-panel';eco.setAttribute('aria-label','Economic Machine');body.querySelector('.public-token-stats')?.before(eco);}\n"
         "   if(eco.dataset.market!==t.id){eco.dataset.market=t.id;import('./em-economy.js').then(m=>{if(eco.isConnected&&eco.dataset.market===t.id)m.mountEconomyPanel(eco,{token:t,toggles:false});}).catch(()=>{eco.textContent='The Economic Machine panel could not load.';});}}\n"
         " }\n function syncChartHeader(data){"),
        ("['Network','Solana']].map(", "['Network',esc(networkName(t))]].map("),
        # identity-first header (release 20261019-02): EVM markets name their network before the full detail arrives
        ("<div><h1>${esc(token.name)}${verificationBadge(token)} <small>${esc(token.symbol)}</small></h1></div>",
         "<div><h1>${esc(token.name)}${verificationBadge(token)} <small>${esc(token.symbol)}</small></h1>${['arbitrum','robinhood'].includes(token.network)?`<p>${networkBadge(token)} · ${esc(token.category)}</p>`:''}</div>"),
        ("<h3>Token overview</h3><dl>${[['Market cap',usd(t.cap,true)],['Fully diluted valuation',usd(t.fdv,true)],['Solana volume (24h)',usd(t.volume,true)]",
         "<h3>Token overview</h3><dl>${[['Market cap',usd(t.cap,true)],['Fully diluted valuation',usd(t.fdv,true)],[['arbitrum','robinhood'].includes(t.network)?esc(networkName(t))+' DEX volume (24h)':'Solana volume (24h)',usd(t.volume,true)]"),
        ("<span>${t.id==='SOL'?'Wrapped SOL mint':'Token mint'}</span>", "<span>${t.id==='SOL'?'Wrapped SOL mint':['arbitrum','robinhood'].includes(t.network)?'Token contract':'Token mint'}</span>"),
        ("function chainPanel(t){\n const c=t.chain||{};\n",
         "function chainPanel(t){\n const c=t.chain||{};\n"
         " if(['arbitrum','robinhood'].includes(t.network))return `<section class=\"public-stat-panel chain-panel\"><span class=\"market-eyebrow\">ON-CHAIN IDENTITY</span><h3>Token contract</h3><dl><div><dt>Network</dt><dd>${esc(networkName(t))}</dd></div><div><dt>Address</dt><dd>${esc(t.mint.slice(0,6)+'…'+t.mint.slice(-4))}</dd></div>${Number.isFinite(c.decimals)?`<div><dt>Decimals</dt><dd>${esc(c.decimals)}</dd></div>`:''}</dl>${link(c.source_url,'Inspect on '+(t.network==='arbitrum'?'Arbiscan':'Blockscout'),'market-text-link')}<p class=\"chain-context\">Ticking pins this market to its exact network and contract address. Prices are a USD on-chain DEX reference across observed pools.</p></section>`;\n"),
        ("(network==='all'||(network==='solana'?t.network!=='exchange':network==='hyperliquid'?",
         "(network==='all'||(network==='solana'?!['exchange','arbitrum','robinhood'].includes(t.network):['arbitrum','robinhood'].includes(network)?t.network===network:network==='hyperliquid'?"),
        ('<option value="solana">Solana</option>', '<option value="solana">Solana</option><option value="arbitrum">Arbitrum One</option><option value="robinhood">Robinhood Chain</option>'),
        ("{mint:token.mint,pool:selectedPool||undefined,aggregation:selectedPool?'pool':'token'}),onStatus:",
         "{mint:token.mint,pool:selectedPool||undefined,aggregation:selectedPool?'pool':'token',...networkParams(token)}),apiBase:location.origin,onStatus:"),
    ],
    'src/market-explorer.js': [
        ("import {MARKET_INTERVALS} from './intervals.js';", "import {MARKET_INTERVALS} from './intervals.js';\nimport {networkParams,networkName} from './networks.js';"),
        ("{mint:t.mint,aggregation:'token'})});\n preview.innerHTML=", "{mint:t.mint,aggregation:'token',...networkParams(t)})});\n preview.innerHTML="),
        ("<small>${esc(token.name)} · ${esc(token.network==='exchange'?token.venue||'Exchange':'Solana')}</small>", "<small>${esc(token.name)} · ${esc(token.network==='exchange'?token.venue||'Exchange':networkName(token))}</small>"),
    ],
    'src/route-preload.js': [
        ("import {preloadMarkets,peekMarkets} from './market-snapshot.js';", "import {preloadMarkets,peekMarkets} from './market-snapshot.js';\nimport {networkParams} from './networks.js';"),
        ("{mint:token.mint,aggregation:'token'})},experience.interval);", "{mint:token.mint,aggregation:'token',...networkParams(token)})},experience.interval);"),
    ],
    'src/navigation-direct.js': [
        ("if(kind==='evm')return {state:'clarify',message:'Token-address charts currently accept Solana mints. Use a listed symbol for other markets.'};",
         "if(kind==='evm')return {state:'choices',message:'Choose the network for this token address.',choices:[{title:'Arbitrum One chart',route:'#/console/workspace/arbitrum%3A'+asset.toLowerCase()},{title:'Robinhood Chain chart',route:'#/console/workspace/robinhood%3A'+asset.toLowerCase()}]};"),
    ],
    'src/workspace-route.js': [
        ("export function workspaceRoute(id=agentContext()?.asset||'SOL',{pool=null,panel=null}={}){",
         "export function workspaceRoute(id=agentContext()?.asset||'SOL',{pool=null,panel=null,wallet=null}={}){"),
        ("if(pool&&/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(pool))params.set('pool',pool);",
         "if(pool&&(/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(pool)||/^(?:arbitrum|robinhood):/.test(id)&&/^0x(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(pool)))params.set('pool',pool);"),
        ("if(['executions','book','agent','pools','candles','status','research'].includes(panel))params.set('panel',panel);",
         "if(['executions','economy','wallets','book','agent','pools','candles','status','research'].includes(panel))params.set('panel',panel);\n if(wallet&&/^0x[a-f0-9]{40}$/.test(wallet))params.set('wallet',wallet);"),
        ("return workspaceRoute(id,{pool:params.get('pool'),panel:params.get('panel')});",
         "return workspaceRoute(id,{pool:params.get('pool'),panel:params.get('panel'),wallet:params.get('wallet')});"),
    ],
    'src/flow-home.js': [
        ("import('./flow-map.js').then(async({mountFlowMap})=>{if(alive)mapDispose=await mountFlowMap(desk,{signal});",
         "import('./em-map.js').then(async({mountEmMap})=>{if(alive)mapDispose=await mountEmMap(desk,{signal});"),
        ("import {mountFlowMotion} from './flow-motion.js';", "import {mountFlowMotion} from './flow-motion.js';\nimport {injectNetworkStyles} from './networks.js';\ninjectNetworkStyles();"),
        ('<a class="aero-inline-link" href="#/markets">Browse all markets ${arrow}</a></div>',
         '<a class="aero-inline-link" href="#/markets">Browse all markets ${arrow}</a></div><nav class="home-network-chips" aria-label="New networks">'
         '<a href="#/markets?network=robinhood"><img src="/assets/ecosystem/robinhood-chain.jpg" alt=""><span><b>Robinhood Chain</b> stock tokens</span></a>'
         '<a href="#/markets?network=arbitrum"><img src="/assets/ecosystem/arbitrum.svg" alt=""><span><b>Arbitrum One</b> markets</span></a>'
         '<a href="#/console/workspace/robinhood%3A0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec?panel=economy"><span><b>Economic Machine</b> · who holds and borrows NVDA</span></a>'
         '<a href="#/?view=map&amp;chain=robinhood"><span><b>Wallet map</b> · Robinhood Chain</span></a></nav>'),
    ],
    'src/intelligence-panel.js': [
        ("import {renderAssistantResponse} from './assistant-response.js';",
         "import {renderAssistantResponse} from './assistant-response.js';\nimport {assistantCard,injectStyles as injectEmStyles} from './em-api.js';\nimport {economyCard} from './em-economy.js';"),
        ("${m.role==='assistant'?renderAssistantResponse(m.text):'<p>'+esc(m.text)+'</p>'}",
         "${m.role==='assistant'?renderAssistantResponse(m.text):'<p>'+esc(m.text)+'</p>'}${m.card?(m.card.kind==='economy'?economyCard(m.card):assistantCard(m.card)):''}"),
        ("workspace=false,onNavigate=null}={}){", "workspace=false,onNavigate=null}={}){injectEmStyles();"),
        ("$('[data-assistant-context]').textContent=ctx.token?ctx.token.symbol+' · '+(ctx.chart?.options.interval||'market overview')+(ctx.execution?' · execution selected':''):'Market workspace';",
         "$('[data-assistant-context]').textContent=ctx.token?ctx.token.symbol+' · '+(ctx.chart?.options.interval||'market overview')+(ctx.execution?' · execution selected':''):'Market workspace';"
         "{const sym=ctx.token?.symbol||'',evm=['arbitrum','robinhood'].includes(ctx.token?.network),ta=$('.assistant-compose textarea'),box=$('.assistant-suggestions'),key=sym+':'+evm;"
         "if(ta)ta.placeholder=sym?(evm?`Ask about ${sym}: why it moved, liquidation levels, or money flow…`:`Ask about ${sym}: analyze it, explain RSI, or plan a position…`):'Analyze SOL, explain RSI, or plan a position…';"
         "if(box&&box.dataset.emFor!==key){box.dataset.emFor=key;box.querySelectorAll('[data-em-intent]').forEach(b=>b.remove());"
         "if(evm)box.insertAdjacentHTML('afterbegin',[['why did '+sym+' move','Why it moved','Price vs money flow'],['liquidation levels for '+sym,'Liquidation levels','Nearest clusters'],['who is leveraged on '+sym,'Who is leveraged','Borrowers and collateral'],['money flow for '+sym,'Money flow','Net flow by wallet class']]"
         ".map(([p,t,d])=>`<button type=\"button\" data-em-intent data-assistant-prompt=\"${esc(p)}\"><strong>${esc(t)}</strong><small>${esc(d)}</small></button>`).join(''));}}"),
        ("let result=positionRequest(q)?null:directNavigation(routed);",
         "let result=positionRequest(q)||/(?:^|[^A-Za-z0-9])0x[a-fA-F0-9]{40}(?![A-Za-z0-9])/.test(routed)?null:directNavigation(routed);"),
    ],
    'src/conversation-state.js': [
        ("||/^#\\/\\?view=map(?:&chain=(solana|arbitrum|robinhood))?(?:&address=(0x[a-fA-F0-9]{40}|[1-9A-HJ-NP-Za-km-z]{32,44}))?$/.test(r));",
         "||/^#\\/\\?view=map(?:&chain=(solana|arbitrum|robinhood))?(?:&address=(0x[a-fA-F0-9]{40}|[1-9A-HJ-NP-Za-km-z]{32,44}))?$/.test(r)||/^#\\/console\\/workspace\\/(?:arbitrum|robinhood)%3A0x[a-f0-9]{40}\\?panel=wallets(?:&wallet=0x[a-f0-9]{40})?$/.test(r));"),
        (".map(l=>({title:l.title.slice(0,120),route:l.route}))}))};",
         ".map(l=>({title:l.title.slice(0,120),route:l.route})),...(m.card&&typeof m.card==='object'&&!Array.isArray(m.card)&&['wallet','top','holders','trace','economy'].includes(m.card.kind)&&JSON.stringify(m.card).length<16000?{card:m.card}:{})}))};"),
        ("links,source:result.resolver==='economic-machine'?'Economic Machine':'Muse · navigation'});",
         "links,source:result.resolver==='economic-machine-wallets'?'Economic Machine · wallets':result.resolver==='economic-machine'?'Economic Machine':'Muse · navigation',...(result.card?{card:result.card}:{})});"),
    ],
    # identity-first chart open (release 20261019-02): EVM identities are '<network>:<0x address>'
    'src/market-identity.js': [
        (" return /^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(t.mint);\n}",
         " if(['arbitrum','robinhood'].includes(t.network))return /^0x[0-9a-f]{40}$/.test(t.mint)&&t.id===t.network+':'+t.mint;\n return /^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(t.mint);\n}"),
    ],
    'src/workspace-markets.js': [
        ("t.network==='exchange'?'global':'solana';}", "t.network==='exchange'?'global':['arbitrum','robinhood'].includes(t.network)?t.network:'solana';}"),
        ("({solana:'Solana',global:'Global crypto',hyperliquid:'Hyperliquid'})", "({solana:'Solana',arbitrum:'Arbitrum One',robinhood:'Robinhood Chain',global:'Global crypto',hyperliquid:'Hyperliquid'})"),
        ("['all','solana','global','hyperliquid'].includes(p.source)", "['all','solana','arbitrum','robinhood','global','hyperliquid'].includes(p.source)"),
    ],
    'src/pro-terminal.js': [
        ("import {tokenImage} from './token-icons.js';", "import {tokenImage} from './token-icons.js';\nimport {chainOf,networkParams,networkName,explorerUrl,NETWORK_LOGOS,networkBadge} from './networks.js';"),
        ("<small class=\"pro-asset-origin\">${esc(marketSourceLabel(t))}", "<small class=\"pro-asset-origin\">${['arbitrum','robinhood'].includes(t.network)?networkBadge(t,true):''}${esc(marketSourceLabel(t))}"),
        ('<option value="solana">Solana</option><option value="global">Global crypto</option>', '<option value="solana">Solana</option><option value="arbitrum">Arbitrum One</option><option value="robinhood">Robinhood Chain</option><option value="global">Global crypto</option>'),
        ("const tabs=[['executions','Executions'],", "const tabs=[['executions','Executions'],['economy','Economy'],['wallets','Wallets'],"),
        ("if(['executions','book','agent','pools','candles','status','research'].includes(initialPanel))dock.open(initialPanel);",
         "if(['executions','economy','wallets','book','agent','pools','candles','status','research'].includes(initialPanel))dock.open(initialPanel);"),
        # catalog identity: EVM rows are '<network>:<0x address>' with that exact address as mint
        (":(/^[1-9A-HJ-NP-Za-km-z]{32,44}$/).test(t.mint);if(typeof t.id!=='string'",
         ":['arbitrum','robinhood'].includes(t.network)?/^0x[0-9a-f]{40}$/.test(t.mint)&&t.id===t.network+':'+t.mint:(/^[1-9A-HJ-NP-Za-km-z]{32,44}$/).test(t.mint);if(typeof t.id!=='string'"),
        (" const isHyperliquid=()=>token?.source==='hyperliquid_public';\n",
         " const isHyperliquid=()=>token?.source==='hyperliquid_public';\n"
         " const evmToken=()=>chainOf(token)!=='solana',explorer=(type,address,label)=>{const n=chainOf(token),kind=type==='address'&&label==='Mint'&&n!=='solana'?'token':type,url=explorerUrl(n,kind,address);return url?external(url,n!=='solana'&&label==='Mint'?'Contract':label):esc(label);};\n"),
        ("const quoteName=mint=>mint==='EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'?'USDC':",
         "const quoteName=mint=>mint==='USD'?'USD':mint==='EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'?'USDC':"),
        ("function receipt(t){if(/^[1-9A-HJ-NP-Za-km-z]{64,90}$/.test(t.signature||''))return t.signature;",
         "function receipt(t){if(/^[1-9A-HJ-NP-Za-km-z]{64,90}$/.test(t.signature||'')||/^0x[0-9a-f]{64}$/.test(t.signature||''))return t.signature;"),
        (":d.identity?.network!=='solana'||d.identity?.baseMint!==token.mint)", ":d.identity?.network!==chainOf(token)||d.identity?.baseMint!==token.mint)"),
        ("{mint:token.mint,pool:pool||undefined,aggregation:pool?'pool':'token'}),apiBase:location.origin,",
         "{mint:token.mint,pool:pool||undefined,aggregation:pool?'pool':'token',...networkParams(token)}),apiBase:location.origin,"),
        ("{mint:token.mint,aggregation:pool?'pool':'token'});if(pool)q.set('pool',pool);\n  tapeStream=subscribeStream(",
         "{mint:token.mint,aggregation:pool?'pool':'token',...networkParams(token)});if(pool)q.set('pool',pool);\n  tapeStream=subscribeStream("),
        ("{mint:token.mint,aggregation:pool?'pool':'token',limit:'100'});if(pool)q.set('pool',pool);const d=await read('engine/trades?'+q);",
         "{mint:token.mint,aggregation:pool?'pool':'token',limit:'100',...networkParams(token)});if(pool)q.set('pool',pool);const d=await read('engine/trades?'+q);"),
        ("<dt>${isHyperliquid()?'Venue base volume (24h)':'Solana volume (24h)'}</dt>",
         "<dt>${isHyperliquid()?'Venue base volume (24h)':evmToken()?networkName(token)+' DEX volume (24h)':'Solana volume (24h)'}</dt>"),
        ("src=\"${executionVenue(p).image||'/assets/ecosystem/solana.svg'}\"", "src=\"${executionVenue(p).image||NETWORK_LOGOS[chainOf(token)]}\""),
        ("  el('.pro-table-caption').textContent=caption;view.innerHTML=html;",
         "  if(evmToken()&&tab==='executions'){const n=chainOf(token);\n"
         "   caption=`${rows.length} / ${tape?.trades?.length||0} decoded ${networkName(token)} swaps · USD · block / transaction / log order${tapeError?' · '+(tape?.trades?.length?'Previous sample retained: ':'Read failed: ')+tapeError:''}`;\n"
         "   html=table(['Time (UTC)','Side','Price (USD)',`Size (${esc(token.symbol)})`,'Notional (USD)','Wallet','Transaction','Block'],rows.map(t=>{const tx=receipt(t),url=explorerUrl(n,'tx',tx);return `<tr><td><button type=\"button\" class=\"workspace-execution-select\" data-pro-execution=\"${esc(t.identity)}\">${date(t.time).slice(5)} ↗</button></td><td class=\"${t.side==='B'?'positive':t.side==='A'?'negative':''}\">${t.side==='B'?'Buy':t.side==='A'?'Sell':'Swap'}</td><td>${num(t.price)}</td><td>${num(t.base_volume)}</td><td>${num(t.quote_volume)}</td><td>${/^0x[0-9a-f]{40}$/.test(t.trader||'')?`<button type=\"button\" class=\"em-wallet-btn\" data-em-wallet=\"${t.trader}\" data-em-chain=\"${n}\">${short(t.trader)}</button>`:'—'}</td><td>${url?`<a href=\"${url}\" target=\"_blank\" rel=\"noopener noreferrer\">${short(tx)} ↗</a>`:'—'}</td><td class=\"pro-canonical\">${num(t.slot)}<small>${num(t.tx_order)} / ${num(t.event_order)}</small></td></tr>`;}),tapeError?'Swap read failed. Retry to reconnect this source.':tape?'No decoded swaps in this window.':'Loading on-chain swaps…');}\n"
         "  if(evmToken()&&tab==='status'){caption=networkName(token)+' on-chain source';html='<div class=\"pro-feed-report\"><section><h3>'+esc(networkName(token)+' · '+token.symbol)+'</h3><p>'+esc((chartState?.coverage?.priceSampling||'')+' '+(chartState?.coverage?.quoteConversion||''))+'</p><p>Policy '+esc(chartState?.identity?.historyPolicy||'em_pool_reference_v2')+'. Deep archive before the hot suffix\\'s first retained minute; the live tail after it. Completeness of the on-chain swap archive is not attested.</p></section></div>';}\n"
         "  el('.pro-table-caption').textContent=caption;view.innerHTML=html;"),
        (" function renderTab(force=false){\n  if(root.dataset.marketDataOpen!=='true')return;\n",
         " function renderTab(force=false){\n  if(root.dataset.marketDataOpen!=='true')return;\n"
         "  if(tab!=='wallets'&&walletsTab){walletsTab.destroy();walletsTab=null;}\n"
         "  if(tab!=='economy'&&economyTab){economyTab.destroy();economyTab=null;}\n"
         "  if(tab==='economy'){\n"
         "   el('.pro-table-caption').textContent='Economy · money state, leverage and flows (Economic Machine)';el('.pro-export').disabled=true;el('.pro-row-limit').hidden=true;\n"
         "   if(economyTab&&economyTab.token===token?.id&&!force)return;economyTab?.destroy();economyTab=null;const epoch=++walletsEpoch;el('.pro-data-view').innerHTML='<p class=\"pro-empty\">Opening the Economic Machine…</p>';\n"
         "   import('./em-economy.js').then(m=>{if(!active()||tab!=='economy'||epoch!==walletsEpoch)return;economyTab=m.mountEconomyPanel(el('.pro-data-view'),{token,compact:el('.pro-data-view').clientWidth<700});}).catch(()=>{if(active()&&tab==='economy'&&epoch===walletsEpoch)el('.pro-data-view').innerHTML='<p class=\"pro-empty\">The Economic Machine could not load. Switch tabs and try again.</p>';});return;\n"
         "  }\n"
         "  if(tab==='wallets'){\n"
         "   el('.pro-table-caption').textContent='Wallets · holders and money flow (Economic Machine)';el('.pro-export').disabled=true;el('.pro-row-limit').hidden=true;\n"
         "   if(walletsTab&&walletsTab.token===token?.id&&!force)return;walletsTab?.destroy();walletsTab=null;const epoch=++walletsEpoch;el('.pro-data-view').innerHTML='<p class=\"pro-empty\">Opening wallets…</p>';\n"
         "   import('./em-wallets.js').then(m=>{if(!active()||tab!=='wallets'||epoch!==walletsEpoch)return;walletsTab=m.mountWalletsTab(el('.pro-data-view'),{token});}).catch(()=>{if(active()&&tab==='wallets'&&epoch===walletsEpoch)el('.pro-data-view').innerHTML='<p class=\"pro-empty\">Wallets could not load. Switch tabs and try again.</p>';});return;\n"
         "  }\n"),
        (" let assistantPanel=null;\n",
         " let assistantPanel=null,walletsTab=null,walletsEpoch=0,emWorkspace=null,economyTab=null,emOverlays=null;\n"
         " import('./em-wallets.js').then(m=>{if(active())emWorkspace=m.attachWorkspace({root,insights,sideTabs,getToken:()=>token,getChart:()=>chart,openSide:id=>dock.open(id)});}).catch(()=>{});\n"
         " import('./em-economy.js').then(m=>{if(active())emOverlays=m.attachEconomyOverlays({getToken:()=>token,getChart:()=>chart});}).catch(()=>{});\n"),
        (" return ()=>{disposed=true;", " return ()=>{disposed=true;emWorkspace?.destroy();walletsTab?.destroy();economyTab?.destroy();emOverlays?.destroy();"),
        ("catalogStream=subscribeStream('/api/public/stream/markets',", "catalogStream=subscribeStream('/api/public/stream/markets?networks=all',"),
    ],
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('release')
    ap.add_argument('--assets', default=str(HERE / 'em-assets'))
    ap.add_argument('--check', action='store_true')
    a = ap.parse_args()
    inputs = Path(a.release) / '.inputs'
    for d in ('map', 'protocols'):
        if not (Path(a.assets) / d).is_dir():
            sys.exit('missing assets/%s (run build_assets.py)' % d)
    apply(inputs, PATCHES, [(HERE / 'src' / m, 'src/' + m) for m in MODULES], check=a.check)
    if a.check:
        return
    dst = inputs / 'assets' / 'em'
    shutil.rmtree(dst, ignore_errors=True)
    for d in ('map', 'protocols'):
        shutil.copytree(Path(a.assets) / d, dst / d, ignore=shutil.ignore_patterns('*.gz', '*.tmp'))
    print(json.dumps({'modules': MODULES, 'assets': sum(1 for p in dst.rglob('*') if p.is_file())}))


if __name__ == '__main__':
    main()
