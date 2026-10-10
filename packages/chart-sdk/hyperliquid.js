/** Own-node retained history + tape/book reads. Live push is not certified yet. */
import {connectData} from './client.js';
const validMarket=m=>typeof m==='string'&&/^[A-Za-z0-9_@:-]{1,64}$/.test(m);
const intervals=new Set([1,3,5,15,30,60,120,240,360,480,720,1440,4320,10080]);
export async function createHyperliquidNodeAdapter({market,interval=1,baseURL,accessToken,fetcher=fetch,signal}={}){
  if(!validMarket(market)||!intervals.has(interval)||typeof accessToken!=='function')throw Error('Provide an explicit node market, interval and data entitlement');
  const root=new URL(baseURL||globalThis.location?.origin||'http://localhost');
  if(root.username||root.password||root.search||root.hash||!(root.protocol==='https:'||root.protocol==='http:'&&['localhost','127.0.0.1','[::1]'].includes(root.hostname)))throw Error('HTTPS or loopback required');
  const lifecycle=new AbortController();const cancel=()=>lifecycle.abort();signal?.throwIfAborted();signal?.addEventListener('abort',cancel,{once:true});
  let identity,revision=-1;
  const get=async(kind,params={})=>{
    const bounded=AbortSignal.any([lifecycle.signal,AbortSignal.timeout(10000)]);
    const wait=promise=>new Promise((resolve,reject)=>{bounded.throwIfAborted();const abort=()=>done(reject,bounded.reason),done=(fn,v)=>{bounded.removeEventListener('abort',abort);fn(v)};bounded.addEventListener('abort',abort,{once:true});Promise.resolve(promise).then(v=>done(resolve,v),e=>done(reject,e));});
    const token=await wait(accessToken());if(typeof token!=='string'||!token||token.length>8185)throw Error('Invalid entitlement');
    const url=new URL('/sdk/hyperliquid/'+kind,root);for(const[k,v]of Object.entries({market,...params}))if(v!==undefined)url.searchParams.set(k,String(v));
    const response=await wait(fetcher(url,{headers:{Authorization:'Bearer '+token},credentials:'omit',signal:bounded}));
    if(!response.ok)throw Error('Own-node source unavailable ('+response.status+')');
    if(Number(response.headers?.get('content-length')||0)>1024*1024)throw Error('Node response bound');
    let text='';
    if(response.body?.getReader){const reader=response.body.getReader(),decoder=new TextDecoder();let size=0;try{while(true){const r=await wait(reader.read());if(r.done)break;size+=r.value.byteLength;if(size>1024*1024)throw Error('Node response bound');text+=decoder.decode(r.value,{stream:true})}text+=decoder.decode()}finally{reader.cancel().catch(()=>{});reader.releaseLock()}}
    else{text=await wait(response.text());if(new TextEncoder().encode(text).length>1024*1024)throw Error('Node response bound')}
    const page=JSON.parse(text),id=page.identity;
    if(page.source!=='hyperliquid_node'||id?.network!=='hyperliquid'||id.venue!=='hyperliquid_node'||id.market!==market||id.series!=='trade'||!['Mainnet','Testnet'].includes(id.chain)||id.pool!==`hyperliquid:${id.chain}:${market}`||!['spot','perpetual'].includes(id.marketType)||page.volumeUnit!==id.base||page.quoteVolumeUnit!==id.quote||identity&&JSON.stringify(id)!==JSON.stringify(identity)||!Number.isSafeInteger(page.sequence)||page.sequence<0||page.sequence<revision)throw Error('Own-node identity/revision changed');
    identity??=id;revision=page.sequence;return page;
  };
  const history=async(before,limit=300)=>{
    if(!Number.isInteger(limit)||limit<1||limit>600||before!==undefined&&(!Number.isSafeInteger(before)||before<=0))throw Error('History bounds');
    const page=await get('candles',{interval,before,limit});if(!Array.isArray(page.candles)||page.candles.length>limit||typeof page.hasMore!=='boolean')throw Error('History shape');
    let last=0;const step=interval*60,offset=interval===10080?345600:0;
    for(const b of page.candles){if(!Number.isSafeInteger(b.time)||b.time<=last||(b.time-offset)%step||before!==undefined&&b.time+step>before||![b.open,b.high,b.low,b.close,b.volume,b.baseVolume,b.quoteVolume].every(Number.isFinite)||b.low<=0||b.low>Math.min(b.open,b.close)||b.high<Math.max(b.open,b.close)||b.volume<0||b.baseVolume!==b.volume||b.quoteVolume<0||!Number.isSafeInteger(b.count)||b.count<1)throw Error('Invalid executed candle');last=b.time;}
    if(page.hasMore?(!page.candles.length||page.nextBefore!==page.candles[0].time):page.nextBefore!==null)throw Error('History cursor');return page;
  };
  const destroy=()=>{cancel();signal?.removeEventListener('abort',cancel)};
  try{const first=await history(undefined,1);return {identity,selection:first,history,
    trades:({before,limit=300}={})=>{if(!Number.isSafeInteger(limit)||limit<1||limit>600||before!==undefined&&(!Number.isSafeInteger(before)||before<1))throw Error('Trade page bounds');return get('trades',{before,limit})},
    book:()=>get('book'),destroy,
    // No polling loop masquerading as the executed-node live stream. Use this
    // retained-history adapter with the existing chart until push is accepted.
    subscribe(callback){if(typeof callback!=='function'||lifecycle.signal.aborted)throw Error('Closed adapter');return ()=>{}}
  }}catch(e){destroy();throw e}
}
export async function connectHyperliquidNodeChart(chart,options){const feed=await createHyperliquidNodeAdapter(options);try{const disconnect=await connectData(chart,feed);return {feed,destroy(){disconnect();feed.destroy()}}}catch(e){feed.destroy();throw e}}
