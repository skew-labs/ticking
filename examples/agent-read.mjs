/** Bounded paid read; never creates an invoice, signs or transfers funds. */
const args=process.argv.slice(2),value=name=>args[args.indexOf(name)+1];
if(!args.includes('--allow-charge')||!args.includes('--market')||!args.includes('--idempotency-key'))throw Error('Require --market, --idempotency-key and explicit --allow-charge.');
const key=process.env.TICKING_API_KEY;if(!key)throw Error('Set a scoped server-side TICKING_API_KEY.');
const base=new URL(process.env.TICKING_API_ORIGIN||'https://api.ticking.dev');
if(base.protocol!=='https:'||base.username||base.password||base.search||base.hash)throw Error('Use an HTTPS API origin.');
const url=new URL('/api/v1/prepaid/price',base);url.searchParams.set('market',value('--market'));
const response=await fetch(url,{headers:{Authorization:'Bearer '+key,'Idempotency-Key':value('--idempotency-key')},signal:AbortSignal.timeout(10000)});
const raw=await response.text();if(raw.length>65536)throw Error('Response exceeded example bound.');
if(!response.ok)throw Error('Read failed with HTTP '+response.status+'; inspect account budget and source availability.');
console.log(JSON.stringify({chargedMicroUsdc:response.headers.get('x-ticking-charged-micro-usdc'),replayed:response.headers.get('x-ticking-replayed'),data:JSON.parse(raw)}));
