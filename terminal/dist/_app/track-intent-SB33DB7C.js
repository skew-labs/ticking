/** Guard for Muse / search resolve. One incident holds the wallet, the trail, and the market chart. */

export const TRACK_INTENT_SCHEMA='ticking.track-intent.v1';
export const TRACK_GUARD_TEXT='Perceive one incident. Plan wallet activity, the transaction trail, and the market chart together. Act with ticking.chartMarkers.track.v1 on that workspace. Solana, Arbitrum, and Robinhood Chain come first. A new mint paints price and the trail on that same chart. Dump and suspect are dump tags only. Fast Feed is latency paint only. Do not open Flow, a hub, a map, an address list, or investigate.';

const ADDR=/0x[a-fA-F0-9]{40}/;
const BASE58=/^[1-9A-HJ-NP-Za-km-z]{32,44}$/;
const TRACK_RE=/where did the money go|follow (?:the )?(?:money|hop)|wallet trail|trace (?:this )?(?:wallet|money)|track (?:this )?(?:wallet|money)|wallet activity|transaction trail|tx trail|flow of funds|whale trail|whale wallet|map this wallet|wallet map|investigate this wallet|on-?chain activity|돈(?:이)?\s*어디|자금\s*추적|어디로\s*갔|자금|지갑\s*활동/i;
const DUMP_RE=/\bdumps?\b|\bdumped\b|\bdumping\b|\bsuspect(?:ed)?\b|의심|덤프/i;
const FAST_RE=/\bfast\s*feed\b|\blatency(?:\s+paint)?\s+only\b/i;
const BARE_RE=/^(?:map|flow|wallet map)$/i;
const MINT_RE=/minted|minting|\bmints?\b|new\s+(?:token|mint|coin|pair)|just\s+listed|\blaunchpad\b|\blaunch(?:ed|ing)?\b|민트|신규\s*(?:토큰|민트)|런치(?:패드)?/i;
const MINT_SYMBOL_SKIP=/^(?:on|the|a|an|token|mint|coin|pair|pad|robinhood|solana|arbitrum|chain|chart)$/i;

function marketSymbol(text){
 if(/^(?:pump)(?:\s+chart)?$/i.test(text))return 'PUMP';
 if(/^(?:sol|solana)(?:\s+chart)?$/i.test(text))return 'SOL';
 if(/^(?:arb|arbitrum)(?:\s+chart)?$/i.test(text))return 'ARB';
 const named=/^(?:chart|price|market)\s+(?:of\s+)?([A-Za-z][A-Za-z0-9]{1,12})$/i.exec(text);
 return named?named[1].toUpperCase():null;
}

function chainHint(text){
 if(/robinhood|\brh\b/i.test(text))return 'robinhood';
 if(/arbitrum|\barb\b/i.test(text))return 'arbitrum';
 if(/solana|\bsol\b/i.test(text))return 'solana';
 return null;
}

export function mintAddress(value){
 const text=String(value??'').trim();
 if(ADDR.test(text))return text.toLowerCase();
 if(BASE58.test(text))return text;
 return null;
}

function mintSymbol(text){
 const re=/\b(?:mint(?:ed|ing)?|new|launch(?:ed|ing)?|launchpad|listed)\s+(?:token\s+|a\s+|the\s+)?([A-Za-z][A-Za-z0-9]{1,12})\b/gi;
 let match;
 while((match=re.exec(text))){
  if(!MINT_SYMBOL_SKIP.test(match[1]))return match[1].toUpperCase();
 }
 return null;
}

function mintChain(text,addr){
 const hinted=chainHint(text);
 if(hinted)return hinted;
 if(addr&&BASE58.test(addr))return 'solana';
 return null;
}

export function researchRoute({mode,wallet,chain,symbol,query,direct,mint}={}){
 if(mode==='mint'){
  if(mint){
   const id=encodeURIComponent('mint:'+mint);
   return `#/console/workspace/${id}${chain?`?chain=${encodeURIComponent(chain)}`:''}`;
  }
  if(symbol)return `#/console/workspace/${encodeURIComponent(symbol)}?launch=1${chain?`&chain=${encodeURIComponent(chain)}`:''}`;
  const params=new URLSearchParams();
  if(query)params.set('q',query);
  if(chain)params.set('chain',chain);
  const qs=params.toString();
  return `#/console/workspace/launch${qs?`?${qs}`:''}`;
 }
 if(mode==='market'&&symbol)return `#/console/workspace/${encodeURIComponent(symbol)}`;
 if(wallet){
  const id=encodeURIComponent('wallet:'+wallet);
  return `#/console/workspace/${id}${chain?`?chain=${encodeURIComponent(chain)}`:''}`;
 }
 if(direct&&query)return `#/console/workspace/auto?q=${encodeURIComponent(query)}`;
 return null;
}

export function classifyTrackIntent(query){
 const text=String(query??'').replace(/\s+/g,' ').trim().slice(0,400);
 const evm=(text.match(ADDR)||[])[0]?.toLowerCase()||null;
 const bareSol=BASE58.test(text)?text:null;
 const mintTalk=MINT_RE.test(text);
 const base58=!evm&&(text.match(/[1-9A-HJ-NP-Za-km-z]{32,44}/)||[])[0]||null;
 const mint=bareSol||(mintTalk?(evm||base58):null);
 const isMint=Boolean(bareSol)||mintTalk;
 const wallet=isMint?null:evm;
 const dump=DUMP_RE.test(text)&&!isMint;
 const fastOnly=FAST_RE.test(text)&&!wallet&&!dump&&!isMint;
 const symbol=isMint?mintSymbol(text):(!dump&&!fastOnly&&!wallet?marketSymbol(text):null);
 const bare=!isMint&&!dump&&!fastOnly&&!wallet&&!symbol&&BARE_RE.test(text);
 const track=!isMint&&!dump&&!fastOnly&&!symbol&&(TRACK_RE.test(text)||Boolean(wallet)||bare);
 const mode=isMint?'mint':dump?'dump':symbol?'market':track?'track':'other';
 const chain=mode==='mint'?mintChain(text,mint):mode==='track'&&wallet?chainHint(text):null;
 const intent={
  schema:TRACK_INTENT_SCHEMA,
  text:TRACK_GUARD_TEXT,
  mode,
  query:text,
  wallet,
  mint,
  chain,
  symbol,
  direct:bare,
  listAddresses:false,
  dumpTagsOnly:mode==='dump',
  perceive:mode==='mint'?'mint':mode==='market'?'market':mode==='other'?'ask':'track',
  plan:mode==='mint'?'paint the new token on the workspace chart':mode==='dump'?'dump tags on the workspace chart':mode==='market'?'open the market chart in this workspace':mode==='track'?(wallet?'paint hops on the workspace chart':'open one workspace for the question'):'leave the chart',
  act:mode==='mint'?'chart+track':mode==='dump'?'dump-tags':mode==='market'?'chart':mode==='track'?'chartMarkers':'none',
  forbid:['flow','hub','map','address-list','constellation','investigate'],
 };
 intent.route=researchRoute(intent);
 return intent;
}

export function trackResolveBody({query,wallet,chain,mode}={}){
 const spoken=String(query||`where did the money go from ${wallet||''}${chain?` on ${chain}`:''}`).trim();
 const intent=classifyTrackIntent(spoken);
 if(mode==='dump'||intent.mode==='dump'){
  intent.mode='dump';intent.dumpTagsOnly=true;intent.perceive='track';
  intent.plan='dump tags on the workspace chart';intent.act='dump-tags';
 }else if(mode==='track'||intent.mode==='track'){
  intent.mode='track';intent.perceive='track';
  intent.plan=intent.wallet?'paint hops on the workspace chart':'open one workspace for the question';intent.act='chartMarkers';
 }else if(mode==='mint'||intent.mode==='mint'){
  intent.mode='mint';intent.dumpTagsOnly=false;intent.perceive='mint';
  intent.plan='paint the new token on the workspace chart';intent.act='chart+track';
 }else if(mode==='market'||intent.mode==='market'){
  intent.mode='market';intent.perceive='market';
  intent.plan='open the market chart in this workspace';intent.act='chart';
 }
 intent.route=researchRoute(intent);
 const tracking=intent.mode==='track'||intent.mode==='dump'||intent.mode==='mint';
 return {query:spoken,track:tracking,listAddresses:false,mode:intent.mode,guard:intent};
}

export function guardedResolveBody(parsed){
 const intent=classifyTrackIntent(parsed?.query||'');
 if((intent.mode!=='track'&&intent.mode!=='dump'&&intent.mode!=='mint')||parsed?.guard)return null;
 return {...trackResolveBody({query:parsed.query}),...parsed,listAddresses:false,track:true,mode:intent.mode,guard:intent};
}
