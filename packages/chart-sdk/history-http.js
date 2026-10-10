/** Bounded signed browser history reads. No private server keys in embeds. */
export const abortable=(promise,signal)=>new Promise((resolve,reject)=>{
 signal.throwIfAborted();const finish=(fn,value)=>{signal.removeEventListener('abort',aborted);fn(value)},aborted=()=>finish(reject,signal.reason);
 signal.addEventListener('abort',aborted,{once:true});Promise.resolve(promise).then(value=>finish(resolve,value),error=>finish(reject,error));
});
export function historyClient({baseURL=globalThis.location?.origin,accessToken,fetcher=fetch,signal,requestTimeoutMs=10000}={}){
 if(typeof accessToken!=='function'||!Number.isInteger(requestTimeoutMs)||requestTimeoutMs<1000||requestTimeoutMs>30000)throw Error('Provide a signed data entitlement and request deadline');
 const root=new URL(baseURL);
 if(root.username||root.password||root.search||root.hash||(root.protocol!=='https:'&&!(root.protocol==='http:'&&['localhost','127.0.0.1','[::1]'].includes(root.hostname))))throw Error('Use trusted HTTPS or loopback for retained history');
 return async(path,args)=>{
  signal?.throwIfAborted();const timeout=AbortSignal.timeout(requestTimeoutMs),bounded=signal?AbortSignal.any([signal,timeout]):timeout;
  const token=await abortable(accessToken(),bounded);
  if(typeof token!=='string'||token.startsWith('mgl_')||token.length>8185||token.split('.').length!==3)throw Error('Use a signed browser entitlement, never a server API key');
  const url=new URL('/sdk/archive/'+path,root);for(const [k,v] of Object.entries(args))if(v!==undefined&&v!==null&&v!=='')url.searchParams.set(k,String(v));
  const headers={Authorization:'Bearer '+token};if(globalThis.location?.origin)headers['X-Slotline-Origin']=globalThis.location.origin;
  const response=await abortable(fetcher(url,{headers,credentials:'omit',signal:bounded}),bounded);
  if(!response.ok)throw Object.assign(Error('Solana retained history unavailable ('+response.status+')'),{status:response.status});
  if(Number(response.headers?.get('content-length')||0)>1024*1024)throw Error('Retained history response exceeds SDK bound');
  let text='';
  if(response.body?.getReader){const reader=response.body.getReader(),decoder=new TextDecoder();let bytes=0;
   try{while(true){const value=await abortable(reader.read(),bounded);if(value.done)break;bytes+=value.value.byteLength;if(bytes>1024*1024)throw Error('Retained history response exceeds SDK bound');text+=decoder.decode(value.value,{stream:true})}text+=decoder.decode()}
   finally{reader.cancel().catch(()=>{});reader.releaseLock()}
  }else{text=await abortable(response.text(),bounded);if(new TextEncoder().encode(text).byteLength>1024*1024)throw Error('Retained history response exceeds SDK bound')}
  return JSON.parse(text);
 };
}
