/** SLOTLINE's server data contract. Only executed results, not JIT quote intent.
 * Browser callers use a public/short-lived data entitlement, never a provider key.
 * The current native service supports registered 1-minute market candles.
 */
export async function createSolanaMarketAdapter({market,baseURL=location.origin,interval=1,fetcher=fetch,eventSource=EventSource}={}) {
  if(!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(market||'')||interval!==1)throw Error('Provide a registered Solana market address and 1m interval');
  const endpoint=new URL(baseURL);if(endpoint.protocol!=='https:'&&endpoint.hostname!=='localhost'&&endpoint.hostname!=='127.0.0.1')throw Error('Chart data requires HTTPS');
  const query=(path,values={})=>{const url=new URL('/market/engine/'+path,endpoint);url.searchParams.set('market',market);url.searchParams.set('interval',String(interval));for(const [k,v] of Object.entries(values))if(v!==undefined)url.searchParams.set(k,String(v));return url};
  let identity;
  const history=async(before,limit=300,{minSequence=0}={})=>{const response=await fetcher(query('candles',{before,limit,minSeq:minSequence}));const page=await response.json();if(!response.ok)throw Error(page.error||'Native market history unavailable');
    const actual=page.identity;if(!actual||actual.market!==market||actual.network!=='solana'||actual.series!=='trade')throw Error('Native market identity mismatch');
    if(identity&&(identity.baseMint!==actual.baseMint||identity.quoteMint!==actual.quoteMint||identity.program!==actual.program))throw Error('Native market identity changed');identity=actual;return page;};
  await history(undefined,1);
  return {identity:{...identity,pool:market},history,subscribe(callback,{afterSequence=0,onGap=()=>Promise.resolve(false)}={}){let closed=false,recovering=false;const source=new eventSource(query('stream',{after:afterSequence}));
    const recover=async()=>{if(closed||recovering)return;recovering=true;try{await onGap()}finally{recovering=false}};
    source.addEventListener('candles',event=>{if(!closed)callback(JSON.parse(event.data))});source.addEventListener('resync',recover);source.onerror=recover;return ()=>{closed=true;source.close()};}};
}
