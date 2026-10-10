const decode=value=>Uint8Array.from(atob(value.replace(/-/g,'+').replace(/_/g,'/')+'='.repeat((4-value.length%4)%4)),c=>c.charCodeAt(0));
export async function verifyLicense(token,publicKey,website,now=Date.now()/1000){
  try{
    if(typeof token!=='string'||token.length>12000||publicKey.algorithm!=='Ed25519')throw Error();
    const [header,payload,signature,...extra]=token.split('.');if(extra.length||!signature)throw Error();
    const h=JSON.parse(new TextDecoder().decode(decode(header))),claims=JSON.parse(new TextDecoder().decode(decode(payload)));
    if(h.alg!=='EdDSA'||h.typ!=='JWT'||h.kid!==publicKey.kid)throw Error();
    const key=await crypto.subtle.importKey('raw',decode(publicKey.key),{name:'Ed25519'},false,['verify']);
    if(!await crypto.subtle.verify('Ed25519',key,decode(signature),new TextEncoder().encode(header+'.'+payload)))throw Error();
    if(claims.iss!=='mingle-chart-sdk'||claims.aud!=='chart-embed'||claims.origin!==website||!Number.isFinite(claims.exp)||claims.exp<=now||claims.iat>now+60||claims.exp-claims.iat>3600||!Number.isInteger(claims.maxCharts)||claims.maxCharts<1||claims.maxCharts>4||!Array.isArray(claims.features)||!/^\w{24}$/.test(claims.project))throw Error();
    return Object.freeze({...claims,features:Object.freeze([...claims.features])});
  }catch{throw Error('Invalid, expired, or unauthorized chart license')}
}
