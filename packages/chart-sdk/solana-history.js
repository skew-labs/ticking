import {createChartSDK,connectData} from './client.js';
import {abortable,historyClient} from './history-http.js';
const address=v=>typeof v==='string'&&/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(v);
const intervals=new Set([1,5,15,60,240,1440]);
const quoteSymbol=quote=>quote==='So11111111111111111111111111111111111111112'?'SOL':quote==='EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'?'USDC':quote;
export async function getSolanaHistoryCatalog({after='',limit=100,...options}={}){
 if(after&&!address(after)||!Number.isInteger(limit)||limit<1||limit>100)throw Error('Use a bounded Solana mint catalog cursor');
 const page=await historyClient(options)('assets',{after,limit});
 if(page.network!=='solana'||page.entireSolanaComplete!==false||page.liveReady!==false||!Array.isArray(page.assets)||page.assets.length>limit||typeof page.hasMore!=='boolean')throw Error('Invalid Solana retained catalog');
 let previous=after;for(const row of page.assets){if(!address(row.mint)||row.mint<=previous)throw Error('Invalid Solana catalog order');previous=row.mint}
 if(page.hasMore&&page.nextAfter!==previous||!page.hasMore&&page.nextAfter!==null)throw Error('Invalid Solana catalog cursor');return page;
}
export async function createSolanaHistoryDataFeed({token,pool,quoteMint,interval=1,onCoverage=()=>{},...options}={}){
 if(Boolean(token)===Boolean(pool)||!address(token||pool)||quoteMint!==undefined&&!address(quoteMint)||!intervals.has(interval))throw Error('Select one Solana mint or pool and supported interval');
 const parent=options.signal,lifecycle=new AbortController();parent?.throwIfAborted();
 const destroy=()=>{lifecycle.abort();parent?.removeEventListener('abort',destroy)};parent?.addEventListener('abort',destroy,{once:true});
 try{
  const get=historyClient({...options,signal:parent?AbortSignal.any([parent,lifecycle.signal]):lifecycle.signal});
  const selection=await get('resolve',{token,pool,quoteMint}),preferred=selection.preferredPool;
  if(selection.network!=='solana'||selection.mode!=='historical_finalized_rpc'||selection.liveReady!==false||selection.entireSolanaComplete!==false)throw Error('Unverified Solana historical source');
  if(selection.ready!==true||!preferred)throw Object.assign(Error('Solana history is not ready: '+selection.state),{state:selection.state,selection});
  if(!address(preferred.pool)||!address(preferred.baseMint)||!address(preferred.quoteMint)||!address(preferred.program)||preferred.baseMint===preferred.quoteMint||typeof preferred.inverted!=='boolean'||pool&&preferred.pool!==pool||token&&preferred.baseMint!==token||quoteMint&&preferred.quoteMint!==quoteMint||pool&&preferred.inverted)throw Error('Solana historical identity mismatch');
  const identity={network:'solana',market:preferred.pool,pool:preferred.pool,baseMint:preferred.baseMint,quoteMint:preferred.quoteMint,quoteSymbol:quoteSymbol(preferred.quoteMint),series:'trade',venue:preferred.program};let sequence=-1;
  selection.identity=identity;selection.delivery={state:'archive_only',live:false};
  return {identity,selection,destroy,
   async history(before,limit=300){
    if(lifecycle.signal.aborted)throw Error('Solana history feed closed');
    if(!Number.isInteger(limit)||limit<1||limit>600||before!==undefined&&(!Number.isSafeInteger(before)||before<=0))throw Error('Invalid bounded Solana history page');
    const page=await get('candles',{pool:identity.pool,interval,before,limit});
    const expectedBase=preferred.inverted?identity.quoteMint:identity.baseMint,expectedQuote=preferred.inverted?identity.baseMint:identity.quoteMint;
    if(page.identity?.network!=='solana'||page.identity.market!==identity.pool||page.identity.baseMint!==expectedBase||page.identity.quoteMint!==expectedQuote||page.identity.series!=='trade'||!Number.isSafeInteger(page.sequence)||page.sequence<0||page.sequence<sequence||!Array.isArray(page.candles)||page.candles.length>limit||typeof page.hasMore!=='boolean'||page.coverage?.source!=='finalized_rpc_receipts'||page.coverage.complete!==false||page.coverage.archiveCompleteness!=='not_attested'||page.coverage.shredJoinVerified!==false)throw Error('Solana retained page source, identity or cursor mismatch');
    let prior=0;
    const candles=page.candles.map(b=>{
     if(!Number.isSafeInteger(b.time)||b.time<=prior||b.time%(interval*60)||before!==undefined&&b.time+interval*60>before||![b.open,b.high,b.low,b.close,b.volume,b.baseVolume].every(Number.isFinite)||Math.min(b.open,b.low,b.close)<=0||b.low>Math.min(b.open,b.close)||b.high<Math.max(b.open,b.close)||b.volume<0||b.baseVolume<0||!Number.isSafeInteger(b.count)||b.count<1)throw Error('Invalid Solana historical candle');prior=b.time;
     const result=preferred.inverted?{...b,open:1/b.open,high:1/b.low,low:1/b.high,close:1/b.close,volume:b.baseVolume,baseVolume:b.volume}:{...b};
     if(![result.open,result.high,result.low,result.close].every(v=>Number.isFinite(v)&&v>0))throw Error('Inverted historical price exceeds finite range');return result;
    });
    if(page.nextBefore!==(candles[0]?.time??null)||page.hasMore&&!candles.length)throw Error('Invalid exclusive Solana history cursor');
    sequence=page.sequence;selection.coverage=page.coverage;try{onCoverage(page.coverage)}catch{}
    return {...page,candles,identity,delivery:selection.delivery,volumeUnit:identity.quoteSymbol,epoch:'solana-finalized-rpc-history:'+identity.pool};
   },subscribe(callback){if(lifecycle.signal.aborted||typeof callback!=='function')throw Error('Solana history feed closed or listener invalid');return ()=>{}}
  };
 }catch(error){destroy();throw error}
}
export async function createSolanaHistoryChart(container,{licenseProvider,publicKey,chart={},...options}={}){
 const deadline=options.requestTimeoutMs??10000;if(!Number.isInteger(deadline)||deadline<1000||deadline>30000)throw Error('Use a bounded chart attachment deadline');
 let sdk,feed,engine,disconnect=()=>{},closed=false,timer;const lifecycle=new AbortController();
 const destroy=()=>{if(closed)return;closed=true;clearTimeout(timer);lifecycle.abort(options.signal?.reason);disconnect();feed?.destroy();sdk?.destroy();options.signal?.removeEventListener('abort',destroy)};
 options.signal?.throwIfAborted();options.signal?.addEventListener('abort',destroy,{once:true});timer=setTimeout(()=>{lifecycle.abort(new DOMException('Chart attachment deadline exceeded','TimeoutError'));destroy()},deadline);
 try{
  const pending=createChartSDK({licenseProvider,publicKey,onLicenseError:e=>{if(!e.retryable)destroy()}}).then(value=>{if(closed){value.destroy();throw lifecycle.signal.reason}return value});
  sdk=await abortable(pending,lifecycle.signal);feed=await createSolanaHistoryDataFeed({...options,signal:lifecycle.signal,accessToken:()=>sdk.dataToken()});
  lifecycle.signal.throwIfAborted();engine=sdk.createChart(container,{...chart,interval:options.interval||1,quote:feed.identity.quoteSymbol,volumeUnit:feed.identity.quoteSymbol});disconnect=await connectData(engine,feed);
  if(closed){disconnect();throw Error('Solana historical attachment canceled')}engine.on('destroy',destroy);clearTimeout(timer);
  return {chart:engine,feed,identity:feed.identity,get coverage(){return feed.selection.coverage},destroy};
 }catch(error){destroy();throw error}
}
