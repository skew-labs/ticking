/** Guard for Muse / search resolve. Track paints hops. Dump stays dump tags. */

export const TRACK_INTENT_SCHEMA='ticking.track-intent.v1';
export const TRACK_GUARD_TEXT='Perceive track. Plan hops on one workspace chart. Act with ticking.chartMarkers.track.v1. Dump and suspect are dump tags only. Do not open Flow, a hub, a map, or an address list.';

const ADDR=/0x[a-fA-F0-9]{40}/;
const TRACK_RE=/where did the money go|follow (?:the )?(?:money|hop)|wallet trail|trace (?:this )?(?:wallet|money)|track (?:this )?(?:wallet|money)|돈(?:이)?\s*어디|자금\s*추적|어디로\s*갔/i;
const DUMP_RE=/\bdumps?\b|\bdumped\b|\bdumping\b|\bsuspect(?:ed)?\b|의심|덤프/i;

export function classifyTrackIntent(query){
 const text=String(query??'').replace(/\s+/g,' ').trim().slice(0,400);
 const wallet=(text.match(ADDR)||[])[0]?.toLowerCase()||null;
 const dump=DUMP_RE.test(text);
 const track=!dump&&(TRACK_RE.test(text)||Boolean(wallet));
 const mode=dump?'dump':track?'track':'other';
 return {
  schema:TRACK_INTENT_SCHEMA,
  text:TRACK_GUARD_TEXT,
  mode,
  wallet,
  listAddresses:false,
  dumpTagsOnly:mode==='dump',
  route:wallet?`#/console/workspace/${encodeURIComponent('wallet:'+wallet)}`:null,
  perceive:mode==='other'?'ask':'track',
  plan:mode==='dump'?'dump tags on the workspace chart':mode==='track'?'paint hops on the workspace chart':'leave the chart',
  act:mode==='dump'?'dump-tags':mode==='track'?'chartMarkers':'none',
  forbid:['flow','hub','map','address-list','constellation'],
 };
}

export function trackResolveBody({query,wallet,chain,mode}={}){
 const spoken=String(query||`where did the money go from ${wallet||''}${chain?` on ${chain}`:''}`).trim();
 const intent=classifyTrackIntent(spoken);
 if(mode==='dump'||intent.mode==='dump'){
  intent.mode='dump';intent.dumpTagsOnly=true;intent.perceive='track';
  intent.plan='dump tags on the workspace chart';intent.act='dump-tags';
 }else if(mode==='track'||intent.mode==='track'){
  intent.mode='track';intent.perceive='track';
  intent.plan='paint hops on the workspace chart';intent.act='chartMarkers';
 }
 return {query:spoken,track:intent.mode!=='other',listAddresses:false,mode:intent.mode,guard:intent};
}
