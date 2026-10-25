import {contextFor,retainContext} from './investigation-context.js';
// REQUESTED-POINT-CONTRACT: pure parsing and source validation, never a latest fallback.
export function requestedHolderPoint(params){
 const out={};for(const k of ['block','time','from','to'])if(params.has(k)){const text=params.get(k);if(!/^\d+$/.test(text)||!Number.isSafeInteger(Number(text)))throw Error('Invalid requested '+k+'.');out[k]=Number(text);}
 if(out.block!=null&&out.time!=null)throw Error('Choose a block or a time, not both.');
 if((out.block!=null||out.time!=null)&&(out.from!=null||out.to!=null||params.has('window')))throw Error('Choose one exact point or one range, not both.');
 if((out.from!=null)!==(out.to!=null)||(out.from!=null&&out.from>out.to))throw Error('Choose a complete ordered time range.');
 if(params.has('window')){if(!['1h','24h','7d','30d','90d','all'].includes(params.get('window')))throw Error('Unsupported requested window.');out.window=params.get('window');}
 return out;
}
export function selectedHolderResponse(card,market,point){
 if(!card||typeof card!=='object')return {available:false,reason:'No holder response was returned for the requested point.'};
 if(card.market&&card.market.toLowerCase()!==market.toLowerCase())return {available:false,reason:'The holder response belongs to another exact market. No other token was substituted.'};
 if(card.held||card.available===false)return card;
 if(!card.market&&(point.block!=null||point.time!=null))return {...card,available:false,snaps:[],reason:'The source did not prove the exact requested market.'};
 if(point.block==null&&point.time==null)return card;
 // A time lookup must echo the requested time; its chain block may precede that time.
 const selected=(card.snaps||[]).filter(x=>point.block!=null
  ?(x.requestedBlock==null||x.requestedBlock===point.block)&&(x.effectiveBlock??x.block)===point.block
  :x.requestedTime===point.time&&Number.isFinite(x.effectiveTime??x.time)&&(x.effectiveTime??x.time)<=point.time);
 if(selected.length!==1)return {...card,available:false,snaps:[],reason:'The source did not return this exact requested point. No current snapshot was substituted.'};
 return {...card,snaps:selected};
}
export function holderPoints(point,now){
 if(point.block!=null)return [{block:point.block}];
 if(point.time!=null)return [{time:point.time}];
 if(point.from!=null)return [{time:point.from},{time:point.to}];
 const seconds={'1h':3600,'24h':86400,'7d':604800,'30d':2592000,'90d':7776000}[point.window];
 return seconds?[{time:now-seconds},{time:now}]:[];
}
export function walletAttribution(address,results,chain){
 const exact=(results||[]).filter(r=>r.type==='wallet'&&(!chain||r.chain===chain)&&String(r.id||'').split(':').pop().toLowerCase()===address.toLowerCase());
 const ranked=exact.filter(r=>r.entity?.id&&r.label).sort((a,b)=>({official:4,strong:3,structural:2,candidate:1}[b.label.grade]||0)-({official:4,strong:3,structural:2,candidate:1}[a.label.grade]||0));
 for(const r of ranked){const l=r.label,category=String(l.category||'');if(!/exchange|fund|institution|custod|asset_manager|market_maker/.test(category))continue;
  const verified=l.verified===true&&['official','strong'].includes(l.grade);
  return {name:l.text||r.title,entity:r.entity.id,category,verified,grade:l.grade||'unverified',source:l.source||null,url:/^https?:\/\//i.test(l.url||'')?l.url:null,asOf:l.as_of??l.asOf??null,chain:r.chain};
 }
 return null;
}
export function rawUnits(raw,decimals){
 if(typeof raw!=='string'||!/^\d{1,100}$/.test(raw)||!Number.isInteger(decimals)||decimals<0||decimals>100)return null;
 const digits=raw.padStart(decimals+1,'0');if(!decimals)return digits.replace(/^0+(?=\d)/,'');
 return (digits.slice(0,-decimals).replace(/^0+(?=\d)/,'')+'.'+digits.slice(-decimals)).replace(/\.?0+$/,'');
}
export function walletSourceCard(card,address,chain){
 if(!card||card.available===false||card._error)return card;
 if(String(card.address||'').toLowerCase()!==address.toLowerCase()||card.chain!==chain)return {available:false,reason:'The wallet source returned another address or chain. No balances or graph were substituted.'};
 return card;
}
export function walletBalanceRows(cards){
 const rows=[];
 for(const [chain,card] of Object.entries(cards||{}).filter(([,c])=>c&&c.available!==false))for(const p of card?.positions||[]){
  const held=!!(card.held||card.sheet?.held||p.held),market=typeof p.market==='string'&&p.market.startsWith(chain+':')&&/^[a-z][a-z0-9]*:0x[0-9a-f]{40}$/i.test(p.market)?p.market:null;
  const token=typeof p.token==='string'&&/^0x[0-9a-f]{40}$/i.test(p.token)?p.token:market?.split(':')[1];
  rows.push({chain,token,market,symbol:p.symbol||p.asset||'Unknown asset',kind:p.kind||'position',protocol:p.protocol||null,amount:held?null:p.amount??p.balance??null,valueUsd:held?null:p.usd??p.valueUsd??null,held,reason:p.heldReason||card.sheet?.heldReason||null,flag:p.flag||null,amountText:held?null:p.amountText??null,rawAmount:held?null:p.rawAmount??null,decimals:p.decimals??null,asOf:p.asOf??p.as_of??card.asOf??card.as_of??null});
 }
 return rows;
}
// A requested historical chart uses a fixed source-time range; never the live feed.
export function holderChartRange(selection,snaps=[]){
 if(!Object.keys(selection||{}).length)return {selected:false};
 let from,to;
 if(selection.from!=null){from=selection.from;to=selection.to;}
 else if(selection.time!=null){to=selection.time;from=Math.max(0,to-86400);}
 else if(selection.block!=null){const s=snaps.find(s=>s.block===selection.block);to=s?.sourceTime;from=Math.max(0,(to??0)-86400);}
 else if(selection.window&&selection.window!=='all'){const ts=snaps.map(s=>s.requestedTime).filter(Number.isFinite);if(ts.length>=2){from=Math.min(...ts);to=Math.max(...ts);}}
 if(!Number.isSafeInteger(from)||!Number.isSafeInteger(to)||from>=to)return {selected:true,available:false,reason:'Price history cannot prove this requested selection. No latest chart was substituted.'};
 const span=to-from,interval=span<=86400?'5m':span<=7*86400?'1h':'1d';
 return {selected:true,available:true,window:{from,to},interval};
}
// END-REQUESTED-POINT-CONTRACT
import {holderMessage,shareText,supplyNote,hasShare} from './holder-state.js';
import {loadMarketArtwork,marketArtwork} from './explorer-v2/market-artwork.js';
import {tokenImage} from './token-icons.js';
// What a search opens (owner direction 2026-10-20; redesign/investigate.html, built on real reads). Left: the subject and what it
// is linked to. Right: Ask Ticking, always beside it.
//   Wallet  #/investigate/0x…      chain lanes on a black stage: the same address on other chains (same EOA key = proven; never for
//           contract wallets), direct transfer partners (proven), bridge moves (both legs = matched, heuristic = inferred), protocols
//           used (thin lines). Tabs: linked wallets (open one: what it holds, bought vs received), cross-chain, protocols, assets.
//           An asset opens the existing chart with ▲ bought, ● received (cost unknown), ▼ sold on top.
//   Token   #/investigate/<chain>:0x…   who held it at points in time (share of supply, linked groups together), a price chart to pick
//           the moment, holders, linked groups and key trades.
// Reads: wallet-xc, wallet-pnl-xc, ego, tools/wallet, search labels, investigate, cluster/first-entry, entities, account. What the
// backend does not serve yet says "Preparing" (INVESTIGATE-SCREEN-NEEDS.md); no number on this page is made up.
import {api,esc,short,isAddr,usd,num,day,CHAIN,chainChip,loadingHTML,errorHTML,emptyHTML,DISCLAIMER,groupIcon,groupKey,groupName,protocolLogos,logoFor,entitlement,accountApi,gatePop,GATE} from './explorer-common.js';
import {title} from './explorer.js';
import {identityMarkets} from './explorer-common.js';
import {gradeChip,lineIcon,GRADES} from './explorer-investigate-map.js';
import {mountWalletFlowChart} from './wallet-flow-chart.js';
import {readHolderConnections} from './holder-connections.js';
import {watchButton} from './explorer-watch.js';
import {edgePanel} from './explorer-investigate-edge.js';

const PREP='<span class="tkv-prep" title="The backend does not serve this yet">Preparing</span>';
const LIVE=Object.values(CHAIN).filter(c=>c.status==='live').map(c=>c.id);
const BY_ID=Object.fromEntries(Object.values(CHAIN).map(c=>[c.chainId,c.id]));
const EXTRA={xlayer:['X Layer',196,'#000000'],bsc:['BNB Chain',56,'#f0b90b'],avalanche:['Avalanche',43114,'#e84142'],zksync:['zkSync Era',324,'#8c8dfc'],scroll:['Scroll',534352,'#ffeeda'],blast:['Blast',81457,'#fcfc03'],mantle:['Mantle',5000,'#000000'],sonic:['Sonic',146,'#1969ff'],gnosis:['Gnosis',100,'#3e6957'],solana:['Solana',null,'#9945ff']};
const chainOf=(id,cid)=>id||BY_ID[cid]||Object.keys(EXTRA).find(k=>EXTRA[k][1]===cid)||(cid?'chain-'+cid:null);
const cname=c=>CHAIN[c]?.short||EXTRA[c]?.[0]||(String(c||'').startsWith('chain-')?'Chain '+String(c).slice(6):c||'unknown chain');
const ccolor=c=>CHAIN[c]?.color||EXTRA[c]?.[2]||'#64748b';
const held=v=>false; // $250M hide rule removed — show full estimated amounts
const money=(v,why)=>v==null?'—':held(v)?`<span class="tk-v-held" title="${esc(why||'Value held back by source reconciliation')}">held back</span>`:usd(v);
// deterministic nickname for a wallet (DESIGN-SYSTEM §4: no real names for individuals)
const ADJ=['Copper','Slate','Amber','Quiet','Night','Rust','Pale','Blue','Silver','Ivory','Moss','Coral','Ember','Frost','Dusk','Sable'],ANI=['Heron','Kite','Fox','Otter','Lynx','Wren','Moth','Marlin','Hare','Finch','Badger','Crane','Viper','Ibis','Stoat','Teal'];
// No generated names: an address without a proven name (entity, ENS) shows its short form.
export const nick=a=>{const x=String(a||'');return x.length>12?x.slice(0,6)+'…':x;};
const tail=a=>'·'+String(a).slice(-4);
const avatar=(a,s=26)=>`<span class="tkv-ava" style="width:${s}px;height:${s}px;background:conic-gradient(from ${(parseInt(String(a).slice(2,6),16)||0)%360}deg,#06b6d4,#5658e8,#ec4899,#06b6d4)" aria-hidden="true"></span>`;
const tokIcon=(chain,addr,sym,s=20)=>chain&&isAddr(addr)?`<img class="tk-logo" style="width:${s}px;height:${s}px" src="/api/public/em/icon/${esc(chain)}/${esc(addr)}" alt="" loading="lazy" data-tk-mono="${esc(sym||'')}">`:`<span class="tk-mono" style="width:${s}px;height:${s}px">${esc(String(sym||'?').slice(0,2))}</span>`;
const INFRA=/dex_pool|dex_router|token_contract|aggregator|bridge|lending|vault/;
const STOPS=/exchange|cex/;
const HOW={buy:'▲ Bought',recv:'● Received · cost unknown',both:'▲ Bought, ● also received',sell:'▼ Sold',same:'◆ From the same owner'};

export function mount(el,{path,params,signal}){
 injectInvestigateStyles();
 const requestedSelection=requestedHolderPoint(params);
 const subject=decodeURIComponent(path.replace(/^\/investigate\/?/,''))||params.get('q')||'';
 const routeChain=/^([a-z][a-z0-9]{1,15}):0x/i.exec(subject)?.[1];
 const scopedChain=CHAIN[params.get('chain')]?params.get('chain'):params.get('kind')==='wallet'&&CHAIN[routeChain]?routeChain:null;
 const LIVE=scopedChain?[scopedChain]:Object.values(CHAIN).filter(c=>c.status==='live').map(c=>c.id);
 const st={mode:null,view:'chart',tab:null,sel:null,open:null,hide:new Set(),hl:null,chartOf:null,group:false,snap:null,playing:null,data:{},model:null,edge:null,steps:[],creditsUsed:0,chain:scopedChain,requestedSelection,selectedContext:null};
 let chartApi=null,timeline=null,ask=null;
 el.innerHTML=`<div class="tkv">
  <main class="tkv-left"><div class="tkv-scope" data-v-scope aria-label="Scope of this investigation"></div><section class="tk-card tkv-saved" data-v-saved hidden></section><section class="tk-card tkv-subj" data-v-subj>${loadingHTML('Reading the subject…')}</section>
   <section class="tk-card tkv-bundle" data-v-bundle hidden></section>
   <section class="tk-card tkv-canvas"><div class="tkv-bar" data-v-bar></div><div class="tkv-xbar" data-v-xbar hidden></div><div class="tkv-body" data-v-cv>${loadingHTML('Loading Wallet Flow…')}</div><div class="tkv-tl" data-v-tl hidden></div></section>
   <section class="tk-card tkv-lower"><div class="tkv-tabs" data-v-tabs role="tablist"></div><div class="tkv-rows" data-v-rows></div></section>${DISCLAIMER}</main>
  <aside class="tkv-right" data-v-ask aria-label="Ask Ticking"></aside></div>`;
 const $=s=>el.querySelector(s);
 const off=()=>signal.aborted;

 // ---------- subject resolution: a token (listed market) or a wallet
 (async()=>{
  let q=subject.trim();
  if(!q){$('[data-v-subj]').innerHTML=emptyHTML('Search a wallet address or a token to investigate it.');return;}
  if(params.get('kind')==='wallet'&&isAddr(q.includes(':')?q.split(':')[1]:q)){st.mode='wallet';st.address=(q.includes(':')?q.split(':')[1]:q).toLowerCase();}
  else if(/^[a-z][a-z0-9]{1,15}:0x[0-9a-f]{40}$/i.test(q)){st.mode='token';st.market=q.toLowerCase();}
  else if(isAddr(q)){const snap=await identityMarkets().catch(()=>({tokens:[]}));if(off())return;
   let hit=(snap.tokens||[]).filter(t=>(!scopedChain||t.network===scopedChain)&&String(t.id).toLowerCase().endsWith(':'+q.toLowerCase()));
   if(hit.length>1){$('[data-v-subj]').innerHTML='<h2>Choose the exact token network</h2><p>This contract address is listed on multiple chains.</p>'+hit.map(t=>`<a class="tk-btn" href="#/investigate/${encodeURIComponent(t.id).replace(/%3A/gi,':')}">${esc(t.symbol)} · ${esc(CHAIN[t.network]?.name||t.network)}</a>`).join('');$('[data-v-cv]').innerHTML=emptyHTML('Choose a network to open its evidence.');return;}
   if(hit.length){st.mode='token';st.market=hit[0].id.toLowerCase();}else{st.mode='wallet';st.address=q.toLowerCase();}}
  else{location.replace('#/search?q='+encodeURIComponent(q));return;}
  ask=await import('./explorer-investigate-ask.js').then(m=>m.mountAsk($('[data-v-ask]'),{st,signal,highlight,openChart,showSnap:k=>setSnap(k),render:renderAll,onGraph:addStep,onSaved:()=>{if(!st.saved)renderSubject();},onCredits:n=>{st.creditsUsed=Math.max(0,st.creditsUsed+n);renderScope();loadCredits();},onBusy:()=>renderXbar()}));
  if(st.mode==='wallet'&&Object.keys(requestedSelection).length){st.selectedContext=contextFor({kind:'wallet',address:st.address,...(scopedChain?{chain:scopedChain}:{})},requestedSelection);retainContext(st.selectedContext);$('[data-v-subj]').innerHTML=emptyHTML('This wallet Flow cannot apply the selected historical point or window. No current Flow was substituted.');$('[data-v-cv]').innerHTML=emptyHTML('The requested context is retained for Ask. Choose an explicit current Flow view to continue.');ask?.intro();return;}
  // Pixel Analysis removed — Wallet Flow is the default investigate chart (Holdings tab opens token charts).
  if(st.mode==='wallet')await loadWallet();else await loadToken();
  if(params.get('saved'))loadSaved(params.get('saved'));
 })().catch(e=>{if(!off())$('[data-v-subj]').innerHTML=errorHTML(e.message||'This view could not load.');});

 // ======================================================================== wallet
 async function loadWallet(){
  const a=st.address;title('Investigate '+short(a));
  $('[data-v-subj]').innerHTML=subjectSkeleton(a);
  const get=(p,o)=>api(p,{signal,timeout:25000,...o}).catch(e=>(e.name==='AbortError'?null:{_error:e.status===404?'not served here yet':e.message,_status:e.status}));
  const [xc,pnl,lab,logos,...rest]=await Promise.all([(scopedChain?Promise.resolve(null):get('/api/public/em/wallet-xc?'+new URLSearchParams({address:a,limit:'50'}))),(scopedChain?Promise.resolve(null):get('/api/public/em/wallet-pnl-xc?'+new URLSearchParams({address:a,limit:'200'}))),
   get('/api/public/em/search?'+new URLSearchParams({q:a,limit:'6'})),protocolLogos(),...LIVE.map(c=>get('/api/public/em/tools/wallet?'+new URLSearchParams({address:a,chain:c,positions_limit:'100'})))]);
  if(off())return;
  const cards=Object.fromEntries(LIVE.map((c,i)=>[c,rest[i]&&!rest[i]._error?walletSourceCard(rest[i],a,c):null]));
  const egoChains=LIVE.filter(c=>cards[c]&&(cards[c].inGalaxy||cards[c].tx_in||cards[c].tx_out));
  const ok=x=>x&&!x._error&&x.available!==false?x:null;
  const [egos,links,acq,chainsR]=await Promise.all([Promise.all(egoChains.map(c=>get('/api/public/em/ego?'+new URLSearchParams({address:a,chain:c,window:'7d',limit:'14'})))),
   Promise.all(egoChains.map(c=>get('/api/public/em/wallet-links?'+new URLSearchParams({chain:c,address:a})))),Promise.all(egoChains.map(c=>get('/api/public/em/acquisition?'+new URLSearchParams({address:a,chain:c})))),
   (scopedChain?Promise.resolve({chains:[]}):get('/api/public/em/address-chains?'+new URLSearchParams({address:a})))]);
  if(off())return;
  // the other chains the address is on (Base, Ethereum, … once served): their wallet cards carry the balance sheet, held or not
  // (address-chains does not index every chain yet, so every registry chain with data is asked; a card with no sheet, brand or activity adds nothing)
  const more=scopedChain?[]:[...new Set([...(ok(chainsR)?.chains||[]).map(r=>chainOf(r.chain,r.chainId)),...Object.values(CHAIN).filter(x=>x.status!=='history-only').map(x=>x.id)])].filter(c=>c&&!LIVE.includes(c)&&CHAIN[c]);
  const moreCards=await Promise.all(more.map(c=>get('/api/public/em/tools/wallet?'+new URLSearchParams({address:a,chain:c,positions_limit:'100'}),{timeout:45000})));if(off())return;
  more.forEach((c,i)=>{const x=moreCards[i];if(x&&!x._error&&(x.sheet||x.icon||x.tx_in||x.tx_out))cards[c]=walletSourceCard(x,a,c);});
  st.walletCoverage=Object.fromEntries([...LIVE.map((c,i)=>[c,rest[i]]),...more.map((c,i)=>[c,moreCards[i]])]);
  st.data={xc:xc?._error?null:xc,xcError:xc?._error,pnl:pnl?._error?null:pnl,pnlError:pnl?._error,lab,logos,cards,egos:Object.fromEntries(egoChains.map((c,i)=>[c,egos[i]?._error?null:egos[i]])),
   links:Object.fromEntries(egoChains.map((c,i)=>[c,ok(links[i])])),acq:Object.fromEntries(egoChains.map((c,i)=>[c,ok(acq[i])])),addrChains:ok(chainsR)};
  st.model=walletModel(a,st.data);st.selectedContext=contextFor({kind:'wallet',address:a,...(scopedChain?{chain:scopedChain}:{})},st.requestedSelection);retainContext(st.selectedContext);st.tab=st.tab||'assets';
  renderAll();ask?.intro();loadFresh();loadCredits();
 }
 function walletModel(a,{xc,pnl,lab,logos,cards,egos,links:wl={},acq={},addrChains}){
  const chains=new Map(),add=(id,p)=>{if(!id)return;chains.set(id,{id,...(chains.get(id)||{}),...Object.fromEntries(Object.entries(p).filter(([,v])=>v!=null))});};
  for(const [c,card] of Object.entries(cards))if(card&&(card.tx_in||card.tx_out||card.inGalaxy))add(c,{txs:(card.tx_in||0)+(card.tx_out||0),sent:card.tx_out,received:card.tx_in,first:card.first_seen,last:card.last_seen,category:card.category,contract:/contract|pool|router|token|vault|manager|multisig|safe|protocol|bridge|lending|perp|stake|issuer|amm/.test(card.category||'')||null,art:card.icon?{icon:card.icon,iconCredit:card.iconCredit,name:card.entity}:null,basis:card.labelBasis,entity:card.entity});
  // a card's balance sheet (tools/wallet): held back = no amount and the reason; partial = the priced part, said so
  for(const [c,card] of Object.entries(cards)){const sh=card?.sheet;if(!sh)continue;
   add(c,sh.held?{valueHeld:`${sh.heldReason||'being re-reconciled'}${sh.heldRows?` (${sh.heldRows} row${sh.heldRows>1?'s':''})`:''}`,value:null,sheetNote:null}
    :{value:sh.net_worth_usd??sh.net_worth??null,sheetNote:sh.partial?(sh.note||`partial: ${sh.unpricedRows||'some'} positions without a price`):null});}
  for(const r of xc?.chains||[]){const c=chainOf(r.chain,r.chain_id),p=r.presence,h=r.sheet?.held?r.sheet:xc?.net_worth_usd?.held_chains?.[r.chain]?.held?xc.net_worth_usd.held_chains[r.chain]:null;
   add(c,{value:h?null:r.sheet?.net_worth_usd??null,valueHeld:h?(h.heldReason||'re-reconciling'):null,label:r.label,...(p?{txs:(p.tx_sent||0)+(p.tx_received||0),sent:p.tx_sent,received:p.tx_received,first:p.first_ts||null,last:p.last_ts||null,contract:!!p.is_contract}:{})});}
  for(const r of addrChains?.chains||[]){const c=chainOf(r.chain,r.chainId);add(c,{txs:r.txs,sent:r.txSent,received:r.txReceived,first:r.firstSeen,last:r.lastSeen,
   contract:r.kind==='contract',kind:r.kind,sameOwner:r.sameOwner,grade:r.grade,why:r.why,receiveOnly:r.receiveOnly,bridgeLegs:r.bridgeLegs});}
  const legs=xc?.bridges?.legs||[];
  for(const l of legs){add(chainOf(l.chain,l.chain_id),{});const cc=chainOf(l.counterparty?.chain,l.counterparty?.chain_id);if(cc&&l.counterparty?.wallet)add(cc,{});}
  for(const f of [...(xc?.money_went||[]),...(xc?.money_came||[])])add(chainOf(f.chain,f.chain_id),{});
  const observedChainCount=chains.size;
  if(!chains.size)add(scopedChain||LIVE[0],{coverageUnknown:true});
  const list=[...chains.values()],home=[...list].sort((x,y)=>(y.txs||0)-(x.txs||0))[0].id;
  const homeC=chains.get(home),contract=list.some(c=>c.contract===true)||(addrChains?.entity?.contractOn||[]).length>0||xc?.entity?.link==='not_linked'&&/contract|safe|code/.test(xc.entity.basis||'');
  const lanes=[...list.filter(c=>c.id!==home).sort((x,y)=>(x.first||9e9)-(y.first||9e9)),homeC].map(c=>({id:c.id,name:cname(c.id),color:ccolor(c.id)}));
  const nodes=[],links=[],seen=new Set(),node=n=>{if(!seen.has(n.id)){seen.add(n.id);nodes.push(n);}return n.id;};
  const label=(lab?.results||[]).find(r=>r.type==='wallet'&&r.label&&(!scopedChain||r.chain===scopedChain)&&String(r.id||'').split(':').pop().toLowerCase()===a.toLowerCase());
  const selfId=c=>c===home?'self':'self-on:'+c;
  node({id:'self',kind:'self',lane:home,address:a,label:'This wallet',sub:short(a),tip:`<b>This wallet on ${esc(cname(home))}</b>${homeC.txs!=null?num(homeC.txs)+' transactions':''}${homeC.first?' · since '+day(homeC.first):''}`});
  for(const c of list){if(c.id===home)continue;
   node({id:selfId(c.id),kind:'self-on',lane:c.id,address:a,contract:!!c.contract,label:'This wallet on '+cname(c.id),sub:c.txs!=null?`${num(c.txs)} txs${c.first?' · since '+day(c.first):''}`:'activity: preparing',
    tip:`<b>Same address on ${esc(cname(c.id))}</b>${c.sent!=null?`${num(c.sent)} sent · ${num(c.received)} received`:'Transaction count on this chain: preparing'}${c.value!=null?'<br>Balance sheet '+money(c.value):''}`});
   // the same address is the same key only for an EOA; contract wallets (Safes, smart accounts) can differ per chain
   if(c.sameOwner===true||(c.kind==='eoa'&&homeC.kind==='eoa'&&!contract&&!c.contract)){const e=xc?.entity,ev=e&&(e.chains||[]).some(x=>chainOf(x.chain,x.chain_id)===c.id);
    links.push({a:'self',b:selfId(c.id),g:'same',chain:c.id,why:`Same address on ${cname(c.id)}: ${c.why||'an externally owned account, one private key on every chain'}${ev&&e.counter?.length?' · '+e.counter.join('; '):''}${c.receiveOnly||c.sent===0?' (receive only there)':''}`,
     parts:[{kind:'same',grade:'same',chain:c.id,rule:c.why||'An externally owned account: one private key controls this address on every chain',
      facts:[c.sent!=null?`${num(c.sent)} sent · ${num(c.received)} received on ${cname(c.id)}`:null,c.first?`First seen on ${cname(c.id)} ${day(c.first)}`:null,c.kind?`${cname(c.id)}: ${c.kind==='eoa'?'externally owned':c.kind} (${addrChains?.chains?.find(x=>chainOf(x.chain,x.chainId)===c.id)?.kindBasis||'code check'})`:null,...(addrChains?.entity?.evidence||[])].filter(Boolean),
      counter:[...(addrChains?.entity?.counter||[]),...(ev&&e.counter?.length?e.counter:[])],noTx:'No transaction is needed for this link: the same externally owned address is the same private key on every chain.'}]});}}
  // bridges: both legs found = matched, heuristic = inferred; the other side may be this address or another wallet
  const pair={};
  for(const l of legs){const cp=l.counterparty||{},cls=l.match?.class,g=cls==='exact'||cls==='structural'?'matched':cls==='heuristic'?'inferred':'unknown';
   const here=chainOf(l.chain,l.chain_id),there=chainOf(cp.chain,cp.chain_id);if(!g||!there||!cp.wallet)continue;
   const other=cp.wallet.toLowerCase()===a?selfId(there):node({id:`w:${there}:${cp.wallet.toLowerCase()}`,kind:'wallet',lane:there,address:cp.wallet.toLowerCase(),label:nick(cp.wallet)+' '+tail(cp.wallet),sub:short(cp.wallet)});
   const k=[selfId(here),other,l.asset||l.token||l.symbol||'',l.protocol||l.bridge||'',cls||'unknown'].join('|'),p=pair[k]||(pair[k]={id:'bridge:'+k,a:selfId(here),b:other,chain:here,g,n:0,bridges:new Set(),dirs:new Set(),last:0,legs:[],seen:new Set(),counter:new Set()});
   const mv=[l.tx,cp.tx].filter(Boolean).sort().join('|');if(p.seen.has(mv))continue;p.seen.add(mv);p.legs.push(l);(l.match?.counter||[]).forEach(x=>p.counter.add(x));
   p.n++;p.bridges.add(l.protocol||l.bridge);p.dirs.add(l.dir==='out'?`${cname(here)} → ${cname(there)}`:`${cname(there)} → ${cname(here)}`);p.last=Math.max(p.last,l.ts||0);if(g==='matched')p.g='matched';}
  for(const p of Object.values(pair))links.push({id:p.id,a:p.a,b:p.b,g:p.g,chain:p.chain,why:`${p.n} bridge move${p.n>1?'s':''} (${[...p.bridges].join(', ')}), ${[...p.dirs].join(' and ')}${p.g==='matched'?', both legs found':p.g==='inferred'?', matched by amount and time only':', match not verified'} · last ${day(p.last)}`,
   parts:[{kind:'bridge',grade:p.g,chain:p.chain,rule:p.g==='matched'?'Both legs of the move were found: it left one chain and arrived on the other with the same deposit id or structure':p.g==='inferred'?'Matched by amount and time only: no shared deposit id':'No verified bridge match',
    facts:[`${p.n} move${p.n>1?'s':''} through ${[...p.bridges].join(', ')}`,[...p.dirs].join(' and '),`Last move ${day(p.last)}`],legs:p.legs,counter:[...p.counter]}]});
  // direct transfer partners and protocols used, per mapped chain (ego: counterparties since first seen, venues in 7 days)
  const prot=new Map();
  for(const [c,ego] of Object.entries(egos)){if(!ego)continue;const center=selfId(c),byId=Object.fromEntries((ego.nodes||[]).map(n=>[n.id,n]));
   if(c!==home&&!seen.has(center))node({id:center,kind:'self-on',lane:c,address:a,label:'This wallet on '+cname(c)});
   const wallets=[];
   for(const e of ego.edges||[]){const n=byId[e.target];if(!n)continue;
    if(n.kind==='venue'||INFRA.test(n.category||'')){const name=n.name||'Protocol',key=c+':'+name.toLowerCase();const p=prot.get(key)||{id:'p:'+key,chain:c,name,entity:n.entity_id,category:n.category,address:n.address,usd:0,events:0,types:new Set()};
     p.usd+=e.usd||0;p.events+=e.events||0;p.types.add(e.type);prot.set(key,p);continue;}
    if(n.address==='0x0000000000000000000000000000000000000000')continue;
    wallets.push({n,e});}
   wallets.sort((x,y)=>(y.e.usd||0)-(x.e.usd||0)||(y.e.events||0)-(x.e.events||0));
   for(const {n,e} of wallets.slice(0,10)){const ex=STOPS.test(n.category||'');
    const id=node({id:`w:${c}:${n.address}`,kind:ex?'exchange':'wallet',lane:c,address:n.address,category:n.category,stop:ex,big:(e.usd||0)>1e5,
     label:ex?(n.name||'Exchange'):(n.name&&n.name!=='Wallet'&&n.name!=='Unclassified'?n.name:nick(n.address)+' '+tail(n.address)),sub:short(n.address)});
    const dir=e.countOnly?`${e.direction||'both ways'}, ${num(e.events)} transfers`:`sent ${usd(e.fwdUsd)} · received ${usd(e.backUsd)} · ${num(e.events)} transfers`;
    links.push({a:center,b:id,g:'proven',chain:c,usd:e.usd||0,why:`${cname(c)}: ${dir}${ex?' · an exchange: tracing stops here':''}`,
     parts:[{kind:'transfer',grade:'proven',chain:c,rule:'Direct transfers between the two: money moved between them, which is not the same as one owner',
      facts:[e.countOnly?`${e.direction||'Both ways'} · ${num(e.events)} transfers`:`This wallet sent ${usd(e.fwdUsd)} and received ${usd(e.backUsd)}`,`${num(e.events)} transfers since it was first seen`],
      fetch:{chain:c,a,b:n.address},counts:`${num(e.events)} transfers`,counter:ex?['An exchange: many people deposit to it, so it does not link them to each other']:[]}]});}}
  const RANK={judged:4,inferred:3,matched:2,proven:1,used:0},stops={};
  const TYPE_RULE={transfers_both_ways:'Transfers both ways between the two',shared_deposit:'The same exchange deposit address',deposit:'The same exchange deposit address',safe_owner:'An owner of the same Safe',created_contract:'One created the other',creator:'One created the other',operator:'The same operator or contract controls both',shared_funder:'The same funder (counts only with a second, independent signal)',same_block:'Traded in the same block',co_buy:'Bought the same token close together'};
  for(const [c,L] of Object.entries(wl)){if(!L)continue;const center=selfId(c);stops[c]=L.stops||[];
   for(const x of (L.links||[]).filter(x=>x.grade).slice(0,14)){const ex=x.kind==='exchange'||STOPS.test(x.category||'');
    const id=node({id:`w:${c}:${x.address}`,kind:ex?'exchange':'wallet',lane:c,address:x.address,category:x.category||x.kind,stop:ex,label:x.entity||nick(x.address)+' '+tail(x.address),sub:short(x.address)});
    const why=`${cname(c)}: ${(x.evidence||[]).map(e=>e.text||`${String(e.type||'').replace(/_/g,' ')}${e.symbol?' '+e.symbol:''}${e.n?' ×'+e.n:''}`).join('; ')}${x.counter?.length?' · against: '+x.counter.join('; '):''}${x.note?' · '+x.note:''}`;
    const fund=(L.funders||[]).filter(f=>!f.usable&&(f.wallets||[]).includes(x.address)).map(f=>`Shared funder ${short(f.funder)} not used: ${(f.counter||[]).join('; ')||'it funds too many wallets'}`);
    const part={kind:x.grade==='judged'?'owner':'linked',grade:x.grade,chain:c,rule:(x.types||[]).map(t=>TYPE_RULE[t]||String(t).replace(/_/g,' ')).join(' · ')||why,
     facts:(x.evidence||[]).map(e=>e.text||`${num(e.n)} ${String(e.type||'').replace(/_/g,' ')}${e.symbol?' of '+e.symbol:e.asset?' of '+short(e.asset):''}${e.firstTime?` · ${day(e.firstTime)} → ${day(e.lastTime)}`:''}${e.firstBlock!=null?` · blocks ${num(e.firstBlock)}–${num(e.lastBlock)}`:''}`).concat(x.note?[x.note]:[]),
     counter:[...(x.counter||[]),...fund],fetch:{chain:c,a,b:x.address}};
    const old=links.find(l=>(l.a===center&&l.b===id)||(l.b===center&&l.a===id));
    if(old){(old.parts=old.parts||[]).push(part);if(RANK[x.grade]>RANK[old.g]){old.g=x.grade;old.why=why+' · '+old.why;}else old.why+=' · '+why;}else links.push({a:center,b:id,g:x.grade,chain:c,why,parts:[part]});}
   for(const x of L.stops||[]){const n=nodes.find(n=>n.address===x.address);if(n){n.stop=true;n.stopWhy=x.why||x.reason||x.kind||'';n.tip=`<b>${esc(n.label)}</b>Tracing stops: ${esc(n.stopWhy)}`;}}}
  for(const p of [...prot.values()].sort((x,y)=>y.usd-x.usd).slice(0,8)){const id=p.entity?.startsWith('protocol:')?p.entity.slice(9):null,b=id&&logos?.registry?.[id]&&logos.brands?.[logos.registry[id]];
   const brand=String(p.name).split('·')[0].trim().toLowerCase(),bb=Object.values(logos?.brands||{}).find(x=>x.name.toLowerCase()===brand);
   const lg=b?b.file:logoFor(logos,{name:p.name})?.icon||bb?.file||(/token/.test(p.category||'')&&p.address?`/api/public/em/icon/${p.chain}/${p.address}`:`/assets/em/kinds/${/pool|router|dex/i.test(p.name+' '+(p.category||''))?'dex':/lend/i.test(p.name)?'lending':/bridge/i.test(p.name)?'bridge':'other'}.svg`);
   node({id:p.id,kind:'protocol',lane:p.chain,label:p.name,logo:lg,usd:p.usd,events:p.events});
   links.push({a:selfId(p.chain),b:p.id,g:'used',chain:p.chain,why:`${p.name}: ${usd(p.usd)} through it in 7 days · ${num(p.events)} actions`,
    parts:[{kind:'protocol',grade:'used',chain:p.chain,rule:`Sent money through ${p.name}'s contracts`,facts:[`${usd(p.usd)} through it in the last 7 days`,`${num(p.events)} actions: ${[...p.types].filter(Boolean).join(', ')||'—'}`],noTx:'Protocol use is summed over the last 7 days; the protocol page lists its activity.'}]});}
  // assets: what it bought and what it received (cost unknown); a token is a chart when it is a listed market
  const how=Object.fromEntries(Object.entries(acq).flatMap(([c,A])=>(A?.tokens||[]).map(t=>[c+':'+String(t.token).toLowerCase(),t])));
  const assets=(pnl?.rows||[]).map(r=>{const A=how[r.chain+':'+String(r.token).toLowerCase()],same=(A?.topSenders||[]).filter(x=>x.type==='received_same_owner'),recv=(A?.topSenders||[]).filter(x=>x.type==='received');
   return {...r,acq:A,same,recv,how:same.length?'same':r.buys>0&&(r.transfersIn>0||recv.length)?'both':r.buys>0?'buy':(r.transfersIn>0||recv.length)?'recv':r.sells>0?'sell':'buy'};});
  const moves=new Set(legs.map(l=>[l.tx,l.counterparty?.tx].filter(Boolean).sort().join('|'))).size;
  links.forEach(l=>{l.id=l.id||l.a+'|'+l.b;});
  return {address:a,home,homeC,observedChainCount,graphAvailable:Object.values(egos||{}).some(g=>g&&g.available!==false&&Array.isArray(g.nodes)),chains:list,lanes,nodes,links,legs,moves,label,contract,attribution:walletAttribution(a,lab?.results,scopedChain),balanceRows:walletBalanceRows(cards),recent:Object.entries(cards).flatMap(([chain,c])=>(c?.recent||[]).slice(0,5).map(t=>({...t,chain}))).sort((a,b)=>(b.time||0)-(a.time||0)).slice(0,30),entity:xc?.entity||null,assets,prot:[...prot.values()],totals:pnl?.totals||null,coverage:pnl?.coverage||null,stops,basis:xc?.coverage?.chains||{}};
 }
 // a name is shown as the wallet's name when it is proven: verified in the search labels, or the card's label is a registry /
 // published-list / exchange-published one (labelBasis from the bridge), even when the graph-loop label of the same name came from an agent
 // a machine label ('uniswap_v4:pool_manager') reads as the card's name, else in words ('Uniswap V4 · pool manager')
 const wordsOf=t=>String(t).split(':').map((x,i)=>{x=x.replace(/_/g,' ');return i?x:x.replace(/\bv(\d)/gi,'V$1').replace(/^\w/,c=>c.toUpperCase());}).join(' · ');
 const provenName=m=>{const lb=m.label?.label,card=(m.chains||[]).find(c=>(c.basis==='registry'||c.basis==='research')&&c.entity)?.entity||'';
  if(lb?.verified&&lb.text)return /^[a-z0-9_]+:/.test(lb.text)?card||wordsOf(lb.text):lb.text;return card;};
 function subjectName(m){return provenName(m)||nick(m.address);}
 function subjectSkeleton(a){return `${avatar(a,46)}<div class="tkv-who"><h1>${esc(nick(a))} <span class="tk-muted">${tail(a)}</span></h1><div class="tkv-addr tk-addr">${esc(a)}</div><div class="tkv-chips">${loadingHTML('Reading its chains…')}</div></div>`;}
 function renderSubject(){const s=$('[data-v-subj]');
  if(st.mode==='token')return renderTokenSubject(s);
  const m=st.model,a=m.address,lb=m.label?.label,name=subjectName(m),institution=m.attribution;
  const linked=m.nodes.filter(n=>n.kind==='wallet'||n.kind==='exchange').length;
  const art=(m.chains||[]).map(c=>c.art).find(Boolean);   // a proven brand on any chain (registry / published list / an exchange's own wallet)
  s.innerHTML=`${art?`<img class="tkx-art" src="${esc(art.icon)}" width="46" height="46" alt="" title="${esc(art.iconCredit||art.name||'')}">`:avatar(a,46)}<div class="tkv-who"><h1>${esc(name)} <span class="tk-muted">${tail(a)}</span> ${chainChip(m.home,{full:true})}</h1>
   <div class="tkv-addr"><span class="tk-addr">${esc(a)}</span><button type="button" class="tkv-copy" data-v-copy title="Copy the address">Copy</button></div>
   <div class="tkv-chips">${institution?`<span class="tk-label entity" data-wallet-institution="${institution.verified?'verified':'candidate'}">${institution.verified?'기관':'기관 추정'} · ${esc(institution.name)} · ${esc(cname(institution.chain))} · ${esc(institution.category)} · ${esc(institution.grade)}</span><span class="tk-muted">${esc(institution.source||'Source unavailable')} · ${institution.asOf?esc(String(institution.asOf)):'기준 시점 미제공'}${institution.url?` · <a class="tk-link" href="${esc(institution.url)}" target="_blank" rel="noopener">근거</a>`:''}</span>`:''}${m.homeC.category?`<span class="tk-chip">${groupIcon(m.homeC.category,14)}${esc(groupName(m.homeC.category)||m.homeC.category)}</span>`:''}${provenName(m)?`<span class="tk-label entity" title="${esc('Verified · '+(lb?.verified?lb.source||'':(m.chains||[]).find(c=>c.basis==='registry'||c.basis==='research')?.basis||''))}">${esc(provenName(m))}</span>`:lb&&!/eoa|user|unclassified|unknown|contract/i.test(lb.text||'')?`<span class="tk-label" title="${esc('Inferred · '+(lb.source||''))}">${esc(lb.text||lb.category)} · inferred</span>`:''}
    ${m.contract?'<span class="tk-chip warn" title="A contract wallet can be controlled by different owners on each chain">Contract wallet: same address is not the same owner</span>':m.lanes.length>1?`<span class="tk-chip tkv-violet">Indexed on ${m.lanes.length-1} other chain${m.lanes.length>2?'s':''}</span>`:''}</div>
   <div class="tkv-pills">${m.lanes.slice().reverse().map(l=>{const c=m.chains.find(x=>x.id===l.id);return `<button type="button" class="tkv-cpill" data-v-chain="${esc(l.id)}"><i style="background:${esc(l.color)}"></i>${esc(l.name)}<b>${c?.txs!=null?num(c.txs):'—'}</b></button>`;}).join('')}<span class="tk-muted tkv-pnote">transactions per chain</span></div></div>
   <div class="tkv-stats"><div><b>${m.graphAvailable?linked:'—'}</b><span>Returned linked wallets</span></div><div><b>${m.graphAvailable?m.prot.length:'—'}</b><span>Returned protocols</span></div><div><b>${m.observedChainCount||'—'}</b><span>Observed chains</span></div></div>
   <div class="tkv-acts">${watchButton({kind:'wallet',chain:m.home,target:a,label:name})}${keepButtons()}<a class="tk-btn" href="#/address/${esc(a)}?chain=${esc(m.home)}">Full details</a></div>`;
  bindKeep(s);
  s.querySelectorAll('[data-v-chain]').forEach(b=>b.onclick=()=>{st.tab='chains';st.open=b.dataset.vChain;st.sel=b.dataset.vChain===m.home?'self':'self-on:'+b.dataset.vChain;st.hl=null;renderBar();renderCanvas();renderTabs();renderRows();$('[data-v-cv]')?.scrollIntoView({behavior:'smooth',block:'nearest'});});
  s.querySelector('[data-v-copy]').onclick=e=>{navigator.clipboard?.writeText(a);e.target.textContent='Copied';};}
 function renderBar(){const b=$('[data-v-bar]');
  if(st.mode==='token')return renderTokenBar(b);
  const m=st.model;
  // Map graph + Pixel Analysis removed. Lower canvas is Wallet Flow (or token chart when a holding is picked).
  st.view='chart';
  b.innerHTML=`<div class="tkv-seg"><button type="button" data-v-view="chart" class="on">Chart</button></div>
   ${st.chartOf?`<span class="tkv-crumb">${tokIcon(st.chartOf.chain,st.chartOf.token,st.chartOf.symbol,18)}<b>${esc(st.chartOf.symbol)}</b> · trades of <b>${esc(st.chartOf.who===m.address?'this wallet':nick(st.chartOf.who))}</b></span>`
    :`<span class="tkv-crumb">Wallet Flow · open a holding in the <b>Holdings</b> tab for its token chart · ${m.lanes.length} chain${m.lanes.length>1?'s':''} indexed</span>`}
   <div class="tkv-filters"></div>`;}
 function renderCanvas(){const box=$('[data-v-cv]');chartApi?.destroy();chartApi=null;box.innerHTML='';box.className='tkv-body';renderXbar();
  if(st.mode==='token')return renderHolderCanvas(box);
  // Wallet Flow chart (primary + mapped related wallets on one axis). Token-price chart when a holding is picked.
  if(st.chartOf)return renderChartView(box);
  box.classList.add('chart');
  const host=document.createElement('div');host.className='tkv-chart wf-investigate';box.append(host);
  mountWalletFlowChart(host,{address:st.address,chain:st.chain||st.model?.home||'ethereum',interval:'1h',limit:168,label:subjectName(st.model)||'Wallet Flow',signal}).then(api=>{chartApi=api;}).catch(e=>{host.innerHTML=emptyHTML('Wallet Flow unavailable. '+String(e.message||e));});}
 function openEdge(id){st.edge=st.edge===id?null:id;st.sel=null;st.hl=null;renderCanvas();}
 // research on the same map: three starts above the stage; each goes plan → credit consent → run in the Ask pane, and what the run
 // finds is added here, the newest step highlighted, with one step back
 function renderXbar(){const x=$('[data-v-xbar]');x.hidden=true; // map research starts retired with the Map graphif(x.hidden)return;
  const last=st.steps[st.steps.length-1],busy=st.expanding;
  x.innerHTML=`<div class="tkv-xbtns">${[['funding','◀ Find the source of funds'],['next','See where it went next ▶'],['related','Related wallets on Flow']].map(([k,t])=>`<button type="button" data-v-x="${k}"${busy?' disabled':''}>${t}</button>`).join('')}</div>
   <div class="tkv-xstep">${busy?`<span class="tkv-xbusy">${esc(busy)}</span>`:last?`<span>Step ${st.steps.length+1} · ${esc(last.label)} · <b>+${last.nodes.length} wallet${last.nodes.length===1?'':'s'}</b>, +${last.links.length} link${last.links.length===1?'':'s'}</span><button type="button" data-v-back>↶ Back one step</button>`:'<span>Starts show their plan and credits before anything runs</span>'}</div>`;
  x.querySelectorAll('[data-v-x]').forEach(b=>b.onclick=()=>ask?.expand(b.dataset.vX));
  x.querySelector('[data-v-back]')?.addEventListener('click',backStep);}
 // what a run found → nodes and lines on this map. The run's graph (em-account runs/{id}.graph, every graph step merged):
 // nodes {id:"chain:0x…", address, chain, kind, label, category, hop, stop}, edges {a, b (node ids), chain, grade, kind, why, txCount,
 // symbol, amount, first/lastTime, evidence (the edge-evidence path), counter}. A bridge edge crosses chains. Nothing is placed without
 // an address and a chain.
 function addStep({kind,step}){if(st.mode!=='wallet'||!st.model)return 0;const m=st.model,a=m.address,d=step?.data||{},nn=[],nl=[];
  const selfId=c=>c===m.home?'self':'self-on:'+c,known=new Set(m.nodes.map(n=>n.id)),lanes=new Set(m.lanes.map(l=>l.id)),byGid={};
  const split=v=>{const t=String(v||'').toLowerCase().split(':');return t.length>1?[t[0],t[t.length-1]]:[null,t[0]];};
  const idOf=(c,addr)=>addr===a?selfId(c):`w:${c}:${addr}`;
  const addNode=x=>{const [gc,ga]=split(x.id),c=chainOf(x.chain,x.chainId)||gc,addr=String(x.address||ga||'').toLowerCase();if(!isAddr(addr)||!c)return null;const id=idOf(c,addr);
   if(!lanes.has(c)){lanes.add(c);m.lanes.unshift({id:c,name:cname(c),color:ccolor(c)});}
   if(!known.has(id)){known.add(id);const stop=x.stop&&typeof x.stop==='object'?x.stop:null,ex=STOPS.test(x.category||x.kind||'');
    const n={id,kind:id.startsWith('self')?'self-on':ex?'exchange':'wallet',lane:c,address:addr,category:x.category||x.kind,stop:ex||!!x.stop,stopWhy:stop?.why||(typeof x.stop==='string'?x.stop:''),
     label:x.entity||x.label||nick(addr)+' '+tail(addr),sub:short(addr),fresh:true,hop:x.hop??null};m.nodes.push(n);nn.push(id);}
   if(x.id)byGid[String(x.id).toLowerCase()]=id;return id;};
  const nodeFor=(v,fallbackChain)=>{const k=String(v||'').toLowerCase();if(byGid[k])return byGid[k];const [gc,addr]=split(k),c=gc||fallbackChain;return c&&isAddr(addr)?idOf(c,addr):null;};
  const addLink=x=>{const [fromChain,toChain]=String(x.chain||'').split('>'),ia=nodeFor(x.a||x.source,x.fromChain||fromChain),ib=nodeFor(x.b||x.target,x.toChain||toChain||fromChain);if(!ia||!ib||ia===ib||!known.has(ia)||!known.has(ib))return;
   const linkId=x.id?'research:'+x.id:[ia,ib,x.kind||'transfer',x.asset||x.symbol||'',x.bridge||'',x.matchClass||'',x.grade||'unknown'].join('|');
   if(m.links.some(l=>l.id===linkId))return;const g=x.grade||'unknown',c=x.chain||split(x.a)[0],A=m.nodes.find(n=>n.id===ia),B=m.nodes.find(n=>n.id===ib);
   const what=x.kind==='bridge'?'bridge':x.kind==='link'?'linked':'transfer';
   const L={id:linkId,a:ia,b:ib,g,chain:c,fresh:true,usd:x.usd||0,why:x.why||`${cname(c)}: ${x.txCount!=null?num(x.txCount)+' transfers':''}${x.symbol?' of '+x.symbol:''}`,evidencePath:typeof x.evidence==='string'&&x.evidence.startsWith('/api/public/em/')?x.evidence:null,
    parts:[{kind:what,grade:g,chain:c,rule:x.why||step.title||'Found by research',counter:x.counter||[],
     facts:[x.totalsScope==='all_time'?'Counts and amounts cover all recorded time; the selected window only filters overlapping activity':null,x.amountScope==='leading_asset_all_time'?'Amount is for the leading asset across all recorded time':null,x.txCount!=null?`${num(x.txCount)} transfer${x.txCount===1?'':'s'}${x.symbol?' of '+x.symbol:''}${x.amount!=null?' · '+num(x.amount):''}`:null,x.firstTime?`${day(x.firstTime)}${x.lastTime&&x.lastTime!==x.firstTime?' → '+day(x.lastTime):''}`:null,x.firstBlock!=null?`Blocks ${Number(x.firstBlock).toLocaleString('en-US')}–${Number(x.lastBlock??x.firstBlock).toLocaleString('en-US')}`:null,B?.hop!=null?`Hop ${B.hop} from this wallet`:null].filter(Boolean),
     fetch:what!=='bridge'&&A?.address&&B?.address&&A.lane===B.lane?{chain:c,a:A.address,b:B.address}:null}]};m.links.push(L);nl.push(L.id);};
  const graphs=Array.isArray(d.nodes)?[d]:Object.values(d).filter(g=>g&&Array.isArray(g.nodes));
  for(const g of graphs){for(const x of g.nodes)addNode(x);for(const x of g.edges||g.links||[])addLink(x);
   for(const x of g.stops||[]){const id=nodeFor(x.id||(x.chain?x.chain+':'+x.address:''));const n=id&&m.nodes.find(y=>y.id===id);if(n){n.stop=true;n.stopWhy=x.why||'';}}}
  if(!nn.length&&!nl.length)return 0;
  for(const n of m.nodes)if(!nn.includes(n.id))n.fresh=false;for(const l of m.links)if(!nl.includes(l.id))l.fresh=false;
  st.steps.push({kind,label:step.title||kind,nodes:nn,links:nl,hops:step.hops||d.hops||Math.max(1,...m.nodes.filter(n=>nn.includes(n.id)).map(n=>n.hop||1)),credits:step.credits||0});
  st.edge=null;st.sel=null;st.hl=null;renderSubject();renderBar();renderCanvas();renderTabs();renderRows();renderScope();loadFresh();return nn.length;}
 function backStep(){const s=st.steps.pop(),m=st.model;if(!s)return;
  m.nodes=m.nodes.filter(n=>!s.nodes.includes(n.id));m.links=m.links.filter(l=>!s.links.includes(l.id));
  const prev=st.steps[st.steps.length-1];for(const n of m.nodes)n.fresh=!!prev?.nodes.includes(n.id);for(const l of m.links)l.fresh=!!prev?.links.includes(l.id);
  if(st.edge&&!m.links.some(l=>l.id===st.edge))st.edge=null;m.lanes=m.lanes.filter(l=>m.nodes.some(n=>n.lane===l.id));
  renderSubject();renderBar();renderCanvas();renderTabs();renderRows();renderScope();}
 // how fresh each chain's data is (freshness: map, ledger, charts, balances, realtime layers) and the credits spent on this subject
 // (intelligence/runs, signed in); both feed the scope line
 async function loadFresh(){const cs=st.mode==='wallet'?st.model?.lanes.map(l=>l.id):st.mode==='token'?[st.token.chain]:[];if(!cs?.length)return;
  const k=cs.slice().sort().join(',');if(st.freshKey===k)return;st.freshKey=k;
  const f=await api('/api/public/em/freshness?chains='+encodeURIComponent(k),{signal,timeout:15000}).catch(()=>null);if(off()||!f)return;st.fresh=f.chains||{};renderScope();}
 async function loadCredits(){const subj=st.mode==='wallet'?st.address:st.token?.id;if(!subj)return;
  const e=await entitlement().catch(()=>null);if(off()||!e?.signedIn){renderScope();return;}   // signed out: no runs to count
  const r=await api('/api/public/em/intelligence/runs?subject='+encodeURIComponent(subj),{signal,timeout:15000,credentials:'same-origin'}).catch(()=>null);if(off())return;
  if(r&&r.net_credits!=null){st.creditsUsed=r.net_credits;st.creditsRuns=r.count;}renderScope();}
 // the scope line: chains, hops, period, the data each chain is read to, credits spent here
 // one chain's data cut: the map layer's block and time (freshness), else the block a read reported; every layer in the tooltip
 function freshChip(c,block){const f=st.fresh?.[c],mp=f?.map,layers=f?['map','ledger','charts','balances','realtime'].filter(k=>f[k]?.block!=null||f[k]?.time).map(k=>`${k}: ${f[k].block!=null?'#'+Number(f[k].block).toLocaleString('en-US'):''} ${String(f[k].timeUtc||'').replace('T',' ').slice(0,16)} UTC${f[k].source?' ('+f[k].source+')':''}`).join('\n'):'';
  const blk=mp?.block??block;
  return blk!=null?`<span class="tkv-sb" title="${esc(layers||'Read up to this block')}">${esc(cname(c))} #${Number(blk).toLocaleString('en-US')}${mp?.timeUtc?' · '+esc(String(mp.timeUtc).slice(5,16).replace('T',' '))+' UTC':''}</span>`:`<span class="tkv-sb">${esc(cname(c))} · map freshness unavailable</span>`;}
 function renderScope(){const box=$('[data-v-scope]');if(!box)return;const parts=[];
  const requested=st.selectedContext?.selection||st.requestedSelection;if(requested?.block!=null)parts.push('Requested block '+num(requested.block));if(requested?.time!=null)parts.push('Requested '+new Date(requested.time*1000).toISOString());if(requested?.from!=null)parts.push('Requested range '+requested.from+' → '+requested.to);if(requested?.window)parts.push('Requested window '+esc(requested.window));
  if(st.mode==='token'&&st.hold){const r=st.shares?.[st.snap],frontier=st.hold.as_of||st.hold.asOf?.[st.token?.chain]||st.hold.asOf;if(st.hold.held)parts.push('Ownership shares withheld');else if(r)parts.push(esc(supplyNote(r)));if(r?.block!=null)parts.push('Actual holder snapshot block '+num(r.block));if(r?.time>0)parts.push(new Date(r.time*1000).toISOString().replace('T',' ').slice(0,16)+' UTC');if(frontier?.block!=null)parts.push('Source indexed through block '+num(frontier.block));if(st.hold.held)parts.push('Balance withheld: '+esc(plainWhy(st.hold.reason||st.hold.heldReason||'under reconciliation')));}

  const CREDITS=`Credits used here: <b>${num(st.creditsUsed)}</b>${st.creditsRuns?`<small> · ${num(st.creditsRuns)} run${st.creditsRuns>1?'s':''}</small>`:''}`;
  if(st.mode==='wallet'&&st.model){const m=st.model,first=Math.min(...m.chains.map(c=>c.first||Infinity)),hops=Math.max(1,...st.steps.map(s=>s.hops||1));
   parts.push(`<b>${m.lanes.length}</b> chain${m.lanes.length>1?'s':''}`,`<b>${hops}</b> hop${hops>1?'s':''}`,`${isFinite(first)?day(first):'first seen: —'} → indexed source frontier<small> · transfer map: since first seen; protocols/trades: last 7 days. Each edge retains its source scope.</small>`);
   const basis=m.lanes.slice().reverse().map(l=>freshChip(l.id,null));
   parts.push(CREDITS,`<span class="tkv-sbs">Data as of ${basis.join('')}</span>`);}
  else if(st.mode==='token'&&st.token){const now=st.snaps?.[st.snaps.length-1],r=st.shares?.[st.snaps.length-1],L=st.snaps?.[0]?.launch;
   parts.push(`<b>1</b> chain · ${esc(cname(st.token.chain))}`,`Linked groups: <b>1</b> hop`,`${Object.keys(st.requestedSelection).length?'Requested selection':L?'Launch '+day(L)+' → latest snapshot':'Latest source snapshots'}<small> · ${num(st.snaps?.length||0)} proven moments</small>`,
    CREDITS,`<span class="tkv-sbs">${r?.block!=null?'Actual holder snapshot: '+esc(cname(st.token.chain))+' #'+num(r.block):'Requested holder snapshot unavailable'}</span>`);}
  else{box.hidden=true;return;}
  box.hidden=false;
  box.innerHTML=`<span class="tkv-sl">Scope</span>${parts.map(x=>`<span class="tkv-si">${x}</span>`).join('')}`;}
 function select(id){st.sel=st.sel===id?null:id;st.hl=null;st.edge=null;
  const n=st.model?.nodes.find(x=>x.id===id);
  if(n?.kind==='self'||n?.kind==='self-on'){st.tab='chains';st.open=n.kind==='self'?st.model.home:n.lane;}else if(n?.kind==='protocol')st.tab='protocols';else if(n){st.tab='wallets';st.open=id;}
  renderTabs();renderCanvas();renderRows();}
 function highlight(ids){if(st.mode!=='wallet')return;const want=new Set((ids||[]).map(x=>String(x).toLowerCase()));
  const hit=st.model.nodes.filter(n=>want.has(n.id)||(n.address&&want.has(n.address))).map(n=>n.id);
  st.sel=null;st.hl=hit.length?hit:null;renderBar();renderCanvas();$('.tkv-canvas').scrollIntoView({behavior:'smooth',block:'nearest'});}
 // ---------- chart view: the existing chart for one market with this wallet's (and optionally its linked wallets') marks
 async function renderChartView(box){box.classList.add('chart');const c=st.chartOf,m=st.model;
  box.innerHTML=`<div class="tkv-ch-top"><h4>${tokIcon(c.chain,c.token,c.symbol,22)}${esc(c.symbol)}</h4>${chainChip(c.chain)}<div class="tkv-tg">Show linked wallets' trades <button type="button" class="tkv-switch${st.group?' on':''}" data-v-sw aria-pressed="${st.group}" aria-label="Show linked wallets' trades"></button></div></div>
   <div class="tkv-chart" data-v-chart>${loadingHTML('Loading the chart…')}</div><div class="tkv-ch-leg"><span><b class="tkv-up">▲</b> Bought</span><span><b class="tk-muted">●</b> Received · cost unknown</span><span><b class="tkv-down">▼</b> Sold</span><span data-v-mknote class="tk-muted"></span></div>`;
  box.querySelector('[data-v-sw]').onclick=()=>{st.group=!st.group;renderCanvas();};
  const host=box.querySelector('[data-v-chart]');
  try{const mod=await import('./explorer-investigate-chart.js');if(off()||st.chartOf!==c)return;host.innerHTML='';
   chartApi=await mod.mountMarketChart(host,{market:c.market,markers:[],interval:'1d',signal});if(!chartApi)return;   // daily bars: a holding's whole history
   const marks=await walletMarks(c,c.who,false),group=st.group?(await Promise.all(linkedFirst(m).slice(0,6).map(n=>walletMarks(c,n.address,true)))).flat():[];
   if(off()||!chartApi)return;chartApi.update({markers:[...group,...marks]});
   box.querySelector('[data-v-mknote]').textContent=marks.note||'';}
  catch(e){if(!off())host.innerHTML=errorHTML(e.message||'The chart could not load.',false);}}
 // marks for one wallet on one market: wallet-trades (every buy ▲, sale ▼, receipt ● with block and tx); the part of the history still
 // being read says so. Without it: first-entry and the large trades investigate reports.
 const SIDE={buy:'buy',buy_other_sender:'buy',sell:'sell',received:'recv',mint:'recv',sent:'sent'},MARK_WORD={buy:'buys',sell:'sales',recv:'receipts',sent:'transfers out'};
 async function walletMarks(c,who,faded){const out=[];out.note='';const pre=faded?nick(who)+' · ':'';
  const wt=await api('/api/public/em/wallet-trades?'+new URLSearchParams({address:who,market:c.market}),{signal,timeout:25000}).catch(e=>({available:false,reason:e.message}));
  if(wt?.available!==false&&(wt?.markers||[]).length){
   for(const m of wt.markers){const side=SIDE[m.kind];if(!side)continue;
    out.push({t:m.time,side,faded,tx:m.tx,amount:num(m.amount),label:pre+(m.kind==='buy_other_sender'?'bought through a swap someone else sent':m.kind==='mint'?'minted':m.kind==='sent'?'sent to '+short(m.counterparty||''):m.kind==='received'?'from '+(m.counterparty?short(m.counterparty):m.basis||'a balance change'):'')+(m.usd!=null?' · '+usd(m.usd):'')});}
   // one mark per side per day on the daily chart: hundreds of receipts would hide the price
   if(out.length>40){const by=new Map();for(const x of out){const k=x.side+':'+Math.floor(x.t/86400);const g=by.get(k);if(g){g.n++;g.label=`${g.n} ${MARK_WORD[x.side]} that day`;}else by.set(k,{...x,n:1});}out.splice(0,out.length,...by.values());}
   const cv=wt.coverage||{},n=wt.counts||{};
   out.note=`${num(n.buy||0)} buys · ${num(n.sell||0)} sales · ${num(n.received||0)} receipts`+(cv.transfersPreparing?.length||cv.job?.state==='paused'?` · part of the transfer history: ${cv.job?.state==='paused'?'paused':'preparing'}`:'');
   return out;}
  out.note='Trade markers were not returned for this wallet and token. Balance positions are shown separately.';
  const fe=await api('/api/public/em/cluster/first-entry?'+new URLSearchParams({market:c.market,wallets:who}),{signal,timeout:25000}).catch(()=>null);
  const mem=(fe?.members||[])[0];
  if(mem?.firstBuy)out.push({t:mem.firstBuy.time,side:'buy',label:pre+'first buy · '+usd(mem.firstBuy.usd),amount:num(mem.firstBuy.amount),tx:mem.firstBuy.tx,faded});
  if(mem?.firstReceive&&mem.firstReceive.kind!=='swap')out.push({t:mem.firstReceive.time,side:'recv',label:pre+'first receipt from '+short(mem.firstReceive.from),amount:num(mem.firstReceive.amount),tx:mem.firstReceive.tx,faded});
  if(out.length)out.note='Partial trade markers: '+out.length+' first-entry records. Full trade history is unavailable.';
  return out;}
 // the wallets most likely under the same control first: judged, inferred, matched, then direct transfer partners
 function linkedFirst(m){const R={judged:4,inferred:3,matched:2,same:2,proven:1};const g=id=>Math.max(0,...m.links.filter(l=>l.a===id||l.b===id).map(l=>R[l.g]||0));
  return m.nodes.filter(n=>n.kind==='wallet'&&n.address).sort((x,y)=>g(y.id)-g(x.id));}
 function openChart(asset,who){const m=st.model;if(!asset?.market)return;
  st.chartOf={market:asset.market,chain:asset.chain,token:asset.token,symbol:asset.symbol,who:who||m.address};st.view='chart';
  renderBar();renderCanvas();$('.tkv-canvas').scrollIntoView({behavior:'smooth',block:'start'});}

 // ---------- lower tabs (wallet)
 function renderTabs(){const t=$('[data-v-tabs]');
  if(st.mode==='token')return renderTokenTabs(t);
  const m=st.model,tabs=[['wallets','Linked wallets',m.graphAvailable?m.nodes.filter(n=>n.kind==='wallet'||n.kind==='exchange').length:'—'],['chains','Cross-chain',m.lanes.length],['protocols','Protocols',m.graphAvailable?m.prot.length:'—'],['assets','Holdings',m.balanceRows.length||'—'],['transactions','Recent transactions',m.recent.length||'—']];
  if(!tabs.some(x=>x[0]===st.tab))st.tab=tabs[0][0];
  t.innerHTML=tabs.map(([k,n,c])=>`<button type="button" role="tab" data-v-tab="${k}" class="${st.tab===k?'on':''}" aria-selected="${st.tab===k}">${n}<span>${c}</span></button>`).join('');
  t.querySelectorAll('[data-v-tab]').forEach(b=>b.onclick=()=>{st.tab=b.dataset.vTab;st.open=null;renderTabs();renderRows();});}
 const pnlCache={};
 async function holdingsOf(addr,chain){
  const key=chain+':'+addr;
  pnlCache[key]=pnlCache[key]||api('/api/public/em/tools/wallet?'+new URLSearchParams({address:addr,chain,positions_limit:'100'}),{signal,timeout:25000}).then(raw=>{const card=walletSourceCard(raw,addr,chain);return {cardsOverride:{[chain]:card},balanceRows:walletBalanceRows({[chain]:card})};});
  return pnlCache[key];
 }
 function holdTable(rows,{chain,who}={}){rows=(rows||[]).filter(r=>!chain||r.chain===chain);
  if(!rows.length)return `<p class="tkv-empty">${chain&&!LIVE.includes(chain)?`Holdings with cost basis on ${esc(cname(chain))}: ${PREP} (trader analytics cover Arbitrum and Robinhood).`:'No swap-bought holdings recorded for this wallet on the mapped chains.'}</p>`;
  return `<div class="tk-table-wrap"><table class="tkv-htbl"><thead><tr><th>Asset</th><th class="r">Balance</th><th class="r">Value</th><th>How it got here</th><th>First buy</th><th class="r">Avg cost</th><th class="r">P&amp;L</th><th></th></tr></thead><tbody>${rows.slice(0,40).map((r,i)=>{
   const v=r.verified===false?` title="${esc(r.unverifiedReason==='price_flag'?'The fills disagree with the reference price':'Transfers moved a large share of this position')}"`:'';
   return `<tr${r.market?` class="ck" data-v-asset="${i}" data-v-who="${esc(who||'')}"`:''}><td><span class="tkv-tok">${tokIcon(r.chain,r.token,r.symbol)}${esc(r.symbol||short(r.token))} ${chainChip(r.chain)}</span></td>
    <td class="r tk-num">${num(r.balance)}</td><td class="r tk-num">${r.valueUsd==null?'<span class="tk-faint" title="No price">—</span>':money(r.valueUsd)}</td>
    <td><span class="tkv-how ${r.how}">${HOW[r.how]}</span>${r.same?.length?` ${gradeChip('judged')}`:''}${(r.same?.[0]||r.recv?.[0])?`<small class="tk-muted" title="${esc((r.same?.[0]||r.recv[0]).linkEvidence||'')}"> · from ${esc(short((r.same?.[0]||r.recv[0]).address))} ×${num((r.same?.[0]||r.recv[0]).n)}</small>`:r.transfersIn>0&&r.buys>0?`<small class="tk-muted"> · ${num(r.transfersIn)} received</small>`:''}</td><td class="tk-num">${r.firstBuyTs?day(r.firstBuyTs):'—'}</td>
    <td class="r tk-num"${v}>${r.avgCostUsd==null?'—':`<span class="${r.verified===false?'tk-v-est':''}">${usd(r.avgCostUsd)}</span>`}</td><td class="r tk-num ${r.pnlUsd>0?'tkv-up':r.pnlUsd<0?'tkv-down':''}">${r.pnlUsd==null?'—':money(r.pnlUsd)}</td>
    <td class="r">${r.market?'<span class="tkv-go">Chart ›</span>':''}</td></tr>`;}).join('')}</tbody></table></div>`;}
 function bindHold(scope,rows){scope.querySelectorAll('[data-v-asset]').forEach(r=>r.onclick=e=>{e.stopPropagation();openChart(rows[+r.dataset.vAsset],r.dataset.vWho||st.model.address);});}
 function actualHoldingsHTML(m){
  const rows=m.balanceRows;
  const statuses=Object.entries(m.cardsOverride||st.data.cards||{}).map(([chain,c])=>`<p class="tkv-empty">${chainChip(chain)} ${c?.available===false?esc(c.reason||'This wallet source is unavailable'):c?.sheet?.held?`<b>Held</b> · ${esc(c.sheet.heldReason||'Source reconciliation pending')}`:!c?.positions?.length?'Balances unavailable in this returned indexed source; no zero balance is inferred.':c.sheet?.partial?'Partial balance sheet; unpriced positions may remain.':'Returned balance positions'}${c?.asOf!=null?' · As of '+esc(typeof c.asOf==='object'?JSON.stringify(c.asOf):String(c.asOf)):' · Balance source time unavailable'}</p>`).join('');
  const unavailable=Object.entries(m.cardsOverride?{}:st.walletCoverage||{}).filter(([c,x])=>!st.data.cards[c]||x?.available===false||x?._error).map(([c,x])=>`<p class="tkv-empty">${chainChip(c)} ${esc(x?.reason||x?._error||'No wallet balance coverage returned for this address.')} · registry membership does not establish served wallet coverage.</p>`).join('');
  const bounded=Object.entries(m.cardsOverride||st.data.cards||{}).filter(([,c])=>c?.positionsTotal!=null||c?.positionsTruncated).map(([c,x])=>`<p class="tkv-empty">${chainChip(c)} ${num(x.positionsReturned??x.positions?.length??0)} of ${x.positionsTotal==null?'unknown':num(x.positionsTotal)} source positions returned${x.positionsTruncated?' · bounded result is truncated':''}. Coverage and unpriced positions remain as reported by the source.</p>`).join('');
  return statuses+unavailable+bounded+(rows.length?`<div class="tk-table-wrap"><table class="tkv-htbl"><thead><tr><th>Asset / chain</th><th>Position</th><th class="r">Balance · source precision</th><th class="r">Value</th></tr></thead><tbody>${rows.slice(0,80).map((r,i)=>`<tr${r.market&&!r.held?` class="ck" data-v-balance="${i}"`:''}><td><span class="tkv-tok">${tokIcon(r.chain,r.token,r.symbol)}${esc(r.symbol)} ${chainChip(r.chain)}</span></td><td>${esc(r.kind)}${r.protocol?' · '+esc(r.protocol):''}</td><td class="r">${r.held?`<span class="tk-v-held" title="${esc(r.reason||'Held by source')}">Held</span>`:rawUnits(r.rawAmount,r.decimals)??(typeof r.amountText==='string'?esc(r.amountText):r.amount==null?'—':r.amount===0?'0 · source precision':num(r.amount))}</td><td class="r">${r.valueUsd==null?'—':money(r.valueUsd)}${r.flag?` <span class="tk-v-est" title="${esc(r.flag)}">Unverified</span>`:''}</td></tr>`).join('')}</tbody></table></div>`:'<p class="tkv-empty">No verified holding amounts were returned. This does not mean this wallet owns no assets.</p>');
 }
 async function renderRows(){const box=$('[data-v-rows]');
  if(st.mode==='token')return renderTokenRows(box);
  const m=st.model;box.innerHTML='';
  if(st.tab==='transactions'){box.innerHTML=m.recent.length?`<div class="tk-table-wrap"><table><thead><tr><th>Chain / time</th><th>Recorded transaction</th><th>Value</th></tr></thead><tbody>${m.recent.map(t=>`<tr><td>${chainChip(t.chain)} ${t.time?day(t.time):'Time unavailable'}</td><td>${esc(t.text||'Transaction')}${t.tx?` <a class="tk-link" target="_blank" rel="noopener" href="${esc(explorerTx(t.chain,t.tx))}">${esc(short(t.tx))}</a>`:''}</td><td>${t.usd==null?'—':usd(t.usd)}</td></tr>`).join('')}</tbody></table></div>`:'<p class="tkv-empty">Recent transactions were not returned by these sources. No zero activity is inferred.</p>';return;}

  const row=(html,{cls='',onclick}={})=>{const d=document.createElement('div');d.className='tkv-row '+cls;d.innerHTML=html;if(onclick)d.onclick=onclick;box.append(d);return d;};
  if(st.tab==='wallets'){const ws=m.nodes.filter(n=>n.kind==='wallet'||n.kind==='exchange');
   if(!m.graphAvailable){box.innerHTML='<p class="tkv-empty">Connection coverage was not returned for this address. No zero links or common ownership is inferred.</p>';return;}
   if(!ws.length){box.innerHTML=`<p class="tkv-empty">No transfer partners or bridge counterparts recorded yet. Links by behaviour (bought together, same funder, same owner): ${PREP}.</p>`;return;}
   for(const n of ws){const L=m.links.find(l=>(l.a===n.id||l.b===n.id)&&l.g!=='used');
    const r=row(`${n.kind==='exchange'?`<img src="/assets/em/groups/exchange.svg" width="26" height="26" alt="">`:n.logo?`<img class="tkx-art" src="${esc(n.logo)}" width="26" height="26" alt="">`:groupIcon(n.category,26)||avatar(n.address)}
     <div class="tkv-nm">${esc(n.label)}<small>${esc(short(n.address))} · ${esc(cname(n.lane))}</small></div><div class="tkv-rel">${esc(L?.why||'')}<small>1 hop from this wallet</small></div>${L?gradeChip(L.g):''}<span class="tkv-car" aria-hidden="true">›</span>`,
     {cls:(st.sel===n.id?'sel ':'')+(st.open===n.id?'open':''),onclick:()=>{st.open=st.open===n.id?null:n.id;st.sel=n.id;renderBar();renderCanvas();renderRows();}});
    if(st.open===n.id){const d=document.createElement('div');d.className='tkv-hold';const stopNote=n.stop?'<p class="tkv-empty">Exchange classification: further ownership tracing stops here. These are this address’s on-chain balance reads, with no claim about internal exchange accounts.</p>':'';d.innerHTML=stopNote+loadingHTML('Reading what it holds…');r.after(d);
     holdingsOf(n.address,n.lane).then(result=>{if(!d.isConnected)return;d.innerHTML=stopNote+actualHoldingsHTML(result);d.querySelectorAll('[data-v-balance]').forEach(r=>r.onclick=e=>{e.stopPropagation();openChart(result.balanceRows[+r.dataset.vBalance],n.address);});}).catch(e=>{if(d.isConnected)d.innerHTML=e.status===404?`<p class="tkv-empty">What it holds, with cost basis: ${PREP}</p>`:errorHTML(e.message,false);});}}
   return;}
  if(st.tab==='chains'){const bridges=m.legs;
   row(`<div class="tkv-xsum">${m.contract?'<b>A contract wallet.</b> The same address on another chain can belong to someone else, so chains are not linked by address here.'
    :m.lanes.length>1?`<b>Indexed on ${m.lanes.length} chains.</b> Shared control is shown only where the source confirms EOA or matched evidence. ${m.moves?`${m.moves} bridge move${m.moves>1?'s':''} recorded.`:''}`
    :'Seen on one mapped chain only.'}</div>`,{cls:'plain'});
   for(const l of m.lanes.slice().reverse()){const c=m.chains.find(x=>x.id===l.id)||{},id=l.id===m.home?'self':'self-on:'+l.id,how=l.id===m.home?'<span class="tk-chip">Searched here</span>':m.contract||c.contract?'<span class="tk-chip warn">Contract: not linked</span>':c.sameOwner===true?gradeChip('same'):'<span class="tk-chip">Ownership unverified</span>';
    const r=row(`<span class="tkv-cmono" style="background:${esc(l.color)}">${esc(String(l.name).slice(0,2).toUpperCase())}</span><div class="tkv-nm">${esc(CHAIN[l.id]?.name||l.name)}<small>${c.txs!=null?num(c.txs)+' txs':'txs: preparing'} · ${c.first?'since '+day(c.first):'first seen: preparing'} · ${c.valueHeld?`value: <span class="tk-v-held" title="${esc(c.valueHeld)}">held back</span>`:c.value!=null?money(c.value):'value: —'}</small></div>
     <div class="tkv-rel">${l.id===m.home?'The chain you searched':c.sent===0?'Receives here, has not signed a transaction here':c.txs!=null?'Signed transactions here':'Seen through a bridge move'}<small>${l.id===m.home?'':m.contract||c.contract?'Same address, owner not linked':c.sameOwner===true?'Same address · confirmed EOA key':'Same address · ownership unverified'}</small></div>${how}<span class="tkv-car" aria-hidden="true">›</span>`,
     {cls:(st.open===l.id?'open':''),onclick:()=>{st.open=st.open===l.id?null:l.id;st.sel=id;st.hl=null;renderBar();renderCanvas();renderRows();}});
    if(st.open===l.id){const d=document.createElement('div');d.className='tkv-hold';d.innerHTML=holdTable(m.assets,{chain:l.id})+(c.valueHeld?`<p class="tkv-empty"><span class="tk-v-held" title="${esc(c.valueHeld)}">Balance sheet held back</span> · ${esc(c.valueHeld)}. No amount is shown until it is reconciled.</p>`
      :c.value!=null&&!LIVE.includes(l.id)?`<p class="tkv-empty">Balance sheet on ${esc(cname(l.id))}: ${money(c.value)} (no cost basis)${c.sheetNote?` · <span class="tk-v-est" title="${esc(c.sheetNote)}">partial</span>`:''}.</p>`
      :c.sheetNote&&!LIVE.includes(l.id)?`<p class="tkv-empty">Balance sheet on ${esc(cname(l.id))}: <span class="tk-v-est" title="${esc(c.sheetNote)}">partial</span> · ${esc(c.sheetNote)}</p>`:'');r.after(d);bindHold(d,m.assets.filter(x=>x.chain===l.id));}}
   const h=document.createElement('div');h.className='tkv-xh';h.textContent='Bridge moves in this investigation';box.append(h);
   if(!bridges.length){box.insertAdjacentHTML('beforeend','<p class="tkv-empty">No bridge moves recorded for this address.</p>');return;}
   const once=new Set();
   for(const l of bridges.slice(0,40)){const k=[l.tx,l.counterparty?.tx].filter(Boolean).sort().join('|');if(k&&once.has(k))continue;once.add(k);
    const cp=l.counterparty||{},here=chainOf(l.chain,l.chain_id),there=chainOf(cp.chain,cp.chain_id),cls=l.match?.class;
    const g=cls==='exact'||cls==='structural'?'matched':cls==='heuristic'?'inferred':null,from=l.dir==='out'?here:there,to=l.dir==='out'?there:here;
    row(`<span class="tkv-lic">${lineIcon(g||'used',26)}</span><div class="tkv-nm">${esc(cname(from))} → ${esc(cname(to))}<small>${esc(l.protocol||l.bridge||'bridge')} · ${day(l.ts)}</small></div>
     <div class="tkv-rel">${cp.wallet?(cp.wallet.toLowerCase()===m.address?'To the same address':'Other side: '+esc(short(cp.wallet))):'Other side not found yet'}<small>${g?(g==='matched'?'Both legs found ('+esc(cls)+')':'Matched by amount and time only'):cls==='one_side'?'One side only: the destination chain is named by the event':esc(cls||'')}</small></div>
     ${g?gradeChip(g):'<span class="tk-chip">One side</span>'}<span></span>`,{onclick:()=>{const ids=[here===m.home?'self':'self-on:'+here];if(there)ids.push(there===m.home?'self':cp.wallet&&cp.wallet.toLowerCase()!==m.address?`w:${there}:${cp.wallet.toLowerCase()}`:'self-on:'+there);highlight(ids);}});}
   return;}
  if(st.tab==='protocols'){if(!m.graphAvailable){box.innerHTML='<p class="tkv-empty">Protocol activity coverage was not returned. No zero activity is inferred.</p>';return;}if(!m.prot.length){box.innerHTML='<p class="tkv-empty">No protocol use recorded in the last 7 days on the mapped chains.</p>';return;}
   for(const p of m.prot.slice().sort((x,y)=>y.usd-x.usd)){const n=m.nodes.find(x=>x.id===p.id);
    row(`${n?.logo?`<img class="tk-logo md" src="${esc(n.logo)}" alt="">`:'<img class="tk-logo md" src="/assets/em/kinds/other-soft.svg" alt="">'}<div class="tkv-nm">${esc(p.name)}<small>${esc(cname(p.chain))}</small></div><div class="tkv-rel">${usd(p.usd)} through it in 7 days<small>${num(p.events)} actions · ${esc([...p.types].join(', '))}</small></div>${gradeChip('used')}<span></span>`,
     {cls:st.sel===p.id?'sel':'',onclick:()=>{st.sel=p.id;renderBar();renderCanvas();renderRows();}});}
   return;}
  const actual=document.createElement('div');actual.className='tkv-hold flat';actual.innerHTML=actualHoldingsHTML(m);box.append(actual);actual.querySelectorAll('[data-v-balance]').forEach(r=>r.onclick=()=>openChart(m.balanceRows[+r.dataset.vBalance],m.address));
  const d=document.createElement('div');d.className='tkv-hold flat';d.innerHTML='<h3>Trading positions · cost basis where served</h3>'+(st.data.pnlError?(/not served/.test(st.data.pnlError)?`<p class="tkv-empty">Holdings with cost basis and P&amp;L: ${PREP}</p>`:errorHTML('Holdings are unavailable right now: '+st.data.pnlError,false)):holdTable(m.assets,{who:m.address}))+
   (m.coverage?.without?`<p class="tkv-empty">${esc(m.coverage.without)}</p>`:'');box.append(d);bindHold(d,m.assets);}

 // ======================================================================== keep: save, export, reopen
 // Save keeps a research run as an investigation (Pro and Team, em-account); reopening one shows what changed since it was saved
 // first (snapshot-diff: new links, balance changes, group trades), then the map with the saved graph on it. Export: a saved
 // investigation exports with the transactions behind each link (invscreen export); an unsaved page exports what its map shows.
 function keepButtons(){return `<button type="button" class="tk-btn${st.saved?' primary':''}" data-v-save title="${st.saved?'Saved: open the list':st.lastRun?'Save this investigation':'Save keeps a research run'}">${st.saved?'★ Saved':'☆ Save'}</button>
  <div class="tkv-expw"><button type="button" class="tk-btn" data-v-exp aria-haspopup="menu" aria-expanded="false">Export ▾</button><div class="tkv-expm tk-card" role="menu" hidden><button type="button" role="menuitem" data-v-fmt="json">JSON · nodes, links${st.saved?', transactions':''}</button><button type="button" role="menuitem" data-v-fmt="csv">CSV · one table</button></div></div>`;}
 function bindKeep(s){s.querySelector('[data-v-save]')?.addEventListener('click',e=>save(e.currentTarget));
  const eb=s.querySelector('[data-v-exp]'),menu=s.querySelector('.tkv-expm');if(!eb)return;
  eb.onclick=()=>{menu.hidden=!menu.hidden;eb.setAttribute('aria-expanded',String(!menu.hidden));};
  menu.querySelectorAll('[data-v-fmt]').forEach(b=>b.onclick=()=>{menu.hidden=true;exportAs(eb,b.dataset.vFmt);});}
 const isPaid=e=>e.limits?.save!=null?!!e.limits.save:['pro','team'].includes(e.tier);
 async function save(btn){const e=await entitlement({fresh:true});if(off())return;
  if(!e.signedIn)return gatePop(btn,...GATE.signin('Saving an investigation'));
  if(st.saved)return void(location.hash='#/investigations');
  if(!isPaid(e))return gatePop(btn,...GATE.pro('Saving investigations',e.name));
  if(!st.lastRun)return gatePop(btn,'Nothing to save yet: a saved investigation keeps a research run.','Ask a Flow question (related wallets on Flow costs the least), then save when it finishes.',st.mode==='wallet'?[['Related wallets on Flow',()=>ask?.expand('related')]]:[]);
  btn.disabled=true;btn.textContent='Saving…';
  const title=(st.mode==='wallet'?subjectName(st.model)+' '+tail(st.address):st.token.symbol)+' · '+new Date().toISOString().slice(0,10);
  try{st.saved=await accountApi('/api/public/em/investigations',{method:'POST',body:{run_id:st.lastRun,title},signal});renderSubject();renderSaved();}
  catch(x){if(off())return;btn.disabled=false;btn.textContent='☆ Save';
   if(x.code==='pro_required')gatePop(btn,...GATE.pro('Saving investigations',x.detail?.tier||e.name));
   else if(x.code==='saved_limit')gatePop(btn,`All ${x.detail?.limit??''} saved investigations are in use.`,'Delete one you no longer need, then save again.',[['Saved investigations','#/investigations']]);
   else if(x.status===409)gatePop(btn,'The research run is still going.','Save again when it finishes.');
   else if(x.status===401)gatePop(btn,...GATE.signin('Saving an investigation'));
   else gatePop(btn,'Could not save: '+x.message,'Try again in a moment.');}}
 async function exportAs(btn,fmt){const e=await entitlement({fresh:true});if(off())return;
  if(!e.signedIn)return gatePop(btn,...GATE.signin('Exporting evidence'));
  if(!(e.limits?.export!=null?e.limits.export:['pro','team'].includes(e.tier)))return gatePop(btn,...GATE.pro('Evidence export',e.name));
  const id=st.saved?.investigation_id,label=btn.textContent;
  if(id){btn.disabled=true;btn.textContent='Preparing…';   // the first export of an investigation attaches every link's transactions (up to ~30 s)
   try{const r=await fetch(`/api/public/em/investigations/${encodeURIComponent(id)}/export?format=${fmt}`,{credentials:'same-origin',signal});
    if(!r.ok){const d=await r.json().catch(()=>null),c=d?.detail?.code;throw Object.assign(new Error(d?.detail?.error||d?.detail||'Export failed'),{status:r.status,code:c});}
    download(await r.blob(),(/filename="?([^";]+)/.exec(r.headers.get('Content-Disposition')||'')||[])[1]||`${id}.${fmt}`);}
   catch(x){if(!off())gatePop(btn,x.code==='pro_required'?'Evidence export is on Pro.':'Could not export: '+x.message,x.code==='pro_required'?'Your plan does not include it.':'Try again in a moment.',x.code==='pro_required'?[['See plans','#/pricing']]:[]);}
   finally{btn.disabled=false;btn.textContent=label;}return;}
  const d=pageExport();download(new Blob([fmt==='json'?JSON.stringify(d,null,1):toCSV(d)],{type:fmt==='json'?'application/json':'text/csv'}),`ticking-${st.mode==='wallet'?st.address.slice(0,10):st.token.symbol}-${new Date().toISOString().slice(0,10)}.${fmt}`);}
 function download(blob,name){const u=URL.createObjectURL(blob),a=document.createElement('a');a.href=u;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(u),4000);}
 // what this page's map shows, in the export's shape: nodes, links with their grade and reasons (transactions come with a saved one)
 function pageExport(){const asOf=Object.fromEntries(Object.entries(st.fresh||{}).map(([c,f])=>[c,f.map||null]));
  if(st.mode==='token'){const r=st.shares?.[st.snap],sn=st.snaps?.[st.snap];
   return {schema:'investigation-export/1',source:'page',subject:{type:'token',market:st.token.id},moment:sn?{key:sn.key,time:sn.t}:null,asOf,
    nodes:(r?.holders||[]).map(h=>({id:st.token.chain+':'+h.address,chain:st.token.chain,address:h.address,kind:h.kind,label:h.label,share:h.share,group:h.cluster||null})),links:(r?.connections?.links||[]).map(l=>({...l,a:st.token.chain+':'+l.a,b:st.token.chain+':'+l.b})),
    groups:(r?.groups||[]).map(g=>({id:g.id,label:g.label,share:g.share,members:g.members})),connectionScope:r?.connections?{partial:true,sources:r.connections.sources,read:r.connections.read}:null,note:'Holders at the selected moment; recorded token transfers ending before that block. Connected does not mean common owner.'};}
  const m=st.model;return {schema:'investigation-export/1',source:'page',subject:{type:'wallet',address:m.address,chain:m.home},asOf,
   nodes:m.nodes.map(n=>({id:n.id,chain:n.lane,address:n.address||null,kind:n.kind,label:n.label,category:n.category||null,hop:n.hop??null,stop:n.stop?n.stopWhy||true:null})),
   links:m.links.map(l=>({id:l.id,a:l.a,b:l.b,chain:l.chain||null,grade:l.g==='same'?'proven':l.g,kind:l.parts?.[0]?.kind||null,why:l.why,rules:(l.parts||[]).map(p=>p.rule),against:[...new Set((l.parts||[]).flatMap(p=>p.counter||[]))]})),
   note:'What the map shows. Save an investigation to export the transactions behind each link.'};}
 function toCSV(d){const cols=['record','id','chain','address','kind','label','a','b','grade','link_kind','why','share','group'],q=v=>v==null?'':/[",\n]/.test(String(v))?'"'+String(v).replace(/"/g,'""')+'"':String(v);
  const rows=[...d.nodes.map(n=>({record:'node',...n})),...d.links.map(l=>({record:'link',id:l.id,chain:l.chain,a:l.a,b:l.b,grade:l.grade,link_kind:l.kind,why:[l.why,...(l.against||[]).map(x=>'against: '+x)].join(' | ')}))];
  return [cols.join(','),...rows.map(r=>cols.map(c=>q(r[c])).join(','))].join('\n');}
 // a saved investigation reopened: what changed since it was saved comes first
 async function loadSaved(id){const box=$('[data-v-saved]');box.hidden=false;box.innerHTML=loadingHTML('Opening the saved investigation…');
  try{const [inv,since]=await Promise.all([accountApi('/api/public/em/investigations/'+encodeURIComponent(id),{signal}),accountApi('/api/public/em/investigations/'+encodeURIComponent(id)+'/since',{signal,timeout:60000}).catch(e=>({available:false,reason:e.message,status:e.status}))]);
   if(off())return;st.saved=inv;st.since=since;const sn=inv.snapshot||{};
   if(st.mode==='wallet'&&(sn.nodes||[]).length){addStep({kind:'saved',step:{title:'Saved '+day(Date.parse(inv.saved_at)/1000||inv.saved_at),data:{nodes:sn.nodes,edges:sn.links||[],stops:sn.stops||[]}}});
    st.steps.pop();st.model.nodes.forEach(n=>n.fresh=false);st.model.links.forEach(l=>l.fresh=false);renderCanvas();renderSubject();renderXbar();}
   renderSaved();}
  catch(x){if(off())return;box.innerHTML=x.code==='pro_required'?`<p class="tkv-empty"><b>Saved investigations are on Pro.</b> Your plan does not include them. <a class="tk-link" href="#/pricing">See plans</a></p>`
   :x.status===401?`<p class="tkv-empty"><b>Sign in to open a saved investigation.</b> <a class="tk-link" href="#/console">Sign in</a></p>`:x.status===404?'<p class="tkv-empty"><b>This saved investigation no longer exists.</b> <a class="tk-link" href="#/investigations">Saved investigations</a></p>'
   :`<p class="tkv-empty">The saved investigation could not open: ${esc(x.message)}</p>`;}}
 function renderSaved(){const box=$('[data-v-saved]'),inv=st.saved;if(!box||!inv)return;box.hidden=false;const d=st.since,inc=d?.incidents||[];
  const at=inv.saved_at?new Date(typeof inv.saved_at==='number'?inv.saved_at*1000:inv.saved_at).toISOString().replace('T',' ').slice(0,16)+' UTC':'';
  box.innerHTML=`<div class="tkv-sv-h"><span class="tkv-sv-k">★ Saved investigation</span><b>${esc(inv.title||'')}</b><span class="tk-muted">saved ${esc(at)}</span><a class="tk-link" href="#/investigations">All saved</a></div>
   ${!d?'':d.available===false?`<p class="tkv-empty">What changed since it was saved: ${PREP} ${esc(d.reason||'')}</p>`
   :`<div class="tkv-sv-c"><b>${inc.length?`${num(inc.length)} change${inc.length>1?'s':''} since it was saved`:'Nothing changed since it was saved'}</b><span class="tk-muted">${d.asOfNow?'checked to '+Object.entries(d.asOfNow).map(([c,a])=>`${cname(c)} ${a?.block!=null?'#'+Number(a.block).toLocaleString('en-US'):a?.time?new Date(a.time*1000).toISOString().slice(0,16).replace('T',' ')+' UTC':''}`).join(', '):''}</span></div>
    ${inc.map(x=>`<details class="tkv-inc"><summary>${gradeChip(x.grade||'proven')}<b>${esc(x.title||x.kind)}</b><span class="tk-muted">${esc(String(x.window?.fromUtc||'').replace('T',' ').slice(0,16))}${x.window?.toUtc&&x.window.toUtc!==x.window.fromUtc?' → '+esc(String(x.window.toUtc).slice(11,16)):''}</span></summary>
     <ul>${(x.events||[]).slice(0,12).map(ev=>`<li><span class="tkv-how ${/sell/.test(ev.kind)?'sell':/buy/.test(ev.kind)?'buy':'recv'}">${esc(String(ev.kind||'').replace(/_/g,' '))}</span> ${esc(ev.text||ev.title||'')}${ev.before!=null&&ev.after!=null?` · ${num(ev.before)} → ${num(ev.after)}`:''}${ev.usd!=null?' · '+usd(ev.usd):''}${(ev.txs||ev.tx?[].concat(ev.txs||ev.tx):[]).slice(0,3).map(t=>` <a class="tk-link tk-addr" target="_blank" rel="noopener" href="${esc(explorerTx(ev.chain||x.chain,t))}">${esc(String(t).slice(0,10))}…</a>`).join('')}</li>`).join('')}</ul></details>`).join('')}`}`;}
 const explorerTx=(c,tx)=>(CHAIN[c]?.explorer||'https://arbiscan.io')+'/tx/'+tx;

 // ======================================================================== token
 // moments from holders-at (launch, +10m, +1h, +6h, +1d, now: every holder at that block, from the token's transfer history);
 // linked groups at a moment from investigate when the range is light enough; the launch from token-launch (the creation tx)
 const SNAP_LABEL={launch:'Launch',now:'Latest snapshot','selected-start':'Requested start','selected-end':'Requested end'},invCache={};
 const inv=(from,to)=>invCache[from+'|'+to]=invCache[from+'|'+to]||api('/api/public/em/investigate?'+new URLSearchParams({market:st.token.id,from:String(from),to:String(to),limit:'20'}),{signal,timeout:30000});
 const kindCat=h=>({mev:'mev_bot',whale:'whale',exchange:'exchange',contract:'contract_other',safe:'contract_other',router:'dex_router',bridge:'bridge'}[h.kind]||h.category);
 const fromSnap=s=>({count:s.holders??null,supply:s.supply,rawSupply:s.rawSupply,source:s.source,status:s.status,reason:s.reason,available:s.available!==false&&!st.hold?.held,block:s.effectiveBlock??s.block,time:s.effectiveTime??s.time,complete:s.complete,coverage:s.coverage,supplyAvailable:s.supplyAvailable,supplyBlock:s.supplyBlock,supplyReason:s.supplyReason,requestedBlock:s.requestedBlock,requestedTime:s.requestedTime,groups:[],d:null,
  holders:(s.top||[]).map(h=>({id:h.address,address:h.address,kind:h.kind==='pool'?'pool':h.kind,category:kindCat(h),share:h.pct,amount:h.balance??h.amount,rawAmount:h.rawAmount,isCreator:h.isCreator,
   label:h.entity||(h.isCreator?'Creator · '+nick(h.address):h.kind==='pool'?'Pool':h.kind==='router'?'Router':nick(h.address))+(h.entity?'':' '+tail(h.address)),
   how:h.isCreator?'the creator':h.kind==='pool'?'liquidity pool':h.kind==='exchange'?'an exchange: tracing stops':''}))});
 async function loadToken(){
  const t=await fetch('/api/public/markets/'+encodeURIComponent(st.market)).then(r=>r.ok?r.json():null).catch(()=>null)
   ||await identityMarkets().then(s=>(s.tokens||[]).find(x=>String(x.id).toLowerCase()===st.market)).catch(()=>null);if(off())return;
  st.token=t?{id:t.id,chain:t.network,token:t.mint,symbol:t.symbol,name:t.name,icon:t.icon,price:t.price}:{id:st.market,chain:st.market.split(':')[0],token:st.market.split(':')[1],symbol:short(st.market.split(':')[1])};
  title('Investigate '+st.token.symbol);
  st.snaps=[];st.shares={};st.snap=0;st.selectedContext=contextFor({kind:'token',asset:st.market,symbol:st.token.symbol},st.requestedSelection);
  renderSubject();renderTimeline();
  loadMarketArtwork().then(()=>{if(off())return;const art=marketArtwork(st.token.chain,st.token.token);if(art)st.token.icon=art.path;renderSubject();});
  const get=(p,timeout=30000)=>api(p,{signal,timeout}).catch(e=>e.name==='AbortError'?null:{available:false,reason:e.message,status:e.status,requestFailed:true});
  // Holder snapshots have their own capability. Launch/group analysis is
  // independent and remains limited to the engine that actually serves it.
  st.launchPrep=!LIVE.includes(st.token.chain);
  const point=st.requestedSelection;const exactPoint=point.block!=null||point.time!=null;
  const requestedPoints=holderPoints(point,Math.floor(Date.now()/1000));
  const readPoint=async p=>selectedHolderResponse(await get('/api/public/em/holders-at?'+new URLSearchParams({market:st.market,limit:'100',...(p.block!=null?{block:String(p.block)}:{t:String(p.time)})}),20000),st.market,p);
  const readHolders=async()=>{
   if(!requestedPoints.length)return get('/api/public/em/holders-at?'+new URLSearchParams({market:st.market,limit:'100',snaps:'1'}),20000);
   const cards=await Promise.all(requestedPoints.map(readPoint));
   const unavailable=cards.find(c=>c.held||c.available===false);if(unavailable)return {...unavailable,requestedSelection:point};
   const first=cards[0];return {...first,snaps:cards.flatMap((c,i)=>(c.snaps||[]).map(x=>({...x,key:cards.length>1?(i===0?'selected-start':'selected-end'):x.key}))),requestedSelection:point};
  };
  const [launch,hold]=await Promise.all([
   st.launchPrep?Promise.resolve(null):get('/api/public/em/token-launch?market='+encodeURIComponent(st.market)),readHolders()]);if(off())return;
  st.selectedContext=contextFor({kind:'token',asset:st.market,symbol:st.token.symbol},point);retainContext(st.selectedContext);
  st.tokenPrep=!hold||!!hold.held||hold.available===false;
  const artwork=await loadMarketArtwork();if(off())return;
  const art=marketArtwork(st.token.chain,st.token.token);if(art)st.token.icon=art.path;
  st.launch=launch?.available?launch:null;st.launchReason=launch?.reason;st.hold=hold;st.shares={};
  const now=Math.floor(Date.now()/1000);
  if(hold?.snaps?.length){st.snaps=hold.snaps.map(x=>({k:SNAP_LABEL[x.key]||x.key,key:x.key,t:x.requestedTime??x.effectiveTime??x.time??now,sourceTime:x.effectiveTime??x.time??null,block:x.requestedBlock??x.effectiveBlock??x.block,requestedPoint:exactPoint,requestedTime:x.requestedTime??null,launch:hold.launch?.time||null}));hold.snaps.forEach((x,i)=>{st.shares[i]=fromSnap(x);});}
  // holders-at not ready (a token read for the first time): the same six moments from the launch, holders from investigate
  else if(st.launch&&!st.tokenPrep&&!exactPoint)st.snaps=[['launch',0],['+10m',600],['+1h',3600],['+6h',21600],['+1d',86400],['now',null]].map(([key,o])=>({k:SNAP_LABEL[key]||key,key,t:o==null?now:st.launch.firstTime+o,launch:st.launch.firstTime,viaInvestigate:true}));
  else if(st.tokenPrep)st.snaps=[];
  else if(exactPoint)st.snaps=[];
  else st.snaps=[['−7d',7*86400],['−1d',86400],['−6h',21600],['−1h',3600],['Now',0]].map(([k,o])=>({k,t:now-o,o}));   // no launch either: relative to now
  st.snap=Math.max(0,st.snaps.findIndex(x=>x.key==='now'));if(!st.snaps[st.snap]?.key)st.snap=Math.max(0,st.snaps.length-1);st.tab='holders';
  renderAll();ask?.intro();loadSnap(st.snap);loadFresh();loadCredits();
 }
 // a moment: holders-at when served; linked groups (investigate clusters) layered on when that range is light enough
 async function loadSnap(k){const s=st.snaps[k];if(!s)return null;
  if(st.shares[k]?.holders){if(!st.shares[k].groupsTried){st.shares[k].groupsTried=true;groupsAt(k);}return st.shares[k];}
  if(st.shares[k])return st.shares[k];
  if(st.tokenPrep){const r=st.shares[k]={available:false,holders:[],reason:st.hold?.reason||st.hold?.heldReason};if(!off()){renderCanvas();renderRows();renderScope();}return r;}
  const now=Math.floor(Date.now()/1000),L=s.launch;
  const from=L?Math.max(L-60,s.t-30*86400):s.o?now-s.o:now-3600,to=L?Math.max(s.t,L+60):now,end=L?true:!s.o;
  st.shares[k]=inv(from,to).then(d=>{
   if(d.available===false)return {available:false,reason:d.reason,holders:[],groups:[],count:null};
   const pct=w=>end?w.pctTo:w.pctFrom,ws=(d.wallets||[]).filter(w=>pct(w)>0);
   if((d.wallets||[]).length&&!(d.wallets||[]).some(w=>hasShare(pct(w))))return {available:false,reason:'Balances at this moment could not be verified by the range reader.',holders:[],groups:[],count:null};
   return {d,count:null,partial:true,supplyBasis:d.supply?.basis,block:end?d.toBlock:d.fromBlock,holders:ws.map(w=>({id:w.address,label:w.name||(w.role==='venue'?'Pool':nick(w.address)+' '+tail(w.address)),kind:w.role==='venue'?'pool':w.role==='exchange'?'exchange':'wallet',category:w.category,share:pct(w),
     how:w.buyUsd>0&&w.sellUsd>0?`bought ${usd(w.buyUsd)} · sold ${usd(w.sellUsd)} in the range`:w.buyUsd>0?`bought ${usd(w.buyUsd)} in the range`:w.sellUsd>0?`sold ${usd(w.sellUsd)} in the range`:'held through the range',cluster:w.cluster,address:w.address})),
    groups:clustersOf(d,pct)};}).catch(e=>({error:e.message||'unavailable',status:e.status}));
  const r=await st.shares[k];st.shares[k]=r;if(!off()&&st.snap===k){renderCanvas();renderRows();renderTabs();}if(!off())renderScope();return r;}
 const clustersOf=(d,pct)=>(d.clusters||[]).map((c,i)=>({id:c.id,label:'Linked group '+(i+1),share:(d.wallets||[]).filter(w=>w.cluster===c.id).reduce((a,w)=>a+(pct(w)||0),0),members:c.members.map(x=>x.toLowerCase()),c})).filter(g=>g.share>0);
 const holderGraphCache=new Map();
 async function groupsAt(k){
  const r=st.shares[k];if(!r||!r.holders||off())return;
  try{
   const chain=st.token.chain;
   const connections=await readHolderConnections({market:st.token.id,holders:r.holders,block:r.block,signal,
    read:address=>{if(!holderGraphCache.has(address))holderGraphCache.set(address,api('/api/public/em/graph?'+new URLSearchParams({chain,address,hops:'1',window:'all',limit:'64'}),{signal,timeout:5000}));return holderGraphCache.get(address);}});
   if(off())return;r.connections=connections;r.groups=connections.groups.map(g=>({...g,c:{size:g.members.length,strength:'recorded transfer',summary:'Connected by recorded transfers of this exact token ending no later than the selected block. Not evidence of common ownership.',links:g.links.map(l=>({a:l.a,b:l.b,types:['token_transfer'],evidence:[l.n+' token transfers; blocks '+l.firstBlock+'–'+l.lastBlock],counter:['A transfer does not prove common ownership.']})),flags:[]}}));
   for(const h of r.holders){delete h.cluster;const g=r.groups.find(x=>x.members.includes(h.id));if(g)h.cluster=g.id;}
  }catch(e){if(e.name==='AbortError')return;r.connections={links:[],groups:[],read:0,partial:true,reason:'Recorded connection reads are temporarily unavailable.'};r.groups=[];}
  if(!off()&&st.snap===k){renderCanvas();renderRows();renderTabs();}
 }
 async function tokenTradesAt(k){const r=st.shares[k],s=st.snaps[k];if(!r||!s||r.tradesTried)return;r.tradesTried=true;
  // Transaction-event analysis is requested only when its tab is opened, not to draw holder connections.
  const from=Math.max(s.launch||0,s.t-3600),to=s.t;
  try{const d=await inv(from,to);if(d.available===false)throw new Error(d.reason||'Trade events are unavailable.');r.d=d;r.tradesScope='Trade events in the hour ending at this selected moment.';}
  catch(e){if(e.name==='AbortError')return;r.tradesError='Trade events could not be read for this hour. Open the chart workspace to choose a smaller range.';}
  if(!off()&&st.snap===k){renderRows();renderTabs();}
 }
 function setSnap(k,keep){const sn=st.snaps[k];if(sn){const chosen=sn.requestedTime!=null?{time:sn.requestedTime}:sn.block!=null?{block:sn.block}:sn.t!=null?{time:sn.t}:{};st.selectedContext=contextFor({kind:'token',asset:st.token.id,symbol:st.token.symbol},chosen);retainContext(st.selectedContext);}if(!keep&&st.playing){clearInterval(st.playing);st.playing=null;}st.snap=k;st.sel=null;renderSubject();renderBar();renderCanvas();renderTabs();renderRows();renderTimeline();renderScope();loadSnap(k);}
 // linked buying after launch, in one card at the top of the token: how much of the buying in the first 10 minutes, hour or day came
 // from linked groups, their grade, the evidence and the evidence against (bundle-summary). Wallets that share only an exchange
 // withdrawal are listed apart: they are not grouped.
 async function renderBundle(){const box=$('[data-v-bundle]');if(!box)return;box.hidden=st.mode!=='token';if(box.hidden||box.dataset.ready)return;box.dataset.ready='1';
  if(Object.keys(st.requestedSelection).length){box.innerHTML='<p class="tkv-empty">Launch-window summaries are separate from the selected holder point and were not substituted for it.</p>';return;}
  if(st.launchPrep||st.tokenPrep){box.innerHTML=`<div class="tkv-bd-h"><span class="tkv-bd-k">Linked buying after launch</span></div><p class="tkv-empty">Linked buying after launch on ${esc(CHAIN[st.token.chain]?.short||st.token.chain)}: analysis is not available.</p>`;return;}
  box.innerHTML=`<div class="tkv-bd-h"><span class="tkv-bd-k">Linked buying after launch</span></div>${loadingHTML('Reading who bought after launch…')}`;
  const b=await api('/api/public/em/bundle-summary?'+new URLSearchParams({market:st.token.id,window:'1h'}),{signal,timeout:30000,credentials:'same-origin'}).catch(e=>({available:false,reason:e.message}));if(off())return;   // per account (the cookie): Pro sees the groups
  if(!b||b.available===false||!(b.windows||[]).length){box.innerHTML=`<div class="tkv-bd-h"><span class="tkv-bd-k">Linked buying after launch</span></div><p class="tkv-empty">Linked buying after launch is unavailable. ${esc(plainWhy(b?.reason||''))}</p>`;return;}
  st.bundle=b;st.bundleWin=st.bundleWin||b.window||'1h';drawBundle(box);if(suspectedMembers().length&&st.mode==='token')renderCanvas();}
 const WIN_NAME={'10m':'first 10 minutes','1h':'first hour','1d':'first day'};
 // a read's validation text ("market must be <arbitrum|robinhood>:…") is for developers: the screen says what is not ready instead
 const plainWhy=e=>/must be|invalid|unsupported|unknown|not one of/i.test(String(e||''))?'not served for this token yet':String(e||'');
 function drawBundle(box){const b=st.bundle,w=b.windows.find(x=>x.window===st.bundleWin)||b.windows[0],name=WIN_NAME[w.window]||w.window;
  const gs=(w.groupsDetail||[]).slice().sort((x,y)=>(y.buysUsd||0)-(x.buysUsd||0)),ex=w.exchangeOnly||[],lock=b.locked;
  const top=w.window===b.window?b:null,evid=uniq((top?.evidence||gs.flatMap(g=>g.evidence||[])).map(e=>typeof e==='string'?e:e.text||'')).slice(0,4);
  const sus=w.suspected||[],hasSus=w.suspectedWallets>0;   // suspected (owner 2026-10-20): a third tier, never a group, never in pct; none = no tile
  const late=b.lateSharedWithdrawals?.wallets||0;   // shared exchange withdrawals received only after buying: told, never evidence
  const against=uniq([...(hasSus?[]:ex.map(x=>`Only a shared exchange withdrawal${x.entity?' ('+x.entity+')':` from ${short(x.funder)}`}: not grouped · ${num(x.wallets)} wallets, ${usd(x.buysUsd)} (${Number(x.pctUsd).toFixed(2)}%)`)),...(top?.counter||gs.flatMap(g=>g.counter||[])).map(e=>typeof e==='string'?e:e.text||'').filter(t=>!/only a shared exchange withdrawal/.test(t))]).slice(0,5);
  const as=b.asOf?.[st.token.chain];
  box.innerHTML=`<div class="tkv-bd-h"><span class="tkv-bd-k">Linked buying after launch</span><div class="tkv-seg sm">${b.windows.map(x=>`<button type="button" data-v-bw="${esc(x.window)}" class="${x.window===w.window?'on':''}">${esc({'10m':'10 min','1h':'1 hour','1d':'1 day'}[x.window]||x.window)}</button>`).join('')}</div>${w.grade?gradeChip(w.grade):''}</div>
   <p class="tkv-bd-t">${w.groups?`<b>${Number(w.pct).toFixed(1)}%</b> of the buys in the ${esc(name)} (${usd(w.linkedBuysUsd)} of ${usd(w.buysUsd)}) came from <b>${num(w.groups)} linked group${w.groups===1?'':'s'}</b> (${num(w.wallets)} wallets).`
    :`No linked groups among the ${num(w.buyers)} buyer${w.buyers===1?'':'s'} in the ${esc(name)} (${usd(w.buysUsd)} of buys).`}${hasSus?` <span class="tkv-sus-t">Suspected, shown apart: <b>${Number(w.suspectedPct).toFixed(1)}%</b> (${usd(w.suspectedBuysUsd)}) from ${num(w.suspectedWallets)} wallets that share an exchange withdrawal address.</span>`
    :late?` <span class="tkv-sus-t">No suspected wallets: ${num(late)} buyers share an exchange withdrawal address, but each received that money only after buying, so it is not evidence.</span>`:''}</p>
   <div class="tkv-bd-st"><div><b>${Number(w.pct||0).toFixed(1)}%</b><span>of buys in USD</span></div><div><b>${w.pctBase!=null?Number(w.pctBase).toFixed(1)+'%':'—'}</b><span>of tokens bought</span></div><div><b>${num(w.groups)} · ${num(w.wallets)}</b><span>groups · wallets</span></div>${hasSus?`<div class="sus"><b>${Number(w.suspectedPct||0).toFixed(1)}%</b><span>suspected · ${num(w.suspectedWallets)} wallets ${SUS_I}</span></div>`:''}<div><b>${num(w.buyers)}</b><span>buyers · ${num(w.buys)} buys</span></div></div>
   ${lock?`<div class="tkv-lock"><span aria-hidden="true">🔒</span><div><b>Which wallets are in each group, and the evidence for and against: on Pro.</b><p>${esc(lock.reason||'Your plan shows the numbers.')}</p></div><a class="tk-btn primary sm" href="#/pricing">See on Pro</a></div>`:''}
   ${gs.length?`<ul class="tkv-bd-gs">${gs.slice(0,5).map((g,i)=>`<li>${gradeChip(g.grade||'inferred')}<b>Group ${i+1}</b><span>${num(g.wallets)} wallets · ${usd(g.buysUsd)} (${Number(g.pctUsd).toFixed(2)}%)</span><small>${esc((g.evidence||[])[0]||g.strength||'')}</small></li>`).join('')}</ul>`:''}
   ${sus.length?`<ul class="tkv-bd-gs sus">${sus.slice(0,4).map(x=>`<li><span class="tkv-sus-chip" title="${esc(SUS_WHY)}">Suspected</span><b>${num(x.wallets)} wallets</b><span>${usd(x.buysUsd)} (${Number(x.pctUsd).toFixed(2)}%) · shared withdrawal from ${esc(x.entity||'exchange wallet '+short(x.funder))}</span><small>${esc(susFacts(x))}</small></li>`).join('')}</ul>`:''}
   ${lock?'':`<div class="tkv-bd-cols"><div><span class="tkv-bd-k">Evidence</span><ul>${evid.map(e=>`<li>${esc(e)}</li>`).join('')||'<li>None in this window.</li>'}</ul></div><div><span class="tkv-bd-k">Against · not grouped</span><ul class="ag">${against.map(e=>`<li>⚖ ${esc(e)}</li>`).join('')||'<li>None recorded.</li>'}</ul></div></div>`}
   <p class="tkv-bd-f">Grouping is by links and timing, never a shared funder alone, and it is not proof of one owner.${hasSus?' Suspected wallets share only an exchange withdrawal address: they are never grouped and not counted in the linked share.':''}${b.fundingRule?' Funding counts only when it came before the wallet\'s first buy.':''} ${b.buyersConsidered!=null?`The ${num(b.buyersConsidered)} largest of ${num(b.buyersTotal)} first-day buyers were checked`:''}${(b.hubBuyersExcluded||[]).length?`; ${num(b.hubBuyersExcluded.length)} hub left out`:''}. ${b.usdBasis?.eth?'USD: '+esc(b.usdBasis.eth)+'.':''}${as?` Data as of block ${Number(as.block).toLocaleString('en-US')} · ${esc(String(as.timeUtc||'').replace('T',' ').slice(0,16))} UTC.`:''}</p>`;
  box.querySelectorAll('[data-v-bw]').forEach(x=>x.onclick=()=>{st.bundleWin=x.dataset.vBw;drawBundle(box);});}
 const SUS_WHY='Shares an exchange withdrawal address with other early buyers. Thousands of people withdraw from the same exchange, so this alone is not evidence of one owner.';
 const SUS_I=`<i class="tkv-sus-i" title="${esc(SUS_WHY)}">ⓘ</i>`;
 // the wallets of the suspected sets (first-day window, Pro detail), for the holder map's dashed rings
 function suspectedMembers(){const w=(st.bundle?.windows||[]).find(x=>x.window==='1d');return (w?.suspected||[]).flatMap(x=>x.members||[]).map(a=>a.toLowerCase());}
 // what the per-wallet signals say, as facts: funded before or after buying, a burst, funds sent back to the exchange
 function susFacts(x){const sg=x.signals||[],n=sg.length;if(!n)return SUS_WHY;
  const before=sg.filter(y=>y.fundedBeforeFirstBuy).length,after=sg.filter(y=>y.fundedBeforeFirstBuy===false),back=sg.filter(y=>(y.signals||[]).includes('returned')).length,burst=sg.filter(y=>(y.signals||[]).includes('burst')).length;
  const days=after.map(y=>(y.firstFunding-y.firstBuy)/86400).filter(d=>d>0),span=days.length?` ${Math.round(Math.min(...days))}–${Math.round(Math.max(...days))} days after buying`:'';
  return [before?`${before} of ${n} funded by this address before buying`:`None funded by this address before buying${span?` (first transfer${span})`:''}`,burst?`${burst} funded within 10 minutes of each other`:'',back?`${back} sent funds back to this exchange after buying`:'None sent funds back to this exchange',
   `signals: ${x.strength?.stronger||0} stronger · ${x.strength?.some||0} some · ${x.strength?.weak||0} weak`].filter(Boolean).join(' · ');}
 const uniq=xs=>{const seen=new Set();return xs.filter(x=>{const k=String(x).toLowerCase();if(!x||seen.has(k))return false;seen.add(k);return true;});};
 function renderTokenSubject(s){const t=st.token,L=st.launch,now=st.shares?.[st.snap],selected=Object.keys(st.selectedContext?.selection||st.requestedSelection).length>0;
  s.innerHTML=`${tokenImage({...t,network:t.chain,mint:t.token},'tk-logo xl',46)}<div class="tkv-who"><h1>${esc(t.symbol)} ${t.name&&t.name!==t.symbol?`<span class="tk-muted">${esc(t.name)}</span>`:''} ${chainChip(t.chain,{full:true})}</h1>
   <div class="tkv-addr"><span class="tk-addr">${esc(t.token)}</span></div><div class="tkv-chips"><span class="tk-chip" title="${esc(L?.timeBasis||'')}">${L||st.hold?.launch?.time?'Launched '+new Date((L?.firstTime||st.hold.launch.time)*1000).toISOString().replace('T',' ').slice(0,19)+' UTC · block '+num(L?.firstBlock||st.hold.launch.block):'Launch time unavailable'}</span>
    ${L?.creation?.from?`<span class="tk-chip">Creator <a class="tk-link tk-addr" href="#/investigate/${esc(L.creation.from)}">${esc(short(L.creation.from))}</a></span>`:''}${selected?'<span class="tk-chip">Chart: selected context must be applied first</span>':`<a class="tk-chip" href="#/console/workspace/${encodeURIComponent(t.id).replace(/%3A/gi,':')}">Open the chart workspace ›</a>`}</div></div>
   <div class="tkv-stats"><div><b>${!selected&&t.price!=null?'$'+Number(t.price).toPrecision(3):'—'}</b><span>${selected?'Price at requested selection unavailable':'Latest market quote'}</span></div><div><b>${now?.count!=null?num(now.count):st.tokenPrep?'Unavailable':PREP}</b><span>Holders at selected moment</span></div><div><b>${L?.creation?.from&&now?.holders?shareText(now.holders.find(h=>h.isCreator)?.share):'—'}</b><span>Creator at selected moment</span></div></div>
   <div class="tkv-acts">${watchButton({kind:'token',chain:t.chain,target:t.id,label:t.symbol})}${keepButtons()}</div>`;bindKeep(s);}
 function renderTokenBar(b){const s=st.snaps[st.snap];if(!s){b.innerHTML='<span class="tkv-crumb">Holder history</span>';return;}b.innerHTML=`<div class="tkv-seg"><button type="button" class="on">Token chart</button></div><span class="tkv-crumb">Recorded holders of <b>${esc(st.token.symbol)}</b> at <b>${esc(s.k)}</b> · ${new Date(s.t*1000).toISOString().replace('T',' ').slice(0,16)} UTC</span>`;}
 async function renderHolderCanvas(box){const r=await Promise.resolve(st.shares[st.snap]);if(off())return;
  if(!r&&!st.tokenPrep){box.innerHTML=loadingHTML('Reading who held it…');return;}
  const message=holderMessage(st.hold,r,CHAIN[st.token.chain]?.short||st.token.chain);
  if(message){box.innerHTML=`<div class="tkv-stagemsg" data-holder-state="${esc(message.kind)}"><b>${esc(message.title)}</b><p>${esc(message.detail)}</p></div>`;return;}
  // Holder topology map removed — token price chart here; holders remain in tabs.
  box.classList.add('chart');
  const host=document.createElement('div');host.className='tkv-chart';box.append(host);
  box.insertAdjacentHTML('beforeend','<p class="tkv-empty">Holder topology map removed. Holders stay in the tabs below.</p>');
  try{const mod=await import('./explorer-investigate-chart.js');chartApi=await mod.mountMarketChart(host,{market:st.token.id,compact:false,interval:st.priceInterval||'1h',snaps:st.snaps,cursor:st.snap,onPick:k=>setSnap(k),signal});}catch(e){host.textContent=String(e.message||e);}
 }
 function renderTimeline(){const tl=$('[data-v-tl]');tl.hidden=st.mode!=='token';if(tl.hidden)return;
  if(st.requestedSelection.block!=null&&!st.snaps.length&&!st.hold){tl.innerHTML=loadingHTML('Resolving the selected block time for its price chart…');return;}
  if(!tl.dataset.ready){const epoch=st.timelineEpoch=(st.timelineEpoch||0)+1;tl.dataset.ready='1';tl.innerHTML=`<div class="tkv-tl-bar"><button type="button" class="tkv-play" data-v-play aria-label="Play the moments">▶</button><h4>Token price</h4><div class="tkv-snaps" data-v-intervals>${['5m','1h','1d'].map(tf=>`<button type="button" data-v-tf="${tf}" aria-pressed="${(st.priceInterval||'1h')===tf}" class="${(st.priceInterval||'1h')===tf?'on':''}">${tf}</button>`).join('')}</div><div class="tkv-snaps" data-v-snaps></div></div><p class="tkv-tlnote" data-v-tlnote></p><div class="tkv-tl-chart" data-v-tlc></div>`;
   tl.querySelector('[data-v-play]').onclick=()=>{if(st.playing){clearInterval(st.playing);st.playing=null;renderTimeline();return;}
    if(st.snap===st.snaps.length-1)st.snap=0;setSnap(st.snap);st.playing=setInterval(()=>{if(st.snap>=st.snaps.length-1||off()){clearInterval(st.playing);st.playing=null;renderTimeline();return;}setSnap(st.snap+1,true);},2200);renderTimeline();};
   tl.querySelectorAll('[data-v-tf]').forEach(b=>b.onclick=()=>{st.priceInterval=b.dataset.vTf;timeline?.destroy();timeline=null;delete tl.dataset.ready;renderTimeline();});
   const requested=holderChartRange(st.requestedSelection,st.snaps),win=requested.selected?requested.window:null,host=tl.querySelector('[data-v-tlc]');
   if(requested.selected&&requested.available===false){host.innerHTML=emptyHTML(requested.reason);}
   else{if(requested.selected)tl.querySelector('[data-v-tlnote]').textContent=`Price chart range: ${new Date(win.from*1000).toISOString()} → ${new Date(win.to*1000).toISOString()}. A point selection shows its preceding 24 hours; holder reads retain the exact selected point.`;
    import('./explorer-investigate-chart.js').then(m=>m.mountMarketChart(host,{market:st.token.id,compact:true,interval:st.priceInterval||requested.interval||'1h',window:win,snaps:st.snaps,cursor:st.snap,onPick:k=>setSnap(k),signal}))
     .then(api=>{if(off()||epoch!==st.timelineEpoch){api?.destroy();return;}timeline=api;if(api?.empty){if(requested.selected){host.innerHTML=emptyHTML('Price bars are unavailable in this selected historical range. No latest chart was substituted.');}else host.innerHTML=emptyHTML('No recorded price bars for this exact token yet.');}else if(api?.note)tl.querySelector('[data-v-tlnote]').textContent=api.note;}).catch(e=>{if(!off()&&epoch===st.timelineEpoch)host.innerHTML=errorHTML(e.message||'The chart could not load.',false);});}}
  tl.querySelector('[data-v-play]').textContent=st.playing?'❚❚':'▶';
  tl.querySelector('[data-v-snaps]').innerHTML=st.snaps.map((s,k)=>`<button type="button" data-v-k="${k}" class="${k===st.snap?'on':''}">${esc(s.k)}</button>`).join('');
  tl.querySelectorAll('[data-v-k]').forEach(b=>b.onclick=()=>setSnap(+b.dataset.vK));const play=tl.querySelector('[data-v-play]');play.hidden=st.snaps.length<2;tl.querySelector('[data-v-intervals]').style.marginLeft='auto';timeline?.update({cursor:st.snap,snaps:st.snaps});}
 // no bars for the launch window in the chart archive: the six moments on a time strip (the first hour gets the most room), honest
 // about the missing price; clicking anywhere picks the nearest moment, as on the chart
 function strip(host,cov){const W=1000,X=t=>{const L=st.snaps[0].launch,m=(t-L)/60,seg=[[-10,60,.02,.4],[60,360,.4,.64],[360,1440,.64,.86]];
   for(const [a,b,x0,x1] of seg)if(m<=b)return W*(x0+(x1-x0)*Math.max(0,m-a)/(b-a));return W*.97;};
  const draw=()=>{host.innerHTML=`<div class="tkv-strip"><svg viewBox="0 0 ${W} 120" preserveAspectRatio="none" aria-label="Moments since launch">
    <line x1="0" x2="${W}" y1="88" y2="88" class="ax"/>${st.snaps.map((s,i)=>`<g class="mk${i===st.snap?' on':''}" data-v-k="${i}"><line x1="${X(s.t)}" x2="${X(s.t)}" y1="14" y2="88"/><text x="${X(s.t)+(s.key==='now'?-4:4)}" y="26" text-anchor="${s.key==='now'?'end':'start'}">${esc(s.k)}${s.key==='now'?' →':''}</text><text class="t" x="${X(s.t)+(s.key==='now'?-4:4)}" y="106" text-anchor="${s.key==='now'?'end':'start'}">${s.key==='now'?day(s.t):new Date(s.t*1000).toISOString().slice(11,16)}</text></g>`).join('')}</svg>
    <p>Price for the launch day is not in the chart archive yet${cov?` (it starts ${day(cov)})`:''}: ${PREP}. Select a recorded moment to view its holder balances.</p></div>`;
   host.querySelector('svg').onclick=e=>{const r=e.currentTarget.getBoundingClientRect(),x=(e.clientX-r.left)/r.width*W;let best=0,bd=1e9;st.snaps.forEach((s,i)=>{const d=Math.abs(X(s.t)-x);if(d<bd){bd=d;best=i;}});setSnap(best);};};
  draw();return {update(){draw();},destroy(){}};}
 function renderTokenTabs(t){
  Promise.resolve(st.shares[st.snap]).then(x=>{const tabs=[['holders','Holders',st.tokenPrep||x?.available===false?'—':x?.count??x?.holders?.length??'…'],['groups','Connections',x?.connections?x.connections.links.length:st.tokenPrep?'—':'…'],['trades','Trade events',x?.d?.events?.length??(st.tokenPrep||x?.tradesError?'—':'')],['launch','Launch',st.launch?(st.launch.firstTransfers||[]).length:'—']];
   t.innerHTML=tabs.map(([k,n,c])=>`<button type="button" role="tab" data-v-tab="${k}" class="${st.tab===k?'on':''}">${n}<span>${c}</span></button>`).join('');
   t.querySelectorAll('[data-v-tab]').forEach(b=>b.onclick=()=>{st.tab=b.dataset.vTab;if(st.tab==='trades')tokenTradesAt(st.snap);renderTokenTabs(t);renderRows();});});}
 async function renderTokenRows(box){box.innerHTML=loadingHTML('Reading holders…');const r=await Promise.resolve(st.shares[st.snap]);if(off())return;
  const message=holderMessage(st.hold,r,CHAIN[st.token.chain]?.short||st.token.chain);
  if(message){box.innerHTML=`<p class="tkv-empty">${esc(message.title)}. ${esc(message.detail)}</p>`;return;}
  if(!r){box.innerHTML=loadingHTML('Reading holders…');return;}
  const prev=st.snap>0?await Promise.resolve(st.shares[st.snap-1]):null,pv=Object.fromEntries((prev?.holders||[]).map(h=>[h.id,h.share]));
  if(st.tab==='holders'){const rows=r.holders.slice().sort((a,b)=>hasShare(a.share)&&hasShare(b.share)?b.share-a.share:(b.amount??0)-(a.amount??0));
   box.innerHTML=`<div class="tk-table-wrap"><table class="tkv-htbl"><thead><tr><th>#</th><th>Holder</th><th class="r">Amount</th><th>Share of supply</th><th class="r">Change</th><th>Who</th><th></th></tr></thead><tbody>${rows.map((h,i)=>{
    const d=prev&&hasShare(pv[h.id])&&hasShare(h.share)?h.share-pv[h.id]:null;
    return `<tr class="ck${st.sel===h.id?' sel':''}" data-v-h="${esc(h.id)}"><td class="tk-muted">${i+1}</td><td><span class="tkv-tok">${h.kind==='pool'?'<img class="tk-logo" src="/assets/em/kinds/dex-soft.svg" alt="">':groupIcon(h.category,20)||avatar(h.id,20)}<a class="tk-link" href="#/investigate/${esc(h.address)}?kind=wallet&chain=${esc(st.token.chain)}">${esc(h.label)}</a>${h.cluster?' '+gradeChip('proven').replace('Proven','Connected'):''}</span></td>
     <td class="r tk-num">${h.amount!=null?num(h.amount):'—'}</td><td class="tk-num">${hasShare(h.share)?`<span class="tkv-shbar"><i style="width:${Math.min(100,h.share)}%"></i></span>`:''}${shareText(h.share)}</td><td class="r tk-num ${d>0.005?'tkv-up':d<-0.005?'tkv-down':'tk-muted'}">${d==null?'—':(d>0?'▲ +':d<0?'▼ ':'')+d.toFixed(2)}</td><td class="tk-muted">${esc(h.how)}</td>
     <td class="r">${h.kind==='pool'?'':`<a class="tkv-go" href="#/investigate/${esc(h.address)}?kind=wallet&chain=${esc(st.token.chain)}">Investigate ›</a>`}</td></tr>`;}).join('')}</tbody></table></div><p class="tkv-empty">${esc(supplyNote(r))} ${r.partial?`Shown: wallets that traded around this moment. Every holder: ${PREP}.`:`${r.count!=null?num(r.count)+' holders at this moment; the top '+rows.length+' shown':''} · source: ${esc(r.source||'')}`}</p>`;
   box.querySelectorAll('[data-v-h]').forEach(x=>x.onclick=e=>{if(e.target.closest('a'))return;st.sel=st.sel===x.dataset.vH?null:x.dataset.vH;renderCanvas();renderTokenRows(box);});return;}
  if(st.tab==='groups'){
   const c=r.connections;if(!c){box.innerHTML=loadingHTML('Reading recorded token transfers between shown holders…');return;}
   if(!c.links.length){box.innerHTML=`<p class="tkv-empty">${esc(c.reason||'No connection was returned in this bounded sample. This does not prove these holders are unrelated.')} ${c.read||0} sampled holder reads.</p>`;return;}
   box.innerHTML=`<p class="tkv-empty">${c.links.length} recorded token-flow connections · ${r.groups.length} transfer groups. Only transfers ending by selected block ${num(r.block)} are shown. Pools and exchanges are displayed but do not merge holder groups. A connection does not prove common ownership.</p>`+r.groups.map(g=>`<div class="tkv-group"><div class="tkv-gh"><b style="color:${g.color}">${esc(g.label)} · ${g.members.length} wallets</b><span class="tk-muted">${hasShare(g.share)?shareText(g.share,2)+' of supply':'Supply share unavailable'}</span></div><p class="tk-muted">${g.members.map(a=>esc(short(a))).join(' · ')}</p></div>`).join('')+`<div class="tk-table-wrap"><table class="tkv-htbl"><thead><tr><th>From</th><th>To</th><th>Transfers</th><th>Token amount</th><th>Last recorded block</th></tr></thead><tbody>${c.links.map(l=>`<tr><td><a class="tk-link" href="#/investigate/${esc(l.a)}?kind=wallet&chain=${esc(st.token.chain)}">${esc(short(l.a))}</a></td><td><a class="tk-link" href="#/investigate/${esc(l.b)}?kind=wallet&chain=${esc(st.token.chain)}">${esc(short(l.b))}</a></td><td>${num(l.n)}</td><td>${num(l.amount)} ${esc(st.token.symbol)}</td><td>${num(l.lastBlock)}</td></tr>`).join('')}</tbody></table></div>`;return;
  }
  if(st.tab==='launch'){const L=st.launch;box.innerHTML=L?`<div class="tk-table-wrap"><table class="tkv-htbl"><thead><tr><th>Transfer</th><th>From</th><th>To</th><th class="r">Amount</th><th class="r">Of reference supply</th><th>Tx</th></tr></thead><tbody>${(L.firstTransfers||[]).map((x,i)=>`<tr><td class="tk-muted">${i+1}</td><td>${addrCell(x.from)}</td><td>${addrCell(x.to)}</td><td class="r tk-num">${num(x.amount)}</td><td class="r tk-num">${shareText(x.pctSupply,4)}</td><td class="tk-addr tk-muted">${esc(short(x.tx))}</td></tr>`).join('')}</tbody></table></div><p class="tkv-empty">From the creation transaction ${esc(short(L.creation?.tx||''))} at block ${num(L.firstBlock)} · ${esc(L.timeBasis||'')}. Percentages use the launch reader’s reference supply, not historical supply at each transfer.</p>`:`<p class="tkv-empty">Launch details unavailable. ${esc(st.launchReason||'')}</p>`;return;}
  if(!r.d){box.innerHTML=r.tradesError?`<p class="tkv-empty">${esc(r.tradesError)}</p>`:loadingHTML('Reading trade events for the selected hour…');return;}
  const ev=(r.d.events||[]).slice().sort((a,b)=>a.time-b.time);
  box.innerHTML=(r.tradesScope?`<p class="tkv-empty">${esc(r.tradesScope)}</p>`:'')+(ev.length?`<div class="tk-table-wrap"><table class="tkv-htbl"><thead><tr><th>When (UTC)</th><th>What</th><th>Wallet</th><th class="r">USD</th><th>Tx</th></tr></thead><tbody>${ev.map(e=>`<tr><td class="tk-num">${new Date(e.time*1000).toISOString().replace('T',' ').slice(5,16)}</td><td><span class="tkv-how ${/buy/.test(e.kind)?'buy':/sell/.test(e.kind)?'sell':'recv'}">${esc(e.kind.replace(/_/g,' '))}</span></td>
   <td>${(e.wallets||[]).slice(0,2).map(w=>`<a class="tk-link" href="#/investigate/${esc(w)}">${esc(short(w))}</a>`).join(' → ')}</td><td class="r tk-num">${usd(e.usd)}</td><td class="tk-addr tk-muted">${esc(short(e.tx||''))}</td></tr>`).join('')}</tbody></table></div>`:'<p class="tkv-empty">No trade events returned in this hour.</p>');}

 const addrCell=a=>a==='0x0000000000000000000000000000000000000000'?'<span class="tk-muted">mint</span>':`<a class="tk-link tk-addr" href="#/investigate/${esc(a)}">${esc(short(a))}</a>`;
 function renderAll(){renderScope();renderSubject();renderBundle();renderBar();renderCanvas();renderTabs();renderRows();renderTimeline();}
 let rz=0;const onResize=()=>{clearTimeout(rz);rz=setTimeout(()=>{if(st.mode==='wallet'&&st.chartOf)renderCanvas();},200);};addEventListener('resize',onResize);
 const onKey=e=>{if(e.key==='Escape'&&st.edge){st.edge=null;renderCanvas();}};addEventListener('keydown',onKey);
 return ()=>{removeEventListener('resize',onResize);removeEventListener('keydown',onKey);clearInterval(st.playing);chartApi?.destroy();timeline?.destroy();ask?.destroy?.();};
}

let styled=false;
function injectInvestigateStyles(){if(styled)return;styled=true;const s=document.createElement('style');s.textContent=`
.tkx-page:has(.tkv){padding:0;background:var(--tk-bg-sub)}
.tkv{display:grid;grid-template-columns:minmax(0,1fr) 420px;min-height:calc(100vh - 64px)}
.tkv-left{min-width:0;padding:18px 22px 40px}
.tkv-right{border-left:1px solid var(--tk-line);background:var(--tk-surface);position:sticky;top:64px;height:calc(100vh - 64px);display:flex;flex-direction:column;min-height:0}
.tkv-subj{display:flex;align-items:center;gap:14px;padding:16px 18px;flex-wrap:wrap}
.tkv-who{min-width:0;flex:1}.tkv-who h1{font:700 21px var(--tk-display);display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0}
.tkv-addr{display:flex;gap:8px;align-items:center;font-size:12.5px;color:var(--tk-muted);margin-top:2px;flex-wrap:wrap}.tkv-copy{border:0;background:none;color:var(--tk-link);font-weight:600;cursor:pointer;font-size:12px}
.tkv-chips,.tkv-pills{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:8px}.tkv-violet{background:#f3edff;color:#7c3aed}
.tkv-cpill{display:inline-flex;align-items:center;gap:6px;height:26px;padding:0 10px;border-radius:8px;border:1px solid var(--tk-line-strong);background:var(--tk-surface);font:600 12.5px var(--tk-font);color:var(--tk-ink);cursor:pointer}
.tkv-cpill:hover{border-color:var(--tk-accent)}.tkv-cpill i{width:9px;height:9px;border-radius:50%}.tkv-cpill b{color:var(--tk-muted)}.tkv-pnote{font-size:12px}
.tkv-stats{display:flex;gap:22px;margin-left:auto}.tkv-stats div{display:grid;text-align:right}.tkv-stats b{font:700 18px var(--tk-display)}.tkv-stats span{font-size:12px;color:var(--tk-muted)}
.tkv-acts{display:flex;gap:8px;flex-wrap:wrap}.tkv-expw{position:relative}.tkv-expm{position:absolute;right:0;top:calc(100% + 6px);z-index:30;display:grid;min-width:230px;padding:6px}.tkv-expm[hidden]{display:none}
.tkv-expm button{border:0;background:none;text-align:left;padding:8px 10px;border-radius:8px;font:500 13px var(--tk-font);color:var(--tk-ink);cursor:pointer}.tkv-expm button:hover{background:var(--tk-hover)}
.tkv-lock{display:flex;align-items:center;gap:12px;margin-top:12px;padding:12px 14px;border:1px dashed var(--tk-line-strong);border-radius:12px;background:var(--tk-bg-sub)}.tkv-lock p{margin:2px 0 0;font-size:12.5px;color:var(--tk-muted)}.tkv-lock .tk-btn{margin-left:auto}
.tkv-saved{margin-bottom:14px;padding:12px 16px}.tkv-sv-h{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.tkv-sv-h a{margin-left:auto}.tkv-sv-k{font:700 11px var(--tk-font);letter-spacing:.06em;text-transform:uppercase;color:var(--tk-accent)}
.tkv-sv-c{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;margin-top:8px}.tkv-inc{border-top:1px solid var(--tk-line);padding:8px 0}.tkv-inc summary{display:flex;gap:8px;align-items:center;flex-wrap:wrap;cursor:pointer}.tkv-inc ul{margin:6px 0 0;padding-left:18px;display:grid;gap:3px;font-size:13px}
.tkv-ava{border-radius:50%;flex:none;display:inline-block}
.tkv-prep{display:inline-flex;align-items:center;height:20px;padding:0 8px;border-radius:999px;font-size:11px;font-weight:600;color:var(--tk-warn);background:var(--tk-warn-soft);white-space:nowrap;vertical-align:middle}
.tkv-canvas{margin-top:14px;overflow:hidden}.tkv-lower{margin-top:14px;overflow:hidden}
.tkv-bar{display:flex;align-items:center;gap:10px;padding:10px 12px;border-bottom:1px solid var(--tk-line);flex-wrap:wrap}
.tkv-seg{display:inline-flex;background:var(--tk-surface-2);border-radius:10px;padding:3px}.tkv-seg button{border:0;background:none;height:28px;padding:0 12px;border-radius:8px;font:600 13px var(--tk-font);color:var(--tk-muted);cursor:pointer}
.tkv-seg button.on{background:var(--tk-surface);color:var(--tk-ink);box-shadow:var(--tk-shadow)}.tkv-seg button:disabled{opacity:.45;cursor:not-allowed}
.tkv-crumb{font-size:13px;color:var(--tk-muted);display:flex;align-items:center;gap:6px;min-width:0}.tkv-crumb b{color:var(--tk-ink)}
.tkv-filters{margin-left:auto;display:flex;gap:6px;flex-wrap:wrap}.tkv-filters button{border:1px solid var(--tk-line-strong);cursor:pointer}.tkv-filters button.off{opacity:.4;text-decoration:line-through}
.tkv-g{display:inline-flex;align-items:center;gap:5px;height:22px;padding:0 7px;border-radius:6px;font:700 11px var(--tk-font);white-space:nowrap;background:var(--tk-surface-2)}
.tkv-g.proven{color:var(--tk-up);background:var(--tk-up-soft)}.tkv-g.matched{color:#2563eb;background:var(--tk-info-soft)}.tkv-g.inferred{color:var(--tk-warn);background:var(--tk-warn-soft)}.tkv-g.judged{color:#7c3aed;background:#f3edff}.tkv-g.used{color:var(--tk-muted)}
.tkv-body{position:relative;background:#05070d;height:470px;overflow:hidden}.tkv-body>svg{width:100%;height:100%;display:block}.tkv-body.chart{background:var(--tk-surface);height:auto}
.tkv-body .tkx-state{color:#8d96a8;justify-content:center;height:100%}
.tkv-lane{font:700 10.5px var(--tk-font);letter-spacing:1.2px;fill:#8d96a8}
.tkv-node{cursor:pointer}.tkv-dim{opacity:.18;transition:opacity .2s}.tkv-edge{transition:opacity .2s}
.tkv-lbl{font:600 11px var(--tk-font);fill:#cfd5e2;pointer-events:none;paint-order:stroke;stroke:#05070d;stroke-width:4px;stroke-linejoin:round}.tkv-lbl.s{font-weight:500;fill:#8d96a8;font-size:10.5px}
.tkv-glbl{fill:#cfcbff;font:700 12px var(--tk-font)}.tkv-glbl.s{font-weight:500;fill:#a5a0ff;font-size:11px}.tkv-pool{fill:#e8ebf1;font:700 12px var(--tk-font)}.tkv-pool.b{font:700 15px var(--tk-display)}.tkv-pct{fill:#05070d;font:800 11px var(--tk-display)}
.tkv-legend{position:absolute;left:12px;bottom:10px;display:flex;flex-wrap:wrap;gap:4px 14px;font-size:11.5px;color:#8d96a8;pointer-events:none}.tkv-legend span{display:inline-flex;gap:6px;align-items:center}
.tkv-stats.tkv-stats{position:absolute;right:12px;top:10px}
.tkv-body .tkv-stats{position:absolute;right:12px;top:10px;display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end;max-width:70%;margin:0}
.tkv-body .tkv-stats span{background:rgba(13,17,27,.85);border:1px solid #1c2230;color:#e8ebf1;border-radius:8px;padding:4px 9px;font-size:12px}.tkv-body .tkv-stats .hl{border-color:#a5a0ff;color:#cfcbff}.tkv-body .tkv-stats .sus{border:1px dashed #f5b041;color:#f8d08a;cursor:help}
.tkv-stagemsg{color:#cfd5e2;padding:60px 28px;text-align:center}.tkv-stagemsg p{color:#8d96a8;margin-top:6px}
.tkv-tip{position:absolute;z-index:6;pointer-events:none;background:var(--tk-surface);color:var(--tk-ink);border:1px solid var(--tk-line-strong);border-radius:10px;padding:8px 10px;font-size:12px;box-shadow:var(--tk-shadow-pop);max-width:320px}
.tkv-tip b{display:block;font-size:12.5px;margin-top:2px}
.tkv-ch-top{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:10px 14px}.tkv-ch-top h4{margin:0;display:flex;align-items:center;gap:8px;font:700 15px var(--tk-display)}
.tkv-tg{margin-left:auto;display:flex;gap:6px;align-items:center;font-size:12.5px;color:var(--tk-muted)}
.tkv-switch{width:34px;height:20px;border-radius:999px;background:var(--tk-line-strong);position:relative;border:0;padding:0;cursor:pointer}.tkv-switch::after{content:"";position:absolute;left:2px;top:2px;width:16px;height:16px;border-radius:50%;background:#fff;transition:.15s}
.tkv-switch.on{background:var(--tk-accent)}.tkv-switch.on::after{left:16px}
.tkv-chart{height:420px;position:relative}.tkv-chart-canvas{position:relative;height:100%}.tkv-ch-leg{display:flex;gap:14px;flex-wrap:wrap;padding:8px 14px 12px;font-size:12px;color:var(--tk-muted)}
.tkv-up{color:var(--tk-up)}.tkv-down{color:var(--tk-down)}
.tkv-marks{position:absolute;pointer-events:none;z-index:4}.tkv-mk{position:absolute;transform:translate(-50%,-50%);pointer-events:auto;border:0;background:none;padding:0;font:700 15px/1 var(--tk-font);cursor:pointer}
.tkv-mk.buy{color:var(--tk-up)}.tkv-mk.sell{color:var(--tk-down)}.tkv-mk.recv,.tkv-mk.sent{color:var(--tk-muted)}.tkv-mk.faded{opacity:.4}
.tkv-snap{position:absolute;top:0;bottom:0;border-left:1px dashed var(--tk-line-strong)}.tkv-snap b{position:absolute;top:2px;left:4px;font:700 10.5px var(--tk-font);color:var(--tk-muted)}.tkv-snap.on{border-left:2px solid var(--tk-accent)}.tkv-snap.on b{color:var(--tk-accent)}
.tkv-tl{border-top:1px solid var(--tk-line);background:var(--tk-surface)}.tkv-tl-bar{display:flex;align-items:center;gap:10px;padding:10px 12px 0;flex-wrap:wrap}.tkv-tl-bar h4{margin:0;font:700 13px var(--tk-font)}
.tkv-snaps{display:flex;gap:4px;flex-wrap:wrap;margin-left:auto}.tkv-snaps button{border:1px solid var(--tk-line-strong);background:var(--tk-surface);height:28px;padding:0 10px;border-radius:8px;font:600 12.5px var(--tk-font);color:var(--tk-ink-2);cursor:pointer}
.tkv-snaps button.on{background:var(--tk-accent);border-color:var(--tk-accent);color:var(--tk-accent-ink)}.tkv-play{width:28px;height:28px;border-radius:8px;border:1px solid var(--tk-line-strong);background:var(--tk-surface);cursor:pointer}
.tkv-tl-chart{height:190px;position:relative}.tkv-tlnote{margin:0;padding:4px 14px 0;font-size:11.5px;color:var(--tk-faint)}.tkv-tl{padding-bottom:22px}.tkv-tlnote:empty{display:none}
.tkv-strip svg{width:100%;height:120px;display:block;cursor:pointer}.tkv-strip .ax{stroke:var(--tk-line-strong)}.tkv-strip .mk line{stroke:var(--tk-line-strong);stroke-dasharray:3 4}
.tkv-strip .mk text{font:700 12px var(--tk-font);fill:var(--tk-muted)}.tkv-strip .mk text.t{font-weight:500;font-size:11px}.tkv-strip .mk.on line{stroke:var(--tk-accent);stroke-width:2;stroke-dasharray:none}.tkv-strip .mk.on text{fill:var(--tk-accent)}
.tkv-strip p{margin:0;padding:0 14px 12px;font-size:12px;color:var(--tk-muted)}.tkv-snap.edge{border-left-style:dotted}
.tkv-tabs{display:flex;gap:2px;padding:0 10px;border-bottom:1px solid var(--tk-line);overflow-x:auto}.tkv-tabs button{border:0;background:none;padding:12px 10px;font:600 13.5px var(--tk-font);color:var(--tk-muted);position:relative;white-space:nowrap;cursor:pointer}
.tkv-tabs button span{font-size:12px;color:var(--tk-faint);margin-left:4px}.tkv-tabs button.on{color:var(--tk-ink)}.tkv-tabs button.on::after{content:"";position:absolute;left:8px;right:8px;bottom:-1px;height:2px;background:var(--tk-accent)}
.tkv-row{display:grid;grid-template-columns:30px minmax(0,1.3fr) minmax(0,1.7fr) auto 14px;gap:12px;align-items:center;padding:11px 16px;border-bottom:1px solid var(--tk-line);cursor:pointer}
.tkv-row:hover{background:var(--tk-hover)}.tkv-row.sel{background:var(--tk-accent-soft)}.tkv-row.plain{display:block;cursor:default;padding:0}.tkv-row.plain:hover{background:none}
.tkv-nm{font-weight:600;display:grid;min-width:0}.tkv-nm small{font:11.5px var(--tk-mono);color:var(--tk-muted);font-weight:400}
.tkv-rel{font-size:13px;color:var(--tk-ink-2);min-width:0}.tkv-rel small{display:block;color:var(--tk-muted);font-size:12px}
.tkv-car{color:var(--tk-faint);transition:transform .15s}.tkv-row.open .tkv-car{transform:rotate(90deg)}
.tkv-cmono{width:26px;height:26px;border-radius:50%;display:grid;place-items:center;font:700 9px var(--tk-display);color:#fff}
.tkv-lic{display:grid;place-items:center}
.tkv-xsum{padding:12px 16px;font-size:13.5px;color:var(--tk-ink-2);background:#f3edff}.tkv-xsum b{color:#7c3aed}
.tkv-xh{padding:14px 16px 6px;font-size:12px;font-weight:700;color:var(--tk-muted);text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid var(--tk-line)}
.tkv-hold{background:var(--tk-bg-sub);border-bottom:1px solid var(--tk-line);padding:4px 16px 12px 58px}.tkv-hold.flat{background:none;border:0;padding:4px 16px 12px}
.tkv-htbl{width:100%;border-collapse:collapse;font-size:13px}.tkv-htbl th{text-align:left;font-size:11.5px;color:var(--tk-muted);font-weight:600;padding:8px 6px;border-bottom:1px solid var(--tk-line);white-space:nowrap}
.tkv-htbl td{padding:8px 6px;border-bottom:1px solid var(--tk-line);white-space:nowrap}.tkv-htbl .r{text-align:right}.tkv-htbl tr.ck{cursor:pointer}.tkv-htbl tr.ck:hover td{background:var(--tk-hover)}.tkv-htbl tr.sel td{background:var(--tk-accent-soft)}
.tkv-go{color:var(--tk-link);font-weight:600;font-size:12.5px}.tkv-tok{display:inline-flex;align-items:center;gap:8px;font-weight:600}
.tkv-how{font-size:12.5px;font-weight:600;white-space:nowrap}.tkv-how.buy{color:var(--tk-up)}.tkv-how.sell{color:var(--tk-down)}.tkv-how.recv,.tkv-how.both{color:var(--tk-muted)}.tkv-how.same{color:#2563eb}
.tkv-shbar{display:inline-block;width:70px;height:6px;border-radius:999px;background:var(--tk-surface-2);vertical-align:middle;margin-right:8px;overflow:hidden}.tkv-shbar i{display:block;height:100%;background:var(--tk-accent);border-radius:999px}
.tkv-empty{padding:12px 16px;font-size:13px;color:var(--tk-muted);margin:0}
.tkv-group{padding:14px 16px;border-bottom:1px solid var(--tk-line)}.tkv-gh{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.tkv-group ul{list-style:none;padding:0;margin:8px 0 0;display:grid;gap:6px}
.tkv-group li{display:flex;gap:8px;align-items:flex-start;flex-wrap:wrap;font-size:13px}.tkv-counter{display:block;width:100%;color:var(--tk-warn);padding-left:4px}
.tkv-mono{font-family:var(--tk-mono)}
.tkv-scope{display:flex;align-items:center;gap:6px;flex-wrap:wrap;font-size:12.5px;color:var(--tk-ink-2);padding:0 2px 10px}.tkv-scope:empty{display:none}.tkv-scope b{color:var(--tk-ink)}.tkv-scope small{color:var(--tk-muted)}
.tkv-sl{font:700 11px var(--tk-font);letter-spacing:.06em;text-transform:uppercase;color:var(--tk-accent);background:var(--tk-accent-soft);border-radius:6px;padding:2px 7px}
.tkv-si{display:inline-flex;align-items:center;gap:4px;white-space:nowrap;height:26px;padding:0 9px;border-radius:8px;background:var(--tk-surface);border:1px solid var(--tk-line)}.tkv-sbs{display:inline-flex;gap:6px;align-items:center;flex-wrap:wrap;white-space:normal}
.tkv-si:has(.tkv-sbs){height:auto;min-height:26px;padding:3px 9px}
.tkv-sb{font:600 11.5px var(--tk-mono);background:var(--tk-bg-sub);border:1px solid var(--tk-line);border-radius:6px;padding:1px 6px;color:var(--tk-ink-2)}
.tkv-xbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:8px 12px;background:#0b0f19;border-bottom:1px solid #1c2230}
.tkv-xbtns{display:flex;gap:6px;flex-wrap:wrap}.tkv-xbtns button{height:30px;padding:0 12px;border-radius:9px;border:1px solid #2a3245;background:#121828;color:#e8ebf1;font:600 12.5px var(--tk-font);cursor:pointer}
.tkv-xbtns button:hover{border-color:#a5a0ff;color:#fff}.tkv-xbtns button:disabled{opacity:.45;cursor:wait}
.tkv-xstep{margin-left:auto;display:flex;align-items:center;gap:8px;font-size:12px;color:#8d96a8}.tkv-xstep b{color:#34d399}.tkv-xstep button{height:28px;padding:0 10px;border-radius:8px;border:1px solid #2a3245;background:none;color:#cfd5e2;font:600 12px var(--tk-font);cursor:pointer}
.tkv-xbusy{color:#cfcbff}.tkv-fresh{animation:tkvpulse 1.6s ease-in-out infinite}@keyframes tkvpulse{50%{stroke-opacity:.25}}
.tkv-tiph{display:block;color:var(--tk-muted);font-size:11px;margin-top:3px}
.tkv-bundle{margin-top:14px;padding:14px 18px}.tkv-bd-h{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.tkv-bd-k{font:700 11px var(--tk-font);letter-spacing:.06em;text-transform:uppercase;color:var(--tk-muted)}
.tkv-bd-t{margin:8px 0 0;font:500 17px/1.35 var(--tk-display);color:var(--tk-ink)}.tkv-bd-t b{color:var(--tk-accent)}
.tkv-bd-st{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-top:12px}.tkv-bd-st div{background:var(--tk-bg-sub);border-radius:10px;padding:8px 10px;display:grid}.tkv-bd-st b{font:700 17px var(--tk-display)}.tkv-bd-st span{font-size:11.5px;color:var(--tk-muted)}
.tkv-bd-cols{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:12px}.tkv-bd-cols ul{list-style:none;margin:4px 0 0;padding:0;display:grid;gap:4px;font-size:12.5px;color:var(--tk-ink-2)}
.tkv-bd-cols li[data-v-bdk]{cursor:pointer;border-radius:6px;padding:2px 4px;margin-left:-4px}.tkv-bd-cols li[data-v-bdk]:hover{background:var(--tk-hover)}.tkv-bd-cols ul.ag{color:var(--tk-warn)}
.tkv-bd-f{margin:10px 0 0;font-size:11.5px;color:var(--tk-faint)}
.tkv-sus-t{display:block;margin-top:4px;font:500 14px/1.4 var(--tk-font);color:var(--tk-ink-2)}.tkv-sus-t b{color:var(--tk-warn)!important}
.tkv-bd-st div.sus{background:var(--tk-warn-soft);outline:1px dashed var(--tk-warn);outline-offset:-1px}.tkv-bd-st div.sus b{color:var(--tk-warn)}.tkv-sus-i{font-style:normal;cursor:help}
.tkv-bd-gs.sus li{border:1px dashed var(--tk-warn);background:var(--tk-warn-soft)}.tkv-sus-chip{font:700 11px var(--tk-font);color:var(--tk-warn);border:1px dashed var(--tk-warn);border-radius:99px;padding:1px 8px;cursor:help}
.tkv-seg.sm button{height:24px;padding:0 9px;font-size:12px}.tkv-bd-h .tkv-seg{margin-left:auto}.tkv-bd-h .tkv-bd-k{margin-right:0}
.tkv-bd-gs{list-style:none;margin:12px 0 0;padding:0;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}.tkv-bd-gs li{display:flex;flex-wrap:wrap;align-items:center;gap:4px 10px;padding:8px 10px;border:1px solid var(--tk-line);border-radius:10px;font-size:13px}.tkv-bd-gs span{color:var(--tk-ink-2)}.tkv-bd-gs small{flex-basis:100%;color:var(--tk-muted)}
@media (max-width:1180px){.tkv{grid-template-columns:minmax(0,1fr) 360px}}
@media (max-width:980px){.tkv{display:block}.tkv-left{padding:12px 12px 110px}.tkv-bd-st,.tkv-bd-gs{grid-template-columns:1fr 1fr}.tkv-bd-cols{grid-template-columns:1fr}.tkv-xstep{margin-left:0}
 .tkv-right{position:fixed;left:0;right:0;bottom:0;top:auto;height:74px;z-index:40;border-left:0;border-top:1px solid var(--tk-line);border-radius:18px 18px 0 0;box-shadow:0 -8px 30px rgba(16,24,40,.14);transition:height .25s}
 .tkv-right.open{height:86vh}.tkv-stats{margin-left:0;width:100%;justify-content:space-between}.tkv-stats div{text-align:left}
 .tkv-row{grid-template-columns:26px minmax(0,1fr) auto}.tkv-row .tkv-rel{grid-column:2/4;grid-row:2}.tkv-car{display:none}.tkv-hold{padding-left:12px;overflow-x:auto}.tkv-filters{display:none}}
@media (max-width:760px){.tkv-scope{flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch;padding-bottom:8px}.tkv-sbs{flex-wrap:nowrap}.tkv-chips .tk-chip{white-space:normal;height:auto;min-height:26px;padding-top:3px;padding-bottom:3px}
 .tkv-body{height:auto;overflow-x:auto;-webkit-overflow-scrolling:touch}.tkv-body.chart{overflow:visible}.tkv-body>svg{width:auto;height:auto}.tkv-body.holders>svg{width:100%}
 .tkv-legend{position:sticky;left:0;bottom:auto;width:calc(100vw - 48px);padding:8px 0 10px 12px;margin-left:-12px}.tkv-body .tkv-stats{position:static;padding:10px 10px 0;max-width:none;justify-content:flex-start}.tkv-chart{height:340px}.tkv-tl-chart{height:150px}}
`;document.head.append(s);}
