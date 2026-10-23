/** Guard for Muse / search resolve. One incident holds the wallet, the trail, and the market chart. */

export const TRACK_INTENT_SCHEMA='ticking.track-intent.v1';
export const TRACK_GUARD_TEXT='Perceive one incident. Plan wallet activity, the transaction trail, and the market chart together. Act with ticking.chartMarkers.track.v1 on that workspace. Solana, Arbitrum, and Robinhood Chain come first. Dump and suspect are dump tags only. Fast Feed is latency paint only. Do not open Flow, a hub, a map, an address list, or investigate.';

const ADDR=/0x[a-fA-F0-9]{40}/;
const TRACK_RE=/where did the money go|follow (?:the )?(?:money|hop)|wallet trail|trace (?:this )?(?:wallet|money)|track (?:this )?(?:wallet|money)|wallet activity|transaction trail|tx trail|flow of funds|whale trail|whale wallet|map this wallet|wallet map|investigate this wallet|on-?chain activity|돈(?:이)?\s*어디|자금\s*추적|어디로\s*갔|자금|지갑\s*활동/i;
const DUMP_RE=/\bdumps?\b|\bdumped\b|\bdumping\b|\bsuspect(?:ed)?\b|의심|덤프/i;
const FAST_RE=/\bfast\s*feed\b|\blatency(?:\s+paint)?\s+only\b/i;
const BARE_RE=/^(?:map|flow|wallet map)$/i;

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

export function researchRoute({mode,wallet,chain,symbol,query,direct}={}){
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
 const wallet=(text.match(ADDR)||[])[0]?.toLowerCase()||null;
 const dump=DUMP_RE.test(text);
 const fastOnly=FAST_RE.test(text)&&!wallet&&!dump;
 const symbol=!dump&&!fastOnly&&!wallet?marketSymbol(text):null;
 const bare=!dump&&!fastOnly&&!wallet&&!symbol&&BARE_RE.test(text);
 const track=!dump&&!fastOnly&&!symbol&&(TRACK_RE.test(text)||Boolean(wallet)||bare);
 const mode=dump?'dump':symbol?'market':track?'track':'other';
 const chain=mode==='track'&&wallet?chainHint(text):null;
 const intent={
  schema:TRACK_INTENT_SCHEMA,
  text:TRACK_GUARD_TEXT,
  mode,
  query:text,
  wallet,
  chain,
  symbol,
  direct:bare,
  listAddresses:false,
  dumpTagsOnly:mode==='dump',
  perceive:mode==='market'?'market':mode==='other'?'ask':'track',
  plan:mode==='dump'?'dump tags on the workspace chart':mode==='market'?'open the market chart in this workspace':mode==='track'?(wallet?'paint hops on the workspace chart':'open one workspace for the question'):'leave the chart',
  act:mode==='dump'?'dump-tags':mode==='market'?'chart':mode==='track'?'chartMarkers':'none',
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
 }else if(mode==='market'||intent.mode==='market'){
  intent.mode='market';intent.perceive='market';
  intent.plan='open the market chart in this workspace';intent.act='chart';
 }
 intent.route=researchRoute(intent);
 const tracking=intent.mode==='track'||intent.mode==='dump';
 return {query:spoken,track:tracking,listAddresses:false,mode:intent.mode,guard:intent};
}

export function guardedResolveBody(parsed){
 const intent=classifyTrackIntent(parsed?.query||'');
 if((intent.mode!=='track'&&intent.mode!=='dump')||parsed?.guard)return null;
 return {...trackResolveBody({query:parsed.query}),...parsed,listAddresses:false,track:true,mode:intent.mode,guard:intent};
}
