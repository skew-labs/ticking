/** Node/server helper. One shared domain token; never bundle this into a browser. */
export function createLicenseBroker({endpoint,apiKey,origin,fetcher=fetch}){
  if(typeof document!=='undefined')throw Error('The license broker belongs on your backend');
  const url=new URL(endpoint),site=new URL(origin);
  if(url.username||url.password||url.hash||url.search||(url.protocol!=='https:'&&!(url.protocol==='http:'&&['localhost','127.0.0.1'].includes(url.hostname))))throw Error('Use a trusted HTTPS license endpoint');
  if(typeof apiKey!=='string'||!apiKey.startsWith('mgl_'))throw Error('Configure a server API key');
  if(site.pathname!=='/'||site.search||site.hash||site.username||site.password||(site.protocol!=='https:'&&!(site.protocol==='http:'&&['localhost','127.0.0.1'].includes(site.hostname))))throw Error('Use an exact allowed frontend origin');
  let cached=null,pending=null;
  return {
    async license(){
      if(cached&&cached.expiresAt>Date.now()/1000+120)return {license:cached.license};
      if(!pending)pending=(async()=>{const response=await fetcher(url.href,{method:'POST',headers:{Authorization:'Bearer '+apiKey,'Content-Type':'application/json'},body:JSON.stringify({origin:site.origin}),signal:AbortSignal.timeout(15000)});if(!response.ok)throw Error('SDK license unavailable');const result=await response.json();if(typeof result.license!=='string'||!Number.isFinite(result.expiresAt)||result.expiresAt<=Date.now()/1000)throw Error('Invalid SDK license response');cached={license:result.license,expiresAt:result.expiresAt};return {license:cached.license}})().finally(()=>{pending=null});
      return pending;
    },
    clear(){cached=null}
  };
}
