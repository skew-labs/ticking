const settings=JSON.parse(document.querySelector('#ticking-settings').textContent);
document.querySelector('tick-chart')?.remove();
await import('./tick-embed.js');
let grant=settings.grant;
const provider=async()=>{if(grant.expires*1000>Date.now()+15000)return grant;const r=await fetch('/api/embeds/'+encodeURIComponent(settings.publishable)+'/renew',{method:'POST',headers:{Authorization:'Bearer '+grant.token,'Content-Type':'application/json'},body:'{}',credentials:'omit'});if(!r.ok)throw Error('Chart authorization expired.');return grant=await r.json();};
const chart=document.createElement('tick-chart');chart.setAttribute('market',settings.market);chart.setAttribute('aggregation',grant.aggregation);if(grant.mint)chart.setAttribute('mint',grant.mint);chart.setAttribute('theme',settings.theme);chart.setAttribute('tools','');chart.tokenProvider=provider;document.body.prepend(chart);
if(settings.trades){const {createTradeFeed,mountExecutionTape}=await import('./trade-feed.js');const feed=createTradeFeed({tokenProvider:provider});const stop=mountExecutionTape(document.querySelector('#tape'),feed);window.addEventListener('pagehide',()=>{stop();feed.destroy();},{once:true});}
