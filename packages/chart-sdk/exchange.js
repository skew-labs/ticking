import {createChartSDK,connectData} from './client.js';

const intervals=new Set([1,5,15,60,240,1440]);
const symbol=value=>typeof value==='string'&&value.length>0&&value.length<=40&&/^[\p{L}\p{N}_]+$/u.test(value);
const source=value=>['binance_spot','kraken_spot'].includes(value);
const abortable=(promise,signal)=>new Promise((resolve,reject)=>{
  signal.throwIfAborted();const finish=(fn,value)=>{signal.removeEventListener('abort',aborted);fn(value)},aborted=()=>finish(reject,signal.reason);
  signal.addEventListener('abort',aborted,{once:true});Promise.resolve(promise).then(value=>finish(resolve,value),error=>finish(reject,error));
});
function client({baseURL='',accessToken,fetcher=fetch,signal,requestTimeoutMs=10000}={}){
  if(typeof accessToken!=='function'||!Number.isInteger(requestTimeoutMs)||requestTimeoutMs<1000||requestTimeoutMs>30000)throw Error('Provide a data entitlement callback and bounded request deadline');
  const root=new URL(baseURL||globalThis.location?.origin||'http://localhost');
  if(root.username||root.password||root.search||root.hash||(root.protocol!=='https:'&&!(root.protocol==='http:'&&['localhost','127.0.0.1','[::1]'].includes(root.hostname))))throw Error('Use HTTPS or loopback for history requests; credentials belong in headers');
  return async(path,args)=>{
    signal?.throwIfAborted();const url=new URL('/sdk/exchange/'+path,root);
    for(const [key,value] of Object.entries(args))if(value!==undefined&&value!==null&&value!=='')url.searchParams.set(key,String(value));
    const timeout=AbortSignal.timeout(requestTimeoutMs),bounded=signal?AbortSignal.any([signal,timeout]):timeout;
    const token=await abortable(accessToken(),bounded);bounded.throwIfAborted();
    if(typeof token!=='string'||!token||token.length>8185)throw Error('Invalid data entitlement');
    const headers={Authorization:'Bearer '+token};if(globalThis.location?.origin)headers['X-Slotline-Origin']=globalThis.location.origin;
    const response=await abortable(fetcher(url,{headers,credentials:'omit',signal:bounded}),bounded);
    if(!response.ok)throw Object.assign(Error('Exchange history unavailable ('+response.status+')'),{status:response.status});
    const size=Number(response.headers?.get('content-length')||0);if(size>1024*1024)throw Error('Exchange history response exceeds the SDK bound');
    // Bound before JSON allocation, including chunked responses without a length.
    let text='';
    if(response.body?.getReader){const reader=response.body.getReader(),decoder=new TextDecoder();let bytes=0;
      try{while(true){const value=await abortable(reader.read(),bounded);if(value.done)break;bytes+=value.value.byteLength;if(bytes>1024*1024)throw Error('Exchange history response exceeds the SDK bound');text+=decoder.decode(value.value,{stream:true})}text+=decoder.decode()}
      finally{reader.cancel().catch(()=>{});reader.releaseLock()}
    }else {text=await abortable(response.text(),bounded);if(new TextEncoder().encode(text).byteLength>1024*1024)throw Error('Exchange history response exceeds the SDK bound')}
    return JSON.parse(text);
  };
}

export async function getExchangeCatalog({source:venue='binance_spot',asset,after='',query='',limit=100,...options}={}){
  if(!source(venue)||asset!==undefined&&!symbol(asset)||after&&!symbol(after)||typeof query!=='string'||query.length>40||!Number.isInteger(limit)||limit<1||limit>100)throw Error('Invalid bounded exchange catalog request');
  return client(options)(asset===undefined?'assets':'markets',{source:venue,asset,after,query,limit});
}

export async function createExchangeDataFeed({source:venue='binance_spot',market,asset,quote,interval=1,onCoverage=()=>{},...options}={}){
  if(!source(venue)||!intervals.has(interval)||Boolean(market)===Boolean(asset)||[market,asset,quote].some(v=>v!==undefined&&!symbol(v))||market&&quote)throw Error('Select one exchange market or asset and a supported interval');
  const lifecycle=new AbortController(),parent=options.signal;
  const destroy=()=>{lifecycle.abort();parent?.removeEventListener('abort',destroy)};
  parent?.throwIfAborted();parent?.addEventListener('abort',destroy,{once:true});
  const get=client({...options,signal:parent?AbortSignal.any([parent,lifecycle.signal]):lifecycle.signal});
  try{
    const selection=await get('resolve',{source:venue,market,asset,quote}),identity=selection.identity;
    if(identity?.network!=='exchange'||identity.venue!==venue||!symbol(identity.market)||identity.series!=='trade'||!symbol(identity.base)||!symbol(identity.quote)||identity.quoteSymbol!==identity.quote||identity.pool!=='exchange:'+venue+':'+identity.market||market&&identity.market!==market||asset&&identity.base!==asset||quote&&identity.quote!==quote||selection.volumeUnit!==identity.base||selection.delivery?.live!==false)throw Error('Exchange resolution changed source or units');
    let sequence=-1;
    return {identity,selection,destroy,
      async history(before,limit=300){
        if(lifecycle.signal.aborted)throw Error('Exchange data feed closed');
        if(!Number.isInteger(limit)||limit<1||limit>600||before!==undefined&&(!Number.isSafeInteger(before)||before<=0))throw Error('Invalid bounded history page');
        const page=await get('candles',{source:venue,market:identity.market,interval,before,limit});
        if(page.identity?.pool!==identity.pool||page.identity?.network!=='exchange'||page.source!==venue||page.volumeUnit!==identity.base||page.quoteVolumeUnit!==identity.quote||page.delivery?.live!==false||!Number.isSafeInteger(page.sequence)||page.sequence<sequence||!Array.isArray(page.candles)||page.candles.length>limit||typeof page.hasMore!=='boolean')throw Error('Exchange history identity, revision or bounds mismatch');
        let prior=0;for(const bar of page.candles){
          if(!Number.isSafeInteger(bar.time)||bar.time<=prior||bar.time%(interval*60)||before!==undefined&&bar.time+interval*60>before||![bar.open,bar.high,bar.low,bar.close,bar.volume,bar.baseVolume].every(Number.isFinite)||Math.min(bar.open,bar.low,bar.close)<=0||bar.low>Math.min(bar.open,bar.close)||bar.high<Math.max(bar.open,bar.close)||bar.volume<0||bar.baseVolume!==bar.volume||bar.quoteVolume!==null&&(!Number.isFinite(bar.quoteVolume)||bar.quoteVolume<0))throw Error('Invalid exchange candle or volume unit');prior=bar.time;
        }
        if(page.hasMore&&(!page.candles.length||page.nextBefore!==page.candles[0].time)||!page.hasMore&&page.nextBefore!==null)throw Error('Invalid exclusive history cursor');
        sequence=page.sequence;selection.coverage=page.coverage;try{onCoverage(page.coverage)}catch{}
        return page;
      },
      // This retained-history adapter has no live trade stream. Chart recovery
      // and older-page loading still use the generic SDK's bounded history path.
      subscribe(callback){if(lifecycle.signal.aborted||typeof callback!=='function')throw Error('Exchange data feed closed or invalid listener');return ()=>{}}
    };
  }catch(error){destroy();throw error}
}

export async function createExchangeChart(container,{licenseProvider,publicKey,chart={},...options}={}){
  const deadline=options.requestTimeoutMs??10000;
  if(!Number.isInteger(deadline)||deadline<1000||deadline>30000)throw Error('Provide a bounded chart attachment deadline');
  const lifecycle=new AbortController();let sdk,feed,engine,disconnect=()=>{},closed=false,timer;
  const destroy=()=>{if(closed)return;closed=true;clearTimeout(timer);lifecycle.abort(options.signal?.reason);disconnect();feed?.destroy();sdk?.destroy();options.signal?.removeEventListener('abort',destroy)};
  options.signal?.throwIfAborted();options.signal?.addEventListener('abort',destroy,{once:true});
  timer=setTimeout(()=>{lifecycle.abort(new DOMException('Chart attachment deadline exceeded','TimeoutError'));destroy()},deadline);
  try{
    const pending=createChartSDK({licenseProvider,publicKey,onLicenseError:error=>{if(!error.retryable)destroy()}}).then(value=>{if(closed){value.destroy();throw lifecycle.signal.reason}return value});
    sdk=await abortable(pending,lifecycle.signal);
    if(closed){sdk.destroy();throw Error('Exchange chart attachment canceled')}
    feed=await createExchangeDataFeed({...options,signal:lifecycle.signal,accessToken:()=>sdk.dataToken()});
    if(closed){feed.destroy();throw Error('Exchange chart attachment canceled')}
    engine=sdk.createChart(container,{...chart,interval:options.interval||1,quote:feed.identity.quoteSymbol,volumeUnit:feed.identity.base});
    disconnect=await connectData(engine,feed);if(closed){disconnect();throw Error('Exchange chart attachment canceled')}engine.on('destroy',destroy);clearTimeout(timer);
    return {chart:engine,feed,identity:feed.identity,get coverage(){return feed.selection.coverage},destroy};
  }catch(error){destroy();throw error}
}
