/** Read the first-party pool discovery state using the same browser entitlement.
 * The host application chooses its waiting/loading UI. No price is fabricated.
 */
export const discoveryStates=['registered','awaiting_first_verified_execution','discovery_pending','awaiting_pool_validation','adapter_required','quote_unavailable','capacity_limited'];
export function validateDiscovery(value,address){
  if(!value||value.address!==address||!discoveryStates.includes(value.state)||!Array.isArray(value.pools)||value.entireSolanaComplete!==false)throw Error('Invalid discovery response');
  return value;
}
export async function getSolanaCoverage({pool,token,quoteMint,baseURL=globalThis.location?.origin,accessToken,fetcher=fetch,signal}={}){
  const address=value=>/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(value||'');
  if(Boolean(pool)===Boolean(token)||!address(pool||token)||(quoteMint&&!address(quoteMint))||typeof accessToken!=='function')throw Error('Select one Solana address and a browser entitlement callback');
  signal?.throwIfAborted();
  const endpoint=new URL(baseURL);
  if(endpoint.username||endpoint.password||endpoint.search||endpoint.hash||(endpoint.protocol!=='https:'&&!(endpoint.protocol==='http:'&&['localhost','127.0.0.1'].includes(endpoint.hostname))))throw Error('Use a trusted HTTPS chart data endpoint');
  const secret=await accessToken();if(typeof secret!=='string'||secret.startsWith('mgl_')||secret.length>8192||secret.split('.').length!==3)throw Error('Use an origin-bound browser entitlement');
  const url=new URL('/market/sdk/coverage',endpoint);url.searchParams.set(pool?'pool':'token',pool||token);
  if(quoteMint)url.searchParams.set('quoteMint',quoteMint);
  const response=await fetcher(url,{headers:{Authorization:'Bearer '+secret,...(globalThis.location?.origin?{'X-Slotline-Origin':globalThis.location.origin}:{})},credentials:'omit',signal:signal?AbortSignal.any([signal,AbortSignal.timeout(15000)]):AbortSignal.timeout(15000)});
  if(!response.ok)throw Error('Discovery request failed ('+response.status+')');
  return validateDiscovery(await response.json(),pool||token);
}
