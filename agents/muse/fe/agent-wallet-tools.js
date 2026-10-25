import {readWalletSource} from './wallet-source-read.js';
import {portfolioPlan,portfolioMarkup,queuePortfolio} from './wallet-portfolio.js';
import {CHAIN_REGISTRY as _tkChains,isEvmNetwork as _tkEvm,queryChain as _tkScope,queryChains as _tkScopes,stripChain as _tkStrip} from './chain-scope.js';
import {em,esc,short,day,CHAINS,mapRoute,workspaceWalletRoute,walletCard} from './em-api.js';
import {rememberAgentWallet,selectedAgentWallet,walletToolSelection,walletMarket,walletAddress,walletChain} from './agent-wallet-context.js';
import {mentionedMarkets} from './analysis-market-selection.js';

// Product tool routing stays deterministic. The interpreter never receives host
// access, arbitrary HTTP destinations, customer credentials or signing tools.
const KEY='ticking-agent-wallet-results-v1';let memory=[];
const TX=/^0x[0-9a-f]{64}$/i;
const kinds={liquid:'Held tokens',collateral:'Collateral',supplied:'Supplied',debt:'Borrowed',lp:'Liquidity'};
const quantity=n=>Number.isFinite(n)?n.toLocaleString('en-US',{maximumSignificantDigits:7}):'Not recorded';
export function walletToolIntent(q,ctx={}){
 if(!/\b0x[0-9a-fA-F]{40}\b/.test(q)&&/\b(?:what (?:can you|tools|data)|available (?:tools|features)|capabilities|supported (?:tools|chains|networks)|which (?:chains|networks))\b|무슨\s*(?:기능|데이터)|뭘\s*(?:할|쓸)|어떤\s*(?:기능|데이터).*쓸|사용\s*가능한\s*기능|(?:어느|어떤|지원).*체인|체인.*(?:지원|데이터.*(?:볼|쓸|사용))/i.test(q))return 'capabilities';
 if((/bitcoin|비트코인|\bnative btc\b/i.test(q)||ctx.wallet&&!walletChain(ctx.wallet.chain))&&!/\b0x[0-9a-fA-F]{40}\b/.test(q)&&!_tkScopes(q).some(walletChain))return null; // Native address evidence keeps its own workflow.
 const explicit=/\b0x[0-9a-fA-F]{40}\b/.test(q),prior=ctx.wallet||selectedAgentWallet();
 const selectedReference=/\b(?:this|that|selected) wallet(?:'s)?\b|\bits\b|(?:이|그|선택한)\s*지갑/i.test(q);
 // Muse Flow FIRST — asset Flow needs no wallet; search asks like "who accumulated ARB" must not early-null
 const flowAsk=/\b(?:wallet\s*flow|asset\s*flow|fund\s*flow|net\s*(?:in|out)flow|inflow|outflow|accumulat\w*|whale\s*flow|money\s*flow)\b|플로우|유입|유출|축적|고래\s*흐름|(?:누가|who).*(?:쌓|accumul|hold)|(?:어디로|where).*(?:갔|went|sent)|(?:show|open).*(?:flow|유입)/i.test(q);
 if(/\bcross[\-\s]?chain|across\s+chains|multi[\-\s]?chain|all\s+chains|크로스\s*체인|멀티\s*체인|전\s*체인/i.test(q))return 'crosschain-flow';
 if(flowAsk){
  if(/\b(?:ARB|WETH|PENDLE|GMX|USDC|USDT|WBTC|DAI|OP|LINK|UNI)\b|markets?\s*asset|토큰\s*플로우/i.test(q)&&!explicit&&!prior&&!selectedReference&&!/\bvitalik|foundation|binance|기관|지갑\b/i.test(q))return 'asset-flow';
  if(explicit||prior||selectedReference||/\b0x[0-9a-fA-F]{40}\b|vitalik|foundation|binance|기관|지갑/i.test(q))return 'flow';
  if(/\b(?:ARB|WETH|PENDLE|GMX|USDC|USDT|WBTC|DAI|OP)\b/i.test(q))return 'asset-flow';
  return 'flow';
 }
 if(!explicit&&!prior)return null;
 if(!explicit&&!selectedReference&&/\b(?:find|discover|search|which|who|whales?|largest|biggest|top)\b|찾|고래|누가|어느|어떤\s*지갑|대규모\s*지갑/i.test(q))return null;
 if(/\b(?:investigat\w*|funding|funder|trace|counterpart\w*)\b|추적|자금.*출처|공통.*자금|같이.*움직/i.test(q))return null; // bounded investigation workflow
 const walletWords=/\b(?:wallet|address|its|this|that|portfolio|holdings|balances?)\b|지갑|이것|그것|포트폴리오|보유|잔고/i.test(q);
 if(explicit&&!walletWords&&/\b(?:chart|token|candles?|price|terminal)\b|차트|토큰|가격/i.test(q))return null;
 if(/charts|multi.?chart|차트.*(?:여러|[248]|두|네|여덟)|(?:보유|자산|포트폴리오).*차트/i.test(q)&&(/holdings|portfolio|assets?|보유|포트폴리오|자산/i.test(q)||selectedReference))return 'portfolio';
 if(/\b(?:on (?:the |this )?chart|plot (?:its|the) trades|show (?:its|the) trades on)\b|차트에|그래프에/i.test(q)&&walletWords)return 'chart';
 if(/\b(?:map|connections|money routes)\b|매핑|지도|맵에|자금\s*경로/i.test(q)&&walletWords)return 'map';
 if(walletWords&&/\b(?:holdings|portfolio|balances?|holds?|positions?|assets?)\b|보유|잔고|포트폴리오|포지션/i.test(q))return 'holdings';
 if(walletWords&&/\b(?:trades?|bought|sold|buys?|sells?|activity|when)\b|매수|매도|거래|샀|팔았|언제|활동/i.test(q))return 'trades';
 if(explicit&&(walletWords||/^\s*0x[0-9a-fA-F]{40}\s*$/.test(q)))return 'holdings';
 return null;
}
function validResult(r){
 if(r?.version!==1||!/^aw-[0-9a-f-]{36}$/.test(r.id)||!walletChain(r.chain)||!walletAddress(r.address)||!['holdings','trades','flow'].includes(r.kind)||!Number.isFinite(r.checkedAt)||!Array.isArray(r.items)||r.items.length>24||r.market&&!walletMarket(r.market,r.chain)||JSON.stringify(r).length>=120000)return false;
 if(r.kind==='flow')return !!(r.flow&&typeof r.flow==='object'&&(Array.isArray(r.flow.series)||r.flow.primary));
 if(r.kind==='holdings'&&!(r.card?.address===r.address&&r.card?.chain===r.chain&&r.card?.market===r.market&&Array.isArray(r.card.positions)&&Array.isArray(r.card.flags)&&r.card.sheet&&typeof r.card.sheet==='object'))return false;
 return r.items.every(x=>x&&typeof x==='object'&&(x.amount===null||Number.isFinite(x.amount))&&(r.kind==='holdings'?typeof x.asset==='string'&&typeof x.kind==='string'&&typeof x.protocol==='string'&&(x.usd===null||Number.isFinite(x.usd)):Number.isFinite(x.time)&&x.time>0&&x.time<=r.checkedAt+60000&&typeof x.action==='string'&&(x.tx===null||TX.test(x.tx))&&(x.price===null||Number.isFinite(x.price)&&x.price>0)));
}
function results(){try{const r=JSON.parse(sessionStorage.getItem(KEY)||'[]');if(Array.isArray(r)&&r.length<=3&&JSON.stringify(r).length<150000)memory=r.filter(validResult);}catch{}return memory;}
function saveResult(r){if(!validResult(r))throw Error('The wallet tool returned an invalid result.');memory=[...results().filter(x=>x.id!==r.id),r].slice(-3);try{sessionStorage.setItem(KEY,JSON.stringify(memory));}catch{}return r.id;}
export function clearWalletToolResults(){memory=[];try{sessionStorage.removeItem(KEY);}catch{}}
export function walletToolResult(ref){return results().find(r=>r.id===ref)||null;}
export async function capabilityReply(ctx,query=''){
 const coverage=await em('wallet-coverage',{ttl:30000});if(coverage.schema!=='ticking.wallet-coverage.v1'||!Array.isArray(coverage.chains))throw Error('The connected-source registry is unavailable.');
 const named=_tkScopes(query),chainSummary=coverage.chains.filter(c=>!named.length||named.includes(c.id)).map(c=>'**'+c.name+'** — '+c.reviewedCharts+' reviewed charts; '+(c.hasData?(c.configured.econ?'indexed wallet records':'market records; wallet balances not connected'):'source not active')+(c.hasData&&c.configured.graph?'; graph paths':'')).join('\n- ');
 const chart=!!ctx.chart?.options.editable,evm=walletChain(ctx.token?.network),wallet=ctx.wallet||selectedAgentWallet();
 return {source:'Ticking · connected tools',context:ctx.token?.symbol||'Workspace',text:'## Your connected workspace\n\n'+
 '**Charts and analysis**\n'+(chart?'- Edit the selected chart: intervals, indicators, drawings, Fibonacci and editable position plans.\n- Analyze retained source bars, compare scenarios and explain their evidence.':'- Read other markets and compare source-backed analyses. Open a chart below to edit its drawings or position plan.')+'\n\n'+
 '**Connected chain sources**\n- '+chainSummary+'\n\n**Wallet investigations**\n- Read the selected exact chain’s indexed balances, lending positions, counterparties and decoded activity.\n- Open up to eight distinct recorded asset charts from a wallet, with debt and collateral shown separately.\n- Follow funding leads and expand the investigation graph with transaction links.\n- Show an exact wallet’s recorded trades on its matching market chart.\n- Prepare a wallet-group sell alert for review in the map.\n\n'+
 '**Shared context**\n'+(wallet?'- Selected wallet: '+short(wallet.address)+' · '+CHAINS[wallet.chain]?.name+'.':'- Provide a public address and network, or ask a Flow question about a known wallet/asset.')+'\n'+(ctx.workspace?'- Chart commands target the active cell. Select another cell or say “Select chart 2”.':'- Chart and wallet selections follow this tab across the map and workspace.')+'\n\n'+
 '**Data limits**\n'+(evm?'Wallet queries use this exact token contract, never a same-named token. ':'')+'A chart does not guarantee wallet discovery. Native BTC movements are not purchases or sales; Solana chart executions currently lack a connected trader-address feed. Indexed activity is partial; shared routes do not prove common ownership. Entry rules still apply when wallet context is present.',links:[{title:'Agent tools',route:'#/docs/agent-tools'},...(ctx.token?[{title:'Open '+ctx.token.symbol+' workspace',route:'#/console/workspace/'+encodeURIComponent(ctx.token.id)}]:[])]};
}
export async function runWalletTool(q,ctx,{signal,onProgress}={}){
 let kind=walletToolIntent(q,ctx);if(!kind)return null;if(kind==='capabilities')return capabilityReply(ctx,q);
 // Multi-chain Markets mints via flow-agent-catalog (not Arb-only hardcode)
 let _flowCat=null,_flowCatAt=0;
 async function loadFlowCatalog(signal){if(_flowCat&&Date.now()-_flowCatAt<60000)return _flowCat;const d=await em('flow-agent-catalog',{signal,ttl:60000});if(d?.schema!=='ticking.flow-agent-catalog.v1')throw Error('flow-agent-catalog unavailable');_flowCat=d;_flowCatAt=Date.now();return d;}
 if(kind==='crosschain-flow'){
  onProgress?.('Reading cross-chain Flow…');
  const selection=walletToolSelection(q,ctx);
  const eid=(q.match(/\b(uniswap_treasury|arbitrum_foundation|okx\w*|vitalik_buterin)\b/i)||[])[1];
  let path;
  if(eid) path='crosschain-flow/entity/'+encodeURIComponent(eid.toLowerCase());
  else if(selection?.address) path='crosschain-flow/'+encodeURIComponent(selection.address)+'?'+new URLSearchParams({interval:'1h',limit:'24',...(selection.chain?{chain:selection.chain}:{})});
  else return {text:'Name an entity or wallet for cross-chain Flow (e.g. Uniswap Treasury, or a mapped address).',source:'Economic Machine · Cross-chain Flow'};
  let data;try{data=await em(path,{signal});}catch(e){return {text:'Cross-chain Flow unavailable ('+String(e.message||e).slice(0,120)+').',source:'Economic Machine · Cross-chain Flow'};}
  const sum=data.flowSummary||{};const money=n=>'$'+Intl.NumberFormat('en',{notation:'compact',maximumFractionDigits:2}).format(Number(n)||0);
  const label=(data.entity?.name||'Cross-chain')+' Flow';
  const r={version:1,id:'aw-'+crypto.randomUUID(),kind:'flow',flowKind:'crosschain',address:data.address,chain:'multi',market:null,items:[],flow:data,checkedAt:Date.now(),asOf:null};
  memory=[...results().filter(x=>x.id!==r.id),r].slice(-3);try{sessionStorage.setItem(KEY,JSON.stringify(memory));}catch{}
  return {text:'## '+label+'\n\nChains: '+(data.chains||[]).join(', ')+' · legs '+(data.okLegs||0)+'/'+(data.legCount||0)+'\n\nIn '+money(sum.netInUsd)+' · Out '+money(sum.netOutUsd)+' · Holdings '+money(data.totalUsd)+'.\n\nRolled up from real per-chain wallet Flow — not synthetic bridge volume.',source:'Economic Machine · Cross-chain Flow',context:'multi',walletToolRef:r.id};
 }
 if(kind==='asset-flow'){
  let achain,amint,sym;
  const tokId=ctx.token?.id||'';
  if(/^[a-z0-9_]+:0x[0-9a-fA-F]{40}$/i.test(tokId)){const p=tokId.split(':');achain=p[0].toLowerCase();amint=p[1].toLowerCase();sym=ctx.token?.symbol;}
  else {
    sym=(q.match(/\b(ARB|WETH|PENDLE|GMX|USDC|USDT|WBTC|DAI|OP|LINK|UNI)\b/i)||[])[1]?.toUpperCase();
    try{
      const cat=await loadFlowCatalog(signal);
      const named=_tkScopes(q);
      const hits=[];
      for(const [ch,rows] of Object.entries(cat.markets?.majors||{})){
        for(const r of rows||[]){if(sym&&r.symbol===sym&&r.assetFlow)hits.push(r);}
      }
      const pref=named.length?hits.filter(h=>named.includes(h.chain)):hits;
      const use=pref.length===1?pref:(hits.length===1?hits:null);
      if(!use){
        if(hits.length>1)return {text:'**'+sym+'** exists on multiple chains with Asset Flow. Pick one: '+hits.map(h=>h.chain).join(', ')+' (or paste the contract).',source:'Economic Machine · Asset Flow'};
        return {text:'Name a Markets asset with local transfer index (see flow-agent-catalog), or paste chain + contract. Do not assume Arbitrum.',source:'Economic Machine · Asset Flow'};
      }
      achain=use[0].chain;amint=use[0].mint;
    }catch(e){
      // Fallback Arb majors only if catalog down — still labeled
      const ASSET_MINTS={ARB:'arbitrum:0x912ce59144191c1204e64559fe8253a0e49e6548',WETH:'arbitrum:0x82af49447d8a07e3bd95bd0d56f35241523fbab1',PENDLE:'arbitrum:0x0c880f6761f1af8d9aa9c466984b80dab9a8c9e8',GMX:'arbitrum:0xfc5a1a6eb076a2c7ad06ed22c90d7e710e35ad0a'};
      const mintMarket=ASSET_MINTS[sym];
      if(!mintMarket)return {text:'Asset catalog unavailable and no fallback mint for that symbol.',source:'Economic Machine · Asset Flow'};
      [achain,amint]=mintMarket.split(':');
    }
  }
  const mintMarket=achain+':'+amint;
  const selection={address:amint,chain:achain,market:mintMarket};
  onProgress?.('Reading Markets asset Flow…');
  const qs=new URLSearchParams({chain:achain,interval:'1h',limit:'96'});
  let data;try{data=await em('asset-flow/'+encodeURIComponent(amint)+'?'+qs,{signal});}catch(e){return {text:'Asset Flow unavailable ('+String(e.message||e).slice(0,120)+').',source:'Economic Machine · Asset Flow'};}
  if(signal?.aborted)throw new DOMException('Aborted','AbortError');
  const series=data.series||data.primary?.series||[];let netIn=0,netOut=0;for(const b of series){netIn+=Number(b.netInUsd)||0;netOut+=Number(b.netOutUsd)||0;}
  const label=(data.asset?.symbol||sym||'Asset')+' Flow';
  const r={version:1,id:'aw-'+crypto.randomUUID(),kind:'flow',flowKind:'asset',address:amint,chain:achain,market:mintMarket,items:[],flow:{schema:data.schema,address:data.address,mint:data.mint||amint,chain:achain,interval:data.interval||'1h',series,primary:data.primary,mapped:[],groups:data.groups,totalUsd:data.totalUsd,flowSummary:data.flowSummary||{netInUsd:netIn,netOutUsd:netOut,netDeltaUsd:netIn-netOut},asset:data.asset,framingNote:data.framingNote||'Green inflow / red outflow — Markets asset Flow, not token price.'},checkedAt:Date.now(),asOf:null};
  memory=[...results().filter(x=>x.id!==r.id),r].slice(-3);try{sessionStorage.setItem(KEY,JSON.stringify(memory));}catch{}
  const money=n=>'$'+Intl.NumberFormat('en',{notation:'compact',maximumFractionDigits:2}).format(Number(n)||0);
  return {text:'## '+label+'\n\n**'+(data.asset?.symbol||sym)+' · '+(CHAINS[achain]?.name||achain)+'**\n\nIn '+money(netIn)+' · Out '+money(netOut)+' · Net '+(netIn-netOut>=0?'+':'−')+money(Math.abs(netIn-netOut))+'.\n\nAsk who moved this asset, or open a wallet Flow for a holder.',source:'Economic Machine · Asset Flow',context:CHAINS[achain]?.name||achain,walletToolRef:r.id,links:[{title:'Whale Tracker',route:'#/tracker'},{title:'Markets',route:'#/markets'}]};
 }
 const selection=walletToolSelection(q,ctx);if(!selection)return {text:'Choose a public wallet address and its exact network from the registered chains. The same address on another chain has separate evidence. Your chart is unchanged.',source:'Ticking · wallet selection'};
 if(kind==='trades'||kind==='chart'){
  const subject=_tkStrip(q),named=mentionedMarkets(subject,ctx.markets?.length?ctx.markets:undefined,ctx.token?.id);
  if(named.length>1)return {text:'Choose one exact token and network for this wallet’s actions. Your current chart is unchanged.',source:'Ticking · exact wallet market'};
  if(named.length===1){const target=walletMarket(named[0],selection.chain);if(!target)return {text:'That instrument is not an exact token market on the selected wallet’s network. Choose its matching token contract and network; no other asset’s actions were substituted.',source:'Ticking · exact wallet market'};selection.market=target;}
 }
 const {address,chain,market}=selection;
 if(kind==='map'){
  // Map expand retired — related wallets belong on Flow as series. Fall through to flow.
  kind='flow';
 }
 if(kind==='chart'){
  if(!market)return {text:'Choose the exact token chart on this registered chain for these trade markers. A wallet can hold several assets; no token was inferred from its name.',source:'Ticking · exact chart selection'};
  rememberAgentWallet(selection);
  if(ctx.token?.id===market&&ctx.chart&&ctx.showWallet)await ctx.showWallet(address,chain);
  return {text:ctx.token?.id===market&&ctx.chart?'Requested recorded wallet actions on the selected chart. Markers require returned transaction times and prices; an empty sample is not proof of no trading.':'Open the matching chart below to inspect this wallet’s recorded actions. Your current view is unchanged.',source:'Economic Machine · wallet chart',context:CHAINS[chain].name,links:[{title:'Open wallet on exact chart',route:workspaceWalletRoute(market,address)},{title:'Wallet connections',route:mapRoute(chain,address,market)}]};
 }
 if(kind==='trades'&&!market)return {text:'Which exact token should I inspect for this wallet? Open a matching token chart or return to the chart you investigated. Holdings and transfers alone do not establish a purchase or sale.',source:'Ticking · exact market selection'};
 if(kind==='flow'||kind==='asset-flow'){
  const isAsset=kind==='asset-flow';
  onProgress?.(isAsset?'Reading Markets asset Flow…':'Reading wallet Flow…');
  rememberAgentWallet(selection);
  const q=new URLSearchParams({chain,interval:'1h',limit:'96',...(isAsset?{}:{mapped:'1'})});
  const path=isAsset
    ?('asset-flow/'+encodeURIComponent(selection.market?.split(':')[1]||address)+'?'+q)
    :('wallet-flow/'+encodeURIComponent(address)+'?'+q);
  let data;
  try{
    data=await em(path,{signal});
  }catch(e){
    return {text:'Flow is unavailable right now ('+String(e.message||e).slice(0,120)+'). Try again, or open Investigate for this address.',source:'Economic Machine · Flow',context:CHAINS[chain]?.name||chain,links:[{title:'Open wallet Flow',route:'#/investigate/'+address+(chain?'?kind=wallet&chain='+chain:'')}]};
  }
  if(signal?.aborted)throw new DOMException('Aborted','AbortError');
  const schema=String(data?.schema||'');
  if(!schema.includes('flow'))return {text:'Flow response was not recognized. No chart was attached.',source:'Economic Machine · Flow'};
  const series=data.series||data.primary?.series||[];
  let netIn=0,netOut=0;for(const b of series){netIn+=Number(b.netInUsd)||0;netOut+=Number(b.netOutUsd)||0;}
  const label=isAsset?(data.asset?.symbol||'Asset')+' Flow':(data.entityName||data.primary?.label||short(address))+' Flow';
  const r={version:1,id:'aw-'+crypto.randomUUID(),kind:'flow',flowKind:isAsset?'asset':'wallet',address:isAsset?(data.mint||data.address||address):address,chain:data.chain||chain,market:market||null,items:[],flow:{schema:data.schema,address:data.address,mint:data.mint,chain:data.chain||chain,interval:data.interval||'1h',series,primary:data.primary,mapped:(data.mapped||[]).slice(0,6),groups:data.groups,totalUsd:data.totalUsd,flowSummary:data.flowSummary||{netInUsd:netIn,netOutUsd:netOut,netDeltaUsd:netIn-netOut},asset:data.asset,aggregateMode:data.aggregateMode,entityName:data.entityName,framingNote:data.framingNote||'Green inflow / red outflow / holdings line — not token price.'},checkedAt:Date.now(),asOf:null};
  // validResult requires holdings/trades — save via relaxed path
  memory=[...results().filter(x=>x.id!==r.id),r].slice(-3);try{sessionStorage.setItem(KEY,JSON.stringify(memory));}catch{}
  const money=n=>'$'+Intl.NumberFormat('en',{notation:'compact',maximumFractionDigits:2}).format(Number(n)||0);
  return {text:'## '+label+'\n\n**'+(isAsset?(data.asset?.symbol||'Asset'):short(address))+' · '+(CHAINS[r.chain]?.name||r.chain)+'**\n\nIn '+money(netIn)+' · Out '+money(netOut)+' · Net '+(netIn-netOut>=0?'+':'−')+money(Math.abs(netIn-netOut))+(Number.isFinite(Number(data.totalUsd))?' · Holdings '+money(data.totalUsd):'')+'.\n\nFlow charts show on-chain USD movement over time — not a price candle chart. Related wallets appear as extra series on the same axis when mapped.',source:'Economic Machine · Flow',context:CHAINS[r.chain]?.name||r.chain,walletToolRef:r.id,links:[{title:'Open full Flow',route:'#/investigate/'+encodeURIComponent(r.address)+'?kind=wallet&chain='+encodeURIComponent(r.chain)},{title:'Whale Tracker',route:'#/tracker'}]};
 }
  onProgress?.(kind==='holdings'?'Reading the selected wallet’s indexed balance sheet…':'Reading exact-market wallet actions…');
 const beforeWallet=selectedAgentWallet(),walletKey=v=>v?`${v.chain}:${v.address}:${v.market||''}`:'';
 const data=kind==='holdings'||kind==='portfolio'?await readWalletSource('wallet',{address,chain},{signal,onProgress}):await em('tools/trades?'+new URLSearchParams({address,market}),{signal});
 if(kind==='portfolio'){
  const requested=/\b8\b|여덟/.test(q)?8:/\b2\b|두\s*개/.test(q)?2:4,plan=portfolioPlan(data,requested);rememberAgentWallet(selection);
  if(!plan.assets.length)return {text:'No unambiguous chartable asset was returned for this wallet. '+plan.unresolved.map(p=>p.asset+': '+p.reason.replaceAll('_',' ')).join('; '),source:'Ticking · portfolio source',context:CHAINS[chain].name};
  const ids=plan.assets.map(a=>a.market);if(ctx.workspace?.openPortfolio)await ctx.workspace.openPortfolio(plan);else{queuePortfolio(plan);location.hash='#/console/workspace/'+encodeURIComponent(ids[0]);}
  return {text:'Opening '+ids.length+' distinct recorded asset charts. Borrowing, collateral and held amounts stay separate. '+(plan.unresolved.length?plan.unresolved.length+' source rows were excluded because their identity or quality is unresolved. ':'')+'These are public-address exposures, not confirmed entry positions or a person’s complete portfolio.',source:'Economic Machine · holdings charts',context:CHAINS[chain].name,links:[{title:'Open holdings workspace',route:workspaceWalletRoute(ids[0],address)},{title:'Wallet connections',route:mapRoute(chain,address,ids[0])}]};
 }
 if(signal?.aborted)throw new DOMException('Aborted','AbortError');
 const afterWallet=selectedAgentWallet(),sameTarget=afterWallet?.address===address&&afterWallet.chain===chain&&(!market||!afterWallet.market||afterWallet.market===market);
 if(walletKey(beforeWallet)!==walletKey(afterWallet)&&!sameTarget)throw Error('The selected wallet changed while this read was running. Ask again for the current wallet.');
 if(kind==='holdings'&&(data.chain!==chain||walletAddress(data.address)!==address))throw Error('Wallet identity mismatch. No result was attached.');
 if(kind==='trades'&&((data.chain&&data.chain!==chain)||(data.market&&data.market!==market)||(data.address&&walletAddress(data.address)!==address)))throw Error('Wallet trade identity mismatch. No result was attached.');
 const items=kind==='holdings'?(data.positions||[]).slice(0,24).map(p=>({asset:String(p.asset||'Unknown asset').slice(0,100),kind:String(p.kind||'unknown').slice(0,40),protocol:String(p.protocol||'').slice(0,60),amount:Number.isFinite(p.amount)?p.amount:null,usd:Number.isFinite(p.usd)?p.usd:null,chain,tokenAddress:p.tokenAddress||null,market:p.market||null,identityBasis:p.identityBasis||null,identityReason:p.identityReason||null,held:!!p.held,flag:p.flag||null})):
  (data.items||[]).slice(0,24).filter(t=>Number.isFinite(t.time)).map(t=>({time:t.time,price:Number.isFinite(t.price)&&t.price>0?t.price:null,tx:TX.test(t.tx||t.tx_hash||'')?(t.tx||t.tx_hash).toLowerCase():null,action:t.side==='B'?'buy':t.side==='A'?'sell':String(t.action||'swap').slice(0,40),amount:Number.isFinite(t.amount)?t.amount:Number.isFinite(t.base_volume)?t.base_volume:null}));
 const card=kind==='holdings'?{kind:'wallet',address,chain,market,marketSymbol:ctx.token?.id===market?ctx.token.symbol:'',category:String(data.category||'unknown').slice(0,40),entity:String(data.entity||'').slice(0,100),asOf:Number.isFinite(data.asOf)?data.asOf:typeof data.asOf==='string'&&Number.isFinite(Date.parse(data.asOf))?Date.parse(data.asOf)/1000:null,sheet:{...Object.fromEntries(Object.entries(data.sheet||{}).filter(([k,v])=>['liquid','supplied','collateral','lp','debt','net_worth'].includes(k)&&Number.isFinite(v))),held:!!data.sheet?.held},positionRows:data.positionRows,positionsTruncated:data.positionsTruncated,positions:items,flags:(data.flags||[]).filter(s=>typeof s==='string').slice(0,12)}:null;
 const r={version:1,id:'aw-'+crypto.randomUUID(),kind,address,chain,market,items,card,checkedAt:Date.now(),asOf:card?.asOf||null};
 rememberAgentWallet(selection);const ref=saveResult(r);
 return {text:'## '+(kind==='holdings'?'Wallet balance sheet':'Recorded wallet actions')+'\n\n**'+short(address)+' · '+CHAINS[chain].name+'**\n\n'+(kind==='holdings'?(items.length?items.length+' returned position rows. Held assets, collateral and borrowed amounts are shown separately; these are not entry positions or an audited portfolio.':'No position rows were returned in this indexed snapshot. This does not establish a zero balance.'):(items.length?items.length+' returned actions for **'+market+'**. Only recorded swap directions are labeled bought or sold.':'No matching actions were returned in this exact-market sample. This does not prove the wallet never traded.'))+'\n\nAsk to investigate its funding routes, show it on the chart, or open its map below.',source:'Economic Machine · wallet tools',context:CHAINS[chain].name,walletToolRef:ref,links:[...(market?[{title:'Open wallet on exact chart',route:workspaceWalletRoute(market,address)}]:[]),{title:'Wallet connections',route:mapRoute(chain,address,market||undefined)}]};
}
export function walletToolMarkup(ref){
 const r=walletToolResult(ref);if(!r)return '';
 if(r.kind==='flow'){
  const f=r.flow||{},kind=r.flowKind||'wallet',label=(kind==='asset'?(f.asset?.symbol||'Asset'):(f.entityName||short(r.address)))+' Flow';
  const sum=f.flowSummary||{};const money=n=>'$'+Intl.NumberFormat('en',{notation:'compact',maximumFractionDigits:2}).format(Number(n)||0);
  let payload='';try{payload=JSON.stringify({kind,address:r.address,chain:r.chain,label,data:f}).replace(/'/g,'&#39;');}catch{payload='';}
  return `<section class="wf-answer" data-wf-answer data-wf-kind="${kind}" data-wf-address="${r.address}" data-wf-chain="${r.chain}" data-wf-label="${label.replace(/"/g,'&quot;')}" data-wf-payload='${payload}'><div class="wf-answer-sum"><b>${label}</b><span class="up">In ${money(sum.netInUsd)}</span><span class="down">Out ${money(sum.netOutUsd)}</span><span>Net ${money(sum.netDeltaUsd??((sum.netInUsd||0)-(sum.netOutUsd||0)))}</span></div><p class="wf-note">Loading Flow chart…</p></section><p class="assistant-footnote">Flow = USD inflow / outflow / holdings over time — not a price chart. Related wallets are series on this axis.</p>`;
 }
 const explorer=CHAINS[r.chain].explorer;
 const content=r.kind==='holdings'?walletCard({...r.card,positions:[]},{compact:false})+`<section class="em-card"><b>Returned position quantities</b>${r.items.map(p=>`<div><strong>${esc(p.asset)}</strong> <small>${esc(kinds[p.kind]||p.kind)} · ${esc(p.protocol)}</small><div>${esc(quantity(p.amount))} units${p.usd!==null?' · '+esc(quantity(p.usd))+' USD snapshot':''}</div></div>`).join('')}</section>`:`<section class="em-card"><b>Indexed actions · ${esc(short(r.address))}</b><ol>${r.items.map(t=>`<li><strong>${esc(t.action.replaceAll('_',' '))}</strong> · ${esc(new Date(t.time).toISOString().slice(0,16).replace('T',' '))} UTC${t.amount!==null?' · '+esc(quantity(t.amount))+' units':''}${t.price?' · '+esc(quantity(t.price))+' USD reference':''}${t.tx?` <a href="${explorer}/tx/${t.tx}" target="_blank" rel="noopener noreferrer">Transaction ↗</a>`:''}</li>`).join('')}</ol><p>No entry cost, realized profit or whole-wallet coverage is inferred.</p></section>`;
 return content+(r.kind==='holdings'?portfolioMarkup(portfolioPlan(r.card,8))+`<div class="assistant-links">${[2,4,8].map(n=>`<button type="button" class="em-btn" data-wallet-portfolio="${n}">Open top ${n} charts</button>`).join('')}</div>`:'')+`<p class="assistant-footnote">Indexed snapshot${r.asOf?' · '+esc(day(r.asOf)):''} · checked ${new Date(r.checkedAt).toISOString().slice(0,19).replace('T',' ')} UTC. Coverage is partial.</p>`;
}
